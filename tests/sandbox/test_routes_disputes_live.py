"""The list/detail routes against the REAL sandbox (no fake, no mock). Marked `sandbox`,
never run by default - see tests/sandbox/README.md. Asserts only structural/plain-language properties that
must hold regardless of the sandbox dispute's current real state (never a specific status/amount, which
can legitimately change between runs).
"""
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from copilot.app import create_app

# A real sandbox dispute (created by hand) is passed in by the person running this test; its id is not kept in the
# repository. Without it the live tests are skipped.
KNOWN_DISPUTE_ID = os.environ.get("PROOFIE_KNOWN_DISPUTE_ID", "")

pytestmark = [pytest.mark.sandbox, pytest.mark.skipif(not KNOWN_DISPUTE_ID, reason="set PROOFIE_KNOWN_DISPUTE_ID")]


def _client():
    return TestClient(create_app(), client=("127.0.0.1", 12345))


def test_list_page_renders_against_the_real_sandbox():
    r = _client().get("/disputes")
    assert r.status_code == 200
    assert "Checked PayPal:" in r.text
    assert KNOWN_DISPUTE_ID in r.text   # the known real dispute must show up in the real list


def test_detail_page_renders_the_known_real_dispute():
    r = _client().get(f"/disputes/{KNOWN_DISPUTE_ID}")
    assert r.status_code == 200
    assert KNOWN_DISPUTE_ID in r.text
    assert "Checked PayPal:" in r.text
    # whatever the current real status is, it must be plain language, never the raw PayPal constant
    for raw_constant in ("WAITING_FOR_SELLER_RESPONSE", "WAITING_FOR_BUYER_RESPONSE", "UNDER_REVIEW"):
        main_text_before_details = r.text.split("<details>")[0]
        assert raw_constant not in main_text_before_details


def test_unknown_dispute_id_shows_a_plain_404_not_a_crash():
    r = _client().get("/disputes/PP-R-DOES-NOT-EXIST-0000")
    assert r.status_code == 404
    assert "That dispute was not found" in r.text
    assert "RESOURCE_NOT_FOUND" not in r.text   # PayPal's real issue code must not leak into the page
