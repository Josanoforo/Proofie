"""Print view: the header of what was sent per attempt state, the link 'Save a copy of what you sent', and the page
structure (one h1, one main). Synthetic data only; the page is read-only."""
import re
import sqlite3

import pytest

from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.core.send import build_request_input, sha256_json
from copilot.db import repo
from copilot.features.package_print import service
from _dom import parse

from .test_print import DID, files, prepared, url  # noqa: F401


def durable(db):
    """The database file and the uploads. A database already in WAL mode (an attempt was stored) keeps its -wal/-shm
    auxiliaries, which SQLite itself maintains for any reader; the business content is the main file."""
    import hashlib
    from pathlib import Path
    return (hashlib.sha256(Path(db).read_bytes()).hexdigest(),
            {k: v for k, v in files(Path(db).parent).items() if not k.endswith(("-wal", "-shm"))})

TRACKING = "DEMO-TRK-0001"


def seed_attempt(db, pid, state="confirmed", *, names=("demo.txt",), reason=None, registered=True, tracking=TRACKING,
                 carrier="FEDEX", registered_for=None, other_name=None, request_id="req-demo-1", sha_salt=""):
    """An attempt of this package as the send path stores it: the registry row and the audit entry that registers it."""
    claims = repo.list_claims(db, pid)
    req = build_request_input(claims, tracking, carrier)
    if other_name:
        req["evidences"][0]["evidence_info"]["tracking_info"][0].update(carrier_name="OTHER", carrier_name_other=other_name)
    shas = [a["sha256"] for a in repo.list_attachments(db, pid)] if names else []     # no names: the attempt carried no file
    if registered:
        repo.log_action(db, "system", "send_attempt_registered", DID,
                        {"package_id": registered_for or pid, "approval_sha256": "x" * 64, "request_id": request_id,
                         "attachments": list(names), "attachment_sha256": shas})
    store = SQLiteAttemptStore(db)
    try:
        sha = sha256_json(req) + sha_salt
        assert store.claim(DID, sha)
        store.write(DID, sha, {"state": state, "request_id": request_id, "request_input": req,
                               "attachment_filenames": list(names), "attachment_sha256": shas,
                               **({"reason": reason} if reason else {})})
    finally:
        store.close()


def body(html):
    return parse(html)


def test_a_sent_version_prints_dispute_tracking_carrier_files_and_the_status_line(prepared):
    client, db, uploads, pid, doc, sha = prepared
    seed_attempt(db, pid)
    before = durable(db)
    response = client.get(url(pid))
    assert response.status_code == 200, response.text
    dom = body(response.text)
    assert "<title>Evidence package v1 - dispute PP-R-DEMO-0000001</title>" in response.text
    header = dom.find("div", data_state="print-header")
    assert header is not None
    text = header.text()
    assert "Dispute: PP-R-DEMO-0000001" in text and "Version 1" in text
    assert f"Tracking number: {TRACKING}" in text and "FedEx" in text.replace("Fedex", "FedEx")
    assert "Files sent: demo.txt" in text
    assert dom.find("p", data_state="print-status").text().startswith("PayPal's record showed it at ")
    assert text.endswith("when Proofie checked.")
    # the fingerprint stays OPEN on page 1, after the new header block
    assert re.search(r'<details open><summary>Technical details</summary><p>Local package record: ', response.text)
    assert response.text.index('data-state="print-header"') < response.text.index("Local package record")
    assert sha in response.text
    assert durable(db) == before                                               # read-only


def test_a_never_sent_package_says_so_and_prints_no_tracking_or_files(prepared):
    client, db, uploads, pid, doc, sha = prepared
    response = client.get(url(pid))
    assert response.status_code == 200
    dom = body(response.text)
    assert dom.find("p", data_state="print-status").text() == "Not sent to PayPal"
    text = dom.find("div", data_state="print-header").text()
    assert "Dispute: PP-R-DEMO-0000001" in text and "Tracking number" not in text and "Files sent" not in text


@pytest.mark.parametrize("state, status_line, held", [
    ("confirmed", None, True),
    ("sent_unconfirmed", "Send attempted - not confirmed by PayPal", False),
    ("failed", "PayPal did not accept it", False),
    ("blocked", "Not sent: Proofie stopped it", False),
    ("abandoned", "You marked it as not shown in PayPal", False),
    ("pending", "Sending started - not confirmed yet", False),
])
def test_the_whole_header_per_attempt_state_never_says_sent_for_an_attempt_nobody_holds(prepared, state, status_line, held):
    client, db, uploads, pid, doc, sha = prepared
    seed_attempt(db, pid, state)
    response = client.get(url(pid))
    assert response.status_code == 200
    dom = body(response.text)
    text = dom.find("div", data_state="print-header").text()
    if held:
        assert f"Tracking number: {TRACKING}" in text and "Files sent: demo.txt" in text
        assert dom.find("p", data_state="print-status").text().startswith("PayPal's record showed it at ")
    else:
        assert f"Tracking number in this attempt: {TRACKING}" in text and "Files in this attempt: demo.txt" in text
        assert "Files sent" not in text and f"Tracking number: {TRACKING}" not in text
        assert dom.find("p", data_state="print-status").text() == status_line
        assert "record showed it" not in text


def test_a_partial_send_is_held_by_paypal_and_names_the_files_it_does_not_list(prepared):
    client, db, uploads, pid, doc, sha = prepared
    seed_attempt(db, pid, "sent_unconfirmed", reason="missing_attachment:demo.txt")
    dom = body(client.get(url(pid)).text)
    assert dom.find("p", data_state="print-status").text() == "Statements in PayPal, file missing: demo.txt"
    text = dom.find("div", data_state="print-header").text()
    assert "Files sent: demo.txt" in text and f"Tracking number: {TRACKING}" in text


def test_an_abandoned_true_partial_send_reads_as_such_with_the_real_constant(prepared):
    from copilot.core import send_service
    client, db, uploads, pid, doc, sha = prepared
    seed_attempt(db, pid, "abandoned", reason=send_service.ABANDONED_PARTIAL_REASON)
    dom = body(client.get(url(pid)).text)
    assert dom.find("p", data_state="print-status").text() == (
        "You marked it as not received; when Proofie checked, PayPal showed the statements without the file")


def test_a_confirmed_send_with_a_file_added_afterwards_says_so(prepared):
    client, db, uploads, pid, doc, sha = prepared
    seed_attempt(db, pid, "confirmed", names=())                               # the confirmed send carried no file
    status_line = body(client.get(url(pid)).text).find("p", data_state="print-status").text()
    assert status_line == "Confirmed in PayPal, a file was added afterwards"


def test_a_confirmed_attempt_wins_over_a_later_failed_one_for_the_same_package(prepared):
    client, db, uploads, pid, doc, sha = prepared
    seed_attempt(db, pid, "confirmed")
    seed_attempt(db, pid, "failed", request_id="req-demo-2", sha_salt="-later", tracking="DEMO-TRK-0002")
    dom = body(client.get(url(pid)).text)
    assert dom.find("p", data_state="print-status").text().startswith("PayPal's record showed it at ")
    assert f"Tracking number: {TRACKING}" in dom.find("div", data_state="print-header").text()


def test_an_attempt_registered_for_another_package_proves_nothing_here(prepared):
    client, db, uploads, pid, doc, sha = prepared
    other = repo.insert_package(db, DID, "human_edit", True, [])
    seed_attempt(db, pid, registered_for=other)                                # same statements, owned by another package
    dom = body(client.get(url(pid)).text)
    assert dom.find("p", data_state="print-status").text() == "Not sent to PayPal"
    assert "Tracking number" not in dom.find("div", data_state="print-header").text()


def test_an_unregistered_attempt_counts_only_when_it_carries_this_packages_statements(prepared):
    client, db, uploads, pid, doc, sha = prepared
    seed_attempt(db, pid, registered=False)                                    # the content fallback of the package page
    dom = body(client.get(url(pid)).text)
    assert dom.find("p", data_state="print-status").text().startswith("PayPal's record showed it at ")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE claims SET text=? WHERE package_id=?", ("Synthetic shipment proof, changed.", pid))
    dom = body(client.get(url(pid)).text)
    assert dom.find("p", data_state="print-status").text() == "Not sent to PayPal"


@pytest.mark.parametrize("field", ["tracking", "filename", "carrier_name_other", "missing_file"])
def test_a_value_that_fails_the_privacy_filter_refuses_the_page(prepared, field):
    client, db, uploads, pid, doc, sha = prepared
    private = "someone@example.com"
    if field == "tracking":
        seed_attempt(db, pid, tracking=private)
    elif field == "filename":
        seed_attempt(db, pid, names=(private + ".pdf",))
    elif field == "carrier_name_other":
        seed_attempt(db, pid, other_name=private)
    else:
        seed_attempt(db, pid, "sent_unconfirmed", reason="missing_attachment:" + private + ".pdf")
    before = durable(db)
    response = client.get(url(pid))
    assert response.status_code == 400 and private not in response.text
    assert durable(db) == before


def test_the_service_header_is_plain_data_without_a_connection_left_open(prepared):
    client, db, uploads, pid, doc, sha = prepared
    seed_attempt(db, pid)
    package = service.read(db, uploads, DID, pid)
    header = package["header"]
    assert header["dispute_id"] == DID and header["state"] == "confirmed"
    assert header["tracking"] == TRACKING and header["files"] == ["demo.txt"] and header["when"]
    with sqlite3.connect(db) as conn:                                         # no read transaction left behind
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()


# ----------------------------------------------------------------------------------- A11Y-STRUCTURE (print page)
def long_package(db):
    claim = repo.list_claims(db, 1)[0]
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE claims SET text=? WHERE id=?", ("Synthetic word. " * 500, claim["id"]))


def test_the_print_page_has_one_h1_one_main_and_every_sheet_inside_it(prepared):
    client, db, uploads, pid, doc, sha = prepared
    long_package(db)
    response = client.get(url(pid))
    assert response.status_code == 200
    dom = body(response.text)
    sheets = dom.find_all("section", class_="sheet")
    assert len(sheets) > 2                                                    # several sheets, as before
    assert len(dom.find_all("h1")) == 1 and len(dom.find_all("main")) == 1
    main = dom.find("main")
    assert all(s.inside("main") for s in sheets)
    assert main.find("h1").text() == "Evidence package v1 - dispute PP-R-DEMO-0000001"
    # the other sheets repeat the title as a plain line, not a heading
    for s in sheets[1:]:
        assert s.find("h1") is None
        assert "Evidence package v1 - dispute PP-R-DEMO-0000001" in s.find("p", class_="sheet-title").text()
    assert response.text.count('class="sheet"') == len(sheets)


def test_the_print_page_title_names_version_and_dispute(prepared):
    client, db, uploads, pid, doc, sha = prepared
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE packages SET version=? WHERE id=?", (7, pid))
    html = client.get(url(pid)).text
    assert "<title>Evidence package v7 - dispute PP-R-DEMO-0000001</title>" in html


def test_the_print_page_styles_a_focus_ring_forced_colours_and_44px_targets(prepared):
    client, db, uploads, pid, doc, sha = prepared
    html = client.get(url(pid)).text
    css = html.split("<style>", 1)[1].split("</style>", 1)[0]
    assert re.search(r"a:focus-visible,\s*summary:focus-visible\s*\{\s*outline:\s*3px solid #[0-9a-f]{6}", css)
    assert re.search(r"@media \(forced-colors: active\)\s*\{[^}]*outline:\s*3px solid Highlight", css)
    assert re.search(r"summary\s*\{[^}]*min-height:\s*44px", css)
    assert re.search(r"\.screen-only a\s*\{[^}]*min-height:\s*44px", css)
    assert "<script" not in html and "https://" not in html                      # the page stays self-contained
