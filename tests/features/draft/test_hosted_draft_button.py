"""The draft page's model button is replaced by the hosted-demo sentence when COPILOT_HOSTED_DEMO is true; with it

off (or not the literal true) the block keeps its byte layout, with the approved label; with the draft flag off there is no page."""

import pytest



from draft_fakes import DISPUTE_ID

from test_draft_routes import PAGE, _web



HOSTED = "COPILOT_HOSTED_DEMO"

SENTENCE = "The AI model is off in the hosted demo. Your message starts as the fixed template."

ORIGINAL_BLOCK = ('<h2 id="ai-title">Optional: a draft from the AI model</h2>')   # Section heading and secondary model button





def test_hosted_on_shows_the_sentence_and_no_button_and_no_form(monkeypatch):

    monkeypatch.setenv(HOSTED, "true")

    web, fake, call = _web(monkeypatch)

    r = web.get(PAGE)

    assert r.status_code == 200

    assert f'<p data-state="model-off">{SENTENCE}</p>' in r.text

    assert "Ask the local assistant for a draft (English and Spanish)" not in r.text and 'data-state="model-form"' not in r.text

    assert "/draft/model" not in r.text and call.calls == []





@pytest.mark.parametrize("value", [None, "", "false", "1", "yes"])

def test_hosted_off_the_model_block_is_byte_identical_to_before(monkeypatch, value):

    if value is None:

        monkeypatch.delenv(HOSTED, raising=False)

    else:

        monkeypatch.setenv(HOSTED, value)

    web, _fake, _call = _web(monkeypatch)

    r = web.get(PAGE)

    assert r.status_code == 200

    assert ORIGINAL_BLOCK in r.text

    tail = r.text[r.text.index(ORIGINAL_BLOCK):]

    assert ('<button type="submit" class="btn btn-secondary" data-sending-text="Writing the draft...">Ask the local assistant for a draft (English and Spanish)</button>\n</form>\n') in tail

    assert "model-off" not in r.text and SENTENCE not in r.text





def test_draft_flag_off_there_is_no_page_whatever_hosted_says(monkeypatch):

    monkeypatch.setenv(HOSTED, "true")

    web, fake, call = _web(monkeypatch, flag=None)

    assert web.get(PAGE).status_code == 404 and fake.calls == [] and call.calls == []

