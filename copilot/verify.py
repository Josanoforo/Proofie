"""Read-only document and attachment integrity check: python -m copilot.verify.
A downloaded case file is checked on its own, from the zip alone: python -m copilot.verify --case-file <zip>."""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import stat
import zipfile

from copilot import config

_SHA = re.compile(r"[0-9a-f]{64}")
_DOCUMENT_SOURCE = re.compile(r"doc_([1-9][0-9]{0,18})")


def _unlinked(path: Path) -> bool:
    """Do not follow linked storage while inspecting content-addressed files."""
    try:
        for part in (path, *path.parents):
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                return False
    except OSError:
        return False
    return True


def _snapshot(database: Path):
    # No repo._connect/ensure_schema: those create directories and run migrations.
    conn = sqlite3.connect(database.as_uri() + "?mode=ro&cache=private", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        documents = {row["id"]: dict(row) for row in conn.execute(
            "SELECT id, dispute_id, sha256 FROM documents ORDER BY id")}
        packages = {row["id"]: dict(row) for row in conn.execute(
            "SELECT id, dispute_id FROM packages ORDER BY id")}
        attachments = [dict(row) for row in conn.execute(
            "SELECT package_id, document_id, sha256 FROM package_attachments ORDER BY package_id,document_id")]
        references = defaultdict(set)
        for row in attachments:
            references[row["package_id"]].add(row["document_id"])
        for row in conn.execute("SELECT package_id, source_ids_json FROM claims ORDER BY id"):
            sources = json.loads(row["source_ids_json"])
            if not isinstance(sources, list) or any(not isinstance(value, str) for value in sources):
                raise ValueError("Invalid package source index")
            for source in sources:
                match = _DOCUMENT_SOURCE.fullmatch(source)
                if match:
                    document_id = int(match[1])
                    if document_id > 9223372036854775807:
                        raise ValueError("Invalid package source index")
                    references[row["package_id"]].add(document_id)
        return documents, packages, attachments, references
    finally:
        conn.close()


def _digest(path: Path) -> str | None:
    if not _unlinked(path) or not path.is_file():
        return None
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def verify(database: Path, uploads: Path) -> tuple[int, list[str]]:
    database, uploads = Path(database).absolute(), Path(uploads).absolute()
    if not _unlinked(database) or not database.is_file():
        return 2, ["No readable data directory/database is available."]
    try:
        documents, packages, attachments, references = _snapshot(database)
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return 1, ["FAIL database: stored metadata cannot be read safely.", "TOTAL 1 FAIL (database)"]

    memberships = defaultdict(list)
    for package_id in sorted(references):
        # Same sorted union of claim sources and attachments as package_print.
        for number, document_id in enumerate(sorted(references[package_id]), 1):
            memberships[document_id].append((package_id, number))
    frozen = defaultdict(list)
    for row in attachments:
        frozen[row["document_id"]].append(row["sha256"])

    lines, failures = [], 0
    targets = sorted(set(documents) | set(memberships))
    for document_id in targets:
        document = documents.get(document_id)
        labels = ",".join(f"{package}:E{number}" for package, number in memberships[document_id]) or "none"
        problems, actual = [], None
        if document is None:
            problems.append("document metadata is missing")
        elif not isinstance(document["sha256"], str) or not _SHA.fullmatch(document["sha256"]):
            problems.append("invalid stored hash")
        else:
            actual = _digest(uploads / document["sha256"])
            if actual is None:
                problems.append("file is missing or cannot be read safely")
            elif actual != document["sha256"]:
                problems.append("file hash does not match document metadata")
            if actual is not None and any(expected != actual for expected in frozen[document_id]):
                problems.append("attachment hash does not match file")
        for package_id, _number in memberships[document_id]:
            package = packages.get(package_id)
            if package is None or document is not None and package["dispute_id"] != document["dispute_id"]:
                problems.append("package/document ownership does not match")
                break
        state = "FAIL" if problems else "OK"
        failures += bool(problems)
        detail = "; ".join(problems) if problems else "hashes match"
        lines.append(f"{state} document={document_id} packages={labels} sha256={actual or 'unavailable'}: {detail}")
    lines.append(f"TOTAL {len(targets)} checked: {len(targets) - failures} OK, {failures} FAIL")
    return (1 if failures else 0), lines


# ---- W2-4: the case file (copilot/features/package_print/case_file.py) -------------------------------------------------
CASE_FILE_SCHEMA = "proofie.case_file.v1"
_MEMBER = re.compile(r"MANIFEST\.json|print\.html|ledger\.json|files/[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_MEMBER_LIMIT = 64 * 1024 * 1024          # one member, uncompressed (the print view refuses a larger exhibit)
_TOTAL_LIMIT = 512 * 1024 * 1024


def verify_case_file(path: Path) -> tuple[int, list[str]]:
    """Check a case file from the zip alone (no database, no web code): every member against MANIFEST.json's SHA-256,
    no member left unlisted, the source commit named, and, when a send is recorded, the package hash (sha256_json of the
    stored request input) and the approval fingerprint (copilot.core.send) recomputed, with each attached file present.
    Exit 0 = every check passes, 1 = a check fails, 2 = the file is not a readable zip."""
    from copilot.core.send import approval_fingerprint_with_attachments, sha256_json
    path = Path(path).absolute()
    if not _unlinked(path) or not path.is_file():
        return 2, ["No readable case file at that path."]
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        return 2, ["The case file is not a readable zip."]
    results = []

    def record(ok, text):
        results.append(("OK " if ok else "FAIL ") + text)

    with archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        safe = True
        for info in infos:
            if not _MEMBER.fullmatch(info.filename) or info.is_dir():
                record(False, f"unsafe member name {info.filename!r}")
                safe = False
            elif info.file_size > _MEMBER_LIMIT:
                record(False, f"member {info.filename} is larger than the limit")
                safe = False
        if len(set(names)) != len(names):
            record(False, "a member name appears twice")
            safe = False
        if sum(info.file_size for info in infos) > _TOTAL_LIMIT:
            record(False, "the members are larger than the limit")
            safe = False
        manifest = None
        if safe and "MANIFEST.json" in names:
            try:
                manifest = json.loads(archive.read("MANIFEST.json").decode("utf-8"))
            except (ValueError, UnicodeDecodeError, zipfile.BadZipFile, OSError):
                manifest = None
        if safe and (not isinstance(manifest, dict) or manifest.get("schema") != CASE_FILE_SCHEMA
                     or not isinstance(manifest.get("files"), dict)):
            record(False, "MANIFEST.json is missing or is not a proofie.case_file.v1 manifest")
            safe = False
        if safe:
            listed = manifest["files"]
            for name in sorted(set(names) - {"MANIFEST.json"} - set(listed)):
                record(False, f"member {name} is not listed in MANIFEST.json")
            for name, expected in sorted(listed.items()):
                if name not in names or name == "MANIFEST.json":
                    record(False, f"member {name} is missing")
                    continue
                try:
                    actual = hashlib.sha256(archive.read(name)).hexdigest()
                except (zipfile.BadZipFile, OSError, RuntimeError):
                    actual = None
                record(actual == expected, f"member {name} sha256={actual or 'unavailable'}"
                       + ("" if actual == expected else " does not match the manifest"))
            commit = manifest.get("source_commit")
            record(isinstance(commit, str) and bool(_COMMIT.fullmatch(commit)),
                   f"source commit {commit}" if commit else "source commit: not named")
            send = manifest.get("send")
            if send is None:
                record(True, "send: no send record in this case file (no request to recompute)")
            elif not isinstance(send, dict) or not isinstance(send.get("request_input"), dict):
                record(False, "send: the record is not readable")
            else:
                try:
                    package = sha256_json(send["request_input"])
                    record(package == send.get("package_sha256"), f"package hash {package}"
                           + ("" if package == send.get("package_sha256") else " does not match the recorded one"))
                    shas = send.get("attachment_sha256")
                    if not isinstance(shas, list) or not all(isinstance(s, str) for s in shas):
                        raise TypeError("attachment list")
                    fingerprint = approval_fingerprint_with_attachments(
                        send.get("dispute_id") or manifest.get("dispute_id"), send.get("action"),
                        send["request_input"], shas)
                    record(fingerprint == send.get("approval_sha256"), f"approval fingerprint {fingerprint}"
                           + ("" if fingerprint == send.get("approval_sha256") else " does not match the recorded one"))
                    for sha in shas:
                        record(f"files/{sha}" in listed, f"attached file {sha}"
                               + (" is in the case file" if f"files/{sha}" in listed else " is missing"))
                except (TypeError, ValueError):
                    record(False, "send: the record cannot be recomputed")
    failures = sum(1 for line in results if line.startswith("FAIL"))
    results.append(f"TOTAL {len(results)} checked: {len(results) - failures} OK, {failures} FAIL")
    return (1 if failures else 0), results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, help="Explicit folder containing copilot.db and uploads")
    parser.add_argument("--case-file", type=Path, help="A downloaded case file (zip) to check on its own")
    args = parser.parse_args(argv)
    if args.case_file is not None:
        code, lines = verify_case_file(args.case_file.expanduser())
        for line in lines:
            print(line)
        return code
    if args.data_dir is not None:
        data = args.data_dir.expanduser().absolute()
        database, uploads = data / config.DB_PATH.name, data / config.UPLOADS_DIR.name
    else:
        database, uploads = config.db_path(), config.uploads_dir()
    code, lines = verify(database, uploads)
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
