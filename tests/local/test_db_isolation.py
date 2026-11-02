"""Guard test for conftest.py's autouse isolation fixtures (requested after the DB_PATH bug hit twice in
one session - see copilot/config.py's module docstring; extended 2026-10-05 after a second, analogous
bug: any test anywhere that uploads an image and reaches create_package's AI path calls the real
copilot/core/vision_model.propose_image_claims(), whose own evidence log was not isolated). Proves,
not just by code review:

1. The fixtures are actually active - config.db_path()/uploads_dir() do NOT resolve to the real
   data/copilot.db / data/uploads while a test is running, and vision_model.LOG does not resolve to the
   real evidence/copilot_vision.jsonl either.
2. Exercising the exact modules that caused the real pollution before (routes_documents, routes_package
   - both its manual and AI-with-an-image paths, routes_disputes' auto_prep scheduling, repo directly)
   through the running app does not touch a single byte of the real data/ directory or the real vision
   evidence log.

If this ever fails, something in copilot/ went back to a frozen-at-import-time constant instead of
calling the function-based override - see copilot/config.py's docstring for why that regression is so
easy to reintroduce by accident.
"""
import hashlib
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from copilot import config
from copilot.app import create_app
from copilot.core import vision_model
from copilot.db import repo
from copilot.web.routes_disputes import get_client

REC = Path(__file__).resolve().parents[2] / "pilot" / "fixtures" / "synthetic"
WAITING = json.loads((REC / "dispute_PP-R-DEMO-0000001_waiting_for_seller.json").read_text(encoding="utf-8"))["body"]
LIST_PAGE1 = json.loads((REC / "disputes_list_page1.json").read_text(encoding="utf-8"))["body"]
DID = "PP-R-DEMO-0000001"
PDF_BYTES = b"%PDF-1.7\nnot a fully valid pdf body, just enough to be detected by its magic header\n"


class FakeClient:
    def __init__(self):
        self.calls = []

    def call(self, method, path, op, **kw):
        self.calls.append(op)
        if op == "list_disputes":
            return LIST_PAGE1
        if op == "show_dispute":
            return WAITING
        if op in ("context_transaction_search",):
            return {"transaction_details": []}
        if op == "context_tracking":
            return {"trackers": []}
        raise AssertionError(f"unexpected op {op}")


def _hash_file(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hash_dir(path: Path) -> str:
    if not path.exists():
        return "missing"
    h = hashlib.sha256()
    for f in sorted(path.iterdir()):
        if f.is_file():
            h.update(f.name.encode("utf-8"))
            h.update(f.read_bytes())
    return h.hexdigest()


def test_isolation_fixture_redirects_away_from_the_real_paths():
    assert config.db_path() != config.DB_PATH
    assert config.uploads_dir() != config.UPLOADS_DIR
    assert not str(config.db_path()).startswith(str(config.DATA_DIR))
    assert vision_model.LOG != vision_model.EVIDENCE / "copilot_vision.jsonl"


def test_exercising_the_app_never_touches_the_real_data_directory():
    real_db_before = _hash_file(config.DB_PATH)
    real_uploads_before = _hash_dir(config.UPLOADS_DIR)
    real_vision_log_before = _hash_file(vision_model.EVIDENCE / "copilot_vision.jsonl")

    # Drive the exact code paths that caused real pollution earlier: the inbox's auto_prep
    # scheduling (routes_disputes.py), a document upload (routes_documents.py), a manual package
    # (routes_package.py), plus a direct repo.* call (the pattern every one of those routes uses
    # internally) - all through config.db_path()/uploads_dir(), never a frozen constant.
    app = create_app()
    app.dependency_overrides[get_client] = lambda: FakeClient()
    c = TestClient(app, client=("127.0.0.1", 12345))

    r = c.get("/disputes")   # schedules + runs the background auto_prep for DID
    assert r.status_code == 200

    import secrets
    token = secrets.token_urlsafe(32)
    c.cookies.set("csrf_token", token)
    c.post(f"/disputes/{DID}/documents", files={"file": ("label.pdf", PDF_BYTES, "application/pdf")},
          data={"csrf_token": token}, follow_redirects=False)
    c.post(f"/disputes/{DID}/packages?manual=1", data={"csrf_token": token}, follow_redirects=False)

    # And the second bug this guard was extended for: an IMAGE upload + the non-manual (AI) path, which
    # reaches copilot/core/vision_model.propose_image_claims() for real (network-blocked by conftest.py's
    # other fixture, but that must never mean its evidence LOG WRITE reaches the real file either).
    c.post(f"/disputes/{DID}/documents", files={"file": ("photo.png",
                                                         b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image/png")},
          data={"csrf_token": token}, follow_redirects=False)
    c.post(f"/disputes/{DID}/packages", data={"csrf_token": token}, follow_redirects=False)

    repo.log_action(config.db_path(), "system", "guard_test_direct_write", DID, {})

    assert _hash_file(config.DB_PATH) == real_db_before
    assert _hash_dir(config.UPLOADS_DIR) == real_uploads_before
    assert _hash_file(vision_model.EVIDENCE / "copilot_vision.jsonl") == real_vision_log_before
