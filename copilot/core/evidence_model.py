"""Evidence package generation, reusing pilot/step4_evidence_ai.py's run_with_docs/validate/
enforce_sufficiency UNCHANGED (pilot/ stays intact, per docs/ARCHITECTURE.md). One more check is layered on
top here, in copilot/, rather than by editing pilot/:

- enforce_sufficiency's downgrade is advisory, never a hard block (already true of pilot's own design:
  it feeds review_flags, which the --confirm-review-flags gate already treats as "needs explicit human
  confirmation", not "cannot send"). No change is needed for that part.

- What pilot's validate() does NOT cover (found in review): a claim can assert
  delivery/signature/receipt ("delivered on 2026-09-20") while citing a source that only proves a
  DIFFERENT fact on the same date ("picked up 2026-09-20") - fact_tokens() checks that the date is
  literal in the source, not that the delivery ASSERTION itself is. find_unsupported_delivery_assertions
  closes that gap: it blocks the specific sentence, never the whole package, and applies identically to
  model-written and vendor-edited text alike.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_PILOT_DIR = Path(__file__).resolve().parents[2] / "pilot"
if str(_PILOT_DIR) not in sys.path:
    sys.path.insert(0, str(_PILOT_DIR))

import step4_evidence_ai as ai  # noqa: E402 - path insert must happen first

from copilot import config  # noqa: E402 - see above

# Words that assert the specific fact of delivery/signature/receipt having happened. Deliberately NOT
# "shipped", "in transit", "picked up", "label created" - those are the honest partial responses that
# should remain approvable without a delivery proof.
_DELIVERY_ASSERTION = re.compile(
    r"\b(delivered|signed for|received by|entregad[oa]|firmad[oa]|recibid[oa])\b", re.I)


def _doc_number(source_id: str) -> str | None:
    """'doc_12' -> '12' (the documents table row id, the same number the checklist uses in 'document:12')."""
    return source_id.split("_", 1)[1] if source_id.startswith("doc_") else None


def find_unsupported_delivery_assertions(out: dict, sources: dict[str, str],
                                         untrusted_doc_ids: frozenset | set = frozenset()) -> list[dict]:
    """Returns [{label, text, matched_phrase}] for each claim/notes field that asserts delivery/signature/
    receipt without that specific phrase appearing (case-insensitively, literally) in the relevant source
    text. Empty list = nothing to block. Claims are checked against ONLY their own cited source_ids (same
    scoping pilot's claims_grounded_in_cited_sources uses); notes are checked against the full corpus
    (same scoping pilot's text_facts_literal_in_sources uses) - deliberately mirroring pilot's existing
    scoping rules rather than inventing a new one.

    `untrusted_doc_ids` (numeric document ids, see untrusted_document_ids below) are left OUT of the
    corpus the notes are checked against, so a phrase that only an untrusted document contains (an injected
    "...delivered...") can not make a delivery assertion in the notes look supported. Claims that cite such a
    document are flagged by find_delivery_claims_citing_untrusted_documents, not here."""
    problems = []
    corpus = "\n".join(text for key, text in sources.items() if _doc_number(key) not in untrusted_doc_ids)
    for ev_i, ev in enumerate(out.get("proposed_evidence", [])):
        for c_i, c in enumerate(ev.get("claims", [])):
            cited_text = "\n".join(sources.get(s, "") for s in c.get("source_ids", []))
            for m in _DELIVERY_ASSERTION.finditer(c.get("text", "")):
                if m.group(0).lower() not in cited_text.lower():
                    problems.append({"label": f"proposed_evidence[{ev_i}].claims[{c_i}]",
                                     "text": c["text"], "matched_phrase": m.group(0),
                                     "claim_id": c.get("id"), "kind": "unsupported"})
        for m in _DELIVERY_ASSERTION.finditer(ev.get("notes", "")):
            if m.group(0).lower() not in corpus.lower():
                problems.append({"label": f"proposed_evidence[{ev_i}].notes", "text": ev["notes"],
                                 "matched_phrase": m.group(0), "claim_id": None, "kind": "unsupported"})
    return problems


_CONFLICT_CHECK_IDS = ("tracking_number", "destination")


def find_delivery_claims_citing_conflicting_sources(claims: list, checks: list) -> list[dict]:
    """A delivery/signature/receipt assertion is blocked - exactly like
    find_unsupported_delivery_assertions above - if it cites a document that copilot/core/checklist.py's
    tracking_number or destination check found in CONFLICT with PayPal's own data. PayPal disagreeing with
    that document means it cannot serve as delivery proof, independently of whether the assertion's exact
    phrase is literally present in the document's text.

    `claims`: copilot/db/repo.py's list_claims() shape ({"text", "source_ids", ...}, source_ids like
    "doc_<id>"). `checks`: copilot/core/checklist.py's evaluate() output (Check.sources like
    "document:<id>") - both id spaces are the same documents table row id, compared by that numeric
    string. Never overridable: a conflict_note can acknowledge the checklist conflict itself,
    but can never re-admit a document already disqualified as delivery proof by this rule."""
    conflicting_doc_nums = {src.split(":", 1)[1] for c in checks if c.state == "conflict"
                            and c.id in _CONFLICT_CHECK_IDS for src in c.sources
                            if src.startswith("document:")}
    if not conflicting_doc_nums:
        return []
    problems = []
    for claim in claims:
        claim_doc_nums = {sid.split("_", 1)[1] for sid in claim.get("source_ids", []) if sid.startswith("doc_")}
        if not (claim_doc_nums & conflicting_doc_nums):
            continue
        for m in _DELIVERY_ASSERTION.finditer(claim.get("text", "")):
            problems.append({"label": "claim", "text": claim["text"], "matched_phrase": m.group(0),
                             "claim_id": claim.get("id"), "kind": "conflict",
                             "doc_ids": sorted(claim_doc_nums & conflicting_doc_nums)})
    return problems


_INTEGRITY_CHECK_ID = "document_integrity"


def untrusted_document_kinds(checks: list, extracted_docs: list = ()) -> dict[str, str]:
    """The documents untrusted_document_ids lists, each with WHY - 'untrusted' (the integrity check found
    text that reads like instructions to an AI) or 'unreadable' (text with an illegible field). A document that is both
    counts as 'untrusted'."""
    kinds: dict[str, str] = {}
    for c in checks:
        if c.id == _INTEGRITY_CHECK_ID and c.state not in ("verified", "not_applicable") and c.risk:
            for src in c.sources:
                if src.startswith("document:") and src.split(":", 1)[1] != "manual":
                    kinds[src.split(":", 1)[1]] = "untrusted"
    for d in extracted_docs:
        if (d.doc_id != "manual" and d.raw_text_available
                and any((f or {}).get("confidence") == "illegible" for f in (d.fields or {}).values())):
            kinds.setdefault(str(d.doc_id), "unreadable")
    return kinds


def untrusted_document_ids(checks: list, extracted_docs: list = ()) -> set[str]:
    """The numeric ids of the uploaded documents that must never be the SOURCE of a delivery, signature or
    receipt assertion: (1) those the checklist's `document_integrity` check flags as carrying text that reads like
    instructions to an AI (the check names them in `sources`; that branch is the one marked `risk`), and (2) any
    document that has text but an illegible field, read from the extracted documents because the integrity check
    reports only the injection documents when one exists.

    Deliberately NOT listed: an image the app could not read at all (no extracted text). It has no text a phrase could
    be matched in; the only text it can ever have is the line the vendor themselves confirmed from the picture
    (copilot/core/checklist_input.py, by design: a screenshot the vendor looked at and confirmed
    may support delivery). Making it untrusted would silently undo that feature. The vendor's own typed values
    ('manual') are not a document and are never listed."""
    return set(untrusted_document_kinds(checks, extracted_docs))


def find_delivery_claims_citing_untrusted_documents(claims: list, checks: list, extracted_docs: list = ()) -> list[dict]:
    """Same shape as find_delivery_claims_citing_conflicting_sources: a delivery/signature/receipt assertion
    that cites a document the checks do not trust (untrusted_document_ids) is blocked, whether or not the phrase is
    literally in that document - an injected sentence can contain any phrase. Never overridable. Honest partial claims
    ("shipped", "in transit") never match _DELIVERY_ASSERTION, so they stay approvable."""
    kinds = untrusted_document_kinds(checks, extracted_docs)
    if not kinds:
        return []
    problems = []
    for claim in claims:
        cited = sorted(n for n in {_doc_number(sid) for sid in claim.get("source_ids", [])} if n in kinds)
        if not cited:
            continue
        for m in _DELIVERY_ASSERTION.finditer(claim.get("text", "")):
            problems.append({"label": "claim", "text": claim["text"], "matched_phrase": m.group(0),
                             "reason": "untrusted_document", "claim_id": claim.get("id"),
                             "kind": kinds[cited[0]], "doc_ids": cited})
    return problems


def describe_delivery_problem(problem: dict) -> str:
    """The review-flag sentence for one problem found by the helpers above (shared by create_package and auto_prep so
    both say the same thing)."""
    if problem.get("reason") == "untrusted_document":
        return ('Delivery assertion citing a document the checks do not trust (it contains text that reads like '
                f'instructions, or could not be read): "{problem["text"]}"')
    return f'Unsupported or conflicting delivery assertion: "{problem["text"]}"'


def propose_package(dispute: dict, docs: dict[str, str], names: dict[str, str], seed: int = 7,
                    temperature: float = 0.4, label: str = "copilot") -> dict:
    """One AI-proposed package, via pilot's unchanged pipeline, plus the delivery-assertion check. Never
    raises on a bad model response - that is what validation_ok=False already communicates. CAN raise for
    a genuinely unavailable model/GPU (pilot's preflight(), called unconditionally inside run_with_docs,
    raises RuntimeError when the GPU/Ollama is busy or unreachable) - the caller decides what "unavailable"
    means for it (copilot/web/routes_package.py and copilot/core/auto_prep.py both catch it and fall back to manual
    mode). In the hosted demo it raises before any network attempt (config.refuse_model_if_hosted)."""
    config.refuse_model_if_hosted()
    # pilot/step4_evidence_ai.py is never edited (docs/ARCHITECTURE.md) - its OLLAMA/
    # MODEL are module-level constants pilot's own functions read as plain names from their own module's
    # globals, so setting them here (never at import time - config.ollama_url()/model_name() read the
    # environment fresh every call) genuinely redirects every call pilot makes, including preflight()'s.
    ai.OLLAMA = config.ollama_url()
    ai.MODEL = config.model_name()
    rec = ai.run_with_docs(dispute, docs, names, seed, temperature, label, "app")
    sources = {ai.DISPUTE_ID: json.dumps(dispute, ensure_ascii=False), **docs}
    out = rec.get("_output")
    rec["unsupported_delivery_assertions"] = (find_unsupported_delivery_assertions(out, sources)
                                              if out is not None else [])
    return rec


def claims_from_output(out: dict | None) -> list[dict]:
    """The copilot/db/repo.py-shaped claims list from a propose_package() record's raw model output
    (rec["_output"]), flattened across proposed_evidence/claims - shared by routes_package.create_package
    and copilot/core/auto_prep.py so both persist claims identically. [] for None (nothing to persist)."""
    if out is None:
        return []
    return [{"evidence_index": i, "claim_index": j, "text": c["text"], "source_ids": c["source_ids"],
            "basis": c["basis"]}
           for i, ev in enumerate(out["proposed_evidence"]) for j, c in enumerate(ev["claims"])]
