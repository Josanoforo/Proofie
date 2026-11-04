"""The seven default-mode identity checks, with every optional flag unset."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response

from copilot.app import HOST, create_app
from copilot.web.security import LocalhostOnlyMiddleware, set_csrf_cookie

GOLDEN_PATHS = sorted([
    "/disputes", "/disputes/{dispute_id}", "/disputes/{dispute_id}/due.ics", "/disputes/{dispute_id}/workspace/{view}", "/disputes/{dispute_id}/documents",
    "/disputes/{dispute_id}/documents/{document_id}/original",     # UX-05 / UX v3: stored original (404 in the hosted demo)
    "/disputes/{dispute_id}/documents/{document_id}/exclude",
    "/disputes/{dispute_id}/documents/{document_id}/include",
    "/disputes/{dispute_id}/documents/{document_id}/confirm-reading",
    "/disputes/{dispute_id}/manual-fields", "/disputes/{dispute_id}/packages",
    "/disputes/{dispute_id}/packages/{package_id}",
    "/disputes/{dispute_id}/packages/{package_id}/attach/{document_id}",
    "/disputes/{dispute_id}/packages/{package_id}/detach/{document_id}",
    "/disputes/{dispute_id}/packages/{package_id}/claims",
    "/disputes/{dispute_id}/packages/{package_id}/claims/{claim_id}",
    "/disputes/{dispute_id}/packages/{package_id}/claims/{claim_id}/remove",
    "/disputes/{dispute_id}/packages/{package_id}/approve",
    "/disputes/{dispute_id}/packages/{package_id}/send",
    "/disputes/{dispute_id}/packages/{package_id}/check",
    "/disputes/{dispute_id}/packages/{package_id}/result/{rid}",               # Send/check answer 303 here
    "/disputes/{dispute_id}/packages/{package_id}/confirm-not-received",
])


@pytest.fixture(autouse=True)
def local_mode(monkeypatch):
    import os
    for key in list(os.environ):
        if key.startswith("COPILOT_FEATURE_") or key == "COPILOT_HOSTED_DEMO":
            monkeypatch.delenv(key, raising=False)


def test_routes_match_the_golden_default_table():
    app = create_app()
    assert sorted(app.openapi()["paths"]) == GOLDEN_PATHS
    assert sorted(getattr(r, "path", "") for r in app.routes if getattr(r, "path", None)) == [
        "/", "/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc", "/static"]


def test_only_the_original_loopback_middleware_is_present():
    assert [m.cls for m in create_app().user_middleware] == [LocalhostOnlyMiddleware]


def test_csrf_cookie_header_is_the_original_bytes():
    response = Response()
    set_csrf_cookie(response, "synthetic")
    assert response.headers["set-cookie"] == "csrf_token=synthetic; HttpOnly; Path=/; SameSite=strict"


def test_remote_peer_still_gets_the_loopback_refusal():
    web = TestClient(create_app(), client=("10.0.0.1", 1))
    assert web.get("/").status_code == 403


def test_health_and_access_remain_absent_locally():
    web = TestClient(create_app(), client=("127.0.0.1", 1))
    assert web.get("/healthz").status_code == web.get("/access").status_code == 404


def test_default_bind_host_is_loopback():
    assert HOST == "127.0.0.1"


def test_base_layout_is_unchanged_from_the_default_baseline():
    root = Path(__file__).resolve().parents[2]
    target = "copilot/web/templates/_base.html"
    current = (root / target).read_text(encoding="utf-8").replace("\r\n", "\n")
    # The frozen baseline of the default layout (committed fixture): the page frame as the default layout renders it (the hosted-demo footer expression is the one permitted difference
    # checked below), the shell and the icon link. It is pinned by hash, so it runs in a clone or a ZIP exactly as in the source checkout.
    import hashlib
    baseline = (root / "tests/local/fixtures/_base-baseline-0.html").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert hashlib.sha256(baseline.encode()).hexdigest() == "7a5e3a272eeab457874641552a16e3d34b3da187dee4cd76f73f927495e73c44"
    # Only this hosted footer expression is permitted; the entire default layout stays exact.
    original_footer = "{{ hosted_demo_sentence }}"
    # The same expression, in parentheses, through the note_lines filter (one sentence per line).
    variant_footer = ('{{ (request.app.state.hosted_demo_sentence if request is defined and '
                      'request.scope is defined and "app" in request.scope and '
                      'request.app.state.hosted_demo_sentence else hosted_demo_sentence)|note_lines }}')
    assert current.count(variant_footer) == baseline.count(original_footer) == 1
    # Exactly these brand head lines are added (2026-11-02, BRAND-SURFACES): the three icon links after the charset and the
    # Open Graph block before the title; no other baseline byte is exempted.
    icon = ('<link rel="icon" type="image/svg+xml" href="/static/brand/proofie-mark.svg">\n'
            '<link rel="icon" type="image/png" sizes="32x32" href="/static/brand/favicon-32.png">\n'
            '<link rel="apple-touch-icon" href="/static/brand/apple-touch-icon.png">\n')
    assert current.count(icon) == 1 and baseline.count(icon) == 0
    og = current[current.index("{%- set og_base"):current.index("<title>")]
    assert current.count(og) == 1 and baseline.count(og) == 0 and 'property="og:image"' in og
    assert '<meta charset="utf-8">\n' + icon in current
    # Approved delta list of the shell: these six edits, and nothing else, are the new frame. Each is
    # removed here exactly once; what is left must equal the baseline byte for byte.
    nav_start = current.index("  {%- block nav %}")
    nav_end = current.index("  {%- endblock %}\n", nav_start) + len("  {%- endblock %}\n")
    nav_block = current[nav_start:nav_end]
    assert "feature_enabled('fulfilment')" in nav_block and nav_block.count("{%- block nav %}") == 1
    approved_deltas = [
        '<a class="skip-link" href="#main">Skip to main content</a>\n',                  # 1 skip link
        ' translate="no"',                                                                # 2 brand is not translated
        nav_block,                                                                        # 3 main navigation block
        "{% block breadcrumb %}{% endblock %}\n",                                         # 4 breadcrumb block
        '<div id="inflight-status" class="visually-hidden" role="status" aria-live="polite"></div>\n',  # 5 in-flight region
        ' data-safety="assistant-only"',                                                  # 7 UX pass 3: stable hook of the assistant-only line
    ]
    # Approved delta 8 (LINK-FIX M5): on the access page the brand is plain text, because the gate sends a click on the link straight back.
    # The default layout keeps its one brand link exactly; the wrapper is removed here once, and the rendered check below runs on the
    # whole template (a render without a request takes the link branch).
    gate_brand = (
        "  {%- if request is defined and request.scope is defined and request.url.path == '/access' %}\n"
        '  <span class="brand" translate="no"><span class="brand-mark" aria-hidden="true"></span>Proofie</span>\n'
        "  {%- else %}\n"
        '  <a class="brand" href="/disputes" translate="no"><span class="brand-mark" aria-hidden="true"></span>Proofie</a>\n'
        "  {%- endif %}\n")
    link_brand = '  <a class="brand" href="/disputes" translate="no"><span class="brand-mark" aria-hidden="true"></span>Proofie</a>\n'
    assert current.count(gate_brand) == 1
    unwrapped = current.replace(gate_brand, link_brand, 1)
    stripped = unwrapped.replace(variant_footer, original_footer, 1).replace(icon, "", 1).replace(og, "", 1)
    for delta in approved_deltas:
        assert stripped.count(delta) == 1, delta
        stripped = stripped.replace(delta, "", 1)
    assert stripped.count('<main id="main" tabindex="-1">') == 1                                         # 6 the skip-link target
    assert stripped.replace('<main id="main" tabindex="-1">', "<main>", 1) == baseline
    # Prove the flag-off output, rather than equating its bytes with the hosted source expression.
    from copilot.web.routes_disputes import templates
    original_render = templates.env.from_string(baseline).render()
    expected_render = original_render.replace('<meta charset="utf-8">\n', '<meta charset="utf-8">\n' + icon, 1)
    expected_render = expected_render.replace("<title>", templates.env.from_string(og).render() + "\n<title>", 1)
    rendered = templates.env.from_string(current).render()
    nav_html = rendered[rendered.index('<nav class="site-nav"'):rendered.index("</nav>") + len("</nav>")]
    assert nav_html.count("<li>") == 1 and "All disputes" in nav_html and "Tracking at the sale" not in nav_html  # flag off
    for delta in ('<a class="skip-link" href="#main">Skip to main content</a>\n', ' translate="no"', nav_html, ' data-safety="assistant-only"',
                  '<div id="inflight-status" class="visually-hidden" role="status" aria-live="polite"></div>\n'):
        assert rendered.count(delta) == 1, delta
        rendered = rendered.replace(delta, "", 1)
    # UX pass 3: the navigation sits in a wrapper that the script turns into a phone menu; the wrapper is part of the approved nav block
    for wrapper in ('<div class="site-menu" data-state="site-menu">', '<div class="disc-body" id="site-menu-body">', '</div></div>'):
        assert rendered.count(wrapper) == 1, wrapper
        rendered = rendered.replace(wrapper, "", 1)
    rendered = rendered.replace('<main id="main" tabindex="-1">', "<main>", 1)
    assert "\n".join(line for line in rendered.split("\n") if line.strip()) == \
        "\n".join(line for line in expected_render.split("\n") if line.strip())
    assert '{%- if hosted_demo() %}' in current
