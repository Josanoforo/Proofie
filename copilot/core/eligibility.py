"""Eligibility notes: claim-level exclusions and conditions that PayPal's published Seller Protection
terms state, matched by code against the dispute object PayPal returned.

Separate from checklist.py on purpose: a note is not a check. It never changes a check's state, carries no action and
no verdict, and `checklist.evaluate()` never reads it. The rows live in the rule file's `eligibility_notes` section
(copilot/rules/README.md); each one quotes the page verbatim with its citation. No model, no network: the dispute
fields come from the GET the review page already makes.
"""
from __future__ import annotations

from dataclasses import dataclass

from copilot.core.disputes import humanize_reason

# The only dispute-object fields a trigger may name (PayPal Customer Disputes API v1, schema `dispute`).
TRIGGER_FIELDS = ("reason", "dispute_channel")

# Plain language for dispute_channel, taken from the descriptions in the pinned disputes document
# (components.schemas.dispute_channel). Only values a row can trigger on need a text; anything else is shown as-is.
_CHANNEL_TEXT = {
    "INTERNAL": "The buyer filed it with PayPal",
    "EXTERNAL": "The buyer filed it with their card issuer or bank",
    "ALERT": "A pre-chargeback alert: the buyer contacted their card issuer",
}

# The one sentence the block says about every note (the wording may change; the meaning must stay).
EFFECT_SENTENCE = ("These are PayPal's own published words, shown because they match this dispute. They are "
                   "information only: they do not change any check on this page, and only PayPal decides whether "
                   "Seller Protection applies.")


@dataclass(frozen=True)
class EligibilityNote:
    id: str
    quote: str
    quote_lang: str
    lead_in: str | None
    citation_text: str
    matched_on: tuple          # plain-language lines naming the dispute fields that matched, in trigger order
    citation_url: str | None = None   # the row's citation.url: the page the quote was read from


def rows(rules) -> list:
    """The rule set's eligibility rows (an empty list when the file has none)."""
    section = (rules.raw or {}).get("eligibility_notes") or {}
    return list(section.get("rows") or [])


def citation_text(citation: dict) -> str:
    return (f"{citation['page_title']}, last updated {citation['page_date']}, section "
            f"“{citation['section']}” (read {citation['read_date']})")


def _matched_line(field: str, value: str) -> str:
    if field == "reason":
        return humanize_reason(value)
    return _CHANNEL_TEXT.get(value, f"Dispute channel: {value}")


def _matches(trigger: dict, dispute: dict) -> bool:
    if not trigger:
        return False                                   # a row without a trigger never shows
    for field, values in trigger.items():
        if field not in TRIGGER_FIELDS:
            return False
        if dispute.get(field) not in values:
            return False
    return True


def notes_for(dispute: dict | None, rules) -> list:
    """Rows of `rules` whose trigger matches the dispute's own fields (`reason`, `dispute_channel`, as PayPal sent
    them). `dispute` is the raw GET body (normalize() does not keep dispute_channel). No match -> []."""
    if not dispute or rules is None:
        return []
    out = []
    for row in rows(rules):
        if row.get("effect") != "informational" or row.get("basis") != "published_policy":
            continue
        trigger = row.get("trigger") or {}
        if not _matches(trigger, dispute):
            continue
        out.append(EligibilityNote(
            id=row["id"], quote=row["quote"], quote_lang=row.get("quote_lang") or "en",
            lead_in=row.get("lead_in"), citation_text=citation_text(row["citation"]),
            matched_on=tuple(_matched_line(f, dispute.get(f)) for f in trigger),
            citation_url=row["citation"].get("url") or None))
    return out


def shipment_information(rules, total_minor=None, currency=None) -> dict:
    """Source-backed programme information, not an eligibility or shipment verdict."""
    lines = [{"label": row["label"],
              "kind": "required" if row["kind"] == "mandatory" else "required when it applies",
              "citation": row["citation"]}
             for row in rules.checks.values() if row.get("basis") == "published_policy"]
    if not rules.signature_required:
        signature = "not required by the US page"
    elif type(total_minor) is not int or not currency:
        signature = "cannot tell: amount or currency not available"
    else:
        limit = rules.signature_limits.get(str(currency).upper())
        if limit is None:
            signature = "cannot tell: no limit is listed for this currency"
        else:
            # Same strict boundary as checklist._signature: the MX PDF says supera.
            signature = ("needed for this sale" if total_minor > limit["limit_minor"]
                         else "not needed for this amount")
    return {"available": True, "source_title": rules.source_title, "source_url": rules.source_url,
            "lines": lines, "signature": signature,
            "other_requirements": ((rules.raw or {}).get("basic_requirements_not_checked_by_app") or [])
            if rules.country == "US" else []}


__all__ = ["TRIGGER_FIELDS", "EFFECT_SENTENCE", "EligibilityNote", "rows", "citation_text", "notes_for",
           "shipment_information"]
