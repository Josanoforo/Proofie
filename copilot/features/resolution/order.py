"""The order rule, as pure functions over a normalised dispute (copilot.core.disputes.normalize).

Sandbox behaviour observed while testing:
* a fresh INR dispute in INQUIRY + WAITING_FOR_SELLER_RESPONSE carries the links accept_claim, the escalation link,
  make_offer (allowed offer_type REFUND only), provide_evidence and send_message;
* make-offer as the first seller write answers 200; afterwards the make_offer link is gone and the status is
  WAITING_FOR_BUYER_RESPONSE; a seller message also removes the offer link;
* after ANY seller write the dispute shows UNDER_REVIEW with only `self` for about 15 to 60 s, and a second write
  inside that window answers 422 ACTION_NOT_ALLOWED_IN_CURRENT_DISPUTE_STATE.
`normalize` keeps PayPal's rels with hyphens ("make-offer"); both spellings are accepted here. Nothing in this
feature ever uses PayPal's dispute-escalation link."""
from __future__ import annotations

from dataclasses import dataclass

LINK_OFFER, LINK_MESSAGE, LINK_DEFEND = "make-offer", "send-message", "provide-evidence"
STATUS_SETTLING, STATUS_WAITING_BUYER = "UNDER_REVIEW", "WAITING_FOR_BUYER_RESPONSE"

TEXT_OFFER_FIRST = ("Proofie has seen PayPal remove the refund option after a seller message. "
                    "If you want to offer a refund, do it first.")
TEXT_OFFER_SENT = "Offer confirmed by reading the dispute back: waiting for the buyer's reply"
TEXT_WAITING_BUYER = ("Waiting for the buyer's reply. Proofie has no record of a confirmed offer sent from this app, "
                      "and PayPal no longer offers the option to make one.")
TEXT_SETTLING = "PayPal is updating this dispute, refresh in a minute"
TEXT_MESSAGE_OPEN = ("The option to make an offer is gone. You can still message the buyer or defend with "
                     "your evidence file.")
TEXT_DEFEND = "Defend with your evidence file"
TEXT_NONE_OPEN = "No seller action is open"


@dataclass(frozen=True)
class Actions:
    offer: bool
    message: bool
    defend: bool


def has_link(norm: dict, rel: str) -> bool:
    links = norm.get("action_links") or {}
    return rel in links or rel.replace("-", "_") in links


def allowed_actions(norm: dict) -> Actions:
    return Actions(has_link(norm, LINK_OFFER), has_link(norm, LINK_MESSAGE), has_link(norm, LINK_DEFEND))


def is_settling(norm: dict) -> bool:
    """UNDER_REVIEW with no link other than `self` (normalize drops `self`)."""
    return norm.get("status") == STATUS_SETTLING and not (norm.get("action_links") or {})


def next_step_text(norm: dict, history=()) -> str:
    a = allowed_actions(norm)
    if a.offer:
        return TEXT_OFFER_FIRST
    if norm.get("status") == STATUS_WAITING_BUYER:
        confirmed_offer = any(
            row.get("dispute_id") == norm.get("dispute_id") and norm.get("dispute_id")
            and row.get("action") == "offer" and row.get("status") == "sent_confirmed"
            for row in history
        )
        return TEXT_OFFER_SENT if confirmed_offer else TEXT_WAITING_BUYER
    if is_settling(norm):
        return TEXT_SETTLING
    if a.message and not a.offer:
        return TEXT_MESSAGE_OPEN
    if a.defend:
        return TEXT_DEFEND
    return TEXT_NONE_OPEN
