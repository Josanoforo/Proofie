"""The hosted-demo sentence renders in the base layout only when COPILOT_HOSTED_DEMO is the literal "true"."""
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from copilot import config
from copilot.app import create_app
from copilot.web.routes_disputes import get_client
from _hosted import authenticated_client, configure_hosted

REC = Path(__file__).resolve().parents[2] / "pilot" / "fixtures" / "synthetic"
WAITING = json.loads((REC / "dispute_PP-R-DEMO-0000001_waiting_for_seller.json").read_text(encoding="utf-8"))["body"]
DID = "PP-R-DEMO-0000001"
LOCAL_FOOTER = "Independent project. Not affiliated with or endorsed by PayPal."


class FakeClient:
    def call(self, method, path, op, **kw):
        if op == "show_dispute":
            return WAITING
        if op == "context_transaction_search":
            return {"transaction_details": []}
        if op == "context_tracking":
            return {"trackers": []}
        raise AssertionError(f"unexpected op {op}")


def _note_text(html):
    """The text of the hosted note with its tags removed and its white space normalised (what a reader sees, line breaks aside)."""
    import re
    from html import unescape
    inner = re.search(r'<p class="hosted-demo-note"[^>]*>(.*?)</p>', html, re.S).group(1)
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", inner)).split())


def _page(monkeypatch):
    configure_hosted(monkeypatch)
    app = create_app()
    app.dependency_overrides[get_client] = lambda: FakeClient()
    r = authenticated_client(app).get(f"/disputes/{DID}")
    assert r.status_code == 200
    return r.text


@pytest.mark.parametrize("value", [None, "", "false", "1", "yes", "TRUE-ish"])
def test_flag_off_or_not_literal_true_renders_nothing_and_keeps_local_text(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("COPILOT_HOSTED_DEMO", raising=False)
    else:
        monkeypatch.setenv("COPILOT_HOSTED_DEMO", value)
    assert config.hosted_demo() is False
    html = _page(monkeypatch)
    assert "hosted-demo" not in html and "hosted demo" not in html and LOCAL_FOOTER in html
    assert "PayPal sandbox &middot; assistant only, you review everything" in html


FINAL_TEXT = (
    "Proofie is built to run on your own computer. "
    "With its default settings, it connects only to PayPal's sandbox. "
    "An AI model, if you turn one on, also runs on your computer. "
    "This copy is the hosted demo of the same app. "
    "Here the AI assistant is switched off: you type the shipment facts yourself. "
    "The demo keeps its database and a log of its PayPal calls on the host's disk; a host restart may erase them. "
    "It uses one shared PayPal sandbox account, so everyone who opens it sees the same disputes and uploads. "
    "The app keeps a PayPal access token only in memory, until it expires; it never writes one to disk or to its log. "
    "It keeps a log of PayPal's answers. "
    "A fixed list of field names removes names, e-mail addresses, phone numbers, addresses, payer and account ids and bank numbers from that log. "
    "Tracking numbers, transaction ids and amounts stay in it. "
    "So does free text such as messages and item descriptions, except e-mail addresses typed in it. "
    "Files you upload, and the text read from them, are stored on this host. "
    "The app sends data only to PayPal's sandbox; the company that hosts this demo also carries the traffic and runs the server.")


def test_the_constant_is_table_2s_final_text_verbatim():
    assert config.HOSTED_DEMO_SENTENCE == FINAL_TEXT


@pytest.mark.parametrize("value", ["true", " TRUE ", "True"])
def test_flag_true_renders_the_final_text_once_in_the_footer_with_the_seven_claims_in_order(monkeypatch, value):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", value)
    html = _page(monkeypatch).replace("&#39;", "'")
    assert html.count('data-state="hosted-demo"') == 1 and LOCAL_FOOTER in html
    assert "hosted-demo" in html[html.index('<footer'):]
    assert _note_text(html) == FINAL_TEXT          # one sentence per line; the text without the tags is unchanged
    claims = ("built to run on your own computer",
              "Here the AI assistant is switched off",
              "keeps its database and a log of its PayPal calls on the host's disk",
              "one shared PayPal sandbox account",
              "keeps a PayPal access token only in memory",
              "Files you upload, and the text read from them, are stored on this host",
              "sends data only to PayPal's sandbox")
    positions = [html.index(c) for c in claims]
    assert positions == sorted(positions)


def test_the_sentence_no_longer_claims_a_session_or_raw_hashes_only():
    s = config.HOSTED_DEMO_SENTENCE
    assert "session" not in s and "only hashes" not in s and "raw PayPal" not in s


# ---- the token-redaction class check: what the sentence says is removed is removed, what it says is kept is kept ------------------
def _sanitize():
    import copilot.core.disputes  # noqa: F401  (puts pilot/ on sys.path)
    from client import sanitize
    return sanitize


REMOVED_BY_CLASS = {                       # class named in the sentence -> property names PayPal uses for it
    "names": ["given_name", "surname", "full_name", "name", "middle_name", "alternate_full_name"],
    "phone numbers": ["phone", "phone_number", "national_number"],
    "addresses": ["address", "shipping_address", "address_line_1", "address_line_2", "admin_area_1", "admin_area_2",
                  "postal_code", "line1", "line2", "city"],
    "payer ids": ["payer_id", "payer_account_id"],
    "account ids": ["account_id", "merchant_id", "payee_account_id", "merchant_account_id", "paypal_account_id"],
    "bank numbers": ["account_number", "account_no", "account_num", "bank_account_number", "iban", "routing_number"],
}


def test_every_class_the_sentence_says_is_removed_is_removed_by_a_key_in_the_list():
    sanitize = _sanitize()
    for cls, keys in REMOVED_BY_CLASS.items():
        out = sanitize({k: "DEMO-VALUE" for k in keys})
        assert set(out.values()) == {"[redacted]"}, (cls, out)


def test_e_mail_addresses_are_removed_by_key_and_by_pattern():
    sanitize = _sanitize()
    assert sanitize({"email_address": "a@example.com"})["email_address"] == "[redacted]"
    assert sanitize({"email": "a@example.com"})["email"] == "[email]"                  # value pattern
    assert "a@example.com" not in sanitize({"note": "write to a@example.com please"})["note"]


def test_every_item_the_sentence_says_is_kept_is_kept():
    sanitize = _sanitize()
    kept = {"tracking_number": "TRACK-DEMO-1", "transaction_id": "TXN-DEMO-1", "invoice_id": "INV-DEMO-1",
            "amount": {"currency_code": "USD", "value": "1234.56"},
            "message": "Please deliver to the back door", "item_description": "Blue widget, size M"}
    assert sanitize(kept) == kept


def test_the_sentence_constant_is_marked_as_the_hosted_demo_wording():
    src = (Path(config.__file__)).read_text(encoding="utf-8")
    assert "# The hosted-demo wording. Every sentence is under 25 words" in src


# One sentence per line inside the existing paragraph; wording and the single data-state unchanged.
def test_the_note_is_one_paragraph_with_one_block_span_per_sentence(monkeypatch):
    import re
    from html import unescape
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    html = _page(monkeypatch)
    assert html.count('data-state="hosted-demo"') == 1 and html.count('class="hosted-demo-note"') == 1
    inner = re.search(r'<p class="hosted-demo-note"[^>]*>(.*?)</p>', html, re.S).group(1)
    assert "<ul" not in inner and "<li" not in inner and "<br" not in inner         # a list inside <p> would be invalid
    lines = [unescape(m) for m in re.findall(r'<span class="note-line">(.*?)</span>', inner, re.S)]
    assert len(lines) == 14 and " ".join(lines) == FINAL_TEXT
    assert all(line.endswith(".") and line[0].isupper() for line in lines)
    assert ".hosted-demo-note .note-line { display: block; }" in (Path(__file__).resolve().parents[2] / "copilot" / "web" / "static" / "app.css").read_text(encoding="utf-8")


def test_note_lines_splits_only_between_sentences_and_escapes():
    from copilot.web.presentation import note_lines
    assert str(note_lines("One. Two, three; four. Five!")) == (
        '<span class="note-line">One.</span> <span class="note-line">Two, three; four.</span> <span class="note-line">Five!</span>')
    assert str(note_lines("a <b>x</b>. Next.")) == ('<span class="note-line">a &lt;b&gt;x&lt;/b&gt;.</span> <span class="note-line">Next.</span>')
    assert str(note_lines("No sentence break here")) == '<span class="note-line">No sentence break here</span>'
    assert str(note_lines("e.g. lower case after a dot stays one line. Yes.")).count("note-line\"") == 2
