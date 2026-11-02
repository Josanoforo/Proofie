"""Synthetic HTTPS access fixture for offline hosted web tests."""
import pytest
from fastapi.testclient import TestClient

from copilot import config

TEST_ACCESS_CODE = "test-hosted-access-code"


def configure_hosted(monkeypatch):
    if config.hosted_demo():
        monkeypatch.setenv("COPILOT_ACCESS_CODE", TEST_ACCESS_CODE)


def authenticated_client(app, client=("127.0.0.1", 12345), **kwargs):
    web = TestClient(app, base_url="https://localhost" if config.hosted_demo() else "http://localhost", client=client, **kwargs)
    if config.hosted_demo():
        assert web.get("/access").status_code == 200
        response = web.post("/access", data={"code": config.access_code(), "csrf_token": web.cookies.get("csrf_token")},
                            follow_redirects=False)
        assert response.status_code == 303 and web.cookies.get("proofie_access")
        web.cookies.delete("csrf_token")
    return web


def reset_csrf(web):
    """Clear every CSRF cookie scope while retaining the browser's access cookie."""
    for cookie in list(web.cookies.jar):
        if cookie.name == "csrf_token":
            web.cookies.delete(cookie.name, domain=cookie.domain, path=cookie.path)


def set_test_csrf(web, token):
    reset_csrf(web)
    if config.hosted_demo():
        web.cookies.set("csrf_token", token, domain="localhost.local", path="/")
    else:
        web.cookies.set("csrf_token", token)


@pytest.fixture
def hosted_web(monkeypatch):
    def build(dependency=None, client=("127.0.0.1", 12345)):
        from copilot.app import create_app
        from copilot.web.routes_disputes import get_client
        monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
        configure_hosted(monkeypatch)
        app = create_app()
        if dependency is not None:
            app.dependency_overrides[get_client] = dependency
        return authenticated_client(app, client)
    return build
