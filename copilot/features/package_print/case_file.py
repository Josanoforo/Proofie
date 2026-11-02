"""W2-4: the case file that outlives the case. One zip, built from the read-only print snapshot (service.read):

- print.html: the print view as saved (no download link);
- ledger.json: the ledger's own JSON export, when the ledger feature is on;
- files/<sha256>: every local exhibit of the package (attached or cited), named by its hash;
- MANIFEST.json (schema proofie.case_file.v1): the source commit, the package, the send record that copilot/verify.py
  recomputes (request input, package hash, attachment hashes, approval fingerprint), PayPal's read-back and the SHA-256
  of every other member.

The zip is deterministic: sorted members, fixed timestamps, so the same package gives the same bytes. Nothing here
writes to the database, calls PayPal or calls a model.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from pathlib import Path

from copilot.core.raw_storage import read_raw
from copilot.features.package_print.service import PrintError

SCHEMA = "proofie.case_file.v1"
ROOT = Path(__file__).resolve().parents[3]
_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def _commit(text) -> str | None:
    text = (text or "").strip() if isinstance(text, str) else ""
    return text if _COMMIT.fullmatch(text) else None


def source_commit(root: Path = ROOT) -> str | None:
    """The commit this code was built from, read from files only (no subprocess, no network): the export kit's
    MANIFEST.json `source_commit`, else the git checkout's HEAD (a worktree's `.git` file, `commondir`, loose refs,
    then packed-refs). None when neither says."""
    try:
        manifest = root / "MANIFEST.json"
        if manifest.is_file():
            found = _commit(json.loads(manifest.read_text(encoding="utf-8")).get("source_commit"))
            if found:
                return found
        git = root / ".git"
        if git.is_file():
            pointer = git.read_text(encoding="utf-8").strip()
            if not pointer.startswith("gitdir:"):
                return None
            gitdir = Path(pointer[len("gitdir:"):].strip())
            gitdir = gitdir if gitdir.is_absolute() else (root / gitdir)
        elif git.is_dir():
            gitdir = git
        else:
            return None
        head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
        if _commit(head):
            return head
        if not head.startswith("ref: "):
            return None
        ref = head[len("ref: "):].strip()
        common = gitdir
        if (gitdir / "commondir").is_file():
            common = (gitdir / (gitdir / "commondir").read_text(encoding="utf-8").strip()).resolve()
        for base in (gitdir, common):
            loose = base / ref
            if loose.is_file():
                found = _commit(loose.read_text(encoding="utf-8"))
                if found:
                    return found
        packed = common / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref:
                    return _commit(parts[0])
    except (OSError, ValueError, AttributeError):
        return None
    return None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build(package: dict, dispute_id: str, print_html: bytes, uploads_dir, ledger_json: bytes | None,
          commit: str | None) -> bytes:
    """The zip bytes. Raises PrintError when an exhibit file is not the file its hash names."""
    members = {"print.html": print_html}
    for sha in package["case"]["exhibits"]:
        raw = read_raw(Path(uploads_dir), sha)
        if raw is None or _sha(raw) != sha:
            raise PrintError("A local exhibit is missing or inconsistent.")
        members[f"files/{sha}"] = raw
    ledger = None
    if ledger_json is not None:
        members["ledger.json"] = ledger_json
        ledger = {"schema": json.loads(ledger_json).get("schema"), "sha256": _sha(ledger_json)}
    send = package["case"]["send"]
    manifest = {
        "schema": SCHEMA, "source_commit": commit, "dispute_id": dispute_id, "package_id": package["package_id"],
        "version": package["version"],
        "send": None if send is None else {k: send[k] for k in (
            "dispute_id", "action", "request_id", "state", "when", "request_input", "package_sha256",
            "attachment_sha256", "approval_sha256")},
        "approval": package["case"]["approval"],
        "readback": send["readback"] if send else None,
        "ledger": ledger,
        "files": {name: _sha(data) for name, data in sorted(members.items())},
    }
    members["MANIFEST.json"] = json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=1).encode("utf-8")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, date_time=_FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, members[name])
    return out.getvalue()
