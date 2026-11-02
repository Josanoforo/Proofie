"""The re-armed case is sent once; a second send on the same case is not offered and the send route refuses it without a PayPal POST.

The state replayed here is the one PayPal showed after the IPR walk's send (2026-11-01, copilot_web.jsonl:149 of the walk): the
dispute under review in the chargeback stage, the seller's entries OTHER (our re-arm note) and PROOF_OF_FULFILLMENT, the only
action link provide_supporting_info. Only the fields the app reads are kept; no PayPal body is copied. Offline: fake PayPal client."""
import json

import pytest
from _dom import parse
from _hosted import set_test_csrf

import test_routes_send as send_tests
from copilot.core import disputes
from test_evidence_once_per_dispute import RULE, no_way_to_send
from test_ux_rc41_fix_state import STATEMENT, posts
from test_ux_rc41_package import DID, approved, env, page  # noqa: F401  (env is the web fixture)

pytestmark = [pytest.mark.usefixtures("web_mode")]


def after_the_send(rels):
    link = lambda rel: {"rel": rel, "href": f"https://api-m.sandbox.paypal.com/v1/customer/disputes/{DID}/{rel.replace('_', '-')}", "method": "POST"}
    body = json.loads(json.dumps(send_tests.WAITING))
    body.update(status="UNDER_REVIEW", dispute_life_cycle_stage="CHARGEBACK", links=[link(rel) for rel in rels],
                evidences=[{"source": "SUBMITTED_BY_SELLER", "evidence_type": "OTHER", "notes": disputes.REARM_NOTE, "date": "2026-11-02T00:00:00Z"},
                           {"source": "SUBMITTED_BY_SELLER", "evidence_type": "PROOF_OF_FULFILLMENT", "notes": "DEMO DATA (sandbox test)",
                            "date": "2026-11-02T00:06:00Z"}])
    return body


NOT_ASKING = "PayPal is not asking for evidence on this case right now."


@pytest.mark.parametrize("rels,sentence", [(["self", "provide_supporting_info"], NOT_ASKING), (["self", "provide_evidence"], RULE)],
                         ids=["as-recorded", "paypal-asks-again"])
def test_after_its_send_the_re_armed_case_refuses_a_second_send_without_a_paypal_post(env, monkeypatch, tmp_path, rels, sentence):
    c, db, token, fake = env
    monkeypatch.setenv("COPILOT_FEATURE_REARM_NOTE_IGNORED", "true")
    monkeypatch.setenv("PROOFIE_EVIDENCE_DIR", str(tmp_path))
    (tmp_path / disputes.REARM_LOG).write_text(json.dumps({"op": disputes.REARM_OP, "method": "POST", "status": 200,
                                                           "path": f"/v1/customer/disputes/{DID}/escalate"}) + "\n", encoding="utf-8")
    pkg = approved(c, db, token, STATEMENT)
    fake.detail_body = fake.pre_send_get_body = fake.recovery_get_body = after_the_send(rels)
    set_test_csrf(c, token)
    refused = c.post(f"/disputes/{DID}/packages/{pkg}/send", data={"csrf_token": token}, follow_redirects=False)
    result = c.get(refused.headers["location"])
    main = " ".join(parse(result.text).find("main").text().split())
    assert result.status_code == 409, result.status_code
    assert sentence in main, main
    assert posts(fake) == []                                                    # not one PayPal POST
    no_way_to_send(page(c, pkg).text, fake)                                       # and the package page offers no send control
