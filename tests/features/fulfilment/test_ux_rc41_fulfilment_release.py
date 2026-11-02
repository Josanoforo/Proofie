"""The "Prepare the tracking again" action after PayPal does not list a tracking.

The send-once key of an approved tracking body is released only by the rule the resolution flow uses for a missing
message (copilot/core/write_guard.py release_min_age): a successful, complete read that does not list the tracking AND a
POST older than the settle window and at least 10 minutes. Otherwise the key stays taken and the page says "not listed
yet". A release sends nothing; a repeat needs a new check page and a new approval, and it is single use.

Fake client and temp database only; nothing reaches PayPal."""
import re
import sqlite3
import time

import httpx
import pytest

from _hosted import reset_csrf, set_test_csrf
from copilot import config
from copilot.core import write_guard
from copilot.features.fulfilment import register as reg
from copilot.features.fulfilment import store
from test_routes import CAP, FakeClient, TOKEN, _web

pytestmark = [pytest.mark.usefixtures("web_mode")]

FORM = {"csrf_token": TOKEN, "carrier": "FEDEX", "tracking_number": "771234567890", "status": "SHIPPED"}
YET = "PayPal answered and does not list tracking 771234567890 for this sale yet. Proofie will not send it again; check again later."
PREPARE = "Prepare the tracking again"


def _prepared(web):
    response = web.post(f"/fulfilment/{CAP}/preview", data=FORM, follow_redirects=False)
    reset_csrf(web)
    set_test_csrf(web, TOKEN)
    assert response.status_code == 303, response.text[:400]
    token = re.fullmatch(rf"/fulfilment/{CAP}/check/([A-Za-z0-9_-]+)#preview", response.headers["location"]).group(1)
    return token, store.get_check(CAP, token)["request_sha256"]


def _get(web, path):
    response = web.get(path)
    reset_csrf(web)
    set_test_csrf(web, TOKEN)
    return response


def _register(web, token, sha):
    return web.post(f"/fulfilment/{CAP}/register", data={"csrf_token": TOKEN, "token": token, "request_sha256": sha},
                    follow_redirects=False)


def _check_again(web):
    saved = store.get_file(CAP)
    r = web.post(f"/fulfilment/{CAP}/check", data={"csrf_token": TOKEN, "file_id": str(saved["id"])},
                 follow_redirects=False)
    assert r.status_code == 303
    return _get(web, r.headers["location"]).text


def _age_reservation(sha, seconds):
    """Pretend the POST was reserved `seconds` ago (the reservation time is the clock the release rule reads)."""
    conn = sqlite3.connect(str(config.db_path()))
    try:
        conn.execute("UPDATE write_reservations SET created_at = ? WHERE subject_id = ? AND action = ? "
                     "AND request_sha256 = ?", (time.time() - seconds, CAP, reg.ACTION, sha))
        conn.commit()
    finally:
        conn.close()


def _unknown_send(monkeypatch, fake=None):
    fake = fake or FakeClient(post_raises=httpx.ConnectError("down"))
    web, _ = _web(monkeypatch, fake)
    token, sha = _prepared(web)
    assert _register(web, token, sha).status_code == 303
    assert store.get_file(CAP)["status"] == "unknown" and len(fake.posts()) == 1
    fake.post_raises = None                                   # from here on a POST would succeed if one were made
    return web, fake, token, sha


def test_a_too_young_absence_stays_blocked_says_yet_and_sends_nothing(web_mode, monkeypatch):
    web, fake, token, sha = _unknown_send(monkeypatch)
    _age_reservation(sha, 5 * 60)                             # younger than 10 minutes
    page = _check_again(web)
    assert YET in page and PREPARE not in page
    assert write_guard.lookup(CAP, reg.ACTION, sha)["state"] == write_guard.UNKNOWN
    new_token, _ = _prepared(web)                             # a new check page still shows the block
    check = _get(web, f"/fulfilment/{CAP}/check/{new_token}").text
    assert 'data-state="already-registered"' in check and 'data-state="confirm-form"' not in check
    again = _register(web, new_token, sha)
    assert again.status_code == 303 and "already=1" in again.headers["location"]
    assert len(fake.posts()) == 1


def test_an_incomplete_read_never_releases_even_when_the_send_is_old(web_mode, monkeypatch):
    class NoListing(FakeClient):
        def call(self, method, path, op, **kw):
            if method == "GET" and path.startswith("/v1/shipping/trackers"):
                self.calls.append((method, path, op))
                return {}                                     # PayPal answered without a trackers list
            return super().call(method, path, op, **kw)
    web, fake, token, sha = _unknown_send(monkeypatch, NoListing(post_raises=httpx.ConnectError("down")))
    _age_reservation(sha, 3 * 3600)
    page = _check_again(web)
    assert YET in page and PREPARE not in page
    assert write_guard.lookup(CAP, reg.ACTION, sha)["state"] == write_guard.UNKNOWN and len(fake.posts()) == 1


def test_an_old_absence_releases_and_a_repeat_needs_a_new_check_page_and_approval(web_mode, monkeypatch):
    web, fake, token, sha = _unknown_send(monkeypatch)
    _age_reservation(sha, 11 * 60 + 10)
    page = _check_again(web)
    assert ("PayPal answered and does not list tracking 771234567890 for this sale 11 minutes after it was sent. "
            "You can prepare it again; nothing is sent automatically.") in page
    assert PREPARE in page and len(fake.posts()) == 1         # the release itself sends nothing
    assert write_guard.lookup(CAP, reg.ACTION, sha)["state"] == write_guard.REFUSED
    # the earlier check page (its approval) is spent: re-posting it is refused and sends nothing
    stale = _register(web, token, sha)
    assert stale.status_code == 409 and len(fake.posts()) == 1
    # a new check page shows the confirm form again; approving it sends once
    new_token, _ = _prepared(web)
    check = _get(web, f"/fulfilment/{CAP}/check/{new_token}").text
    assert 'data-state="confirm-form"' in check and 'data-state="already-registered"' not in check
    sent = _register(web, new_token, sha)
    assert sent.status_code == 303 and len(fake.posts()) == 2
    assert store.get_file(CAP)["status"] == "registered"
    # the release is single use: the same approval cannot send again
    blocked = _register(web, new_token, sha)
    assert blocked.status_code == 303 and "already=1" in blocked.headers["location"] and len(fake.posts()) == 2


def _set_reservation_state(sha, state):
    conn = sqlite3.connect(str(config.db_path()))
    try:
        conn.execute("UPDATE write_reservations SET state = ? WHERE subject_id = ? AND action = ? AND request_sha256 = ?",
                     (state, CAP, reg.ACTION, sha))
        conn.commit()
    finally:
        conn.close()


def test_an_old_reserved_key_is_never_released_and_no_prepare_link_is_offered(web_mode, monkeypatch):
    """A send in flight (RESERVED: for example a process that stopped before it recorded the end) is never freed by a
    check, however old it is: the key stays taken, the old approval is not spent, and the page offers no new preparation.
    The route guard and write_guard.release_after_absence each protect it; this test fails if both are removed."""
    web, fake, token, sha = _unknown_send(monkeypatch)
    _set_reservation_state(sha, write_guard.RESERVED)
    _age_reservation(sha, 3 * 3600)
    assert write_guard.release_after_absence(CAP, reg.ACTION, sha) is False           # the guard itself, called directly
    assert write_guard.lookup(CAP, reg.ACTION, sha)["state"] == write_guard.RESERVED
    page = _check_again(web)
    assert PREPARE not in page and "can prepare it again" not in page and "prepare it again" not in page.lower()
    assert write_guard.lookup(CAP, reg.ACTION, sha)["state"] == write_guard.RESERVED
    assert store.get_check(CAP, token) is not None                                    # the old approval is not spent
    again = _register(web, token, sha)
    assert again.status_code == 303 and "already=1" in again.headers["location"] and len(fake.posts()) == 1


def test_the_fulfilment_release_age_is_its_own_pinned_ten_minutes():
    """Fulfilment reads its own constant (routes.RELEASE_MIN_AGE_SECONDS), so a change of write_guard that
    the resolution test pins is not enough. Trackers have no settle window: the floor of write_guard governs, 600 s."""
    from copilot.features.fulfilment import routes as fulfilment_routes
    assert fulfilment_routes.RELEASE_MIN_AGE_SECONDS == 600
    assert fulfilment_routes.RELEASE_MIN_AGE_SECONDS == write_guard.release_min_age(0) == write_guard.RELEASE_FLOOR_SECONDS
