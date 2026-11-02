"""The contract-checks page as a captioned table.

Expected: one table row per request fixture of run(), words not file names, a result of 'matches', 'differs' or 'no schema
to check'; one counts row whose numbers are the run's own; a caption; scoped headers; the 'What this means' paragraph in
place of the old offline sentence; the long sentence stays in Technical details; the webhook rows are worded as samples;
zero recorded responses never say 'the rest differ'; no network call; no environment-variable name outside the details."""
import json
import re
from html import unescape

import pytest
from _dom import parse
from _hosted import authenticated_client

from copilot.app import create_app
from copilot.features.conformance.suite import configured_run

WHAT_THIS_MEANS = ("What this means: these checks compare the shape of what Proofie sends with PayPal's published "
                   "documents, pinned 6 Oct 2026. They run on this computer, contact no one and do not check your "
                   "disputes. You do not need to do anything.")


def _page(monkeypatch, evidence=None):
    monkeypatch.setenv("COPILOT_FEATURE_CONFORMANCE", "true")
    monkeypatch.delenv("PROOFIE_SPECS_DIR", raising=False)
    if evidence is None:
        monkeypatch.delenv("PROOFIE_EVIDENCE_DIR", raising=False)
    else:
        monkeypatch.setenv("PROOFIE_EVIDENCE_DIR", str(evidence))
    page = unescape(authenticated_client(create_app()).get("/conformance").text)
    return page, parse(page)


def test_the_table_has_one_row_per_request_fixture_a_caption_and_scoped_headers(monkeypatch):
    page, dom = _page(monkeypatch)
    result, _ = configured_run()
    table = dom.find("table", data_state="request-checks")
    assert table.find("caption").text() == "Proofie's requests compared with PayPal's published API documents"
    assert all(th.attrs.get("scope") in ("col", "row") for th in table.find_all("th"))
    rows = table.find("tbody").find_all("tr")
    request_rows = [r for r in rows if r.attrs.get("data-row") == "request"]
    assert len(request_rows) == len(result["requests"]) == 16
    words = {"matches", "differs", "no schema to check"}
    results = [r.find_all("td")[-1].text() for r in request_rows]
    assert {t.split(":")[0] for t in results} <= words
    assert sum(t == "matches" for t in results) == sum(x["valid"] is True for x in result["requests"])
    assert sum(t.startswith("differs") for t in results) == sum(x["valid"] is False for x in result["requests"])
    names = [r.find("th").text() for r in request_rows]
    assert len(set(names)) == 16 and not any("_" in n for n in names)
    assert not any(n.startswith(("evidence-", "offer-", "tracker-", "appeal-", "webhook-")) for n in names)


def test_the_counts_row_carries_the_runs_own_numbers(monkeypatch):
    page, dom = _page(monkeypatch)
    result, _ = configured_run()
    requests = result["requests"]
    valid, invalid, none = (sum(r["valid"] is v for r in requests) for v in (True, False, None))
    row = dom.find("table", data_state="request-checks").find("tr", data_row="counts")
    assert row.find("th").text() == "All request checks"
    assert row.find("td").text() == (f"{valid} of {len(requests)} match; {invalid} differs; "
                                     f"{none} has no schema to check.")


def test_the_webhook_rows_are_worded_as_samples_because_verification_is_off(monkeypatch):
    page, dom = _page(monkeypatch)
    rows = [r for r in dom.find("table", data_state="request-checks").find_all("tr") if r.attrs.get("data-row") == "request"
            and r.find("th").text().startswith("Webhook")]
    assert len(rows) == 2 and all("a sample; verification is off" in r.find("th").text() for r in rows)


def test_the_what_this_means_paragraph_replaces_the_offline_sentence_and_the_long_sentence_stays_in_details(monkeypatch):
    page, dom = _page(monkeypatch)
    assert dom.find("p", data_state="what-this-means").text() == WHAT_THIS_MEANS
    assert "These are offline checks that run on this computer." not in page
    result, _ = configured_run()
    details = dom.find("details", data_state="technical")
    assert result["sentence"] in details.text() and "PROOFIE_SPECS_DIR" in details.text()
    outside = re.sub(r"<details.*?</details>", "", page, flags=re.S)
    assert "PROOFIE_" not in outside and "the rest differ" not in outside


def test_the_mapped_carrier_sentence_comes_from_the_run(monkeypatch):
    page, dom = _page(monkeypatch)
    result, _ = configured_run()
    assert all(not m["issues"] for m in result["mapped"])
    assert dom.find("p", data_state="mapped").text() == (
        "The DOMINO carrier, as sent in the evidence request and in the appeal: 2 of 2 match PayPal's published documents.")


def test_with_no_recorded_responses_the_page_never_says_the_rest_differ(monkeypatch, tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "empty.jsonl").write_text("", encoding="utf-8")
    page, dom = _page(monkeypatch, evidence)
    assert "Responses checked: none. No recorded PayPal responses were found on this computer." in page
    outside = re.sub(r"<details.*?</details>", "", page, flags=re.S)
    assert "the rest differ" not in outside
    assert "differ" not in outside.split("Responses checked")[1][:300]


def test_a_recorded_response_that_differs_is_counted_by_the_run_not_typed(monkeypatch, tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    bad = {"method": "GET", "path": "/v1/customer/disputes/PP-R-X", "status": 200, "body": {"dispute_id": 12}}
    (evidence / "one.jsonl").write_text(json.dumps(bad) + "\n", encoding="utf-8")
    page, dom = _page(monkeypatch, evidence)
    result, _ = configured_run()
    counts = result["response_counts"]
    line = [p.text() for p in dom.find_all("p") if p.text().startswith("Responses checked:")][0]
    assert f"{counts['checked']} recorded PayPal response" in line and (counts["different"] > 0) == ("differ" in line)


def test_the_page_makes_no_network_call(monkeypatch):
    import httpx

    def forbidden(*args, **kwargs):
        raise AssertionError("the contract-checks page must not call out")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    page, dom = _page(monkeypatch)
    assert dom.find("table", data_state="request-checks") is not None


def test_a_row_that_does_not_match_says_why_in_one_clause_from_the_runs_own_issues(monkeypatch):
    page, dom = _page(monkeypatch)
    rows = {r.find("th").text(): r.find_all("td")[-1].text() for r in dom.find("table", data_state="request-checks").find_all("tr")
            if r.attrs.get("data-row") == "request"}
    differing = [v for v in rows.values() if v.startswith("differs")]
    assert differing == ["differs: the field webhook_id does not follow the published pattern"]
    assert rows["Message to the buyer"] == "no schema to check: PayPal publishes no JSON schema for this request"
    assert all(v == "matches" for k, v in rows.items() if not v.startswith(("differs", "no schema")))


@pytest.mark.parametrize("valid, invalid, none, expected", [
    (1, 1, 1, "1 of 3 matches; 1 differs; 1 has no schema to check."),
    (2, 0, 0, "2 of 2 match; 0 differ; 0 have no schema to check."),
    (14, 1, 1, "14 of 16 match; 1 differs; 1 has no schema to check.")])
def test_the_counts_row_grammar_follows_the_count(valid, invalid, none, expected):
    from copilot.features.conformance.routes import request_table
    rows = [{"name": "x", "valid": True, "issues": []}] * valid + [{"name": "x", "valid": False, "issues": []}] * invalid + [
        {"name": "x", "valid": None, "issues": []}] * none
    assert request_table(rows)["counts"] == expected
