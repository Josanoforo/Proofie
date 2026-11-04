"""Read a coherent local package snapshot without business writes, migrations or network calls."""
from __future__ import annotations
import hashlib
import json
import logging
import re
import sqlite3
import stat
from pathlib import Path
from copilot.core.raw_storage import read_raw
from copilot.core.send_service import sent_files_of, split_rows

logger = logging.getLogger(__name__)


class PrintError(ValueError):
    pass
class PackageMissing(PrintError):
    pass

_SHA = re.compile(r"[a-f0-9]{64}")
_PRIVATE = re.compile(r"[^\s<>]+@[^\s<>]+|\bBearer\b|\b(?:password|client[_-]?secret|access[_-]?token|api[_-]?key)\s*[:=]|[A-Za-z]:[\\/]|/(?:home|Users|mnt|etc)/|\.env\b", re.I)
_BASES = {"verified": "Source-backed statement", "seller_statement": "Seller statement",
          "buyer_statement": "Buyer statement", "model_interpretation": "Suggested statement — review required"}

def _path(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise PrintError("Linked storage is not supported for printing.")
    return path

def _text(value):
    if not isinstance(value, str) or len(value) > 100000 or _PRIVATE.search(value):
        raise PrintError("This package contains text that cannot be printed safely.")
    if any(ord(c) < 32 and c not in "\n\r\t" for c in value):
        raise PrintError("This package contains text that cannot be printed safely.")
    return value

def _sent_document_ids(conn, dispute_id, package_id):
    """(sent, late, unlisted): the ids of the attached documents that a CONFIRMED send of this package carried, and the ids of the
    attached documents that no confirmed (or true partial) send of it carried - files added after the response was sent.
    Read in the same read-only snapshot. The attempt is mapped to the package through the audit entry that registered it
    (a legacy attempt without one proves nothing about which package it was, so it counts as nothing here); a file is the
    same file by its sha256. `unlisted`: the attached documents that a true partial send carried but whose
    names PayPal's read did not list (the attempt's missing_attachment reason): sent, and not in PayPal."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='send_attempts'").fetchone():
        return set(), set(), set()
    columns={r[1] for r in conn.execute("PRAGMA table_info(send_attempts)")}
    shas="attachment_sha256_json" if "attachment_sha256_json" in columns else "NULL"
    names="attachment_filenames_json" if "attachment_filenames_json" in columns else "NULL"
    reason="reason" if "reason" in columns else "NULL"
    # a confirmed attempt, or a true partial one (statements in PayPal, a file missing): PayPal holds this version's statements
    attempts=conn.execute("SELECT request_id,state,"+reason+" AS reason,"+shas+" AS shas,"+names+" AS names FROM send_attempts "
                          "WHERE dispute_id=? AND request_id IS NOT NULL AND (state='confirmed' OR "
                          "(state='sent_unconfirmed' AND "+reason+" LIKE 'missing_attachment:%'))",(dispute_id,)).fetchall()
    if not attempts:
        return set(), set(), set()
    registered={}
    for entry in conn.execute("SELECT detail FROM audit_log WHERE dispute_id=? AND action='send_attempt_registered' "
                              "ORDER BY id",(dispute_id,)):
        try:
            detail=json.loads(entry[0])
        except (TypeError,ValueError):
            continue
        if isinstance(detail,dict):
            registered[detail.get("request_id")]=detail
    rows=[dict(r) for r in conn.execute(
        "SELECT a.document_id,a.sha256,d.filename FROM package_attachments a JOIN documents d ON d.id=a.document_id "
        "WHERE a.package_id=? ORDER BY a.document_id",(package_id,))]
    sent=set()
    unlisted=set()
    carried_any=set()
    held=False
    for attempt in attempts:
        detail=registered.get(attempt["request_id"])
        if not detail or detail.get("package_id")!=package_id:
            continue
        try:
            record={"attachment_sha256":json.loads(attempt["shas"]) if attempt["shas"] else [],
                    "attachment_filenames":json.loads(attempt["names"]) if attempt["names"] else []}
        except (TypeError,ValueError):
            continue
        sent_files=sent_files_of(record,detail)
        if sent_files.known:
            held=True
            carried=split_rows(sent_files,rows)[0]
            carried_any.update(r["document_id"] for r in carried)
            if attempt["state"]=="confirmed":
                sent.update(r["document_id"] for r in carried)
            else:
                not_listed={n for n in str(attempt["reason"] or "").split(":",1)[-1].split(",") if n}
                unlisted.update(r["document_id"] for r in carried if r["filename"] in not_listed)
    late={r["document_id"] for r in rows}-carried_any if held else set()
    return sent,late,unlisted-sent

def _registered(conn, dispute_id):
    """request_id -> the audit detail written when the attempt was registered (read-only)."""
    registered = {}
    for entry in conn.execute("SELECT detail FROM audit_log WHERE dispute_id=? AND action='send_attempt_registered' "
                              "ORDER BY id", (dispute_id,)):
        try:
            detail = json.loads(entry[0])
        except (TypeError, ValueError):
            continue
        if isinstance(detail, dict):
            registered[detail.get("request_id")] = detail
    return registered


def _notes_of(request_input):
    try:
        return request_input["evidences"][0]["notes"]
    except (KeyError, IndexError, TypeError):
        return None


def _sent_header(conn, dispute_id, package_id, claims):
    """What the send record says about THIS package, read in the same snapshot, by the rule of
    send_service.package_attempts: the attempts the audit entry `send_attempt_registered` maps to the package; only when
    there are none, an attempt that no package owns and whose stored statements equal this package's statements now (the
    content fallback: the statements text, the part of the content hash that does not depend on the tracking form). The
    attempt that speaks for the package is a confirmed one, else the latest. Tracking, carrier and file names are what the
    attempt stored when it was sent, never what the package would send now; each goes through _text(). None when no attempt
    speaks for this package (never sent)."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='send_attempts'").fetchone():
        return None
    columns = {r[1] for r in conn.execute("PRAGMA table_info(send_attempts)")}
    cols = ",".join(c if c in columns else "NULL AS " + c for c in
                    ("request_id", "state", "reason", "ts", "request_input_json", "attachment_filenames_json",
                     "package_sha256", "attachment_sha256_json", "readback_json"))
    attempts = conn.execute("SELECT " + cols + " FROM send_attempts WHERE dispute_id=?", (dispute_id,)).fetchall()
    registered = _registered(conn, dispute_id)
    mine = [a for a in attempts if a["request_id"] in registered
            and registered[a["request_id"]].get("package_id") == package_id]
    if not mine:
        from copilot.core.send import build_request_input
        try:
            current = _notes_of(build_request_input([dict(c) for c in claims], "x", None))
        except ValueError:
            current = None
        for a in attempts:
            try:
                stored = _notes_of(json.loads(a["request_input_json"])) if a["request_input_json"] else None
            except (TypeError, ValueError):
                stored = None
            if a["request_id"] not in registered and current is not None and stored == current:
                mine.append(a)
    if not mine:
        return None
    confirmed = [a for a in mine if a["state"] == "confirmed"]
    attempt = confirmed[-1] if confirmed else sorted(mine, key=lambda a: a["ts"] or "")[-1]
    state = attempt["state"]
    reason = str(attempt["reason"] or "")
    missing = []
    if state == "sent_unconfirmed" and reason.startswith("missing_attachment:"):
        state = "partial"
        missing = [_text(n) for n in reason.split(":", 1)[1].split(",") if n]
    tracking = carrier = None
    try:
        request_input = json.loads(attempt["request_input_json"]) if attempt["request_input_json"] else {}
        info = request_input["evidences"][0]["evidence_info"]["tracking_info"][0]
        tracking = info.get("tracking_number")
        from copilot.core import carriers
        carrier = carriers.display_name(info.get("carrier_name_other") or info.get("carrier_name")) or None
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        pass
    try:
        names = json.loads(attempt["attachment_filenames_json"]) if attempt["attachment_filenames_json"] else []
    except (TypeError, ValueError):
        names = []
    if not isinstance(names, list):
        names = []
    return {"state": state, "reason": reason, "when": attempt["ts"], "missing": missing,
            "tracking": _text(tracking) if tracking else None, "carrier": _text(carrier) if carrier else None,
            "files": [_text(n) for n in names],
            "case_send": _case_send(attempt, registered.get(attempt["request_id"]) or {}, dispute_id)}


def _case_send(attempt, detail, dispute_id):
    """W2-4: what the case file needs to recompute this attempt's hashes without the database: the request input it
    stored, its package hash (sha256_json of that input), the attachment hashes, the approval fingerprint the audit
    entry registered, and the read-back PayPal's record gave when the send was confirmed. None when the attempt did not
    store its request input or a stored hash is not a SHA-256 (nothing could be recomputed; the page still prints).
    Every text goes through _text()."""
    try:
        request_input = json.loads(attempt["request_input_json"]) if attempt["request_input_json"] else None
        shas = json.loads(attempt["attachment_sha256_json"]) if attempt["attachment_sha256_json"] else []
        readback = json.loads(attempt["readback_json"]) if attempt["readback_json"] else None
    except (TypeError, ValueError):
        return None
    package_sha = attempt["package_sha256"]
    approval_sha = detail.get("approval_sha256")
    if (not isinstance(request_input, dict) or not isinstance(package_sha, str) or not _SHA.fullmatch(package_sha)
            or not isinstance(shas, list) or not all(isinstance(s, str) and _SHA.fullmatch(s) for s in shas)
            or (approval_sha is not None and not (isinstance(approval_sha, str) and _SHA.fullmatch(approval_sha)))):
        return None
    _text(json.dumps(request_input, ensure_ascii=False))
    if not isinstance(readback, dict) or not isinstance(readback.get("documents_present", []), list):
        readback = None
    if readback is not None:
        readback = {"evidence_type": _text(str(readback.get("evidence_type") or "")) or None,
                    "documents_present": [_text(n) for n in readback.get("documents_present", [])],
                    "paypal_date": _text(readback["paypal_date"]) if readback.get("paypal_date") else None,
                    "read_at": _text(readback["read_at"]) if readback.get("read_at") else None}
    return {"request_id": _text(attempt["request_id"]) if attempt["request_id"] else None, "state": attempt["state"],
            "when": attempt["ts"], "action": "provide_evidence", "dispute_id": _text(dispute_id),
            "request_input": request_input, "package_sha256": package_sha, "attachment_sha256": shas,
            "approval_sha256": approval_sha, "readback": readback if attempt["state"] == "confirmed" else None}


def _approval(conn, package_id):
    """The package's latest approval that is still valid: when it was given and its fingerprint (read-only)."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='approvals'").fetchone():
        return None
    row = conn.execute("SELECT approval_sha256, approved_at FROM approvals WHERE package_id=? AND invalidated_at IS NULL "
                       "ORDER BY id DESC LIMIT 1", (package_id,)).fetchone()
    if row is None:
        return None
    if not (isinstance(row["approval_sha256"], str) and _SHA.fullmatch(row["approval_sha256"])):
        return None  # an old placeholder row (first pilot days) is no fingerprint: print without an approval mark, never refuse
    return {"approval_sha256": row["approval_sha256"], "approved_at": _text(row["approved_at"])}


def read(db_path, uploads_dir, dispute_id, package_id):
    if type(package_id) is not int or not 0 < package_id <= 9223372036854775807:
        raise PackageMissing("Package not found.")
    path = _path(db_path)
    if not path.is_file():
        raise PackageMissing("Package not found.")
    conn = None
    try:
        # Ordinary SQLite auxiliaries are allowed; linked/reparse storage is not.
        # Check even dangling links with lstat before SQLite opens any auxiliary.
        for suffix in ("-wal", "-shm", "-journal"):
            _path(Path(str(path)+suffix))
        # Separate read-only connection: SQLite owns WAL coordination and locks.
        conn = sqlite3.connect(path.as_uri()+"?mode=ro&cache=private", uri=True, timeout=0.25)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        package = conn.execute("SELECT id, dispute_id, version FROM packages WHERE id=? AND dispute_id=?",
                               (package_id, dispute_id)).fetchone()
        if package is None:
            raise PackageMissing("Package not found.")
        if type(package["version"]) is not int or not 0 < package["version"] <= 9223372036854775807:
            raise PrintError("The package metadata is inconsistent.")
        claims = conn.execute("SELECT id,text,source_ids_json,basis FROM claims WHERE package_id=? "
                              "ORDER BY evidence_index,claim_index,id LIMIT 201", (package_id,)).fetchall()
        attached = conn.execute("SELECT document_id,sha256 FROM package_attachments WHERE package_id=? "
                                "ORDER BY document_id LIMIT 201", (package_id,)).fetchall()
        if len(claims)>200 or len(attached)>200:
            raise PrintError("This package is too large for the printable view.")
        sent_ids,late_ids,unlisted_ids=_sent_document_ids(conn,dispute_id,package_id)
        sent_header=_sent_header(conn,dispute_id,package_id,claims)
        case_send=sent_header.pop("case_send",None) if sent_header is not None else None
        approval=_approval(conn,package_id)
        needed = {a["document_id"] for a in attached}
        links = []
        for claim in claims:
            ids = json.loads(claim["source_ids_json"])
            if not isinstance(ids,list) or len(ids)>100 or any(not isinstance(s,str) for s in ids):
                raise PrintError("The package source index is inconsistent.")
            local=[];missing=False
            for source in ids:
                match=re.fullmatch(r"doc_([1-9][0-9]{0,18})",source)
                if match:
                    did=int(match[1])
                    if did>9223372036854775807:raise PrintError("The package source index is inconsistent.")
                    needed.add(did);local.append(did)
                else:
                    missing=True
            links.append((claim,local,missing or not ids))
        if len(needed)>200:
            raise PrintError("This package is too large for the printable view.")
        documents={}
        acknowledgements=[]
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='active_approval_receipts'").fetchone():
            receipt=conn.execute("SELECT r.confirmations_json FROM active_approval_receipts r JOIN approvals a "
                                 "ON a.id=r.approval_id WHERE r.package_id=? AND a.invalidated_at IS NULL "
                                 "ORDER BY a.id DESC LIMIT 1", (package_id,)).fetchone()
            if receipt:
                text=receipt[0]
                if not isinstance(text,str) or len(text)>100000:
                    raise PrintError("The approval record is too large for printing.")
                confirmations=json.loads(text)
                if not isinstance(confirmations,list) or len(confirmations)>200:
                    raise PrintError("The approval record is inconsistent.")
                for confirmation in confirmations:
                    if not isinstance(confirmation,dict):
                        raise PrintError("The approval record is inconsistent.")
                    differences=confirmation.get("acknowledged_differences",[])
                    if not isinstance(differences,list) or len(differences)>2:
                        raise PrintError("The date acknowledgement is inconsistent.")
                    for difference in differences:
                        if not isinstance(difference,dict) or difference.get("check_id") not in {"delivery_date","dates_coherent"}:
                            raise PrintError("The date acknowledgement is inconsistent.")
                        item={"check_id":difference["check_id"],"note":_text(difference.get("note"))}
                        if not item["note"].strip():
                            raise PrintError("The date acknowledgement is missing its note.")
                        if item not in acknowledgements:
                            acknowledgements.append(item)
        if needed:
            marks=",".join("?" for _ in needed)
            rows=conn.execute("SELECT id,dispute_id,sha256,size_bytes,excluded FROM documents WHERE id IN ("+marks+")",
                              tuple(sorted(needed))).fetchall()
            documents={x["id"]:x for x in rows}
        # All SQL rows belong to one snapshot; release it before filesystem validation.
        conn.rollback()
        conn.close()
        conn = None
        exhibits=[];numbers={}
        root=_path(uploads_dir)
        # An ATTACHED file left out of the checks is still sent with the package, so it is
        # printed with that note; an excluded file that is only cited (not sent) is still refused.
        attached_ids={a["document_id"] for a in attached}
        for did in sorted(needed):
            doc=documents.get(did)
            if not doc or doc["dispute_id"]!=dispute_id or (doc["excluded"] and did not in attached_ids) \
                    or not _SHA.fullmatch(doc["sha256"]):
                raise PrintError("A local exhibit is missing or inconsistent.")
            sha=doc["sha256"]
            if any(a["document_id"]==did and a["sha256"]!=sha for a in attached):
                raise PrintError("A local exhibit is missing or inconsistent.")
            rawpath=_path(root/sha)
            if not rawpath.is_relative_to(root) or not rawpath.is_file() or rawpath.stat().st_size>67108864:
                raise PrintError("A local exhibit is missing or inconsistent.")
            raw=read_raw(root,sha)
            if raw is None or len(raw)!=doc["size_bytes"] or hashlib.sha256(raw).hexdigest()!=sha:
                raise PrintError("A local exhibit is missing or inconsistent.")
            number=len(exhibits)+1;numbers[did]=number
            exhibits.append({"number":number,"sha256":sha,"excluded_sent":bool(doc["excluded"]),"sent":did in sent_ids,
                             "late":did in late_ids,"unlisted":did in unlisted_ids})
        pages=[]
        for claim,local,missing in links:
            text=_text(claim["text"])
            citations=[{"number":numbers[x],"sha256":documents[x]["sha256"]} for x in dict.fromkeys(local)]
            chunks=[]
            remaining=text
            while remaining:
                cut=1600 if len(remaining)>1600 else len(remaining)
                if cut<len(remaining):
                    boundary=remaining.rfind(" ",0,cut)
                    if boundary>0:cut=boundary+1
                chunks.append(remaining[:cut]);remaining=remaining[cut:]
            chunks=chunks or [""]
            for index,chunk in enumerate(chunks):
                pages.append({"kind":"claim","claim":claim["id"],"text":chunk,"continuation":index>0,
                              "basis":_BASES.get(claim["basis"],"Unclassified statement — review required"),
                              "citations":citations,"missing":missing})
        for exhibit in exhibits:
            pages.append({"kind":"exhibit",**exhibit})
        if not pages:
            pages.append({"kind":"empty"})
        header={"dispute_id":_text(dispute_id),"state":None,"reason":"","when":None,"missing":[],"tracking":None,
                "carrier":None,"files":[],"later":False}
        if sent_header is not None:
            header.update(sent_header)
            # a confirmed send of which an attached file was not part: PayPal holds the statements, not that file
            header["later"]=sent_header["state"]=="confirmed" and bool(late_ids)
        # W2-4: the record the case file carries and copilot/verify.py recomputes; `exhibits` are the files it bundles.
        case={"send":case_send,"approval":approval,"exhibits":[e["sha256"] for e in exhibits]}
        return {"package_id":package_id,"version":package["version"],"pages":pages,
                "acknowledgements":acknowledgements,"header":header,"case":case}
    except (sqlite3.Error, OSError, json.JSONDecodeError, TypeError, OverflowError) as exc:
        # The page words this as one refusal; the log keeps which read failed, so a one-off 400 can be told apart.
        logger.warning("package print: package %s read failed (%s: %s)", package_id, type(exc).__name__, exc)
        raise PrintError("The local package cannot be read safely for printing.") from None
    finally:
        if conn is not None:
            conn.close()
