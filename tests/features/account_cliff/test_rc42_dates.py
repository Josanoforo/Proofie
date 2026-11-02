"""ISO dates inside generated sentences read as words on the account page, at every site that uses the filter, and a date-shaped
run inside an identifier is never rewritten. Renders the real template with synthetic data; no PayPal, no model."""
from datetime import date
from types import SimpleNamespace

import pytest

from copilot.web.routes_disputes import templates

SAMPLE = "ref 2026-10-07-AB, AB-2026-10-07, 2026-10-07T10:00, 12026-10-07, x/2026-10-07 and (2026-10-07) then 2026-06-01."
CONVERTED_PARTS = ("(7 October 2026)", "then 1 June 2026.")
KEPT_PARTS = ("2026-10-07-AB", "AB-2026-10-07", "2026-10-07T10:00", "12026-10-07", "x/2026-10-07")


def assert_converted_and_kept(html):
    for part in CONVERTED_PARTS + KEPT_PARTS:
        assert part in html, part


def account_page(**override):
    constant = SimpleNamespace(name="plain", display="plain", source="plain", value=1)
    fact = SimpleNamespace(name="plain", display="plain", source="plain", read_date="2026-10-05")
    window = SimpleNamespace(window_start=date(2026, 7, 1), window_end=date(2026, 9, 30), limitations=[])
    context = {"line": "plain", "schedule_limitation": "plain", "summary": window, "sales": 1, "disputes": 0, "rate": "0 %",
               "status": "ok", "fee_now": "15.00 USD", "fee_if_high": "30.00 USD", "computed_at": "2026-10-17T12:00:00+00:00",
               "constants": [constant], "network_facts": [fact], "won_sentence": "plain"}
    for site, value in override.items():
        if site.startswith("constant_"):
            setattr(constant, site[len("constant_"):], value)
        elif site.startswith("fact_"):
            setattr(fact, site[len("fact_"):], value)
        else:
            context[site] = value
    return templates.env.get_template("features/account_cliff/account.html").render(**context)


@pytest.mark.parametrize("site", ["constant_name", "constant_display", "constant_source", "fact_display", "fact_source",
                                  "won_sentence"])
def test_each_account_site_converts_standalone_dates_and_keeps_identifiers(site):
    assert_converted_and_kept(account_page(**{site: SAMPLE}))
