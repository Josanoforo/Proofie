"""LINK-FIX2 F3: a package whose latest approval row holds an old placeholder instead of a 64-hex fingerprint (a row from the
first pilot days: `pending-gate`) made the print page answer 400 "The approval record is inconsistent", so the seller could not
print a package they could read everywhere else.

Expected, written before the first run (fails on 1f38178c with 400, every time):
- print answers 200 with that row present, twice in a row; the page shows no approval mark (no fingerprint, no "Approved");
- a real 64-hex approval still prints with its fingerprint (unchanged);
- nothing in the data folder changes (read-only page).
"""
import pytest

from copilot.db import repo

from .test_print import files, prepared, url  # noqa: F401  (prepared is a fixture)

REAL = "ab" * 32


def _approval_lines(html):
    return [line for line in html.splitlines() if REAL in line or "pending-gate" in line]


def test_a_legacy_placeholder_fingerprint_prints_without_an_approval_mark(prepared):
    client, db, uploads, pid, doc, sha = prepared
    repo.insert_approval(db, pid, "pending-gate", "vendor")
    before = files(db.parent)
    for _ in range(2):
        response = client.get(url(pid))
        assert response.status_code == 200, response.text[:400]
        assert "pending-gate" not in response.text and REAL not in response.text
    assert files(db.parent) == before


def test_a_real_fingerprint_still_prints_with_its_mark(prepared):
    client, db, uploads, pid, doc, sha = prepared
    repo.insert_approval(db, pid, REAL, "vendor")
    response = client.get(url(pid))
    assert response.status_code == 200, response.text[:400]
    assert REAL in response.text
