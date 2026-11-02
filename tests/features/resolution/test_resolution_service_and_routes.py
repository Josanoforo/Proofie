"""/disputes/<id>/resolution on the fake PayPal and a temp database: flag off (default), flag on, the writes flag."""
from _hosted import authenticated_client, reset_csrf
import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from copilot import config
from copilot.app import create_app
from copilot.db import repo
from copilot.features.resolution import actions as act
from copilot.features.resolution import routes, service, store
from copilot.web.routes_disputes import get_client
from resolution_fakes import BASE, DISPUTE_ID, FakePayPal, paypal_error

FLAG, WRITES = "COPILOT_FEATURE_RESOLUTION", "COPILOT_FEATURE_RESOLUTION_WRITES"
ROOT = Path(__file__).resolve().parents[3]
TOKEN = "test-csrf-token"
PAGE = f"/disputes/{DISPUTE_ID}/resolution"
OFFER_FORM = {"csrf_token": TOKEN, "action": "offer", "amount": "10.00", "note": "Partial refund while we check"}
MESSAGE_FORM = {"csrf_token": TOKEN, "action": "message"}


def test_ui_copy_a3_writes_off_is_information_not_instructions(monkeypatch):
    web, fake = _web(monkeypatch, writes=None)
    page = web.get(PAGE)
    assert page.status_code == 200
    assert "It does not send offers or messages" in page.text
    assert "Offer first" not in page.text
    assert not re.search(r'<form[^>]*action="[^\"]*/(?:preview|register)"', page.text)
    assert not fake.posts()


def _web(monkeypatch, fake=None, flag="true", writes="true"):
    for name, value in ((FLAG, flag), (WRITES, writes)):
        monkeypatch.delenv(name, raising=False) if value is None else monkeypatch.setenv(name, value)
    monkeypatch.delenv("COPILOT_FEATURE_ACCOUNT_CLIFF", raising=False)
    fake = fake or FakePayPal()
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    web = authenticated_client(app, client=("127.0.0.1", 12345))
    set_test_csrf(web, TOKEN)
    return web, fake


def _preview(web, form):
    if form.get("action") == "message" and "text" not in form:
        # A submitted browser form carries its prefilled, seller-reviewed text.
        import html as html_module
        page = web.get(PAGE).text
        reset_csrf(web)
        set_test_csrf(web,TOKEN)
        form = {**form,"text":html_module.unescape(re.search(r'<textarea[^>]*name="text"[^>]*>(.*?)</textarea>',page,re.S).group(1))}
    html = web.post(PAGE + "/preview", data=form).text
    reset_csrf(web)
    set_test_csrf(web, TOKEN)
    return html


def _confirm_form(html):
    """The confirm form carries the token, the action and the approval fingerprint, never the body."""
    sha = re.search(r'data-state="request-sha256">([0-9a-f]{64})<', html).group(1)
    token = re.search(r'name="token" value="([^"]*)"', html).group(1)
    return {"csrf_token": TOKEN, "action": re.search(r'name="action" value="(\w+)"', html.split("confirm-form")[1]).group(1),
            "token": token, "approval_sha256": sha}


# ---- flag off (the default) ------------------------------------------------------------------------------------
@pytest.mark.local_spec
def test_flag_off_the_route_is_404_and_nothing_is_called(monkeypatch):
    web, fake = _web(monkeypatch, flag=None, writes=None)
    assert web.get(PAGE).status_code == 404
    assert web.post(PAGE + "/preview", data=OFFER_FORM).status_code == 404
    assert web.post(PAGE + "/register", data=OFFER_FORM).status_code == 404
    assert not [p for p in web.get("/openapi.json").json()["paths"] if "resolution" in p and not p.endswith("/resolution/draft")]   # that route is the negotiation_drafts flag's
    assert fake.calls == []


@pytest.mark.local_spec
@pytest.mark.parametrize("value", ["1", "yes", "false", ""])
def test_flag_off_for_any_value_other_than_true(monkeypatch, value):
    web, _fake = _web(monkeypatch, flag=value)
    assert web.get(PAGE).status_code == 404


@pytest.mark.local_spec
def test_the_writes_flag_alone_does_not_turn_the_feature_on(monkeypatch):
    web, _fake = _web(monkeypatch, flag=None, writes="true")
    assert web.get(PAGE).status_code == 404


def test_create_app_with_the_flag_off_loads_no_resolution_module():
    code = ("import sys\nfrom copilot.app import create_app\ncreate_app()\n"
            "print(sorted(m for m in sys.modules if m.startswith('copilot.features.resolution')))")
    env = {k: v for k, v in os.environ.items() if not k.startswith("COPILOT_FEATURE_")}
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


# ---- flag on ---------------------------------------------------------------------------------------------------
@pytest.mark.local_spec
def test_page_shows_the_three_actions_in_order_with_enabled_states_and_the_economics_line(monkeypatch):
    web, fake = _web(monkeypatch)
    r = web.get(PAGE)
    assert r.status_code == 200
    assert 'data-state="action-offer" data-enabled="yes"' in r.text
    assert 'data-state="action-message" data-enabled="yes"' in r.text
    assert 'data-state="action-defend" data-enabled="yes"' in r.text
    assert r.text.index("action-offer") < r.text.index("action-message") < r.text.index("action-defend")
    assert "Proofie has seen PayPal remove the refund option after a seller message. If you want to offer a refund, do it first." in r.text
    assert 'class="resolution-economics"' in r.text and "What each choice costs" in r.text
    assert "Refund the buyer in full: 40.00 USD, with no dispute fee at this stage" in r.text
    assert "Defend with evidence: fee 15.00 USD if PayPal decides for you; 55.00 USD if PayPal decides for the buyer" in r.text
    import html as htmllib
    assert "Fee if the buyer closes the case: not confirmed by PayPal's pages" in htmllib.unescape(r.text)
    assert "Where these numbers come from" in r.text
    assert "Concede" not in re.sub(r"<details.*?</details>", "", r.text, flags=re.S)
    assert f'href="/disputes/{DISPUTE_ID}"' in r.text                      # defend: a link to the existing page
    assert all(m == "GET" for m, _p, _o in fake.calls)


@pytest.mark.local_spec
def test_a_bad_dispute_id_is_400_before_any_call(monkeypatch):
    web, fake = _web(monkeypatch)
    assert web.get("/disputes/not-an-id/resolution").status_code == 400
    assert web.post("/disputes/not-an-id/resolution/preview", data=OFFER_FORM).status_code == 400
    assert fake.calls == []


@pytest.mark.local_spec
def test_preview_shows_the_body_and_the_hash_and_posts_nothing(monkeypatch):
    web, fake = _web(monkeypatch)
    html = _preview(web, OFFER_FORM)
    form = _confirm_form(html)
    body = store.get_check(form["token"], DISPUTE_ID)["body"]
    assert body["offer_amount"] == {"currency_code": "USD", "value": "10.00"}
    assert form["approval_sha256"] == act.request_sha256(body)
    assert not fake.posts()


@pytest.mark.local_spec
@pytest.mark.parametrize("amount", ["", "abc", "10.999", "-5", "1e3"])
def test_preview_refuses_an_unreadable_amount(monkeypatch, amount):
    web, fake = _web(monkeypatch)
    r = web.post(PAGE + "/preview", data={**OFFER_FORM, "amount": amount})
    assert r.status_code == 400 and 'data-state="form-error"' in r.text and not fake.posts()


def test_amounts_are_parsed_by_integer_arithmetic():
    assert [routes.parse_amount_cents(t) for t in ("10", "10.5", "10.05", "0.01", " 7 ")] == [1000, 1050, 1005, 1, 700]
    assert routes.parse_amount_cents("10.001") is None


def test_dr1b_comma_decimal_and_errors_keep_amount_and_note(monkeypatch, web_mode):
    assert routes.parse_amount_cents("10,05") == 1005
    assert routes.parse_amount_cents("1,234.56") is None
    web, fake = _web(monkeypatch)
    response = web.post(PAGE + "/preview", data={**OFFER_FORM,"amount":"bad decimal","note":"Keep synthetic note"})
    assert response.status_code == 400
    assert "There is a problem" in response.text and 'value="bad decimal"' in response.text
    assert ">Keep synthetic note</textarea>" in response.text and 'href="#field-amount"' in response.text
    assert "For example 10.00" in response.text
    assert not fake.posts()


@pytest.mark.local_spec
def test_register_with_a_wrong_hash_is_409_and_nothing_is_posted(monkeypatch):
    web, fake = _web(monkeypatch)
    form = _confirm_form(_preview(web, OFFER_FORM))
    r = web.post(PAGE + "/register", data={**form, "approval_sha256": "0" * 64})
    assert r.status_code == 409 and "Nothing was sent" in r.text and not fake.posts()
    assert store.list_actions(DISPUTE_ID) == []


@pytest.mark.local_spec
def test_register_after_the_link_disappeared_is_409_and_nothing_is_posted(monkeypatch):
    web, fake = _web(monkeypatch)
    form = _confirm_form(_preview(web, OFFER_FORM))
    fake.phase, fake.status = "after_offer", "WAITING_FOR_BUYER_RESPONSE"
    r = web.post(PAGE + "/register", data=form)
    assert r.status_code == 409 and not fake.posts()


@pytest.mark.local_spec
def test_a_happy_offer_one_post_then_one_poll_and_one_readback_on_the_refresh_confirmed_row_and_audit(monkeypatch):
    web, fake = _web(monkeypatch, FakePayPal(settling_reads=0))
    form = _confirm_form(_preview(web, OFFER_FORM))
    r = web.post(PAGE + "/register", data=form)                    # 303 to the page, which settles the pending row once
    assert r.status_code == 200 and 'data-status="sent_confirmed"' in r.text
    assert [c[1] for c in fake.posts()] == [f"{BASE}/make-offer"]
    ops = [c[2] for c in fake.calls]
    assert ops.count("resolution_poll") == 1 and ops.count("resolution_readback") == 1
    rows = store.list_actions(DISPUTE_ID)
    assert len(rows) == 1 and rows[0]["status"] == "sent_confirmed"
    assert len([a for a in repo.list_audit(config.db_path(), DISPUTE_ID) if a["action"] == "resolution_offer"]) == 1
    hub = web.get(PAGE).text
    assert 'data-state="history"' in hub and 'data-action="offer"' in hub
    assert 'data-state="action-offer" data-enabled="no"' in hub        # the offer window is closed now
    assert 'http-equiv="refresh"' not in hub and 'data-state="pending-settle"' not in hub


@pytest.mark.local_spec
def test_register_answers_at_once_with_a_redirect_and_the_page_waits_with_a_refresh_meta(monkeypatch):
    web, fake = _web(monkeypatch, FakePayPal(settling_reads=99))
    form = _confirm_form(_preview(web, OFFER_FORM))
    r = web.post(PAGE + "/register", data=form, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"{PAGE}/sent/{store.list_actions(DISPUTE_ID)[0]['id']}#sent"
    assert [c[2] for c in fake.calls].count("resolution_poll") == 0          # nothing waited inside the request
    page = web.get(PAGE)
    assert "http-equiv" not in page.text                                      # No meta refresh; the seller checks
    assert "Check PayPal again" in page.text and "Reads the dispute from PayPal. Sends nothing." in page.text
    assert 'data-state="pending-settle"' in page.text and "PayPal answered OK; not listed yet. Proofie is waiting for PayPal to update the dispute." in page.text
    assert 'data-status="sent_pending_settle"' in page.text
    assert len(fake.posts()) == 1


@pytest.mark.local_spec
def test_each_refresh_of_a_pending_page_is_one_poll_and_never_a_post(monkeypatch):
    web, fake = _web(monkeypatch, FakePayPal(settling_reads=99))
    form = _confirm_form(_preview(web, OFFER_FORM))
    web.post(PAGE + "/register", data=form)
    before = [c[2] for c in fake.calls].count("resolution_poll")
    for _ in range(3):
        web.get(PAGE)
    assert [c[2] for c in fake.calls].count("resolution_poll") - before == 3 and len(fake.posts()) == 1


@pytest.mark.local_spec
def test_a_422_is_stored_as_refused_and_not_retried(monkeypatch):
    web, fake = _web(monkeypatch, FakePayPal(post_raises=paypal_error(422)))
    form = _confirm_form(_preview(web, MESSAGE_FORM))
    r = web.post(PAGE + "/register", data=form)
    assert r.status_code == 409 and "Nothing was sent. PayPal was still updating this dispute. Wait a minute, then review and send again; your text is kept." in r.text
    assert "dbg-1" not in r.text and 'data-state="confirm-form"' in r.text         # the check page again, the text kept
    assert 'data-status="refused_422"' in web.get(PAGE).text
    assert len(fake.posts()) == 1 and store.list_actions(DISPUTE_ID)[0]["status"] == "refused_422"


@pytest.mark.local_spec
def test_a_message_first_removes_the_offer_from_the_next_page_render(monkeypatch):
    web, fake = _web(monkeypatch, FakePayPal(settling_reads=0))
    form = _confirm_form(_preview(web, MESSAGE_FORM))
    assert "DEMO-FDX-000001" in store.get_check(form["token"], DISPUTE_ID)["body"]["message"]
    assert web.post(PAGE + "/register", data=form).status_code == 200
    page = web.get(PAGE).text
    assert 'data-state="action-offer" data-enabled="no"' in page
    reset_csrf(web)                                      # the GET rotated the csrf cookie; put the fixed one back
    set_test_csrf(web, TOKEN)
    again = _preview(web, OFFER_FORM)
    assert 'data-state="preview-refused"' in again and not [c for c in fake.posts() if c[1].endswith("/make-offer")]


@pytest.mark.local_spec
def test_missing_or_wrong_csrf_gives_the_apps_usual_refusal_and_no_call(monkeypatch):
    web, fake = _web(monkeypatch)
    form = _confirm_form(_preview(web, OFFER_FORM))
    before = len(fake.calls)
    for csrf in ("", "forged"):
        assert web.post(PAGE + "/register", data={**form, "csrf_token": csrf}).status_code == 403
        assert web.post(PAGE + "/preview", data={**OFFER_FORM, "csrf_token": csrf}).status_code == 403
    assert len(fake.calls) == before and not fake.posts()


@pytest.mark.local_spec
def test_unknown_action_and_unreadable_body_are_400(monkeypatch):
    web, fake = _web(monkeypatch)
    assert web.post(PAGE + "/preview", data={"csrf_token": TOKEN, "action": "escalate"}).status_code == 400
    assert web.post(PAGE + "/register", data={"csrf_token": TOKEN, "action": "escalate", "body_json": "{}",
                                              "approval_sha256": "x"}).status_code == 400
    expired = web.post(PAGE + "/register", data={"csrf_token": TOKEN, "action": "offer", "token": "nope", "approval_sha256": "x"})
    assert expired.status_code == 410 and "This check has expired. Nothing was sent." in expired.text
    assert not fake.posts()


# ---- the second flag: the fallback of the kill criterion -------------------------------------------------------
@pytest.mark.local_spec
def test_writes_flag_off_the_page_is_read_only_and_both_post_routes_refuse(monkeypatch):
    web, fake = _web(monkeypatch, writes=None)
    r = web.get(PAGE)
    assert r.status_code == 200 and 'data-state="writes-off"' in r.text
    assert 'data-state="action-offer" data-enabled="no"' in r.text and 'data-state="action-message" data-enabled="no"' in r.text
    assert 'class="resolution-economics"' in r.text
    assert web.post(PAGE + "/preview", data=OFFER_FORM).status_code == 403
    assert web.post(PAGE + "/register", data={**MESSAGE_FORM, "body_json": "{}", "approval_sha256": "x"}).status_code == 403
    assert not fake.posts()


# ---- guarantees about the code itself --------------------------------------------------------------------------
def test_the_feature_has_only_the_expected_routes_and_no_escalation_or_evidence_call():
    feature = ROOT / "copilot" / "features" / "resolution"
    source = "\n".join(p.read_text(encoding="utf-8") for p in sorted(feature.glob("*.py")))
    assert "/escalate" not in source and "escalate(" not in source and "/provide-evidence" not in source
    paths = sorted(f"{m} {r.path}" for r in routes.router.routes for m in r.methods if m != "HEAD")
    assert paths == ["GET /disputes/{dispute_id}/resolution", "GET /disputes/{dispute_id}/resolution/check/{token}",
                     "GET /disputes/{dispute_id}/resolution/sent/{row_id}", "POST /disputes/{dispute_id}/resolution/check",
                     "POST /disputes/{dispute_id}/resolution/preview", "POST /disputes/{dispute_id}/resolution/register"]


def test_no_module_of_the_feature_imports_ai_or_uses_floats_for_money():
    feature = ROOT / "copilot" / "features" / "resolution"
    for path in sorted(feature.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            assert not (isinstance(node, ast.Name) and node.id == "float"), path.name
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
                assert not any("evidence_model" in n or "ollama" in n.lower() or "vision" in n for n in names), path.name


# Explicit mode contract; local specifications keep their original business assertions.
pytestmark = [pytest.mark.usefixtures("web_mode")]


def test_cd_write_configuration_keeps_access_csrf_and_approval_hash(web_mode, monkeypatch):
    from copilot import config
    fake = FakePayPal()
    web, observed = _web(monkeypatch, fake)
    assert web.app is not None and observed is fake and fake.calls == []
    target = PAGE + "/register"
    if config.hosted_demo():
        naked = TestClient(web.app, base_url="https://localhost")
        assert naked.post(target, data={}).status_code == 401
        assert fake.calls == []
    assert web.post(target, data={}).status_code == 403
    assert fake.calls == []
    set_test_csrf(web, TOKEN)
    rejected = web.post(target, data={"csrf_token": TOKEN, "action": "message", "token": "no-such-token",
                                     "approval_sha256": "0" * 64})
    assert rejected.status_code == 410 and "This check has expired. Nothing was sent." in rejected.text
    assert fake.calls == []
    set_test_csrf(web, TOKEN)
    preview = web.post(PAGE + "/preview", data={"csrf_token": TOKEN, "action": "message",
                                              "text": "Synthetic seller message"})
    assert preview.status_code == 200 and 'data-state="confirm-form"' in preview.text
    assert not fake.posts()


def test_safe_read_contract_with_write_subflag_off_in_both_modes(web_mode, monkeypatch):
    web, fake = _web(monkeypatch, writes=None)
    r = web.get(PAGE)
    assert r.status_code == 200
    assert not [c for c in fake.calls if c[0] == "POST"]

from _hosted import set_test_csrf



def _feature_dependencies(directory, namespace):
    """Resolve source imports without importing or executing feature modules."""
    import ast
    from importlib.util import resolve_name
    dependencies = set()
    for source in sorted(directory.rglob("*.py")):
        relative = source.relative_to(directory)
        package = ".".join(("copilot", "features", namespace, *relative.parts[:-1]))
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8-sig"), filename=str(source))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = resolve_name("." * node.level + (node.module or ""), package) if node.level else (node.module or "")
                names = [base, *(base + "." + alias.name for alias in node.names)]
            else:
                continue
            for name in names:
                parts = name.split(".")
                if len(parts) >= 3 and parts[:2] == ["copilot", "features"] and parts[2] != namespace:
                    dependencies.add(parts[2])
    return dependencies

_FROZEN_FEATURE_DEPENDENCIES = {'account_cliff', 'draft', 'fulfilment', 'negotiation_drafts', 'local_autopilot'}      # Read-only lookups (draft origin, local tracking file); the last two are lazy imports behind their own flags (UX v3, declared)


def _assert_feature_dependencies(directory):
    assert _feature_dependencies(directory, 'resolution') == _FROZEN_FEATURE_DEPENDENCIES


def test_exact_feature_dependencies_by_source_ast():
    from pathlib import Path
    directory = Path(__file__).resolve().parents[3] / "copilot" / "features" / 'resolution'
    _assert_feature_dependencies(directory)


def test_dependency_graph_rejects_planted_absolute_and_relative_imports(tmp_path):
    from pathlib import Path
    import pytest
    source = Path(__file__).resolve().parents[3] / "copilot" / "features" / 'resolution'
    for form, statement in (("absolute", "import copilot.features.triage.service"),
                            ("relative", "from ..triage import service")):
        copy = tmp_path / form
        for path in source.rglob("*.py"):
            target = copy / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(path.read_text(encoding="utf-8-sig"), encoding="utf-8")
        (copy / "planted.py").write_text(statement + "\n", encoding="utf-8")
        assert _feature_dependencies(copy, 'resolution') == _FROZEN_FEATURE_DEPENDENCIES | {"triage"}
        with pytest.raises(AssertionError):
            _assert_feature_dependencies(copy)


@pytest.mark.parametrize("action,values,field", [
    ("offer", {"amount":"0", "note":"Keep this note"}, "amount"),
    ("offer", {"amount":"99999", "note":"Keep this note"}, "amount"),
    ("offer", {"amount":"10", "note":""}, "note"),
    ("message", {"text":"Keep https://synthetic.invalid here"}, "text"),
])
def test_dr1b_rejected_inputs_keep_values_and_link_to_the_field(monkeypatch, web_mode, action, values, field):
    web, fake = _web(monkeypatch)
    response = web.post(PAGE+"/preview", data={"csrf_token":TOKEN,"action":action,**values})
    assert response.status_code == 400
    assert "Error:" in response.text and "There is a problem" in response.text
    assert f'href="#field-{field}"' in response.text and f'id="error-{field}"' in response.text
    assert response.text.index('class="error-summary"') < response.text.index("<h1>")
    for value in values.values():
        if value: assert value in response.text
    assert not fake.posts()
