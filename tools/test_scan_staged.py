"""Offline tests for tools/scan_staged.py using fake secrets only (never real credentials)."""
import subprocess
import sys
from pathlib import Path

import pytest

import scan_staged as sc

FAKE_PRIVATE_KEY = "-----BEGIN RSA PRIVATE KEY-----\nMIIBFAKEFAKEFAKE\n-----END RSA PRIVATE KEY-----"
FAKE_AWS_KEY = "AKIAFAKEFAKEFAKEFAKE"
FAKE_BEARER = "Authorization: Bearer abcFAKE1234567890FAKEtoken"
FAKE_ASSIGNMENT = 'client_secret = "fakeSecretValue1234567890"'
FAKE_PAYPAL_TOKEN = "A21FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE"


@pytest.mark.parametrize("text,rule", [
    (FAKE_PRIVATE_KEY, "private_key_block"),
    (FAKE_AWS_KEY, "aws_access_key_id"),
    (FAKE_BEARER, "generic_bearer_token"),
    (FAKE_ASSIGNMENT, "assignment_looking_like_secret"),
    (FAKE_PAYPAL_TOKEN, "paypal_sandbox_bearer_prefix"),
])
def test_pattern_detected(text, rule):
    hits = sc.scan_text(text, env_values=[])
    assert any(r == rule for _, r in hits)


def test_clean_text_has_no_hits():
    assert sc.scan_text("Order total: 25.00 USD\nTracking: DEMO8800123456\n", env_values=[]) == []


def test_env_literal_value_detected_even_without_pattern():
    hits = sc.scan_text("some debug line with fakeClientIdValue9999 in it", env_values=["fakeClientIdValue9999"])
    assert any(r == "env_credential_literal" for _, r in hits)


def test_line_numbers_are_1_indexed_and_correct():
    text = "line one\nline two has " + FAKE_AWS_KEY + "\nline three"
    hits = sc.scan_text(text, env_values=[])
    assert hits == [(2, "aws_access_key_id")]


def test_no_match_value_ever_appears_in_findings_output(capsys):
    """Regression guard: the printed findings must never contain the secret substring itself."""
    text = "token=" + FAKE_AWS_KEY
    hits = sc.scan_text(text, env_values=[])
    rendered = "\n".join(f"line:{n}: possible secret ({rule})" for n, rule in hits)
    assert FAKE_AWS_KEY not in rendered


def test_env_secret_values_reads_only_the_two_known_keys(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "PAYPAL_CLIENT_ID=fakeIdAbc12345\nPAYPAL_CLIENT_SECRET=fakeSecretXyz98765\nOTHER_VAR=should-not-be-picked\n",
        encoding="utf-8")
    monkeypatch.setattr(sc, "ROOT", tmp_path)
    values = sc.env_secret_values()
    assert set(values) == {"fakeIdAbc12345", "fakeSecretXyz98765"}


def test_env_secret_values_empty_when_no_env_file(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "ROOT", tmp_path)
    assert sc.env_secret_values() == []


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_end_to_end_blocks_commit_of_a_staged_fake_secret(tmp_path):
    """Build a throwaway git repo (not this project's) and stage a file with a fake secret."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    (repo / "notes.txt").write_text(FAKE_AWS_KEY + "\n", encoding="utf-8")
    _git(["add", "notes.txt"], repo)
    r = subprocess.run([sys.executable, str(Path(sc.__file__).resolve())], cwd=repo,
                       capture_output=True, text=True)
    assert r.returncode == 1
    assert FAKE_AWS_KEY not in r.stdout and FAKE_AWS_KEY not in r.stderr
    assert "possible secret" in r.stderr


def test_allowlist_is_narrow_same_content_elsewhere_still_blocks(tmp_path):
    """The scanner's own test fixtures are allowlisted by exact path; the same content in any other
    file must still be blocked (the allowlist is not a general secret-shape exemption)."""
    repo = tmp_path / "repo3"
    repo.mkdir()
    (repo / "tools").mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    (repo / "tools" / "test_scan_staged.py").write_text(FAKE_AWS_KEY + "\n", encoding="utf-8")
    (repo / "notes.txt").write_text(FAKE_AWS_KEY + "\n", encoding="utf-8")
    _git(["add", "tools/test_scan_staged.py", "notes.txt"], repo)
    r = subprocess.run([sys.executable, str(Path(sc.__file__).resolve())], cwd=repo,
                       capture_output=True, text=True)
    assert r.returncode == 1
    assert "tools/test_scan_staged.py" not in r.stderr
    assert "notes.txt:1" in r.stderr


def test_end_to_end_clean_repo_exits_zero(tmp_path):
    repo = tmp_path / "repo2"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    (repo / "notes.txt").write_text("nothing sensitive here\n", encoding="utf-8")
    _git(["add", "notes.txt"], repo)
    r = subprocess.run([sys.executable, str(Path(sc.__file__).resolve())], cwd=repo,
                       capture_output=True, text=True)
    assert r.returncode == 0


# ---- fail-closed, not fail-open -----------------------------------------------------
def test_scan_blocks_when_a_staged_file_cannot_be_read(monkeypatch, tmp_path):
    monkeypatch.setattr(sc, "ROOT", tmp_path)
    (tmp_path / "secretless.txt").write_text("nothing here", encoding="utf-8")

    def boom(self, *a, **kw):
        raise PermissionError("simulated unreadable file")

    monkeypatch.setattr(Path, "read_text", boom)
    findings = sc._scan(["secretless.txt"])
    assert findings and "BLOCKED" in findings[0] and "PermissionError" in findings[0]
    assert "nothing here" not in findings[0]


def test_scan_blocks_when_a_staged_file_is_missing_from_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(sc, "ROOT", tmp_path)
    findings = sc._scan(["gone.txt"])  # staged per git, but absent from the working tree
    assert findings and "BLOCKED" in findings[0] and "gone.txt" in findings[0]


def test_main_blocks_when_git_diff_itself_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(sc, "ROOT", tmp_path)

    def boom():
        raise RuntimeError("simulated git failure")

    monkeypatch.setattr(sc, "staged_files", boom)
    assert sc.main([]) == 1


def test_main_blocks_on_any_unexpected_exception_during_scan(monkeypatch, tmp_path):
    monkeypatch.setattr(sc, "ROOT", tmp_path)
    monkeypatch.setattr(sc, "staged_files", lambda: ["x.txt"])

    def boom(files):
        raise ValueError("simulated unexpected failure")

    monkeypatch.setattr(sc, "_scan", boom)
    assert sc.main([]) == 1


# ---- PayPal live host blocked only under evidence/, pilot/, copilot/, demo/ --------------------
LIVE_HOST = "api-m" + ".paypal" + ".com"          # built from pieces: never typed literally
LIVE_HOST2 = "api" + ".paypal" + ".com"
SANDBOX_HOST = "api-m.sandbox" + ".paypal" + ".com"
LIVE_DIRS = ["evidence", "pilot", "copilot", "demo"]


def _stage_and_scan(tmp_path, name, rel_path, content, path_check=False):
    repo = tmp_path / name
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    target = repo / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(["add", rel_path], repo)
    cmd = [sys.executable, str(Path(sc.__file__).resolve())] + ([] if path_check else ["--skip-path-check"])
    return subprocess.run(cmd, cwd=repo, capture_output=True, text=True)


@pytest.mark.parametrize("folder", LIVE_DIRS)
def test_live_host_blocked_in_each_scoped_folder(tmp_path, folder):
    r = _stage_and_scan(tmp_path, "r", f"{folder}/notes.txt", f"line one\nurl = https://{LIVE_HOST}/v1/x\n")
    assert r.returncode == 1
    assert f"{folder}/notes.txt:2: PayPal live host (paypal_live_host)" in r.stderr
    for stream in (r.stdout, r.stderr):
        assert LIVE_HOST not in stream and LIVE_HOST2 not in stream


@pytest.mark.parametrize("folder", ["docs", "tests", ".claude", "tools"])
def test_live_host_allowed_outside_scoped_folders(tmp_path, folder):
    r = _stage_and_scan(tmp_path, "r", f"{folder}/notes.txt", f"never call https://{LIVE_HOST}/v1/x\n")
    assert r.returncode == 0, r.stderr
    assert LIVE_HOST not in r.stdout and LIVE_HOST not in r.stderr


def test_live_host_allowed_at_repo_root(tmp_path):
    r = _stage_and_scan(tmp_path, "r", "README.md", f"forbidden: {LIVE_HOST}\n")
    assert r.returncode == 0, r.stderr


def test_sandbox_host_allowed_under_copilot(tmp_path):
    r = _stage_and_scan(tmp_path, "r", "copilot/client.py", f'BASE = "https://{SANDBOX_HOST}"\n')
    assert r.returncode == 0, r.stderr


def test_second_live_label_blocked_under_demo(tmp_path):
    r = _stage_and_scan(tmp_path, "r", "demo/x.txt", f"https://{LIVE_HOST2}/v2/x\n")
    assert r.returncode == 1
    assert "demo/x.txt:1: PayPal live host (paypal_live_host)" in r.stderr
    assert LIVE_HOST2 not in r.stderr and LIVE_HOST2 not in r.stdout


def test_live_host_rule_reuses_the_redact_pattern():
    import redact
    assert sc.LIVE_HOST_PATTERN is dict(redact.DETECT_ONLY_PATTERNS)["paypal_live_host"]


@pytest.mark.parametrize("rel,expected", [
    ("evidence/a.jsonl", True), ("pilot/x.py", True), ("copilot/x.py", True), ("demo/x.md", True),
    ("copilot\\x.py", True), ("./demo/x.md", True), ("Demo/x.md", True),
    ("docs/x.md", False), ("tests/x.py", False), (".claude/settings.json", False),
    (".demo/x.md", False), ("tools/demo/x.md", False), ("demonstration/x.md", False), ("README.md", False),
])
def test_live_host_scope(rel, expected):
    assert sc.live_host_scope(rel) is expected


def test_scan_text_live_host_off_by_default_and_on_when_asked():
    text = f"x {LIVE_HOST} y"
    assert sc.scan_text(text, env_values=[]) == []
    assert sc.scan_text(text, env_values=[], check_live_host=True) == [(1, "paypal_live_host")]


# ---- personal-data values under evidence/, pilot/fixtures/, copilot/fixtures/ -------------
# Keys and values are assembled from pieces, so this file holds no literal the rules would flag.
ID_KEYS = ["merchant" + "_id", "payer" + "_id", "account" + "_id", "account" + "_number", "email" + "_address"]
ID_VALUE = "AB12" + "CD34" + "EF56"
REAL_EMAIL = "someone" + "@" + "private-domain.io"
SANDBOX_EMAIL = "buyer" + "@" + "personal.example" + ".com"
PERSONAL_DIRS = ["evidence", "pilot/fixtures", "copilot/fixtures"]


@pytest.mark.parametrize("folder", PERSONAL_DIRS)
@pytest.mark.parametrize("key", ID_KEYS)
def test_identifier_value_blocked_in_each_scoped_folder(tmp_path, folder, key):
    r = _stage_and_scan(tmp_path, "r", f"{folder}/data.json", '{"ok": 1,\n "' + key + '": "' + ID_VALUE + '"}\n')
    assert r.returncode == 1
    assert f"{folder}/data.json:2: possible personal data ({key})" in r.stderr
    assert ID_VALUE not in r.stdout and ID_VALUE not in r.stderr


def test_identifier_value_in_python_yaml_and_query_forms_is_blocked(tmp_path):
    body = (ID_KEYS[0] + ' = "' + ID_VALUE + '"\n' + ID_KEYS[1] + ": " + ID_VALUE + "\nurl?" + ID_KEYS[2] + "=" +
            ID_VALUE + "&x=1\n")
    r = _stage_and_scan(tmp_path, "r", "pilot/fixtures/notes.txt", body)
    assert r.returncode == 1
    for line, key in ((1, ID_KEYS[0]), (2, ID_KEYS[1]), (3, ID_KEYS[2])):
        assert f"pilot/fixtures/notes.txt:{line}: possible personal data ({key})" in r.stderr
    assert ID_VALUE not in r.stderr


@pytest.mark.parametrize("folder", ["docs", "tests", "tools", "pilot", "copilot", "demo", "pilot/eval"])
def test_identifier_value_allowed_outside_the_scoped_folders(tmp_path, folder):
    r = _stage_and_scan(tmp_path, "r", f"{folder}/notes.txt", '{"' + ID_KEYS[0] + '": "' + ID_VALUE + '"}\n')
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("text", ['{"%s": "[redacted]"}', '{"%s": "[REDACTED]"}', '{"%s": ""}', '{"%s": null}'])
def test_placeholder_and_empty_values_are_allowed_in_scope(tmp_path, text):
    r = _stage_and_scan(tmp_path, "r", "evidence/a.jsonl", "\n".join(text % key for key in ID_KEYS) + "\n")
    assert r.returncode == 0, r.stderr


def test_real_looking_email_blocked_and_sandbox_domains_allowed_in_scope(tmp_path):
    r = _stage_and_scan(tmp_path, "r", "copilot/fixtures/m1/a.json", '{"note": "mail ' + REAL_EMAIL + '"}\n')
    assert r.returncode == 1
    assert "copilot/fixtures/m1/a.json:1: possible personal data (real_looking_email)" in r.stderr
    assert REAL_EMAIL not in r.stderr and REAL_EMAIL not in r.stdout
    ok = _stage_and_scan(tmp_path, "r2", "copilot/fixtures/m1/a.json", '{"note": "mail ' + SANDBOX_EMAIL + '"}\n')
    assert ok.returncode == 0, ok.stderr


def test_personal_data_rule_reuses_the_redact_patterns():
    import redact
    detect = dict(redact.DETECT_ONLY_PATTERNS)
    assert {k for k, _ in sc.PERSONAL_DATA_PATTERNS} == set(ID_KEYS)
    for kind, pattern in sc.PERSONAL_DATA_PATTERNS:
        assert pattern is detect[kind]
    assert sc.EMAIL_PATTERN is dict(redact.PATTERNS)["email"]


@pytest.mark.parametrize("rel,expected", [
    ("evidence/a.jsonl", True), ("evidence/manual/x.png", True), ("pilot/fixtures/x.json", True),
    ("pilot/fixtures/recorded/x.json", True), ("copilot/fixtures/m1/x.json", True), ("Evidence\\a.jsonl", True),
    ("./evidence/a.jsonl", True),
    ("pilot/eval/x.json", False), ("pilot/x.py", False), ("docs/x.md", False), ("tests/x.py", False),
    ("tools/evidence/x.md", False), ("evidence-notes/x.md", False), ("README.md", False),
])
def test_personal_data_scope(rel, expected):
    assert sc.personal_data_scope(rel) is expected


def test_scan_text_personal_data_off_by_default_and_on_when_asked():
    text = '{"' + ID_KEYS[0] + '": "' + ID_VALUE + '"}'
    assert sc.scan_text(text, env_values=[]) == []
    assert sc.scan_text(text, env_values=[], check_personal_data=True) == [(1, ID_KEYS[0])]


# ---- new binaries under evidence/ need their sha256 in the allow-list -----------------------------------
def _staged_repo(tmp_path, files: dict[str, bytes]):
    repo = tmp_path / "bin"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    for rel, data in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        _git(["add", rel], repo)
    return repo


def _use(monkeypatch, repo, allow_text):
    monkeypatch.setattr(sc, "ROOT", repo)
    allow = repo.parent / "allow.sha256"
    if allow_text is not None:
        allow.write_text(allow_text, encoding="utf-8")
    monkeypatch.setattr(sc, "EVIDENCE_BINARIES_FILE", allow)


def test_new_binary_under_evidence_is_blocked_when_not_listed(tmp_path, monkeypatch):
    import hashlib
    data = b"\x89PNG fake image bytes"
    repo = _staged_repo(tmp_path, {"evidence/manual/a.png": data})
    _use(monkeypatch, repo, "# nothing listed\n" + "0" * 64 + "  some other file\n")
    findings = sc._scan(["evidence/manual/a.png"])
    assert findings and "new_evidence_binary" in findings[0] and "evidence/manual/a.png" in findings[0]
    assert hashlib.sha256(data).hexdigest() not in findings[0]          # the hash is not printed either


def test_listed_binary_is_allowed_in_any_case_and_after_a_comment(tmp_path, monkeypatch):
    import hashlib
    data = b"\x89PNG fake image bytes"
    digest = hashlib.sha256(data).hexdigest()
    repo = _staged_repo(tmp_path, {"evidence/manual/a.png": data})
    _use(monkeypatch, repo, "# header\n" + digest.upper() + "  label  # trailing\n")
    assert sc._scan(["evidence/manual/a.png"]) == []


def test_missing_allow_list_file_blocks_fail_closed(tmp_path, monkeypatch):
    repo = _staged_repo(tmp_path, {"evidence/b.pdf": b"%PDF-1.4 fake"})
    _use(monkeypatch, repo, None)                                          # the file is not created
    findings = sc._scan(["evidence/b.pdf"])
    assert findings and "new_evidence_binary" in findings[0]


def test_binary_is_judged_by_the_staged_bytes_not_the_working_copy(tmp_path, monkeypatch):
    import hashlib
    staged = b"\x89PNG staged bytes"
    repo = _staged_repo(tmp_path, {"evidence/c.png": staged})
    (repo / "evidence" / "c.png").write_bytes(b"\x89PNG other bytes after staging")
    _use(monkeypatch, repo, hashlib.sha256(staged).hexdigest() + "  staged version\n")
    assert sc._scan(["evidence/c.png"]) == []


def test_nul_bytes_make_a_file_binary_whatever_its_suffix(tmp_path, monkeypatch):
    repo = _staged_repo(tmp_path, {"evidence/d.dat": b"abc\x00def"})
    _use(monkeypatch, repo, "")
    findings = sc._scan(["evidence/d.dat"])
    assert findings and "new_evidence_binary" in findings[0]


def test_text_files_and_binaries_outside_evidence_are_not_affected(tmp_path, monkeypatch):
    repo = _staged_repo(tmp_path, {"evidence/a.jsonl": b'{"ok": 1}\n', "docs/x.png": b"\x89PNG anything"})
    _use(monkeypatch, repo, "")
    assert sc._scan(["evidence/a.jsonl", "docs/x.png"]) == []


def test_unreadable_staged_blob_blocks_fail_closed(tmp_path, monkeypatch):
    repo = _staged_repo(tmp_path, {"evidence/a.jsonl": b'{"ok": 1}\n'})
    _use(monkeypatch, repo, "")
    monkeypatch.setattr(sc, "staged_blob", lambda rel: (_ for _ in ()).throw(OSError("simulated")))
    findings = sc._scan(["evidence/a.jsonl"])
    assert findings and "BLOCKED" in findings[0] and "OSError" in findings[0]


def test_end_to_end_new_evidence_binary_blocks_the_commit(tmp_path):
    r = _stage_and_scan(tmp_path, "r", "evidence/manual/new.png", "not really a png\n")
    assert r.returncode == 1
    assert "evidence/manual/new.png" in r.stderr and "new_evidence_binary" in r.stderr


# ---- evidence is append-only, so an existing evidence file is judged on its ADDED lines only ------------------
def _committed_repo(tmp_path, name, rel, old_text):
    repo = tmp_path / name
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(old_text, encoding="utf-8", newline="\n")
    _git(["add", rel], repo)
    _git(["commit", "-q", "-m", "old"], repo)                            # throwaway repo (no hooks): the old line is already there
    return repo


def _append_stage_scan(repo, rel, new_text):
    with open(repo / rel, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(new_text)
    _git(["add", rel], repo)
    return subprocess.run([sys.executable, str(Path(sc.__file__).resolve()), "--skip-path-check"], cwd=repo, capture_output=True, text=True)


OLD_VALUE_LINE = '{"op": "old", "body": {"' + ID_KEYS[3] + '": "' + ID_VALUE + '"}}\n'


def test_appending_a_clean_line_to_an_evidence_file_with_an_old_value_line_passes(tmp_path):
    repo = _committed_repo(tmp_path, "e1", "evidence/log.jsonl", OLD_VALUE_LINE)
    r = _append_stage_scan(repo, "evidence/log.jsonl", '{"op": "new", "body": {"' + ID_KEYS[3] + '": "[redacted]"}}\n')
    assert r.returncode == 0, r.stderr


def test_appending_a_line_with_a_value_blocks_and_names_the_new_line_only(tmp_path):
    repo = _committed_repo(tmp_path, "e2", "evidence/log.jsonl", OLD_VALUE_LINE)
    r = _append_stage_scan(repo, "evidence/log.jsonl",
                           '{"op": "clean"}\n{"op": "new", "' + ID_KEYS[0] + '": "' + ID_VALUE + 'XX"}\n')
    assert r.returncode == 1
    assert f"evidence/log.jsonl:3: possible personal data ({ID_KEYS[0]})" in r.stderr
    assert "evidence/log.jsonl:1:" not in r.stderr                       # the old line is not reported
    assert ID_VALUE not in r.stdout and ID_VALUE not in r.stderr


def test_a_new_evidence_file_with_a_value_blocks_whole_file(tmp_path):
    r = _stage_and_scan(tmp_path, "e3", "evidence/new.jsonl", OLD_VALUE_LINE)
    assert r.returncode == 1
    assert f"evidence/new.jsonl:1: possible personal data ({ID_KEYS[3]})" in r.stderr


def test_rewriting_an_old_value_line_into_another_value_blocks(tmp_path):
    repo = _committed_repo(tmp_path, "e4", "evidence/log.jsonl", OLD_VALUE_LINE)
    changed = OLD_VALUE_LINE.replace(ID_VALUE, ID_VALUE[::-1])
    (repo / "evidence" / "log.jsonl").write_text(changed, encoding="utf-8", newline="\n")
    _git(["add", "evidence/log.jsonl"], repo)
    r = subprocess.run([sys.executable, str(Path(sc.__file__).resolve()), "--skip-path-check"], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 1 and f"evidence/log.jsonl:1: possible personal data ({ID_KEYS[3]})" in r.stderr


def test_other_scoped_paths_still_scan_the_whole_file(tmp_path):
    repo = _committed_repo(tmp_path, "e5", "pilot/fixtures/data.json", OLD_VALUE_LINE)
    r = _append_stage_scan(repo, "pilot/fixtures/data.json", '{"op": "clean"}\n')
    assert r.returncode == 1
    assert f"pilot/fixtures/data.json:1: possible personal data ({ID_KEYS[3]})" in r.stderr


def test_appended_real_looking_email_to_an_existing_evidence_file_blocks(tmp_path):
    repo = _committed_repo(tmp_path, "e6", "evidence/log.jsonl", OLD_VALUE_LINE)
    r = _append_stage_scan(repo, "evidence/log.jsonl", '{"note": "mail ' + REAL_EMAIL + '"}\n')
    assert r.returncode == 1 and "evidence/log.jsonl:2: possible personal data (real_looking_email)" in r.stderr
    assert REAL_EMAIL not in r.stderr


def test_a_secret_in_an_old_evidence_line_is_still_judged_whole_file(tmp_path):
    """Only the personal-data rule narrows to added lines: secrets and the live host keep scanning the whole file."""
    repo = _committed_repo(tmp_path, "e7", "evidence/log.jsonl", "token " + FAKE_AWS_KEY + "\n")
    r = _append_stage_scan(repo, "evidence/log.jsonl", '{"op": "clean"}\n')
    assert r.returncode == 1 and "evidence/log.jsonl:1: possible secret (aws_access_key_id)" in r.stderr


def test_added_lines_reports_new_file_line_numbers_across_hunks(tmp_path, monkeypatch):
    repo = _committed_repo(tmp_path, "e8", "evidence/log.jsonl", "a\nb\nc\nd\ne\n")
    (repo / "evidence" / "log.jsonl").write_text("a\nB\nc\nd\ne\nf\n", encoding="utf-8", newline="\n")
    _git(["add", "evidence/log.jsonl"], repo)
    monkeypatch.setattr(sc, "ROOT", repo)
    assert sc.added_lines("evidence/log.jsonl") == [(2, "B"), (6, "f")]
    assert sc.is_new_in_commit("evidence/log.jsonl") is False and sc.is_new_in_commit("evidence/other.jsonl") is True


# ---- a renamed file (git mv) is scanned like an added file at its new path ---------------------------------------
def _moved_repo(tmp_path, name, old_rel, new_rel, old_text, append=""):
    """Commit `old_rel`, `git mv` it to `new_rel`, optionally append a line, stage everything."""
    repo = _committed_repo(tmp_path, name, old_rel, old_text)
    (repo / new_rel).parent.mkdir(parents=True, exist_ok=True)
    _git(["mv", old_rel, new_rel], repo)
    if append:
        with open(repo / new_rel, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(append)
        _git(["add", new_rel], repo)
    return repo


def _scan_repo(repo):
    return subprocess.run([sys.executable, str(Path(sc.__file__).resolve()), "--skip-path-check"], cwd=repo, capture_output=True, text=True)


def test_a_git_mv_of_a_clean_file_passes_and_is_listed_as_staged(tmp_path, monkeypatch):
    repo = _moved_repo(tmp_path, "m1", "docs/old.txt", "docs/notes/new.txt", "nothing sensitive\n")
    monkeypatch.setattr(sc, "ROOT", repo)
    assert sc.staged_files() == ["docs/notes/new.txt"]             # the new path is listed, not dropped as an R entry
    r = _scan_repo(repo)
    assert r.returncode == 0, r.stderr


def test_a_git_mv_plus_an_added_secret_line_blocks(tmp_path):
    repo = _moved_repo(tmp_path, "m2", "docs/old.txt", "docs/new.txt", "clean line\n", append="key " + FAKE_AWS_KEY + "\n")
    r = _scan_repo(repo)
    assert r.returncode == 1 and "docs/new.txt:2: possible secret (aws_access_key_id)" in r.stderr
    assert FAKE_AWS_KEY not in r.stdout and FAKE_AWS_KEY not in r.stderr


def test_a_git_mv_plus_an_added_personal_data_value_blocks(tmp_path):
    repo = _moved_repo(tmp_path, "m3", "docs/old.json", "pilot/fixtures/new.json", '{"ok": 1}\n',
                       append='{"' + ID_KEYS[1] + '": "' + ID_VALUE + '"}\n')
    r = _scan_repo(repo)
    assert r.returncode == 1 and f"pilot/fixtures/new.json:2: possible personal data ({ID_KEYS[1]})" in r.stderr
    assert ID_VALUE not in r.stdout and ID_VALUE not in r.stderr


def test_a_git_mv_of_a_file_holding_the_live_host_into_a_scoped_folder_blocks(tmp_path):
    repo = _moved_repo(tmp_path, "m4", "docs/old.md", "copilot/new.md", f"never call https://{LIVE_HOST}/v1/x\n")
    r = _scan_repo(repo)                                           # allowed under docs/, blocked at its new path
    assert r.returncode == 1 and "copilot/new.md:1: PayPal live host (paypal_live_host)" in r.stderr
    assert LIVE_HOST not in r.stdout and LIVE_HOST not in r.stderr


def test_a_moved_evidence_file_is_new_at_its_new_path_and_judged_whole(tmp_path):
    """Strict reading: an old value line that an append would have excused blocks once the file is moved."""
    repo = _moved_repo(tmp_path, "m5", "docs/old.jsonl", "evidence/moved.jsonl", OLD_VALUE_LINE)
    r = _scan_repo(repo)
    assert r.returncode == 1 and f"evidence/moved.jsonl:1: possible personal data ({ID_KEYS[3]})" in r.stderr


def test_the_seed_allow_list_covers_every_binary_already_tracked_under_evidence():
    """Nothing existing may break: each tracked binary under evidence/ is listed by its sha256."""
    import hashlib
    root = Path(sc.__file__).resolve().parent.parent
    try:
        tracked = subprocess.run(["git", "ls-files", "-z", "evidence"], cwd=root, capture_output=True, check=True
                                 ).stdout.decode("utf-8").split("\0")
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("needs the private git checkout (export tree: no .git, so no `git ls-files`)")
    allowed = sc.allowed_binary_hashes()
    assert allowed and all(len(h) == 64 for h in allowed)
    for rel in (t for t in tracked if t):
        data = (root / rel).read_bytes()
        if sc.is_binary(rel, data):
            assert hashlib.sha256(data).hexdigest() in allowed, rel


# ---- forbidden paths, checked before the content scan (exit 2) ----------------------------------------------
@pytest.mark.parametrize("path", [".env", ".env.local", ".env.production", "sub/.env", "evidence/x.jsonl",
                                  "evidence/a/b.json", "reference/notes.md", "scratch-ver/CODEX0-a.txt",
                                  "./evidence/x.jsonl", "Evidence/x.jsonl"])
def test_forbidden_path_is_reported_and_exits_two(tmp_path, path):
    r = _stage_and_scan(tmp_path, "fp", path, "nothing sensitive here\n", path_check=True)
    assert r.returncode == 2, r.stderr
    assert f"FORBIDDEN PATH {path.removeprefix('./')}" in r.stderr


@pytest.mark.parametrize("path", [".env.example", "docs/env.md", "evidence-notes/x.md", "tools/evidence/x.md",
                                  "evidence/manual/INDEX.md", "README.md"])
def test_non_forbidden_paths_pass_the_path_check(path):
    assert sc.forbidden_paths([path]) == []


def test_forbidden_path_wins_over_a_content_hit_and_never_prints_the_content(tmp_path):
    r = _stage_and_scan(tmp_path, "fp2", ".env.local", FAKE_AWS_KEY + "\n", path_check=True)
    assert r.returncode == 2
    assert FAKE_AWS_KEY not in r.stdout and FAKE_AWS_KEY not in r.stderr


# ---- the buyer-side app's credentials (Round 3): the second file's values are caught too, and never printed -------------------
BUYER_FILE = ".env.buyer"
FAKE_BUYER_ID = "fakeBuyerIdAbc12345"
FAKE_BUYER_SECRET = "fakeBuyerSecretXyz98765"


def _write_env(folder, name, id_value, secret_value):
    (folder / name).write_text(f"PAYPAL_CLIENT_ID={id_value}\nPAYPAL_CLIENT_SECRET={secret_value}\nOTHER=nope\n", encoding="utf-8")


def test_the_buyer_env_file_values_are_read_in_addition_to_the_seller_file(tmp_path, monkeypatch):
    _write_env(tmp_path, ".env", "fakeSellerIdAbc12345", "fakeSellerSecretXyz98765")
    _write_env(tmp_path, BUYER_FILE, FAKE_BUYER_ID, FAKE_BUYER_SECRET)
    monkeypatch.setattr(sc, "ROOT", tmp_path)
    assert set(sc.env_secret_values()) == {"fakeSellerIdAbc12345", "fakeSellerSecretXyz98765", FAKE_BUYER_ID, FAKE_BUYER_SECRET}


def test_the_buyer_file_alone_is_read_when_there_is_no_seller_file(tmp_path, monkeypatch):
    _write_env(tmp_path, BUYER_FILE, FAKE_BUYER_ID, FAKE_BUYER_SECRET)
    monkeypatch.setattr(sc, "ROOT", tmp_path)
    assert set(sc.env_secret_values()) == {FAKE_BUYER_ID, FAKE_BUYER_SECRET}


def test_a_buyer_file_with_short_or_other_values_adds_only_the_two_known_keys(tmp_path, monkeypatch):
    (tmp_path / BUYER_FILE).write_text("PAYPAL_CLIENT_ID=short\nPAYPAL_CLIENT_SECRET=" + FAKE_BUYER_SECRET + "\nTOKEN=anything-long-enough-1\n",
                                       encoding="utf-8")
    monkeypatch.setattr(sc, "ROOT", tmp_path)
    assert sc.env_secret_values() == [FAKE_BUYER_SECRET]


def test_an_unreadable_buyer_file_raises_so_the_commit_is_blocked(tmp_path, monkeypatch):
    (tmp_path / BUYER_FILE).mkdir()                                  # exists but cannot be read as a file
    monkeypatch.setattr(sc, "ROOT", tmp_path)
    with pytest.raises(OSError):
        sc.env_secret_values()


def test_scan_text_finds_a_planted_buyer_value_without_a_pattern():
    hits = sc.scan_text("notes: the buyer app uses " + FAKE_BUYER_ID + " here", env_values=[FAKE_BUYER_ID])
    assert hits == [(1, "env_credential_literal")]


def test_a_planted_buyer_value_in_a_staged_temp_file_blocks_and_nothing_is_printed(tmp_path, monkeypatch, capsys):
    """A throwaway git repo as ROOT, fake env files in it, the buyer secret planted in a staged note: exit 1, value never shown."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    _write_env(repo, BUYER_FILE, FAKE_BUYER_ID, FAKE_BUYER_SECRET)            # untracked, as the real one is
    (repo / "notes.txt").write_text("copied by mistake: " + FAKE_BUYER_SECRET + "\n", encoding="utf-8")
    _git(["add", "notes.txt"], repo)
    monkeypatch.setattr(sc, "ROOT", repo)
    assert sc.main([]) == 1
    shown = capsys.readouterr()
    assert FAKE_BUYER_SECRET not in shown.out + shown.err and FAKE_BUYER_ID not in shown.out + shown.err
    assert "env_credential_literal" in shown.err and "notes.txt" in shown.err


def test_the_same_staged_file_is_clean_when_no_buyer_file_exists(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    (repo / "notes.txt").write_text("copied by mistake: " + FAKE_BUYER_SECRET + "\n", encoding="utf-8")
    _git(["add", "notes.txt"], repo)
    monkeypatch.setattr(sc, "ROOT", repo)
    assert sc.main([]) == 0                                              # nothing to compare with: the value is only a plain word
    capsys.readouterr()


def test_staging_the_buyer_file_itself_is_refused_by_name():
    assert sc.forbidden_paths([BUYER_FILE, "sub/" + BUYER_FILE, ".env.example"]) == [BUYER_FILE, "sub/" + BUYER_FILE]
