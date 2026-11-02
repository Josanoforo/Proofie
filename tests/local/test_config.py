"""Offline tests for copilot/config.py's ollama_url()/model_name(). Both read
os.environ fresh on every call - the same discipline as db_path()/uploads_dir() (see that module's
docstring) - so a monkeypatch.setenv() mid-test, or a later real .env change, is never missed.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from copilot import config


def test_ollama_url_defaults_to_the_documented_local_port():
    assert config.ollama_url() == "http://127.0.0.1:11434" == config.DEFAULT_OLLAMA_URL


def test_model_name_defaults_to_the_documented_model():
    assert config.model_name() == "qwen3.5:9b" == config.DEFAULT_MODEL


def test_both_are_read_fresh_from_the_environment_every_call(monkeypatch):
    monkeypatch.setenv("COPILOT_OLLAMA_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("COPILOT_MODEL", "some-other-model")
    assert config.ollama_url() == "http://127.0.0.1:1"
    assert config.model_name() == "some-other-model"

    monkeypatch.delenv("COPILOT_OLLAMA_URL")
    monkeypatch.delenv("COPILOT_MODEL")
    assert config.ollama_url() == config.DEFAULT_OLLAMA_URL   # reverts without a process restart
    assert config.model_name() == config.DEFAULT_MODEL


# ---- optional-feature flag convention ---------------------------------------------------------------
def test_feature_enabled_is_off_by_default(monkeypatch):
    monkeypatch.delenv("COPILOT_FEATURE_ACCOUNT_CLIFF", raising=False)
    assert config.feature_enabled("account-cliff") is False


def test_feature_enabled_only_the_literal_true_turns_it_on(monkeypatch):
    for value, expected in [("true", True), ("TRUE ", True), (" True", True), ("false", False), ("1", False),
                            ("yes", False), ("", False)]:
        monkeypatch.setenv("COPILOT_FEATURE_ACCOUNT_CLIFF", value)
        assert config.feature_enabled("account-cliff") is expected, value


def test_feature_enabled_normalises_the_name_and_reads_every_call(monkeypatch):
    monkeypatch.setenv("COPILOT_FEATURE_ACCOUNT_CLIFF", "true")
    assert config.feature_enabled("account-cliff") and config.feature_enabled("ACCOUNT_CLIFF")
    monkeypatch.delenv("COPILOT_FEATURE_ACCOUNT_CLIFF")
    assert not config.feature_enabled("account-cliff")
    assert not config.feature_enabled("other-feature")
