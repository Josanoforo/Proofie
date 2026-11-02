"""The PayPal-Request-Id of a "same statements, other files" send.

The attempt registry key is the statements and the tracking, not the files, and the send's PayPal-Request-Id was derived
from that key alone. When state3's reclaim path sends a package with the same statements and OTHER files over an
abandoned row that another package's attempt registered, that id is the earlier attempt's id: PayPal may honour
idempotency and answer with the earlier result, without the new files. The reclaimed send must carry a new id,
deterministic for its own content (statements, tracking and the set of attachment sha256), and every other send keeps
the id it always had.

Offline only: fake PayPal clients, a temp database per test (conftest.py). Effects are read from the requests the fake
saw, stored rows and audit entries.
"""
import pytest

from copilot.core import send_service
from copilot.core.disputes import PayPalError
from copilot.core.send import sha256_json
from copilot.db import repo

from test_ux_rc41_fix3_state3 import age_attempts, attempts
from test_ux_rc41_fix_state import LABEL_A, OTHER, STATEMENT, add_file, confirmed_body, posts, request_input
from test_ux_rc41_fix_state_status import seed_attempt
from test_ux_rc41_package import DID, add_claims, approve_passive, draft, env, new_package  # noqa: F401

pytestmark = [pytest.mark.usefixtures("web_mode")]


def test_int3_a_normal_repeat_of_the_same_package_keeps_its_request_id(env):
    """One package, refused by PayPal (4xx) and sent again: both POSTs carry the id derived from its statements, as
    before this change (first sends and repeats of the same package are untouched)."""
    c, db, token, fake = env
    pkg = draft(c, db, token, STATEMENT)
    add_file(c, db, token, pkg, "a.pdf", LABEL_A)
    approve_passive(c, token, pkg)
    sha = sha256_json(request_input(db, pkg))
    fake.provide_evidence_error = PayPalError(422, "UNPROCESSABLE_ENTITY", "dbg-int3", {})
    c.post(f"/disputes/{DID}/packages/{pkg}/send", data={"csrf_token": token}, follow_redirects=False)
    assert len(posts(fake)) == 1 and [a["state"] for a in attempts(db)] == ["failed"]
    fake.provide_evidence_error = None
    fake.post_send_get_bodies = [confirmed_body(db, pkg, ["a.pdf"])]
    c.post(f"/disputes/{DID}/packages/{pkg}/send", data={"csrf_token": token}, follow_redirects=False)
    assert len(posts(fake)) == 2
    assert [p["request_id"] for p in posts(fake)] == [send_service.request_id_for(DID, sha)] * 2
    assert [a["state"] for a in attempts(db)] == ["confirmed"]
