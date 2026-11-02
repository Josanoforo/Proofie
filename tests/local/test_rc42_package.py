"""Package page: same statements and same files, an earlier send that reached PayPal without every file, the attach name
guard, the counts sentence, the saved-tracking offer, the undo banner, the Resolution Center link, product-voice copy,
closed labels, tables, forms and the print link.

Offline only: fake PayPal clients, a temp database per test (conftest.py). Effects are read from stored rows, the requests
the fake saw and the rendered DOM, never from words alone. Helpers come from the earlier round modules.
"""
import pytest
from _dom import parse
from _hosted import set_test_csrf

from copilot.db import repo

import test_routes_send as send_tests  # noqa: F401
from test_ux_rc41_fix3_state3 import age_attempts, attempts, form_token  # noqa: F401
from test_ux_rc41_fix_state import (LABEL_A, LABEL_B, OTHER, STATEMENT, add_file, posts,  # noqa: F401
                                    two_versions_same_statements)
from test_ux_rc41_fix_state_status import panel, seed_attempt  # noqa: F401
from test_ux_rc41_package import DID, add_claims, approve_passive, draft, env, new_package, page  # noqa: F401

pytestmark = [pytest.mark.usefixtures("web_mode")]

ADVICE = ("Change a statement in this package (for example add one sentence), then mark the earlier send as not "
          "received on this package's page, then send this package.")


def forms_to(dom, suffix):
    return [f for f in dom.find_all("form") if f.attrs.get("action", "").endswith(suffix)]


def actions_of(node):
    out = [(a.text(), a.attrs.get("href")) for a in node.find_all("a")]
    for form in node.find_all("form"):
        for b in form.find_all("button"):
            out.append((b.text(), form.attrs.get("action")))
    return out


def seller_evidence(env, v1, names):
    """PayPal's fresh read now lists evidence submitted by the seller (the statements of v1 with the named files)."""
    from test_ux_rc41_fix_state import confirmed_body
    c, db, token, fake = env
    fake.detail_body = fake.pre_send_get_body = fake.recovery_get_body = {**send_tests.WAITING,
                                                                          **confirmed_body(db, v1, names)}


def twin_unconfirmed(env):
    """v1 was sent and never confirmed (aged); v2 has the same statements AND the same file, approved."""
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    add_file(c, db, token, v1, "a.pdf", LABEL_A)
    approve_passive(c, token, v1)
    seed_attempt(db, v1, "sent_unconfirmed")
    v2 = new_package(c, token)
    add_claims(db, v2, STATEMENT)
    add_file(c, db, token, v2, "a.pdf", LABEL_A)
    approve_passive(c, token, v2)
    assert repo.get_latest_valid_approval(db, v2)
    age_attempts(db)
    return v1, v2


# ===== same statements and same files
def test_same_files_unconfirmed_twin_package_page_is_the_collision_step_with_no_send_form(env):
    c, db, token, fake = env
    v1, v2 = twin_unconfirmed(env)
    html = page(c, v2).text
    node, text = panel(html)
    assert node.attrs["data-next-step"] == "same_statements"
    assert "An earlier send of this exact package is not confirmed." in text
    assert ("Package v1 has the same statements and the same files, and PayPal has not confirmed it. PayPal may "
            "already have it. Proofie will not send it again.") in text
    assert ADVICE in text
    assert not forms_to(parse(html), "/send")
    assert ("Change a statement", f"/disputes/{DID}/packages/{v2}#statements") in actions_of(node)
    block = parse(html).find("details", id="why-blocked")
    assert "1. Change a statement in this package (for example add one sentence)." in block.text()
    assert "1. Press Check again." not in block.text()
    assert posts(fake) == []


def test_same_files_unconfirmed_twin_dispute_page_has_no_send_form_and_the_mark_answer_is_unchanged(env):
    c, db, token, fake = env
    v1, v2 = twin_unconfirmed(env)
    html = c.get(f"/disputes/{DID}").text
    assert panel(html)[0].attrs["data-next-step"] == "same_statements"
    assert not forms_to(parse(html), "/send")
    form = forms_to(parse(page(c, v2).text), "/confirm-not-received")[0]
    before = attempts(db)
    r = c.post(form.attrs["action"], data={"csrf_token": form_token(form), "confirmed": "on"})
    assert "cannot be marked from here yet" in r.text and "Nothing was waiting" not in r.text
    assert attempts(db) == before and posts(fake) == []


def test_same_files_confirmed_twin_offers_no_send_control_and_the_server_still_refuses(env):
    c, db, token, fake = env
    v1, v2 = two_versions_same_statements(env, [("a.pdf", LABEL_A)], [("a.pdf", LABEL_A)])
    seller_evidence(env, v1, ["a.pdf"])
    html = page(c, v2).text
    node, text = panel(html)
    assert node.attrs["data-next-step"] == "same_statements"
    assert "PayPal already has this exact package." in text and "Nothing to send." in text
    assert "Proofie sends evidence once per dispute" in text          # the rule replaces "a changed statement makes a new submission"
    assert "new submission" not in text and "Change a statement" not in text
    assert "Send evidence to PayPal" not in html and not forms_to(parse(html), "/send")
    assert ("View the dispute and PayPal's answer", f"/disputes/{DID}") in actions_of(node)
    set_test_csrf(c, token)
    r = c.post(f"/disputes/{DID}/packages/{v2}/send", data={"csrf_token": token}, follow_redirects=False)
    res = c.get(r.headers["location"])                                       # the send answers 303 to its result page
    assert res.status_code == 409 and "Package already received" in res.text   # the server answer is unchanged
    assert posts(fake) == []


def test_same_files_other_files_case_keeps_its_wording(env):
    """The other-files collision (pinned by test_ux_rc41_fix5_advice.py) is not touched by the same-files branch."""
    c, db, token, fake = env
    v1, v2 = two_versions_same_statements(env, [("a.pdf", LABEL_A)], [("a.pdf", LABEL_A), ("b.pdf", OTHER)])
    text = panel(page(c, v2).text)[1]
    assert "which carried other files" in text and "exact package" not in text


def test_same_files_a_package_with_other_statements_is_not_a_twin(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    add_file(c, db, token, v1, "a.pdf", LABEL_A)
    approve_passive(c, token, v1)
    seed_attempt(db, v1, "sent_unconfirmed")
    v2 = new_package(c, token)
    add_claims(db, v2, "A different statement.")
    add_file(c, db, token, v2, "a.pdf", LABEL_A)
    approve_passive(c, token, v2)
    age_attempts(db)
    node, text = panel(page(c, v2).text)
    assert node.attrs["data-next-step"] == "other_pending" and "exact package" not in text


# ===== an earlier send that reached PayPal without every file
PARTIAL_REASON = "missing_attachment:b.pdf"
V2_CHANGED = "We shipped the parcel with FedEx; the label and the receipt are attached."


def v1_partial(env, *, v2_text=STATEMENT):
    """v1 carried a.pdf and b.pdf, PayPal listed the statements but not b.pdf (a true partial, aged); v2 has `v2_text`
    (the same statements, or a changed one), the same two files, approved."""
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    add_file(c, db, token, v1, "a.pdf", LABEL_A)
    add_file(c, db, token, v1, "b.pdf", LABEL_B)
    approve_passive(c, token, v1)
    seed_attempt(db, v1, "sent_unconfirmed", reason=PARTIAL_REASON)
    v2 = new_package(c, token)
    add_claims(db, v2, v2_text)
    add_file(c, db, token, v2, "a.pdf", LABEL_A)
    add_file(c, db, token, v2, "b.pdf", LABEL_B)
    approve_passive(c, token, v2)
    age_attempts(db)
    return v1, v2


def test_partial_earlier_send_changed_statement_is_paused_with_the_rule_and_no_tick_box(env):
    c, db, token, fake = env
    v1, v2 = v1_partial(env, v2_text=V2_CHANGED)
    seller_evidence(env, v1, ["a.pdf"])
    html = page(c, v2).text
    block = parse(html).find("details", id="why-blocked")
    text = block.text()
    assert "An earlier send reached PayPal without every file" in text
    assert "Proofie sends evidence once per dispute" in text and "is not there" not in text
    assert not forms_to(block, "/confirm-not-received") and "Mark as not received" not in text
    assert "Start a new submission" not in html
    node, step_text = panel(html)
    assert node.attrs["data-next-step"] == "other_pending"
    assert "An earlier send reached PayPal without every file." in step_text
    assert posts(fake) == []


def test_partial_earlier_send_same_statements_after_a_partial_send_is_the_partial_collision_step(env):
    c, db, token, fake = env
    v1, v2 = v1_partial(env)
    seller_evidence(env, v1, ["a.pdf"])
    html = page(c, v2).text
    node, text = panel(html)
    assert node.attrs["data-next-step"] == "same_statements"
    assert "PayPal showed these statements without every file." in text
    assert "When Proofie checked, PayPal showed the statements of package v1 without the file b.pdf." in text and "Proofie sends evidence once per dispute" in text
    assert "mark the earlier send as not received" not in text and ADVICE not in text
    assert "then send this package" not in html and "Change a statement" not in text
    assert "An earlier send of these statements is not confirmed" not in text and "PayPal may already have" not in text
    assert not forms_to(parse(html), "/send") and "Start a new submission" not in html
    block = parse(html).find("details", id="why-blocked").text()
    assert "An earlier send reached PayPal without every file" in block
    assert panel(c.get(f"/disputes/{DID}").text)[0].attrs["data-next-step"] == "same_statements"
    assert posts(fake) == []


def test_partial_earlier_send_an_ordinary_unconfirmed_earlier_send_keeps_the_old_strings(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    approve_passive(c, token, v1)
    seed_attempt(db, v1, "sent_unconfirmed")
    v2 = new_package(c, token)
    add_claims(db, v2, V2_CHANGED)
    age_attempts(db)
    block = parse(page(c, v2).text).find("details", id="why-blocked")
    text = block.text()
    assert "Sending is paused: an earlier send is not confirmed yet" in text
    assert [b.text() for b in forms_to(block, "/confirm-not-received")[0].find_all("button")] == ["Mark as not received"]
    assert "and the earlier send is not there." in forms_to(block, "/confirm-not-received")[0].text() or \
        "and the earlier send is not there." in block.text()
    assert "without every file" not in text and "Start a new submission" not in text


# ===== attach guard for a second file of the same name
def _upload_doc(c, db, token, name, data):
    from test_ux_rc41_package import upload
    before = {d["id"] for d in repo.list_documents(db, DID)}
    assert upload(c, token, name, data).status_code == 303
    return next(d["id"] for d in repo.list_documents(db, DID) if d["id"] not in before)


def test_attach_name_guard_a_second_file_with_the_name_of_an_attached_one_is_refused_and_writes_nothing(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    add_file(c, db, token, pkg, "label.pdf", LABEL_A)
    twin = _upload_doc(c, db, token, "label.pdf", LABEL_B)                     # other bytes, same name
    rows_before = repo.list_attachments(db, pkg)
    audit_before = [e for e in repo.list_audit(db, DID) if e["action"] == "attach_document"]
    set_test_csrf(c, token)
    r = c.post(f"/disputes/{DID}/packages/{pkg}/attach/{twin}", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 400
    assert "Cannot attach this file" in r.text
    assert ("A file named label.pdf is already attached to this package. PayPal lists attached files by name only, so "
            "Proofie can confirm two files only when their names differ. Detach the other file first, or upload this "
            "one again under another name.") in r.text
    assert repo.list_attachments(db, pkg) == rows_before                       # no attachment row
    assert [e for e in repo.list_audit(db, DID) if e["action"] == "attach_document"] == audit_before   # no audit row
    assert posts(fake) == []


def test_attach_name_guard_other_names_and_the_same_document_still_attach(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    first = add_file(c, db, token, pkg, "label.pdf", LABEL_A)
    other = _upload_doc(c, db, token, "receipt.pdf", LABEL_B)
    set_test_csrf(c, token)
    assert c.post(f"/disputes/{DID}/packages/{pkg}/attach/{other}", data={"csrf_token": token},
                  follow_redirects=False).status_code == 303
    assert c.post(f"/disputes/{DID}/packages/{pkg}/attach/{first}", data={"csrf_token": token},
                  follow_redirects=False).status_code == 303                       # the very same document: not a twin
    assert sorted(a["filename"] for a in repo.list_attachments(db, pkg)) == ["label.pdf", "receipt.pdf"]


# ===== counts sentence under the Checks heading
import re  # noqa: E402


def _count_line(dom):
    node = dom.find("p", data_state="checks-count")
    return node, (node.text() if node is not None else "")


def test_counts_sentence_line_sits_under_the_checks_heading_and_equals_the_row_states(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    dom = parse(page(c, pkg).text)
    node, text = _count_line(dom)
    assert node is not None
    rows = [r for r in dom.find_all("tr") if "data-check-state" in r.attrs]
    states = [r.attrs["data-check-state"] for r in rows]
    verified, cannot = states.count("verified"), states.count("present_unverifiable")
    total = sum(1 for s in states if s != "not_applicable")
    assert total > 0
    noun = "check" if total == 1 else "checks"
    assert re.sub(r"\s+([;.])", r"\1", text) == f"{verified} of {total} {noun} verified; {cannot} cannot be checked."
    # it is the first element after the Checks heading, and the Checks table follows it
    main = dom.find("main")
    order = [n for n in main.walk() if n.tag in ("h2", "p", "table") and (n.attrs.get("id") == "checks"
             or n.attrs.get("data-state") == "checks-count" or n.tag == "table")]
    ids = [(n.tag, n.attrs.get("id") or n.attrs.get("data-state")) for n in order]
    assert ids.index(("h2", "checks")) + 1 == ids.index(("p", "checks-count"))
    for link in node.find_all("a"):
        target = link.attrs["href"].lstrip("#")
        assert dom.find("tr", id=target) is not None                             # every count links to a real row
    # the numbers link to the first row of that state, in table order
    if verified:
        first = next(r for r in rows if r.attrs["data-check-state"] == "verified")
        assert any(a.attrs["href"] == "#" + first.attrs["id"] for a in node.find_all("a"))


def test_counts_sentence_never_predicts_an_outcome(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    node, text = _count_line(parse(page(c, pkg).text))
    assert "%" not in text and not re.search(r"likely|chance|odds|probab|win|predict", text, re.I)


def test_counts_sentence_is_absent_without_a_rule_set(env, monkeypatch):
    from copilot.core.checklist import UnknownCountryError
    from copilot.web import routes_package
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)

    def no_rules(*a, **k):
        raise UnknownCountryError("XX")
    monkeypatch.setattr(routes_package, "_checks_for", no_rules)
    html = page(c, pkg).text
    assert _count_line(parse(html))[0] is None and 'data-state="no-rule-set"' in html


# ===== link to the printable copy of a sent version
def _more_views(html):
    return [(a.text(), a.attrs["href"]) for a in parse(html).find("nav", aria_label="More package views").find_all("a")]


def test_copy_link_is_on_the_sent_package_page_only(env, monkeypatch):
    from test_ux_rc41_package import approved, mark_sent
    c, db, token, fake = env
    monkeypatch.setenv("COPILOT_FEATURE_PACKAGE_PRINT", "true")
    open_pkg = approved(c, db, token, STATEMENT)
    print_url = f"/disputes/{DID}/packages/{open_pkg}/print"
    assert (_more_views(page(c, open_pkg).text))[0] == ("Print the evidence package", print_url)
    mark_sent(db, open_pkg)
    links = _more_views(page(c, open_pkg).text)
    assert links[0] == ("Save a copy of what you sent", print_url)
    assert "Print the evidence package" not in [t for t, _ in links]


# ===== the tracking saved for the sale is offered once
CAPTURE = "DEMOTXN0000000001"                      # the seller_transaction_id of the synthetic dispute


_FULFILMENT_DDL = """CREATE TABLE IF NOT EXISTS fulfilment_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT, capture_id TEXT NOT NULL, created_at TEXT NOT NULL, request_json TEXT NOT NULL,
    request_sha256 TEXT NOT NULL, approval_sha256 TEXT NOT NULL, tracker_readback_json TEXT NOT NULL,
    checks_json TEXT NOT NULL, file_sha256 TEXT NOT NULL, status TEXT NOT NULL, limitation_text TEXT)"""


def register_tracker(db, number="DEMO-TRK-77", carrier="FEDEX", status="registered", other=None):
    """A saved tracking file of the sale, written as the tracking form's store writes it (the table of
    copilot/features/fulfilment/store.py; the feature module itself is not imported by this test)."""
    import json
    import sqlite3
    tracker = {"transaction_id": CAPTURE, "tracking_number": number, "carrier": carrier, "status": "SHIPPED"}
    if other:
        tracker["carrier_name_other"] = other
    with sqlite3.connect(db) as conn:
        conn.execute(_FULFILMENT_DDL)
        conn.execute("INSERT INTO fulfilment_files (capture_id, created_at, request_json, request_sha256, approval_sha256, "
                     "tracker_readback_json, checks_json, file_sha256, status, limitation_text) "
                     "VALUES (?, '2026-10-18T00:00:00-0600', ?, ?, ?, 'null', '{}', ?, ?, NULL)",
                     (CAPTURE, json.dumps({"trackers": [tracker]}), "a" * 64, "b" * 64, "c" * 64, status))


def _note(html):
    return parse(html).find("p", data_state="tracker-on-file")


@pytest.fixture
def fulfilment_on(env, monkeypatch):
    monkeypatch.setenv("COPILOT_FEATURE_FULFILMENT", "true")
    return env


def test_saved_tracking_offer_offers_the_registered_tracking_when_the_package_has_none(fulfilment_on):
    c, db, token, fake = fulfilment_on
    register_tracker(db)
    pkg = new_package(c, token)
    add_claims(db, pkg, STATEMENT)
    html = page(c, pkg).text
    note = _note(html)
    assert note is not None
    assert note.text().startswith(f"Tracking DEMO-TRK-77 (FedEx) is saved in Proofie for sale {CAPTURE}.")
    link = [a for a in note.find_all("a") if a.text().startswith("Use these values")][0]
    assert link.attrs["href"] == f"/disputes/{DID}/packages/{pkg}?use_tracking=1#shipment-details"
    assert parse(html).find("input", name="tracking_number").attrs.get("value", "") == ""      # nothing was prefilled
    # the link fills the inputs but writes nothing (no manual field, no audit row, no PayPal call)
    audit_before = len(repo.list_audit(db, DID))
    used = page(c, pkg, "?use_tracking=1").text
    dom = parse(used)
    assert dom.find("input", name="tracking_number").attrs["value"] == "DEMO-TRK-77"
    assert [o for o in dom.find("select", name="carrier").find_all("option") if "selected" in o.attrs][0].attrs["value"] == "FEDEX"
    assert dom.find("details", data_state="shipment-form").attrs.get("open") is not None
    assert "Filled in from the tracking file. Check them, then press Save shipment details." in _note(used).text()
    assert repo.get_manual_fields(db, DID) == {} and len(repo.list_audit(db, DID)) == audit_before
    assert posts(fake) == []


def test_saved_tracking_offer_is_silent_without_a_tracker_or_when_it_is_not_registered(fulfilment_on):
    c, db, token, fake = fulfilment_on
    pkg = new_package(c, token)
    add_claims(db, pkg, STATEMENT)
    assert _note(page(c, pkg).text) is None                                          # no tracker file at all
    register_tracker(db, status="saved_without_registration")
    assert _note(page(c, pkg).text) is None                                          # saved only, never registered
    register_tracker(db, status="registered_unconfirmed")
    assert _note(page(c, pkg).text) is not None                                      # accepted, not confirmed: offered


def test_saved_tracking_offer_is_silent_when_both_places_agree_and_shows_both_when_they_differ(fulfilment_on):
    c, db, token, fake = fulfilment_on
    register_tracker(db, number="DEMO-FDX-000001")
    pkg = draft(c, db, token, STATEMENT)                                              # the shipment form saved DEMO-FDX-000001
    assert _note(page(c, pkg).text) is None
    register_tracker(db, number="DEMO-TRK-88")
    note = _note(page(c, pkg).text)
    assert note is not None
    text = note.text()
    assert "DEMO-FDX-000001" in text and "DEMO-TRK-88" in text                       # both numbers are shown


def test_saved_tracking_offer_needs_the_fulfilment_feature_and_never_shows_on_a_sent_version(env, monkeypatch):
    from test_ux_rc41_package import approved, mark_sent
    c, db, token, fake = env
    register_tracker(db)
    pkg = new_package(c, token)
    add_claims(db, pkg, STATEMENT)
    assert _note(page(c, pkg).text) is None                                          # feature off
    monkeypatch.setenv("COPILOT_FEATURE_FULFILMENT", "true")
    assert _note(page(c, pkg).text) is not None
    set_test_csrf(c, token)
    sent = approved(c, db, token, STATEMENT)
    mark_sent(db, sent)
    assert _note(page(c, sent).text) is None


def test_saved_tracking_offer_keeps_one_primary_button_and_does_not_create_the_table_on_a_page_view(fulfilment_on):
    import sqlite3
    c, db, token, fake = fulfilment_on
    pkg = new_package(c, token)
    add_claims(db, pkg, STATEMENT)
    html = page(c, pkg).text
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='fulfilment_files'").fetchone() is None
    assert len([b for b in parse(html).find_all("button") if "btn-primary" in b.attrs.get("class", "").split()]) <= 1


# ===== undo in the done banner
def _banner(html):
    return parse(html).find("div", data_state="done")


def _undo_form(banner):
    forms = banner.find_all("form") if banner is not None else []
    return forms[0] if forms else None


def _fields(form):
    return {i.attrs.get("name"): i.attrs.get("value") for i in form.find_all("input")}


def test_undo_in_the_detached_banner_posts_to_attach_and_returns_the_attached_banner(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    doc = add_file(c, db, token, pkg, "label.pdf", LABEL_A)
    set_test_csrf(c, token)
    r = c.post(f"/disputes/{DID}/packages/{pkg}/detach/{doc}", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    html = c.get(r.headers["location"]).text
    banner = _banner(html)
    assert banner.attrs["data-done"] == "detached"
    form = _undo_form(banner)
    assert form.attrs["action"] == f"/disputes/{DID}/packages/{pkg}/attach/{doc}" and form.attrs["method"] == "post"
    assert _fields(form)["csrf_token"]
    assert [b.text() for b in form.find_all("button")] == ["Undo removing label.pdf from the response"]
    assert "Undo" in [b.text().split()[0] for b in form.find_all("button")]
    # the undo is the existing guarded route: it attaches again and its own banner carries no undo
    assert repo.list_attachments(db, pkg) == []
    back = c.post(form.attrs["action"], data={"csrf_token": _fields(form)["csrf_token"]}, follow_redirects=False)
    assert back.status_code == 303
    html2 = c.get(back.headers["location"]).text
    assert _banner(html2).attrs["data-done"] == "attached" and _undo_form(_banner(html2)) is None
    assert [a["document_id"] for a in repo.list_attachments(db, pkg)] == [doc]


def test_undo_in_the_excluded_banner_posts_to_include_with_return_to(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    doc = _upload_doc(c, db, token, "label.pdf", LABEL_A)
    set_test_csrf(c, token)
    r = c.post(f"/disputes/{DID}/documents/{doc}/exclude", data={"csrf_token": token, "return_to": f"package:{pkg}"},
               follow_redirects=False)
    assert r.status_code == 303 and f"/packages/{pkg}" in r.headers["location"]
    banner = _banner(c.get(r.headers["location"]).text)
    assert banner.attrs["data-done"] == "document_excluded"
    form = _undo_form(banner)
    assert form.attrs["action"] == f"/disputes/{DID}/documents/{doc}/include"
    fields = _fields(form)
    assert fields["return_to"] == f"package:{pkg}" and fields["csrf_token"]
    assert [b.text() for b in form.find_all("button")] == ["Undo leaving out label.pdf"]
    back = c.post(form.attrs["action"], data={"csrf_token": fields["csrf_token"], "return_to": fields["return_to"]},
                  follow_redirects=False)
    assert back.status_code == 303 and f"/packages/{pkg}" in back.headers["location"]
    html = c.get(back.headers["location"]).text
    assert _banner(html).attrs["data-done"] == "document_included" and _undo_form(_banner(html)) is None
    assert repo.get_document(db, doc)["excluded"] == 0


def test_banners_without_an_undo_and_forged_or_stale_urls(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    doc = add_file(c, db, token, pkg, "label.pdf", LABEL_A)
    # the opposite keys carry no control
    for key in ("attached",):
        banner = _banner(page(c, pkg, f"?done={key}&doc={doc}").text)
        assert banner is not None and _undo_form(banner) is None
    # forged: 'detached' for a file that IS attached, 'document_excluded' for one that is not excluded
    assert _banner(page(c, pkg, f"?done=detached&doc={doc}").text) is None
    assert _banner(page(c, pkg, f"?done=document_excluded&doc={doc}").text) is None
    # a document of nobody: nothing
    assert _banner(page(c, pkg, "?done=detached&doc=999").text) is None


def test_a_sent_version_shows_no_undo(env):
    from test_ux_rc41_package import approved, mark_sent
    c, db, token, fake = env
    pkg = approved(c, db, token, STATEMENT)
    doc = _upload_doc(c, db, token, "label.pdf", LABEL_A)
    mark_sent(db, pkg)                                                              # confirmed, with no files
    banner = _banner(page(c, pkg, f"?done=detached&doc={doc}").text)
    assert banner is None or _undo_form(banner) is None


# ===== Resolution Center link
def _paypal_links(html):
    from urllib.parse import urlsplit
    return [a for a in parse(html).find_all("a") if urlsplit(a.attrs.get("href", "")).netloc.endswith("paypal.com")]


@pytest.mark.parametrize("state", ["other", "twin", "partial"])
def test_resolution_center_words_are_a_sandbox_link_in_every_why_blocked_variant(env, state):
    c, db, token, fake = env
    if state == "other":
        v1 = draft(c, db, token, STATEMENT)
        approve_passive(c, token, v1)
        seed_attempt(db, v1, "sent_unconfirmed")
        v2 = new_package(c, token)
        add_claims(db, v2, V2_CHANGED)
        age_attempts(db)
    elif state == "twin":
        v1, v2 = twin_unconfirmed(env)
    else:
        v1, v2 = v1_partial(env)
        seller_evidence(env, v1, ["a.pdf"])
    html = page(c, v2).text
    links = _paypal_links(html)
    if state == "partial":
        assert links and all(a.attrs["href"] == "https://www.sandbox.paypal.com/resolutioncenter" for a in links)
        assert all(a.attrs["rel"] == "noopener" and a.attrs["target"] == "_blank" for a in links)
        assert not parse(html).find("details", id="why-blocked").find("input", name="confirmed")   # no tick box, no button
        return
    assert len(links) == 2                                                        # the step 2 sentence and the tick box
    for a in links:
        assert a.attrs["href"] == "https://www.sandbox.paypal.com/resolutioncenter"
        assert a.attrs["rel"] == "noopener" and a.attrs["target"] == "_blank"
        assert a.children[0] == "PayPal's Resolution Center"
        assert "(opens in a new tab)" in a.text()
    assert "www.paypal.com/resolutioncenter" not in html
    block = parse(html).find("details", id="why-blocked")
    box = block.find("input", name="confirmed")
    assert box.parent.find("a") is not None                                          # the tick box words carry the link
    assert posts(fake) == []                                                         # a link is display only


# ===== closed words instead of derived labels
def test_a_suggested_reading_of_a_field_outside_the_table_never_prints_the_raw_key(env):
    c, db, token, fake = env
    pkg = new_package(c, token)
    doc = repo.insert_document(db, DID, "photo.png", "image/png", 10, "sha-p", None, False)
    repo.insert_claims(db, pkg, [
        {"evidence_index": 0, "claim_index": 0, "text": "The label shows a date.", "source_ids": [f"doc_{doc}"],
         "basis": "model_interpretation", "suggested_field": "ship_date", "suggested_value": "2026-10-14"},
        {"evidence_index": 0, "claim_index": 1, "text": "The label shows something else.", "source_ids": [f"doc_{doc}"],
         "basis": "model_interpretation", "suggested_field": "weird_new_key", "suggested_value": "x"}])
    dom = parse(page(c, pkg).text)
    lines = [n.text() for n in dom.find_all("p", data_state="reading-suggested")]
    assert len(lines) == 2
    assert lines[0].startswith("Proofie read this from photo.png: Ship date ")
    assert lines[1].startswith("Proofie read this from photo.png: A field Proofie does not name ")
    assert "weird" not in " ".join(lines)


def test_evidence_types_PayPal_asked_for_read_in_words_never_as_a_raw_type():
    from copilot.core import checklist, disputes

    class Collect:
        def __init__(self):
            self.rows = []

        def add(self, check_id, state, message, sources, action=None, **kw):
            self.rows.append((check_id, state, message, kw.get("label")))

    b = Collect()
    checklist._case_requests(b, {"evidence_requested": ["PROOF_OF_FULFILLMENT", "PROOF_OF_REFUND", "OTHER",
                                                          "ODD_NEW_TYPE"], "evidence_submitted": []})
    labels = [r[3] for r in b.rows]
    assert labels == ["PayPal asked for: Proof of fulfillment", "PayPal asked for: Proof of refund",
                      "PayPal asked for: Other", "PayPal asked for: An evidence type Proofie does not name"]
    assert all("_" not in r[2] and "ODD" not in r[2] for r in b.rows)
    assert disputes.EVIDENCE_TYPE_WORDS["PROOF_OF_FULFILLMENT"] == "Proof of fulfillment"
    assert disputes.evidence_type_words(None) == "An evidence type Proofie does not name"
    assert disputes.evidence_type_words("ODD_NEW_TYPE") == "An evidence type Proofie does not name"


def test_no_visible_label_is_derived_with_replace_underscore_in_the_package_and_approval_sources():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "copilot"
    for rel in ("web/routes_package.py", "core/checklist.py", "web/templates/package_review.html",
                "web/templates/features/active_approval/review.html"):
        source = (root / rel).read_text(encoding="utf-8")
        for spelling in (".replace('_'", '.replace("_"', "|replace('_'", '|replace("_"'):
            assert spelling not in source, (rel, spelling)


# ===== the page speaks in the product voice
def test_the_package_page_speaks_in_the_proofie_voice(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, "Shipped with FedEx; the label is attached.")           # no pronoun of the seller's own
    dom = parse(page(c, pkg).text)
    headings = [h.text() for h in dom.find_all("h3")]
    assert "What is known, what is missing, what does not match" in headings
    assert not any("What we know" in h for h in headings)
    main = dom.find("main").text()
    assert not re.search(r"\b(we|our|ours|us)\b", main, re.I), re.search(r".{30}\b(we|our|ours|us)\b.{30}", main, re.I)
    assert "Present but we cannot verify" not in main and "Present, but we cannot verify" not in main


def test_group_title_and_row_tag_say_proofie_cannot_verify(env):
    from copilot.web import routes_package
    titles = dict(routes_package._CHECK_GROUP_TITLES)
    assert titles["present_unverifiable"] == "Present, but Proofie cannot verify"
    assert routes_package._STATE_LABELS["present_unverifiable"] == "Present, but Proofie cannot verify"


def test_the_uncertain_send_banner_says_proofie_does_not_know(env):
    from test_ux_rc41_package import approved, mark_attempt
    c, db, token, fake = env
    pkg = approved(c, db, token, STATEMENT)
    mark_attempt(db, pkg, "sent_unconfirmed")
    text = parse(page(c, pkg).text).find("main").text()
    assert "Proofie does not know whether PayPal has this evidence." in text
    assert "We do not know whether" not in text


# ===== forms without the browser validation bubble
def _required_post_forms(html):
    out = []
    for form in parse(html).find_all("form"):
        if form.attrs.get("method", "").lower() != "post":
            continue
        if any("required" in i.attrs for i in form.find_all("input")):
            out.append(form)
    return out


def test_the_package_forms_with_a_required_control_carry_novalidate_and_keep_aria_required(env):
    c, db, token, fake = env
    # the approve form with its conditional required override (a package with an open notice, legacy approval flow)
    flagged = repo.insert_package(db, DID, "human_edit", True, ["A synthetic notice."])
    add_claims(db, flagged, STATEMENT)
    save_shipment_pkg = flagged
    from test_ux_rc41_package import save_shipment
    save_shipment(c, token, save_shipment_pkg)
    approve_form = [f for f in _required_post_forms(page(c, flagged).text) if f.attrs["action"].endswith("/approve")]
    assert len(approve_form) == 1 and "novalidate" in approve_form[0].attrs
    override = approve_form[0].find("input", name="sufficiency_override")
    assert "required" in override.attrs and override.attrs["aria-required"] == "true"
    # the confirm-not-received form
    set_test_csrf(c, token)
    v1 = draft(c, db, token, STATEMENT)
    approve_passive(c, token, v1)
    seed_attempt(db, v1, "sent_unconfirmed")
    v2 = new_package(c, token)
    add_claims(db, v2, V2_CHANGED)
    age_attempts(db)
    forms = _required_post_forms(page(c, v2).text)
    mark = [f for f in forms if f.attrs["action"].endswith("/confirm-not-received")]
    assert len(mark) == 1 and "novalidate" in mark[0].attrs and "required" in mark[0].find("input", name="confirmed").attrs
    assert all("novalidate" in f.attrs for f in forms)                                # none left on the page


def test_an_empty_post_still_gets_the_proofie_refusal_page_not_a_browser_bubble(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    approve_passive(c, token, v1)
    set_test_csrf(c, token)
    r = c.post(f"/disputes/{DID}/packages/{v1}/confirm-not-received", data={"csrf_token": token})
    assert r.status_code == 400 and "Confirm the PayPal check first" in r.text


# ===== table captions and header scopes
def test_package_tables_have_a_hidden_caption_and_scoped_headers(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    add_file(c, db, token, pkg, "label.pdf", LABEL_A)
    dom = parse(page(c, pkg).text)
    captions = {}
    for table in dom.find_all("table"):
        caption = table.find("caption")
        assert caption is not None, "a table without a caption"
        assert "visually-hidden" in caption.attrs.get("class", "").split()
        captions[caption.text()] = table
        heads = table.find_all("th")
        # O4: the package contents index names each row with a row header (scope="row"); every column header stays scope="col"
        assert heads and all(th.attrs.get("scope") in ("col", "row") for th in heads)
        assert all(th.attrs.get("scope") == "col" for th in heads if th.inside("thead"))
        assert any(th.attrs.get("scope") == "col" for th in heads)
    assert "Files you can add to the response" in captions and "Checks on this package" in captions
    assert len(captions["Checks on this package"].find_all("tr")) > 1


# ===== form convention sentence, final-send sentence, one tracking link target
FINAL = "Sending to PayPal cannot be undone; Proofie sends it once."


def test_one_sentence_says_how_every_mark_on_the_page_works_and_sits_above_the_first_marked_form(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    html = page(c, pkg).text
    dom = parse(html)
    note = dom.find("p", class_="form-convention")
    # O4 folds the whole editable part of the package page in one block ("Open package details"); the sentence is visible
    # whenever a mark is: it sits in that same block, above the first marked form, and in no other fold of its own
    def folds(node):
        out, parent = [], node.parent
        while parent is not None:
            if parent.tag == "details":
                out.append(parent.attrs.get("id") or parent.attrs.get("data-state"))
            parent = parent.parent
        return out
    assert note is not None and folds(note) == ["package-edit-sections"]
    assert all(folds(m) == ["package-edit-sections"] or "package-edit-sections" in folds(m) for m in dom.find_all("strong", class_="field-mark"))
    text = note.text()
    for word in ("Required", "sending", "open notices", "PayPal's Seller Protection rules", "Needed", "checks red", "optional"):
        assert word in text, word
    long_note = dom.find("p", class_="form-convention-long")
    assert long_note is not None and long_note.inside("details")
    for phrase in ("Required to send", "Required while a notice is open", "Required by PayPal's Seller Protection rules", "Needed", "optional"):
        assert phrase in long_note.text(), phrase
    marks = {m.text() for m in dom.find_all("strong", class_="field-mark")}
    assert marks and all(m.startswith(("Required", "Needed")) for m in marks)          # every mark is covered by the sentence
    assert html.index('class="form-convention"') < html.index('class="field-mark"')   # above the first mark of the page
    form = [f for f in dom.find_all("form") if f.attrs.get("action", "").endswith("/manual-fields")][0]
    labels = " ".join(label.text() for label in form.find_all("label"))
    assert not re.search(r"\boptional\b", labels, re.I)                                # never both conventions on the form


def test_published_policy_source_notes_are_closed_until_requested(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    dom = parse(page(c, pkg).text)
    notes = dom.find("details", data_state="eligibility-sources")
    if notes:
        assert "open" not in notes.attrs
        assert notes.find("summary").text().startswith("Published policy sources")
        assert "Source:" in notes.text() and "Published" in notes.text()


def test_a_sent_version_has_no_convention_sentence_because_it_has_no_form(env):
    from test_ux_rc41_package import approved, mark_sent
    c, db, token, fake = env
    pkg = approved(c, db, token, STATEMENT)
    mark_sent(db, pkg)
    assert parse(page(c, pkg).text).find("p", class_="form-convention") is None


def test_the_send_step_says_before_the_button_that_sending_cannot_be_undone(env):
    from test_ux_rc41_package import approved
    c, db, token, fake = env
    pkg = approved(c, db, token, STATEMENT)
    set_test_csrf(c, token)
    html = page(c, pkg).text
    node, text = panel(html)
    assert node.attrs["data-next-step"] == "send"
    assert FINAL in text
    assert text.index(FINAL) < text.index("Send evidence to PayPal")                    # before the send control
    assert "Sends package v1" in text                                                   # the note stays as built
    # the sentence is true: a repeat send is refused by the server itself
    form = forms_to(parse(html), "/send")[0]
    assert form.find("button") is not None


def test_the_send_step_keeps_one_sentence_when_the_last_attempt_was_refused_or_blocked(env):
    from test_ux_rc41_package import approved, mark_attempt
    c, db, token, fake = env
    pkg = approved(c, db, token, STATEMENT)
    mark_attempt(db, pkg, "failed")
    node, text = panel(page(c, pkg).text)
    assert node.attrs["data-next-step"] == "send" and text.count(FINAL) == 1
    assert "PayPal did not accept the last attempt." in text
    assert text.index(FINAL) < text.index("Send evidence to PayPal")                    # before the control, never in the note


def test_the_no_tracking_link_has_one_target_on_the_whole_page(env):
    from test_ux_rc41_package import approve_passive
    c, db, token, fake = env
    pkg = new_package(c, token)
    add_claims(db, pkg, STATEMENT)
    approve_passive(c, token, pkg)
    dom = parse(page(c, pkg).text)
    targets = {a.attrs["href"] for a in dom.find_all("a") if a.text().strip().lower() == "type the tracking number"}
    assert len(targets) == 1 and targets == {"#field-tracking_number"}


def test_the_withheld_send_sentence_is_short_and_keeps_its_meaning(env):
    from test_ux_rc41_package import approved, mark_attempt
    c, db, token, fake = env
    pkg = approved(c, db, token, STATEMENT)
    mark_attempt(db, pkg, "sent_unconfirmed")
    withheld = parse(page(c, pkg).text).find("p", data_state="send-withheld").text()
    assert withheld == ("A send of this content may already have reached PayPal, so there is no Send button until Check "
                        "again shows what PayPal has.")
    assert len(withheld.split()) < 25


# ===== status label of an abandoned partial row
def test_an_abandoned_partial_row_reads_differently_from_an_ordinary_abandoned_one():
    from copilot.core import send_service
    from copilot.web import status
    partial = status.SendRecord(state="abandoned", attempt={"reason": send_service.ABANDONED_PARTIAL_REASON})
    ordinary = status.SendRecord(state="abandoned", attempt={"reason": send_service.ABANDONED_REASON})
    for view in (status.evidence_status, status.package_status):
        assert view(partial).label == "You marked it as not received; when Proofie checked, PayPal showed the statements without the file"
        assert view(ordinary).label == "You marked it as not shown in PayPal"
        assert view(partial).kind == view(ordinary).kind == "bad"          # red on every page


# ===== further package page behaviour
def test_a_blocked_last_attempt_also_carries_the_sentence_before_the_control(env):
    from test_ux_rc41_package import approved, mark_attempt
    c, db, token, fake = env
    pkg = approved(c, db, token, STATEMENT)
    mark_attempt(db, pkg, "blocked")
    node, text = panel(page(c, pkg).text)
    assert node.attrs["data-next-step"] == "send" and text.count(FINAL) == 1
    assert text.index(FINAL) < text.index("Send evidence to PayPal")
    assert not parse(page(c, pkg).text).find("p", class_="action-note").text().endswith(FINAL)


def test_an_abandoned_true_partial_send_is_worded_as_such_on_both_pages_with_the_real_constant(env):
    from copilot.core import send_service
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    approve_passive(c, token, v1)
    seed_attempt(db, v1, "abandoned", reason=send_service.ABANDONED_PARTIAL_REASON)
    node, text = panel(page(c, v1).text)
    assert node.attrs["data-next-step"] == "abandoned"
    assert "You marked the earlier send as not received." in text and "When Proofie checked, PayPal showed its statements without the file." in text
    assert "not shown in PayPal" not in text
    dnode, dtext = panel(c.get(f"/disputes/{DID}").text)
    assert dnode.attrs["data-next-step"] == "abandoned"
    assert "When Proofie checked, PayPal showed the statements of package v1 without the file." in dtext
    assert "Nothing has reached PayPal yet" not in dtext


def test_an_ordinary_abandoned_row_keeps_its_old_words(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    approve_passive(c, token, v1)
    seed_attempt(db, v1, "abandoned", reason="vendor confirmed PayPal's Resolution Center does not show it")
    node, text = panel(page(c, v1).text)
    assert node.attrs["data-next-step"] == "abandoned" and "You marked the earlier send as not received." in text


def test_check_again_on_the_same_files_twin_answers_from_the_twins_row(env):
    from test_ux_rc41_fix_state import confirmed_body
    c, db, token, fake = env
    v1, v2 = twin_unconfirmed(env)
    form = [f for f in parse(page(c, v2).text).find_all("form") if f.attrs.get("action", "").endswith("/check")][0]
    tok = form_token(form)
    r = c.post(form.attrs["action"], data={"csrf_token": tok})
    assert "Nothing pending" not in r.text and "Still not confirmed by PayPal" in r.text
    assert posts(fake) == []
    fake.recovery_get_body = {**send_tests.WAITING, **confirmed_body(db, v1, ["a.pdf"])}      # PayPal now lists v1
    set_test_csrf(c, token)
    r = c.post(form.attrs["action"], data={"csrf_token": token})
    assert "Received by PayPal" in r.text and "Nothing pending" not in r.text
    assert posts(fake) == [] and [a["state"] for a in attempts(db)] == ["confirmed"]


def test_check_again_on_the_partial_collision_says_what_paypal_shows(env):
    c, db, token, fake = env
    v1, v2 = v1_partial(env)
    form = [f for f in parse(page(c, v2).text).find_all("form") if f.attrs.get("action", "").endswith("/check")][0]
    r = c.post(form.attrs["action"], data={"csrf_token": form_token(form)})
    assert "Nothing pending" not in r.text and "b.pdf" in r.text
    assert posts(fake) == []


def test_a_mixed_history_of_partial_and_ordinary_unconfirmed_sends_uses_the_ordinary_words(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    approve_passive(c, token, v1)
    seed_attempt(db, v1, "sent_unconfirmed", reason="missing_attachment:b.pdf")
    v2 = new_package(c, token)
    add_claims(db, v2, "Another statement.")
    save_shipment_for_v2 = v2                                                   # the shipment details are shared
    approve_passive(c, token, v2)
    seed_attempt(db, v2, "sent_unconfirmed", request_id="req-v2")
    v3 = new_package(c, token)
    add_claims(db, v3, V2_CHANGED)
    age_attempts(db)
    block = parse(page(c, v3).text).find("details", id="why-blocked").text()
    assert "Sending is paused: an earlier send is not confirmed yet" in block
    assert "without every file" not in block and "Start a new submission" not in block


def test_the_counts_sentence_pluralises_and_is_absent_when_nothing_counts():
    from copilot.core.checklist import Check
    from copilot.web import routes_package

    def check(i, state):
        return Check(id=i, label_for_seller=i, state=state, basis="case_request", basis_ref="r", sources=[],
                     action="none", risk=False, seller_message="m")
    one = routes_package._checks_count([check("a", "verified")])
    assert one["line"] == "1 of 1 check verified; 0 cannot be checked."
    many = routes_package._checks_count([check("a", "verified"), check("b", "present_unverifiable"), check("c", "missing")])
    assert many["line"] == "1 of 3 checks verified; 1 cannot be checked."
    assert routes_package._checks_count([check("a", "not_applicable")]) is None and routes_package._checks_count([]) is None


def test_the_package_page_names_the_real_tag_and_one_phrase_for_the_basis(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    html = page(c, pkg).text
    assert "What PayPal did not return" in html and "could not tell us" not in html
    from copilot.web import routes_package
    assert routes_package._BASIS_LABELS["app_quality_rule"] == "Proofie's own quality rule"
    assert "This app's own quality rule" not in html


def test_undo_for_a_removal_needs_an_audit_entry_of_that_removal_on_this_package(env):
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    doc = _upload_doc(c, db, token, "label.pdf", LABEL_A)                         # uploaded, never attached to this package
    set_test_csrf(c, token)
    assert _banner(page(c, pkg, f"?done=detached&doc={doc}").text) is None        # nothing was removed: no banner, no undo
    set_test_csrf(c, token)
    add_file_doc = add_file(c, db, token, pkg, "second.pdf", LABEL_B)
    set_test_csrf(c, token)
    c.post(f"/disputes/{DID}/packages/{pkg}/detach/{add_file_doc}", data={"csrf_token": token}, follow_redirects=False)
    assert _undo_form(_banner(page(c, pkg, f"?done=detached&doc={add_file_doc}").text)) is not None


def test_field_errors_on_the_package_page_use_the_shared_error_glyph(env):
    c, db, token, fake = env
    pkg = new_package(c, token)
    set_test_csrf(c, token)
    r = c.post(f"/disputes/{DID}/packages/{pkg}/claims", data={"csrf_token": token, "text": "  "})
    assert r.status_code == 400
    err = parse(r.text).find("p", id="error-new-claim")
    assert err is not None and "field-error" in err.attrs["class"].split()
    assert err.find("span", aria_hidden="true") is not None and err.text().endswith("Enter a statement before saving.")


def test_saved_tracking_offer_with_a_carrier_typed_by_name(fulfilment_on):
    c, db, token, fake = fulfilment_on
    register_tracker(db, number="DEMO-OTH-1", carrier="OTHER", other="Acme Post")
    pkg = new_package(c, token)
    add_claims(db, pkg, STATEMENT)
    note = _note(page(c, pkg).text)
    assert note is not None and note.text().startswith(f"Tracking DEMO-OTH-1 (Acme Post) is saved in Proofie for sale {CAPTURE}.")
    dom = parse(page(c, pkg, "?use_tracking=1").text)
    assert dom.find("input", name="carrier_other_name").attrs["value"] == "Acme Post"
    assert [o for o in dom.find("select", name="carrier").find_all("option") if "selected" in o.attrs][0].attrs["value"] == "OTHER"
