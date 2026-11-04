"""D-101 B3: accepting the claim is the seller's last option (COPILOT_FEATURE_ACCEPT_CLAIM).
Offered only while a fresh GET carries PayPal's accept-claim link; never proposed; its own check page states the consequence; the seller
types the amount phrase and approves the exact request by hash; the send is a separate click; one POST per approval, then a read-back GET.
Synthetic fakes only, no network."""
import ast
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "resolution"))
from _hosted import authenticated_client, reset_csrf  # noqa: E402
from resolution_fakes import BASE, DISPUTE_ID, FakePayPal  # noqa: E402

from copilot import config  # noqa: E402
from copilot.app import create_app  # noqa: E402
from copilot.features.accept_claim import service as accept  # noqa: E402
from copilot.web.routes_disputes import get_client  # noqa: E402

PAGE = f"/disputes/{DISPUTE_ID}/resolution"
ACCEPT = PAGE + "/accept-claim"
TOKEN = "test-csrf-token"
PHRASE = "REFUND 40.00 USD"
CONSEQUENCE = "This refunds the buyer and closes the dispute. Proofie cannot undo it."
ROOT = Path(__file__).resolve().parents[3]


class AcceptFake(FakePayPal):
    """The fresh phase offers accept_claim; an accept-claim POST closes the case (RESOLVED, no links)."""
    def __init__(self, *a, close_on_post=True, **k):
        super().__init__(*a, **k)
        self.closed, self.close_on_post, self.bodies = False, close_on_post, []

    def _post(self, path, kw):
        if path.endswith("/accept-claim"):
            if self.post_raises:
                raise self.post_raises
            self.bodies.append(kw["json_body"])
            self.closed = self.close_on_post
            return {}
        return super()._post(path, kw)

    def _dispute(self):
        return self._raw("RESOLVED", ()) if self.closed else super()._dispute()

    def accept_posts(self):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith("/accept-claim")]


class GetOnly(AcceptFake):
    def call(self, method, path, op, **kw):
        assert method == "GET", "a dry run cannot write to PayPal"
        return super().call(method, path, op, **kw)


def _web(monkeypatch, fake, flag="true"):
    for name in ("COPILOT_FEATURE_RESOLUTION", "COPILOT_FEATURE_RESOLUTION_WRITES"):
        monkeypatch.setenv(name, "true")
    if flag is None:
        monkeypatch.delenv("COPILOT_FEATURE_ACCEPT_CLAIM", raising=False)
    else:
        monkeypatch.setenv("COPILOT_FEATURE_ACCEPT_CLAIM", flag)
    monkeypatch.delenv("COPILOT_FEATURE_ACCOUNT_CLIFF", raising=False)
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    web = authenticated_client(app, client=("127.0.0.1", 12345))
    _csrf(web)
    return web


def _csrf(web):
    reset_csrf(web)
    web.cookies.set("csrf_token", TOKEN)


def _check(web):
    """Review accepting the claim: returns (token, sha, the check page's html)."""
    r = web.post(ACCEPT + "/preview", data={"csrf_token": TOKEN}, follow_redirects=False)
    assert r.status_code == 303, r.status_code
    page = web.get(r.headers["location"]).text
    _csrf(web)
    return (re.search(r'name="token" value="([^"]*)"', page).group(1),
            re.search(r'data-state="request-sha256">([0-9a-f]{64})<', page).group(1), page)


def _send(web, token, sha, phrase=PHRASE):
    r = web.post(ACCEPT + "/register", data={"csrf_token": TOKEN, "token": token, "approval_sha256": sha, "phrase": phrase},
                 follow_redirects=False)
    _csrf(web)
    return r


# ---- offered only when PayPal offers it, last, never proposed ---------------------------------------------------------
def test_no_link_means_no_button_and_no_preview(monkeypatch):
    fake = AcceptFake(phase="after_offer")
    web = _web(monkeypatch, fake)
    page = web.get(PAGE).text
    assert "accept-claim-option" not in page and "Review accepting the claim" not in page
    _csrf(web)
    r = web.post(ACCEPT + "/preview", data={"csrf_token": TOKEN})
    assert r.status_code == 409 and not fake.accept_posts()


def test_with_the_link_the_option_is_listed_last_after_defend_and_is_not_primary(monkeypatch):
    web = _web(monkeypatch, AcceptFake())
    page = web.get(PAGE).text
    assert "accept-claim-option" in page
    assert page.index('data-state="action-defend"') < page.index('data-state="accept-claim-option"')
    assert page.index('data-state="action-offer"') < page.index('data-state="accept-claim-option"')
    block = page[page.index('data-state="accept-claim-option"'):]
    block = block[:block.index("</form>")]
    assert CONSEQUENCE in block and "btn-primary" not in block and 'class="btn primary' not in block
    assert "Proofie cannot refund for you" not in page      # the old pointer would contradict the option


def test_it_is_not_reachable_from_a_proposal_or_a_default(monkeypatch):
    """No other page or draft links to the accept routes, and the preview needs the explicit POST of its own button."""
    web = _web(monkeypatch, AcceptFake())
    for path in (PAGE, f"/disputes/{DISPUTE_ID}", "/disputes"):
        html = web.get(path).text
        if path != PAGE:
            assert "accept-claim" not in html, path
    assert web.get(ACCEPT + "/preview").status_code in (404, 405)      # a GET never starts it
    sources = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "copilot").rglob("*.py")
                        if "accept_claim" not in p.parts and p.name != "app.py")
    assert "accept-claim/preview" not in sources and "accept_claim.service" not in sources.replace("accept_claim.service import offered", "")


def test_flag_off_means_no_route_and_no_control(monkeypatch):
    fake = AcceptFake()
    web = _web(monkeypatch, fake, flag=None)
    page = web.get(PAGE).text
    assert "accept-claim-option" not in page and "Proofie cannot refund for you" in page      # the old pointer stays
    for method, path in (("post", "/preview"), ("get", "/check/x"), ("post", "/register"), ("get", "/sent/1")):
        assert getattr(web, method)(ACCEPT + path).status_code in (404, 405)
    assert not fake.accept_posts()


@pytest.mark.pool_scope
def test_the_hosted_demo_refuses_it(monkeypatch):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    monkeypatch.setenv("COPILOT_FEATURE_ACCEPT_CLAIM", "true")
    assert config.hosted_demo() and not accept.enabled()
    fake = AcceptFake()
    assert accept.prepare(fake, DISPUTE_ID).ok is False
    out = accept.register(fake, DISPUTE_ID, accept.approval_body(DISPUTE_ID, "40.00", "USD"), "0" * 64, PHRASE)
    assert out.status == "disabled" and fake.calls == []


# ---- the check page, the phrase, the hash -------------------------------------------------------------------------------
def test_the_check_page_states_the_consequence_the_amount_from_paypal_and_the_phrase(monkeypatch):
    web = _web(monkeypatch, GetOnly())
    token, sha, page = _check(web)
    assert CONSEQUENCE in page and "40.00 USD" in page and PHRASE in page and sha in page
    assert "Not sent yet" in page and 'name="phrase"' in page


def test_the_approved_request_is_sent_once_and_read_back(monkeypatch):
    fake = AcceptFake()
    web = _web(monkeypatch, fake)
    token, sha, _ = _check(web)
    assert not fake.accept_posts()      # the check page sent nothing
    r = _send(web, token, sha)
    assert r.status_code == 303 and "/accept-claim/sent/" in r.headers["location"]
    assert len(fake.accept_posts()) == 1 and fake.bodies == [{"note": accept.NOTE, "accept_claim_type": "REFUND"}]
    gets = [c for c in fake.calls if c[0] == "GET" and c[2] == accept.READBACK_OP]
    assert len(gets) >= 1      # the read-back GET
    page = web.get(r.headers["location"]).text
    assert 'data-status="sent_confirmed"' in page and "PayPal shows the claim accepted and the buyer refunded" in page


def test_a_wrong_or_empty_phrase_is_refused_and_sends_nothing(monkeypatch):
    fake = AcceptFake()
    web = _web(monkeypatch, fake)
    token, sha, _ = _check(web)
    for phrase in ("", "refund 40.00 usd", "REFUND 40.00", "REFUND 4.00 USD", PHRASE + " now"):
        r = _send(web, token, sha, phrase)
        assert r.status_code == 409 and "phrase" in r.text.lower(), phrase
    assert not fake.accept_posts()


def test_a_hash_that_is_not_the_stored_one_is_refused(monkeypatch):
    fake = AcceptFake()
    web = _web(monkeypatch, fake)
    token, sha, _ = _check(web)
    assert _send(web, token, "0" * 64).status_code == 409 and not fake.accept_posts()


def test_a_stale_page_is_refused_when_the_link_is_gone(monkeypatch):
    fake = AcceptFake()
    web = _web(monkeypatch, fake)
    token, sha, _ = _check(web)
    fake.phase = "after_offer"      # PayPal no longer offers accept-claim
    r = _send(web, token, sha)
    assert r.status_code == 409 and "does not offer" in r.text and not fake.accept_posts()


def test_a_stale_page_is_refused_when_the_amount_changed(monkeypatch):
    fake = AcceptFake()
    web = _web(monkeypatch, fake)
    token, sha, _ = _check(web)
    fake.amount = "35.00"
    r = _send(web, token, sha)
    assert r.status_code == 409 and "changed" in r.text and not fake.accept_posts()


def test_a_second_send_is_refused_and_makes_no_second_post(monkeypatch):
    fake = AcceptFake(close_on_post=False)      # PayPal still shows the link: only the app can refuse the repeat
    web = _web(monkeypatch, fake)
    token, sha, _ = _check(web)
    assert _send(web, token, sha).status_code == 303 and len(fake.accept_posts()) == 1
    again = _send(web, token, sha)
    assert again.status_code in (303, 409) and len(fake.accept_posts()) == 1
    token2, sha2, _ = _check(web)      # a new check of the same request is the same approval hash
    assert sha2 == sha
    r = _send(web, token2, sha2)
    assert len(fake.accept_posts()) == 1 and r.status_code == 303      # redirected to the first send's record


def test_a_definite_refusal_by_paypal_is_stored_and_not_retried(monkeypatch):
    from resolution_fakes import paypal_error
    fake = AcceptFake(post_raises=paypal_error(400, "INVALID_REQUEST"))
    web = _web(monkeypatch, fake)
    token, sha, _ = _check(web)
    r = _send(web, token, sha)
    assert r.status_code == 502 or r.status_code == 409
    assert len([c for c in fake.calls if c[0] == "POST"]) == 1      # one attempt, no automatic retry
    assert "Nothing" in r.text or "could not" in r.text.lower()


def test_dry_runs_write_nothing_to_paypal(monkeypatch):
    web = _web(monkeypatch, GetOnly())
    web.get(PAGE)
    _csrf(web)
    token, sha, page = _check(web)
    web.get(ACCEPT + f"/check/{token}")
    assert PHRASE in page      # GetOnly would have raised on any non-GET call


# ---- the assistant has no such tool ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["copilot/mcp_server.py", "copilot/features/mcp_local_contracts/server.py",
                                  "copilot/features/mcp_local_contracts/service.py"])
def test_the_mcp_servers_still_have_no_accept_tool(path):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    names = [n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert not [n for n in names if "accept" in n.lower()], names
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in
             ("accept_claim", "accept-claim", "accept_dispute_claim")]
    assert not calls


# ---- guard 4: the amount and the currency come from PayPal's dispute read, not from constants in code ------------------------
class OtherMoney(AcceptFake):
    def __init__(self, currency, amount, **k):
        super().__init__(amount=amount, **k)
        self.currency = currency

    def _raw(self, status, rels):
        raw = super()._raw(status, rels)
        raw["dispute_amount"] = {"currency_code": self.currency, "value": self.amount}
        return raw


@pytest.mark.parametrize("currency,amount,phrase", [("EUR", "123.45", "REFUND 123.45 EUR"), ("MXN", "999.00", "REFUND 999.00 MXN"),
                                                   ("USD", "7.05", "REFUND 7.05 USD")])
def test_the_check_page_shows_the_amount_and_currency_the_dispute_read_gave(monkeypatch, currency, amount, phrase):
    fake = OtherMoney(currency, amount)
    web = _web(monkeypatch, fake)
    token, sha, page = _check(web)
    assert phrase in page and f"{amount} {currency}" in page and CONSEQUENCE in page
    assert "40.00" not in page      # the fixture default of other tests is not carried over
    assert _send(web, token, sha, PHRASE).status_code == 409 and not fake.accept_posts()      # the 40.00 USD phrase does not fit
    assert _send(web, token, sha, phrase).status_code == 303 and len(fake.accept_posts()) == 1


def test_the_currency_changing_between_check_and_send_is_stale(monkeypatch):
    fake = OtherMoney("USD", "40.00")
    web = _web(monkeypatch, fake)
    token, sha, _ = _check(web)
    fake.currency = "EUR"
    r = _send(web, token, sha)
    assert r.status_code == 409 and "changed" in r.text and not fake.accept_posts()


def test_no_amount_in_the_dispute_read_means_no_check(monkeypatch):
    class NoAmount(AcceptFake):
        def _raw(self, status, rels):
            raw = super()._raw(status, rels)
            raw.pop("dispute_amount")
            return raw
    fake = NoAmount()
    web = _web(monkeypatch, fake)
    assert web.post(ACCEPT + "/preview", data={"csrf_token": TOKEN}).status_code == 409 and not fake.accept_posts()


# ---- guard 9: the hosted demo refuses it, for pool cases, the record case and any other ------------------------------------------
@pytest.mark.pool_scope
@pytest.mark.parametrize("dispute", [DISPUTE_ID, "PP-R-HJE-10194967", "PP-R-POOL-0000001"])
def test_the_hosted_demo_refuses_every_route_for_every_case(monkeypatch, dispute):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    monkeypatch.setenv("COPILOT_ACCESS_CODE", "test-access-code-for-hosted")
    fake = AcceptFake()
    web = _web(monkeypatch, fake)
    base = f"/disputes/{dispute}/resolution/accept-claim"
    if config.hosted_demo():
        _csrf(web)
        for method, path, data in (("post", "/preview", {"csrf_token": TOKEN}),
                                   ("post", "/register", {"csrf_token": TOKEN, "token": "x", "approval_sha256": "0" * 64, "phrase": PHRASE}),
                                   ("get", "/check/x", None), ("get", "/sent/1", None)):
            r = getattr(web, method)(base + path, **({"data": data} if data else {}))
            assert r.status_code in (403, 404, 303), (path, r.status_code)
            assert not fake.accept_posts()
        page = web.get(f"/disputes/{dispute}/resolution")
        assert "accept-claim-option" not in page.text
