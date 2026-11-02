"""The stylesheet and the script are always revalidated (UX pass 3b): a browser that kept an older copy asks again, so a redeploy
never leaves a page half old and half new. Expected before the first run: a static file answers 200 with Cache-Control no-cache and
an ETag, and a conditional request with that ETag answers 304 without a body."""
import pytest

from copilot.app import create_app
from _hosted import authenticated_client


@pytest.mark.parametrize("path", ["/static/app.js", "/static/app.css"])
def test_static_files_are_revalidated_and_answer_304_when_unchanged(path):
    client = authenticated_client(create_app(), client=("127.0.0.1", 12345))
    first = client.get(path)
    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-cache"
    etag = first.headers["etag"]
    again = client.get(path, headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.content == b""
