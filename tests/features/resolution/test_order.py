"""The order rule over synthetic normalised disputes (links shaped like the PayPal sandbox's responses)."""
from copilot.features.resolution import order
import pytest


@pytest.mark.parametrize("record", ["sent_pending_settle", "sent_unconfirmed", "sent_confirmed", None])
def test_h2_distinguishes_no_confirmed_offer_from_an_existing_sent_record(record):
    """Proposed wording, pending a decision; no transport."""
    norm = {"dispute_id": "SYNTHETIC", "status": "WAITING_FOR_BUYER_RESPONSE",
            "action_links": {"send-message": {}}}
    history = [] if record is None else [dict(dispute_id="SYNTHETIC", action="offer", status=record)]
    text = order.next_step_text(norm, history)
    if record == "sent_confirmed":
        assert text == "Offer confirmed by reading the dispute back: waiting for the buyer's reply"
    else:
        assert text == ("Waiting for the buyer's reply. Proofie has no record of a confirmed offer sent from "
                        "this app, and PayPal no longer offers the option to make one.")
        assert "no record of an offer sent" not in text

LINKS = ("accept-claim", "make-offer", "provide-evidence", "send-message")


def _norm(status="WAITING_FOR_SELLER_RESPONSE", links=LINKS):
    return {"status": status, "action_links": {rel: {"href": "https://x/" + rel, "method": "POST"} for rel in links}}


def test_all_links_give_every_action():
    assert order.allowed_actions(_norm()) == order.Actions(offer=True, message=True, defend=True)


def test_after_an_offer_there_is_no_offer_link():
    n = _norm("WAITING_FOR_BUYER_RESPONSE", ("provide-evidence", "send-message"))
    assert order.allowed_actions(n) == order.Actions(offer=False, message=True, defend=True)


def test_underscore_spellings_are_accepted_too():
    n = {"status": "WAITING_FOR_SELLER_RESPONSE", "action_links": {"make_offer": {}, "send_message": {}}}
    assert order.allowed_actions(n) == order.Actions(offer=True, message=True, defend=False)


def test_settling_means_under_review_with_no_link_and_allows_nothing():
    n = _norm("UNDER_REVIEW", ())
    assert order.is_settling(n) is True
    assert order.allowed_actions(n) == order.Actions(False, False, False)
    assert order.is_settling(_norm("UNDER_REVIEW", ("provide-evidence",))) is False
    assert order.is_settling(_norm()) is False


def test_the_five_texts():
    assert order.next_step_text(_norm()) == "Proofie has seen PayPal remove the refund option after a seller message. If you want to offer a refund, do it first."
    assert order.next_step_text(_norm("WAITING_FOR_BUYER_RESPONSE", ("send-message",))) == \
        "Waiting for the buyer's reply. Proofie has no record of a confirmed offer sent from this app, and PayPal no longer offers the option to make one."
    assert order.next_step_text(_norm("UNDER_REVIEW", ())) == "PayPal is updating this dispute, refresh in a minute"
    assert order.next_step_text(_norm("WAITING_FOR_SELLER_RESPONSE", ("provide-evidence",))) == \
        "Defend with your evidence file"
    assert order.next_step_text(_norm("RESOLVED", ())) == "No seller action is open"


def test_message_only_after_the_offer_window_closed_has_its_own_sentence():
    n = _norm("WAITING_FOR_SELLER_RESPONSE", ("send-message", "provide-evidence"))
    assert order.TEXT_MESSAGE_OPEN == ("The option to make an offer is gone. You can still message the buyer or defend with "
                                       "your evidence file.")
    assert order.next_step_text(n) == order.TEXT_MESSAGE_OPEN
    assert order.next_step_text(_norm("WAITING_FOR_SELLER_RESPONSE", ("send-message",))) == order.TEXT_MESSAGE_OPEN


def test_the_message_sentence_does_not_replace_the_offer_waiting_or_settling_texts():
    assert order.next_step_text(_norm("WAITING_FOR_BUYER_RESPONSE", ("send-message", "provide-evidence"))) == \
        "Waiting for the buyer's reply. Proofie has no record of a confirmed offer sent from this app, and PayPal no longer offers the option to make one."
    assert order.next_step_text(_norm()) == "Proofie has seen PayPal remove the refund option after a seller message. If you want to offer a refund, do it first."
    assert order.next_step_text(_norm("UNDER_REVIEW", ())) == "PayPal is updating this dispute, refresh in a minute"
