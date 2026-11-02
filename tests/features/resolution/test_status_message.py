"""D-101 B5, the prepared status message: the registered tracking written as a message the seller reviews and sends once
through the existing resolution check page, or as a copy-ready note headed "Not sent by Proofie". Fake PayPal, temp database."""
from _hosted import authenticated_client, reset_csrf, set_test_csrf
import html as html_module
import re
from pathlib import Path

import pytest

from copilot import config
from copilot.app import create_app
from copilot.features.fulfilment import store as fulfilment_store
from copilot.features.fulfilment.tracker_request import build_tracker_body
from copilot.features.resolution import actions as act
from copilot.features.resolution import service, status_message
from copilot.web.routes_disputes import get_client
from resolution_fakes import CAPTURE, DISPUTE_ID, FakePayPal

FLAGS = ("COPILOT_FEATURE_RESOLUTION", "COPILOT_FEATURE_RESOLUTION_WRITES", "COPILOT_FEATURE_RESOLUTION_STATUS_MESSAGE")
TOKEN = "test-csrf-token"
HUB = f"/disputes/{DISPUTE_ID}/resolution"
PAGE = HUB + "/status-message"


class NoTrackers(FakePayPal):
    """PayPal lists no tracker for the sale: with no local record either, nothing is prepared."""
    def call(self, method, path, op, **kw):
        if method == "GET" and path.startswith("/v1/shipping/trackers"):
            self.calls.append((method, path, op))
            return {"trackers": []}
        return super().call(method, path, op, **kw)


def _web(monkeypatch, fake=None, status_flag="true", register=True):
    for name, value in zip(FLAGS, ("true", "true", status_flag)):
        monkeypatch.delenv(name, raising=False) if value is None else monkeypatch.setenv(name, value)
    monkeypatch.delenv("COPILOT_FEATURE_ACCOUNT_CLIFF", raising=False)
    fake = fake or FakePayPal()
    if register:
        _register_tracking()
    app = create_app()
    app.dependency_overrides[get_client] = lambda: fake
    web = authenticated_client(app, client=("127.0.0.1", 12345))
    set_test_csrf(web, TOKEN)
    return web, fake


def _register_tracking(status="registered"):
    body = build_tracker_body(CAPTURE, "DEMO-FDX-000001", "FEDEX", None)
    sha = "a" * 64
    fulfilment_store.save_file(CAPTURE, body, sha, sha, None, {}, status)


def _text_of(page_html):
    return html_module.unescape(re.search(r'<textarea[^>]*name="text"[^>]*>(.*?)</textarea>', page_html, re.S).group(1)).strip()


def _review(web, text):
    reset_csrf(web)
    set_test_csrf(web, TOKEN)                  # a page GET replaced the cookie; the form carries TOKEN
    html = web.post(HUB + "/preview", data={"csrf_token": TOKEN, "action": "message", "text": text}).text
    reset_csrf(web)
    set_test_csrf(web, TOKEN)
    return html


def _confirm(html):
    return {"csrf_token": TOKEN, "action": "message",
            "token": re.search(r'name="token" value="([^"]*)"', html).group(1),
            "approval_sha256": re.search(r'data-state="request-sha256">([0-9a-f]{64})<', html).group(1)}


def test_the_text_is_built_by_code_from_the_registered_tracking_and_has_no_link():
    text = status_message.status_text("FEDEX", "DEMO-FDX-000001")
    assert "DEMO-FDX-000001" in text and "FedEx" in text
    assert service.message_text_problem(text) is None
    assert status_message.status_link_for(CAPTURE) is None            # B4 is not on this build: no link rides in the text
    assert "http" not in text and "@" not in text
    assert status_message.status_text("", "") == "Hello, your order has shipped. If it has not arrived, reply here and I will look into it right away."


def test_a_get_shows_the_prepared_message_and_writes_nothing(monkeypatch):
    web, fake = _web(monkeypatch)
    page = web.get(PAGE)
    assert page.status_code == 200 and 'data-state="status-form"' in page.text
    text = _text_of(page.text)
    assert "DEMO-FDX-000001" in text and service.message_text_problem(text) is None
    assert f'action="{HUB}/preview#preview"' in page.text and "Review the message" in page.text
    assert 'data-state="removes-offer"' in page.text                    # the fresh read offers make_offer
    assert fake.posts() == []                                          # a GET writes nothing to PayPal


def test_reviewing_the_prepared_text_stops_at_the_check_page_with_no_write_and_the_offer_warning(monkeypatch):
    web, fake = _web(monkeypatch)
    text = _text_of(web.get(PAGE).text)
    check = _review(web, text)
    assert 'data-state="confirm-form"' in check and 'data-state="message-removes-offer"' in check
    assert check.index('data-state="message-removes-offer"') < check.index("Send message to buyer")
    assert fake.posts() == []                                          # nothing reaches PayPal before the Send click


def test_no_send_without_the_send_message_link(monkeypatch):
    web, fake = _web(monkeypatch, FakePayPal(phase="after_message"))     # PayPal no longer offers send_message
    page = web.get(PAGE)
    assert page.status_code == 200 and 'data-state="status-form"' not in page.text and 'data-state="status-not-offered"' in page.text
    reset_csrf(web)
    set_test_csrf(web, TOKEN)
    refused = web.post(HUB + "/preview", data={"csrf_token": TOKEN, "action": "message", "text": "Your order has shipped."})
    assert 'data-state="confirm-form"' not in refused.text and refused.status_code in (400, 409)
    assert fake.posts() == []


def test_the_approval_hash_changes_when_the_text_is_edited(monkeypatch):
    web, _fake = _web(monkeypatch)
    text = _text_of(web.get(PAGE).text)
    first = _confirm(_review(web, text))["approval_sha256"]
    second = _confirm(_review(web, text + " Thank you."))["approval_sha256"]
    assert first != second
    assert first == act.request_sha256(act.message_body(text))


def test_a_second_send_of_the_same_message_is_refused(monkeypatch):
    web, fake = _web(monkeypatch)
    text = _text_of(web.get(PAGE).text)
    form = _confirm(_review(web, text))
    sent = web.post(HUB + "/register", data=form, follow_redirects=False)
    assert sent.status_code == 303 and len(fake.posts()) == 1
    reset_csrf(web)
    set_test_csrf(web, TOKEN)
    again = web.post(HUB + "/register", data=form, follow_redirects=False)
    assert len(fake.posts()) == 1                                      # the guard answered; nothing went to PayPal
    assert again.status_code in (303, 409)
    reset_csrf(web)
    set_test_csrf(web, TOKEN)
    second_review = _review(web, text)
    assert 'data-state="confirm-form"' not in second_review or "already" in second_review.lower()
    assert len(fake.posts()) == 1


def test_the_copy_ready_note_has_the_not_sent_header_and_the_dated_file_name(monkeypatch):
    web, fake = _web(monkeypatch)
    page = web.get(f"/status-message/{CAPTURE}")
    assert page.status_code == 200 and "Not sent by Proofie" in page.text and "DEMO-FDX-000001" in page.text
    saved = web.get(f"/status-message/{CAPTURE}/note.txt")
    assert saved.status_code == 200
    name = status_message.note_filename()
    assert re.fullmatch(r"not-sent-by-proofie-\d{4}-\d{2}-\d{2}\.txt", name)
    assert saved.headers["content-disposition"] == f'attachment; filename="{name}"'
    assert saved.text.splitlines()[0] == "Not sent by Proofie" and "DEMO-FDX-000001" in saved.text
    assert fake.calls == []                                            # the note never touches PayPal


def test_a_dispute_that_offers_no_message_points_to_the_note(monkeypatch):
    web, _fake = _web(monkeypatch, FakePayPal(phase="after_message"))
    page = web.get(PAGE).text
    assert f'href="/status-message/{CAPTURE}"' in page


def test_without_a_registered_tracking_nothing_is_prepared(monkeypatch):
    web, fake = _web(monkeypatch, NoTrackers(), register=False)
    page = web.get(PAGE).text
    assert 'data-state="status-no-tracking"' in page and "<textarea" not in page
    assert web.get(f"/status-message/{CAPTURE}/note.txt").status_code == 404
    assert fake.posts() == []


def test_an_unconfirmed_registration_is_not_used(monkeypatch):
    web, _fake = _web(monkeypatch, NoTrackers(), register=False)
    _register_tracking("registered_unconfirmed")
    assert 'data-state="status-no-tracking"' in web.get(PAGE).text


def test_with_no_local_record_the_message_is_built_from_paypals_own_tracker_list_by_get_only(monkeypatch):
    web, fake = _web(monkeypatch, register=False)                      # the fake's PayPal lists DEMO-FDX-000001 for the sale
    page = web.get(PAGE)
    assert 'data-state="status-form"' in page.text and "DEMO-FDX-000001" in _text_of(page.text)
    assert any(c[0] == "GET" and c[1].startswith("/v1/shipping/trackers") for c in fake.calls)
    assert fake.posts() == []
    note = web.get(f"/status-message/{CAPTURE}")
    assert "DEMO-FDX-000001" in note.text and fake.posts() == []
    assert fake.calls and all(method == "GET" for method, _path, _op in fake.calls)      # the fallback only ever reads


def test_a_local_registered_record_wins_over_paypals_list(monkeypatch):
    web, fake = _web(monkeypatch)
    fulfilment_store.save_file(CAPTURE, build_tracker_body(CAPTURE, "LOCAL-777", "FEDEX", None), "b" * 64, "b" * 64, None, {}, "registered")
    monkeypatch.setattr(status_message, "paypal_tracking", lambda *a, **k: pytest.fail("the local record answers; PayPal's list is not read"))
    assert "LOCAL-777" in _text_of(web.get(PAGE).text)
    assert fake.posts() == []


@pytest.mark.parametrize("value", [None, "false", "1", ""])
def test_flag_off_gives_the_old_page(monkeypatch, value):
    web, fake = _web(monkeypatch, status_flag=value)
    assert web.get(PAGE).status_code == 404
    assert web.get(f"/status-message/{CAPTURE}").status_code == 404
    assert web.get(f"/status-message/{CAPTURE}/note.txt").status_code == 404
    hub = web.get(HUB).text
    assert "status-message" not in hub
    check = _review(web, "Your order has shipped.")
    assert "message-removes-offer" not in check
    assert fake.posts() == []


def test_flag_on_the_resolution_page_links_to_the_prepared_message(monkeypatch):
    web, _fake = _web(monkeypatch)
    assert f'href="{PAGE}"' in web.get(HUB).text


def test_the_module_is_imported_only_when_its_flag_is_on():
    import subprocess
    import sys
    code = ("import sys\nfrom copilot.app import create_app\ncreate_app()\n"
            "print(sorted(m for m in sys.modules if m.endswith('resolution.status_message')))")
    env = {k: v for k, v in __import__('os').environ.items() if not k.startswith("COPILOT_FEATURE_")}
    env.update(COPILOT_FEATURE_RESOLUTION="true", PYTHON_DOTENV_DISABLED="1")
    root = Path(config.__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(root)
    off = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=str(root))
    assert off.returncode == 0 and off.stdout.strip() == "[]", off.stderr
