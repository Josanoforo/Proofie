"""D-101 P5: the shipment-time page offers to register the tracking with PayPal; the write stays in the approval-bound
fulfilment flow, behind a sandbox-host assertion. No network, no model, no real client."""
import pytest

from copilot.features.ship_moment_capture import service
from copilot.features.fulfilment import register, store
from copilot.features.fulfilment.tracker_request import build_tracker_body, request_sha256

ID = "12345678901234567"
JPEG = ("label.jpg", "image/jpeg", b"\xff\xd8\xffsample")


@pytest.fixture
def uploads(tmp_path, monkeypatch):
    monkeypatch.setattr(service.config, "uploads_dir", lambda: tmp_path)
    monkeypatch.setattr(store, "get_file", lambda capture_id: None)
    return tmp_path


def test_tracking_number_is_saved_cleaned_and_kept_by_a_later_photo_save(uploads):
    saved = service.save(ID, "Estafeta", JPEG, None, tracking_number=" 8 0 1 2 3 4 5 6 ")
    assert saved["tracking_number"] == "80123456"
    later = service.save(ID, "Estafeta", ("n.jpg", "image/jpeg", b"\xff\xd8\xffnew"), None)
    assert later["tracking_number"] == "80123456"


def test_tracking_number_alone_is_a_valid_save_and_an_overlong_one_is_refused(uploads):
    assert service.save(ID, "Estafeta", None, None, tracking_number="80123456")["tracking_number"] == "80123456"
    with pytest.raises(ValueError, match="tracking number"):
        service.save(ID, "Estafeta", JPEG, None, tracking_number="9" * 65)
    with pytest.raises(ValueError, match="Choose a label photo"):
        service.save(ID, "Estafeta", None, None)


def test_offer_states_and_the_exact_code_that_would_be_sent(uploads):
    assert service.registration_offer(ID, None) is None
    photo_only = service.save(ID, "Estafeta", JPEG, None)
    assert service.registration_offer(ID, photo_only)["state"] == "no_number"
    ready = service.registration_offer(ID, service.save(ID, "Estafeta", JPEG, None, tracking_number="80123456"), "PP-R-DEMO-0000001")
    assert ready["state"] == "ready" and ready["code"] == "MEX_ESTAFETA" and ready["carrier_label"] == "Estafeta"
    assert ready["href"] == f"/fulfilment/{ID}?from_dispute=PP-R-DEMO-0000001&prefill_tracking=80123456&prefill_carrier=MEX_ESTAFETA&prefill_status=SHIPPED"
    other = service.registration_offer(ID, service.save(ID, "Mensajeria del Valle", JPEG, None, tracking_number="80123456"))
    assert other["state"] == "other_carrier" and other["code"] == "OTHER"
    assert other["href"] == f"/fulfilment/{ID}?prefill_tracking=80123456&prefill_carrier=OTHER&prefill_carrier_name=Mensajeria%20del%20Valle&prefill_status=SHIPPED"


def test_offer_says_registered_when_a_stored_file_exists(uploads, monkeypatch):
    record = service.save(ID, "Redpack", JPEG, None, tracking_number="ABC12345")
    monkeypatch.setattr(store, "get_file", lambda capture_id: {"id": 7, "status": "registered", "limitation_text": None})
    offer = service.registration_offer(ID, record)
    assert offer["state"] == "registered" and offer["file_href"] == f"/fulfilment/{ID}/file/7"


def test_the_offer_never_calls_paypal_and_the_page_text_says_nothing_is_sent(uploads):
    import inspect
    assert "client" not in inspect.getsource(service.registration_offer).replace("client_", "")
    form = (service.config.ROOT / "copilot" / "web" / "templates" / "features" / "ship_moment_capture" / "form.html").read_text(encoding="utf-8")
    assert "Nothing is sent to PayPal until you review the exact request there and approve it." in form


class FakeClient:
    def __init__(self, base_url=None):
        if base_url is not None:
            self.base_url = base_url
        self.calls = []

    def call(self, method, path, op, **kwargs):
        self.calls.append((method, path))
        if method == "POST":
            return {}
        return {"trackers": [{"transaction_id": ID, "tracking_number": "80123456", "status": "SHIPPED", "carrier": "MEX_ESTAFETA"}]}


def _body():
    return build_tracker_body(ID, "80123456", "MEX_ESTAFETA", None)


@pytest.mark.parametrize("base", ["https://api-m.paypal.com", "https://api-m.paypal.com/", "http://api-m.sandbox.paypal.com",
                                  "https://api-m.sandbox.paypal.com.evil.test", "https://evil.test/api-m.sandbox.paypal.com",
                                  "https://api-m.sandbox.paypal.com:8443", ""])
def test_register_refuses_any_host_but_the_sandbox_before_a_single_call(base):
    client = FakeClient(base)
    with pytest.raises(register.HostRefused):
        register.register_tracker(client, _body(), request_sha256(_body()))
    with pytest.raises(register.HostRefused):
        register.register_once(client, _body(), request_sha256(_body()))
    assert client.calls == []


def test_register_posts_then_reads_back_on_the_sandbox_host_and_on_a_client_without_a_base():
    for client in (FakeClient("https://api-m.sandbox.paypal.com"), FakeClient()):
        result = register.register_tracker(client, _body(), request_sha256(_body()))
        assert result.status == "registered" and result.confirmed
        assert [c[0] for c in client.calls] == ["POST", "GET"]


def test_the_real_client_publishes_the_sandbox_base_url(monkeypatch):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "x")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "y")
    from client import SandboxClient
    assert SandboxClient("p5-test-host").base_url.rstrip("/") == "https://api-m.sandbox.paypal.com"


def test_fulfilment_form_prefills_from_the_link_values_and_drops_anything_that_is_not_clean():
    from copilot.features.fulfilment import routes
    used = routes._query_prefill("80123456", "MEX_ESTAFETA", "")
    assert used["used"] is True and "80123456" in used["sentence"] and "Estafeta" in used["sentence"]
    assert used["values"] == {"tracking_number": "80123456", "carrier": "MEX_ESTAFETA", "carrier_other_name": ""}
    assert routes._query_prefill("80123456", "MEX_ESTAFETA", "", "SHIPPED")["values"]["status"] == "SHIPPED"
    assert "status" not in routes._query_prefill("80123456", "MEX_ESTAFETA", "", "NOPE")["values"]
    other = routes._query_prefill("80 12 34", "OTHER", "  Mensajeria   del Valle ")
    assert other["values"] == {"tracking_number": "801234", "carrier": "OTHER", "carrier_other_name": "Mensajeria del Valle"}
    assert routes._query_prefill("", "MEX_ESTAFETA", "") is None and routes._query_prefill("9" * 65, "FEDEX", "") is None
    assert routes._query_prefill("80123456", "NOT_A_CODE", "x")["values"]["carrier"] == ""


def test_dossier_cites_paypals_tracker_record_only_for_a_verified_registered_file(monkeypatch):
    from copilot.features.delivery_facts import service as facts
    from copilot.features.evidence_dossier import service as dossier
    seen = {"tracking": "80123456", "carrier": "MEX_ESTAFETA", "carrier_name": None, "status": "SHIPPED"}
    good = {"record_status": "registered", "readback": seen, "saved_at": "2026-11-01T18:00:00Z", "sha256": "ab" * 32}
    monkeypatch.setattr(facts, "read", lambda db, capture: good)
    cited = dossier.paypal_tracker_record({"seller_transaction_ids": [ID]})
    assert cited["tracking_number"] == "80123456" and cited["status"] == "SHIPPED" and "does not show" in cited["note"]
    monkeypatch.setattr(facts, "read", lambda db, capture: {**good, "record_status": "registered_unconfirmed"})
    assert dossier.paypal_tracker_record({"seller_transaction_ids": [ID]}) is None
    monkeypatch.setattr(facts, "read", lambda db, capture: {**good, "readback": None})
    assert dossier.paypal_tracker_record({"seller_transaction_ids": [ID]}) is None
    monkeypatch.setattr(facts, "read", lambda db, capture: good)
    assert dossier.paypal_tracker_record({"seller_transaction_ids": [ID, "ABCDEFGHIJKLMNOPQ"]}) is None
    assert dossier.paypal_tracker_record({"seller_transaction_ids": []}) is None

    def missing(db, capture):
        raise facts.FactsMissing("none")
    monkeypatch.setattr(facts, "read", missing)
    assert dossier.paypal_tracker_record({"seller_transaction_ids": [ID]}) is None


def _client(monkeypatch, tmp_path):
    import os
    import re
    from fastapi.testclient import TestClient
    from copilot import config
    config.load_env_file = lambda: None
    for key in list(os.environ):
        if key.startswith("COPILOT_FEATURE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("COPILOT_HOSTED_DEMO", raising=False)
    monkeypatch.setenv("COPILOT_FEATURE_SHIP_MOMENT_CAPTURE", "true")
    monkeypatch.setenv("COPILOT_DATA_DIR", str(tmp_path))
    from copilot.app import create_app
    return TestClient(create_app(), base_url="http://127.0.0.1", client=("127.0.0.1", 12345)), re


def test_page_saves_a_tracking_number_and_then_shows_the_offer_without_calling_paypal(monkeypatch, tmp_path):
    import html
    client, re = _client(monkeypatch, tmp_path)
    path = f"/fulfilment/{ID}/moment"
    page = client.get(path)
    assert page.status_code == 200 and 'name="tracking_number"' in page.text and "Register this tracking with PayPal" not in page.text
    token = html.unescape(re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1))
    saved = client.post(path, data={"csrf_token": token, "carrier": "Estafeta", "tracking_number": "801 234 567"}, follow_redirects=True)
    assert saved.status_code == 200
    text = html.unescape(saved.text)
    assert "Register this tracking with PayPal" in text and "MEX_ESTAFETA" in text and "801234567" in text
    assert f"/fulfilment/{ID}?prefill_tracking=801234567&prefill_carrier=MEX_ESTAFETA&prefill_status=SHIPPED" in text
    assert "Nothing is sent to PayPal until you review the exact request there and approve it." in text


def test_offer_is_one_button_that_posts_the_records_values_to_the_existing_preview(monkeypatch, tmp_path):
    import html
    client, re = _client(monkeypatch, tmp_path)
    path = f"/fulfilment/{ID}/moment"
    token = html.unescape(re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text).group(1))
    page = client.post(path, data={"csrf_token": token, "carrier": "Estafeta", "tracking_number": "801234567"}, follow_redirects=True)
    text = html.unescape(page.text)
    assert f'action="/fulfilment/{ID}/preview"' in text and ">Review what will be sent</button>" in text
    for name, value in (("tracking_number", "801234567"), ("carrier", "MEX_ESTAFETA"), ("status", "SHIPPED")):
        assert f'name="{name}" value="{value}"' in text, name
    assert 'name="csrf_token"' in text
    # the approval is still the seller's click on the check page: nothing here writes to PayPal
    assert "Add tracking in PayPal" not in text
