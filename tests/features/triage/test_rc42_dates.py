"""ISO dates inside generated sentences read as words on the triage page, at every site that uses the filter, and a
date-shaped run inside an identifier (a tracking number, a reference) is never rewritten. Renders the real templates with
synthetic data; no PayPal, no model. Lives with the feature tests (it renders feature templates)."""
import pytest

from copilot.features.claims.model import safe_text
from copilot.web.routes_disputes import templates

# standalone dates (converted) next to identifier-shaped runs (kept as they are)
SAMPLE = "ref 2026-10-07-AB, AB-2026-10-07, 2026-10-07T10:00, 12026-10-07, x/2026-10-07 and (2026-10-07) then 2026-06-01."
CONVERTED_PARTS = ("(7 October 2026)", "then 1 June 2026.")
KEPT_PARTS = ("2026-10-07-AB", "AB-2026-10-07", "2026-10-07T10:00", "12026-10-07", "x/2026-10-07")


def assert_converted_and_kept(html):
    for part in CONVERTED_PARTS + KEPT_PARTS:
        assert part in html, part


def triage_page(**override):
    check = {"state": "missing", "label_for_seller": "Tracking number", "seller_message": "plain", "basis_ref": "plain"}
    verdict = {"norm": {"stage": "INQUIRY", "status": "WAITING_FOR_SELLER_RESPONSE"}, "state": "cannot_meet_rule",
               "scope": "plain scope", "text": "plain text", "recommendation": "plain advice", "buyer_closed": False,
               "missing": [dict(check)], "requests": [{"seller_message": "plain"}], "cautions": [dict(check)]}
    context = {"verdict": verdict, "dispute_id": "PP-R-DEMO-0000001", "limitations": [{"message": "plain"}],
               "economics": {"text": "Concede: refund 1.00 USD + fee 0.00 USD = 1.00 USD · Fight: fee 15.00 USD"},
               "rules": {"country": "MX", "effective_date": "2025-01-13", "rule_set_id": "r", "source_url": "https://example.test/"},
               "checks": [], "safe_text": safe_text, "feature_enabled": lambda name: False}
    for site, value in override.items():
        if site == "scope" or site == "text":
            verdict[site] = value
        elif site == "missing_message":
            verdict["missing"][0]["seller_message"] = value
        elif site == "missing_basis":
            verdict["missing"][0]["basis_ref"] = value
        elif site == "request":
            verdict["requests"][0]["seller_message"] = value
        elif site == "caution":
            verdict["cautions"][0]["seller_message"] = value
        elif site == "limitation":
            context["limitations"][0]["message"] = value
        elif site == "economics":
            context["economics"] = {"text": value}
    return templates.env.get_template("features/triage/review.html").render(**context)


@pytest.mark.parametrize("site", ["scope", "text", "missing_message", "missing_basis", "request", "caution", "limitation",
                                  "economics"])
def test_each_triage_site_converts_standalone_dates_and_keeps_identifiers(site):
    assert_converted_and_kept(triage_page(**{site: SAMPLE}))
