from html import unescape
"""/disputes/<id>/draft on the fake PayPal, a fake model call and a throwaway database: flag off, flag on, the hand-off, the guarantees."""
from _hosted import authenticated_client, reset_csrf
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests" / "local"))
from _hosted import authenticated_client, configure_hosted

import pytest
from fastapi.testclient import TestClient

from copilot.app import create_app
from copilot.features.draft import store
from copilot.web.routes_disputes import get_client
from draft_fakes import DISPUTE_ID, EN_NOEST, ES_NOEST, FakePayPal, model_call
from draft_fakes import _no_httpx_calls  # noqa: F401  (autouse: no httpx call in these tests)

FLAG = "COPILOT_FEATURE_DRAFT"
ROOT = Path(__file__).resolve().parents[3]
TOKEN = "test-csrf-token"
PAGE, MODEL_POST = f"/disputes/{DISPUTE_ID}/draft", f"/disputes/{DISPUTE_ID}/draft/model"
GOOD = {"en": EN_NOEST, "es": ES_NOEST}


def _web(monkeypatch, fake=None, call=None, flag="true"):
    monkeypatch.delenv(FLAG, raising=False) if flag is None else monkeypatch.setenv(FLAG, flag)
    fake = fake or FakePayPal()
    call = call or model_call(GOOD)
    configure_hosted(monkeypatch)
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    if flag == "true" and not config.hosted_demo():
        from copilot.features.draft import routes
        app.dependency_overrides[routes.get_model_call] = lambda: call
    web = authenticated_client(app)
    set_test_csrf(web, TOKEN)
    return web, fake, call


def _reset(web):
    reset_csrf(web)
    set_test_csrf(web, TOKEN)


# ---- flag off (the default) ------------------------------------------------------------------------------------
def test_flag_off_both_routes_are_404_and_nothing_is_called(monkeypatch):
    web, fake, call = _web(monkeypatch, flag=None)
    assert web.get(PAGE).status_code == 404
    assert web.post(MODEL_POST, data={"csrf_token": TOKEN}).status_code == 404
    if config.hosted_demo():
        assert web.get("/openapi.json").status_code == 404
    else:
        assert not [p for p in web.get("/openapi.json").json()["paths"] if "/draft" in p and not p.endswith("/resolution/draft")]   # that route is the negotiation_drafts flag's
    assert fake.calls == [] and call.calls == []


@pytest.mark.parametrize("value", ["1", "yes", "false", ""])
def test_flag_off_for_any_value_other_than_true(monkeypatch, value):
    web, _fake, _call = _web(monkeypatch, flag=value)
    assert web.get(PAGE).status_code == 404


def test_create_app_with_the_flag_off_loads_no_draft_module():
    code = ("import sys\nfrom copilot.app import create_app\ncreate_app()\n"
            "print(sorted(m for m in sys.modules if m.startswith('copilot.features.draft')))")
    env = {k: v for k, v in os.environ.items() if not k.startswith("COPILOT_FEATURE_")}
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


# ---- flag on ---------------------------------------------------------------------------------------------------
def test_the_page_shows_the_facts_the_template_the_channel_and_the_disclaimer_and_never_calls_the_model(monkeypatch):
    web, fake, call = _web(monkeypatch)
    r = web.get(PAGE)
    assert r.status_code == 200
    for text in ("40.00 USD", "DEMO-FDX-000001", "Domicilio Demo 123", 'data-state="facts"', 'data-state="template"',
                 "Hello, this is about your order of 28 September 2026 for 40.00 USD."):
        assert text in r.text
    # The facts table shows the order date in words (assistant); the ISO form stays only in the raw facts (local)
    assert '<th scope="row">Order date</th><td>28 September 2026</td>' in r.text
    assert 'data-state="disclaimer"' in r.text and "can be wrong" in r.text and ('data-state="no-draft"' in r.text) == (not config.hosted_demo())
    assert 'data-state="model-label"' not in r.text and call.calls == []
    assert all(m == "GET" for m, _p, _o in fake.calls)


@pytest.mark.local_spec
def test_the_model_post_stores_the_draft_and_redirects_303_to_the_page(monkeypatch):
    web, _fake, call = _web(monkeypatch)
    r = web.post(MODEL_POST, data={"csrf_token": TOKEN}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == PAGE
    assert len(call.calls) == 1 and store.latest_draft(DISPUTE_ID)["validation_ok"] is True


@pytest.mark.local_spec
def test_the_page_after_a_valid_draft_shows_both_languages_and_the_suggested_by_the_local_model_label(monkeypatch):
    web, _fake, _call = _web(monkeypatch)
    web.post(MODEL_POST, data={"csrf_token": TOKEN})
    _reset(web)
    page = web.get(PAGE).text
    assert "Written by AI (local model)" in page and 'data-state="draft" data-valid="yes"' in page
    assert 'data-lang="en"' in page and 'data-lang="es"' in page and "Hola, sobre su pedido" in page and "Hello, about your order" in page
    assert 'data-state="problems"' not in page


@pytest.mark.local_spec
def test_a_refused_draft_shows_its_problems_and_the_textareas_hold_the_template(monkeypatch):
    web, _fake, _call = _web(monkeypatch, call=model_call({"en": EN_NOEST + " Ref 98765.", "es": ES_NOEST}))
    web.post(MODEL_POST, data={"csrf_token": TOKEN})
    _reset(web)
    page = web.get(PAGE).text
    assert 'data-state="draft" data-valid="no"' in page and "It has numbers that are not in the facts: 98765" in page
    assert "The AI text was not used" in page and "Your message still holds the fixed template." in page
    textarea_en = re.search(r'<textarea name="text"[^>]*>(.*?)</textarea>', page, re.S).group(1)
    assert textarea_en.startswith("Hello, this is about your order of 28 September 2026") and "98765" not in textarea_en
    assert "Starts as the fixed template, written by code from PayPal" in page


@pytest.mark.local_spec
def test_an_unavailable_model_answers_503_with_the_sentence_on_the_page(monkeypatch):
    web, _fake, call = _web(monkeypatch, call=model_call(raises=RuntimeError("GPU busy")))
    r = web.post(MODEL_POST, data={"csrf_token": TOKEN}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == PAGE + "?model=unavailable#ai"       # No re-run on refresh
    shown = web.get(PAGE + "?model=unavailable")
    assert "The AI model did not answer. Your message still holds the fixed template." in shown.text and 'data-state="model-error"' in shown.text
    assert 'data-state="template"' in shown.text and store.latest_draft(DISPUTE_ID) is None and len(call.calls) == 1


def test_missing_or_wrong_csrf_is_the_apps_usual_403_and_the_model_is_not_called(monkeypatch):
    web, fake, call = _web(monkeypatch)
    for token in ("", "forged"):
        assert web.post(MODEL_POST, data={"csrf_token": token}).status_code == 403
    assert call.calls == [] and fake.calls == []


def test_a_bad_dispute_id_is_400_before_any_call(monkeypatch):
    web, fake, call = _web(monkeypatch)
    assert web.get("/disputes/not-an-id/draft").status_code == 400
    assert web.post("/disputes/not-an-id/draft/model", data={"csrf_token": TOKEN}).status_code == 400
    assert fake.calls == [] and call.calls == []


# ---- the hand-off to the resolution feature ----------------------------------------------------------------------
@pytest.mark.local_spec
def test_with_the_send_message_link_the_form_posts_the_text_to_the_resolution_preview_with_action_message(monkeypatch):
    web, _fake, _call = _web(monkeypatch)
    web.post(MODEL_POST, data={"csrf_token": TOKEN})
    _reset(web)
    page = web.get(PAGE).text
    form = re.search(r'<form method="post" action="([^"]+)" novalidate data-state="handoff-form">(.*?)</form>', page, re.S)
    assert form.group(1) == f"/disputes/{DISPUTE_ID}/resolution/preview#preview"
    assert 'name="action" value="message"' in form.group(2) and 'name="csrf_token"' in form.group(2)
    assert re.search(r'<textarea name="text"[^>]*>Hello, this is about your order of 28 September 2026', form.group(2))     # The template, never the AI text
    assert "PayPal accepts a message on this dispute. Nothing is sent from this page." in page


@pytest.mark.local_spec
def test_the_hand_off_form_reaches_the_resolution_preview_and_shows_the_exact_body_and_its_hash(monkeypatch):
    monkeypatch.setenv("COPILOT_FEATURE_RESOLUTION", "true")
    monkeypatch.setenv("COPILOT_FEATURE_RESOLUTION_WRITES", "true")
    web, fake, _call = _web(monkeypatch)
    web.post(MODEL_POST, data={"csrf_token": TOKEN})
    _reset(web)
    page = web.get(PAGE).text
    text = re.search(r'<textarea name="text"[^>]*>(.*?)</textarea>', page, re.S).group(1)
    import html
    token = re.search(r'data-state="handoff-form">\s*<input type="hidden" name="csrf_token" value="([^"]+)"', page).group(1)
    preview = web.post(f"/disputes/{DISPUTE_ID}/resolution/preview",     # the page's own cookie and the form's own token
                       data={"csrf_token": token, "action": "message", "text": html.unescape(text)})
    assert preview.status_code == 200 and "Hello, this is about your order of 28 September 2026" in preview.text   # the check page (303 followed)
    assert re.search(r'data-state="request-sha256">[0-9a-f]{64}<', preview.text)
    assert not [c for c in fake.calls if c[0] != "GET"]                    # nothing was sent: the seller still has to approve


def test_without_the_send_message_link_the_text_is_copy_only_and_there_is_no_hand_off_form(monkeypatch):
    web, _fake, _call = _web(monkeypatch, FakePayPal(rels=("provide_evidence",)))
    page = web.get(PAGE).text
    assert 'data-channel="copy_only"' in page and 'data-state="handoff-form"' not in page and 'name="text"' not in page
    assert "PayPal offers no send-message option for this dispute. You can copy this text and send it to the buyer yourself; PayPal\'s record will not show it." in unescape(page)
    assert 'data-state="copy-en"' in page and 'data-state="copy-es"' not in page and "readonly" not in page
    assert "No Spanish text: the fixed template is in English only." in page


def test_the_spanish_text_is_a_copy_area_in_both_channels(monkeypatch):
    web, _fake, _call = _web(monkeypatch)
    web.post(MODEL_POST, data={"csrf_token": TOKEN})
    _reset(web)
    page = web.get(PAGE).text
    if config.hosted_demo():
        assert 'data-state="copy-es"' not in page
        assert "No Spanish text: the fixed template is in English only." in page
    else:
        assert 'data-state="copy-es"' in page


# ---- guarantees about the code itself --------------------------------------------------------------------------
def test_exactly_two_routes_and_no_paypal_write_path_anywhere_in_the_feature():
    from copilot.features.draft import routes
    feature = ROOT / "copilot" / "features" / "draft"
    paths = sorted(f"{m} {r.path}" for r in routes.router.routes for m in r.methods if m != "HEAD")
    assert paths == ["GET /disputes/{dispute_id}/draft", "POST /disputes/{dispute_id}/draft/model"]
    source = "\n".join(p.read_text(encoding="utf-8") for p in sorted(feature.glob("*.py")))
    template = (ROOT / "copilot" / "web" / "templates" / "features" / "draft" / "draft.html").read_text(encoding="utf-8")
    for forbidden in ("/escalate", "/provide-evidence", "/send-message", "/make-offer", "/accept-claim", "/appeal"):
        assert forbidden not in source and forbidden not in template, forbidden
    assert 'call("POST"' not in source and '"POST"' not in source and "json_body" not in source


@pytest.mark.local_spec
def test_the_only_post_the_page_offers_to_a_resolution_route_is_the_preview(monkeypatch):
    web, _fake, _call = _web(monkeypatch)
    web.post(MODEL_POST, data={"csrf_token": TOKEN})
    _reset(web)
    actions = set(re.findall(r'<form method="post" action="([^"]+)"', web.get(PAGE).text))
    assert actions == {f"/disputes/{DISPUTE_ID}/draft/model", f"/disputes/{DISPUTE_ID}/resolution/preview#preview"}


def test_no_test_of_the_feature_reaches_httpx():
    import httpx
    with pytest.raises(AssertionError):
        httpx.post("http://127.0.0.1:11434/api/chat", json={})
    with pytest.raises(AssertionError):
        httpx.get("http://127.0.0.1:11434/api/ps")


# Explicit mode contract; local specifications keep their original business assertions.
from copilot import config


@pytest.mark.hosted_spec
@pytest.mark.parametrize("answer", [GOOD, {"en": EN_NOEST + " Ref 98765.", "es": ES_NOEST}])
def test_hosted_model_post_refuses_without_call_or_stored_draft_and_keeps_the_template(monkeypatch, answer):
    web, fake, call = _web(monkeypatch, call=model_call(answer))
    r = web.post(MODEL_POST, data={"csrf_token": TOKEN})
    assert r.status_code == 503 and "off in the hosted demo" in r.text
    assert call.calls == [] and all(m == "GET" for m, _p, _o in fake.calls) and store.latest_draft(DISPUTE_ID) is None
    _reset(web)
    page = web.get(PAGE).text
    assert 'data-state="template"' in page and 'data-state="model-form"' not in page
    assert "Hello, this is about your order of 28 September 2026" in page


@pytest.mark.hosted_spec
def test_hosted_handoff_uses_fixed_template_and_cd_resolution_keeps_access_csrf(monkeypatch):
    web, fake, call = _web(monkeypatch)
    page = web.get(PAGE).text
    assert 'data-state="handoff-form"' in page and "Hello, this is about your order" in page
    assert call.calls == [] and all(m == "GET" for m, _p, _o in fake.calls)
    monkeypatch.setenv("COPILOT_FEATURE_RESOLUTION", "true")
    monkeypatch.setenv("COPILOT_FEATURE_RESOLUTION_WRITES", "true")
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    before = list(fake.calls)
    target = f"/disputes/{DISPUTE_ID}/resolution/preview"
    naked = TestClient(app, base_url="https://localhost")
    assert naked.post(target, data={}).status_code == 401
    assert fake.calls == before and call.calls == []
    enabled = authenticated_client(app)
    assert enabled.post(target, data={}).status_code == 403
    assert fake.calls == before and call.calls == []
    set_test_csrf(enabled, TOKEN)
    preview = enabled.post(target, data={"csrf_token": TOKEN, "action": "message",
                                        "text": "Synthetic seller message"})
    assert preview.status_code == 200 and 'data-state="confirm-form"' in preview.text
    assert call.calls == [] and all(m == "GET" for m, _p, _o in fake.calls)
pytestmark = [pytest.mark.usefixtures("web_mode")]

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

_FROZEN_FEATURE_DEPENDENCIES = {'resolution'}


def _assert_feature_dependencies(directory):
    assert _feature_dependencies(directory, 'draft') == _FROZEN_FEATURE_DEPENDENCIES


def test_exact_feature_dependencies_by_source_ast():
    from pathlib import Path
    directory = Path(__file__).resolve().parents[3] / "copilot" / "features" / 'draft'
    _assert_feature_dependencies(directory)


def test_dependency_graph_rejects_planted_absolute_and_relative_imports(tmp_path):
    from pathlib import Path
    import pytest
    source = Path(__file__).resolve().parents[3] / "copilot" / "features" / 'draft'
    for form, statement in (("absolute", "import copilot.features.triage.service"),
                            ("relative", "from ..triage import service")):
        copy = tmp_path / form
        for path in source.rglob("*.py"):
            target = copy / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(path.read_text(encoding="utf-8-sig"), encoding="utf-8")
        (copy / "planted.py").write_text(statement + "\n", encoding="utf-8")
        assert _feature_dependencies(copy, 'draft') == _FROZEN_FEATURE_DEPENDENCIES | {"triage"}
        with pytest.raises(AssertionError):
            _assert_feature_dependencies(copy)


def test_dr1b_handoff_message_has_own_required_label_and_convention(web_mode, monkeypatch):
    from _dom import parse
    web, fake, call = _web(monkeypatch)
    page = web.get(PAGE)
    assert page.status_code == 200
    form = parse(page.text).find("form", data_state="handoff-form")
    assert "Fields marked Required" not in form.text()                      # One field, no convention sentence
    control = form.find("textarea", name="text")
    assert control.attrs.get("aria-required") == "true" and control.attrs.get("id")
    label = form.find("label", **{"for": control.attrs["id"]})
    assert "English text for the buyer" in label.text() and "Required" in label.text()
    assert control.text() and form.find("input", name="action").attrs["value"] == "message"
    assert all(c[0] == "GET" for c in fake.calls) and call.calls == []


def test_dr1b_fact_display_is_human_but_sheet_and_buyer_template_are_verbatim(web_mode, monkeypatch):
    import json
    from _dom import parse
    from copilot import config
    from copilot.features.draft import service
    web, fake, call = _web(monkeypatch)
    before = service.draft_view(fake, config.db_path(), DISPUTE_ID)
    page = web.get(PAGE)
    assert page.status_code == 200
    dom = parse(page.text)
    facts = dom.find("table", data_state="facts")
    shown = {row.find("th").text(): row.find("td").text() for row in facts.find_all("tr")}
    assert shown["Order date"] == "28 September 2026"
    assert shown["Last tracking status"] == "Shipped" and shown["Dispute stage"] == "Inquiry"
    raw = dom.find("pre", data_state="facts-raw")
    if config.hosted_demo():
        assert raw is None                                                      # The data block is not shown in the hosted demo
    else:
        assert raw is not None and raw.inside("details")
        assert json.loads(raw.text()) == [list(pair) for pair in before.sheet.labelled()]
    prefill = unescape(re.search(r'<textarea name="text"[^>]*>(.*?)</textarea>', page.text, re.S).group(1))
    assert prefill == before.template_text and 'data-state="template"' in page.text and "<pre data-state=\"template\">" not in page.text
    assert service.draft_view(fake, config.db_path(), DISPUTE_ID).sheet == before.sheet
    assert call.calls == [] and all(c[0] == "GET" for c in fake.calls)
