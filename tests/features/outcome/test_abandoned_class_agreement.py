"""One status word, one class on every page: an attempt the seller marked as not received reads red (tag--bad) on the
package page, on the dispute page's send task and on the outcome page's send history."""
from _dom import parse
from copilot import config
from copilot.core import send_service
from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.core.checklist_input import extracted_docs_for
from copilot.core.send import build_request_input, pick_tracking, sha256_json
from copilot.db import repo

from outcome_fakes import DISPUTE_ID
from test_outcome_routes import PAGE, _package, _web

WORDS = "You marked it as not shown in PayPal"


def abandoned_attempt(db, pkg):
    """The registry row of the package's content, registered for the package through the audit entry the send writes."""
    tracking, carrier = pick_tracking(extracted_docs_for(db, DISPUTE_ID))
    req = build_request_input(repo.list_claims(db, pkg), tracking, carrier)
    repo.log_action(db, "system", "send_attempt_registered", DISPUTE_ID,
                    {"package_id": pkg, "approval_sha256": "x" * 64, "request_id": "req-demo-1", "attachments": [],
                     "attachment_sha256": []})
    store = SQLiteAttemptStore(db)
    try:
        assert store.claim(DISPUTE_ID, sha256_json(req))
        store.write(DISPUTE_ID, sha256_json(req), {"state": "abandoned", "request_id": "req-demo-1", "request_input": req,
                                                  "attachment_filenames": [], "reason": send_service.ABANDONED_REASON})
    finally:
        store.close()


def test_an_abandoned_attempt_is_red_on_the_package_page_the_dispute_page_and_the_outcome_page(web_mode, monkeypatch):
    pkg = _package()
    abandoned_attempt(config.db_path(), pkg)
    web, fake = _web(monkeypatch, appeal=None)
    package_page = parse(web.get(f"/disputes/{DISPUTE_ID}/packages/{pkg}").text)
    chip = package_page.find("p", data_state="package-chip")
    dispute_page = parse(web.get(f"/disputes/{DISPUTE_ID}").text)
    send_row = dispute_page.find("tr", data_task="send")
    outcome_row = parse(web.get(PAGE).text).find("tr", data_state_value="abandoned")
    for name, node in (("package chip", chip), ("dispute send task", send_row), ("outcome history", outcome_row)):
        assert node is not None, name
        assert WORDS in node.text(), name
        tag = node.find("span", class_="tag")
        assert tag is not None and "tag--bad" in tag.attrs["class"].split(), name
        assert node.find("span", class_="tag--na") is None and node.find("span", class_="tag--info") is None, name
    assert fake.posts() == []
