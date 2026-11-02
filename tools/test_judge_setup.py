"""R1-1: `judge_check --setup` writes the credentials file and never shows a value. Temp dir, fake paths, no network."""
import subprocess
import sys

import pytest

import judge_check
import judge_setup as js

CLIENT = "-".join(["PLANTED", "ID", "0123456789", "AA"])
SECRET = "-".join(["PLANTED", "VALUE", "9876543210", "ZZ"])
EXAMPLE = "# example\nPAYPAL_CLIENT_ID=your-sandbox-client-id-here\nPAYPAL_CLIENT_SECRET=your-sandbox-secret-here\nCOPILOT_FEATURE_X=true\n"


@pytest.fixture
def root(tmp_path):
    (tmp_path / ".env.example").write_text(EXAMPLE, encoding="utf-8")
    return tmp_path


def answers(*values):
    queue = list(values)
    return lambda prompt: queue.pop(0)


def run(root, ask, confirm=lambda p: "s", token=lambda i, s: ["a", "b", "https://uri.paypal.com/services/disputes/read-seller"],
        ignores=lambda p: True):
    out = []
    code = js.setup(root, ask=ask, confirm=confirm, token=token, ignores=ignores, say=out.append)
    return code, "\n".join(out)


def test_the_planted_values_land_in_the_settings_file_and_nowhere_else(root, capsys):
    code, said = run(root, answers(CLIENT, SECRET))
    shown = said + capsys.readouterr().out + capsys.readouterr().err
    text = (root / ".env").read_text(encoding="utf-8")
    assert code == 0 and f"PAYPAL_CLIENT_ID={CLIENT}" in text and f"PAYPAL_CLIENT_SECRET={SECRET}" in text
    assert "COPILOT_FEATURE_X=true" in text and "your-sandbox" not in text      # the feature lines come from the example
    assert CLIENT not in shown and SECRET not in shown
    assert "3 scopes; dispute scopes: yes" in said


def test_the_token_call_receives_the_values_but_the_output_stays_clean(root):
    seen = []
    code, said = run(root, answers(CLIENT, SECRET), token=lambda i, s: seen.append((i, s)) or 401)
    assert seen == [(CLIENT, SECRET)] and "HTTP 401" in said and CLIENT not in said and SECRET not in said


def test_a_target_git_would_track_is_refused(root):
    code, said = run(root, answers(CLIENT, SECRET), ignores=lambda p: False)
    assert code == 1 and not (root / ".env").exists() and "does not ignore" in said


def test_the_buyer_app_values_are_refused(root):
    (root / ".env.buyer").write_text(f"PAYPAL_CLIENT_ID={CLIENT}\nPAYPAL_CLIENT_SECRET=other-buyer-secret-0000000\n", encoding="utf-8")
    code, said = run(root, answers(CLIENT, SECRET))
    assert code == 1 and not (root / ".env").exists() and "buyer app" in said and CLIENT not in said


def test_a_keep_answer_leaves_the_file_byte_equal(root):
    before = b"PAYPAL_CLIENT_ID=existing\r\nodd bytes \xff\n"
    (root / ".env").write_bytes(before)
    asked = []
    code, said = run(root, lambda p: asked.append(p), confirm=lambda p: "n")
    assert code == 0 and (root / ".env").read_bytes() == before and asked == []      # not even asked for a value


@pytest.mark.parametrize("bad", ["", "short", "has a space in it 0123456789"])
def test_empty_short_or_spaced_values_write_nothing(root, bad):
    code, said = run(root, answers(bad))
    assert code == 1 and not (root / ".env").exists()


def test_equal_values_write_nothing(root):
    code, _ = run(root, answers(CLIENT, CLIENT))
    assert code == 1 and not (root / ".env").exists()


def test_only_the_sandbox_host_is_called():
    with pytest.raises(ValueError):
        js.sandbox_token_scopes(CLIENT, SECRET, url="https://" + ".".join(["api-m", "paypal", "com"]) + "/v1/oauth2/token")
    assert js.SANDBOX_TOKEN_URL.startswith("https://api-m.sandbox.paypal.com/")


def test_git_really_ignores_the_settings_file_in_this_repo():
    top = js.Path(__file__).resolve().parents[1]
    if subprocess.run(["git", "-C", str(top), "rev-parse", "--git-dir"], capture_output=True).returncode != 0:
        # a kit extracted from the ZIP has no repository for git to answer from: check the line git would read
        assert ".env" in (top / ".gitignore").read_text(encoding="utf-8-sig").splitlines()
        return
    assert js.git_ignores(top, top / ".env")


def test_the_setup_flag_is_wired_and_the_running_flags_row_is_still_there(monkeypatch):
    called = []
    monkeypatch.setattr(js, "setup", lambda r: called.append(r) or 0)
    assert judge_check.main(["--setup"]) == 0 and called
    assert hasattr(judge_check, "check_running_flags")
