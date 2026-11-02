"""D-101 P1: the whole appeal loop on a stateful fake of PayPal's sandbox life cycle (no real call):
UNDER_REVIEW -> POST adjudicate BUYER_FAVOR -> RESOLVED with an `appeal` link -> approval -> one POST /appeal -> GET read-back.
The fake changes state only because a POST happened, like the sandbox; the app reads every state back by GET."""

import pytest

from copilot.features.outcome import store
from outcome_fakes import BASE, DISPUTE_ID, FakePayPal, dispute_raw
from test_outcome_routes import PAGE, _package, _preview_sha, _reset_cookie, _web, TOKEN

pytestmark = pytest.mark.usefixtures("legacy_approval_scenario")

UNDER_REVIEW = dict(status="UNDER_REVIEW", rels=("adjudicate", "require_evidence"))
LOST = dict(status="RESOLVED", rels=("appeal",), outcome={"outcome_code": "RESOLVED_BUYER_FAVOUR"})
APPEALED = dict(status="WAITING_FOR_SELLER_RESPONSE", stage="ARBITRATION", rels=(),
                outcome={"outcome_code": "RESOLVED_BUYER_FAVOUR"})


class LifeCycle(FakePayPal):
    """UNDER_REVIEW until POST /adjudicate; RESOLVED (appeal link, or none when `offer_appeal` is false) until POST /appeal."""

    def __init__(self, offer_appeal=True):
        super().__init__(dispute_raw(**UNDER_REVIEW))
        self.offer_appeal = offer_appeal

    def call(self, method, path, op, **kw):
        if method == "POST" and path.endswith("/adjudicate"):
            self.calls.append((method, path, op, kw))
            assert self.raw["status"] == "UNDER_REVIEW"             # the sandbox allows it only then
            self.raw = dispute_raw(**{**LOST, **({} if self.offer_appeal else {"rels": ()})})
            return {}
        if method == "POST" and path.endswith("/appeal"):
            self.calls.append((method, path, op, kw))
            self.raw = dispute_raw(**APPEALED)
            return {}
        return super().call(method, path, op, **kw)

    def adjudicate(self):
        return self.call("POST", f"{BASE}/adjudicate", "adjudicate", json={"adjudication_outcome": "BUYER_FAVOR"})


def _paths(fake, method):
    return [c[1] for c in fake.calls if c[0] == method]


def test_before_the_loss_the_page_has_no_appeal_form_and_the_register_route_posts_nothing(monkeypatch):
    pkg = _package()
    web, fake = _web(monkeypatch, LifeCycle())
    page = web.get(PAGE).text
    _reset_cookie(web)                                               # a page load rotates the CSRF cookie
    assert 'data-state="appeal-form"' not in page and 'data-state="appeal-unavailable"' in page
    assert web.post(PAGE + "/appeal/preview", data={"csrf_token": TOKEN, "package_id": str(pkg)}).status_code == 409
    assert _paths(fake, "POST") == []


def test_the_loop_from_the_loss_to_the_recorded_appeal_posts_one_appeal_and_reads_it_back(monkeypatch):
    pkg = _package()
    web, fake = _web(monkeypatch, LifeCycle())
    fake.adjudicate()                                                # the sandbox settles against us
    page = web.get(PAGE).text
    _reset_cookie(web)                                               # a page load rotates the CSRF cookie
    assert "RESOLVED_BUYER_FAVOUR" in page or "Decided" in page
    assert 'data-state="appeal-form"' in page and "Appeal window" in page
    sha, preview = _preview_sha(web, pkg)
    assert _paths(fake, "POST") == [f"{BASE}/adjudicate"]            # preview sent nothing
    result = web.post(PAGE + "/appeal/register", data={"csrf_token": TOKEN, "package_id": str(pkg), "approval_sha256": sha})
    assert result.status_code == 200 and 'data-status="sent_confirmed"' in result.text
    assert _paths(fake, "POST") == [f"{BASE}/adjudicate", f"{BASE}/appeal"]
    assert fake.calls[-1][0] == "GET"                                # the appeal was read back by GET
    rows = store.list_appeals(DISPUTE_ID)
    assert len(rows) == 1 and rows[0]["status"] == "sent_confirmed" and rows[0]["approval_sha256"] == sha
    assert 'data-state="appeal-history"' in result.text


def test_a_second_submit_of_the_same_approval_sends_no_second_appeal(monkeypatch):
    pkg = _package()
    web, fake = _web(monkeypatch, LifeCycle())
    fake.adjudicate()
    sha, _ = _preview_sha(web, pkg)
    form = {"csrf_token": TOKEN, "package_id": str(pkg), "approval_sha256": sha}
    assert web.post(PAGE + "/appeal/register", data=form).status_code == 200
    _reset_cookie(web)
    fake.raw = dispute_raw(**LOST)                                   # even if PayPal offered the link again
    web.post(PAGE + "/appeal/register", data=form)
    assert _paths(fake, "POST").count(f"{BASE}/appeal") == 1


def test_when_the_sandbox_offers_no_appeal_after_the_loss_nothing_is_prepared_or_sent(monkeypatch):
    pkg = _package()
    web, fake = _web(monkeypatch, LifeCycle(offer_appeal=False))
    fake.adjudicate()
    page = web.get(PAGE).text
    _reset_cookie(web)                                               # a page load rotates the CSRF cookie
    assert 'data-state="appeal-form"' not in page and 'data-state="appeal-unavailable"' in page
    assert web.post(PAGE + "/appeal/preview", data={"csrf_token": TOKEN, "package_id": str(pkg)}).status_code == 409
    assert web.post(PAGE + "/appeal/register", data={"csrf_token": TOKEN, "package_id": str(pkg), "approval_sha256": "0" * 64}).status_code in (409,)
    assert _paths(fake, "POST") == [f"{BASE}/adjudicate"] and store.list_appeals(DISPUTE_ID) == []


def test_an_approval_for_other_evidence_is_refused_and_posts_nothing(monkeypatch):
    pkg = _package()
    web, fake = _web(monkeypatch, LifeCycle())
    fake.adjudicate()
    sha, _ = _preview_sha(web, pkg)
    wrong = ("0" if sha[0] != "0" else "1") + sha[1:]
    r = web.post(PAGE + "/appeal/register", data={"csrf_token": TOKEN, "package_id": str(pkg), "approval_sha256": wrong})
    assert r.status_code == 409 and _paths(fake, "POST") == [f"{BASE}/adjudicate"]


def test_the_real_sandbox_loss_without_an_appeal_link_says_so_and_offers_no_appeal(monkeypatch):
    """HZY after our adjudicate BUYER_FAVOR on 2026-11-01: RESOLVED / RESOLVED_BUYER_FAVOUR with links = [self] only,
    recorded from PayPal (logs-RC44/D101/p1/evidence/hzy_resolved_get.jsonl:1). Only the dispute id is replaced."""
    import json
    from pathlib import Path
    raw = json.loads((Path(__file__).parent / "fixtures" / "hzy_resolved_buyer_favour_2026-11-01.json").read_text(encoding="utf-8"))
    assert [l["rel"] for l in raw["links"]] == ["self"]
    raw["dispute_id"] = DISPUTE_ID
    pkg = _package()
    web, fake = _web(monkeypatch, FakePayPal(raw))
    page = web.get(PAGE).text
    _reset_cookie(web)
    assert "RESOLVED_BUYER_FAVOUR" in page or "Decided" in page
    assert "PayPal does not offer an appeal now." in page and 'data-state="appeal-form"' not in page
    assert web.post(PAGE + "/appeal/preview", data={"csrf_token": TOKEN, "package_id": str(pkg)}).status_code == 409
    assert fake.posts() == [] and store.list_appeals(DISPUTE_ID) == []
