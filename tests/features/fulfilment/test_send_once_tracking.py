"""The same approved tracking body is registered with PayPal once.
A repeat click after a registered, unconfirmed or unknown outcome makes no PayPal call; two simultaneous submits make
one POST; a definite PayPal refusal can be approved again; every write POST ends in a 303 redirect (PRG)."""
import threading
import time

import httpx
import pytest

from copilot.core.disputes import PayPalError
from copilot.features.fulfilment import register as reg
from copilot.features.fulfilment import store
from copilot.features.fulfilment.tracker_request import body_from_form, request_sha256
from test_routes import CAP, FORM, FakeClient, _register_data, _sha_of_preview, _web


def _register(web, sha, **kw):
    return web.post(f"/fulfilment/{CAP}/register", data=_register_data(sha), **kw)


def test_register_answers_303_and_the_result_page_is_a_get_that_calls_nothing(monkeypatch):
    web, fake = _web(monkeypatch)
    sha, _ = _sha_of_preview(web)
    r = _register(web, sha, follow_redirects=False)
    assert r.status_code == 303
    row = store.get_file(CAP)
    assert r.headers["location"] == f"/fulfilment/{CAP}/file/{row['id']}"
    calls_before = len(fake.calls)
    page = web.get(r.headers["location"])
    assert page.status_code == 200 and 'data-status="registered"' in page.text
    assert len(fake.calls) == calls_before, "re-showing the saved result must not read or write PayPal"
    assert len(fake.posts()) == 1


def test_repeat_register_after_registered_makes_no_second_post_and_says_so(monkeypatch):
    web, fake = _web(monkeypatch)
    sha, _ = _sha_of_preview(web)
    assert _register(web, sha, follow_redirects=False).status_code == 303
    web2_sha = request_sha256(body_from_form(CAP, FORM["tracking_number"], FORM["carrier"], FORM["status"]))
    calls_before = len(fake.calls)
    again = _register(web, sha, follow_redirects=False)
    assert again.status_code == 303                                           # PRG also when blocked
    assert len(fake.calls) == calls_before and len(fake.posts()) == 1
    page = web.get(again.headers["location"])
    assert page.status_code == 200 and 'data-state="already-registered"' in page.text
    assert "This exact tracking was already registered on" in page.text and "Nothing was sent again" in page.text
    assert len(store.list_files()) == 1
    assert web2_sha == sha


def test_repeat_after_registered_unconfirmed_is_blocked(monkeypatch):
    web, fake = _web(monkeypatch, FakeClient(list_after_post=False))
    sha, _ = _sha_of_preview(web)
    _register(web, sha, follow_redirects=False)
    assert store.get_file(CAP)["status"] == "registered_unconfirmed"
    again = _register(web, sha, follow_redirects=False)
    assert again.status_code == 303 and len(fake.posts()) == 1
    assert 'data-state="already-registered"' in web.get(again.headers["location"]).text


def test_repeat_after_an_unknown_outcome_is_blocked_and_says_to_check_paypal(monkeypatch):
    fake = FakeClient(post_raises=httpx.ConnectError("boom"))
    web, _ = _web(monkeypatch, fake)
    sha, _ = _sha_of_preview(web)
    _register(web, sha, follow_redirects=False)
    assert len(fake.posts()) == 1
    again = _register(web, sha, follow_redirects=False)
    assert again.status_code == 303 and len(fake.posts()) == 1
    page = web.get(again.headers["location"]).text
    assert 'data-state="already-registered"' in page and "Check PayPal" in page


def test_a_definite_paypal_refusal_can_be_approved_again(monkeypatch):
    fake = FakeClient(post_raises=PayPalError(422, "ISSUE", "dbg-9", {}))
    web, _ = _web(monkeypatch, fake)
    sha, _ = _sha_of_preview(web)
    _register(web, sha, follow_redirects=False)
    fake.post_raises = None
    again = _register(web, sha, follow_redirects=False)
    assert again.status_code == 303 and len(fake.posts()) == 2
    assert store.get_file(CAP)["status"] == "registered"


def test_a_different_tracking_number_is_a_different_request(monkeypatch):
    web, fake = _web(monkeypatch)
    sha, _ = _sha_of_preview(web)
    _register(web, sha, follow_redirects=False)
    other = {**FORM, "tracking_number": "TRK2OTHER20261006"}
    sha2, _ = _sha_of_preview(web, other)
    assert sha2 != sha
    r = web.post(f"/fulfilment/{CAP}/register", data=_register_data(sha2), follow_redirects=False)
    assert r.status_code == 303 and len(fake.posts()) == 2


def test_two_threads_registering_the_same_body_make_exactly_one_post():
    class SlowPost(FakeClient):
        def call(self, method, path, op, **kw):
            if method == "POST":
                time.sleep(0.4)
            return super().call(method, path, op, **kw)

    fake = SlowPost()
    body = body_from_form(CAP, "TRK1TEST20261006", "FEDEX", "SHIPPED")
    sha, results, barrier = request_sha256(body), [], threading.Barrier(2)

    def go():
        barrier.wait()
        results.append(reg.register_once(fake, body, sha))

    threads = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(timeout=20) for t in threads]
    kinds = sorted(type(r).__name__ for r in results)
    assert kinds == ["AlreadyRegistered", "RegisterResult"]
    assert len(fake.posts()) == 1


def test_flag_off_save_without_registration_also_redirects(monkeypatch):
    web, fake = _web(monkeypatch, register=None)
    sha, _ = _sha_of_preview(web)
    r = _register(web, sha, follow_redirects=False)
    assert r.status_code == 303 and fake.posts() == []
    assert 'data-status="saved_without_registration"' in web.get(r.headers["location"]).text
