"""Case-scoped read/prepare tools backed by Proofie's configured local DB."""
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from copilot import config
from copilot.db import repo

ROOT = Path(__file__).resolve().parents[3]
OWNED_ROOT = ROOT / "data" / "mejoras10" / "06"      # the default; owned_root() follows COPILOT_DATA_DIR when it is set
DEFAULT_REGISTRY = OWNED_ROOT / "registry.json"
DEFAULT_AUDIT = OWNED_ROOT / "audit.jsonl"


def owned_root():
    """The folder this server may read and write: the configured data folder's MCP subfolder (06), read on every call."""
    if config.data_dir() == config.DATA_DIR:
        return OWNED_ROOT
    return config.data_dir() / "mejoras10" / "06"
TOOLS = {"list_local_cases", "read_local_case", "prepare_local_note"}
MAX_REGISTRY_BYTES = 2_000_000
MAX_AUDIT_BYTES = 10_000_000
MAX_CASES = 500


def _is_linked_storage(path):
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _owned_path(value, label):
    if not isinstance(value, (str, os.PathLike)):
        raise PermissionError(f"{label} path must be local.")
    raw = Path(value)
    if ".." in raw.parts:
        raise PermissionError(f"{label} path traversal is not allowed.")
    owned = owned_root()
    candidate = raw if raw.is_absolute() else owned / raw
    root = owned.absolute()
    try:
        current = root.parents[2]       # the folder that holds the data folder; every part below it is checked for links
        for part in root.relative_to(current).parts:
            current = current / part
            if _is_linked_storage(current):
                raise PermissionError(f"{label} path contains linked storage.")
        relative = candidate.absolute().relative_to(root)
        current = root
        for part in relative.parts:
            current = current / part
            if _is_linked_storage(current):
                raise PermissionError(f"{label} path contains linked storage.")
        resolved_root, resolved = root.resolve(), candidate.resolve(strict=False)
    except PermissionError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise PermissionError(f"{label} path must remain inside the MCP local data folder.") from exc
    if resolved_root not in resolved.parents:
        raise PermissionError(f"{label} path must remain inside the MCP local data folder.")
    return candidate


def _registry(registry_path=None):
    path = _owned_path(registry_path or os.environ.get("PROOFIE_MCP_LOCAL_REGISTRY", owned_root() / "registry.json"), "Registry")
    if not path.exists():
        return {"actor_cases": {}}
    if path.stat().st_size > MAX_REGISTRY_BYTES:
        raise PermissionError("Local actor allowlist exceeds 2 MB; refusing access.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        raise PermissionError("Local actor allowlist unreadable or invalid; refusing access.") from exc
    if not isinstance(value, dict) or set(value) != {"actor_cases"} or not isinstance(value["actor_cases"], dict) or len(value["actor_cases"]) > 100:
        raise PermissionError("Local actor allowlist schema invalid; refusing access.")
    total = 0
    for actor, ids in value["actor_cases"].items():
        if (not isinstance(actor, str) or not actor or len(actor) > 100 or not isinstance(ids, list)
                or len(ids) > MAX_CASES or any(not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", cid) for cid in ids)
                or len(ids) != len(set(ids))):
            raise PermissionError("Local actor-to-case authorization is invalid.")
        total += len(ids)
    if total > MAX_CASES:
        raise PermissionError("Local actor allowlist exceeds 500 case references.")
    return value


def _artifact_case(case_id, db_path=None):
    db_path = db_path or config.db_path()
    documents = repo.list_documents(db_path, case_id)
    packages = repo.list_packages(db_path, case_id)
    if not documents and not packages:
        raise PermissionError("No local Proofie artifacts exist for this authorized case.")
    return {
        "case_id": case_id,
        "source": "Proofie local package/document records; provider state not asserted",
        "documents": [{k: d.get(k) for k in ("id", "filename", "content_type", "size_bytes", "sha256", "extraction_ok", "excluded")}
                      for d in documents],
        "packages": [{k: p.get(k) for k in ("id", "version", "source", "created_at", "effective_next_step", "validation_ok")}
                     for p in packages],
    }


def _case_dir(case_id):
    if not isinstance(case_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", case_id):
        raise ValueError("Case ID is invalid.")
    digest = hashlib.sha256(case_id.encode("utf-8")).hexdigest()
    return _owned_path(owned_root() / "cases" / digest, "Case note storage")


def notes_for_case(case_id):
    directory = _case_dir(case_id)
    path = _owned_path(directory / "prepared-notes.json", "Case note storage")
    if not path.exists():
        return []
    if path.stat().st_size > 250_000:
        raise PermissionError("Prepared local notes exceed 250 KB; refusing access.")
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        raise PermissionError("Prepared local notes are invalid; refusing access.") from exc
    if not isinstance(values, list) or len(values) > 100 or any(not isinstance(x, dict) or set(x) != {"created_at", "note", "status"} for x in values):
        raise PermissionError("Prepared local notes schema invalid; refusing access.")
    return values


def _save_note(case_id, note):
    directory = _case_dir(case_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = _owned_path(directory / "prepared-notes.json", "Case note storage")
    rows = notes_for_case(case_id)
    if len(rows) >= 100:
        raise PermissionError("Prepared local note history is full.")
    rows.append({"created_at": datetime.now(timezone.utc).isoformat(), "note": note, "status": "prepared locally; not sent"})
    encoded = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 250_000:
        raise PermissionError("Prepared local notes exceed 250 KB; refusing write.")
    temporary = _owned_path(directory / "prepared-notes.json.tmp", "Case note storage")
    temporary.write_text(encoded, encoding="utf-8")
    temporary.replace(path)
    return rows[-1]


def _audit(name, args, actor, outcome, audit_path=None):
    path = _owned_path(audit_path or os.environ.get("PROOFIE_MCP_LOCAL_AUDIT", owned_root() / "audit.jsonl"), "Audit")
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = {"case_id": args.get("case_id")} if isinstance(args.get("case_id"), str) else {}
    note = args.get("note")
    if isinstance(note, str):
        safe["note_length"] = len(note)
        safe["note_sha256_prefix"] = hashlib.sha256(note[:1001].encode()).hexdigest()
    row = {"time": datetime.now(timezone.utc).isoformat(), "tool": str(name)[:100], "args": safe,
           "actor": str(actor)[:100], "result": outcome}
    encoded = json.dumps(row, ensure_ascii=False) + "\n"
    if path.exists() and path.stat().st_size + len(encoded.encode("utf-8")) > MAX_AUDIT_BYTES:
        raise PermissionError("Local MCP audit exceeds 10 MB; refusing further writes.")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def invoke(name, args, actor="local-user", *, registry_path=None, audit_path=None, db_path=None):
    try:
        if not isinstance(name, str) or name not in TOOLS:
            raise PermissionError("Tool is not allowed")
        if not isinstance(args, dict):
            raise ValueError("Arguments must be an object")
        registry = _registry(registry_path)
        if not isinstance(actor, str) or not actor:
            raise PermissionError("Server actor is not configured")
        allowed_cases = registry["actor_cases"].get(actor, [])
        if name == "list_local_cases":
            if args:
                raise ValueError("Unexpected arguments")
            result = []
            for cid in allowed_cases:
                try:
                    artifact = _artifact_case(cid, db_path)
                except PermissionError:
                    continue
                result.append({"case_id": cid, "artifact_count": len(artifact["documents"]) + len(artifact["packages"])})
        else:
            cid = args.get("case_id")
            if not isinstance(cid, str) or len(cid) > 100 or cid not in allowed_cases:
                raise PermissionError("Actor is not authorized for this local case")
            artifact = _artifact_case(cid, db_path)
            if name == "read_local_case":
                if set(args) != {"case_id"}:
                    raise ValueError("Unexpected or missing arguments")
                result = artifact
            else:
                if set(args) != {"case_id", "note"} or not isinstance(args["note"], str) or not args["note"].strip() or len(args["note"]) > 1000:
                    raise ValueError("Strict argument schema requires case_id and note <=1000 chars")
                saved = _save_note(cid, args["note"])
                result = {"case_id": cid, **saved, "source": artifact["source"]}
        _audit(name, args, actor, "handled", audit_path)
        return result
    except Exception:
        _audit(name, args if isinstance(args, dict) else {}, actor, "refused", audit_path)
        raise


def audit_protocol_refusal(name, args, actor=None):
    _audit(name, args if isinstance(args, dict) else {}, actor or os.environ.get("PROOFIE_MCP_LOCAL_ACTOR", "local-user"), "refused")
