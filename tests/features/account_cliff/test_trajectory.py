"""D-101 P3: the dated path to the high-volume rule, on synthetic fixtures. Today is 2026-11-10 (a 30-day month, 20 days
remaining), so the next window is Sep, Oct and Nov 2026 and applies to cases opened from 2026-12-01; the pace is the last 30
days (2026-10-12 to 2026-11-10). Every expected figure below is worked by hand from the rule (more than 100 sales and
disputes * 10000 >= 150 * sales) before the code ran."""
from datetime import date, datetime, timezone

import httpx
import pytest
from _hosted import authenticated_client

from copilot.app import create_app
from copilot.core.disputes import PayPalError
from copilot.features.account_cliff import trajectory as t
from copilot.web.routes_disputes import get_client

TODAY = date(2026, 11, 10)
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def _p(sales_prev, sales_month, disputes_prev, disputes_month, pace_sales=300, pace_disputes=6):
    return t.project(today=TODAY, sales_prev=sales_prev, sales_month=sales_month, disputes_prev=disputes_prev,
                     disputes_month=disputes_month, pace_sales=pace_sales, pace_disputes=pace_disputes)


def test_dated_path_numbers_worked_by_hand():
    # counted 200 + 100 = 300 sales; projected 300 + 300*20//30 = 500; disputes 5 + 2 = 7; 8 disputes are 1.60 %, 7 are 1.40 %,
    # so 1 more is needed; 6 disputes in the last 30 days -> 1 more in ceil(1*30/6) = 5 days -> 2026-11-15; next month at
    # this pace 6*30//30 = 6 disputes, 6 * 1500 = 9000 cents
    p = _p(200, 100, 5, 2)
    assert p.state == t.DATED
    assert (p.sales_counted, p.sales_projected, p.disputes_counted) == (300, 500, 7)
    assert (p.disputes_to_cross, p.days_to_cross, p.cross_date) == (1, 5, date(2026, 11, 15))
    assert (p.window_start, p.window_end, p.applies_from, p.pace_start) == (
        date(2026, 9, 1), date(2026, 11, 30), date(2026, 12, 1), date(2026, 10, 12))
    assert (p.expected_next_month, p.extra_fee_per_dispute_cents, p.extra_fee_next_month_cents) == (6, 1500, 9000)
    rows = dict(t.rows(p))
    assert rows["Days until then, at the 30-day pace"] == "5 (on 2026-11-15)"
    assert rows["Extra fee next month at this pace"] == "90.00 USD"
    assert rows["Extra fee per dispute if the high-volume fee applies"] == "15.00 USD"
    assert rows["Pace: sales / disputes in the last 30 days"] == "300 / 6"
    assert t.headline(p) == ("At the pace of the last 30 days (6 disputes), 1 more dispute would reach 1.50 % on 2026-11-15, "
                             "in 5 days. For cases opened from 2026-12-01, the high-volume fee may apply.")


def test_extra_fee_is_the_difference_of_two_sourced_constants():
    from copilot.features.account_cliff.rules import HIGH_VOLUME_FEE_CENTS, STANDARD_FEE_CENTS
    assert t.EXTRA_FEE_PER_DISPUTE_CENTS == HIGH_VOLUME_FEE_CENTS.value - STANDARD_FEE_CENTS.value == 1500
    assert HIGH_VOLUME_FEE_CENTS.source and STANDARD_FEE_CENTS.source


def test_not_this_month_when_the_pace_is_too_slow():
    # 1 dispute in 30 days, 7 more needed (D = 1) -> 210 days, long after the month ends: no date is given
    p = _p(200, 100, 0, 1, pace_disputes=1)
    assert (p.state, p.disputes_to_cross, p.days_to_cross, p.cross_date) == (t.NOT_THIS_MONTH, 7, 210, None)
    assert t.headline(p) == ("For cases opened from 2026-12-01, 7 more disputes would reach 1.50 %. At the pace of the last 30 "
                             "days (1 dispute), they would not arrive before 2026-12-01.")
    assert dict(t.rows(p))["Days until then, at the 30-day pace"] == t.NOT_AVAILABLE


def test_no_pace_when_no_dispute_in_the_last_30_days():
    p = _p(200, 100, 5, 0, pace_disputes=0)
    assert (p.state, p.disputes_to_cross, p.days_to_cross, p.cross_date) == (t.NO_PACE, 3, None, None)
    assert "cannot be dated" in t.headline(p)
    assert p.extra_fee_next_month_cents == 0


def test_already_over_at_exactly_the_threshold():
    p = _p(200, 100, 8, 0)   # 8 * 10000 = 80000 >= 150 * 500 = 75000 -> over; 7 gives 70000 < 75000
    assert (p.state, p.disputes_to_cross, p.days_to_cross) == (t.ALREADY_OVER, 0, 0)
    assert _p(200, 100, 7, 0).disputes_to_cross == 1
    assert "may apply" in t.headline(p)


def test_exactly_one_hundred_projected_sales_is_below_the_minimum_and_101_is_not():
    below = _p(40, 20, 3, 0, pace_sales=60)   # 60 + 60*20//30 = 100
    assert (below.state, below.sales_projected, below.disputes_to_cross) == (t.BELOW_MINIMUM, 100, None)
    assert "would not apply" in t.headline(below)
    assert dict(t.rows(below))["More disputes to reach the threshold"] == t.NOT_AVAILABLE
    assert _p(40, 20, 3, 0, pace_sales=62).state != t.BELOW_MINIMUM   # 60 + 62*20//30 = 101


def test_projection_rounds_down_in_whole_sales():
    assert _p(0, 0, 0, 0, pace_sales=7).sales_projected == 4   # 7*20//30 = 4


def test_the_pace_does_not_depend_on_the_day_of_the_month():
    # the same 30-day pace on the 1st: 29 days remain, so 300*29//30 = 290 more sales; nothing is multiplied from one day
    p = t.project(today=date(2026, 11, 1), sales_prev=27, sales_month=12, disputes_prev=0, disputes_month=0,
                  pace_sales=39, pace_disputes=0)
    assert p.sales_projected == 39 + 39 * 29 // 30 == 76 and p.state == t.BELOW_MINIMUM


def test_no_float_and_text_is_made_by_code():
    p = _p(200, 100, 5, 2)
    for value in vars(p).values():
        assert not isinstance(value, float)
    assert all(isinstance(v, str) for _, v in t.rows(p))


# ---- the read: a fake PayPal, GET only, shaped like the existing account_cliff fixtures ----------------------------
def _row(day):
    return {"transaction_info": {"transaction_event_code": "T0006", "transaction_status": "S",
                                 "transaction_initiation_date": f"{day}T10:00:00+0000"}}


def _dispute(i, created):
    return {"dispute_id": f"PP-X-{i}", "status": "RESOLVED", "create_time": created,
            "dispute_amount": {"currency_code": "USD", "value": "10.00"}}


class FakeClient:
    def __init__(self, raises=None, undated=False):
        self.raises, self.calls = raises, []
        # prev window: 150 (Sep 15) + 50 (Oct 5) + 100 (Oct 20) = 300; this month 100 (Nov 3); last 30 days: 100 + 100 = 200
        self.rows = ([_row("2026-09-15")] * 150 + [_row("2026-10-05")] * 50 + [_row("2026-10-20")] * 100
                     + [_row("2026-11-03")] * 100
                     + [{"transaction_info": {"transaction_event_code": "T0006", "transaction_status": "S"}}] * (3 if undated else 0))
        self.disputes = ([_dispute(i, "2026-09-12T10:00:00Z") for i in range(3)]
                         + [_dispute(10 + i, "2026-10-12T10:00:00Z") for i in range(2)]     # Oct 12 is the first pace day
                         + [_dispute(20 + i, "2026-11-04T10:00:00Z") for i in range(2)]
                         + [_dispute(99, "2026-08-31T10:00:00Z")])                          # before the window

    def call(self, method, path, op, **kw):
        self.calls.append((method, path, op))
        if self.raises is not None:
            raise self.raises
        if op == "list_disputes":
            return {"items": self.disputes}
        assert op == "account_cliff_search"
        return {"transaction_details": self.rows if "start_date=2026-09-01" in path else [], "total_pages": 1}


def test_compute_path_counts_by_month_and_pace_from_the_reads_and_only_reads():
    # counted 300 + 100 = 400; pace sales 200 -> projected 400 + 200*20//30 = 533; disputes 3+2+2 = 7, pace 2+2 = 4;
    # 8 needed at 533 sales (1.5 % of 533 = 7.995), so 1 more -> ceil(1*30/4) = 8 days -> 2026-11-18; 4*30//30 = 4 next month
    fake = FakeClient()
    p = t.compute_path(fake, today=TODAY, now=NOW)
    assert (p.state, p.sales_counted, p.sales_projected, p.disputes_counted) == (t.DATED, 400, 533, 7)
    assert (p.pace_sales, p.pace_disputes) == (200, 4)
    assert (p.disputes_to_cross, p.days_to_cross, p.cross_date) == (1, 8, date(2026, 11, 18))
    assert (p.expected_next_month, p.extra_fee_next_month_cents) == (4, 6000)
    assert p.limitations == () and {c[0] for c in fake.calls} == {"GET"}
    assert {c[2] for c in fake.calls} == {"account_cliff_search", "list_disputes"}
    starts = [c[1] for c in fake.calls if c[2] == "account_cliff_search"]
    assert len(starts) == 3 and "start_date=2026-09-01" in starts[0] and "end_date=2026-11-10T23%3A59%3A59Z" in starts[-1]


def test_compute_path_reports_sales_without_a_date_instead_of_counting_them():
    p = t.compute_path(FakeClient(undated=True), today=TODAY, now=NOW)
    assert p.sales_counted == 400 and len(p.limitations) == 1


@pytest.mark.parametrize("error", [PayPalError(403, "X", "dbg", {}), PayPalError(500, None, None, {})])
def test_compute_path_is_unknown_when_paypal_fails_never_a_guess(error):
    p = t.compute_path(FakeClient(raises=error), today=TODAY, now=NOW)
    assert p.state == t.UNKNOWN and p.limitations
    assert p.sales_projected is None and p.disputes_to_cross is None and p.cross_date is None
    assert all(v == t.NOT_AVAILABLE for label, v in t.rows(p) if "Extra fee per dispute" not in label)
    assert "could not be worked out" in t.headline(p)


def test_compute_path_lets_a_programming_error_through():
    with pytest.raises(KeyError):
        t.compute_path(FakeClient(raises=KeyError("bug")), today=TODAY, now=NOW)


# ---- the page --------------------------------------------------------------------------------------------------------
def _web(monkeypatch, path_flag, fake=None):
    monkeypatch.setenv("COPILOT_FEATURE_ACCOUNT_CLIFF", "true")
    if path_flag is None:
        monkeypatch.delenv("COPILOT_FEATURE_ACCOUNT_CLIFF_PATH", raising=False)
    else:
        monkeypatch.setenv("COPILOT_FEATURE_ACCOUNT_CLIFF_PATH", path_flag)
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake or FakeClient()
    return authenticated_client(app, client=("127.0.0.1", 12345))


def test_flag_off_the_account_page_has_no_path_section(monkeypatch):
    html = _web(monkeypatch, None).get("/account").text
    assert "Path to the cliff" not in html and "cliff-path" not in html


def test_flag_on_the_account_page_shows_the_path_inside_the_same_page(monkeypatch):
    real = t.compute_path
    monkeypatch.setattr(t, "compute_path", lambda client, today=None, now=None: real(client, TODAY, NOW))
    r = _web(monkeypatch, "true").get("/account")
    assert r.status_code == 200 and "Path to the cliff" in r.text and 'data-path-state="dated"' in r.text
    assert "18 November 2026" in r.text and "60.00 USD" in r.text and "may apply" in r.text
    assert "T10:00" not in r.text and " cents" not in r.text            # no raw machine values
    assert r.text.count("<h1>") == 1


def test_flag_on_the_account_page_with_a_failed_read_says_not_available(monkeypatch):
    r = _web(monkeypatch, "true", FakeClient(raises=PayPalError(403, "X", "dbg", {}))).get("/account")
    assert r.status_code == 200 and 'data-path-state="unknown"' in r.text and "could not be worked out" in r.text
    assert "dbg" not in r.text


@pytest.mark.parametrize("error", [PayPalError(400, "X", "dbg", {}), PayPalError(404, "X", "dbg", {}),
                                   PayPalError(422, "X", "dbg", {}), PayPalError(429, "X", "dbg", {})])
def test_a_4xx_answer_from_paypal_still_gives_a_200_page_with_the_unknown_note(monkeypatch, error):
    r = _web(monkeypatch, "true", FakeClient(raises=error)).get("/account")
    assert r.status_code == 200 and 'data-path-state="unknown"' in r.text and "could not be worked out" in r.text
    assert "Not available" in r.text and "dbg" not in r.text


@pytest.mark.parametrize("error", [httpx.ConnectError("down"), TimeoutError("slow"), PayPalError(500, "X", "dbg", {})])
def test_a_fake_that_raises_still_gives_a_200_page_with_the_unknown_note(monkeypatch, error):
    r = _web(monkeypatch, "true", FakeClient(raises=error)).get("/account")
    assert r.status_code == 200 and 'data-path-state="unknown"' in r.text and "dbg" not in r.text


def test_an_unexpected_failure_inside_the_path_never_takes_the_page_down(monkeypatch, caplog):
    def boom(client, today=None, now=None):
        raise ValueError("boom with a secret-looking detail")
    monkeypatch.setattr(t, "compute_path", boom)
    r = _web(monkeypatch, "true").get("/account")
    assert r.status_code == 200 and 'data-path-state="unknown"' in r.text and "Not available" in r.text
    assert "boom" not in r.text and "secret-looking" not in r.text           # the reason is plain, the detail goes to the log
    assert "not in the expected shape" in r.text
    assert any("account_cliff path" in rec.message for rec in caplog.records)


def test_compute_path_safe_returns_unknown_instead_of_raising():
    p = t.compute_path_safe(FakeClient(raises=KeyError("bug")), today=TODAY, now=NOW)
    assert p.state == t.UNKNOWN and p.limitations and p.sales_projected is None


def test_the_case_overview_has_no_cliff_path(monkeypatch):
    assert "cliff-path" not in _web(monkeypatch, "true").get("/disputes").text


def test_the_feature_still_has_no_write_route(monkeypatch):
    web = _web(monkeypatch, "true")
    for verb in ("post", "put", "patch", "delete"):
        assert getattr(web, verb)("/account").status_code in (403, 404, 405)
