"""The clause behind each check: PayPal's own published words for a published_policy check, or a plain label for a
check that is Proofie's own rule or PayPal's request in this case.

Read from the rule file only (`checks.<id>.clause`, copilot/rules/README.md). No model, no network. A clause never
changes a check's state and is not part of the `Check` dataclass, so the approval digest and the MCP output stay as
they are. A published_policy check whose file has no clause yet shows nothing (never a paraphrase).
"""
from __future__ import annotations

from copilot.core.eligibility import citation_text

OWN_RULE = "Proofie's own rule"
CASE_REQUEST = "PayPal asked for this"
# The keys of a `clause` object, the same as an eligibility row's quote plus its citation (flattened).
CLAUSE_KEYS = ("quote", "quote_lang", "section", "page_title", "page_date", "url", "read_date")


def clause_for(rules, check_id: str) -> dict | None:
    """What the page shows under a check: {"kind": "published", quote, quote_lang, citation_text, url},
    {"kind": "own_rule", text} or {"kind": "case_request", text}; None when the rule set has nothing to show."""
    rule = (rules.checks or {}).get(check_id) if rules is not None else None
    if not rule:
        return None
    basis = rule.get("basis")
    if basis == "app_quality_rule":
        return {"kind": "own_rule", "text": OWN_RULE}
    if basis == "case_request":
        return {"kind": "case_request", "text": CASE_REQUEST}
    clause = rule.get("clause")
    if basis != "published_policy" or not clause:
        return None
    return {"kind": "published", "quote": clause["quote"], "quote_lang": clause.get("quote_lang") or "en",
            "citation_text": citation_text(clause), "url": clause.get("url") or None}


def clauses_for(rules, checks) -> dict:
    """check id -> clause_for(...) for the checks on a page (checks with nothing to show are left out)."""
    out = {}
    for check in checks:
        clause = clause_for(rules, check.id)
        if clause:
            out[check.id] = clause
    return out
