"""The refusal text of the GPU preflight is neutral: it says what is busy and nothing about who owns what."""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from copilot.core import vision_model as vm  # noqa: E402

NEUTRAL = "GPU or Ollama busy (another model is loaded or the GPU is in use), not running: "


class _Ps:
    def __init__(self, models):
        self._models = models

    def json(self):
        return {"models": self._models}


def _smi(used, util):
    return subprocess.CompletedProcess([], 0, stdout=f"{used}, {util}\n", stderr="")


@pytest.mark.parametrize("models", [[{"name": "some-other-model:1b"}]])
def test_a_foreign_loaded_model_is_refused_with_the_neutral_text(monkeypatch, models):
    ai = vm.ai
    monkeypatch.setattr(ai.httpx, "get", lambda *a, **kw: _Ps(models))
    monkeypatch.setattr(ai.subprocess, "run", lambda *a, **kw: _smi(500, 5))
    with pytest.raises(RuntimeError) as err:
        ai.preflight()
    text = str(err.value)
    assert text.startswith(NEUTRAL)
    assert "project" not in text.lower() and "priority" not in text.lower()


def test_a_sustained_gpu_load_is_refused_with_the_same_text(monkeypatch):
    ai = vm.ai
    monkeypatch.setattr(ai.httpx, "get", lambda *a, **kw: _Ps([]))
    monkeypatch.setattr(ai.subprocess, "run", lambda *a, **kw: _smi(9000, 95))
    monkeypatch.setattr(ai.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError) as err:
        ai.preflight()
    assert str(err.value).startswith(NEUTRAL)


def test_no_source_file_keeps_the_old_wording():
    root = Path(__file__).resolve().parents[2]
    old = " ".join(("other", "active", "project", "has", "priority"))       # built from pieces: this file holds no copy of it
    hits = [str(p.relative_to(root)) for top in ("copilot", "pilot", "demo", "tests") for p in (root / top).rglob("*.py")
            if old in p.read_text(encoding="utf-8", errors="replace")]
    assert hits == []
