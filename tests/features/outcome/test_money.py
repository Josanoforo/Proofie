"""Money per case (kept, lost, still held) and the totals per currency, on records shaped like PayPal's sandbox GETs of two
resolved cases (a kept one and a lost one: same fields and values, synthetic ids)."""
from _hosted import authenticated_client

from copilot.app import create_app
from copilot.core.disputes import PayPalError
from copilot.features.outcome import money
from copilot.web.routes_disputes import get_client

FLAG = "COPILOT_FEATURE_OUTCOME"
KEPT_ID, LOST_ID, HELD_ID, ODD_ID, USD_ID = ("PP-R-KEP-0000001", "PP-R-LOS-0000002", "PP-R-HEL-0000003", "PP-R-ODD-0000004",
                                             "PP-R-USD-0000005")


def _mxn(value):
    return {"currency_code": "MXN", "value": value}


def kept_raw():
    return {"dispute_id": KEPT_ID, "status": "RESOLVED", "dispute_life_cycle_stage": "CHARGEBACK", "update_time": "2026-11-10T02:30:00.680Z",
            "dispute_amount": _mxn("3099.00"),
            "dispute_outcome": {"outcome_code": "RESOLVED_WITH_PAYOUT", "outcome_reason": "REPRESENTED_BY_PAYPAL",
                                "amount_refunded": _mxn("3099.00")},
            "refund_details": {"allowed_refund_amount": _mxn("3099.00")}}


def lost_raw():
    return {"dispute_id": LOST_ID, "status": "RESOLVED", "dispute_life_cycle_stage": "CHARGEBACK", "update_time": "2026-11-10T02:36:10.501Z",
            "dispute_amount": _mxn("2199.00"),
            "dispute_outcome": {"outcome_code": "RESOLVED_BUYER_FAVOUR", "outcome_reason": "INELIGIBLE_SELLER_PROTECTION_POLICY",
                                "amount_refunded": _mxn("2199.00")},
            "money_movements": [
                {"affected_party": "SELLER", "amount": _mxn("2199.00"), "initiated_time": "2026-11-10T02:36:10.501Z", "type": "DEBIT",
                 "reason": "DISPUTE_SETTLEMENT"},
                {"affected_party": "SELLER", "amount": _mxn("105.35"), "initiated_time": "2026-11-10T02:36:10.501Z", "type": "DEBIT",
                 "reason": "DISPUTE_SETTLEMENT_FEE"}]}


def held_raw():
    return {"dispute_id": HELD_ID, "status": "WAITING_FOR_SELLER_RESPONSE", "dispute_life_cycle_stage": "CHARGEBACK",
            "update_time": "2026-11-10T01:00:00.000Z", "dispute_amount": _mxn("500.00")}


def odd_raw():
    """Resolved for the buyer, but the record lists no debit to the seller: fits neither pattern."""
    return {"dispute_id": ODD_ID, "status": "RESOLVED", "update_time": "2026-11-10T03:00:00.000Z", "dispute_amount": _mxn("800.00"),
            "dispute_outcome": {"outcome_code": "RESOLVED_BUYER_FAVOUR"}}


def usd_raw():
    return {"dispute_id": USD_ID, "status": "UNDER_REVIEW", "update_time": "2026-11-10T01:30:00.000Z",
            "dispute_amount": {"currency_code": "USD", "value": "25.00"}}


RAWS = {r["dispute_id"]: r for r in (kept_raw(), lost_raw(), held_raw(), odd_raw(), usd_raw())}
LIST_KEYS = ("dispute_id", "status", "update_time", "dispute_amount", "dispute_life_cycle_stage")


class Fake:
    """The list answers items without money_movements (as PayPal's list does); a dispute GET answers the full record."""
    def __init__(self, raws=None, fail=()):
        self.raws, self.fail, self.calls = raws or RAWS, set(fail), []

    def call(self, method, path, op, **kw):
        self.calls.append((method, path, op))
        assert method == "GET"
        if path.startswith("/v1/customer/disputes?"):
            items = [{k: r[k] for k in LIST_KEYS if k in r} for r in self.raws.values()]
            for item in items:                # PayPal's list gives update_time without the milliseconds its record has
                item["update_time"] = item["update_time"][:19] + ".000Z"
            return {"items": items, "links": []}
        dispute_id = path.rsplit("/", 1)[1]
        if dispute_id in self.fail:
            raise PayPalError(503, "SERVICE_UNAVAILABLE", "dbg-1", {})
        return self.raws[dispute_id]

    def detail_reads(self):
        return [c for c in self.calls if "?" not in c[1]]


# ---- the rule ------------------------------------------------------------------------------------------------------------------

def test_kept_is_resolved_with_no_seller_debit():
    case = money.classify(kept_raw())
    assert (case.state, case.amount_cents, case.currency, case.fee_cents) == ("kept", 309900, "MXN", None)
    assert case.word == "Kept" and case.amount_text == "3,099.00 MXN"


def test_lost_is_the_settlement_debit_and_the_fee_stays_apart():
    case = money.classify(lost_raw())
    assert (case.state, case.amount_cents, case.currency) == ("lost", 219900, "MXN")
    assert (case.fee_cents, case.fee_currency) == (10535, "MXN")          # never added into the amount lost
    assert case.amount_text == "2,199.00 MXN" and case.fee_text == "105.35 MXN"


def test_held_is_any_case_with_no_outcome_yet():
    case = money.classify(held_raw())
    assert (case.state, case.amount_cents, case.word) == ("held", 50000, "Still held")


def test_a_resolved_record_that_fits_neither_pattern_states_no_amount():
    case = money.classify(odd_raw())
    assert case.state == "unstated" and case.amount_cents is None and case.amount_text is None
    assert case.outcome_words == "Resolved in the buyer's favor"
    other_debit = lost_raw()
    other_debit["money_movements"] = [{"affected_party": "SELLER", "type": "DEBIT", "reason": "SOMETHING_ELSE", "amount": _mxn("1.00")}]
    other_debit["dispute_outcome"] = {"outcome_code": "RESOLVED_WITH_PAYOUT"}
    assert money.classify(other_debit).state == "unstated"                 # a debit to her: not "kept"
    unreadable = lost_raw()
    unreadable["money_movements"][0]["amount"] = {"currency_code": "MXN", "value": "2199.001"}
    assert money.classify(unreadable).state == "unstated"                  # never rounded, never guessed


def test_a_buyer_credit_is_not_a_seller_debit():
    raw = kept_raw()
    raw["money_movements"] = [{"affected_party": "BUYER", "type": "CREDIT", "reason": "DISPUTE_SETTLEMENT", "amount": _mxn("3099.00")}]
    assert money.classify(raw).state == "kept"


def test_money_text_is_integer_arithmetic():
    assert money.money_text(5, "MXN") == "0.05 MXN" and money.money_text(123456789, "USD") == "1,234,567.89 USD"
    assert money.money_text(1500, "JPY") == "1,500 JPY" and money.money_text(None, "MXN") is None
    assert money.money_text(True, "MXN") is None and money.money_text(10.0, "MXN") is None


def test_totals_per_currency_never_mixed():
    rows = money.totals([money.classify(r) for r in RAWS.values()])
    assert [t.currency for t in rows] == ["MXN", "USD"]
    mxn, usd = rows
    assert (mxn.kept_cents, mxn.lost_cents, mxn.held_cents, mxn.fee_cents) == (309900, 219900, 50000, 10535)
    assert (mxn.kept_cases, mxn.lost_cases, mxn.held_cases) == (1, 1, 1)
    assert (usd.held_cents, usd.held_cases, usd.kept_cents, usd.lost_cents) == (2500, 1, 0, 0)
    assert mxn.text("lost") == "2,199.00 MXN"


# ---- the pages -----------------------------------------------------------------------------------------------------------------

def _web(monkeypatch, fake, flag="true"):
    monkeypatch.setenv(FLAG, flag) if flag else monkeypatch.delenv(FLAG, raising=False)
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    return authenticated_client(app, client=("127.0.0.1", 12345))


def test_case_page_kept_lost_held_in_plain_words(monkeypatch):
    fake = Fake()
    web = _web(monkeypatch, fake)
    kept = web.get(f"/disputes/{KEPT_ID}/outcome/money")
    assert kept.status_code == 200 and 'data-money-state="kept"' in kept.text
    assert "Kept: " in kept.text and "3,099.00 MXN" in kept.text and "You keep this sale." in kept.text
    lost = web.get(f"/disputes/{LOST_ID}/outcome/money")
    assert 'data-money-state="lost"' in lost.text and "Lost: " in lost.text and "2,199.00 MXN" in lost.text
    assert "Fees on PayPal" in lost.text and "105.35 MXN" in lost.text
    assert "2,304.35" not in lost.text and "chargeback fee" not in lost.text.lower()
    held = web.get(f"/disputes/{HELD_ID}/outcome/money")
    assert 'data-money-state="held"' in held.text and "Still held: " in held.text and "500.00 MXN" in held.text
    odd = web.get(f"/disputes/{ODD_ID}/outcome/money")
    assert "Resolved in the buyer&#39;s favor, as stated by PayPal" in odd.text and "800.00" not in odd.text
    assert all(call[0] == "GET" for call in fake.calls) and len(fake.calls) == 4          # one GET per page, no write


def test_case_page_says_so_when_paypal_does_not_answer(monkeypatch):
    web = _web(monkeypatch, Fake(fail={KEPT_ID}))
    page = web.get(f"/disputes/{KEPT_ID}/outcome/money")
    assert page.status_code == 200 and 'data-state="case-money-not-read"' in page.text and "Kept" not in page.text


def test_totals_page_and_stored_reads(monkeypatch):
    fake = Fake()
    web = _web(monkeypatch, fake)
    page = web.get("/money")
    assert page.status_code == 200
    for text in ("3,099.00 MXN", "2,199.00 MXN", "500.00 MXN", "105.35 MXN", "25.00 USD"):
        assert text in page.text
    assert page.text.count('data-currency="') == 2 and "Amount not stated by PayPal" in page.text
    assert len(fake.detail_reads()) == 3                      # the three resolved cases; open cases come from the list
    web.get("/money")
    assert len(fake.detail_reads()) == 3                      # resolved read-backs are reused, not asked again


def test_totals_page_bounds_fresh_reads_and_counts_the_rest(monkeypatch):
    fake = Fake()
    view = money.account_view(fake, max_reads=1)
    assert view.fresh_reads == 1 and view.not_read == 2 and len(fake.detail_reads()) == 1
    unanswered = money.account_view(Fake(fail={LOST_ID}))
    assert unanswered.not_read >= 1 and all(c.dispute_id != LOST_ID for c in unanswered.cases)


def test_pages_do_not_exist_with_the_outcome_flag_off(monkeypatch):
    web = _web(monkeypatch, Fake(), flag="false")
    assert web.get("/money").status_code == 404
    assert web.get(f"/disputes/{KEPT_ID}/outcome/money").status_code == 404


def test_outcome_page_links_to_the_case_money(monkeypatch):
    web = _web(monkeypatch, Fake())
    page = web.get(f"/disputes/{LOST_ID}/outcome")
    assert f'href="/disputes/{LOST_ID}/outcome/money"' in page.text


import pytest  # noqa: E402


@pytest.mark.pool_scope
def test_hosted_demo_totals_cover_only_the_pool_with_one_read_per_case(monkeypatch):
    from copilot.core import pool_scope
    monkeypatch.setattr(pool_scope, "active", lambda: True)
    monkeypatch.setattr(pool_scope, "listed_ids", lambda: frozenset({KEPT_ID, HELD_ID}))
    fake = Fake()
    view = money.account_view(fake)
    assert sorted(c.dispute_id for c in view.cases) == sorted([KEPT_ID, HELD_ID])
    assert len(fake.calls) == 2 and all("?" not in call[1] for call in fake.calls)        # never the account's list, no second read
    assert [(t.currency, t.kept_cents, t.held_cents, t.lost_cents) for t in view.totals] == [("MXN", 309900, 50000, 0)]


def test_inbox_links_to_the_totals(monkeypatch):
    web = _web(monkeypatch, Fake())
    page = web.get("/disputes")
    assert page.status_code == 200 and 'href="/money"' in page.text
    off = _web(monkeypatch, Fake(), flag="false").get("/disputes")
    assert off.status_code == 200 and 'href="/money"' not in off.text
