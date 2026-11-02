"""D-101 P3 step for tools/auto_run.py: the account page shows the dated path to the high-volume dispute rule.

Own module so the lane adds one import and one registration line to auto_run.py. The step opens /account, checks that the
'Path to the cliff' section is there with one of its state sentences, that no raw machine value shows, and that the page
does not scroll sideways at 375. It reads; it never writes. The text is checked by what the code says, not by the numbers
(those depend on the account being read and are checked in tests/features/account_cliff/test_trajectory.py)."""
from __future__ import annotations

import re

STEP = "6.8"
TITLE = "Cuenta: ruta fechada al límite de disputas"
HEADING = "Path to the cliff"
EXPECTED = (f"/account responde 200 con '{HEADING}', una de las frases de estado, sin valores crudos "
            "y sin scroll horizontal a 375")
STATES = ("dated", "not_this_month", "no_pace", "already_over", "below_minimum", "unknown")
# One sentence fragment per state, from copilot/features/account_cliff/trajectory.py:headline
SENTENCES = ("would reach", "cannot be dated", "may apply", "would not apply", "could not be worked out")
RAW_VALUES = (r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", r"\b\d+ cents\b")
DESKTOP, MOBILE = {"width": 1440, "height": 1000}, {"width": 375, "height": 812}


def run(c) -> str:
    status = c.goto("/account")
    if status != 200:
        raise AssertionError(f"/account respondió {status}; se esperaba 200 (¿está COPILOT_FEATURE_ACCOUNT_CLIFF_PATH activa?)")
    c.require(HEADING)
    section = c.page.locator('[data-state="cliff-path"]')
    if section.count() != 1:
        raise AssertionError(f"hay {section.count()} secciones 'cliff-path'; se esperaba 1")
    state = section.get_attribute("data-path-state")
    if state not in STATES:
        raise AssertionError(f"estado desconocido: {state!r}")
    text = section.inner_text()
    if not any(s in text for s in SENTENCES):
        raise AssertionError("la sección no trae ninguna de las frases de estado")
    raw = [p for p in RAW_VALUES if re.search(p, text, flags=re.I)]
    if raw:
        raise AssertionError("valores crudos en la sección: " + ", ".join(raw))
    widths = {}
    try:
        for name, size in (("375", MOBILE), ("1440", DESKTOP)):
            c.page.set_viewport_size(size)
            c.page.goto(c.base + "/account", wait_until="load")
            widths[name] = tuple(c.page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]"))
            c.shot(f"account-cliff-{name}")
    finally:
        c.page.set_viewport_size(DESKTOP)
    if widths["375"][0] > widths["375"][1]:
        raise AssertionError(f"scroll horizontal a 375 ({widths['375'][0]} > {widths['375'][1]})")
    first = re.sub(r"\s+", " ", text).strip()[:110]
    return f"200; estado {state}; «{first}»"
