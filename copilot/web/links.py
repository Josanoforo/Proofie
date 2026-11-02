"""Fixed outbound links the shell offers to seller-facing pages.

Display only: the app never requests this address. One link target exists, the sandbox Resolution Center root, confirmed by hand
in the PayPal sandbox on 2026-10-04; there is no per-dispute path, and the live PayPal host is never linked. Templates get `resolution_center_link()` and `RESOLUTION_CENTER_URL` as Jinja globals
(presentation.install); Python callers may import this module directly.
"""
from markupsafe import Markup, escape

RESOLUTION_CENTER_URL = "https://www.sandbox.paypal.com/resolutioncenter"
RESOLUTION_CENTER_NAME = "PayPal's Resolution Center"                    # the words a sentence turns into the link
RESOLUTION_CENTER_LINK_TEXT = "Open PayPal's Resolution Center (sandbox)"    # the standalone link
NEW_TAB_NOTE = "(opens in a new tab)"
SIGN_IN_SENTENCE = ("To see this dispute as PayPal shows it, open PayPal's Resolution Center (sandbox) and sign in "
                    "with the seller account.")


def resolution_center_link(text=None) -> Markup:
    """One <a> to the sandbox Resolution Center: rel="noopener", opens in a new tab, with the new-tab note kept for
    screen readers only. `text` (escaped) defaults to RESOLUTION_CENTER_LINK_TEXT; pass RESOLUTION_CENTER_NAME to link
    the words inside an existing sentence."""
    label = RESOLUTION_CENTER_LINK_TEXT if text is None else str(text)
    return Markup('<a href="{}" rel="noopener" target="_blank">{}<span class="visually-hidden"> {}</span></a>').format(
        RESOLUTION_CENTER_URL, escape(label), NEW_TAB_NOTE)
