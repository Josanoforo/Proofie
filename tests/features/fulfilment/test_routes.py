"""/fulfilment on a fake client and a temp database: flag off (default), flag on, the register flag, the approval hash."""
from _hosted import authenticated_client, reset_csrf
import re

import pytest
from fastapi.testclient import TestClient

from copilot import config
from copilot.app import create_app
from copilot.core.disputes import PayPalError
from copilot.db import repo
from copilot.features.fulfilment import store
from copilot.web.routes_disputes import get_client

FLAG, REG_FLAG = "COPILOT_FEATURE_FULFILMENT", "COPILOT_FEATURE_FULFILMENT_REGISTER"
CAP = "5S647034CC1979815"
TOKEN = "test-csrf-token"


def test_ui_copy_a6_register_off_has_only_local_save_promise(monkeypatch):
    web, fake = _web(monkeypatch, register=None)
    listing = web.get("/fulfilment")
    assert listing.status_code == 200
    assert "it does not register the tracking with PayPal" in listing.text
    sha, preview = _sha_of_preview(web)
    assert "Save the tracking file" in preview and len(sha) == 64
    outside_notice = re.sub(r'<p[^>]*data-state="register-off"[^>]*>.*?</p>', '', preview, flags=re.S)
    assert "register with PayPal" not in outside_notice
    assert not fake.posts()
FORM = {"csrf_token": TOKEN, "tracking_number": "TRK1TEST20261006", "carrier": "fedex", "status": "SHIPPED",
        "declared_postal_code": "06600", "declared_city": "Ciudad de Mexico", "declared_state": "CDMX",
        "declared_country_code": "MX"}
TRANSACTIONS = {"transaction_details": [{
    "transaction_info": {"transaction_id": CAP, "transaction_initiation_date": "2026-10-06T10:00:00+0000",
                         "transaction_amount": {"currency_code": "USD", "value": "35.00"},
                         "protection_eligibility": "01"},
    "shipping_info": {"name": "Ana Demo Ficticia", "address": {
        "line1": "Domicilio Demo 123", "city": "Ciudad de Mexico", "state": "CDMX", "country_code": "MX",
        "postal_code": "06600"}}}], "total_items": 1}


class FakeClient:
    """Serves the transaction, keeps the trackers a POST registered, records (method, path, op)."""
    def __init__(self, post_raises=None, list_after_post=True):
        self.calls, self.trackers = [], []
        self.post_raises, self.list_after_post = post_raises, list_after_post

    def call(self, method, path, op, **kw):
        self.calls.append((method, path, op))
        if method == "POST":
            if self.post_raises:
                raise self.post_raises
            if self.list_after_post:
                self.trackers += [{**t, "shipment_date": "2026-10-06", "last_updated_time": 1,
                                   "links": [{"rel": "self"}]} for t in kw["json_body"]["trackers"]]
            return {"trackers": [{"links": []}]}
        if path.startswith("/v1/reporting/transactions"):
            return TRANSACTIONS
        return {"trackers": self.trackers}

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


def _web(monkeypatch, fake=None, flag="true", register="true"):
    for name, value in ((FLAG, flag), (REG_FLAG, register)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    fake = fake or FakeClient()
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    web = authenticated_client(app, client=("127.0.0.1", 12345))
    set_test_csrf(web, TOKEN)
    return web, fake


_CHECK_TOKENS = {}


def _sha_of_preview(web, form=FORM):
    """The preview answers 303 to the check page (a GET of the stored body). Returns the sha256 shown
    there and the page; the check token is kept for _register_data."""
    response = web.post(f"/fulfilment/{CAP}/preview", data=form, follow_redirects=False)
    assert response.status_code == 303, response.text[:300]
    html = web.get(response.headers["location"]).text
    # the check page rotates the csrf cookie (its confirm form carries the new token); the tests post with the fixed
    # TOKEN, so clear the jar (the response cookie is a second, domain-scoped entry) and put it back
    reset_csrf(web)
    set_test_csrf(web, TOKEN)
    sha = re.search(r'data-state="request-sha256">([0-9a-f]{64})<', html).group(1)
    _CHECK_TOKENS[sha] = re.search(r"/check/([A-Za-z0-9_-]+)#preview", response.headers["location"]).group(1)
    return sha, html


def _register_data(sha, **extra):
    """The confirm form's three fields: csrf token, check token and the sha256 shown on the check page."""
    return {"csrf_token": TOKEN, "token": _CHECK_TOKENS.get(sha, "no-such-check"), "request_sha256": sha, **extra}


# ---- flag off (the default) ------------------------------------------------------------------------------------
@pytest.mark.local_spec
def test_flag_off_every_fulfilment_route_is_404_and_no_router_is_included(monkeypatch):
    web, fake = _web(monkeypatch, flag=None, register=None)
    for path in ("/fulfilment", f"/fulfilment/{CAP}", "/fulfilment/open?capture_id=" + CAP):
        assert web.get(path).status_code == 404, path
    assert web.post(f"/fulfilment/{CAP}/preview", data=FORM).status_code == 404
    assert web.post(f"/fulfilment/{CAP}/register", data={**FORM, "request_sha256": "0" * 64}).status_code == 404
    assert not [p for p in web.get("/openapi.json").json()["paths"] if "fulfilment" in p and not p.endswith("/readiness")]   # that route is the sale_readiness flag's
    assert fake.calls == []


@pytest.mark.local_spec
@pytest.mark.parametrize("value", ["1", "yes", "false", ""])
def test_flag_off_for_any_value_other_than_true(monkeypatch, value):
    web, _fake = _web(monkeypatch, flag=value)
    assert web.get("/fulfilment").status_code == 404


@pytest.mark.local_spec
def test_the_register_flag_alone_does_not_turn_the_feature_on(monkeypatch):
    web, _fake = _web(monkeypatch, flag=None, register="true")
    assert web.get("/fulfilment").status_code == 404


# ---- flag on ---------------------------------------------------------------------------------------------------
@pytest.mark.local_spec
def test_list_page_starts_empty(monkeypatch):
    web, _fake = _web(monkeypatch)
    r = web.get("/fulfilment")
    assert r.status_code == 200 and 'data-state="no-files"' in r.text


@pytest.mark.local_spec
def test_open_redirects_to_the_capture_and_refuses_a_bad_id(monkeypatch):
    web, _fake = _web(monkeypatch)
    r = web.get("/fulfilment/open?capture_id=" + CAP, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/fulfilment/{CAP}"
    assert web.get("/fulfilment/open?capture_id=bad").status_code == 400


@pytest.mark.local_spec
def test_form_shows_paypals_fields_and_sets_the_csrf_cookie(monkeypatch):
    web, fake = _web(monkeypatch)
    r = web.get(f"/fulfilment/{CAP}")
    assert r.status_code == 200
    for text in ("35.00 USD", "Ana Demo Ficticia", "Domicilio Demo 123", "6 October 2026", 'data-state="tracking-form"'):
        assert text in r.text
    assert "csrf_token" in r.headers.get("set-cookie", "")
    assert all(m == "GET" for m, _p, _o in fake.calls)


@pytest.mark.local_spec
def test_a_bad_capture_id_is_a_400_before_any_call(monkeypatch):
    web, fake = _web(monkeypatch)
    assert web.get("/fulfilment/short").status_code == 400
    assert web.post("/fulfilment/short/preview", data=FORM).status_code == 400
    assert web.post("/fulfilment/short/register", data={**FORM, "request_sha256": "x"}).status_code == 400
    assert fake.calls == []


@pytest.mark.local_spec
def test_preview_shows_the_exact_body_and_its_sha256_and_makes_no_call(monkeypatch):
    web, fake = _web(monkeypatch)
    sha, html = _sha_of_preview(web)
    assert len(sha) == 64
    assert "&#34;tracking_number&#34;: &#34;TRK1TEST20261006&#34;" in html or '"tracking_number": "TRK1TEST20261006"' in html
    assert 'name="request_sha256" value="' + sha + '"' in html
    assert not fake.posts()                      # the preview reads PayPal's tracker list (a GET) and sends nothing


@pytest.mark.local_spec
@pytest.mark.parametrize("field,value", [("tracking_number", " "), ("status", "LOST"), ("carrier", " ")])
def test_preview_refuses_an_invalid_form(monkeypatch, field, value):
    web, fake = _web(monkeypatch)
    assert web.post(f"/fulfilment/{CAP}/preview", data={**FORM, field: value}).status_code == 400
    assert fake.calls == []


@pytest.mark.local_spec
def test_register_with_the_right_sha256_registers_reads_back_checks_and_stores_the_file(monkeypatch):
    web, fake = _web(monkeypatch)
    sha, _html = _sha_of_preview(web)
    r = web.post(f"/fulfilment/{CAP}/register", data=_register_data(sha))
    assert r.status_code == 200 and 'data-status="registered"' in r.text
    assert 'data-state="eligibility">01<' in r.text
    assert 'data-state="address-comparison"' in r.text and "Matches" in r.text
    assert "PayPal&#39;s evidence request" in r.text or "PayPal's evidence request" in r.text
    assert [c[2] for c in fake.posts()] == ["fulfilment_tracker_post"] and len(fake.posts()) == 1
    row = store.get_file(CAP)
    assert row["status"] == "registered" and row["request_sha256"] == sha == row["approval_sha256"]
    assert row["file_sha256"] == store.recompute_file_sha256(row)
    audit = [a for a in repo.list_audit(config.db_path(), CAP) if a["action"] == "fulfilment_registered"]
    assert len(audit) == 1 and audit[0]["detail"]["request_sha256"] == sha
    assert web.get("/fulfilment").text.count(f'data-file="{CAP}"') == 1


@pytest.mark.local_spec
def test_register_with_a_stale_sha256_is_409_and_nothing_reaches_the_client(monkeypatch):
    web, fake = _web(monkeypatch)
    sha, _html = _sha_of_preview(web)
    other_sha = "0" * 64                         # the approved fingerprint is not the stored check's
    r = web.post(f"/fulfilment/{CAP}/register", data=_register_data(sha, request_sha256=other_sha))
    assert r.status_code == 409 and "not what would be sent" in r.text
    assert not fake.posts() and store.get_file(CAP) is None


@pytest.mark.local_spec
@pytest.mark.parametrize("sha", ["", "0" * 64, "garbage"])
def test_register_with_a_missing_or_wrong_sha256_is_409(monkeypatch, sha):
    web, fake = _web(monkeypatch)
    real, _html = _sha_of_preview(web)
    assert web.post(f"/fulfilment/{CAP}/register", data=_register_data(real, request_sha256=sha)).status_code == 409
    assert not fake.posts() and store.get_file(CAP) is None


@pytest.mark.local_spec
def test_missing_or_wrong_csrf_gives_the_apps_usual_refusal_and_no_call(monkeypatch):
    web, fake = _web(monkeypatch)
    sha, _html = _sha_of_preview(web)
    no_token = {k: v for k, v in FORM.items() if k != "csrf_token"}
    for form in (no_token, {**FORM, "csrf_token": "forged"}):
        confirm = {k: v for k, v in _register_data(sha).items() if k != "csrf_token"}
        if "csrf_token" in form:
            confirm["csrf_token"] = form["csrf_token"]
        r = web.post(f"/fulfilment/{CAP}/register", data=confirm)
        assert r.status_code == 403 and "session expired" in r.text
        assert web.post(f"/fulfilment/{CAP}/preview", data=form).status_code == 403
    assert not fake.posts() and store.get_file(CAP) is None


@pytest.mark.local_spec
def test_a_refused_post_is_stored_as_failed_with_the_limitation_and_no_read_back(monkeypatch):
    fake = FakeClient(post_raises=PayPalError(422, "ISSUE", "dbg-9", {}))
    web, _ = _web(monkeypatch, fake)
    sha, _html = _sha_of_preview(web)
    gets_at_preview = len([c for c in fake.calls if c[2] == "fulfilment_tracker_get"])   # the preview's own read
    r = web.post(f"/fulfilment/{CAP}/register", data=_register_data(sha))
    assert r.status_code == 200 and 'data-status="failed"' in r.text and "dbg-9" not in r.text
    row = store.get_file(CAP)
    assert row["status"] == "failed" and row["limitation_text"] == "PayPal could not register the tracking."
    assert len([c for c in fake.calls if c[2] == "fulfilment_tracker_get"]) == gets_at_preview   # no read-back after


@pytest.mark.local_spec
def test_an_unlisted_tracker_is_stored_as_registered_unconfirmed(monkeypatch):
    web, _ = _web(monkeypatch, FakeClient(list_after_post=False))
    sha, _html = _sha_of_preview(web)
    r = web.post(f"/fulfilment/{CAP}/register", data=_register_data(sha))
    assert 'data-status="registered_unconfirmed"' in r.text
    assert store.get_file(CAP)["status"] == "registered_unconfirmed"


# ---- the second flag: the fallback of the kill criterion -------------------------------------------------------
@pytest.mark.local_spec
def test_register_flag_off_saves_the_file_without_any_post(monkeypatch):
    web, fake = _web(monkeypatch, register=None)
    sha, html = _sha_of_preview(web)
    assert 'data-state="register-off"' in html
    r = web.post(f"/fulfilment/{CAP}/register", data=_register_data(sha))
    assert r.status_code == 200 and 'data-status="saved_without_registration"' in r.text
    assert fake.posts() == []
    assert store.get_file(CAP)["status"] == "saved_without_registration"


@pytest.mark.local_spec
def test_the_feature_has_only_the_expected_write_routes(monkeypatch):
    web, _fake = _web(monkeypatch)
    for verb in ("put", "patch", "delete"):
        assert getattr(web, verb)(f"/fulfilment/{CAP}/register").status_code == 405, verb
    assert web.post("/fulfilment").status_code == 405
    assert web.post(f"/fulfilment/{CAP}").status_code == 405


# Explicit mode contract; local specifications keep their original business assertions.
pytestmark = [pytest.mark.usefixtures("web_mode")]


def test_cd_write_configuration_keeps_access_csrf_and_approval_hash(web_mode, monkeypatch):
    from copilot import config
    fake = FakeClient()
    web, observed = _web(monkeypatch, fake)
    assert web.app is not None and observed is fake and fake.calls == []
    target = f"/fulfilment/{CAP}/register"
    if config.hosted_demo():
        naked = TestClient(web.app, base_url="https://localhost")
        assert naked.post(target, data=FORM).status_code == 401
        assert fake.calls == []
    assert web.post(target, data={}).status_code == 403
    assert fake.calls == []
    set_test_csrf(web, TOKEN)
    assert web.post(target, data={**FORM, "request_sha256": "0" * 64}).status_code == 409
    assert fake.calls == []
    preview = web.post(f"/fulfilment/{CAP}/preview", data=FORM)
    assert preview.status_code == 200 and "Add tracking in PayPal" in preview.text
    assert not fake.posts()


def test_safe_read_contract_with_write_subflag_off_in_both_modes(web_mode, monkeypatch):
    web, fake = _web(monkeypatch, register=None)
    r = web.get("/fulfilment")
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

_FROZEN_FEATURE_DEPENDENCIES = {'sale_readiness'}      # lazy import behind the sale_readiness flag (UX v3, declared)


def _assert_feature_dependencies(directory):
    assert _feature_dependencies(directory, 'fulfilment') == _FROZEN_FEATURE_DEPENDENCIES


def test_exact_feature_dependencies_by_source_ast():
    from pathlib import Path
    directory = Path(__file__).resolve().parents[3] / "copilot" / "features" / 'fulfilment'
    _assert_feature_dependencies(directory)


def test_dependency_graph_rejects_planted_absolute_and_relative_imports(tmp_path):
    from pathlib import Path
    import pytest
    source = Path(__file__).resolve().parents[3] / "copilot" / "features" / 'fulfilment'
    for form, statement in (("absolute", "import copilot.features.triage.service"),
                            ("relative", "from ..triage import service")):
        copy = tmp_path / form
        for path in source.rglob("*.py"):
            target = copy / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(path.read_text(encoding="utf-8-sig"), encoding="utf-8")
        (copy / "planted.py").write_text(statement + "\n", encoding="utf-8")
        assert _feature_dependencies(copy, 'fulfilment') == _FROZEN_FEATURE_DEPENDENCIES | {"triage"}
        with pytest.raises(AssertionError):
            _assert_feature_dependencies(copy)


@pytest.mark.parametrize("register", ["true", None])
def test_dr1b_r30_tracking_preview_groups_exact_request_with_readable_summary(web_mode, monkeypatch, register):
    import json
    from _dom import parse
    from copilot.features.fulfilment.tracker_request import body_from_form, request_sha256
    web, fake = _web(monkeypatch, register=register)
    sha, page = _sha_of_preview(web)
    dom = parse(page)
    body = dom.find("pre", data_state="request-body")
    assert body is not None and body.inside("details")
    details = body.parent
    while details.tag != "details":
        details = details.parent
    assert details.find("summary").text() == "Technical details"
    expected = body_from_form(CAP, FORM["tracking_number"], FORM["carrier"], FORM["status"])
    assert json.loads(body.text()) == expected
    assert sha == request_sha256(expected)
    assert CAP in details.text() and CAP not in dom.find("h1").text()
    summary = dom.find("dl", data_state="tracking-summary")
    assert summary is not None and not summary.inside("details")
    # The readable summary shows the carrier as sent ("FedEx (PayPal code FEDEX)"), not the typed text
    assert all(value in summary.text() for value in [FORM["tracking_number"], "FedEx (PayPal code FEDEX)", "Shipped"])
    # the confirm form carries only the csrf token, the check token and the fingerprint: no tracking details
    form = dom.find("form", data_state="confirm-form")
    assert sorted(n.attrs["name"] for n in form.find_all("input")) == ["csrf_token", "request_sha256", "token"]
    assert dom.find("input", name="request_sha256").attrs["value"] == sha
    assert not fake.posts()


@pytest.mark.parametrize("country,total,currency,signature", [
    ("MX", 1000001, "MXN", "needed for this sale"),
    ("MX", 999999, "MXN", "not needed for this amount"),
    ("MX", 1000000, "MXN", "not needed for this amount"),
    ("MX", None, "MXN", "cannot tell: amount or currency not available"),
    ("US", 99999999, "USD", "not required by the US page"),
    ("MX", 1000001, "XYZ", "cannot tell: no limit is listed for this currency"),
])
def test_rc4_sale_policy_uses_rule_lines_and_integer_signature_limit(web_mode, monkeypatch, country, total, currency, signature):
    import copy
    from html import unescape
    from copilot.core.checklist import load_rules
    monkeypatch.setenv("COPILOT_RULES_COUNTRY", country)
    monkeypatch.setattr(config, "RULES_COUNTRY", country)
    class SaleFake(FakeClient):
        def call(self, method, path, op, **kwargs):
            result = super().call(method, path, op, **kwargs)
            if path.startswith("/v1/reporting/transactions"):
                result = copy.deepcopy(result)
                info = result["transaction_details"][0]["transaction_info"]
                if total is None:
                    info.pop("transaction_amount")
                else:
                    info["transaction_amount"] = {"currency_code": currency,
                                                  "value": f"{total // 100}.{total % 100:02d}"}
            return result
    web, fake = _web(monkeypatch, fake=SaleFake())
    response = web.get(f"/fulfilment/{CAP}")
    assert response.status_code == 200
    match = re.search(r'<section[^>]*data-state="shipment-policy"[^>]*>(.*?)</section>', response.text, re.S)
    assert match, "The sale form must show the selected Seller Protection rule set"
    text = unescape(re.sub(r"<span lang=.es.>|</span>", "", match.group(1)))      # Spanish phrases sit in lang=es spans
    rules = load_rules(country=country)
    assert "What PayPal requires for this sale" in text
    assert rules.source_title in text and rules.source_url in text
    assert signature in text
    assert "only PayPal decides" in text
    assert "does not decide whether protection applies to this sale" in text
    for row in rules.checks.values():
        if row["basis"] == "published_policy":
            assert row["label"] in text and row["citation"] in text
        else:
            assert row["label"] not in text
    if country == "US":
        assert "PayPal also requires (Proofie does not check this)" in text
        for line in rules.raw["basic_requirements_not_checked_by_app"]:
            assert line in text
    assert len(fake.calls) == (3 if total is None else 2) and all(row[0] == "GET" for row in fake.calls)      # v1.1.2 O: with no amount from the report, the sale itself is asked once (a read-only GET)


def test_rc4_sale_policy_remains_on_validation_error_without_another_read(web_mode, monkeypatch):
    monkeypatch.setenv("COPILOT_RULES_COUNTRY", "US")
    monkeypatch.setattr(config, "RULES_COUNTRY", "US")
    web, fake = _web(monkeypatch)
    response = web.post(f"/fulfilment/{CAP}/preview", data={**FORM, "carrier": ""})
    assert response.status_code == 400
    assert 'data-state="shipment-policy"' in response.text
    assert "not required by the US page" in response.text
    assert FORM["tracking_number"] in response.text
    assert fake.calls == []


def test_rc4_rule_set_without_published_lines_is_explicit(web_mode, monkeypatch):
    from dataclasses import replace
    from copilot.core import checklist
    rules = replace(checklist.load_rules(country="MX"), checks={})
    monkeypatch.setattr(checklist, "load_rules", lambda **kwargs: rules)
    web, fake = _web(monkeypatch)
    response = web.get(f"/fulfilment/{CAP}")
    assert response.status_code == 200
    assert 'data-state="shipment-policy"' in response.text
    assert rules.source_title in response.text
    assert "This rule set lists no requirement lines for this step." in response.text
    assert not fake.posts()
