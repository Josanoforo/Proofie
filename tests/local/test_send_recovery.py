"""Offline tests for copilot/core/send_recovery.py (startup/crash recovery). No real network,
no real sleeping - `sleep` is injected as a no-op recorder so these run instantly."""
import sys
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.core.send_recovery import recover_dispute, reconcile_after_send

DID = "PP-R-TEST-0001"
SHA = "abc123"
REQ_INPUT = {"evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT",
                           "evidence_info": {"tracking_info": [{"carrier_name": "MEX_ESTAFETA",
                                                                "tracking_number": "DEMO1"}]},
                           "notes": "demo"}]}
NOT_YET = {"evidences": []}
CONFIRMED = {"evidences": [{"source": "SUBMITTED_BY_SELLER", "evidence_type": "PROOF_OF_FULFILLMENT",
                           "evidence_info": {"tracking_info": [{"tracking_number": "DEMO1"}]},
                           "notes": "demo", "date": "2026-10-05"}]}


class ScriptedClient:
    """Returns each body in `bodies` in order, one per call(); records every (op) call made."""
    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.calls = []

    def call(self, method, path, op, **kw):
        self.calls.append(op)
        return self.bodies.pop(0)


class SleepRecorder:
    def __init__(self):
        self.delays = []

    def __call__(self, seconds):
        self.delays.append(seconds)


def _store(tmp_path):
    return SQLiteAttemptStore(tmp_path / "attempts.db")


@pytest.mark.parametrize('terminal', ['confirmed', 'abandoned'])
def test_partial_interleaving_preserves_terminal_state_and_return(tmp_path, terminal):
    db = tmp_path / 'interleave.db'
    first, second = SQLiteAttemptStore(db), SQLiteAttemptStore(db)
    calls = []
    class Client:
        def call(self, method, path, op, **kwargs):
            assert method == 'GET'
            calls.append(method)
            return CONFIRMED_WITH_FILE
    try:
        first.claim(DID, SHA)
        first.write(DID, SHA, {'state': 'sent_unconfirmed', 'request_id': 'synthetic-rid',
                              'request_input': REQ_INPUT, 'attachment_filenames': ['label.pdf']})
        class InterleavedStore:
            settled = False
            def settle(self):
                if not self.settled:
                    self.settled = True
                    if terminal == 'confirmed':
                        recover_dispute(Client(), second, DID, db)
                    else:
                        row = second.read(DID, SHA)
                        assert second.abandon(DID, SHA, row['ts'], 'synthetic acknowledgement')
            def read(self, *args):
                stale = first.read(*args)
                self.settle()
                return stale
            def write(self, *args):
                return first.write(*args)
            def write_partial(self, *args):
                self.settle()
                return first.write_partial(*args)
        result = reconcile_after_send(ScriptedClient([CONFIRMED_NO_FILE]), InterleavedStore(),
                                      DID, SHA, REQ_INPUT, attachment_filenames=['label.pdf'],
                                      delays=(0,), sleep=lambda _: None)
        final = second.read(DID, SHA)
        assert final['state'] == terminal
        assert final['request_id'] == 'synthetic-rid'
        assert result['confirmed'] == (terminal == 'confirmed')
        assert result.get('abandoned', False) == (terminal == 'abandoned')
        assert result['files_ok'] == (terminal == 'confirmed')
    finally:
        first.close()
        second.close()


# ---- reconcile_after_send (up to 3 GETs, ~5/15/30s, never re-POSTs) -----------------------------------
def test_confirms_on_the_first_check_and_stops_there(tmp_path):
    store = _store(tmp_path)
    store.claim(DID, SHA)
    client = ScriptedClient([CONFIRMED, NOT_YET, NOT_YET])   # would fail if it checked more than once
    sleep = SleepRecorder()
    result = reconcile_after_send(client, store, DID, SHA, REQ_INPUT, sleep=sleep)
    assert result["confirmed"] is True
    assert sleep.delays == [5]          # only the first delay was used
    assert store.read(DID, SHA)["state"] == "confirmed"


def test_confirms_on_the_last_check(tmp_path):
    store = _store(tmp_path)
    store.claim(DID, SHA)
    client = ScriptedClient([NOT_YET, NOT_YET, CONFIRMED])
    sleep = SleepRecorder()
    result = reconcile_after_send(client, store, DID, SHA, REQ_INPUT, sleep=sleep)
    assert result["confirmed"] is True
    assert sleep.delays == [5, 15, 30]
    assert store.read(DID, SHA)["state"] == "confirmed"


def test_never_confirmed_leaves_state_ambiguous_not_failed(tmp_path):
    store = _store(tmp_path)
    store.claim(DID, SHA)
    store.write(DID, SHA, {"state": "sent_unconfirmed", "request_input": REQ_INPUT})
    client = ScriptedClient([NOT_YET, NOT_YET, NOT_YET])
    sleep = SleepRecorder()
    result = reconcile_after_send(client, store, DID, SHA, REQ_INPUT, sleep=sleep)
    assert result["confirmed"] is False
    assert sleep.delays == [5, 15, 30]
    # NEVER auto-resent, and the state must stay exactly what it was (ambiguous), not flipped to "failed" -
    # a false "failed" would wrongly invite a human to just try again as if nothing had been sent.
    assert store.read(DID, SHA)["state"] == "sent_unconfirmed"


def test_custom_delays_are_honoured(tmp_path):
    store = _store(tmp_path)
    store.claim(DID, SHA)
    client = ScriptedClient([NOT_YET, CONFIRMED])
    sleep = SleepRecorder()
    reconcile_after_send(client, store, DID, SHA, REQ_INPUT, delays=(1, 2, 3), sleep=sleep)
    assert sleep.delays == [1, 2]


# ---- recover_dispute (startup/crash recovery: one check per sent_unconfirmed attempt, no sleeping) --------
def test_recovers_a_sent_unconfirmed_attempt_that_actually_went_through(tmp_path):
    store = _store(tmp_path)
    store.claim(DID, SHA)
    # Only confirms an attempt THIS app actually POSTed (request_id set) - a genuine prior send.
    store.write(DID, SHA, {"state": "sent_unconfirmed", "request_id": "req-1", "request_input": REQ_INPUT})
    client = ScriptedClient([CONFIRMED])
    still_pending = recover_dispute(client, store, DID, tmp_path / "attempts.db")
    assert still_pending == []
    assert store.read(DID, SHA)["state"] == "confirmed"
    assert client.calls == ["recovery_get"]   # exactly one GET, no sleeping/polling here


def test_a_matching_response_never_confirms_an_attempt_this_process_never_posted(tmp_path):
    """Content matching PayPal's GET is not enough on its own - without a request_id, this row
    never actually went through a POST (e.g. a crashed 'pending' claim bumped straight to
    'sent_unconfirmed' by some other path), so it must stay reported as still pending, never confirmed."""
    store = _store(tmp_path)
    store.claim(DID, SHA)
    store.write(DID, SHA, {"state": "sent_unconfirmed", "request_input": REQ_INPUT})   # no request_id
    client = ScriptedClient([CONFIRMED])
    still_pending = recover_dispute(client, store, DID, tmp_path / "attempts.db")
    assert len(still_pending) == 1 and still_pending[0]["package_sha256"] == SHA
    assert store.read(DID, SHA)["state"] == "sent_unconfirmed"


def test_a_get_failure_during_recovery_leaves_the_attempt_still_pending_without_crashing(tmp_path):
    """PayPal being unreachable during recover_dispute's own GET must never raise - this runs both
    right before a send and on ordinary dispute-page views (copilot/web/routes_disputes.py)."""
    import httpx
    from copilot.core.disputes import PayPalError

    class RaisingClient:
        def __init__(self, exc):
            self.exc = exc

        def call(self, method, path, op, **kw):
            raise self.exc

    store = _store(tmp_path)
    store.claim(DID, SHA)
    store.write(DID, SHA, {"state": "sent_unconfirmed", "request_id": "req-1", "request_input": REQ_INPUT})

    for exc in (PayPalError(503, "SERVICE_UNAVAILABLE", "dbg", {}), httpx.ReadTimeout("fake: timed out")):
        still_pending = recover_dispute(RaisingClient(exc), store, DID, tmp_path / "attempts.db")
        assert len(still_pending) == 1 and still_pending[0]["package_sha256"] == SHA
        assert store.read(DID, SHA)["state"] == "sent_unconfirmed"   # untouched, never crashed


def test_a_still_genuinely_unconfirmed_attempt_is_reported_but_not_mutated_further(tmp_path):
    store = _store(tmp_path)
    store.claim(DID, SHA)
    store.write(DID, SHA, {"state": "sent_unconfirmed", "request_input": REQ_INPUT})
    client = ScriptedClient([NOT_YET])
    still_pending = recover_dispute(client, store, DID, tmp_path / "attempts.db")
    assert len(still_pending) == 1 and still_pending[0]["package_sha256"] == SHA
    assert store.read(DID, SHA)["state"] == "sent_unconfirmed"


def test_pending_confirmed_and_failed_attempts_are_never_touched(tmp_path):
    """Only 'sent_unconfirmed' is ambiguous enough to need reconciliation; the other three states already
    know their own outcome and must not trigger a GET at all."""
    store = _store(tmp_path)
    store.claim(DID, "sha-pending")
    store.claim(DID, "sha-confirmed")
    store.write(DID, "sha-confirmed", {"state": "confirmed", "request_input": REQ_INPUT})
    store.claim(DID, "sha-failed")
    store.write(DID, "sha-failed", {"state": "failed", "reason": "preconditions"})
    client = ScriptedClient([])   # would raise IndexError if any GET were attempted
    still_pending = recover_dispute(client, store, DID, tmp_path / "attempts.db")
    assert still_pending == []
    assert client.calls == []


def test_an_attempt_with_no_stored_request_input_is_reported_but_never_crashes(tmp_path):
    store = _store(tmp_path)
    store.claim(DID, SHA)   # claimed as 'pending', bumped to sent_unconfirmed WITHOUT ever recording content
    store.write(DID, SHA, {"state": "sent_unconfirmed"})
    client = ScriptedClient([])   # must never be called - nothing to compare against
    still_pending = recover_dispute(client, store, DID, tmp_path / "attempts.db")
    assert len(still_pending) == 1
    assert client.calls == []


def test_recover_dispute_only_looks_at_the_given_dispute(tmp_path):
    store = _store(tmp_path)
    store.claim("PP-R-OTHER", SHA)
    store.write("PP-R-OTHER", SHA, {"state": "sent_unconfirmed", "request_input": REQ_INPUT})
    client = ScriptedClient([])
    still_pending = recover_dispute(client, store, DID, tmp_path / "attempts.db")   # a different dispute entirely
    assert still_pending == []
    assert client.calls == []


# ---- attachments: a landed-content-but-missing-file outcome must never look like a full success -----------
CONFIRMED_WITH_FILE = {"evidences": [{"source": "SUBMITTED_BY_SELLER", "evidence_type": "PROOF_OF_FULFILLMENT",
                                      "evidence_info": {"tracking_info": [{"tracking_number": "DEMO1"}]},
                                      "notes": "demo", "date": "2026-10-05",
                                      "documents": [{"name": "label.pdf", "url": "https://example.com/x"}]}]}
CONFIRMED_NO_FILE = {"evidences": [{"source": "SUBMITTED_BY_SELLER", "evidence_type": "PROOF_OF_FULFILLMENT",
                                    "evidence_info": {"tracking_info": [{"tracking_number": "DEMO1"}]},
                                    "notes": "demo", "date": "2026-10-05"}]}   # no documents[] at all


def test_reconcile_after_send_confirms_when_the_attached_file_is_also_present(tmp_path):
    store = _store(tmp_path)
    store.claim(DID, SHA)
    client = ScriptedClient([CONFIRMED_WITH_FILE])
    result = reconcile_after_send(client, store, DID, SHA, REQ_INPUT, attachment_filenames=["label.pdf"],
                                  sleep=SleepRecorder())
    assert result == {"confirmed": True, "files_ok": True, "missing_files": [],
                      "details": result["details"]}
    assert store.read(DID, SHA)["state"] == "confirmed"


def test_reconcile_after_send_stops_early_when_content_lands_but_the_file_does_not():
    """Content confirmed on the FIRST check but the file is missing - must stop polling immediately
    (waiting longer cannot add a file that was never attached) and must never re-POST."""
    store_calls = []

    class _Store:
        def write_partial(self, dispute_id, package_sha, state):
            store_calls.append(state)
            return state

        def read(self, *a):
            return None

    client = ScriptedClient([CONFIRMED_NO_FILE, "WOULD BE A SECOND GET IF POLLING CONTINUED"])
    sleep = SleepRecorder()
    result = reconcile_after_send(client, _Store(), DID, SHA, REQ_INPUT, attachment_filenames=["label.pdf"],
                                  sleep=sleep)
    assert result["confirmed"] is False and result["files_ok"] is False
    assert result["missing_files"] == ["label.pdf"]
    assert sleep.delays == [5]   # stopped after the first check, not all three
    assert store_calls[-1]["state"] == "sent_unconfirmed"
    assert store_calls[-1]["reason"] == "missing_attachment:label.pdf"


def test_reconcile_after_send_never_downgrades_a_row_confirmed_concurrently(tmp_path):
    """If some OTHER concurrent caller's own GET already confirmed this exact row
    (every file included) between claim() and this check, the files_ok=False outcome here (this check's
    OWN, slightly different GET) must never downgrade it back to the ambiguous 'sent_unconfirmed'."""
    store = _store(tmp_path)
    store.claim(DID, SHA)
    store.write(DID, SHA, {"state": "confirmed", "request_input": REQ_INPUT, "attachment_filenames": []})
    client = ScriptedClient([CONFIRMED_NO_FILE])   # this check's own GET: content lands, file does not
    result = reconcile_after_send(client, store, DID, SHA, REQ_INPUT, attachment_filenames=["label.pdf"],
                                  sleep=SleepRecorder())
    assert result["confirmed"] is True and result["files_ok"] is True
    assert store.read(DID, SHA)["state"] == "confirmed"   # never downgraded


def test_reconcile_after_send_never_downgrades_an_abandoned_row(tmp_path):
    """A DIFFERENT package's confirm-not-received call can target THIS exact
    package_sha as the "other" one it abandons, while this send's own reconciliation is still polling
    it - the files_ok=False outcome here must never downgrade 'abandoned' back to 'sent_unconfirmed'."""
    store = _store(tmp_path)
    store.claim(DID, SHA)
    store.write(DID, SHA, {"state": "abandoned", "request_input": REQ_INPUT})
    client = ScriptedClient([CONFIRMED_NO_FILE])
    result = reconcile_after_send(client, store, DID, SHA, REQ_INPUT, attachment_filenames=["label.pdf"],
                                  sleep=SleepRecorder())
    assert result["confirmed"] is False and result.get("abandoned") is True
    assert store.read(DID, SHA)["state"] == "abandoned"   # never downgraded


def test_reconcile_after_send_without_attachments_is_unaffected_by_documents(tmp_path):
    """A package with no attachments must still confirm on tracking alone, regardless of documents[]."""
    store = _store(tmp_path)
    store.claim(DID, SHA)
    client = ScriptedClient([CONFIRMED_NO_FILE])
    result = reconcile_after_send(client, store, DID, SHA, REQ_INPUT, sleep=SleepRecorder())
    assert result["confirmed"] is True and result["files_ok"] is True


def test_recover_dispute_leaves_a_missing_attachment_attempt_exactly_as_found(tmp_path):
    store = _store(tmp_path)
    store.claim(DID, SHA)
    store.write(DID, SHA, {"state": "sent_unconfirmed", "request_input": REQ_INPUT,
                           "attachment_filenames": ["label.pdf"], "reason": "missing_attachment:label.pdf"})
    client = ScriptedClient([CONFIRMED_NO_FILE])   # still no file on the next check either
    still_pending = recover_dispute(client, store, DID, tmp_path / "attempts.db")
    assert len(still_pending) == 1
    assert store.read(DID, SHA)["state"] == "sent_unconfirmed"


def test_recover_dispute_confirms_once_the_attached_file_shows_up(tmp_path):
    store = _store(tmp_path)
    store.claim(DID, SHA)
    # Only confirms an attempt THIS app actually POSTed (request_id set) - a genuine prior send.
    store.write(DID, SHA, {"state": "sent_unconfirmed", "request_id": "req-1", "request_input": REQ_INPUT,
                           "attachment_filenames": ["label.pdf"]})
    client = ScriptedClient([CONFIRMED_WITH_FILE])
    still_pending = recover_dispute(client, store, DID, tmp_path / "attempts.db")
    assert still_pending == []
    assert store.read(DID, SHA)["state"] == "confirmed"
