"""/disputes/<id>/outcome on the fake PayPal and a temp database: flag off, flag on (appeal off), both flags on."""
from _hosted import authenticated_client, reset_csrf
import hashlib
import html as htmllib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from copilot import config
from copilot.app import create_app
from copilot.core.raw_storage import write_raw
from copilot.db import repo
from copilot.features.outcome import appeal, routes, store
from copilot.web.dispute_state import REVIEWING_NEUTRAL_TEXT
from copilot.web.routes_disputes import get_client
from outcome_fakes import BASE, DECIDED, DISPUTE_ID, FakePayPal, dispute_raw, paypal_error

FLAG, APPEAL = "COPILOT_FEATURE_OUTCOME", "COPILOT_FEATURE_OUTCOME_APPEAL"
ROOT = Path(__file__).resolve().parents[3]
TOKEN = "test-csrf-token"
PAGE = f"/disputes/{DISPUTE_ID}/outcome"


def test_ui_copy_a4_appeal_off_has_no_write_form_or_history(monkeypatch):
    web, fake = _web(monkeypatch, appeal=None)
    page = web.get(PAGE)
    assert page.status_code == 200
    assert "The appeal call is switched off in this installation" in page.text
    assert not re.search(r'<form[^>]*action="[^\"]*/appeal/', page.text)
    assert 'data-state="appeal-history"' not in page.text
    assert not fake.posts()


def _web(monkeypatch, fake=None, flag="true", appeal="true"):
    for name, value in ((FLAG, flag), (APPEAL, appeal)):
        monkeypatch.delenv(name, raising=False) if value is None else monkeypatch.setenv(name, value)
    fake = fake or FakePayPal()
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    web = authenticated_client(app, client=("127.0.0.1", 12345))
    _reset_cookie(web)
    return web, fake


def _reset_cookie(web):
    reset_csrf(web)
    set_test_csrf(web, TOKEN)


def _package(with_tracking=True):
    path = config.db_path()
    data = b"%PDF-1.4 demo letter"
    sha = hashlib.sha256(data).hexdigest()
    write_raw(config.uploads_dir(), sha, data)
    doc = repo.insert_document(path, DISPUTE_ID, "letter.pdf", "application/pdf", len(data), sha, "text", True)
    pkg = repo.insert_package(path, DISPUTE_ID, source="human_edit", validation_ok=True, review_flags=[])
    repo.insert_claims(path, pkg, [{"evidence_index": 0, "claim_index": 0, "text": "Delivered 2026-10-02",
                                    "source_ids": [], "basis": "seller_statement"}])
    repo.attach_document(path, pkg, doc)
    if with_tracking:
        repo.set_manual_fields(path, DISPUTE_ID, {"tracking_number": {"value": "DEMO-FDX-000001", "confidence": "read"},
                                                  "carrier": {"value": "FedEx", "confidence": "read"}})
    return pkg


def _preview_sha(web, pkg):
    r = web.post(PAGE + "/appeal/preview", data={"csrf_token": TOKEN, "package_id": str(pkg)})
    _reset_cookie(web)
    assert r.status_code == 200, r.text[:300]
    return re.search(r'data-state="request-sha256">([0-9a-f]{64})<', r.text).group(1), r.text


# ---- flag off (the default) ------------------------------------------------------------------------------------
@pytest.mark.local_spec
def test_flag_off_the_route_is_404_and_nothing_is_called(monkeypatch):
    web, fake = _web(monkeypatch, flag=None, appeal=None)
    assert web.get(PAGE).status_code == 404
    assert web.post(PAGE + "/appeal/preview", data={"csrf_token": TOKEN}).status_code == 404
    assert web.post(PAGE + "/appeal/register", data={"csrf_token": TOKEN}).status_code == 404
    assert not [p for p in web.get("/openapi.json").json()["paths"] if "outcome" in p]
    assert fake.calls == []


@pytest.mark.local_spec
@pytest.mark.parametrize("value", ["1", "yes", "false", ""])
def test_flag_off_for_any_value_other_than_true(monkeypatch, value):
    web, _fake = _web(monkeypatch, flag=value)
    assert web.get(PAGE).status_code == 404


@pytest.mark.local_spec
def test_the_appeal_flag_alone_does_not_turn_the_feature_on(monkeypatch):
    web, _fake = _web(monkeypatch, flag=None, appeal="true")
    assert web.get(PAGE).status_code == 404


def test_create_app_with_the_flag_off_loads_no_outcome_module():
    code = ("import sys\nfrom copilot.app import create_app\ncreate_app()\n"
            "print(sorted(m for m in sys.modules if m.startswith('copilot.features.outcome')))")
    env = {k: v for k, v in os.environ.items() if not k.startswith("COPILOT_FEATURE_")}
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


# ---- flag on, appeal flag off ----------------------------------------------------------------------------------
@pytest.mark.local_spec
def test_page_renders_the_three_parts_for_an_undecided_dispute_with_the_appeal_off(monkeypatch):
    web, fake = _web(monkeypatch, FakePayPal(dispute_raw()), appeal=None)
    r = web.get(PAGE)
    assert r.status_code == 200
    assert 'data-state="outcome-sentence" data-decided="no"' in r.text and "PayPal has not decided yet." in r.text
    assert 'data-state="sent-file"' in r.text and 'data-state="gaps"' in r.text
    assert 'data-state="appeal-off"' in r.text and 'data-state="appeal-form"' not in r.text
    assert all(c[0] == "GET" for c in fake.calls)


@pytest.mark.local_spec
def test_page_for_a_decided_dispute_states_the_outcome_as_paypal_does(monkeypatch):
    web, _fake = _web(monkeypatch, FakePayPal(dispute_raw(**DECIDED)), appeal=None)
    r = web.get(PAGE).text
    assert "Decided: Resolved in the buyer&#39;s favor as stated by PayPal" in r or \
        "Decided: Resolved in the buyer's favor as stated by PayPal" in r
    assert "RESOLVED_BUYER_FAVOUR" in r and "10.50" in r and "Appeal window: PayPal shows an appeal link in this dispute" in r
    for forbidden in ("likely", "chance", "probab", "win rate", "odds"):
        assert forbidden not in r.lower()


@pytest.mark.local_spec
def test_the_page_shows_what_was_sent_what_is_missing_and_what_can_be_added(monkeypatch):
    pkg = _package()
    web, _fake = _web(monkeypatch, FakePayPal(dispute_raw(**DECIDED)), appeal=None)
    r = web.get(PAGE).text
    assert f'data-package="{pkg}"' in r and "Delivered 2026-10-02" in r and "letter.pdf" in r
    assert 'data-state="missing-now"' in r and 'data-state="addable"' in r
    assert f'href="/disputes/{DISPUTE_ID}/resolution"' in r and 'href="/fulfilment/5S647034CC1979815"' in r
    assert 'data-state="at-send-note"><strong>When it was sent:</strong> Proofie did not record the checks at send time.' in r


@pytest.mark.local_spec
def test_a_bad_dispute_id_is_400_before_any_call(monkeypatch):
    web, fake = _web(monkeypatch)
    assert web.get("/disputes/not-an-id/outcome").status_code == 400
    assert web.post("/disputes/not-an-id/outcome/appeal/preview", data={"csrf_token": TOKEN}).status_code == 400
    assert fake.calls == []


@pytest.mark.local_spec
def test_with_the_appeal_flag_off_both_appeal_routes_answer_403_and_no_post_exists(monkeypatch):
    pkg = _package()
    web, fake = _web(monkeypatch, appeal=None)
    assert web.post(PAGE + "/appeal/preview", data={"csrf_token": TOKEN, "package_id": str(pkg)}).status_code == 403
    assert web.post(PAGE + "/appeal/register", data={"csrf_token": TOKEN, "package_id": str(pkg),
                                                     "approval_sha256": "0" * 64}).status_code == 403
    assert fake.calls == [] and store.list_appeals(DISPUTE_ID) == []


# ---- both flags on ---------------------------------------------------------------------------------------------
@pytest.mark.local_spec
def test_with_both_flags_the_page_offers_the_appeal_form_only_when_the_link_exists(monkeypatch):
    _package()
    web, _ = _web(monkeypatch, FakePayPal(dispute_raw(**DECIDED)))
    assert 'data-state="appeal-form"' in web.get(PAGE).text
    web, _ = _web(monkeypatch, FakePayPal(dispute_raw()))
    assert 'data-state="appeal-form"' not in web.get(PAGE).text and 'data-state="appeal-unavailable"' in web.get(PAGE).text


@pytest.mark.local_spec
def test_the_appeal_form_carries_novalidate_like_the_other_forms(monkeypatch):
    _package()
    web, _ = _web(monkeypatch, FakePayPal(dispute_raw(**DECIDED)))
    page = web.get(PAGE).text
    form = re.search(r'<form[^>]*data-state="appeal-form"[^>]*>', page)
    assert form is not None and re.search(r"novalidate", form.group(0))
    assert '<select id="appeal-package" name="package_id" required>' in page


@pytest.mark.local_spec
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_preview_shows_the_body_the_hash_and_the_attachments_and_posts_nothing(monkeypatch):
    pkg = _package()
    web, fake = _web(monkeypatch, FakePayPal(dispute_raw(**DECIDED)))
    sha, html = _preview_sha(web, pkg)
    assert "DEMO-FDX-000001" in html and "letter.pdf" in html and 'name="approval_sha256" value="' + sha + '"' in html
    assert not fake.posts()


@pytest.mark.local_spec
def test_preview_without_the_link_is_409(monkeypatch):
    pkg = _package()
    web, fake = _web(monkeypatch, FakePayPal(dispute_raw(status="RESOLVED", rels=())))
    assert web.post(PAGE + "/appeal/preview", data={"csrf_token": TOKEN, "package_id": str(pkg)}).status_code == 409
    assert not fake.posts()


@pytest.mark.local_spec
@pytest.mark.parametrize("package_id", ["", "abc", "999"])
def test_preview_with_a_bad_package_is_400(monkeypatch, package_id):
    _package()
    web, fake = _web(monkeypatch, FakePayPal(dispute_raw(**DECIDED)))
    assert web.post(PAGE + "/appeal/preview", data={"csrf_token": TOKEN, "package_id": package_id}).status_code == 400
    assert not fake.posts()


@pytest.mark.local_spec
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_register_with_the_right_hash_follows_the_pattern_one_post_one_get_stored_and_audited(monkeypatch):
    pkg = _package()
    after = dispute_raw(status="WAITING_FOR_SELLER_RESPONSE", stage="ARBITRATION", rels=(),
                        outcome={"outcome_code": "RESOLVED_BUYER_FAVOUR"})
    web, fake = _web(monkeypatch, FakePayPal(dispute_raw(**DECIDED), after=after))
    sha, _html = _preview_sha(web, pkg)
    r = web.post(PAGE + "/appeal/register", data={"csrf_token": TOKEN, "package_id": str(pkg), "approval_sha256": sha})
    assert r.status_code == 200 and 'data-status="sent_confirmed"' in r.text
    assert [c[1] for c in fake.posts()] == [f"{BASE}/appeal"]
    rows = store.list_appeals(DISPUTE_ID)
    assert len(rows) == 1 and rows[0]["status"] == "sent_confirmed" and rows[0]["approval_sha256"] == sha
    assert rows[0]["request_sha256"] == appeal.request_sha256(json.loads(rows[0]["request_json"])) != sha     # body only
    assert len([a for a in repo.list_audit(config.db_path(), DISPUTE_ID) if a["action"] == "outcome_appeal"]) == 1
    assert 'data-state="appeal-history"' in r.text


@pytest.mark.local_spec
def test_register_with_a_wrong_hash_is_409_and_nothing_is_called(monkeypatch):
    pkg = _package()
    web, fake = _web(monkeypatch, FakePayPal(dispute_raw(**DECIDED)))
    before = len(fake.calls)
    for sha in ("", "0" * 64, "garbage"):
        r = web.post(PAGE + "/appeal/register", data={"csrf_token": TOKEN, "package_id": str(pkg), "approval_sha256": sha})
        assert r.status_code == 409 and "not what would be sent" in r.text
        _reset_cookie(web)
    assert len(fake.calls) == before and not fake.posts() and store.list_appeals(DISPUTE_ID) == []


@pytest.mark.local_spec
def test_register_after_the_link_disappeared_is_409_and_nothing_is_posted(monkeypatch):
    pkg = _package()
    web, fake = _web(monkeypatch, FakePayPal(dispute_raw(**DECIDED)))
    sha, _html = _preview_sha(web, pkg)
    fake.raw = dispute_raw(status="RESOLVED", rels=())
    r = web.post(PAGE + "/appeal/register", data={"csrf_token": TOKEN, "package_id": str(pkg), "approval_sha256": sha})
    assert r.status_code == 409 and not fake.posts() and store.list_appeals(DISPUTE_ID) == []


@pytest.mark.local_spec
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_a_refused_appeal_is_stored_as_failed_and_not_retried(monkeypatch):
    pkg = _package()
    web, fake = _web(monkeypatch, FakePayPal(dispute_raw(**DECIDED), post_raises=paypal_error(422)))
    sha, _html = _preview_sha(web, pkg)
    r = web.post(PAGE + "/appeal/register", data={"csrf_token": TOKEN, "package_id": str(pkg), "approval_sha256": sha})
    assert r.status_code == 200 and 'data-status="failed"' in r.text and "dbg-1" not in r.text
    assert len(fake.posts()) == 1 and store.list_appeals(DISPUTE_ID)[0]["status"] == "failed"


@pytest.mark.local_spec
def test_missing_or_wrong_csrf_gives_the_apps_usual_refusal_and_no_call(monkeypatch):
    pkg = _package()
    web, fake = _web(monkeypatch, FakePayPal(dispute_raw(**DECIDED)))
    for csrf in ("", "forged"):
        assert web.post(PAGE + "/appeal/preview", data={"csrf_token": csrf, "package_id": str(pkg)}).status_code == 403
        assert web.post(PAGE + "/appeal/register", data={"csrf_token": csrf, "package_id": str(pkg),
                                                         "approval_sha256": "0" * 64}).status_code == 403
    assert fake.calls == [] and not fake.posts()


# ---- guarantees about the code itself --------------------------------------------------------------------------
def test_exactly_three_routes_and_none_of_the_forbidden_paths_anywhere_in_the_feature():
    feature = ROOT / "copilot" / "features" / "outcome"
    paths = sorted(f"{m} {r.path}" for r in routes.router.routes for m in r.methods if m != "HEAD")
    assert paths == ["GET /disputes/{dispute_id}/outcome", "POST /disputes/{dispute_id}/outcome/appeal/preview",
                     "POST /disputes/{dispute_id}/outcome/appeal/register"]
    source = "\n".join(p.read_text(encoding="utf-8") for p in sorted(feature.glob("*.py")))
    templates = "\n".join(p.read_text(encoding="utf-8") for p in sorted((ROOT / "copilot" / "web" / "templates" / "features" / "outcome").glob("*.html")))
    for forbidden in ("/escalate", "/provide-evidence", "/send-message", "/make-offer", "/accept-claim"):
        assert forbidden not in source and forbidden not in templates, forbidden


def test_the_only_post_path_in_the_feature_is_the_appeal_link_and_it_is_built_from_the_dispute_id_check():
    source = (ROOT / "copilot" / "features" / "outcome" / "appeal.py").read_text(encoding="utf-8")
    assert source.count('client.call("POST"') == 1 and "/appeal" in source
    for name in ("read.py", "file.py", "gaps.py", "service.py", "store.py"):
        assert 'call("POST"' not in (ROOT / "copilot" / "features" / "outcome" / name).read_text(encoding="utf-8"), name


@pytest.mark.local_spec
def test_preview_and_register_agree_and_a_swapped_attachment_after_the_preview_is_a_409_with_no_post(monkeypatch):
    pkg = _package()
    web, fake = _web(monkeypatch, FakePayPal(dispute_raw(**DECIDED)))
    sha, html = _preview_sha(web, pkg)
    data = b"%PDF-1.4 another letter"
    new_sha = hashlib.sha256(data).hexdigest()
    write_raw(config.uploads_dir(), new_sha, data)
    doc = repo.insert_document(config.db_path(), DISPUTE_ID, "other.pdf", "application/pdf", len(data), new_sha, "t", True)
    repo.attach_document(config.db_path(), pkg, doc)                      # the package changed after the approval
    r = web.post(PAGE + "/appeal/register", data={"csrf_token": TOKEN, "package_id": str(pkg), "approval_sha256": sha})
    assert r.status_code == 409 and not fake.posts() and store.list_appeals(DISPUTE_ID) == []
    assert "Approval fingerprint" in html


# Explicit mode contract; local specifications keep their original business assertions.
pytestmark = [pytest.mark.usefixtures("web_mode")]


def test_the_appeal_flag_no_longer_stops_the_hosted_start(web_mode, monkeypatch):
    fake = FakeClient() if "FakeClient" in globals() else FakePayPal()
    web, observed = _web(monkeypatch, fake)             # hosted or local: the app starts with the flag on, no PayPal call
    assert web.app is not None and observed is fake and fake.calls == []


def test_safe_read_contract_with_write_subflag_off_in_both_modes(web_mode, monkeypatch):
    web, fake = _web(monkeypatch, appeal=None)
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

_FROZEN_FEATURE_DEPENDENCIES = {'resolution', 'fulfilment', 'appeal_workspace'}   # appeal_workspace: lazy import behind its own flag (UX v3, declared change)


def _assert_feature_dependencies(directory):
    assert _feature_dependencies(directory, 'outcome') == _FROZEN_FEATURE_DEPENDENCIES


def test_exact_feature_dependencies_by_source_ast():
    from pathlib import Path
    directory = Path(__file__).resolve().parents[3] / "copilot" / "features" / 'outcome'
    _assert_feature_dependencies(directory)


def test_dependency_graph_rejects_planted_absolute_and_relative_imports(tmp_path):
    from pathlib import Path
    import pytest
    source = Path(__file__).resolve().parents[3] / "copilot" / "features" / 'outcome'
    for form, statement in (("absolute", "import copilot.features.triage.service"),
                            ("relative", "from ..triage import service")):
        copy = tmp_path / form
        for path in source.rglob("*.py"):
            target = copy / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(path.read_text(encoding="utf-8-sig"), encoding="utf-8")
        (copy / "planted.py").write_text(statement + "\n", encoding="utf-8")
        assert _feature_dependencies(copy, 'outcome') == _FROZEN_FEATURE_DEPENDENCIES | {"triage"}
        with pytest.raises(AssertionError):
            _assert_feature_dependencies(copy)


def test_dr1b_abandoned_history_uses_same_red_vocabulary_without_writing(web_mode, monkeypatch):
    from copilot.core.attempt_store import SQLiteAttemptStore
    from _dom import parse
    _package()
    attempts = SQLiteAttemptStore(config.db_path())
    attempts.write(DISPUTE_ID, "b" * 64, {"state": "abandoned", "request_id": "req-demo-history"})
    before = attempts.read(DISPUTE_ID, "b" * 64)
    web, fake = _web(monkeypatch, appeal=None)
    response = web.get(PAGE)
    assert response.status_code == 200
    row = parse(response.text).find("tr", data_state_value="abandoned")
    assert row is not None and row.find("span", class_="tag--bad") is not None
    assert row.find("span", class_="tag--info") is None
    assert "You marked it as not shown in PayPal" in row.text()      # the words of the package page (status.py)
    assert "abandoned" in row.find("details").text()
    assert fake.posts() == [] and attempts.read(DISPUTE_ID, "b" * 64) == before
    attempts.close()


# ---- Breadcrumbs, neutral checks after a send, money with its code ----------------
def _confirmed_attempt():
    from copilot.core.attempt_store import SQLiteAttemptStore
    sent = SQLiteAttemptStore(config.db_path())
    try:
        sent.write(DISPUTE_ID, "sha-confirmed", {"state": "confirmed", "request_id": "req-ux-1",
                                                 "request_input": {"evidences": []}})
    finally:
        sent.close()


def test_ux_u_page_frame_has_breadcrumb_title_and_no_back_link(monkeypatch):
    import _ux_lint as lint
    web, _fake = _web(monkeypatch, FakePayPal(dispute_raw(**DECIDED)), appeal=None)
    r = web.get(PAGE)
    assert lint.breadcrumb_items(r.text) == [("All disputes", "/disputes"), (f"Dispute {DISPUTE_ID}", f"/disputes/{DISPUTE_ID}"),
                                            ("What PayPal decided", None)]
    assert lint.back_links(r.text) == [] and lint.bare_buttons(r.text) == []
    assert f"<title>What PayPal decided: {DISPUTE_ID} - Proofie</title>" in r.text
    assert r.headers["cache-control"] == "no-store"


def test_ux_u_refund_amount_is_formatted_from_cents_with_the_currency_code(monkeypatch):
    web, _fake = _web(monkeypatch, FakePayPal(dispute_raw(**DECIDED)), appeal=None)
    assert "Amount refunded" in web.get(PAGE).text and "10.50 USD" in web.get(PAGE).text


def test_ux_u_after_a_confirmed_send_the_gaps_are_information_not_problems(monkeypatch):
    from _dom import parse
    _confirmed_attempt()
    _package()
    web, _fake = _web(monkeypatch, FakePayPal(dispute_raw(status="UNDER_REVIEW", stage="CLAIM", rels=())), appeal=None)
    r = web.get(PAGE)
    assert "Checked again after sending (for information)" in r.text
    gaps = parse(r.text).find("div", data_state="gaps")
    assert gaps is not None and gaps.find("span", class_="tag--bad") is None
    assert "Missing needed detail" not in gaps.text()
    assert "When it was sent: Proofie did not record the checks at send time." in gaps.text()
    assert "Who received the shipment: not found in your documents" in gaps.text()
    assert "PayPal has not decided yet." in r.text


def test_ux_u_without_a_send_the_gaps_keep_their_problem_wording(monkeypatch):
    web, _fake = _web(monkeypatch, FakePayPal(dispute_raw(**DECIDED)), appeal=None)
    r = web.get(PAGE)
    assert "What was missing, and what can be added" in r.text and "Checked again after sending" not in r.text


def test_ux_u_outcome_templates_have_no_string_replace_and_pass_the_lint():
    import _ux_lint as lint
    folder = ROOT / "copilot" / "web" / "templates" / "features" / "outcome"
    files = sorted(folder.glob("*.html"))
    assert lint.template_lint(files) == []
    assert not [f.name for f in files if "replace('" in f.read_text(encoding="utf-8")]


@pytest.mark.local_spec
def test_ux_u_appeal_controls_are_macro_buttons_and_the_writing_form_is_marked(monkeypatch):
    import _ux_lint as lint
    monkeypatch.setenv("COPILOT_FEATURE_ACTIVE_APPROVAL", "false")
    web, fake = _web(monkeypatch, FakePayPal(dispute_raw(**DECIDED)))
    pkg = _package()
    _sha, page = _preview_sha(web, pkg)
    assert lint.bare_buttons(page) == [] and len(lint.primaries(page)) == 1
    assert 'data-state="confirm-form"' in page and "data-sends" in page.split('data-state="confirm-form"')[0].rsplit("<form", 1)[1] + ">"
    assert "Fields marked Required" not in page and not fake.posts()


def test_attempt_rows_use_the_package_page_words_and_the_outcome_tables_have_captions(web_mode, monkeypatch):
    """The outcome page words an attempt as status.py does for the package page; its tables carry hidden captions and the
    refunded amount uses tabular figures."""
    from copilot.core.attempt_store import SQLiteAttemptStore
    from copilot.web import status
    from _dom import parse
    _package()
    attempts = SQLiteAttemptStore(config.db_path())
    want = {"pending": "Sending started - not confirmed yet", "sent_unconfirmed": "Send attempted - not confirmed by PayPal",
            "confirmed": "Received by PayPal (confirmed by reading the dispute back)", "failed": "PayPal did not accept it",
            "blocked": "Not sent: Proofie stopped it", "abandoned": "You marked it as not shown in PayPal"}
    for index, state in enumerate(want):
        attempts.write(DISPUTE_ID, f"{index:x}" * 64, {"state": state, "request_id": f"req-{state}"})
    attempts.close()
    web, fake = _web(monkeypatch, appeal=None)
    page = web.get(PAGE)
    assert page.status_code == 200
    dom = parse(page.text)
    for state, words in want.items():
        row = dom.find("tr", data_state_value=state)
        assert row is not None, state
        assert words in row.text() and row.find("span", class_="tag") is not None
    from copilot.web.presentation import state_label
    assert [state_label(s) for s in want] == [status.evidence_status(status.SendRecord(state=m)).label
                                              for m in ("reserved", "attempted", "confirmed", "refused", "blocked", "abandoned")]
    captions = {t.attrs.get("data-state"): t.find("caption") for t in dom.find_all("table")}
    assert captions["send-attempts"].text() == "Send attempts"
    assert captions["outcome-fields"].text() == "What PayPal recorded"
    for caption in captions.values():
        assert caption is not None and "visually-hidden" in caption.attrs["class"].split()
    refunded = next(tr for tr in dom.find("table", data_state="outcome-fields").find_all("tr")
                    if tr.find("th").text() == "Amount refunded")
    assert "num" in refunded.find("td").attrs["class"].split()
    assert fake.posts() == []


def test_empty_sections_of_the_record_say_a_sentence_each(web_mode, monkeypatch):
    from _dom import parse
    _package()
    web, fake = _web(monkeypatch, appeal=None)
    texts = [p.text() for p in parse(web.get(PAGE).text).find_all("p")]
    assert "No tracker was registered before the dispute." in texts
    assert "No offer or message was recorded for this dispute." in texts
    assert "None." not in texts and not any(t == "None" for t in texts)


@pytest.mark.parametrize("stage, words", [(None, "Not returned by PayPal"), ("SOMETHING_NEW", "Stage not recognized"),
                                          ("PRE_ARBITRATION", "Pre-arbitration"), ("ARBITRATION", "Arbitration")])
def test_the_stage_row_reads_the_stage_table(web_mode, monkeypatch, stage, words):
    from _dom import parse
    _package()
    web, fake = _web(monkeypatch, FakePayPal(dispute_raw(**{**DECIDED, "stage": stage})), appeal=None)
    page = web.get(PAGE)
    assert page.status_code == 200
    row = next(tr for tr in parse(page.text).find("table", data_state="outcome-fields").find_all("tr")
               if tr.find("th").text() == "Stage")
    assert row.find("td").text() == words


@pytest.mark.local_spec
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_every_technical_details_block_holds_only_its_summary_and_paragraphs(monkeypatch):
    """Raw keys and fingerprints sit in <p> blocks inside Technical details (audit line, send attempt, resolution action, appeal
    attempt, tracker, attachment), never as loose text in a list item or a cell."""
    from _dom import parse
    from copilot.core.attempt_store import SQLiteAttemptStore
    from copilot.features.resolution import store as resolution_store
    pkg = _package()
    attempts = SQLiteAttemptStore(config.db_path())
    attempts.write(DISPUTE_ID, "c" * 64, {"state": "sent_unconfirmed", "request_id": "req-c"})
    attempts.close()
    resolution_store.save_action(DISPUTE_ID, "message", {"message": "hello"}, "d" * 64, "e" * 64, {}, True, "sent_unconfirmed")
    after = dispute_raw(status="WAITING_FOR_SELLER_RESPONSE", stage="ARBITRATION", rels=(),
                        outcome={"outcome_code": "RESOLVED_BUYER_FAVOUR"})
    web, fake = _web(monkeypatch, FakePayPal(dispute_raw(**DECIDED), after=after))
    sha, _html = _preview_sha(web, pkg)
    r = web.post(PAGE + "/appeal/register", data={"csrf_token": TOKEN, "package_id": str(pkg), "approval_sha256": sha})
    assert r.status_code == 200 and 'data-state="appeal-history"' in r.text
    dom = parse(r.text)
    details = dom.find_all("details")
    assert len(details) >= 8                         # attachment, tracker or none, attempt, resolution, audit lines, appeal
    kinds = set()
    for block in details:
        loose = [c for c in block.children if isinstance(c, str) and c.strip()]
        assert not loose, loose
        children = [c for c in block.children if not isinstance(c, str)]
        assert children[0].tag == "summary" and children[0].text() == "Technical details"
        assert all(c.tag == "p" for c in children[1:]), [c.tag for c in children]
        kinds.add(block.parent.tag)
    assert {"li", "td"} <= kinds                     # the list items and the table cells are covered
    assert dom.find("table", data_state="appeal-history").find("details") is not None
    assert dom.find("tr", data_state_value="sent_unconfirmed").find("details") is not None
    assert "message" in " ".join(li.find("details").text() for li in dom.find("ul", data_state="resolution-actions").find_all("li"))


CASES_WITH_AN_INVITATION = ("c02_missing_carrier_proof", "c05_in_transit", "c06_illegible")


def _now_list(monkeypatch, status, rels, case_name="c02_missing_carrier_proof"):
    """The outcome page over real checklist messages: an m1 case supplies the missing checks."""
    import copy
    from copilot.features.outcome import gaps as gaps_module, service
    from copilot.fixtures.m1.loader import fake_client, load_case
    case = load_case(case_name)
    real = gaps_module.missing_then_and_now(fake_client(copy.deepcopy(case)), config.db_path(), DISPUTE_ID, case["dispute_norm"])
    assert {m["action"] for m in real.missing_now} - {"none"}
    monkeypatch.setattr(service, "missing_then_and_now", lambda *a, **kw: real)
    _package(with_tracking=False)
    web, _fake = _web(monkeypatch, FakePayPal(dispute_raw(status=status, stage="CHARGEBACK", rels=rels)), appeal=None)
    page = web.get(PAGE).text
    return real, page.split('data-state="missing-now"', 1)[1].split("</ul>", 1)[0]


@pytest.mark.parametrize("case_name", CASES_WITH_AN_INVITATION)
def test_a_claim_under_review_lists_what_is_missing_now_without_inviting_proof(monkeypatch, case_name):
    import re
    real, now = _now_list(monkeypatch, "UNDER_REVIEW", (), case_name)
    rows = now.split("<li")[1:]
    acting = [m for m in real.missing_now if m["action"] != "none"]
    assert len(rows) == len(real.missing_now) and acting
    assert now.count(REVIEWING_NEUTRAL_TEXT) == len(acting)
    text = htmllib.unescape(re.sub(r"<[^>]+>", " ", now)).lower()
    for invitation in ("add the", "upload", "type it in", "type the number", "choose the carrier", "add it", "reply",
                       "check it", "type in"):
        assert invitation not in text, invitation
    for m in real.missing_now:
        if m["action"] == "none":
            assert htmllib.unescape(m["message"]) in htmllib.unescape(now)       # an action-free row keeps its words


def test_an_ordinary_claim_keeps_the_messages_in_the_now_list(monkeypatch):
    _real, now = _now_list(monkeypatch, "WAITING_FOR_SELLER_RESPONSE", ("provide_evidence",))
    assert "not checked further" not in now
    assert "Add the carrier's proof or type the number." in htmllib.unescape(now)
    _real, now = _now_list(monkeypatch, "WAITING_FOR_SELLER_RESPONSE", ("provide_evidence",), "c05_in_transit")
    assert "You can still reply honestly with the shipping proof" in htmllib.unescape(now)


@pytest.mark.local_spec
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_submitting_or_reloading_the_register_form_twice_sends_one_post(monkeypatch):
    pkg = _package()
    web, fake = _web(monkeypatch, FakePayPal(dispute_raw(**DECIDED), post_raises=httpx.ConnectTimeout("timed out")))
    sha, _html = _preview_sha(web, pkg)
    data = {"csrf_token": TOKEN, "package_id": str(pkg), "approval_sha256": sha}
    first = web.post(PAGE + "/appeal/register", data=data)
    _reset_cookie(web)
    second = web.post(PAGE + "/appeal/register", data=data)              # the reload / second click after an unknown answer
    assert first.status_code == second.status_code == 200
    assert len(fake.posts()) == 1 and len(store.list_appeals(DISPUTE_ID)) == 1
    assert "do not send it again" in second.text
