"""Send-side logic reused from pilot/step5_send_evidence.py, extended where the app needs more than the pilot
did. pilot/ is not imported as a package (it has no __init__.py, by design - see pilot/'s own layout); its
directory is added to sys.path once here so its pure functions can be reused without copy-pasting them.

Pilot's approval_fingerprint never had attachments (the pilot never attached a
file - provide_evidence always went out with only the JSON `input` part). The app adds attachments, so the
fingerprint must cover them: this module's approval_fingerprint_with_attachments wraps the same hashing
primitive (sha256_json) pilot uses, extended with the sorted list of attachment sha256s.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

_PILOT_DIR = Path(__file__).resolve().parents[2] / "pilot"
if str(_PILOT_DIR) not in sys.path:
    sys.path.insert(0, str(_PILOT_DIR))

from step5_send_evidence import (DEMO_PREFIX, RETRYABLE, check_preconditions, classify,  # noqa: E402
                                 request_id_for, sha256_json)

from copilot.core.carriers import evidence_carrier, map_carrier  # noqa: E402

ALLOWED_SEND_HOST = "api-m.sandbox.paypal.com"
NOT_ASKING_MESSAGE = "PayPal is not asking for evidence on this case right now."


def approval_fingerprint_with_attachments(dispute_id: str, action: str, req_input: dict,
                                          attachment_sha256s: list[str]) -> str:
    """Extends pilot's approval_fingerprint(dispute_id, action, req_input) with attachment content: any
    change to the dispute target, the action, the request body, OR which files (by content) are attached
    invalidates a previously computed fingerprint. Attachment order never matters - sorted() means
    re-attaching the same files in a different order does NOT spuriously invalidate an approval, but
    adding, removing, or swapping an attachment for different content always does.

    With zero attachments this returns EXACTLY what pilot's own approval_fingerprint would (the
    "attachment_sha256" key is omitted rather than set to an empty list) - a package that never attaches
    anything, which is everything the pilot ever sent, is unaffected by this extension."""
    payload = {"dispute_id": dispute_id, "action": action, "paypal_request_input": req_input}
    if attachment_sha256s:
        payload["attachment_sha256"] = sorted(attachment_sha256s)
    return sha256_json(payload)


def pick_tracking(extracted_docs: list) -> tuple[str | None, str | None]:
    """The tracking_number/carrier this package would actually send, from the same ExtractedDoc list the
    checklist evaluates (copilot/core/checklist_input.py) - a document's reading is preferred over a
    manual-typed one (doc_id != 'manual') when both exist, since a document is harder to mistype than a
    form field; the first non-'manual' hit wins, else the manual one."""
    manual_tn = manual_carrier = None
    for doc in extracted_docs:
        tn = (doc.fields.get("tracking_number") or {}).get("value")
        car = (doc.fields.get("carrier") or {}).get("value")
        if doc.doc_id == "manual":
            manual_tn, manual_carrier = manual_tn or tn, manual_carrier or car
            continue
        if tn:
            return tn, car
    return manual_tn, manual_carrier


def build_request_input(claims: list, tracking_number: str | None, carrier_value: str | None) -> dict:
    """PayPal's `input` body for POST provide-evidence: only PROOF_OF_FULFILLMENT with tracking is
    supported (same limitation pilot/step5_send_evidence.py's build_request_input has - this app has never
    built evidence of any other PayPal evidence_type). Raises ValueError when there is nothing to send
    yet - the caller must not send, and should say so in plain language."""
    if not tracking_number:
        raise ValueError("no tracking number available yet (upload a document or type one in)")
    m = map_carrier(carrier_value)
    carrier_name, other = evidence_carrier(m.code, m.carrier_name_other)
    tracking = {"carrier_name": carrier_name, "tracking_number": tracking_number}
    if other:
        tracking["carrier_name_other"] = other
    notes = (DEMO_PREFIX + " ".join(c["text"].strip() for c in claims if c.get("text", "").strip())).strip()
    if len(notes) > 2000:
        notes = notes[:2000]
    return {"evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT",
                           "evidence_info": {"tracking_info": [tracking]}, "notes": notes}]}


# The multipart/form-data field for a binary evidence file on POST provide-evidence. Two official-looking
# PayPal sources disagreed before this was confirmed for real:
#   - developer.paypal.com/api/customer-disputes/v1/schema.json (the live OpenAPI schema served by
#     PayPal's own domain, fetched twice independently, same answer both times) names the ONLY documented
#     multipart property "evidence-file" (binary), with no "input" JSON property at all in that schema.
#   - developer.paypal.com/platforms/disputes/handle-disputes/use-disputes-api (the prose integration
#     guide) instead shows a literal, runnable curl example using "input" (JSON) + "file1" (the binary
#     file), which is closer to what this app and pilot/ have always sent for "input".
# A real sandbox send and its follow-up GET confirmed that "evidence-file" is the accepted
# multipart field: the returned evidence entry included the submitted document.
EVIDENCE_FILE_FIELD = "evidence-file"


def build_multipart(req_input: dict, attachments: list) -> list:
    """The httpx `files` value for POST provide-evidence, as a LIST of (field_name, value) tuples rather
    than a dict - EVIDENCE_FILE_FIELD repeats verbatim once per attachment (proven against httpx directly:
    a dict could only ever hold one value per key, a list lets the same field name appear more than once,
    which is how multipart/form-data represents multiple files under one field).
    attachments: [{"filename": str, "content_type": str, "data": bytes}, ...], already read from disk."""
    parts = [("input", (None, json.dumps(req_input, ensure_ascii=False), "application/json"))]
    for att in attachments:
        parts.append((EVIDENCE_FILE_FIELD, (att["filename"], att["data"], att["content_type"])))
    return parts


PARTIAL_MESSAGE = "PayPal received the details but not the attached file — check the attachment."
# The same headline when two or more files are missing. copilot/core/send_recovery.py chooses between the two with
# partial_message(); this module is where both are defined so there is one copy of each.
PARTIAL_MESSAGE_MANY = "PayPal received the details but not the attached files — check the attachments."
# The reason an attempt is left 'sent_unconfirmed' with when PayPal lists the statements but not every file; the names of the
# missing files follow, comma separated (read back by copilot/web/status.py as MISSING_FILE_PREFIX).
MISSING_ATTACHMENT_PREFIX = "missing_attachment:"


def verify_after_with_attachments(raw_dispute: dict, req_input: dict, attachment_filenames: list) -> dict:
    """Confirms our EXACT content (evidence_type, tracking number, notes) and every attached file landed -
    Note: if PayPal silently drops a misnamed multipart file part, a package with attachments
    would otherwise look exactly like a fully successful send.

    It does its OWN full scan over raw_dispute["evidences"] rather than delegating to pilot's
    verify_after, which returns on the FIRST SUBMITTED_BY_SELLER entry whose evidence_type/tracking number
    match - regardless of notes. When an earlier attempt (different claim text, so different notes) left a
    different entry with the same evidence_type/tracking ahead of ours in the list, pilot's function would
    report THAT one's notes_match as false and stop looking, never reaching our own, later, correctly-
    matching entry. This scans every candidate and accepts the first one whose notes ALSO match - files_ok
    is then checked against THAT SAME matched entry's documents[], never a blind union across every
    tracking-matching entry (an unrelated earlier attempt happening to reuse a filename must not count).
    pilot/step5_send_evidence.py itself is untouched - it has its own, intentionally simpler, single-match
    verify_after for the pilot's own callers.

    Returns {"content_ok": bool, "files_ok": bool, "missing_files": [...], "details": {...}}; files_ok is
    vacuously True when attachment_filenames is empty."""
    want = req_input["evidences"][0]
    want_trk = want["evidence_info"]["tracking_info"][0]["tracking_number"]
    want_notes = want["notes"].strip()
    candidates = []
    for e in raw_dispute.get("evidences") or []:
        if e.get("source") != "SUBMITTED_BY_SELLER" or e.get("evidence_type") != want["evidence_type"]:
            continue
        trks = [t.get("tracking_number") for t in ((e.get("evidence_info") or {}).get("tracking_info") or [])]
        if want_trk in trks:
            candidates.append((e, trks))
    match = next(((e, trks) for e, trks in candidates if (e.get("notes") or "").strip() == want_notes), None)
    if match is None:
        content_ok, details = False, {"seller_submitted": [(e.get("evidence_type"), e.get("source"))
                                                            for e in raw_dispute.get("evidences") or []]}
    else:
        e, trks = match
        content_ok, details = True, {"evidence_type": e["evidence_type"], "tracking_numbers": trks,
                                     "date": e.get("date"), "notes_match": True,
                                     "documents_present": sorted(d.get("name") for d in (e.get("documents") or [])
                                                                 if d.get("name"))}
    if not attachment_filenames:
        return {"content_ok": content_ok, "files_ok": True, "missing_files": [], "details": details}
    if not content_ok:
        return {"content_ok": False, "files_ok": False, "missing_files": list(attachment_filenames),
                "details": details}
    # PayPal lists documents by name only, so the names are compared as a multiset: a wanted name is missing when
    # it was attached more often than PayPal lists it (two different files named label.pdf are not confirmed by one listed
    # label.pdf). A false 'partial' never re-sends anything; a false 'confirmed' would be the harm.
    present = [d.get("name") for d in (match[0].get("documents") or []) if d.get("name")]
    available = Counter(present)
    missing = []
    for name in attachment_filenames:
        if available[name] > 0:
            available[name] -= 1
        else:
            missing.append(name)
    return {"content_ok": True, "files_ok": not missing, "missing_files": missing,
           "details": {**details, "documents_present": sorted(present)}}


def readback_record(details: dict | None) -> dict | None:
    """What a confirmed write keeps of PayPal's read (attempt_store `readback`): the evidence type, the names PayPal
    lists with the matching evidence, PayPal's own date text and the time Proofie read it. None when the read matched
    nothing (details carries no evidence_type). Names only, never contents."""
    if not isinstance(details, dict) or not details.get("evidence_type"):
        return None
    return {"evidence_type": details["evidence_type"], "documents_present": list(details.get("documents_present") or []),
            "paypal_date": details.get("date"), "read_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def validate_send_link(norm: dict) -> str | None:
    """None if PayPal's own provide-evidence link (from a FRESH GET) is present and on
    the sandbox host/path/method allowlist; otherwise the plain-language reason nothing will be sent. The
    route calling this must never POST when it returns non-None - this is the one gate between a human's
    approval and a real network call to PayPal."""
    link = (norm.get("action_links") or {}).get("provide-evidence")
    if not link or not link.get("href"):
        return NOT_ASKING_MESSAGE
    href, method = link["href"], (link.get("method") or "").upper()
    parsed = urlparse(href)
    if parsed.scheme != "https" or parsed.netloc != ALLOWED_SEND_HOST:
        return "PayPal gave an unexpected link for this action; nothing was sent."
    if not parsed.path.startswith("/v1/customer/disputes/") or not parsed.path.endswith("/provide-evidence"):
        return "PayPal gave an unexpected link for this action; nothing was sent."
    if method != "POST":
        return "PayPal gave an unexpected link for this action; nothing was sent."
    return None
