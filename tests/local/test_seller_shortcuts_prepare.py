"""D-101 seller item 2: "Upload and prepare the package" is one form, one click (COPILOT_FEATURE_SELLER_SHORTCUTS).
Offline: fake PayPal client (GET only), no model. Nothing is approved or sent."""
import pytest

from copilot import config
from copilot.db import repo
from copilot.web import routes_package

from test_fix1_visible_failures import DID, ROOT, FakeClient, _app, _client

FLAG = "COPILOT_FEATURE_SELLER_SHORTCUTS"
PREPARE = f"/disputes/{DID}/workspace/prepare"
BUTTON = "Upload and prepare the package"


class GetOnly(FakeClient):
    def call(self, method, path, op, **kw):
        assert method == "GET", "preparing a package cannot write to PayPal"
        return super().call(method, path, op, **kw)


@pytest.fixture
def no_model(monkeypatch):
    """The local model is not there: the package starts in manual mode, as for a judge without Ollama."""
    def refuse(*a, **k):
        raise RuntimeError("model unavailable")
    monkeypatch.setattr(routes_package, "propose_package", refuse)


def _pdf():
    return (ROOT / "demo" / "documents" / "03_in_transit.pdf").read_bytes()


def _upload(c, token, then=None):
    data = {"csrf_token": token}
    if then:
        data["then"] = then
    return c.post(f"/disputes/{DID}/documents", files={"file": ("label.pdf", _pdf(), "application/pdf")},
                  data=data, follow_redirects=False)


def test_the_button_is_on_the_prepare_page_only_with_the_setting_and_no_package_yet(monkeypatch):
    monkeypatch.setenv(FLAG, "true")
    c, _ = _client(_app(GetOnly()))
    assert BUTTON in c.get(PREPARE).text
    repo.insert_package(config.db_path(), DID, source="human_edit", validation_ok=False, review_flags=[])
    assert BUTTON not in c.get(PREPARE).text            # a package exists: a document is attached to it instead


def test_without_the_setting_there_is_no_such_button(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    c, _ = _client(_app(GetOnly()))
    assert BUTTON not in c.get(PREPARE).text


def test_one_click_stores_the_document_makes_the_package_and_lands_on_it(monkeypatch, no_model):
    monkeypatch.setenv(FLAG, "true")
    c, token = _client(_app(GetOnly()))
    r = _upload(c, token, then="prepare")
    assert r.status_code == 303 and r.headers["location"].startswith(f"/disputes/{DID}/packages/")
    db = config.db_path()
    assert len(repo.list_documents(db, DID)) == 1 and len(repo.list_packages(db, DID)) == 1
    assert repo.list_packages(db, DID)[0]["source"] == "human_edit"      # manual mode: the seller still checks every value
    assert repo.get_latest_valid_approval(db, repo.list_packages(db, DID)[0]["id"]) is None      # nothing approved


def test_the_prepared_package_page_opens(monkeypatch, no_model):
    monkeypatch.setenv(FLAG, "true")
    c, token = _client(_app(GetOnly()))
    location = _upload(c, token, then="prepare").headers["location"]
    page = c.get(location)
    assert page.status_code == 200 and "Package contents" in page.text


def test_without_the_setting_the_same_post_is_a_plain_upload(monkeypatch, no_model):
    monkeypatch.delenv(FLAG, raising=False)
    c, token = _client(_app(GetOnly()))
    r = _upload(c, token, then="prepare")
    assert r.status_code == 303 and "/workspace/prepare" in r.headers["location"]
    assert repo.list_packages(config.db_path(), DID) == [] and len(repo.list_documents(config.db_path(), DID)) == 1


def test_a_plain_upload_with_the_setting_on_still_only_uploads(monkeypatch, no_model):
    monkeypatch.setenv(FLAG, "true")
    c, token = _client(_app(GetOnly()))
    r = _upload(c, token)
    assert r.status_code == 303 and "/workspace/prepare" in r.headers["location"]
    assert repo.list_packages(config.db_path(), DID) == []


def test_with_a_package_already_there_the_one_click_does_not_make_a_second(monkeypatch, no_model):
    monkeypatch.setenv(FLAG, "true")
    c, token = _client(_app(GetOnly()))
    repo.insert_package(config.db_path(), DID, source="human_edit", validation_ok=False, review_flags=[])
    r = _upload(c, token, then="prepare")
    assert r.status_code == 303 and "document_added_package" in r.headers["location"]
    assert len(repo.list_packages(config.db_path(), DID)) == 1


def test_a_refused_upload_makes_no_package(monkeypatch, no_model):
    monkeypatch.setenv(FLAG, "true")
    c, token = _client(_app(GetOnly()))
    r = c.post(f"/disputes/{DID}/documents", files={"file": ("x.exe", b"MZ not a document", "application/octet-stream")},
               data={"csrf_token": token, "then": "prepare"}, follow_redirects=False)
    assert r.status_code == 400 and repo.list_packages(config.db_path(), DID) == []
