"""Builds copilot/core/checklist.py's `extracted_docs` input from this app's own data (uploaded documents
plus vendor-typed manual fields), shared by copilot/web/routes_package.py (review/approve) and
copilot/web/routes_send.py (what to actually send) so both always agree on what counts as evidence.

A document marked `excluded` (checklist conflict resolution: "remove the document", copilot/db/repo.py's
set_document_excluded) is left out everywhere here - its row stays for history, only its use stops.
"""
from __future__ import annotations

from copilot.config import RULES_COUNTRY
from copilot.core.checklist import ExtractedDoc, load_rules
from copilot.core.field_extraction import extract_fields
from copilot.db import repo

# The checklist's field vocabulary (copilot/core/checklist.py's module docstring) - also the exact set of
# inputs the manual-fields form accepts, so automatic extraction and manual capture always agree.
MANUAL_FIELD_NAMES = ("tracking_number", "carrier", "delivery_status", "delivery_date", "ship_date",
                     "postal_code", "city", "state", "street", "receiver_name", "signature",
                     "delivery_type", "capture_date")

_RULES_CACHE: list = []   # one-element box; lazy so an in-progress edit to copilot/rules/*.json or
                          # checklist.load_rules() never breaks importing this module, and so a bad
                          # COPILOT_RULES_COUNTRY only ever surfaces when something actually needs rules

# A document we could not read at all (document_ingest.py's
# extraction_ok=False - an image, or a PDF below MIN_MEANINGFUL_TEXT_CHARS) might have said anything about
# these required fields; that is illegible, never missing - "missing" would wrongly claim the
# document said nothing. checklist.py's own per-field resolution already prefers a "read" value from any
# OTHER legible document/manual entry over an "illegible" one (see e.g. its _tracking_number: read beats
# ill beats missing) - so adding this costs nothing when something else already answered the field.
REQUIRED_FIELD_NAMES = ("tracking_number", "carrier", "delivery_status", "delivery_date", "postal_code",
                        "city", "state")


def cached_rules():
    """The one RuleSet every caller of evaluate() should use (routes_package.py, routes_send.py,
    copilot/core/auto_prep.py) - loaded once per process. Raises UnknownCountryError (from
    copilot.core.checklist) for an unconfigured COPILOT_RULES_COUNTRY; callers must catch it and show a
    plain-language message, never let it propagate as a 500 or crash a background task."""
    if not _RULES_CACHE:
        _RULES_CACHE.append(load_rules(country=RULES_COUNTRY))
    return _RULES_CACHE[0]


def extracted_docs_for(db_path, dispute_id: str) -> list[ExtractedDoc]:
    """One ExtractedDoc per non-excluded uploaded document - fields actually read for one that extracted
    text, or illegible on every required field (REQUIRED_FIELD_NAMES) for one that did not,
    EXCEPT any field the vendor has explicitly confirmed a vision suggestion for (copilot/db/repo.py's
    document_confirmations, via confirm_document_reading - never the model itself) - that one field reads
    as confidence="read" with the confirmed value instead, by design: a
    real "Entregado" screenshot the vendor has looked at and confirmed must be able to support delivery,
    the same as a legible document would. Plus one synthetic 'manual' doc for whatever the vendor typed
    by hand. Takes db_path explicitly (not copilot.config.DB_PATH) so callers/tests control which
    database is used - the same reason every copilot/db/repo.py function does."""
    out = []
    for d in repo.list_documents(db_path, dispute_id):
        if d["excluded"]:
            continue
        if d["extraction_ok"]:
            out.append(ExtractedDoc(doc_id=str(d["id"]), fields=extract_fields(d["extracted_text"]),
                                    raw_text_available=True, raw_text=d["extracted_text"]))
        else:
            confirmed = repo.get_confirmed_readings(db_path, d["id"])
            fields = {name: {"value": None, "confidence": "illegible"} for name in REQUIRED_FIELD_NAMES
                     if name not in confirmed}
            fields.update({name: {"value": c["value"], "confidence": "read"}
                          for name, c in confirmed.items()})
            out.append(ExtractedDoc(doc_id=str(d["id"]), fields=fields, raw_text_available=False))
    manual = repo.get_manual_fields(db_path, dispute_id)
    if manual:
        out.append(ExtractedDoc(doc_id="manual", fields=manual, raw_text_available=False))
    return out


def docs_for_dispute(db_path, dispute_id: str) -> tuple[dict[str, str], dict[str, str]]:
    """Only successfully-extracted, non-excluded documents become model input or delivery-check sources (a
    failed extraction is never invented as text) - doc ids are "doc_<document row id>" so they are stable
    and traceable back to the documents table, and line up with checklist Check.sources' "document:<id>"
    by the same numeric id.

    An image document with at least one vendor-confirmed reading (document_confirmations, never the
    model) also gets an entry here - a synthesized "field: value" text, never the image's own pixels -
    so a claim citing it has real, vendor-vouched text to be checked against (copilot/core/
    evidence_model.find_unsupported_delivery_assertions), the same as any other legible document."""
    docs, names = {}, {}
    for d in repo.list_documents(db_path, dispute_id):
        if d["excluded"]:
            continue
        if d["extraction_ok"]:
            key = f"doc_{d['id']}"
            docs[key] = d["extracted_text"]
            names[key] = d["filename"]
        else:
            confirmed = repo.get_confirmed_readings(db_path, d["id"])
            if confirmed:
                key = f"doc_{d['id']}"
                docs[key] = "\n".join(f"{name}: {c['value']}" for name, c in confirmed.items())
                names[key] = d["filename"]
    return docs, names


__all__ = ["MANUAL_FIELD_NAMES", "cached_rules", "extracted_docs_for", "docs_for_dispute"]
