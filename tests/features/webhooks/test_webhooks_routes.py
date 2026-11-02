"""/webhooks routes on the fake client and a throwaway database: flag off, id unset, id set, and guarantees about the code."""
from _hosted import authenticated_client, reset_csrf
import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from copilot.app import create_app
from copilot.features.webhooks import routes, store
from copilot.web.routes_disputes import get_client
from webhooks_fakes import DISPUTE_ID, HEADERS, WEBHOOK_ID, FakeClient, dispute_get, event, raw

FLAG, WH_ID = "COPILOT_FEATURE_WEBHOOKS", "COPILOT_WEBHOOK_ID"
ROOT = Path(__file__).resolve().parents[3]
POST, PAGE = "/webhooks/paypal", "/webhooks/events"


def _web(monkeypatch, fake=None, flag="true", webhook_id=WEBHOOK_ID, client_addr=("127.0.0.1", 12345)):
    for name, value in ((FLAG, flag), (WH_ID, webhook_id)):
        monkeypatch.delenv(name, raising=False) if value is None else monkeypatch.setenv(name, value)
    fake = fake or FakeClient()
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    return authenticated_client(app, client=client_addr), fake


# ---- flag off (the default) ------------------------------------------------------------------------------------
def test_flag_off_both_routes_are_404_and_nothing_is_called(monkeypatch):
    web, fake = _web(monkeypatch, flag=None)
    assert web.get(PAGE).status_code == 404
    assert web.post(POST, content=raw(), headers=HEADERS).status_code == 404
    from copilot import config
    if config.hosted_demo():
        assert web.get("/openapi.json").status_code == 404
    else:
        assert not [p for p in web.get("/openapi.json").json()["paths"] if "webhooks" in p]
    assert fake.calls == []


@pytest.mark.parametrize("value", ["1", "yes", "false", ""])
def test_flag_off_for_any_value_other_than_true(monkeypatch, value):
    web, _fake = _web(monkeypatch, flag=value)
    assert web.get(PAGE).status_code == 404 and web.post(POST, content=raw(), headers=HEADERS).status_code == 404


def test_the_webhook_id_alone_does_not_turn_the_feature_on(monkeypatch):
    web, fake = _web(monkeypatch, flag=None, webhook_id=WEBHOOK_ID)
    assert web.post(POST, content=raw(), headers=HEADERS).status_code == 404 and fake.calls == []


def test_create_app_with_the_flag_off_loads_no_webhooks_module():
    code = ("import sys\nfrom copilot.app import create_app\ncreate_app()\n"
            "print(sorted(m for m in sys.modules if m.startswith('copilot.features.webhooks')))")
    env = {k: v for k, v in os.environ.items() if not k.startswith("COPILOT_FEATURE_")}
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


# ---- flag on, id unset -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("value", [None, "", "   "])
def test_with_the_id_unset_or_blank_the_post_is_503_with_no_call_and_nothing_stored(monkeypatch, value):
    web, fake = _web(monkeypatch, webhook_id=value)
    r = web.post(POST, content=raw(), headers=HEADERS)
    assert r.status_code == 503 and fake.calls == [] and store.list_events() == []


def test_with_the_id_unset_the_page_shows_the_not_configured_line_and_the_notice(monkeypatch):
    web, fake = _web(monkeypatch, webhook_id=None)
    r = web.get(PAGE)
    assert r.status_code == 200
    assert "Proofie is not set up to receive PayPal notices." in r.text and "COPILOT_WEBHOOK_ID" in r.text
    assert "Every notice is checked with PayPal." in r.text
    assert 'data-state="no-events"' in r.text and fake.calls == []


def test_with_the_id_set_the_not_configured_line_is_absent(monkeypatch):
    web, _fake = _web(monkeypatch)
    assert 'data-state="not-configured"' not in web.get(PAGE).text


# ---- flag on, id set -------------------------------------------------------------------------------------------
def test_a_verified_event_answers_200_and_appears_on_the_page_with_the_gets_status(monkeypatch):
    web, fake = _web(monkeypatch, FakeClient(get_answer=dispute_get("WAITING_FOR_BUYER_RESPONSE")))
    r = web.post(POST, content=raw(event(status="OPEN")), headers=HEADERS)
    assert r.status_code == 200 and r.json() == {"verification": "verified", "readback_status": "WAITING_FOR_BUYER_RESPONSE"}
    assert [(c[0], c[2]) for c in fake.calls] == [("POST", "webhook_verify"), ("GET", "webhook_readback")]
    body = fake.calls[0][3]
    assert body["webhook_id"] == WEBHOOK_ID and body["transmission_id"] == "TRANS-TEST-1"
    page = web.get(PAGE).text
    assert 'data-event="WH-TEST-1"' in page and "CUSTOMER.DISPUTE.CREATED" in page and "WAITING_FOR_BUYER_RESPONSE" in page
    assert f'href="/disputes/{DISPUTE_ID}"' in page and ">OPEN<" not in page and "verified" in page
    assert store.list_events()[0]["body_sha256"][:12] in page


def test_the_same_event_delivered_twice_is_a_duplicate_with_no_second_call(monkeypatch):
    web, fake = _web(monkeypatch)
    web.post(POST, content=raw(), headers=HEADERS)
    n = len(fake.calls)
    r = web.post(POST, content=raw(), headers=HEADERS)
    assert r.status_code == 200 and r.json()["verification"] == "duplicate" and len(fake.calls) == n
    assert web.get(PAGE).text.count('data-event="WH-TEST-1"') == 1


def test_a_failed_signature_is_400_and_the_page_lists_it_as_failed_with_no_status(monkeypatch):
    web, fake = _web(monkeypatch, FakeClient({"verification_status": "FAILURE"}))
    r = web.post(POST, content=raw(), headers=HEADERS)
    assert r.status_code == 400 and r.json() == {"verification": "failed", "readback_status": None}
    assert fake.gets() == [] and "failed" in web.get(PAGE).text


def test_a_post_without_the_paypal_headers_is_400_and_a_bad_body_is_400_and_oversize_is_413(monkeypatch):
    web, fake = _web(monkeypatch)
    assert web.post(POST, content=raw()).status_code == 400
    assert web.post(POST, content=b"nope", headers=HEADERS).status_code == 400
    assert web.post(POST, content=b"x" * 70000, headers=HEADERS).status_code == 413
    assert fake.calls == []


def test_the_response_is_one_small_json_object_and_nothing_else(monkeypatch):
    web, _fake = _web(monkeypatch)
    r = web.post(POST, content=raw(), headers=HEADERS)
    assert set(r.json()) == {"verification", "readback_status"} and r.headers["content-type"].startswith("application/json")


def test_the_page_lists_the_newest_event_first_and_at_most_100(monkeypatch):
    web, _fake = _web(monkeypatch)
    for i in range(103):
        store.save_event(f"WH-{i:03d}", "CUSTOMER.DISPUTE.UPDATED", "dispute", None, None, "c" * 64, "verified")
    page = web.get(PAGE).text
    assert page.count('data-event="') == 100 and page.index('data-event="WH-102"') < page.index('data-event="WH-101"')
    assert 'data-event="WH-002"' not in page and 'data-event="WH-003"' in page


def test_the_loopback_middleware_still_holds_for_the_listener(monkeypatch):
    web, fake = _web(monkeypatch, client_addr=("10.1.2.3", 4000))
    from copilot import config
    if config.hosted_demo():
        web.cookies.clear()
        assert web.get(PAGE, follow_redirects=False).status_code == 303
        assert fake.calls == []
        assert web.post(POST, content=raw(), headers=HEADERS).status_code == 200
        assert fake.calls[0][1] == "/v1/notifications/verify-webhook-signature"
    else:
        assert web.post(POST, content=raw(), headers=HEADERS).status_code == 403
        assert web.get(PAGE).status_code == 403 and fake.calls == []


def test_the_webhook_id_is_read_stripped_on_every_call(monkeypatch):
    monkeypatch.setenv(WH_ID, "  WH-X  ")
    assert routes.webhook_id() == "WH-X"
    monkeypatch.setenv(WH_ID, "   ")
    assert routes.webhook_id() is None
    monkeypatch.delenv(WH_ID, raising=False)
    assert routes.webhook_id() is None


# ---- guarantees about the code itself --------------------------------------------------------------------------
def test_exactly_two_routes_and_none_of_the_forbidden_paths_anywhere_in_the_feature():
    feature = ROOT / "copilot" / "features" / "webhooks"
    paths = sorted(f"{m} {r.path}" for r in routes.router.routes for m in r.methods if m != "HEAD")
    assert paths == ["GET /webhooks/events", "POST /webhooks/paypal"]
    source = "\n".join(p.read_text(encoding="utf-8") for p in sorted(feature.glob("*.py")))
    templates = (ROOT / "copilot" / "web" / "templates" / "features" / "webhooks" / "events.html").read_text(encoding="utf-8")
    for forbidden in ("/escalate", "/provide-evidence", "/send-message", "/make-offer", "/accept-claim"):
        assert forbidden not in source and forbidden not in templates, forbidden


def test_the_post_route_has_no_csrf_dependency_and_the_get_route_sets_no_csrf_cookie(monkeypatch):
    tree = ast.parse((ROOT / "copilot" / "features" / "webhooks" / "routes.py").read_text(encoding="utf-8"))
    imported = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert "copilot.web.security" not in imported                          # no csrf helper is even imported
    post = next(r for r in routes.router.routes if "POST" in r.methods)
    assert [d.call.__name__ for d in post.dependant.dependencies] == ["verification_client"]
    web, _fake = _web(monkeypatch)
    page = web.get(PAGE)
    assert "set-cookie" not in page.headers and 'name="csrf_token"' not in page.text and "<form" not in page.text
    posted = web.post(POST, content=raw(), headers=HEADERS)
    assert posted.status_code == 200 and "set-cookie" not in posted.headers   # no cookie needed, none issued


def test_the_listener_makes_only_the_verification_post_and_the_dispute_get(monkeypatch):
    web, fake = _web(monkeypatch)
    for i in range(3):
        web.post(POST, content=raw(event(f"WH-TEST-{i}")), headers=HEADERS)
    assert {(c[0], c[1].split("?")[0] if c[0] == "POST" else c[1].rsplit("/", 1)[0]) for c in fake.calls} == {
        ("POST", "/v1/notifications/verify-webhook-signature"), ("GET", "/v1/customer/disputes")}


# Explicit mode contract; local specifications keep their original business assertions.
pytestmark = [pytest.mark.usefixtures("web_mode")]



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

_FROZEN_FEATURE_DEPENDENCIES = set()


def _assert_feature_dependencies(directory):
    assert _feature_dependencies(directory, 'webhooks') == _FROZEN_FEATURE_DEPENDENCIES


def test_exact_feature_dependencies_by_source_ast():
    from pathlib import Path
    directory = Path(__file__).resolve().parents[3] / "copilot" / "features" / 'webhooks'
    _assert_feature_dependencies(directory)


def test_dependency_graph_rejects_planted_absolute_and_relative_imports(tmp_path):
    from pathlib import Path
    import pytest
    source = Path(__file__).resolve().parents[3] / "copilot" / "features" / 'webhooks'
    for form, statement in (("absolute", "import copilot.features.triage.service"),
                            ("relative", "from ..triage import service")):
        copy = tmp_path / form
        for path in source.rglob("*.py"):
            target = copy / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(path.read_text(encoding="utf-8-sig"), encoding="utf-8")
        (copy / "planted.py").write_text(statement + "\n", encoding="utf-8")
        assert _feature_dependencies(copy, 'webhooks') == _FROZEN_FEATURE_DEPENDENCIES | {"triage"}
        with pytest.raises(AssertionError):
            _assert_feature_dependencies(copy)


@pytest.fixture(autouse=True)
def enable_existing_verification_contract(monkeypatch):
    monkeypatch.setenv("COPILOT_FEATURE_WEBHOOKS_VERIFY", "true")


@pytest.mark.parametrize("verification,kind,label", [
    ("verified", "ok", "Signature verified"),
    ("failed", "bad", "Signature verification failed"),
    ("rejected_headers", "bad", "Required verification headers were rejected"),
    ("limitation", "warn", "Signature could not be verified"),
    ("unverified", "warn", "Signature has not been verified"),
])
def test_dr1b_webhook_populated_row_labels_keep_raw_record_without_receiver_call(web_mode, monkeypatch, verification, kind, label):
    from _dom import parse
    web, fake = _web(monkeypatch)
    store.save_event("WH-DEMO-LABEL", "CUSTOMER.DISPUTE.UPDATED", "dispute", DISPUTE_ID,
                     None, "a" * 64, verification, "UNDER_REVIEW")
    before = store.list_events()
    page = web.get(PAGE)
    assert page.status_code == 200
    dom = parse(page.text)
    row = dom.find("tr", data_event="WH-DEMO-LABEL")
    type_cell = row.find("td", data_label="What happened")
    assert type_cell is not None and type_cell.text() == "Dispute updated"
    verified = row.find("td", data_label="Signature check")
    assert label in verified.text() and verified.find("span", class_="tag--" + kind) is not None
    details = row.find("details")
    assert details.find("summary").text() == "Technical details"
    assert "CUSTOMER.DISPUTE.UPDATED" in details.text() and verification in details.text()
    assert "Under review by PayPal" in row.text()
    assert fake.calls == [] and store.list_events() == before
