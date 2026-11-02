"""W2-4: the case file that outlives the case. The print view carries the package hash, the approval fingerprint,
PayPal's read-back and the source commit; one link downloads a zip (print HTML, ledger JSON when the ledger is on, the
files, MANIFEST.json); `python -m copilot.verify --case-file` passes on that zip and fails on a modified one, reading
only the zip. Offline: synthetic DEMO data, a local database, no network."""
import hashlib
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.core.send import approval_fingerprint_with_attachments, build_request_input, sha256_json
from copilot.db import repo
from copilot import verify
from .test_print import DID, prepared, url  # noqa: F401

ROOT = Path(__file__).resolve().parents[3]
READBACK = {"evidence_type": "PROOF_OF_FULFILLMENT", "documents_present": ["demo.txt"],
            "paypal_date": "2026-10-05T10:00:00Z", "read_at": "2026-10-05T10:01:00-06:00"}


def head_commit():
    manifest_path = ROOT / "MANIFEST.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        source_commit = manifest.get("source_commit")
        if not isinstance(source_commit, str) or len(source_commit) != 40 or any(
                character not in "0123456789abcdefABCDEF" for character in source_commit):
            raise ValueError("MANIFEST.json source_commit must be a full 40-character hexadecimal SHA")
        return source_commit
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                          check=True).stdout.strip()


def seed_confirmed(db, pid, *, request_id="req-demo-1"):
    """An approved, confirmed send of this package as the send path stores it, with a real fingerprint."""
    req = build_request_input(repo.list_claims(db, pid), "DEMO-TRK-0001", "FEDEX")
    shas = [a["sha256"] for a in repo.list_attachments(db, pid)]
    fingerprint = approval_fingerprint_with_attachments(DID, "provide_evidence", req, shas)
    repo.insert_approval(db, pid, fingerprint, "vendor")
    repo.log_action(db, "system", "send_attempt_registered", DID,
                    {"package_id": pid, "approval_sha256": fingerprint, "request_id": request_id,
                     "attachments": ["demo.txt"], "attachment_sha256": shas})
    store = SQLiteAttemptStore(db)
    try:
        package_sha = sha256_json(req)
        assert store.claim(DID, package_sha)
        store.write(DID, package_sha, {"state": "confirmed", "request_id": request_id, "request_input": req,
                                       "attachment_filenames": ["demo.txt"], "attachment_sha256": shas,
                                       "readback": READBACK})
    finally:
        store.close()
    return req, package_sha, fingerprint


def case_url(pid):
    return f"/disputes/{DID}/packages/{pid}/case-file"


def download(client, pid):
    response = client.get(case_url(pid))
    assert response.status_code == 200, response.text[:300]
    assert response.headers["content-type"] == "application/zip"
    return response


def rezip(data, change):
    """A copy of the zip with `change(name, bytes) -> bytes | None` applied to each member (None drops it)."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            new = change(info.filename, src.read(info))
            if new is not None:
                dst.writestr(info.filename, new)
    return out.getvalue()


def check(tmp_path, data, name="case.zip"):
    path = tmp_path / name
    path.write_bytes(data)
    return verify.verify_case_file(path)


# ---- the print view ---------------------------------------------------------------------------------------------------
def test_the_print_view_names_the_source_commit_package_hash_fingerprint_and_readback(prepared):
    client, db, _, pid, _, _ = prepared
    req, package_sha, fingerprint = seed_confirmed(db, pid)
    page = client.get(url(pid))
    assert page.status_code == 200
    record = page.text[page.text.index('data-state="case-record"'):]
    assert head_commit() in record
    assert package_sha in record and fingerprint in record and "req-demo-1" in record
    assert "What PayPal&#39;s record lists" in record or "What PayPal's record lists" in record
    assert "demo.txt" in record
    assert f'href="{case_url(pid)}"' in page.text and "Download the case file" in page.text


def test_a_never_sent_package_prints_no_package_hash_and_says_so(prepared):
    client, _, _, pid, _, _ = prepared
    page = client.get(url(pid))
    record = page.text[page.text.index('data-state="case-record"'):]
    assert head_commit() in record and "No send of this package is recorded." in record


# ---- the download -----------------------------------------------------------------------------------------------------
def test_the_case_file_is_a_zip_of_the_print_view_the_files_and_a_manifest_of_hashes(prepared, monkeypatch):
    client, db, _, pid, _, sha = prepared
    monkeypatch.delenv("COPILOT_FEATURE_LEDGER", raising=False)
    req, package_sha, fingerprint = seed_confirmed(db, pid)
    response = download(client, pid)
    data = response.content
    assert response.headers["x-case-file-sha256"] == hashlib.sha256(data).hexdigest()
    assert f'filename="case-file-{DID}-v1.zip"' in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
        manifest = json.loads(z.read("MANIFEST.json"))
        assert names == {"MANIFEST.json", "print.html", f"files/{sha}"}
        for name, digest in manifest["files"].items():
            assert hashlib.sha256(z.read(name)).hexdigest() == digest, name
        assert "Download the case file" not in z.read("print.html").decode("utf-8")       # the saved copy has no link
    assert manifest["schema"] == "proofie.case_file.v1" and manifest["source_commit"] == head_commit()
    assert manifest["dispute_id"] == DID and manifest["package_id"] == pid and manifest["version"] == 1
    send = manifest["send"]
    assert send["request_input"] == req and send["package_sha256"] == package_sha
    assert send["approval_sha256"] == fingerprint and send["action"] == "provide_evidence"
    assert send["attachment_sha256"] == [sha] and send["state"] == "confirmed" and send["request_id"] == "req-demo-1"
    assert manifest["readback"] == READBACK and manifest["ledger"] is None
    assert download(client, pid).content == data                                          # deterministic


def test_with_the_ledger_on_the_case_file_carries_its_json_export(prepared, monkeypatch):
    client, db, _, pid, _, _ = prepared
    seed_confirmed(db, pid)
    monkeypatch.setenv("COPILOT_FEATURE_LEDGER", "true")
    with zipfile.ZipFile(io.BytesIO(download(client, pid).content)) as z:
        manifest = json.loads(z.read("MANIFEST.json"))
        body = z.read("ledger.json")
    assert manifest["ledger"] == {"schema": "proofie.ledger.v1", "sha256": hashlib.sha256(body).hexdigest()}
    assert manifest["files"]["ledger.json"] == hashlib.sha256(body).hexdigest()
    assert json.loads(body)["dispute_id"] == DID


def test_the_case_file_route_is_absent_with_the_print_flag_off(prepared, monkeypatch):
    client, _, _, pid, _, _ = prepared
    monkeypatch.delenv("COPILOT_FEATURE_PACKAGE_PRINT")
    from copilot.app import create_app
    from _hosted import authenticated_client
    assert authenticated_client(create_app()).get(case_url(pid)).status_code == 404


# ---- verify --case-file -----------------------------------------------------------------------------------------------
def test_verify_passes_on_the_exported_case_file(prepared, tmp_path):
    client, db, _, pid, _, _ = prepared
    seed_confirmed(db, pid)
    code, lines = check(tmp_path, download(client, pid).content)
    assert code == 0, lines
    assert lines[-1].startswith("TOTAL") and "0 FAIL" in lines[-1]
    assert any("package hash" in line and line.startswith("OK") for line in lines)
    assert any("approval fingerprint" in line and line.startswith("OK") for line in lines)
    assert any(line.startswith("OK source commit") and head_commit() in line for line in lines)


def test_verify_passes_on_a_never_sent_case_file_and_says_there_is_no_send_record(prepared, tmp_path):
    client, _, _, pid, _, _ = prepared
    code, lines = check(tmp_path, download(client, pid).content)
    assert code == 0, lines
    assert any("no send record" in line for line in lines)


def _edit_manifest(data, edit):
    def change(name, body):
        if name != "MANIFEST.json":
            return body
        manifest = json.loads(body)
        edit(manifest)
        return json.dumps(manifest).encode("utf-8")
    return rezip(data, change)


@pytest.mark.parametrize("defect", ["file_byte", "print_html", "request_input", "approval", "attachment_list",
                                    "extra_member", "missing_member", "no_commit"])
def test_verify_fails_on_a_modified_case_file(prepared, tmp_path, defect):
    client, db, _, pid, _, sha = prepared
    seed_confirmed(db, pid)
    data = download(client, pid).content
    if defect == "file_byte":
        bad = rezip(data, lambda n, b: (b[:-1] + b"X") if n == f"files/{sha}" else b)
    elif defect == "print_html":
        bad = rezip(data, lambda n, b: b.replace(b"DEMO-TRK-0001", b"DEMO-TRK-0002") if n == "print.html" else b)
    elif defect == "request_input":
        bad = _edit_manifest(data, lambda m: m["send"]["request_input"]["evidences"][0].update(notes="Changed."))
    elif defect == "approval":
        bad = _edit_manifest(data, lambda m: m["send"].update(approval_sha256="0" * 64))
    elif defect == "attachment_list":
        bad = _edit_manifest(data, lambda m: m["send"].update(attachment_sha256=[]))
    elif defect == "extra_member":
        out = io.BytesIO(data)
        with zipfile.ZipFile(out, "a") as z:
            z.writestr("files/extra.txt", b"not listed")
        bad = out.getvalue()
    elif defect == "missing_member":
        bad = rezip(data, lambda n, b: None if n == "print.html" else b)
    else:
        bad = _edit_manifest(data, lambda m: m.update(source_commit=None))
    code, lines = check(tmp_path, bad)
    assert code == 1, (defect, lines)
    assert lines[-1].startswith("TOTAL") and "0 FAIL" not in lines[-1]


def test_verify_refuses_what_is_not_a_safe_case_file(tmp_path):
    code, lines = check(tmp_path, b"not a zip")
    assert code == 2, lines
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("../escape.txt", b"x")
        z.writestr("MANIFEST.json", json.dumps({"schema": "proofie.case_file.v1", "files": {}}))
    code, lines = check(tmp_path, out.getvalue(), "unsafe.zip")
    assert code == 1 and any("unsafe member name" in line for line in lines), lines


def test_the_command_line_checks_a_case_file_and_loads_no_web_code(prepared, tmp_path):
    client, db, _, pid, _, _ = prepared
    seed_confirmed(db, pid)
    path = tmp_path / "case.zip"
    path.write_bytes(download(client, pid).content)
    probe = ("import runpy, sys\n"
             "sys.argv = ['copilot.verify', '--case-file', sys.argv[1]]\n"
             "try:\n    runpy.run_module('copilot.verify', run_name='__main__')\n"
             "except SystemExit as exit:\n    code = exit.code\n"
             "web = sorted(m for m in sys.modules if m.split('.')[0] in ('fastapi', 'starlette', 'jinja2')"
             " or m.startswith('copilot.web') or m.startswith('copilot.features'))\n"
             "print('WEB', web)\nsys.exit(code)\n")
    result = subprocess.run([sys.executable, "-c", probe, str(path)], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WEB []" in result.stdout and "TOTAL" in result.stdout
