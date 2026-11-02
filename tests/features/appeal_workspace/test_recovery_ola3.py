import hashlib
import os
import re

from fastapi.testclient import TestClient


def test_recovery_to_lesson_and_next_sale(monkeypatch, tmp_path):
    from copilot import config
    from copilot.app import create_app
    from copilot.core.attempt_store import SQLiteAttemptStore
    from copilot.core.raw_storage import write_raw
    from copilot.db import repo
    from copilot.features.sale_lesson import service as lessons
    from copilot.web.routes_disputes import get_client

    monkeypatch.setattr(config, "load_env_file", lambda: None)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "db.sqlite")
    monkeypatch.setattr(config, "_uploads_dir_override", tmp_path / "uploads")
    monkeypatch.setattr(config, "data_dir", lambda: tmp_path / "data")
    for name in list(os.environ):
        if name.startswith("COPILOT_FEATURE_"): monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "false")
    for name in ("OUTCOME", "OUTCOME_APPEAL", "APPEAL_WORKSPACE", "SALE_LESSON"):
        monkeypatch.setenv("COPILOT_FEATURE_" + name, "true")
    dispute, capture = "PP-R-TST-0000002", "12345678901234567"
    raw = {"dispute_id": dispute, "status": "RESOLVED", "dispute_life_cycle_stage": "CHARGEBACK",
           "reason": "UNAUTHORISED", "dispute_amount": {"currency_code": "USD", "value": "25.00"},
           "disputed_transactions": [{"seller_transaction_id": capture}],
           "dispute_outcome": {"outcome_code": "RESOLVED_BUYER_FAVOUR"},
           "links": [{"rel": "appeal", "href": f"https://api-m.sandbox.paypal.com/v1/customer/disputes/{dispute}/appeal", "method": "POST"}]}
    class Fake:
        def call(self, method, path, op, **kwargs):
            assert method == "GET"
            if path == f"/v1/customer/disputes/{dispute}": return raw
            if path == f"/v1/shipping/trackers?transaction_id={capture}": return {"trackers": []}
            if path.startswith("/v1/reporting/transactions?"): return {"transaction_details": []}
            raise AssertionError((path, op))
    def document(name, data):
        sha = hashlib.sha256(data).hexdigest(); write_raw(config.uploads_dir(), sha, data)
        return repo.insert_document(config.db_path(), dispute, name, "application/pdf" if name.endswith(".pdf") else "text/plain", len(data), sha, "Synthetic", True), sha
    original, old_sha = document("original.txt", b"old bytes")
    new, new_sha = document("new.pdf", b"%PDF-1.4 new bytes")
    duplicate, _ = document("duplicate.txt", b"old bytes")
    missing, _ = document("missing.txt", b"missing bytes")
    # Keep the row while removing its bytes through an isolated fake read below.
    sent = repo.insert_package(config.db_path(), dispute, "human_edit", True, [])
    repo.attach_document(config.db_path(), sent, original)
    repo.insert_approval(config.db_path(), sent, "a" * 64, "vendor")
    attempts = SQLiteAttemptStore(config.db_path())
    try: attempts.write(dispute, "a" * 64, {"state": "confirmed", "readback": {"documents_present": True}})
    finally: attempts.close()
    draft = repo.insert_package(config.db_path(), dispute, "human_edit", True, [])
    from copilot.core import raw_storage
    read = raw_storage.read_raw
    monkeypatch.setattr(raw_storage, "read_raw", lambda directory, digest: None if digest == hashlib.sha256(b"missing bytes").hexdigest() else read(directory, digest))
    app = create_app(); app.dependency_overrides[get_client] = lambda: Fake()
    web = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345))
    page = web.get(f"/disputes/{dispute}/outcome")
    assert page.status_code == 200
    assert "The current outcome is decided" in page.text and "new content" in page.text and "unchanged content" in page.text
    assert "Unavailable for comparison" in page.text and new_sha in page.text and old_sha in page.text
    assert f"/disputes/{dispute}/documents/{new}/original" in page.text and f"/disputes/{dispute}/packages/{draft}#attachments" in page.text
    assert f"/disputes/{dispute}/sale-lesson?capture_id={capture}" in page.text
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    attach = web.post(f"/disputes/{dispute}/packages/{draft}/attach/{new}", data={"csrf_token": token}, follow_redirects=False)
    assert attach.status_code == 303
    assert [a["document_id"] for a in repo.list_attachments(config.db_path(), draft)] == [new]
    page = web.get(f"/disputes/{dispute}/outcome")
    assert re.search(r'new\.pdf.*?attached to.*?package v2', page.text, re.S)
    duplicate_row = next(row for row in re.findall(r"<li>(.*?)</li>", page.text, re.S) if "duplicate.txt" in row and "source Proofie-document" in row)
    assert "not attached to a recorded package" in duplicate_row
    lesson_url = f"/disputes/{dispute}/sale-lesson?capture_id={capture}"
    form = web.get(lesson_url); assert form.status_code == 200
    token = re.search(r'name="csrf_token" value="([^"]+)"', form.text).group(1)
    payload = {"csrf_token": token, "capture_id": capture, "sale_type": "physical", "lesson": "Photograph the parcel before pickup", "outcome_code": "RESOLVED_BUYER_FAVOUR"}
    raw["dispute_outcome"]["outcome_code"] = "RESOLVED_SELLER_FAVOUR"
    assert web.post(f"/disputes/{dispute}/sale-lesson", data=payload).status_code == 409
    assert lessons.list_lessons() == []
    raw["dispute_outcome"]["outcome_code"] = "RESOLVED_BUYER_FAVOUR"
    form = web.get(lesson_url); payload["csrf_token"] = re.search(r'name="csrf_token" value="([^"]+)"', form.text).group(1)
    response = web.post(f"/disputes/{dispute}/sale-lesson", data=payload, follow_redirects=False)
    assert response.status_code == 303
    rows = lessons.lessons_for_preparation("physical")
    assert len(rows) == 1 and rows[0]["text"] == payload["lesson"] and rows[0]["source_capture_id"] == capture
    assert "Seller interpretation" in rows[0]["attribution"] and not lessons.lessons_for_preparation("digital")
    later_sale = web.get("/sale-lessons?kind=physical")
    assert later_sale.status_code == 200 and payload["lesson"] in later_sale.text and "not a PayPal-confirmed cause" in later_sale.text
    after = web.get(f"/disputes/{dispute}/outcome")
    assert payload["lesson"] in after.text and "not a PayPal-confirmed cause" in after.text


def test_recovery_reasons_and_uncertain_attempt_render():
    from types import SimpleNamespace
    from jinja2 import Environment, FileSystemLoader
    from copilot.features.appeal_workspace.service import recovery_context
    env = Environment(loader=FileSystemLoader("copilot/web/templates"), autoescape=True)
    env.globals["hosted_demo"] = lambda: False
    template = env.get_template("features/appeal_workspace/_recovery.html")
    comparison = {"original_package": {"id": 1}, "proposed_evidence": [], "unavailable_sources": []}
    for norm, decided, link, expected, phrase in [
        ({}, True, True, "eligible", "decided and its read includes an appeal link"),
        ({}, True, False, "not eligible", "does not show both a decision"),
        (None, False, False, "unknown", "could not be read")]:
        view = SimpleNamespace(read=SimpleNamespace(norm=norm, decided=decided, appeal_link_present=link),
                               appeal_allowed=True, appeals=[{"status": "sent_unconfirmed"}])
        result = recovery_context(view, comparison)
        html = template.render(appeal_workspace={**comparison, "recovery": result}, dispute_id="PP-R-TST-0000002")
        assert expected in html and phrase in html and "do not resend automatically" in html


def test_hosted_recovery_hides_original_download():
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader("copilot/web/templates"), autoescape=True)
    env.globals["hosted_demo"] = lambda: True
    row = {"name": "new.pdf", "delta": "new content", "source_id": "Proofie-document:2", "sha256": "a" * 64, "id": "document:2", "packages": []}
    recovery = {"eligibility": "eligible", "eligibility_reason": "recorded", "evidence_novelty": "new", "next_action": "Review", "source": "recorded", "provider_confirmation": "Readback required"}
    html = env.get_template("features/appeal_workspace/_recovery.html").render(appeal_workspace={"recovery": recovery, "proposed_evidence": [row]}, dispute_id="PP-R-TST-0000002")
    assert "new.pdf" in html and "/documents/2/original" not in html
