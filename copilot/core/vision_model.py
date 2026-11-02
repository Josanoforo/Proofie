"""Vision suggestions for an image document. Images are never
OCR'd by document_ingest.py - extraction_ok is always False for them, so checklist_input.py already
treats them as illegible on every required field. This module is a purely OPTIONAL
enhancement layered on top, never a replacement: if the model/GPU preflight fails, or the vision call
itself fails for any reason, propose_image_claims() returns [] and the image stays exactly as illegible
as it already was - callers never need special-case handling for "vision failed", they just get nothing
back and the illegible-document route applies as usual.

Reuses pilot/step4_evidence_ai.py's preflight() and KEEP_ALIVE UNCHANGED (pilot/ is never edited); writes
its own request body because pilot's call_model() never sends images (qwen3.5:9b reports Ollama
capability "vision", confirmed 2026-10-05 via /api/show, GPU free at the time).

Every returned claim is basis="model_interpretation" - the exact same basis copilot/core/evidence_model.py
already uses for AI-proposed package claims, so package_review.html's EXISTING "(Suggested - check
against the source)" label applies with no template change. Nothing here ever auto-confirms a claim: it
is persisted exactly like any other proposed claim, edited or approved only by the vendor (routes_package.py's
existing edit/approve flow, unchanged).

Money is never model-written, full stop (the project rule that numbers are computed by code, never by a
model, extended here to vision): the system prompt forbids it explicitly, and _REJECT_IF_MENTIONS_MONEY is
a second, independent check that drops any claim mentioning an amount/currency even if the model ignores
the instruction - defense in depth, not just a prompt.

Every call is logged to evidence/copilot_vision.jsonl (success and failure alike), the same discipline
pilot/step4_evidence_ai.py's own evidence/step4_ai.jsonl already follows - by the image's sha256, never
its raw bytes.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
import time
from pathlib import Path

_PILOT_DIR = Path(__file__).resolve().parents[2] / "pilot"
if str(_PILOT_DIR) not in sys.path:
    sys.path.insert(0, str(_PILOT_DIR))

import httpx
from client import evidence_dir  # noqa: E402 - pilot/client.py, the one evidence-folder function
import step4_evidence_ai as ai  # noqa: E402 - path insert must happen first; preflight()/KEEP_ALIVE only

from copilot import config  # noqa: E402
from copilot.core.checklist_input import MANUAL_FIELD_NAMES  # noqa: E402
from copilot.core.raw_storage import read_raw  # noqa: E402
from copilot.db import repo  # noqa: E402

SYSTEM = """You are looking at a photo or scan of a shipping/delivery document for a PayPal dispute. \
Describe ONLY what you can actually see about: a tracking number, the carrier name, delivery status \
(e.g. delivered, in transit), a delivery or ship date, a destination address or postal code, who \
received it, or a signature. One short, literal sentence per observation - never invent anything not \
visible, never guess a value you cannot read clearly. You must NEVER state, mention, or imply any price, \
cost, amount of money, or currency value, under any circumstance, even if one is visible in the image - \
that is handled by other data, never by you.

For each observation, if (and only if) it clearly and confidently corresponds to one of these exact \
field names, also set "field" to that name and "value" to the literal value you read (otherwise set both \
to null - never guess a field just to fill it in): """ + ", ".join(MANUAL_FIELD_NAMES) + """.

If nothing from the list above is legible, return an empty list. Respond ONLY with JSON matching the \
given schema."""

SCHEMA = {"type": "object", "properties": {"observations": {"type": "array", "items": {
    "type": "object",
    "properties": {"text": {"type": "string"}, "field": {"type": ["string", "null"]},
                  "value": {"type": ["string", "null"]}},
    "required": ["text", "field", "value"]}}},
         "required": ["observations"]}

# Defense in depth beyond the prompt: any claim even mentioning money is dropped outright, never shown,
# never sent - numbers/amounts are computed by code from PayPal's own data, never by a model.
_REJECT_IF_MENTIONS_MONEY = re.compile(
    r"\$|USD|MXN|EUR|GBP|\bpric(?:e|ing)\b|\bcost\b|\bamount\b|\bcurrency\b|\btotal\b|"
    r"\bprecio\b|\bimporte\b|\bmonto\b|\bcosto\b", re.IGNORECASE)

_CONTENT_TYPE_TO_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif"}

# Every call logged, same discipline as pilot/step4_evidence_ai.py's own evidence/step4_ai.jsonl - by
# image hash, never the raw bytes.
EVIDENCE = evidence_dir()          # PROOFIE_EVIDENCE_DIR, default <repo>/evidence (one function: pilot/client.py)
LOG = EVIDENCE / "copilot_vision.jsonl"


def _log(entry: dict) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def propose_image_claims(image_bytes: bytes, content_type: str, doc_id: str) -> list[dict]:
    """[] on ANY failure (preflight busy/unreachable, bad response, timeout) - the caller never needs to
    branch on failure, the existing handling of an illegible document already covers the image regardless.
    doc_id: the documents table row id as a string, cited as source "doc_<id>" exactly like a text
    document's claims (copilot/core/evidence_model.claims_from_output)."""
    image_sha256 = hashlib.sha256(image_bytes).hexdigest()
    ext = _CONTENT_TYPE_TO_EXT.get(content_type)
    if ext is None:
        _log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "op": "vision_claims", "doc_id": doc_id,
             "image_sha256": image_sha256, "content_type": content_type, "ok": False,
             "reason": "unsupported_content_type"})
        return []
    try:
        config.refuse_model_if_hosted()      # hosted demo: [] and no network attempt, logged under its own prefix
    except RuntimeError as exc:
        _log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "op": "vision_claims", "doc_id": doc_id,
             "image_sha256": image_sha256, "content_type": content_type, "ok": False,
             "reason": f"hosted guard: {exc}"})
        return []
    try:
        ai.preflight()
    except Exception as exc:
        _log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "op": "vision_claims", "doc_id": doc_id,
             "image_sha256": image_sha256, "content_type": content_type, "ok": False,
             "reason": f"preflight: {exc}"})
        return []

    body = {"model": config.model_name(), "stream": False, "think": False, "format": SCHEMA,
           "keep_alive": ai.KEEP_ALIVE,
           "messages": [{"role": "system", "content": SYSTEM},
                        {"role": "user", "content": "Describe only what you can literally see.",
                         "images": [base64.b64encode(image_bytes).decode("ascii")]}],
           "options": {"seed": 7, "temperature": 0.2, "num_predict": 600}}
    t0 = time.perf_counter()
    try:
        r = httpx.post(f"{config.ollama_url()}/api/chat", json=body, timeout=180)
        r.raise_for_status()
        content = r.json().get("message", {}).get("content", "")
        observations = json.loads(content).get("observations", [])
    except Exception as exc:
        _log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "op": "vision_claims", "doc_id": doc_id,
             "image_sha256": image_sha256, "content_type": content_type, "ok": False,
             "reason": f"call: {type(exc).__name__}: {exc}",
             "ms": round((time.perf_counter() - t0) * 1000)})
        return []
    ms = round((time.perf_counter() - t0) * 1000)

    claims, rejected_for_money = [], 0
    for i, obs in enumerate(observations):
        if not isinstance(obs, dict):
            continue
        text = str(obs.get("text") or "").strip()
        if not text:
            continue
        field, value = obs.get("field"), obs.get("value")
        if field not in MANUAL_FIELD_NAMES or not value:
            field = value = None   # never trust a field name outside the known vocabulary, or an empty value
        if (_REJECT_IF_MENTIONS_MONEY.search(text)
                or (value and _REJECT_IF_MENTIONS_MONEY.search(str(value)))):
            rejected_for_money += 1
            continue
        claims.append({"evidence_index": 0, "claim_index": i, "text": text,
                       "source_ids": [f"doc_{doc_id}"], "basis": "model_interpretation",
                       "suggested_field": field, "suggested_value": value})
    _log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "op": "vision_claims", "doc_id": doc_id,
         "image_sha256": image_sha256, "content_type": content_type, "ok": True, "ms": ms,
         "model": config.model_name(), "observations_count": len(observations),
         "claims_count": len(claims), "rejected_for_money": rejected_for_money,
         "observations": [c["text"] for c in claims]})
    return claims


def claims_for_dispute_images(db_path, dispute_id: str, evidence_index: int = 0,
                              on_image_done=None) -> list[dict]:
    """Shared by copilot/web/routes_package.py's create_package (the explicit 'Generate' button) and
    copilot/core/auto_prep.py (the inbox's background preparation) so both treat image documents
    identically. One evidence_index for every vision claim across every image document on this dispute
    (not excluded) - claims_from_output's own evidence_index/claim_index pairing is purely descriptive
    (a claim's real id is the claims table's autoincrement PK; nothing looks these two fields up), this
    just keeps it tidy. [] whenever there are no image documents, or propose_image_claims() returns
    nothing for all of them.

    `on_image_done`, if given, is called after each image is processed (auto_prep.py
    passes a heartbeat here - each image can take ~180s with no cap on how many there are, so "the model
    took a while" alone must never make a genuinely still-running prep look stale). Optional and unused
    by routes_package.py's synchronous caller, which has no heartbeat concept at all."""
    claims = []
    uploads_dir = config.uploads_dir()
    image_docs = [d for d in repo.list_documents(db_path, dispute_id)
                 if not d["excluded"] and d["content_type"].startswith("image/")]
    idx = 0
    for d in image_docs:
        data = read_raw(uploads_dir, d["sha256"])
        if data is None:
            continue
        for c in propose_image_claims(data, d["content_type"], str(d["id"])):
            claims.append({**c, "evidence_index": evidence_index, "claim_index": idx})
            idx += 1
        if on_image_done:
            on_image_done()
    return claims


__all__ = ["claims_for_dispute_images", "propose_image_claims"]
