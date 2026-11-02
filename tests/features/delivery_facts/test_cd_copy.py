"""Copy contracts: actual templates, synthetic states, no transport."""

from html import unescape

from pathlib import Path

import re

from types import SimpleNamespace



import pytest

from copilot.web import status as web_status

from copilot.web.routes_package import templates

from copilot.features.fulfilment.tracker_request import STATUS_LABELS, STATUSES



from copilot.features.resolution import actions, order



ROOT = Path(__file__).resolve().parents[3]

STATUS_CHOICES = tuple((code, STATUS_LABELS[code]) for code in STATUSES)





def existing_view(raw):
    """The form page's `existing` DTO as fulfilment.routes._existing_view builds it."""
    view = web_status.tracking_status(raw)
    return dict(row=dict(id=1, created_at="2026-10-15T18:00:00Z", status=raw), status=raw, label=view.label, kind=view.kind)


def fulfilment_context():
    """The DTOs fulfilment.routes hands to its templates, as synthetic values."""
    tracker = dict(transaction_id="SYNTHETIC", tracking_number="DEMO-1", status="SHIPPED", carrier="FEDEX",
                   carrier_name_other="FedEx")
    return dict(
        values=dict(tracking_number="DEMO-1", carrier="FEDEX", carrier_other_name="", status="SHIPPED",
                    declared_line1="", declared_city="", declared_state="", declared_postal_code="",
                    declared_country_code="", declared_country_other=""),
        form_errors={}, address_open=False, from_token="", notice=None, error_page=False,
        reads=dict(payment_read="ok", tracking_read="ok"), statuses=STATUS_CHOICES,
        existing=existing_view("registered"),
        files=[dict(row=dict(id=1, capture_id="SYNTHETIC", created_at="2026-10-15T18:00:00Z", file_sha256="b" * 64,
                             status="registered"), status="registered")],
        tracking_status=web_status.tracking_status,
        file=dict(id=1, status="registered", created_at="2026-10-15T18:00:00Z", file_sha256="b" * 64,
                  request_sha256="a" * 64),
        status="registered", tracking_view=web_status.tracking_status("registered"), tracker=tracker,
        carrier=dict(text="Carrier: FedEx (PayPal code FEDEX)", aggregator=False),
        readback=dict(tracking_number="DEMO-1", status="DELIVERED", carrier="FEDEX", link_rels=[]),
        limitation_text=None, checks=[], post_result={}, recheck=None, status_labels=STATUS_LABELS,
        check_data=dict(address_comparison=dict(result="match", basis="synthetic"), not_evaluable=[], limitations=[]),
        token="synthetic-token", sha256="a" * 64, declared={}, status_label="Shipped", sent_as=None, notes={},
        ups_advisory=False, delivered_warning=False)


def render(page, **changes):

    env = templates.env.overlay()
    # The isolated copy fixture uses the same filters and vocabulary as real routes.
    env.globals = dict(templates.env.globals)

    hosted = changes.pop("hosted", False)

    features = changes.pop("features", frozenset({"outcome"}))

    env.globals.update(hosted_demo=lambda: hosted, feature_enabled=lambda name: name in features)

    norm = dict(status="WAITING_FOR_SELLER_RESPONSE", stage="INQUIRY", currency="USD")

    history = [dict(id=1, released=False, shown_text="", action="offer", status="sent_confirmed", created_at="2026-10-15T18:00:00Z",

                    request_sha256="a" * 64, limitation_text="")]

    ctx = dict(dispute_id="SYNTHETIC", csrf_token="synthetic-csrf", hosted_demo_sentence="Shared demo",

               view=dict(channel="paypal_message", sentence="PayPal accepts a message on this dispute. Nothing is sent from this page.", template_text="English template", limitations=(), offer_possible=False),

               facts=[], draft=dict(en="English draft", es="Borrador español", problems=[]),

               draft_valid=True, paypal_channel=True, prefill_en="English draft", prefill_es="Borrador español",
               draft_state="valid", box_text="English template", box_has_ai=False, ai_es="Borrador español", written_at="",
               rejected_sentences=[], unknown_channel=False, start_refused=False, model_notice=None, facts_raw=[], stale_labels=(),
               disclaimer="The AI model is an assistant only and can be wrong. You review every word.",

               state=dict(norm=norm, pending=True, actions=SimpleNamespace(offer=True, message=True, defend=True),

                          history=history, next_step_text="next", limitations=[]),

               form_values={}, writes_enabled=True, outcome=dict(readback=dict(status="UNDER_REVIEW", stage="INQUIRY", links=["self"])),

               preview=dict(action="offer", allowed=True, sha256="a" * 64), body_text="approved-body",

               register_enabled=True, capture_id="SYNTHETIC", transaction={})

    ctx.update(fulfilment_context())

    if page == "delivery_facts/page.html":

        ctx["facts"] = dict(capture_id="SYNTHETIC", saved_at="2026-10-15T18:00:00Z", record_status="registered",

                            sha256="b" * 64, request=dict(tracking="DEMO-1", carrier="OTHER", status="SHIPPED",

                            shipment="2026-11-01", updated="2026-10-15T18:00:00Z"), readback=None, discrepancies=["tracking"],

                            address="mismatch", partial=True, english="English facts", spanish="Hechos", comparison=[])

    ctx["tag"] = env.get_template("_components.html").module.tag
    # The resolution templates read these too (the routes build them; the copy fixture states them).
    from copilot.features.resolution import service as resolution_service
    from copilot.web import status as web_status
    ctx.update(message_status=web_status.message_status, errors={}, refusal="", max_amount="40.00", economics=None,
               capture_ids=[], latest_message=None, last_checked=None, checked_note="", already=None, tracking_capture=None,
               delivery_fact_ids=[], token="synthetic-token", origin_labels=resolution_service.ORIGIN_LABELS,
               check=dict(action="offer", request_sha256="a" * 64, origin="seller"), disputed_text="40.00 USD",
               body=dict(offer_amount=dict(value="10.00", currency_code="USD"), note="Synthetic note", message="Synthetic message"))
    # Route DTOs always carry these fields; individual copy cases override only their target state.
    for key in ("facts", "existing", "file", "tracker"):
        if key in changes and isinstance(changes[key], dict) and isinstance(ctx.get(key), dict):
            changes[key] = {**ctx[key], **changes[key]}
    if "files" in changes:
        changes["files"] = [{**ctx["files"][0], **row} for row in changes["files"]]
    ctx.update(changes)
    if page == "delivery_facts/page.html":
        ctx["file_view"] = web_status.tracking_status(ctx["facts"].get("record_status"))
    if page == "fulfilment/result.html" and "status" in changes:
        ctx["tracking_view"] = web_status.tracking_status(changes["status"])
    if page == "fulfilment/form.html":
        # Same policy DTO as the real fulfilment route, after transaction overrides.
        from copilot import config
        from copilot.core import checklist, eligibility
        transaction = ctx["transaction"]
        ctx["shipment_policy"] = eligibility.shipment_information(
            checklist.load_rules(country=config.RULES_COUNTRY),
            transaction.get("total_amount_cents"), transaction.get("total_currency"))

    return env.get_template("features/" + page).render(**ctx)


@pytest.mark.parametrize("enabled", [True, False])
def test_resolution_copy_adapter_does_not_hide_the_review_notice(enabled):
    review = dict(norm=dict(status="UNDER_REVIEW", stage="CHARGEBACK", currency="USD"),
                  pending=False, actions=SimpleNamespace(offer=False, message=False, defend=False),
                  history=[], next_step_text="No supported action.", limitations=[])
    html = render("resolution/resolution.html", state=review, preview=None,
                  features=frozenset({"outcome"}) if enabled else frozenset())
    notice = "PayPal is reviewing this claim. Proofie offers no seller action while the review lasts."
    assert (notice in visible(html)) is enabled
    assert ('data-state="claim-under-review"' in html) is enabled
    assert '<form method="post"' not in html





CASES = [

    ("D1", "draft/draft.html", "No Spanish text: the fixed template is in English only.", {"draft_state": "none"}),

    ("D2", "draft/draft.html", "Proofie does not send this Spanish text. Code checked its numbers and dates, not that it says the same as the English.", {}),

    ("D3", "draft/draft.html", "Review this text on the resolution page (nothing is sent yet)", {}),

    ("D4", "draft/draft.html", "PayPal accepts a message on this dispute. Nothing is sent from this page.", {}),

    ("D5", "draft/draft.html", "PayPal offers no send-message option for this dispute. Copy this text and send it to the buyer yourself.", {"view": dict(channel="copy_only", sentence="PayPal offers no send-message option for this dispute. Copy this text and send it to the buyer yourself."), "paypal_channel": False}),

    ("D6", "draft/draft.html", "Ask the local assistant for a draft (English and Spanish)", {}),

    ("D7", "draft/draft.html", "The AI text was not used", {"draft_state": "rejected"}),

    ("D8", "draft/draft.html", "Starts as the fixed template, written by code from PayPal's data. Edit it as you like; you are responsible for what you send.", {}),

    ("F1", "delivery_facts/page.html", "Saved copy for transaction SYNTHETIC. Proofie did not ask PayPal or the carrier just now.", {}),

    ("F2", "delivery_facts/page.html", 'Read this before you copy it. Nothing has been sent and this page cannot send anything. A tracking number added in PayPal, or a "delivered" label, does not by itself prove the package arrived.', {}),

    ("F3", "delivery_facts/page.html", "What this page is based on Saved on 15 Oct 2026, 12:00 pm (Mexico City time) (the day this copy was saved, not a delivery date)", {}),

    ("F4", "delivery_facts/page.html", "File status Registered and confirmed", {}),

    ("F5", "delivery_facts/page.html", "Fingerprint of the saved copy (sha256)", {}),

    ("F6", "delivery_facts/page.html", 'What you entered is your own statement. "What PayPal returned" is the answer saved when the tracking was added. Nothing here confirms that the whole order was delivered, that the buyer received it, that Seller Protection applies, or how a dispute will end.', {}),

    ("F7", "delivery_facts/page.html", "Your entry and PayPal's answer differ on: tracking number. The addresses do not match. Check both; Proofie does not pick one for you.", {}),

    ("F8", "delivery_facts/page.html", "Partial delivery reported. Do not describe this order as fully delivered.", {}),

    ("F9", "delivery_facts/page.html", "What you entered", {}),

    ("F10", "delivery_facts/page.html", "Nothing saved. Without PayPal's answer, neither the tracking nor the delivery can be confirmed.", {}),

    ("F11", "delivery_facts/page.html", "Tracking number DEMO-1 Carrier Other carrier Status Shipped Ship date 1 Nov 2026", {}),

    ("F12", "delivery_facts/page.html", "Message to copy yourself You can paste this into a message to the buyer or into PayPal. Proofie does not send it. Select and copy the text yourself. Proofie does not copy it or send it, and no AI model wrote it.", {}),

    ("R1", "resolution/resolution.html", "Preparing an offer does not send it. You see the exact request first.", {}),

    ("R2", "resolution/resolution.html", "Offer amount Required", {}),

    ("R3", "resolution/resolution.html", "The buyer reads this in PayPal. Up to 2000 characters. Do not include links or email addresses.", {}),

    ("R4", "resolution/_preview.html", "Not sent yet. This is the exact offer PayPal will show the buyer.", {}),

    ("R5", "resolution/_preview.html", "Change the offer", {}),

    ("R6", "resolution/_preview.html", "Fingerprint of this request (sha256). The record keeps it, so you can match what you approved with what was sent:", {}),

    ("R7", "resolution/_preview.html", "This demo shares one PayPal sandbox account: what you send is visible to everyone who opens it.", {"hosted": True}),

    ("R8", "resolution/resolution.html", "PayPal answered OK; not listed yet. Proofie is waiting for PayPal to update the dispute.", {}),

    ("R9", "resolution/resolution.html", "Check PayPal again", {}),

    ("R10", "resolution/_history.html", "When (Mexico City time) Action Result Details", {}),

    ("R11", "resolution/_history.html", "Offer", {}),

    ("R12", "resolution/_history.html", "PayPal no longer shows the refund-offer option", {}),

    ("R13", "resolution/_history.html", "Proofie keeps what you sent and what PayPal showed afterwards.", {}),

    ("R14", "resolution/_history.html", "Nothing has been sent from this app yet.", {"state": dict(history=[])}),

    ("T1", "fulfilment/result.html", "Tracking registered in PayPal", {}),

    ("T2", "fulfilment/form.html", "Tracking already in PayPal none", {}),

    ("T3", "fulfilment/form.html", "The company that delivers the parcel, for example FedEx, DHL, Estafeta or UPS.", {}),

    ("T4", "fulfilment/check.html", "Not sent yet. This is the prepared request, exactly as Proofie will send it to the PayPal sandbox to add this tracking number to the sale.", {}),

    ("T5", "fulfilment/check.html", "Add tracking in PayPal", {}),

    ("T6", "fulfilment/list.html", "Registered and confirmed", {}),

    ("T7", "fulfilment/result.html", "This is a record of the tracking number. It does not prove the package was delivered.", {}),

    ("T8", "fulfilment/result.html", "Fingerprint of the approved request (sha256):", {}),

    ("T9", "fulfilment/list.html", "Add the tracking number of a sale in PayPal before any dispute exists, and keep the file Proofie prepared. You see the exact request first, and approving it sends it.", {}),

    ("T10", "fulfilment/form.html", "Shipped Delivered On hold Canceled Local pickup", {}),

    ("Q1", "delivery_facts/page.html", "Shipment facts for sale SYNTHETIC", {}),

]





CASES += [

    ("F5-note", "delivery_facts/page.html", "(shows the copy was not altered; it does not show the content is true)", {}),

    ("F9-readback", "delivery_facts/page.html", "What PayPal returned", {}),

    ("R4-button", "resolution/_preview.html", "Send offer to PayPal", {}),

    ("R5-message", "resolution/_preview.html", "Change the message", {"check": dict(action="message", request_sha256="a" * 64, origin="seller")}),

    ("R10-heading", "resolution/_history.html", "What this app recorded", {}),

    ("T1-list", "fulfilment/list.html", "Tracking at the sale: files", {}),

    ("T1-title", "fulfilment/result.html", "Tracking at the sale", {}),

    ("T5-approval", "fulfilment/check.html", "Adding the tracking is the send: there is no separate step.", {}),

    ("T8-file", "fulfilment/result.html", "Fingerprint of this file (sha256):", {}),

    ("T8-word", "fulfilment/result.html", "Status as PayPal showed it when Proofie read the trackers back.", {}),

]





def visible(html):

    # Ignore styles/scripts; compare the rendered text, retaining literal wording.

    html = re.sub(r'<span aria-hidden="true">.*?</span>', "", html, flags=re.S)
    html = re.sub(r"<(style|script)\b[^>]*>.*?</\1>", "", html, flags=re.S)

    text = " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())

    return re.sub(r"\s+([.,;:])", r"\1", text)





@pytest.mark.parametrize("hint,page,text,ctx", CASES, ids=[c[0] for c in CASES])

def test_approved_copy(hint, page, text, ctx):

    assert text in visible(render(page, **ctx)), hint





@pytest.mark.parametrize("hint,constant,text", [

    ("H1", "TEXT_OFFER_SENT", "Offer confirmed by reading the dispute back: waiting for the buyer's reply"),

    ("H2", "TEXT_WAITING_BUYER", "Waiting for the buyer's reply. Proofie has no record of a confirmed offer sent from this app, and PayPal no longer offers the option to make one."),

    ("H3", "TEXT_OFFER_FIRST", "Proofie has seen PayPal remove the refund option after a seller message. If you want to offer a refund, do it first."),

    ("H4", "TEXT_MESSAGE_OPEN", "The option to make an offer is gone. You can still message the buyer or defend with your evidence file."),

], ids=["H1", "H2", "H3", "H4"])

def test_order_copy(hint, constant, text):

    assert getattr(order, constant) == text, hint





def test_cd6_no_default_money_promise():

    text = actions.message_text({}, {}, {"delivery_estimate": "2026-11-01"})

    assert text.endswith("If the item has not arrived by 2026-11-01, reply here and I will look into it right away.")

    assert "refund" not in text





def test_d1_valid_spanish_and_invalid_hosted_fallback():

    assert 'data-state="copy-es"' in render("draft/draft.html")

    for ctx in ({"draft_state": "rejected"}, {"draft_state": "none", "hosted": True}):

        html = render("draft/draft.html", **ctx)

        assert 'data-state="copy-es"' not in html

        assert "No Spanish text: the fixed template is in English only." in html





def test_copy_preserves_raw_preview_approval_and_hosted_warning_condition():

    html = render("resolution/_preview.html", hosted=True)

    for fragment in ['name="csrf_token"', 'name="token"', 'name="approval_sha256"', 'value="offer"', "synthetic-token", "a" * 64]:

        assert fragment in html

    assert "Send offer to PayPal sends it: there is no separate send step." in html

    assert "This demo shares one PayPal sandbox account" not in render("resolution/_preview.html")

    assert "Approve and send" not in render("resolution/_preview.html", preview=dict(action="offer", allowed=False))





@pytest.mark.parametrize("raw,label", [

    ("sent_pending_settle", "PayPal accepted the request; not confirmed yet"),

    ("sent_confirmed", "PayPal lists your message"),

    ("sent_unconfirmed", "Send attempted - not confirmed by PayPal"),

    ("rejected_hash", "Not sent: the approval did not match"),

    ("rejected_link", "Not sent: PayPal did not offer this option at that moment"),

    ("rejected_body", "Not sent: the request was not valid"),

    ("read_failed", "Not sent: PayPal could not be read"),

    ("refused_422", "Not sent: PayPal was still updating the dispute"),

    ("failed", "Not sent: PayPal returned an error"),

])

def test_r12_labels_preserve_raw_history(raw, label):

    row = dict(id=1, released=False, shown_text="", action="message", status=raw, created_at="2026-10-15T18:00:00Z", request_sha256="a" * 64)

    if raw == "sent_confirmed":  # "PayPal lists your message" only when the read-back says message_listed

        row["readback_json"] = '{"message_listed": true}'

    html = render("resolution/_history.html", state=dict(history=[row]))

    assert label in visible(html)

    assert f'data-status="{raw}"' in html and 'data-action="message"' in html and "a" * 64 in html





def test_r3_offer_warning_and_no_form_when_writes_off():

    html = render("resolution/resolution.html")

    assert "Sending a message ends the chance to make an offer on this dispute." in visible(html)

    off = render("resolution/resolution.html", writes_enabled=False)

    assert '<form method="post"' not in off

    assert "Preparing an offer does not send it." not in off

    no_offer = dict(norm={}, actions=SimpleNamespace(offer=False, message=True, defend=True), history=[])

    assert "Sending a message ends the chance to make an offer on this dispute." not in render("resolution/resolution.html", state=no_offer)





def test_f7_no_address_mismatch_claim_for_matching_addresses():

    facts = dict(capture_id="SYNTHETIC", request={}, address="match", discrepancies=["carrier"], record_status="saved_without_registration")

    html = render("delivery_facts/page.html", facts=facts)

    assert "differ on: carrier" in visible(html)

    assert "The addresses do not match." not in html





def test_t5_off_variant_remains_save_only():

    html = render("fulfilment/check.html", register_enabled=False)

    assert "Save the tracking file" in html

    assert "Adding the tracking is the send" not in html

    assert "sandbox to add this tracking" not in html





@pytest.mark.parametrize("raw,label", [("registered", "Registered and confirmed"), ("registered_unconfirmed", "Accepted by PayPal, not confirmed"), ("failed", "Not accepted by PayPal"), ("saved_without_registration", "Saved in Proofie only")])

def test_t6_f4_all_file_states_preserve_raw_values(raw, label):

    # The labels come from the one tracking vocabulary (copilot/web/status.py), shown on every page that names a file state.
    facts = dict(record_status=raw, request={}, capture_id="SYNTHETIC")

    html = render("delivery_facts/page.html", facts=facts)

    assert label in visible(html) and f'data-status="{raw}"' in html

    html = render("fulfilment/form.html", existing=existing_view(raw))

    assert label in visible(html) and f'data-status="{raw}"' in html

    html = render("fulfilment/list.html", files=[dict(status=raw)])

    assert label in visible(html) and f'data-status="{raw}"' in html

def test_t7_not_proof_of_delivery_for_accepted_or_confirmed_only():

    text = "This is a record of the tracking number. It does not prove the package was delivered."

    # Only a registration confirmed by reading PayPal back carries the record sentence; an accepted but
    # unconfirmed or unknown send says it is not confirmed instead.
    assert text in visible(render("fulfilment/result.html", status="registered"))

    for raw in ["registered_unconfirmed", "unknown", "saved_without_registration", "failed"]:

        assert text not in visible(render("fulfilment/result.html", status=raw, readback=None)), raw





def test_r9_and_r13_traceability_without_remote_offer_content_claim():

    html = render("resolution/resolution.html")

    assert 'Check PayPal again' in visible(html) and 'PayPal now shows' not in visible(html)
    assert 'UNDER_REVIEW</details>' not in html

    assert 'links:' not in visible(html)

    empty = render("resolution/_history.html", state=dict(history=[]))

    assert "Each row keeps the fingerprint" not in empty

