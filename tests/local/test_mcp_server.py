"""Offline tests for copilot/mcp_server.py (stdio MCP server for the seller's AI assistant).

Everything here uses a scripted FakeClient (no PayPal, no Ollama); conftest.py's autouse fixtures isolate the
DB and block real network. The one stdio test launches the REAL server as a subprocess and only lists its
tools (no tool call, so it never touches the real data/ database).
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from copilot import config, mcp_server
from copilot.core import send_recovery
from copilot.core.send import DEMO_PREFIX, approval_fingerprint_with_attachments, build_request_input, pick_tracking
from copilot.core.checklist_input import extracted_docs_for
from copilot.db import repo

REC = ROOT / "pilot" / "fixtures" / "synthetic"
WAITING = json.loads((REC / "dispute_PP-R-DEMO-0000001_waiting_for_seller.json").read_text(encoding="utf-8"))["body"]
# MCP send scenarios require a sendable case, not an expired historical deadline.
WAITING["seller_response_due_date"] = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
LIST_BODY = json.loads((REC / "disputes_list_page1.json").read_text(encoding="utf-8"))["body"]
DID = "PP-R-DEMO-0000001"
TRACKING = "DEMO-FDX-000001"


# PayPal's own GET must show the SAME notes a real send would carry, or a "confirmed"-looking
# fixture no longer counts as matching (verify_after_with_attachments now checks notes_match too). Every
# _package() below adds exactly one claim, "Shipped by FedEx." - this is the exact notes that would build.
NOTES_ONE_CLAIM = (DEMO_PREFIX + "Shipped by FedEx.").strip()


def _confirmed_body(notes=NOTES_ONE_CLAIM):
    return {"evidences": [{"source": "SUBMITTED_BY_SELLER", "evidence_type": "PROOF_OF_FULFILLMENT",
                           "evidence_info": {"tracking_info": [{"tracking_number": TRACKING}]},
                           "notes": notes, "date": "2026-10-05T00:00:00Z"}]}


class FakeClient:
    def __init__(self, detail=WAITING, post_send_gets=None):
        self.detail = detail
        self.post_send_gets = list(post_send_gets or [])
        self.ops = []
        self.posts = []

    def call(self, method, path, op, **kw):
        self.ops.append((method, op))
        if method == "POST":
            self.posts.append(kw)
            return {}
        if op == "list_disputes":
            return LIST_BODY
        if op in ("mcp_show_dispute", "pre_send_get", "recovery_get"):
            return self.detail
        if op == "post_send_get":
            return self.post_send_gets.pop(0)
        if op == "context_transaction_search":
            return {"transaction_details": []}
        if op == "context_tracking":
            return {"trackers": []}
        raise AssertionError(f"unexpected op {op}")


@pytest.fixture
def fake(monkeypatch):
    f = FakeClient()
    monkeypatch.setattr(mcp_server, "_make_client", lambda: f)
    monkeypatch.setattr(send_recovery.time, "sleep", lambda s: None)
    monkeypatch.delenv(mcp_server.ALLOW_SEND_ENV, raising=False)
    return f


def _package(approved=True, with_tracking=True):
    db = config.db_path()
    if with_tracking:
        repo.set_manual_fields(db, DID, {"tracking_number": {"value": TRACKING, "confidence": "read"},
                                         "carrier": {"value": "FedEx", "confidence": "read"}})
    pid = repo.insert_package(db, DID, source="human_edit", validation_ok=True, review_flags=[])
    repo.insert_claims(db, pid, [{"evidence_index": 0, "claim_index": 0, "text": "Shipped by FedEx.",
                                  "source_ids": [], "basis": "seller_statement"}])
    if approved:
        tn, car = pick_tracking(extracted_docs_for(db, DID))
        req = build_request_input(repo.list_claims(db, pid), tn, car)
        fp = approval_fingerprint_with_attachments(DID, "provide_evidence", req, [])
        repo.insert_approval(db, pid, fp, "vendor")
    return pid


def _audit_entries(dispute_id=DID):
    return [e for e in repo.list_audit(config.db_path(), dispute_id) if e["action"].startswith("mcp_")]


# ---------------------------------------------------------------------------------------- tool shapes
def test_list_disputes_shape(fake):
    out = mcp_server.list_disputes()
    assert out["count"] == 1
    row = out["disputes"][0]
    assert row["dispute_id"] == DID
    assert row["status"] == "Waiting for your response"          # humanized, not the raw enum
    assert row["auto_prep"]["status"] == "not_prepared"
    assert row["review_url"] == f"http://127.0.0.1:8000/disputes/{DID}"
    assert "cannot approve" in out["note"]
    assert {m for m, _ in fake.ops} == {"GET"}


def test_get_dispute_file_shape(fake):
    _package()
    out = mcp_server.get_dispute_file(DID, include_text=True)
    assert out["dispute"]["dispute_id"] == DID
    assert out["checklist"] and {"id", "what", "state", "basis", "message", "action"} <= set(out["checklist"][0])
    assert isinstance(out["context_limitations"], list)
    draft = out["draft"]
    # An approved-but-never-sent package reads "Approved", not "Not sent yet" (that is now
    # reserved for a package with no valid approval at all).
    assert draft["approval"] == "approved" and draft["send_status"] == "Approved"
    assert draft["review_url"].endswith(f"/disputes/{DID}/packages/{draft['package_id']}")
    assert out["review_url"] == f"http://127.0.0.1:8000/disputes/{DID}"
    assert all(m == "GET" for m, _ in fake.ops)


def test_get_dispute_file_without_draft(fake):
    assert mcp_server.get_dispute_file(DID)["draft"] is None


# MCP results reach the client's model provider, so claim and document text are opt-in.
def test_get_dispute_file_omits_claim_and_document_text_by_default(fake):
    _package()
    out = mcp_server.get_dispute_file(DID)
    assert out["text_included"] is False and "include_text=true" in out["text_note"]
    assert out["checklist"] and all("message" not in c for c in out["checklist"])
    assert {"id", "what", "state", "basis", "action"} <= set(out["checklist"][0])      # the structure stays useful
    draft = out["draft"]
    assert "claims" not in draft and draft["claim_count"] == 1
    assert "Shipped by FedEx." not in json.dumps(out)                                   # the claim text is nowhere
    assert draft["approval"] == "approved" and draft["review_url"].endswith(f"/packages/{draft['package_id']}")
    assert all(m == "GET" for m, _ in fake.ops)


def test_get_dispute_file_include_text_returns_claims_and_messages(fake):
    _package()
    out = mcp_server.get_dispute_file(DID, include_text=True)
    assert out["text_included"] is True and out["text_note"] is None
    assert all(c["message"] for c in out["checklist"])
    assert out["draft"]["claims"] == [{"text": "Shipped by FedEx.", "basis": "seller_statement", "source_ids": []}]      # O2: each claim carries the sources it cites
    assert out["draft"]["claim_count"] == 1


def test_include_text_must_be_exactly_true(fake):
    """A truthy non-bool (for example the string 'false') must not switch the text on."""
    _package()
    out = mcp_server.get_dispute_file(DID, include_text="false")
    assert out["text_included"] is False and "claims" not in out["draft"]


def test_include_text_is_audited_and_advertised_in_the_tool_schema(fake):
    mcp_server.get_dispute_file(DID, include_text=True)
    entries = [e for e in _audit_entries() if e["action"] == "mcp_get_dispute_file"]
    assert entries and entries[-1]["detail"]["include_text"] is True
    import inspect
    assert inspect.signature(mcp_server.get_dispute_file).parameters["include_text"].default is False


def test_what_is_missing_keeps_its_messages(fake):
    """The other tool is not part of the opt-in text change: it still returns the per-check message."""
    assert all(c["message"] for c in mcp_server.what_is_missing(DID)["needs_action"])


def test_what_is_missing_only_lists_checks_needing_action(fake):
    out = mcp_server.what_is_missing(DID)
    assert out["count"] == len(out["needs_action"]) > 0           # nothing uploaded: things are missing
    assert all(c["state"] in ("missing", "present_unverifiable", "conflict") for c in out["needs_action"])
    assert all(c["action"] and c["message"] for c in out["needs_action"])


def test_prepare_draft_drafts_only_and_is_idempotent(fake):
    repo.set_manual_fields(config.db_path(), DID, {"tracking_number": {"value": TRACKING, "confidence": "read"}})
    first = mcp_server.prepare_draft(DID)
    # a manual tracking field alone creates an auto_manual SHELL with no claims yet - "started",
    # not "ready"/"prepared" (that distinction needs actual content, same as the inbox).
    assert first["prepared"] is False and first["auto_prep"] == "ready"
    assert "web UI" in first["message"]
    second = mcp_server.prepare_draft(DID)
    assert second["prepared"] is False and "Already prepared" in second["message"]
    pkgs = repo.list_packages(config.db_path(), DID)
    assert len(pkgs) == 1 and pkgs[0]["source"] == "auto_manual"
    assert repo.get_latest_valid_approval(config.db_path(), pkgs[0]["id"]) is None   # never approves
    assert fake.posts == []                                                          # never sends
    # the claim audit entry must say it came from the MCP path, not the web inbox.
    claimed = [e for e in repo.list_audit(config.db_path(), DID) if e["action"] == "auto_prep_claimed"]
    assert len(claimed) == 1 and claimed[0]["detail"]["via"] == "mcp"


def test_prepare_draft_reports_prepared_true_only_with_real_content(fake, monkeypatch):
    """'prepared' is True only once the draft actually has claims - a bare empty auto_manual shell
    (the other test above) is 'ready' at the registry level but not meaningfully 'prepared' yet."""
    db = config.db_path()
    repo.insert_document(db, DID, "a.pdf", "application/pdf", 10, "sha1", "Carrier: Estafeta", True)
    monkeypatch.setattr(mcp_server.auto_prep, "propose_package", lambda *a, **kw: {
        "validation_ok": True, "review_flags": [], "model": "qwen3.5:9b", "prompt_sha256": "abc",
        "raw_output": "{}", "effective_next_step": "submit_evidence_for_human_approval",
        "_output": {"proposed_evidence": [{"evidence_type": "PROOF_OF_FULFILLMENT", "notes": "n",
                                           "tracking_info": [{"carrier_name": "MEX_ESTAFETA",
                                                              "tracking_number": TRACKING}],
                                           "claims": [{"text": "Shipped via Estafeta.",
                                                      "source_ids": ["doc_1"], "basis": "verified"}]}]}})
    out = mcp_server.prepare_draft(DID)
    assert out["prepared"] is True and out["auto_prep"] == "ready"
    assert "review and approve" in out["message"]


def test_prepare_draft_needs_documents(fake):
    out = mcp_server.prepare_draft(DID)
    assert out["prepared"] is False and out["auto_prep"] == "needs_documents"


def test_get_send_status_not_sent(fake):
    out = mcp_server.get_send_status(DID)
    assert out["overall"] == "not_sent" and out["attempts"] == []


def test_get_send_status_never_says_received_without_paypal_confirmation(fake):
    pid = _package()
    from copilot.core.attempt_store import SQLiteAttemptStore
    from copilot.core.send import sha256_json
    db = config.db_path()
    tn, car = pick_tracking(extracted_docs_for(db, DID))
    req = build_request_input(repo.list_claims(db, pid), tn, car)
    store = SQLiteAttemptStore(db)
    store.claim(DID, sha256_json(req))
    # the registry itself claims "confirmed" (request_id set: this app really did POST it once), but
    # PayPal's GET (WAITING fixture) does not show it - get_send_status must re-verify via GET, never
    # trust the stored state alone.
    store.write(DID, sha256_json(req), {"state": "confirmed", "request_id": "req-1", "request_input": req,
                                        "attachment_filenames": []})
    store.close()
    out = mcp_server.get_send_status(DID)
    assert out["overall"] == "pending"
    assert "Received by PayPal" not in out["overall_message"]

    fake.detail = {**WAITING, "evidences": _confirmed_body()["evidences"]}   # now PayPal shows it
    assert mcp_server.get_send_status(DID)["overall"] == "received_by_paypal"


def test_get_send_status_pending_shows_check_again_and_the_dispute_url(fake):
    """A pending row's message must name the real control ("Check again") and
    point at the actual dispute URL, not a vague/placeholder instruction."""
    pid = _package()
    from copilot.core.attempt_store import SQLiteAttemptStore
    from copilot.core.send import sha256_json
    db = config.db_path()
    tn, car = pick_tracking(extracted_docs_for(db, DID))
    req = build_request_input(repo.list_claims(db, pid), tn, car)
    store = SQLiteAttemptStore(db)
    store.claim(DID, sha256_json(req))
    store.write(DID, sha256_json(req), {"state": "sent_unconfirmed", "request_id": "req-1",
                                        "request_input": req})
    store.close()
    out = mcp_server.get_send_status(DID)
    assert out["overall"] == "pending"
    assert "Check again" in out["overall_message"]
    assert mcp_server._dispute_url(DID) in out["overall_message"]


def test_get_send_status_never_says_received_from_a_row_with_no_request_id(fake):
    """The MCP-specific branch: content matching PayPal's GET is not enough on its own without
    request_id - this app never actually POSTed this row (e.g. it is still 'pending', about to attempt its
    own first POST)."""
    pid = _package()
    from copilot.core.attempt_store import SQLiteAttemptStore
    from copilot.core.send import sha256_json
    db = config.db_path()
    tn, car = pick_tracking(extracted_docs_for(db, DID))
    req = build_request_input(repo.list_claims(db, pid), tn, car)
    store = SQLiteAttemptStore(db)
    store.claim(DID, sha256_json(req))
    store.write(DID, sha256_json(req), {"state": "pending", "request_input": req})   # no request_id
    store.close()

    fake.detail = {**WAITING, "evidences": _confirmed_body()["evidences"]}
    out = mcp_server.get_send_status(DID)
    assert out["overall"] != "received_by_paypal"


def test_get_send_status_survives_a_transport_error(fake, monkeypatch):
    """get_send_status must catch httpx.TransportError too, not only PayPalError/RuntimeError."""
    import httpx

    def _raise(*a, **kw):
        raise httpx.ConnectError("fake: connection refused")

    monkeypatch.setattr(fake, "call", _raise)
    out = mcp_server.get_send_status(DID)
    assert "error" in out


@pytest.mark.parametrize("tool_name", ["list_disputes", "get_dispute_file", "what_is_missing",
                                       "prepare_draft", "get_send_status"])
def test_every_tool_that_reads_paypal_survives_a_transport_error(fake, monkeypatch, tool_name):
    """All 5 tools share the identical except clause - parametrized so a
    future tool added without httpx.TransportError in its except tuple gets caught by this test too."""
    import httpx

    def _raise(*a, **kw):
        raise httpx.ConnectError("fake: connection refused")

    monkeypatch.setattr(fake, "call", _raise)
    tool = getattr(mcp_server, tool_name)
    out = tool() if tool_name == "list_disputes" else tool(DID)
    assert "error" in out


def test_get_send_status_shows_blocked_never_as_rejected_by_paypal(fake, monkeypatch):
    """A 'blocked' row (local refusal, e.g. no provide-evidence link) must be reported as
    blocked/"Not sent: ...", never conflated with a real PayPal rejection."""
    pid = _package()
    from copilot.core.attempt_store import SQLiteAttemptStore
    from copilot.core.send import sha256_json
    db = config.db_path()
    tn, car = pick_tracking(extracted_docs_for(db, DID))
    req = build_request_input(repo.list_claims(db, pid), tn, car)
    store = SQLiteAttemptStore(db)
    store.claim(DID, sha256_json(req))
    store.write(DID, sha256_json(req), {"state": "blocked", "reason": "no link yet", "request_input": req})
    store.close()

    out = mcp_server.get_send_status(DID)
    assert out["overall"] == "blocked"
    assert "Not sent: no link yet" in out["overall_message"]
    assert "rejected by PayPal" not in out["overall_message"]

    store = SQLiteAttemptStore(db)
    rec = store.read(DID, sha256_json(req))
    store.close()
    assert rec["state"] == "blocked"   # the registry row itself, not only the text


def test_get_send_status_shows_abandoned_distinctly(fake, monkeypatch):
    """An 'abandoned' row (a human confirmed PayPal's Resolution Center did not show
    it) gets its own status, never conflated with 'received'/'failed'/'pending'."""
    pid = _package()
    from copilot.core.attempt_store import SQLiteAttemptStore
    from copilot.core.send import sha256_json
    db = config.db_path()
    tn, car = pick_tracking(extracted_docs_for(db, DID))
    req = build_request_input(repo.list_claims(db, pid), tn, car)
    store = SQLiteAttemptStore(db)
    store.claim(DID, sha256_json(req))
    store.write(DID, sha256_json(req), {"state": "abandoned", "reason": "vendor confirmed it",
                                        "request_input": req})
    store.close()

    out = mcp_server.get_send_status(DID)
    assert out["overall"] == "abandoned"
    assert "Resolution Center" in out["overall_message"]

    store = SQLiteAttemptStore(db)
    rec = store.read(DID, sha256_json(req))
    store.close()
    assert rec["state"] == "abandoned"


def test_every_call_is_audited_as_system_via_mcp(fake):
    mcp_server.what_is_missing(DID)
    mcp_server.get_send_status(DID)
    entries = _audit_entries()
    assert {e["action"] for e in entries} >= {"mcp_what_is_missing", "mcp_get_send_status"}
    assert all(e["actor"] == "system" and e["detail"]["via"] == "mcp" for e in entries)


# ----------------------------------------------------------------------------------- tool registration
def _tool_names(monkeypatch, allow_send):
    if allow_send:
        monkeypatch.setenv(mcp_server.ALLOW_SEND_ENV, "true")
    else:
        monkeypatch.delenv(mcp_server.ALLOW_SEND_ENV, raising=False)
    tools = asyncio.run(mcp_server.build_server().list_tools())
    return {t.name for t in tools}, tools


@pytest.mark.parametrize("allow", [False, True])
def test_no_approve_tool_exists(monkeypatch, allow):
    names, _ = _tool_names(monkeypatch, allow)
    assert not any("approv" in n and n != "send_approved_package" for n in names)
    assert not any(n.startswith("approve") for n in names)


def test_send_tool_absent_unless_env_set(monkeypatch):
    names, tools = _tool_names(monkeypatch, False)
    assert names == {"list_disputes", "get_dispute_file", "what_is_missing", "prepare_draft", "get_send_status"}
    assert "send_approved_package" not in names
    monkeypatch.setenv(mcp_server.ALLOW_SEND_ENV, "false")
    assert "send_approved_package" not in {t.name for t in asyncio.run(mcp_server.build_server().list_tools())}
    names, _ = _tool_names(monkeypatch, True)
    assert "send_approved_package" in names


def test_descriptions_say_server_cannot_approve(monkeypatch):
    _, tools = _tool_names(monkeypatch, True)
    for t in tools:
        text = " ".join((t.description or "").lower().split())   # docstrings wrap across lines
        assert "cannot approve" in text or "never approves" in text, t.name


# ----------------------------------------------------------------------------------------- validation
@pytest.mark.parametrize("bad", ["", "../etc/passwd", "https://evil.example/x", "PP-R-1/../../x", "pp-r-set-1",
                                 "PP-R-DEMO-0000001 ", "PP-R-SET-1\n", "x" * 200, None, 123,
                                 "PP-R-DEMO-0000001?a=b"])
def test_invalid_dispute_id_rejected_before_any_paypal_call(fake, bad):
    for tool in (mcp_server.get_dispute_file, mcp_server.what_is_missing, mcp_server.prepare_draft,
                 mcp_server.get_send_status, mcp_server.send_approved_package):
        with pytest.raises(mcp_server.InvalidDisputeId):
            tool(bad)
    assert fake.ops == []


def test_valid_dispute_id_formats_accepted():
    for ok in ("PP-R-DEMO-0000001", "PP-D-123456789", "PP-R-ABC-12345"):
        assert mcp_server.DISPUTE_ID_RE.fullmatch(ok)


# ------------------------------------------------------------------------------------------ send tool
def test_send_refused_when_not_approved(fake, monkeypatch):
    monkeypatch.setenv(mcp_server.ALLOW_SEND_ENV, "true")
    _package(approved=False)
    out = mcp_server.send_approved_package(DID)
    assert out["sent"] is False and "approved by a human" in out["message"]
    assert fake.ops == [] and fake.posts == []


def test_send_refused_when_no_package(fake, monkeypatch):
    monkeypatch.setenv(mcp_server.ALLOW_SEND_ENV, "true")
    assert mcp_server.send_approved_package(DID)["sent"] is False
    assert fake.posts == []


def test_send_refused_when_edited_after_approval(fake, monkeypatch):
    monkeypatch.setenv(mcp_server.ALLOW_SEND_ENV, "true")
    pid = _package(approved=True)
    repo.insert_claims(config.db_path(), pid, [{"evidence_index": 0, "claim_index": 1, "text": "Edited later.",
                                                "source_ids": [], "basis": "seller_statement"}])
    out = mcp_server.send_approved_package(DID)
    assert out["sent"] is False and "changed since it was approved" in out["message"]
    assert fake.ops == [] and fake.posts == []


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_send_approved_package_uses_send_service_and_confirms_only_via_paypal(fake, monkeypatch):
    monkeypatch.setenv(mcp_server.ALLOW_SEND_ENV, "true")
    _package(approved=True)
    fake.post_send_gets = [_confirmed_body() | {"links": WAITING["links"]}]
    out = mcp_server.send_approved_package(DID)
    assert out["sent"] is True and out["title"] == "Received by PayPal"
    assert len(fake.posts) == 1
    ops = [o for _, o in fake.ops]
    assert ops.index("pre_send_get") < ops.index("provide_evidence") < ops.index("post_send_get")
    assert any(e["action"] == "mcp_send_approved_package_result" for e in _audit_entries())


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_send_second_call_does_not_post_again(fake, monkeypatch):
    monkeypatch.setenv(mcp_server.ALLOW_SEND_ENV, "true")
    _package(approved=True)
    fake.post_send_gets = [{"evidences": []}] * 3          # PayPal never shows it: result pending
    first = mcp_server.send_approved_package(DID)
    assert first["sent"] is False and "Not confirmed" in first["message"]
    second = mcp_server.send_approved_package(DID)
    assert second["sent"] is False
    assert len(fake.posts) == 1                              # no automatic retry, ever


@pytest.mark.usefixtures('legacy_approval_scenario')
def test_historical_confirmation_message_does_not_deny_received_state(fake, monkeypatch):
    monkeypatch.setenv(mcp_server.ALLOW_SEND_ENV, 'true')
    _package(approved=True)
    fake.post_send_gets = [_confirmed_body() | {'links': WAITING['links']}]
    first = mcp_server.send_approved_package(DID)
    assert first['sent'] is True and len(fake.posts) == 1
    # The next GET omits the evidence; the durable confirmed attempt still wins.
    again = mcp_server.send_approved_package(DID)
    assert again['http_status'] == 409 and again['title'] == 'Package already received'
    assert again['sent'] is False and len(fake.posts) == 1
    assert 'previously confirmed' in again['message']
    assert 'Not confirmed as received' not in again['message']


# ------------------------------------------------------------------------------------------ real stdio
def test_real_server_over_stdio_lists_tools():
    from mcp import Client, StdioServerParameters

    def run(allow):
        env = {**os.environ, mcp_server.ALLOW_SEND_ENV: "true" if allow else "false"}
        params = StdioServerParameters(command=sys.executable, args=["-m", "copilot.mcp_server"],
                                       env=env, cwd=str(ROOT))

        async def go():
            async with Client(params) as client:
                return {t.name for t in (await client.list_tools()).tools}
        return asyncio.run(asyncio.wait_for(go(), timeout=60))

    base = {"list_disputes", "get_dispute_file", "what_is_missing", "prepare_draft", "get_send_status"}
    assert run(False) == base
    assert run(True) == base | {"send_approved_package"}


# the stored "nothing entered" notice is listed only while the package really has no content, as on the web page.
def test_open_notices_drop_the_stored_nothing_entered_flag_once_the_package_has_content(fake):
    from copilot.web.routes_package import EMPTY_MANUAL_FLAG
    db = config.db_path()
    pid = repo.insert_package(db, DID, source="human_edit", validation_ok=True, review_flags=[EMPTY_MANUAL_FLAG])
    assert mcp_server.get_dispute_file(DID)["draft"]["open_notices"] == [EMPTY_MANUAL_FLAG]      # no content: listed
    repo.insert_claims(db, pid, [{"evidence_index": 0, "claim_index": 0, "text": "Shipped by FedEx.",
                                  "source_ids": [], "basis": "seller_statement"}])
    assert mcp_server.get_dispute_file(DID)["draft"]["open_notices"] == []                       # one claim: absent
def test_dr1_mcp_examples_are_only_the_named_synthetic_dispute():
    import re
    from pathlib import Path
    source = (Path(__file__).resolve().parents[2] / "copilot/mcp_server.py").read_text(encoding="utf-8")
    examples = re.findall(r"PP-R-[A-Z]+-[0-9]+", source)
    synthetic_count = examples.count("PP-R-ABC-12345678")
    assert synthetic_count == 2
    assert all(example == "PP-R-ABC-12345678" for example in examples)



@pytest.mark.parametrize("allow_send", [False, True])
def test_tools_list_reports_rc4_annotations_without_approve(monkeypatch, allow_send):
    monkeypatch.setenv(mcp_server.ALLOW_SEND_ENV, "true" if allow_send else "false")
    tools = {tool.name: tool for tool in asyncio.run(mcp_server.build_server().list_tools())}
    read_only = {"list_disputes", "get_dispute_file", "what_is_missing", "get_send_status"}
    expected = read_only | {"prepare_draft"}
    assert set(tools) == expected | ({"send_approved_package"} if allow_send else set())
    assert not any("approve" in name and name != "send_approved_package" for name in tools)
    for name in read_only:
        annotations = tools[name].model_dump(mode="json", by_alias=True)["annotations"]
        assert annotations is not None
        assert annotations["readOnlyHint"] is True
        assert annotations["destructiveHint"] is False
    # prepare_draft saves a draft package in Proofie's local database: not read-only, not destructive, repeatable
    draft = tools["prepare_draft"].model_dump(mode="json", by_alias=True)["annotations"]
    assert draft["readOnlyHint"] is False and draft["destructiveHint"] is False and draft["idempotentHint"] is True
    if allow_send:
        assert tools["send_approved_package"].annotations is not None
        assert tools["send_approved_package"].model_dump(mode="json", by_alias=True)["annotations"]["readOnlyHint"] is False


def _partial_attempt(filenames):
    pid = _package()
    from copilot.core.attempt_store import SQLiteAttemptStore
    from copilot.core.send import sha256_json
    db = config.db_path()
    tn, car = pick_tracking(extracted_docs_for(db, DID))
    req = build_request_input(repo.list_claims(db, pid), tn, car)
    store = SQLiteAttemptStore(db)
    store.claim(DID, sha256_json(req))
    store.write(DID, sha256_json(req), {"state": "sent_unconfirmed", "request_id": "req-1", "request_input": req,
                                        "attachment_filenames": filenames,
                                        "reason": "missing_attachment:" + ",".join(filenames)})
    store.close()


def test_get_send_status_partial_wording_is_singular_for_one_missing_file(fake):
    fake.detail = {**WAITING, "evidences": _confirmed_body()["evidences"]}      # statements listed, no documents
    _partial_attempt(["a.pdf"])
    out = mcp_server.get_send_status(DID)
    assert out["overall"] == "partial"
    assert out["overall_message"] == ("PayPal shows the details but not the attached file. Check the attachment in "
                                      "the web UI.")


def test_get_send_status_partial_wording_is_plural_for_two_missing_files(fake):
    fake.detail = {**WAITING, "evidences": _confirmed_body()["evidences"]}
    _partial_attempt(["a.pdf", "b.pdf"])
    out = mcp_server.get_send_status(DID)
    assert out["overall"] == "partial"
    assert out["overall_message"] == ("PayPal shows the details but not the attached files. Check the attachments in "
                                      "the web UI.")
