"""The draft feature's default_call (the third model call site) refuses before any network attempt when hosted; flag off it
runs preflight and the call exactly as before."""
import httpx
import pytest

from copilot import config
from copilot.features.draft import model
from draft_fakes import sheet


@pytest.fixture
def net(monkeypatch):
    import step4_evidence_ai as ai
    calls = []

    def fake(*a, **kw):
        calls.append("httpx")
        raise httpx.ConnectError("recorded, not sent")
    monkeypatch.setattr(httpx, "get", fake)
    monkeypatch.setattr(httpx, "post", fake)
    monkeypatch.setattr(ai, "preflight", lambda: calls.append("preflight") or {})
    monkeypatch.setattr(ai, "call_model", lambda m, s, seed, t: calls.append("call_model") or ('{"en":"a","es":"b"}', {}))
    monkeypatch.setenv("COPILOT_OLLAMA_URL", "http://203.0.113.9:11434")
    return calls


def test_flag_off_the_default_call_attempts_preflight_then_the_call(monkeypatch, net):
    monkeypatch.delenv("COPILOT_HOSTED_DEMO", raising=False)
    content, _ = model.default_call([{"role": "user", "content": "x"}], model.DRAFT_SCHEMA, 7)
    assert net == ["preflight", "call_model"] and content == '{"en":"a","es":"b"}'


def test_flag_on_the_default_call_raises_the_constant_message_before_any_attempt(monkeypatch, net):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    with pytest.raises(RuntimeError, match=config.HOSTED_MODEL_OFF_MESSAGE):
        model.default_call([{"role": "user", "content": "x"}], model.DRAFT_SCHEMA, 7)
    assert net == []


def test_flag_on_call_draft_returns_the_unavailable_text_and_the_template_stands(monkeypatch, net):
    monkeypatch.setenv("COPILOT_HOSTED_DEMO", "true")
    r = model.call_draft(sheet())
    assert r.error == model.UNAVAILABLE and r.en is None and net == []
