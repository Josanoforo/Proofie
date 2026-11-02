"""The appeal: built with the fulfilment pattern (approval hash, fresh link, one POST, one GET), behind its own flag.
It is sent at most once per approved request (copilot/core/write_guard.py, taken before the POST). No real call of
register_appeal exists outside the tests: no sandbox dispute of ours has offered an appeal.

Endpoint (PayPal Disputes API, "Appeal a dispute": https://developer.paypal.com/docs/api/customer-disputes/v1/#disputes_appeal):
POST /v1/customer/disputes/{id}/appeal takes the same multipart body as provide-evidence (an `input` JSON part with
`evidences` plus the evidence files), so the body is built by `copilot.core.send.build_request_input` and the parts by
`build_multipart`. PayPal offers the `appeal` link only after a decision for the buyer and only for new information
(help230); the fresh GET must show it, otherwise nothing is sent.
Two hashes, both by code:
* request_sha256(body): the send path's `sha256_json` over the body alone (stored in the row's `request_sha256`);
* approval_sha256(dispute_id, body, attachment_sha256s): the send path's own `approval_fingerprint_with_attachments`
  (copilot/core/send.py, imported, never edited) with the action "appeal". It covers the dispute, the body AND the
  sorted attachment sha256s (order never matters; with no attachment it equals the plain fingerprint). This is what
  the seller approves and what register_appeal recomputes before any client call."""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

from copilot import config
from copilot.core import paypal_words, write_guard
from copilot.core.context import SRC_DISPUTE, Limitation
from copilot.core.disputes import PayPalError, normalize
from copilot.core.send import (approval_fingerprint_with_attachments, build_multipart, build_request_input,
                               sha256_json)
from copilot.features.outcome.read import failure_message

ALLOWED_HOST = "api-m.sandbox.paypal.com"
READ_OP, POST_OP, READBACK_OP = "outcome_appeal_read", "outcome_appeal", "outcome_appeal_readback"
ACTION = "appeal"            # the write_guard action
ALREADY_TEXT = {
    write_guard.SENT: "This exact appeal was already sent; nothing was sent now. Read PayPal's side of the dispute instead of sending it again.",
    write_guard.RESERVED: "This exact appeal is being sent right now; nothing was sent now. Read PayPal's side of the dispute before doing anything else.",
    write_guard.UNKNOWN: "This exact appeal was attempted and PayPal's answer was not known; nothing was sent now. Read PayPal's side of the dispute; do not send it again.",
}
_REQUEST_NS = uuid.UUID("7d0b2f6e-91c4-4a8e-b0d3-5e1f6a2c4b99")


class ApprovalMismatch(ValueError):
    """The approval does not cover this exact body: nothing was sent."""


@dataclass(frozen=True)
class AppealResult:
    status: str          # sent_confirmed | sent_unconfirmed | failed | rejected_link | already_attempted (rejected_hash is the route's 409)
    http_status: int | None
    readback: dict | None
    limitations: tuple = ()
    request_sha256: str = ""
    reason: str = ""
    posted: bool = False     # the POST was attempted (so the attempt is stored and audited)
    source_changed: bool = False


def appeal_body(claims: list, tracking_number: str | None, carrier: str | None) -> dict:
    """Same `input` shape as provide-evidence; ValueError when there is no tracking number to send."""
    return build_request_input(claims, tracking_number, carrier)


def request_sha256(body: dict) -> str:
    return sha256_json(body)


def approval_sha256(dispute_id: str, body: dict, attachment_sha256s: list) -> str:
    """The approval fingerprint: dispute, action "appeal", body and the attachments' content hashes."""
    return approval_fingerprint_with_attachments(dispute_id, "appeal", body, list(attachment_sha256s))


def attachment_sha256s(attachments: list) -> list:
    """The content hash of each attachment: of its bytes when it carries them (never trusted from a label), else its
    own `sha256` field."""
    return [hashlib.sha256(a["data"]).hexdigest() if a.get("data") is not None else a["sha256"] for a in attachments]


def _readback(raw: dict, norm: dict) -> dict:
    outcome = raw.get("dispute_outcome") if isinstance(raw, dict) else None
    return {"stage": norm.get("stage"), "status": norm.get("status"), "links": sorted(norm.get("action_links") or {}),
            "outcome": outcome.get("outcome_code") if isinstance(outcome, dict) else None}


def _link_path(norm: dict, dispute_id: str) -> tuple[str | None, str]:
    link = (norm.get("action_links") or {}).get("appeal")
    if not link or not link.get("href"):
        return None, "PayPal does not offer an appeal for this dispute right now; nothing was sent."
    parsed = urlparse(link["href"])
    if (parsed.scheme != "https" or parsed.netloc != ALLOWED_HOST
            or parsed.path != f"/v1/customer/disputes/{dispute_id}/appeal" or (link.get("method") or "").upper() != "POST"):
        return None, "PayPal gave an unexpected link for this action; nothing was sent."
    return parsed.path, ""


def register_appeal(client, dispute_id: str, body: dict, attachments: list,
                    approval_sha256_value: str, *, db_path=None, package_id=None, receipt_id="") -> AppealResult:
    if config.feature_enabled("active_approval"):
        from copilot.features.outcome import appeal_approval
        if db_path is None or type(package_id) is not int:
            raise appeal_approval.ReceiptError("An appeal-specific receipt and package are required. Nothing was sent.")
        return appeal_approval.register(client, db_path, dispute_id, package_id, receipt_id, body,
                                        attachments, approval_sha256_value, _register_appeal)
    return _register_appeal(client, dispute_id, body, attachments, approval_sha256_value)


def _register_appeal(client, dispute_id: str, body: dict, attachments: list,
                     approval_sha256_value: str, *, _before_post=None) -> AppealResult:
    sha = request_sha256(body)
    if approval_sha256_value != approval_sha256(dispute_id, body, attachment_sha256s(attachments)):
        raise ApprovalMismatch("The approval does not match this request; nothing was sent.")
    try:
        raw = client.call("GET", f"/v1/customer/disputes/{dispute_id}", READ_OP)
    except Exception as exc:  # noqa: BLE001 - classified; programming errors re-raised
        msg = failure_message(exc, "read the dispute")
        if msg is None:
            raise
        return AppealResult("failed", getattr(exc, "status", None), None, (Limitation(SRC_DISPUTE, msg, paypal_words.technical_of(exc)),), sha,
                            "PayPal could not be read; nothing was sent.")
    before = normalize(raw, datetime.now(timezone.utc))
    path, reason = _link_path(before, dispute_id)
    if path is None:
        return AppealResult("rejected_link", None, None, (), sha, reason)
    approved_multipart = None
    if _before_post is not None:
        approved_multipart = build_multipart(body, attachments)
        _before_post(raw)                            # the one-attempt receipt is reserved here (may refuse: nothing taken yet)
    # One send per approved request, however many times it is submitted (copilot/core/write_guard.py, the pattern of the
    # resolution, offer and tracking writes): the key (dispute, "appeal", body sha256) is taken atomically BEFORE the POST.
    claim = write_guard.reserve(dispute_id, ACTION, sha)
    if not claim.reserved:
        return AppealResult("already_attempted", None, None, (), sha, ALREADY_TEXT.get(claim.state, ALREADY_TEXT[write_guard.UNKNOWN]))
    final = write_guard.UNKNOWN                      # until the POST ends the outcome is unknown; a crash leaves it so
    try:
        request_id = str(uuid.uuid5(_REQUEST_NS, f"{dispute_id}:{sha}"))
        try:
            client.call("POST", path, POST_OP, multipart=approved_multipart if _before_post is not None else build_multipart(body, attachments),
                        request_id=request_id)
        except Exception as exc:  # noqa: BLE001
            status_code = getattr(exc, "status", None)
            text = failure_message(exc, "send the appeal")
            if text is None:
                raise
            status = "failed" if isinstance(exc, PayPalError) and isinstance(status_code, int) else "sent_unconfirmed"
            # a 4xx other than 429 is a definite PayPal refusal and frees the key; everything else stays taken, and the
            # seller reads PayPal (GET only) instead of sending again
            if isinstance(exc, PayPalError) and isinstance(status_code, int) and 400 <= status_code < 500 and status_code != 429:
                final = write_guard.REFUSED
            return AppealResult(status, status_code if isinstance(status_code, int) else None, None,
                                (Limitation(SRC_DISPUTE, text, paypal_words.technical_of(exc)),), sha, text, True)
        final = write_guard.SENT
        try:
            after_raw = client.call("GET", f"/v1/customer/disputes/{dispute_id}", READBACK_OP)
        except Exception as exc:  # noqa: BLE001
            msg = failure_message(exc, "read the dispute back")
            if msg is None:
                raise
            return AppealResult("sent_unconfirmed", None, None, (Limitation(SRC_DISPUTE, msg, paypal_words.technical_of(exc)),), sha, msg, True)
        after = normalize(after_raw, datetime.now(timezone.utc))
        changed = (after.get("status"), sorted(after.get("action_links") or {})) !=                   (before.get("status"), sorted(before.get("action_links") or {}))
        return AppealResult("sent_confirmed" if changed else "sent_unconfirmed", None, _readback(after_raw, after), (), sha,
                            "" if changed else "PayPal's answer did not show a change yet; refresh before doing anything else.", True)
    finally:
        write_guard.finish(dispute_id, ACTION, sha, final)
