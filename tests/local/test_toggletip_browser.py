"""The toggletip (the "i" next to a label) in a real browser: opens on hover, keyboard focus and click; closes on Escape
and on an outside click; stays inside a 375 px screen; its text stays in the page without scripts.

Expected, written before the first run: closed at first with aria-expanded=false; hover opens it; Escape closes it; a click
pins it open until the next click or an outside click; Tab focus opens it; at 375 px the open body is inside the viewport;
with scripts off the explanation is plain visible text and the button is not drawn."""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "copilot" / "web" / "static"
PAGE = ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'></head>"
        "<body><main><p>Label <span class='tip' data-tip><button type='button' class='tip-btn btn-link' aria-expanded='false' "
        "aria-label='What this means'><span aria-hidden='true'>i</span></button><span class='tip-body'>It explains why.</span></span></p>"
        "<p id='outside'>Elsewhere</p></main></body></html>")


def _launch():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright is not installed")
    return sync_playwright()


def _page(browser, width, scripts=True):
    page = browser.new_page(viewport={"width": width, "height": 700})
    page.set_content(PAGE)
    page.add_style_tag(path=str(STATIC / "app.css"))
    if scripts:
        page.add_script_tag(path=str(STATIC / "app.js"))
    return page


def test_toggletip_opens_on_hover_focus_and_click_and_closes_on_escape_and_outside_click():
    with _launch() as pw:
        browser = pw.chromium.launch()
        try:
            page = _page(browser, 1280)
            button, body = page.locator(".tip-btn"), page.locator(".tip-body")
            assert button.get_attribute("aria-expanded") == "false" and not body.is_visible()
            assert button.get_attribute("aria-describedby") == body.get_attribute("id")
            button.hover()
            assert body.is_visible() and button.get_attribute("aria-expanded") == "true"
            page.keyboard.press("Escape")
            assert not body.is_visible() and button.get_attribute("aria-expanded") == "false"
            page.mouse.move(5, 5)
            button.click()                                   # a click pins it
            page.mouse.move(5, 5)
            assert body.is_visible()
            page.locator("#outside").click()
            assert not body.is_visible()
            page.keyboard.press("Tab")                       # keyboard focus opens it
            assert "tip-btn" in page.evaluate("document.activeElement.className") and body.is_visible()
            page.keyboard.press("Escape")
            assert not body.is_visible()
        finally:
            browser.close()


def test_toggletip_body_stays_inside_a_375_px_screen():
    with _launch() as pw:
        browser = pw.chromium.launch()
        try:
            page = _page(browser, 375)
            page.locator(".tip-btn").click()
            box = page.locator(".tip-body").bounding_box()
            assert box["x"] >= 0 and box["x"] + box["width"] <= 375
            assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
        finally:
            browser.close()


def test_without_scripts_the_explanation_is_plain_text_and_the_button_is_not_drawn():
    with _launch() as pw:
        browser = pw.chromium.launch()
        try:
            page = _page(browser, 1280, scripts=False)
            assert page.locator(".tip-body").is_visible() and not page.locator(".tip-btn").is_visible()
        finally:
            browser.close()
