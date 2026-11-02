"""The page's data: PayPal's outcome as it states it, what was sent, what was and is missing, and (only behind its own
flag) the appeal. Reads only except through appeal.register_appeal, which this module never calls."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from copilot import config
from copilot.web.dispute_state import is_buyer_closed, BUYER_CLOSED_LABEL
from copilot.core.checklist_input import extracted_docs_for
from copilot.core.raw_storage import read_raw
from copilot.core.send import pick_tracking
from copilot.db import repo
from copilot.features.outcome import appeal, store
from copilot.features.outcome.file import SentFile, sent_file
from copilot.features.outcome.gaps import Gaps, missing_then_and_now
from copilot.features.outcome.read import OutcomeRead, read_outcome

SENTENCE_UNDECIDED = "PayPal has not decided yet."
SENTENCE_APPEAL = "Appeal window: PayPal shows an appeal link in this dispute; PayPal accepts an appeal only with new information (PayPal Help Center article 230)."


@dataclass(frozen=True)
class OutcomeView:
    read: OutcomeRead
    file: SentFile
    gaps: Gaps
    appeal_allowed: bool
    sentence: str
    appeal_sentence: str | None
    appeals: tuple


@dataclass(frozen=True)
class AppealPreview:
    body: dict | None
    sha256: str | None          # the approval fingerprint: dispute, action, body and attachment content hashes
    attachments: tuple          # dicts: filename, content_type, data, sha256
    reason: str = ""
    body_sha256: str | None = None   # the body alone (stored as the row's request_sha256)


def outcome_sentence(read: OutcomeRead) -> str:
    if read.norm is None:
        return "Outcome not read: PayPal could not be reached, see the limitations below"
    if is_buyer_closed(read.outcome_code):
        return BUYER_CLOSED_LABEL
    if read.decided:
        return f"Decided: {read.label or 'status ' + str(read.status)} as stated by PayPal"
    return SENTENCE_UNDECIDED


def appeal_allowed(read: OutcomeRead) -> bool:
    return bool(read.appeal_link_present and read.decided and config.feature_enabled("outcome_appeal"))


def outcome_view(client, db_path, dispute_id: str) -> OutcomeView:
    read = read_outcome(client, dispute_id)
    capture = ((read.norm or {}).get("seller_transaction_ids") or [None])[0]
    return OutcomeView(read, sent_file(db_path, dispute_id, capture),
                       missing_then_and_now(client, db_path, dispute_id, read.norm), appeal_allowed(read),
                       outcome_sentence(read), SENTENCE_APPEAL if read.appeal_link_present else None,
                       tuple(store.list_appeals(dispute_id)))


def prepare_appeal(db_path, dispute_id: str, package_id: int) -> AppealPreview:
    """The appeal body from a package of this dispute, built by the same code as the evidence send."""
    package = repo.get_package(db_path, package_id)
    if package is None or package["dispute_id"] != dispute_id:
        return AppealPreview(None, None, (), "That package does not belong to this dispute.")
    tracking_number, carrier = pick_tracking(extracted_docs_for(db_path, dispute_id))
    try:
        body = appeal.appeal_body(repo.list_claims(db_path, package_id), tracking_number, carrier)
    except ValueError as exc:
        return AppealPreview(None, None, (), str(exc))
    attachments = []
    for row in repo.list_attachments(db_path, package_id):
        data = read_raw(config.uploads_dir(), row["sha256"])
        if data is None:
            return AppealPreview(None, None, (), f"The attached file \"{row['filename']}\" could not be found on disk.")
        if hashlib.sha256(data).hexdigest() != row["sha256"]:
            return AppealPreview(None, None, (), f"The attached file \"{row['filename']}\" changed on disk since it "
                                                 f"was attached; attach it again.")
        attachments.append({"filename": row["filename"], "content_type": row["content_type"], "data": data,
                            "sha256": row["sha256"]})
    fingerprint = appeal.approval_sha256(dispute_id, body, repo.list_attachment_sha256s(db_path, package_id))
    return AppealPreview(body, fingerprint, tuple(attachments), "", appeal.request_sha256(body))
