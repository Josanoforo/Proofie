"""Read-only contract page registered only with the feature enabled.

The page says in two labelled lines which schema was used and which responses were checked, both built from the
run's own counts (nothing is typed in). The long original sentence, with the pinned date and provenance, and the names of the
environment variables stay inside a closed Technical details block."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from copilot.core import carriers
from copilot.web.routes_disputes import templates
from .suite import configured_run

router = APIRouter()
CAPTION = "Proofie's requests compared with PayPal's published API documents"
WHAT_THIS_MEANS = ("What this means: these checks compare the shape of what Proofie sends with PayPal's published documents, "
                   "pinned 6 Oct 2026. They run on this computer, contact no one and do not check your disputes. You do not "
                   "need to do anything.")
RESULT_WORDS = {True: "matches", False: "differs", None: "no schema to check"}
SAMPLE_NOTE = "a sample; verification is off"
UNNAMED_REQUEST = "A request Proofie does not name"
SPECS, EVIDENCE = "PROOFIE_SPECS_DIR", "PROOFIE_EVIDENCE_DIR"


def schema_line(skipped) -> str:
    if SPECS in skipped:
        return ("Schema used: the short extracts of PayPal's published documents built into Proofie. The full documents are not "
                "set up on this computer, so the longer checks did not run.")
    return "Schema used: PayPal's full published documents, set up on this computer."


def responses_line(counts: dict, skipped) -> str:
    if EVIDENCE in skipped:
        return "Responses checked: Proofie's own sample responses, not responses from PayPal."
    checked = counts["checked"]
    if checked == 0:
        return "Responses checked: none. No recorded PayPal responses were found on this computer."
    matching = counts["valid"] + counts["artefact_only"]
    line = (f"Responses checked: {checked} recorded PayPal response{'s' if checked != 1 else ''}. "
            f"{matching} match{'es' if matching == 1 else ''} the published schema apart from the sanitiser's own placeholders")
    if counts["different"] > 0:
        line += f"; {counts['different']} differ{'s' if counts['different'] == 1 else ''} from it"
    return line + "."


def technical_line(skipped) -> str:
    specs = f"{SPECS} not set or not a folder" if SPECS in skipped else f"{SPECS} set"
    evidence = f"{EVIDENCE} not set" if EVIDENCE in skipped else f"{EVIDENCE} set"
    return f"{specs}; {evidence}."


def request_words(name: str) -> str:
    """The row name of one request fixture, in words (the fixture names are code names: never printed)."""
    fixed = {"multipart-input-nearest-component": "Evidence request sent with a file", "message-prose-only": "Message to the buyer",
             "context-query": "Transaction search from a dispute", "cliff-query": "Sales search for the account check"}
    if name in fixed:
        return fixed[name]
    kind, _, rest = name.partition("-")
    if kind == "evidence" and rest:
        return f"Evidence request ({carriers.display_name(rest)})"
    if kind == "appeal" and rest:
        return f"Appeal request ({carriers.display_name(rest)})"
    if kind == "tracker" and rest:
        return f"Tracking number registration ({carriers.display_name(rest)})"
    if kind == "offer" and rest.isdigit():
        cents = int(rest)
        return f"Refund offer of {cents // 100}.{cents % 100:02d}"
    if kind == "webhook" and rest:
        which = "sample id with hyphens" if "-" in rest else "plain sample id"
        return f"Webhook signature check, {which} ({SAMPLE_NOTE})"
    return UNNAMED_REQUEST


KEYWORD_REASONS = {"pattern": "does not follow the published pattern", "required": "is required and is missing",
                   "type": "has a value of the wrong kind", "enum": "has a value PayPal does not list",
                   "unknown-parameter": "is not a parameter the documents list"}
NO_SCHEMA_REASON = "PayPal publishes no JSON schema for this request"


def _reason(row) -> str:
    """One visible clause for a request row that is not a plain match, from the run's own issues."""
    if row["valid"] is None:
        return NO_SCHEMA_REASON
    issue = (row.get("issues") or [{}])[0]
    field = str(issue.get("pointer") or "").strip("/").split("/")[-1]
    reason = KEYWORD_REASONS.get(issue.get("keyword"), "does not follow the published schema")
    return f"the field {field} {reason}" if field else f"the request {reason}"


def request_table(requests) -> dict:
    """Rows and the one counts row of the request table, every number counted from the run's own rows."""
    rows = [{"name": request_words(r["name"]), "result": RESULT_WORDS[r["valid"]],
             "reason": _reason(r) if r["valid"] is not True else ""} for r in requests]
    valid, invalid, none = (sum(r["valid"] is v for r in requests) for v in (True, False, None))
    counts = (f"{valid} of {len(requests)} {'matches' if valid == 1 else 'match'}; {invalid} "
              f"{'differs' if invalid == 1 else 'differ'}; {none} {'has' if none == 1 else 'have'} no schema to check.")
    return {"rows": rows, "counts": counts}


def mapped_line(mapped) -> str:
    good = sum(not m["issues"] for m in mapped)
    return (f"The DOMINO carrier, as sent in the evidence request and in the appeal: {good} of {len(mapped)} "
            f"{'matches' if good == 1 else 'match'} PayPal's published documents.")


@router.get("/conformance", response_class=HTMLResponse)
def conformance_page(request: Request):
    result, skipped = configured_run()
    return templates.TemplateResponse(request, "features/conformance/checks.html", {
        "sentence": result["sentence"], "schema_line": schema_line(skipped),
        "responses_line": responses_line(result["response_counts"], skipped), "technical": technical_line(skipped),
        "what_this_means": WHAT_THIS_MEANS, "caption": CAPTION, "table": request_table(result["requests"]),
        "mapped_line": mapped_line(result["mapped"])})
