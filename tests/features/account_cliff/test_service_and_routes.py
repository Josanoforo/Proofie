"""Service, inbox line and /account on synthetic fixtures: flag off (default) and flag on with a fake client."""
from _hosted import authenticated_client, reset_csrf
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from copilot.app import create_app
from copilot.core.disputes import PayPalError
from copilot.features.account_cliff import service
from copilot.features.account_cliff.rules import ALL_CONSTANTS
from copilot.web.routes_disputes import get_client

FLAG = "COPILOT_FEATURE_ACCOUNT_CLIFF"
TODAY = date(2026, 10, 9)
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def _sales(n):
    return [{"transaction_info": {"transaction_event_code": "T0006", "transaction_status": "S"}} for _ in range(n)]


def _dispute(i, created):
    return {"dispute_id": f"PP-X-{i}", "status": "RESOLVED", "create_time": created,
            "dispute_amount": {"currency_code": "USD", "value": "10.00"}}


class FakeClient:
    """Sales in the window (single page), disputes dated inside the window, one outside it."""
    def __init__(self, sales=200, disputes_in_window=2, raises=None):
        self.sales, self.raises = sales, raises
        self.disputes = [_dispute(i, "2026-08-15T10:00:00Z") for i in range(disputes_in_window)]
        self.disputes.append(_dispute(99, "2026-10-02T10:00:00Z"))      # outside the window
        self.calls = []

    def call(self, method, path, op, **kw):
        self.calls.append((method, path, op))
        if self.raises is not None:
            raise self.raises
        if op == "list_disputes":
            return {"items": self.disputes}
        assert op == "account_cliff_search"
        # 92 days = 3 ranges of <= 31 days; put every sale in the first one
        return {"transaction_details": _sales(self.sales) if "start_date=2026-07-01" in path else [], "total_pages": 1}


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    # Route tests use a fixed historical sales window; inject its matching clock.
    # Pure compute/cache tests retain their explicit NOW arguments and assertions.
    class FixtureDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)

    monkeypatch.setattr(service, "datetime", FixtureDateTime)
    service.clear_cache()
    yield
    service.clear_cache()


def _app(fake=None, flag=None, monkeypatch=None):
    if monkeypatch is not None:
        if flag is None:
            monkeypatch.delenv(FLAG, raising=False)
        else:
            monkeypatch.setenv(FLAG, flag)
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake or FakeClient()
    return app


def _web(app):
    return authenticated_client(app, client=("127.0.0.1", 12345))


def test_compute_below_the_threshold():
    s = service.compute(FakeClient(200, 2), today=TODAY, now=NOW)
    assert (s.window_start, s.window_end) == (date(2026, 7, 1), date(2026, 9, 30))
    assert (s.sales, s.disputes, s.rate_bps, s.high_volume, s.disputes_to_threshold) == (200, 2, 100, False, 1)
    assert (s.fee_now_cents, s.fee_if_high_cents, s.limitations) == (1500, 3000, ())


def test_compute_at_the_threshold_is_high_volume_and_fee_now_is_the_high_fee():
    s = service.compute(FakeClient(200, 3), today=TODAY, now=NOW)
    assert (s.rate_bps, s.high_volume, s.disputes_to_threshold, s.fee_now_cents) == (150, True, 0, 3000)


def test_compute_when_paypal_fails_shows_unknowns_and_limitations_never_a_guess():
    s = service.compute(FakeClient(raises=PayPalError(403, "NOT_AUTHORIZED", "dbg", {})), today=TODAY, now=NOW)
    assert (s.sales, s.disputes, s.rate_bps, s.high_volume, s.fee_now_cents) == (None, None, None, None, None)
    assert len(s.limitations) == 2


def test_inbox_line_texts():
    # Three cases, numbers from the rule constants and the counts
    below = service.inbox_line(FakeClient(200, 2), now=NOW)
    assert below == ("High-volume dispute fee: not reached. PayPal applies it only above 100 sales in 3 full months and at a "
                     "dispute rate of 1.50 % or more. Proofie counted 200 sales and 2 disputes in PayPal's records for that period.")
    service.clear_cache()
    reached = service.inbox_line(FakeClient(200, 3), now=NOW)
    assert reached == "High-volume dispute fee: may apply. Proofie counted 200 sales in PayPal's records for that period."
    service.clear_cache()
    few = service.inbox_line(FakeClient(40, 1), now=NOW)
    assert few == ("High-volume dispute fee: not reached. PayPal applies it only above 100 sales in 3 full months. "
                   "Proofie counted 40 sales in PayPal's records for that period.")
    service.clear_cache()
    none = service.inbox_line(FakeClient(0, 0), now=NOW)
    assert none == ("High-volume dispute fee: not reached. PayPal applies it only above 100 sales in 3 full months. "
                    "Proofie counted 0 sales in PayPal's records for that period.")
    service.clear_cache()
    unknown = service.inbox_line(FakeClient(raises=PayPalError(500, None, None, {})), now=NOW)
    assert unknown == "High-volume dispute fee: could not be checked. PayPal's sales records could not be read completely."


def test_inbox_line_is_cached_for_ten_minutes_and_not_when_a_read_failed():
    fake = FakeClient(200, 2)
    first = service.inbox_line(fake, now=NOW)
    reads = len(fake.calls)
    assert service.inbox_line(fake, now=NOW + timedelta(minutes=9, seconds=59)) == first
    assert len(fake.calls) == reads                                  # served from the cache
    service.inbox_line(fake, now=NOW + timedelta(minutes=10))
    assert len(fake.calls) > reads                                   # expired: read again
    service.clear_cache()
    bad = FakeClient(raises=PayPalError(500, None, None, {}))
    service.inbox_line(bad, now=NOW)
    n = len(bad.calls)
    service.inbox_line(bad, now=NOW + timedelta(minutes=1))
    assert len(bad.calls) > n                                        # failures are retried, not cached


def test_inbox_line_returns_none_on_a_programming_error():
    assert service.inbox_line(FakeClient(raises=KeyError("bug")), now=NOW) is None


# ---- flag off (the default) ------------------------------------------------------------------------------------
def test_flag_off_account_page_is_404_and_inbox_has_no_account_cliff_element(monkeypatch):
    web = _web(_app(monkeypatch=monkeypatch))
    assert web.get("/account").status_code == 404
    inbox = web.get("/disputes")
    assert inbox.status_code == 200 and "account-cliff" not in inbox.text


def test_flag_off_for_any_value_other_than_true(monkeypatch):
    web = _web(_app(flag="1", monkeypatch=monkeypatch))
    assert web.get("/account").status_code == 404


def test_flag_off_never_reads_the_cliff_data(monkeypatch):
    fake = FakeClient()
    _web(_app(fake, monkeypatch=monkeypatch)).get("/disputes")
    assert not [c for c in fake.calls if c[2] == "account_cliff_search"]


# ---- flag on, fake client --------------------------------------------------------------------------------------
def test_flag_on_inbox_shows_the_line_computed_from_the_fixture(monkeypatch):
    web = _web(_app(FakeClient(200, 2), "true", monkeypatch))
    html = web.get("/disputes").text
    assert 'class="account-cliff"' in html
    assert "High-volume dispute fee: not reached." in html and "Proofie counted 200 sales and 2 disputes" in html
    assert 'href="/account"' in html and "Fee details" in html


def test_flag_on_account_page_lists_all_six_constants_each_with_a_source(monkeypatch):
    web = _web(_app(FakeClient(200, 2), "true", monkeypatch))
    r = web.get("/account")
    assert r.status_code == 200
    assert len(ALL_CONSTANTS) == 6 and all(c.source.strip() for c in ALL_CONSTANTS)
    assert r.text.count("data-constant=") == 6
    from copilot.web.presentation import dates_in_words      # ISO dates inside a source read as words
    for c in ALL_CONSTANTS:
        assert c.name in r.text and dates_in_words(c.source) in r.text
    for text in ("1 July 2026 to 30 September 2026", "1.00 %", "15.00 USD", "30.00 USD", "1 more dispute would reach it",
                 "These are the account counts read from PayPal for this window. The same formula is used for the fee comparison."):
        assert text in r.text


def test_flag_on_account_page_shows_limitations_when_paypal_fails(monkeypatch):
    web = _web(_app(FakeClient(raises=PayPalError(403, "X", "dbg", {})), "true", monkeypatch))
    r = web.get("/account")
    assert r.status_code == 200 and "High-volume dispute fee: could not be checked." in r.text
    assert "account-cliff-limitations" in r.text
    assert "dbg" not in r.text


def test_flag_on_inbox_survives_a_programming_error_in_the_line(monkeypatch):
    # the disputes list works, only the cliff search is a bug
    class SearchBug(FakeClient):
        def call(self, method, path, op, **kw):
            if op == "account_cliff_search":
                raise KeyError("bug")
            return super().call(method, path, op, **kw)
    web = _web(_app(SearchBug(), "true", monkeypatch))
    r = web.get("/disputes")
    assert r.status_code == 200 and "account-cliff" not in r.text


def test_the_feature_has_no_write_route(monkeypatch):
    web = _web(_app(flag="true", monkeypatch=monkeypatch))
    for verb in ("post", "put", "patch", "delete"):
        assert getattr(web, verb)("/account").status_code == 405, verb


# Explicit mode contract; local specifications keep their original business assertions.
pytestmark = [pytest.mark.usefixtures("web_mode")]
