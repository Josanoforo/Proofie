"""draft_view and make_draft on the fake PayPal and a fake model call; GET only, no Ollama."""
import httpx
import pytest

from copilot import config
from copilot.db import repo
from copilot.features.draft import service, store
from copilot.features.resolution import actions as act
from draft_fakes import DISPUTE_ID, EN_NOEST, EN_OK, ES_NOEST, ES_OK, FakePayPal, model_call, paypal_error
from draft_fakes import _no_httpx_calls  # noqa: F401  (autouse: no httpx call in these tests)


def test_the_view_has_the_sheet_the_template_and_no_draft_yet():
    v = service.draft_view(FakePayPal(), config.db_path(), DISPUTE_ID)
    assert v.draft is None and v.error is None
    assert (v.sheet.amount_text, v.sheet.tracking_number, v.sheet.order_date) == ("40.00 USD", "DEMO-FDX-000001", "2026-09-28")
    assert "DEMO-FDX-000001" in v.template_text and "40.00 USD" in v.template_text


def test_the_template_is_exactly_the_resolution_features_own_text():
    from draft_fakes import NORM, TRACK, TXN
    v = service.draft_view(FakePayPal(), config.db_path(), DISPUTE_ID)
    tracking = {k: TRACK[k] for k in ("tracking_number", "carrier", "tracking_status")}
    assert v.template_text == act.message_text(NORM, TXN, tracking)


def test_the_channel_is_paypal_message_with_the_send_message_link_and_copy_only_without_it():
    with_link = service.draft_view(FakePayPal(rels=("send_message",)), config.db_path(), DISPUTE_ID)
    assert with_link.channel == "paypal_message"
    assert with_link.sentence == "PayPal accepts a message on this dispute. Nothing is sent from this page."
    without = service.draft_view(FakePayPal(rels=("provide_evidence",)), config.db_path(), DISPUTE_ID)
    assert without.channel == "copy_only"
    assert without.sentence == "PayPal offers no send-message option for this dispute. You can copy this text and send it to the buyer yourself; PayPal\'s record will not show it."


def test_an_unreadable_dispute_is_the_unknown_channel_with_a_limitation_and_an_empty_sheet():
    v = service.draft_view(FakePayPal(get_raises=paypal_error(500)), config.db_path(), DISPUTE_ID)
    assert v.channel == "unknown" and v.limitations and v.sheet.literals == () and v.template_text == "" and v.sentence == ""


def test_a_timeout_reading_the_dispute_is_a_limitation_and_a_programming_error_raises():
    assert service.draft_view(FakePayPal(get_raises=httpx.ReadTimeout("slow")), config.db_path(), DISPUTE_ID).limitations
    with pytest.raises(KeyError):
        service.draft_view(FakePayPal(get_raises=KeyError("bug")), config.db_path(), DISPUTE_ID)


def test_make_draft_stores_a_valid_draft_and_returns_it_in_the_view():
    call = model_call({"en": EN_NOEST, "es": ES_NOEST})
    v = service.make_draft(FakePayPal(), config.db_path(), DISPUTE_ID, call=call)
    assert v.error is None and v.draft["validation_ok"] is True and v.draft["en"] == EN_NOEST and v.draft["es"] == ES_NOEST
    assert v.draft["source"] == "model" and v.draft["seed"] == 7 and v.draft["raw_sha256"]
    assert v.draft["transaction_id"] == "5S647034CC1979815"
    assert len(call.calls) == 1
    assert store.latest_draft(DISPUTE_ID)["id"] == v.draft["id"]


def test_a_draft_that_names_a_delivery_estimate_the_facts_do_not_have_is_refused():
    v = service.make_draft(FakePayPal(), config.db_path(), DISPUTE_ID, call=model_call())     # EN_OK/ES_OK name 2026-10-10
    assert v.draft["validation_ok"] is False and "the date 2026-10-10 is not in the facts" in " | ".join(v.draft["problems"])


def test_the_model_sees_only_the_fact_sheet():
    call = model_call()
    v = service.make_draft(FakePayPal(), config.db_path(), DISPUTE_ID, call=call)
    messages, _schema, _seed = call.calls[0]
    user = messages[1]["content"]
    for _label, value in v.sheet.labelled():
        assert value in user
    assert "Ana Demo" not in user and "MERCHANDISE_OR_SERVICE_NOT_RECEIVED" not in user      # nothing outside the sheet


@pytest.mark.parametrize("payload,fragment", [
    ({"en": EN_OK + " Ref 98765.", "es": ES_OK}, "the number 98765"),
    ({"en": EN_OK.replace("2026-10-10", "2026-12-24"), "es": ES_OK}, "the date 2026-12-24"),
    ({"en": EN_OK + " See http://x.test", "es": ES_OK}, "no links or emails"),
    ({"en": EN_OK, "es": ES_OK + " Se lo garantizo."}, "promise word"),
    ({"en": EN_OK, "es": ""}, "Spanish: the text is empty")])
def test_a_refused_draft_is_stored_with_its_problems_and_the_template_stands(payload, fragment):
    v = service.make_draft(FakePayPal(), config.db_path(), DISPUTE_ID, call=model_call(payload))
    assert v.error is None and v.draft["validation_ok"] is False
    assert fragment in " | ".join(v.draft["problems"]) and v.template_text.startswith("Hello, this is about your order")


def test_an_unavailable_model_stores_nothing_and_sets_the_error_text():
    v = service.make_draft(FakePayPal(), config.db_path(), DISPUTE_ID, call=model_call(raises=RuntimeError("GPU busy")))
    assert v.error == "Local model unavailable: the template stands" and v.draft is None
    assert store.latest_draft(DISPUTE_ID) is None
    assert [a for a in repo.list_audit(config.db_path(), DISPUTE_ID) if a["action"] == "draft_message"] == []


def test_a_malformed_answer_stores_nothing_and_sets_the_error_text():
    v = service.make_draft(FakePayPal(), config.db_path(), DISPUTE_ID, call=model_call(raw="not json"))
    assert v.error and "could not be read" in v.error and store.latest_draft(DISPUTE_ID) is None


def test_an_unavailable_model_keeps_showing_the_previous_draft():
    service.make_draft(FakePayPal(), config.db_path(), DISPUTE_ID, call=model_call({"en": EN_NOEST, "es": ES_NOEST}))
    v = service.make_draft(FakePayPal(), config.db_path(), DISPUTE_ID, call=model_call(raises=RuntimeError("busy")))
    assert v.error and v.draft is not None and v.draft["en"] == EN_NOEST


def test_one_audit_row_per_model_call_valid_or_refused():
    service.make_draft(FakePayPal(), config.db_path(), DISPUTE_ID, call=model_call({"en": EN_NOEST, "es": ES_NOEST}))
    service.make_draft(FakePayPal(), config.db_path(), DISPUTE_ID, call=model_call({"en": "bad", "es": "mal"}))
    audit = [a for a in repo.list_audit(config.db_path(), DISPUTE_ID) if a["action"] == "draft_message"]
    assert [a["detail"]["validation_ok"] for a in audit] == [True, False] and all(a["actor"] == "system" for a in audit)


def test_the_feature_only_ever_reads_paypal():
    fake = FakePayPal()
    service.make_draft(fake, config.db_path(), DISPUTE_ID, call=model_call())
    assert fake.calls and all(m == "GET" for m, _p, _o in fake.calls)
