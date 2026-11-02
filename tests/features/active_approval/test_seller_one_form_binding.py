"""D-101 S2 binding: the one-form upload chain ends at package creation. It never creates a package for a refused upload and
never approves or sends anything (spies on every approval and send path, plus a route-table / AST assertion)."""
import pytest

from copilot import config
from copilot.db import repo
from copilot.web import routes_package

from test_fix1_visible_failures import DID, ROOT, FakeClient, _app, _client
from test_seller_shortcuts_prepare import FLAG, GetOnly, _upload, no_model  # noqa: F401


# ---- S2 binding: the one-form chain ends at package creation ---------------------------------------------------------
def _forbid_approval_and_send(monkeypatch):
    from copilot.core import send_service
    from copilot.features.active_approval import service as approval
    from copilot.features.resolution import service as resolution

    def boom(*a, **k):
        raise AssertionError("the one-form path must never approve or send anything")
    monkeypatch.setattr(approval, "approve", boom)
    monkeypatch.setattr(send_service, "send_package", boom)
    monkeypatch.setattr(resolution, "register", boom)
    monkeypatch.setattr(repo, "insert_approval", boom)


def test_the_route_table_says_the_documents_post_reaches_package_creation_and_nothing_further(monkeypatch):
    import inspect
    from copilot.web import routes_documents
    posts = [r for r in routes_documents.router.routes if r.path == "/disputes/{dispute_id}/documents" and "POST" in r.methods]
    assert len(posts) == 1 and posts[0].endpoint is routes_documents.upload_document
    import ast
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(routes_documents.upload_document)))
    called = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "create_package" in called                                           # the chain ends here (passed to the threadpool)
    for forbidden in ("approve", "insert_approval", "send_package", "register", "call"):
        assert forbidden not in called, f"the upload route must not call {forbidden}"


def test_the_one_form_chain_calls_package_creation_once_and_never_approves_or_sends(monkeypatch, no_model):
    monkeypatch.setenv(FLAG, "true")
    _forbid_approval_and_send(monkeypatch)
    made = []
    original = routes_package.create_package

    def spy(*a, **k):
        made.append(a[1])
        return original(*a, **k)
    monkeypatch.setattr(routes_package, "create_package", spy)
    c, token = _client(_app(GetOnly()))
    r = _upload(c, token, then="prepare")
    assert r.status_code == 303 and made == [DID]
    assert len(repo.list_packages(config.db_path(), DID)) == 1


def test_a_refused_upload_never_reaches_package_creation(monkeypatch, no_model):
    monkeypatch.setenv(FLAG, "true")
    _forbid_approval_and_send(monkeypatch)
    made = []
    monkeypatch.setattr(routes_package, "create_package", lambda *a, **k: made.append(a) or None)
    c, token = _client(_app(GetOnly()))
    r = c.post(f"/disputes/{DID}/documents", files={"file": ("x.exe", b"MZ not a document", "application/octet-stream")},
               data={"csrf_token": token, "then": "prepare"}, follow_redirects=False)
    assert r.status_code == 400 and made == [] and repo.list_packages(config.db_path(), DID) == []
