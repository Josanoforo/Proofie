"""Tests for tools/export_public.py: acceptance tests T1-T9, T13 and T14 plus the rules around
them. Every test builds its own synthetic, throwaway git repository under
tmp_path; this module is NEVER run against the real repository (T10-T12 are run on the real release candidate).

Fixtures that look like secrets, identifiers, addresses, hosts or private terms are assembled from pieces at run time,
so this file holds no such literal (the staged-file scanner, and the export's own final scan, would otherwise flag it).
"""
import functools
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_public as ep  # noqa: E402
from conftest import in_git_checkout, is_private_checkout  # noqa: E402

@pytest.fixture(autouse=True)
def isolated_publication_rules(monkeypatch):
    """Legacy synthetic rule cases explicitly exercise flags-off publication.

    Canonical selection is checked separately from source bytes; no runtime
    feature flags are changed by this module fixture.
    """
    monkeypatch.setattr(ep, "PUBLISHED_NAMESPACES", [])


ROOT = Path(__file__).resolve().parents[1]
# The exporter loads sanitize() and the scan patterns from the EXPORTED COMMIT, so every synthetic repo carries the
# real bytes of these three files (a real commit always does).
NEEDS_CHECKOUT = "needs the private git checkout (export tree)"


@functools.lru_cache(maxsize=None)
def _committed(path):                      # HEAD blobs: other sessions may have uncommitted edits in the working tree
    if not in_git_checkout(ROOT):          # an export tree has no HEAD: the tree's own bytes are all there is
        return (ROOT / path).read_bytes()
    return subprocess.run(["git", "-C", str(ROOT), "show", "HEAD:" + path], check=True, capture_output=True).stdout


class _Real(Mapping):
    """The real bytes of the files every synthetic repo carries, read on first use (never at import)."""
    _NAMES = ("pilot/client.py", "tools/redact.py", "tools/personal_data_patterns.py", "tools/export_public.py")

    def __getitem__(self, key):
        if key not in self._NAMES:
            raise KeyError(key)
        return (ROOT / key).read_bytes() if key == "tools/export_public.py" else _committed(key)

    def __iter__(self):
        return iter(self._NAMES)

    def __len__(self):
        return len(self._NAMES)


REAL = _Real()

ACCOUNT_KEY = "account" + "_number"
ACCOUNT_VALUE = "1234" + "5678" + "90123"
MERCHANT_KEY = "merchant" + "_id"
MERCHANT_VALUE = "ABCDEFGH" + "JKLMN"
ACCOUNT_ID_VALUE = "FAKE" + "ACCOUNTID45"
EMAIL = "someone" + "@" + "private-domain.io"
STREET = "Calle" + " Real 123"
LIVE_HOST = "api-m" + ".paypal.com"
PRIVATE_TERM = "zq" + "xvw-private-term"
LF = chr(10)


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=T", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def make_repo(tmp_path, files=None, name="repo"):
    """A synthetic committed repo. Returns (repo path, commit sha)."""
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    contents = {**REAL, "README.md": b"Readme.\n", "copilot/app.py": b"x = 1\n",
                ep.CLAIMS_PATH: json.dumps({"schema": ep.CLAIMS_SCHEMA, "claims": []}), **(files or {})}
    for rel, data in contents.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "synthetic")
    return repo, _git(repo, "rev-parse", "HEAD")


def terms_file(tmp_path, terms=(PRIVATE_TERM,), name="terms.txt"):
    path = tmp_path / name                                   # outside the repo
    path.write_text("\n".join(["# private terms", "", *terms]) + "\n", encoding="utf-8")
    return path


def test_env_template_contains_only_published_feature_flags(tmp_path, monkeypatch):
    """The exported template cannot turn on a namespace absent from that export."""
    monkeypatch.setattr(ep, "PUBLISHED_NAMESPACES", ["fulfilment"])
    template = (b"# Synthetic template\nCOPILOT_RULES_COUNTRY=MX\n"
                b"COPILOT_FEATURE_FULFILMENT=true\n"
                b"COPILOT_FEATURE_FULFILMENT_REGISTER=false\n"
                b"COPILOT_FEATURE_DRAFT=false\n"
                b"COPILOT_FEATURE_WEBHOOKS_VERIFY=false\n")
    repo, sha = make_repo(tmp_path, {".env.example": template,
        "copilot/features/fulfilment/__init__.py": b"",
        "copilot/features/draft/__init__.py": b"",
        "copilot/features/webhooks/__init__.py": b""})
    outputs, files, *_ = ep.build_outputs(repo, sha, None)
    expected = (b"# Synthetic template\nCOPILOT_RULES_COUNTRY=MX\n"
                b"COPILOT_FEATURE_FULFILMENT=true\n"
                b"COPILOT_FEATURE_FULFILMENT_REGISTER=false\n")
    assert outputs[".env.example"] == expected
    entry = next(item for item in files if item["path"] == ".env.example")
    assert entry["action"] == "sanitized"
    assert entry["source_sha256"] == hashlib.sha256(template).hexdigest()
    assert entry["sha256"] == hashlib.sha256(expected).hexdigest()


def run(repo, dest, sha, terms, capsys=None, extra=()):
    code = ep.main([str(dest), "--commit", sha, "--private-terms", str(terms), "--repo", str(repo), *extra])
    out = capsys.readouterr() if capsys else None
    return code, out


def tree(dest):
    return {p.relative_to(dest).as_posix(): p.read_bytes() for p in sorted(dest.rglob("*")) if p.is_file()}


def manifest(dest):
    return json.loads((dest / "MANIFEST.json").read_text(encoding="utf-8"))


def excluded_file(dest):
    """The private list of excluded paths, written NEXT TO the export folder (never inside it)."""
    return dest.parent / (dest.name + ep.EXCLUDED_SUFFIX)


def excluded_entries(dest):
    return json.loads(excluded_file(dest).read_text(encoding="utf-8"))["excluded"]


def blob_bytes(repo, sha, path):
    return subprocess.run(["git", "-C", str(repo), "show", f"{sha}:{path}"], check=True, capture_output=True).stdout


def claims_doc(*claims):
    return json.dumps({"schema": ep.CLAIMS_SCHEMA, "claims": list(claims)})


def claim(cid="EV-01", file="evidence/a.jsonl", lines=(1, 2), proves="A read-only run against the sandbox.",
          used_by=("README.md",), recorded="2026-10-05T06:54Z"):
    return {"id": cid, "file": file, "lines": None if lines is None else list(lines), "recorded_utc": recorded, "proves": proves,
            "used_by": used_by if isinstance(used_by, str) else list(used_by)}


def test_dr1_index_use_labels_do_not_cite_unpublished_material_and_keep_provenance():
    import copy
    private_register = "docs/" + "CLAIMS-REGISTER.md C01"
    claims = [claim("EV-01", used_by=("README.md", "Devpost: Accomplishments", private_register)),
              claim("EV-02", used_by=("Devpost: Accomplishments", private_register))]
    original = copy.deepcopy(claims)
    evidence = {"evidence/a.jsonl": b'{"demo":1}\n{"demo":2}\n'}
    index = ep.build_evidence_index("1" * 40, evidence, claims, False).decode("utf-8")
    assert "CLAIMS-REGISTER" not in index and "Devpost:" not in index
    assert "README.md" in index and "Private material (not published)" in index
    assert "a.jsonl (private file, hashed)" in index
    assert ep.sha256(b'{"demo":1}\n{"demo":2}') in index
    assert ep.sha256(evidence["evidence/a.jsonl"]) in index
    assert "| EV-01 |" in index and "| EV-02 |" in index
    assert "| 1-2 |" in index and claims == original


def test_dr1_env_comment_describes_enabled_release_settings_not_defaults():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    comment = next(line for line in text.splitlines()
                   if line.startswith("# ") and "feature" in line.lower() and "flags" in line.lower())
    rows = re.findall(r'^COPILOT_FEATURE_[A-Z_]+\s*=\s*(true|false)\s*$', text, re.M)
    match = re.search(r'(\d+) settings, (\d+) enabled', comment)
    assert match and int(match.group(1)) == len(rows)
    assert int(match.group(2)) == rows.count("true")
    assert "Code defaults stay off" in comment
    assert "Lane-2" not in comment and "Off by default" not in comment


def report(tmp_path):
    """The most recently written `<DEST>.REDACT-SCAN.txt` in tmp_path (the report sits beside the export folder)."""
    newest = max(tmp_path.glob("*" + ep.REPORT_SUFFIX), key=lambda p: p.stat().st_mtime_ns)
    return newest.read_text(encoding="utf-8")


# ===================================================================================================== glob matcher
@pytest.mark.parametrize("pattern,path,expected", [
    ("copilot/**", "copilot/app.py", True), ("copilot/**", "copilot/web/static/app.css", True),
    ("copilot/**", "copilotx/app.py", False), ("pilot/**.py", "pilot/a.py", True),
    ("pilot/**.py", "pilot/eval/b/c.py", True), ("pilot/**.py", "pilot/a.txt", False),
    ("evidence/*.jsonl", "evidence/a.jsonl", True), ("evidence/*.jsonl", "evidence/manual/a.jsonl", False),
    ("tools/test_*.py", "tools/test_x.py", True), ("tools/test_*.py", "tools/hooks/test_guard.py", False),
    (".venv*/**", ".venv-ref/Lib/x.py", True), ("**.pyc", "a/b/c.pyc", True), ("**.pyc", "a.pyc", True),
    ("**/__pycache__/**", "copilot/__pycache__/x.pyc", True), ("__pycache__/**", "__pycache__/x", True),
    ("docs/ENCARGO-*", "docs/ENCARGO-D2.md", True), ("docs/ENCARGO-*", "docs/sub/ENCARGO-D2.md", False),
    ("LICENSE", "LICENSE", True), ("LICENSE", "docs/LICENSE", False)])
def test_the_glob_matcher(pattern, path, expected):
    assert ep.match(pattern, path) is expected


# ======================================================================================== T1 reads from the commit
def test_t1_the_export_reads_the_commit_not_the_working_tree(tmp_path):
    repo, sha = make_repo(tmp_path, {"copilot/b.py": "b = 1\n", "docs/AI-USAGE.md": "usage\n",
                                     "evidence/a.jsonl": '{"x": 1}\n'})
    terms = terms_file(tmp_path)
    assert run(repo, tmp_path / "clean", sha, terms)[0] == 0
    (repo / "copilot" / "app.py").write_text("x = 'changed after the commit'\n", encoding="utf-8")   # modify
    (repo / "copilot" / "new_untracked.py").write_text("y = 2\n", encoding="utf-8")                  # add
    (repo / "copilot" / "b.py").unlink()                                                             # delete
    (repo / "README.md").write_text("changed readme\n", encoding="utf-8")
    (repo / "evidence" / "a.jsonl").write_text('{"x": 2}\n{"x": 3}\n', encoding="utf-8")              # the index too
    _git(repo, "add", "-A")                                                                          # even staged
    assert run(repo, tmp_path / "dirty", sha, terms)[0] == 0
    assert tree(tmp_path / "dirty") == tree(tmp_path / "clean")                                      # byte-identical
    assert b"changed" not in (tmp_path / "dirty" / "copilot" / "app.py").read_bytes()
    assert not (tmp_path / "dirty" / "copilot" / "new_untracked.py").exists()
    assert (tmp_path / "dirty" / "copilot" / "b.py").exists()


# ===================================================================================================== T2 input safety
def test_t2_an_existing_destination_a_non_commit_and_an_unknown_commit_write_nothing(tmp_path):
    repo, sha = make_repo(tmp_path)
    terms = terms_file(tmp_path)
    existing = tmp_path / "exists"
    existing.mkdir()
    (existing / "keep.txt").write_text("mine", encoding="utf-8")
    assert run(repo, existing, sha, terms)[0] == 2
    assert [p.name for p in existing.iterdir()] == ["keep.txt"]                       # not touched
    tree_oid = _git(repo, "rev-parse", "HEAD^{tree}")
    for bad in (tree_oid, "0" * 40, "no-such-ref"):
        dest = tmp_path / ("out-" + bad[:6])
        assert run(repo, dest, bad, terms)[0] == 2
        assert not dest.exists()
    assert not list(tmp_path.glob("*" + ep.REPORT_SUFFIX))                               # no report either


def test_t2_a_commit_ish_is_resolved_to_a_full_forty_hex_sha(tmp_path):
    repo, sha = make_repo(tmp_path)
    dest = tmp_path / "out"
    assert ep.main([str(dest), "--commit", "HEAD", "--private-terms", str(terms_file(tmp_path)), "--repo", str(repo)]) == 0
    assert manifest(dest)["source_commit"] == sha and re.fullmatch(r"[0-9a-f]{40}", sha)


# ===================================================================================================== T3 default deny
def test_t3_an_unmatched_file_is_absent_and_listed_as_default_deny(tmp_path):
    repo, sha = make_repo(tmp_path, {"weird/new.txt": "x\n", "docs/NEW-INTERNAL.md": "y\n", "copilot/ok.py": "z\n"})
    dest = tmp_path / "out"
    assert run(repo, dest, sha, terms_file(tmp_path))[0] == 0
    assert not (dest / "weird").exists() and not (dest / "docs" / "NEW-INTERNAL.md").exists()
    assert (dest / "copilot" / "ok.py").exists()
    excluded = {e["path"]: e["rule"] for e in excluded_entries(dest)}
    assert excluded["weird/new.txt"] == "default-deny" and excluded["docs/NEW-INTERNAL.md"] == "docs:not-public"


# ===================================================================================================== T4 exclude wins
EXCLUDED_EVEN_IF_INCLUDED = [
    "CLAUDE.md", ".claude/settings.json", "tools/hooks/guard.py", "REVIEW.md", "evidence/manual/x.png",
    "evidence/a.jsonl", "evidence/b.json", "evidence/manual/INDEX.md", "pilot/fixtures/recorded/x.json",
    "pilot/fixtures/recorded/sub/y.json", "docs/evidence-claims.json",
    ".env", ".env.local", "data/copilot.db", ".venv/Lib/x.py", ".venv-ref/Lib/y.py", "reference/a.txt",
    "copilot/__pycache__/x.pyc", "pilot/__pycache__/y.cpython-312.pyc", "tests/local/__pycache__/z.pyc", "tests/stray.pyc",
    "STATUS.md", "docs/ENCARGO-X.md", "docs/INFORME-B2-SUPERVISOR.md", "docs/bitacoras/construccion.md", "docs/MESAS.md",
    "docs/TRANSFER-1.md", "docs/LESSONS.md", "docs/WORKFLOW.md", "docs/HOOKS.md", "docs/AGENT-CALIBRATION.md",
    "docs/REGISTRO-HALLAZGOS.md", "docs/PLAN-ESTRATEGICO.md", "docs/DECISIONES-SUPERVISOR-B2.md", "docs/TABLE2-BOARD.md",
    "docs/SUBMISSION-PLAN.md", "docs/RELEASE-CHECKLIST.md", "docs/JUDGE-ACCESS.md", "docs/COMPLIANCE.md",
    "docs/PRIVACY-INVENTORY.md", "docs/CLAIMS-REGISTER.md", "docs/DEVPOST.md", "docs/VIDEO-SCRIPT.md",
    "docs/OBS-RECORDING.md", "docs/PRESENTATION-PROPOSAL.md", "docs/UX-CRITERIA.md", "docs/ENVIRONMENT.md",
    "docs/DISPUTAS-SANDBOX-2026-10.md", "docs/PREPARACION-DISPUTAS.md", "research/notes.md", "research/eval-results/r.json",
    "docs/SECURITY-REVIEW-2026-10-05.md", "tools/env/setup.ps1"]


def test_t4_every_exclude_pattern_wins_even_where_an_include_rule_also_matches(tmp_path):
    files = {p: "x\n" for p in EXCLUDED_EVEN_IF_INCLUDED}
    files["docs/evidence-claims.json"] = claims_doc()          # excluded from the output, but read for the index
    files.update({".env.example": "EXAMPLE=example-placeholder\n", "docs/AI-USAGE.md": "u\n", "pilot/fixtures/synthetic.txt": "s\n"})
    repo, sha = make_repo(tmp_path, files)
    dest = tmp_path / "out"
    assert run(repo, dest, sha, terms_file(tmp_path))[0] == 0
    present = set(tree(dest))
    assert not (set(EXCLUDED_EVEN_IF_INCLUDED) & present), sorted(set(EXCLUDED_EVEN_IF_INCLUDED) & present)
    assert {".env.example", "docs/AI-USAGE.md", "pilot/fixtures/synthetic.txt"} <= present                                  # the exceptions publish
    for path in EXCLUDED_EVEN_IF_INCLUDED:
        assert ep.classify(path)[0] == "excluded", path


def test_t4_every_exclude_rule_has_a_representative_that_is_excluded():
    for pattern, _exceptions in ep.EXCLUDE:
        sample = pattern.replace("**", "x").replace("*", "a")
        assert ep.classify(sample)[0] == "excluded", (pattern, sample)


# ======================================================================================= T5 no response bodies
def test_t5_no_evidence_or_recorded_fixture_and_no_derivative_of_them_is_in_the_output(tmp_path):
    files = {"evidence/a.jsonl": json.dumps({MERCHANT_KEY: MERCHANT_VALUE}) + LF,
             "evidence/b.json": json.dumps({MERCHANT_KEY: MERCHANT_VALUE}),
             "pilot/fixtures/recorded/x.json": json.dumps({"body": {MERCHANT_KEY: MERCHANT_VALUE}})}
    repo, sha = make_repo(tmp_path, files)
    dest = tmp_path / "out"
    assert run(repo, dest, sha, terms_file(tmp_path))[0] == 0
    present = tree(dest)
    assert not [p for p in present if p.startswith("evidence/") or p.startswith("pilot/fixtures/recorded/")]
    assert all(MERCHANT_VALUE.encode() not in data for data in present.values())      # not even a derivative
    rules = {e["path"]: e["rule"] for e in excluded_entries(dest)}
    assert rules["evidence/a.jsonl"] == rules["evidence/b.json"] == "exclude:evidence/**"
    assert rules["pilot/fixtures/recorded/x.json"] == "exclude:pilot/fixtures/recorded/**"
    assert ep.INDEX_NAME in present and ep.SANITIZED == []                             # the index is the public form
    assert {f["path"]: f["action"] for f in manifest(dest)["files"]}[ep.INDEX_NAME] == "generated"


def test_t5_the_unused_sanitiser_path_still_works_when_a_later_decision_enables_it(tmp_path, monkeypatch):
    """The mechanism stays in the tool, EMPTY. Enabling it is two edits (leave EXCLUDE, join SANITIZED); this proves
    the path: same line count, every line valid JSON, no original value, an invalid line fails and names file and line."""
    source = LF.join([json.dumps({"op": "a", "body": {ACCOUNT_KEY: ACCOUNT_VALUE, MERCHANT_KEY: MERCHANT_VALUE,
                                                       "account_id": ACCOUNT_ID_VALUE}}), "",
                      json.dumps({"contact": EMAIL, "ship": {"address": {"line1": STREET, "state": "NL"}}}),
                      json.dumps({"checks": [{"id": "x", "state": "verified"}]})]) + LF
    repo, sha = make_repo(tmp_path, {"evidence/step.jsonl": source,
                                     "evidence/state.json": json.dumps({MERCHANT_KEY: MERCHANT_VALUE})})
    monkeypatch.setattr(ep, "EXCLUDE", [e for e in ep.EXCLUDE if e[0] != "evidence/**"])
    monkeypatch.setattr(ep, "SANITIZED", ["evidence/*.jsonl", "evidence/*.json"])
    dest = tmp_path / "out"
    assert run(repo, dest, sha, terms_file(tmp_path))[0] == 0
    out = (dest / "evidence" / "step.jsonl").read_text(encoding="utf-8")
    assert len(out.splitlines()) == len(source.splitlines())
    for line in out.splitlines():
        if line.strip():
            json.loads(line)
    for original in (ACCOUNT_VALUE, MERCHANT_VALUE, ACCOUNT_ID_VALUE, EMAIL, STREET):
        assert original not in out
    assert json.loads(out.splitlines()[3])["checks"][0]["state"] == "verified"
    entry = next(f for f in manifest(dest)["files"] if f["path"] == "evidence/step.jsonl")
    assert entry["action"] == "sanitized" and entry["lines"] == 4 and entry["source_sha256"] != entry["sha256"]


def test_t5_the_unused_sanitiser_path_fails_on_an_invalid_line_naming_file_and_line(tmp_path, monkeypatch, capsys):
    bad = json.dumps({"ok": 1}) + LF + "{" + "no json " + MERCHANT_VALUE + LF
    repo, sha = make_repo(tmp_path, {"evidence/broken.jsonl": bad})
    monkeypatch.setattr(ep, "EXCLUDE", [e for e in ep.EXCLUDE if e[0] != "evidence/**"])
    monkeypatch.setattr(ep, "SANITIZED", ["evidence/*.jsonl"])
    dest = tmp_path / "out"
    code, out = run(repo, dest, sha, terms_file(tmp_path), capsys)
    assert code == 2 and not dest.exists()
    assert "evidence/broken.jsonl" in out.err and "line 2" in out.err and MERCHANT_VALUE not in out.err + out.out


def test_the_sanitiser_functions_keep_the_line_structure():
    redact_marker = lambda value: {"k": "[redacted]"}                                     # noqa: E731 - stand-in
    data, lines = ep.sanitize_jsonl(b'{"a": 1}\n\n{"b": 2}', redact_marker, "evidence/x.jsonl")
    assert lines == 3 and data.split(b"\n") == [b'{"k": "[redacted]"}', b"", b'{"k": "[redacted]"}']
    with pytest.raises(ep.ExportError):
        ep.sanitize_json(b"not json", redact_marker, "evidence/x.json")


# ============================================================================ T6 identifier keys with a value
@pytest.mark.parametrize("line", [
    '{"%s": "%s"}' % (MERCHANT_KEY, MERCHANT_VALUE),
    '{"payer' + '_id": "%s"}' % MERCHANT_VALUE,
    "account" + '_id = "%s"' % ACCOUNT_ID_VALUE,
    '{"%s": "%s"}' % (ACCOUNT_KEY, ACCOUNT_VALUE),
    '"email' + '_address": "%s"' % EMAIL,
    "url = 'https://x.test/?" + MERCHANT_KEY + "=" + MERCHANT_VALUE + "'",
    MERCHANT_KEY + ": " + MERCHANT_VALUE,
    "contact " + EMAIL])
def test_t6_an_included_file_with_an_identifier_value_or_a_real_looking_email_blocks_without_the_value(tmp_path, line):
    repo, sha = make_repo(tmp_path, {"copilot/data.py": "a = 1\n" + line + LF})
    assert run(repo, tmp_path / "out", sha, terms_file(tmp_path))[0] == 1
    text = report(tmp_path)
    assert re.search(r"BLOCK copilot/data\.py:2 (identifier_value|email_address|merchant_id|payer_id|account_id|account_number)", text)
    for secret in (MERCHANT_VALUE, ACCOUNT_VALUE, ACCOUNT_ID_VALUE, EMAIL, "private-domain"):
        assert secret not in text


@pytest.mark.parametrize("line", [
    '{"%s": "[redacted]"}' % MERCHANT_KEY,                       # the sanitiser's marker
    '{"%s": ""}' % MERCHANT_KEY,                                  # empty
    MERCHANT_KEY + " = None",
    "merchant" + "_id = norm_value_from_code",                    # a variable, not an id
    "contact someone@example.com, a@example.org",
    '{"email' + '_address": "[email]"}'])
def test_t6_placeholders_and_allowed_domains_do_not_block(tmp_path, line):
    repo, sha = make_repo(tmp_path, {"copilot/data.py": "a = 1\n" + line + LF})
    assert run(repo, tmp_path / "out", sha, terms_file(tmp_path))[0] == 0


def test_t6_an_allowed_synthetic_range_passes_only_on_its_allow_listed_path(tmp_path):
    demo = '{"%s": "DEMOABCDEFGHJ"}' % MERCHANT_KEY                # 13 characters, DEMO prefix
    repo, sha = make_repo(tmp_path, {"copilot/fixtures/m1/c.json": demo + LF})
    assert run(repo, tmp_path / "ok", sha, terms_file(tmp_path))[0] == 0
    repo2, sha2 = make_repo(tmp_path, {"copilot/other/c.json": demo + LF}, name="repo2")
    assert run(repo2, tmp_path / "blocked", sha2, terms_file(tmp_path))[0] == 1
    assert "BLOCK copilot/other/c.json:1 identifier_value" in report(tmp_path)


def test_t6_the_example_configuration_may_hold_placeholder_values(tmp_path):
    repo, sha = make_repo(tmp_path, {".env.example": "OWNER=you@example.com" + LF + "PAYPAL_CLIENT_" + "ID=your-client-id-here-please\n"})
    assert run(repo, tmp_path / "out", sha, terms_file(tmp_path))[0] == 0


def test_t6_a_real_looking_credential_assignment_blocks_and_a_placeholder_does_not(tmp_path):
    key = "PAYPAL_CLIENT" + "_" + "SECRET"
    shape = "A1b2" * 8
    repo, sha = make_repo(tmp_path, {"copilot/a.txt": key + chr(61) + shape + LF})
    assert run(repo, tmp_path / "out", sha, terms_file(tmp_path))[0] == 1
    assert "BLOCK copilot/a.txt:1 credential_assignment" in report(tmp_path) and "A1b2" not in report(tmp_path)


# ====================================================================================================== T7 manifest
def test_t7_the_manifest_is_complete_clean_and_byte_identical_across_runs(tmp_path):
    repo, sha = make_repo(tmp_path, {"docs/AI-USAGE.md": "u\n", "evidence/e.jsonl": json.dumps({"a": 1}) + LF,
                                     "weird/x": "x\n", "docs/MCP.md": "m\n",
                                     "docs/evidence-claims.json": claims_doc(claim(file="evidence/e.jsonl", lines=(1, 1)))})
    terms = terms_file(tmp_path)
    one, two = tmp_path / "one", tmp_path / "two"
    assert run(repo, one, sha, terms)[0] == 0 and run(repo, two, sha, terms)[0] == 0
    first, second = (one / "MANIFEST.json").read_bytes(), (two / "MANIFEST.json").read_bytes()
    assert first == second                                                             # determinism: identical bytes
    data = json.loads(first)
    assert data["schema"] == "proofie-export/1" and re.fullmatch(r"[0-9a-f]{40}", data["source_commit"])
    assert data["source_commit"] == sha and data["rules_version"] == ep.rules_version()
    # the tool AT the source commit: the blob git stored (line endings may differ from the working copy under autocrlf)
    assert data["tool_sha256"] == ep.sha256(blob_bytes(repo, sha, "tools/export_public.py"))
    paths = [f["path"] for f in data["files"]]
    assert paths == sorted(paths) and [e["path"] for e in excluded_entries(one)] == sorted(e["path"] for e in excluded_entries(one))
    for entry in data["files"]:
        assert ep.sha256((one / entry["path"]).read_bytes()) == entry["sha256"]       # every sha256 matches the bytes
    text = first.decode("utf-8")
    assert not re.search(r"[A-Za-z]:\\|\\\\|" + "/Us" + "ers/|" + "/ho" + "me/", text)  # no drive, backslash, local path
    assert str(tmp_path).replace("\\", "/") not in text and tmp_path.name not in text
    assert "MANIFEST.json" not in paths and data["scan"]["hits"] == 0
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:", text)                            # no timestamps
    assert "evidence_dir" not in data


def test_the_manifest_lists_no_excluded_path_only_a_count_by_rule_and_a_hash(tmp_path):
    repo, sha = make_repo(tmp_path, {"docs/AI-USAGE.md": "u\n", "weird/x": "x\n", "internal/secret-plan.md": "p\n",
                                     "research/notes.md": "n\n"})
    dest = tmp_path / "out"
    assert run(repo, dest, sha, terms_file(tmp_path))[0] == 0
    text = (dest / "MANIFEST.json").read_text(encoding="utf-8")
    summary = json.loads(text)["excluded"]
    assert set(summary) == {"count", "by_rule", "sha256"}
    assert summary["count"] >= 3 and sum(summary["by_rule"].values()) == summary["count"]
    assert list(summary["by_rule"]) == sorted(summary["by_rule"])
    for name in ("secret-plan", "research/notes", "weird/x"):
        assert name not in text, name                                    # no excluded path name anywhere in the manifest


def test_the_full_excluded_list_is_a_private_sibling_whose_hash_is_the_manifests(tmp_path):
    repo, sha = make_repo(tmp_path, {"docs/AI-USAGE.md": "u\n", "weird/x": "x\n", "internal/secret-plan.md": "p\n"})
    terms = terms_file(tmp_path)
    one, two = tmp_path / "one", tmp_path / "two"
    assert run(repo, one, sha, terms)[0] == 0 and run(repo, two, sha, terms)[0] == 0
    entries = excluded_entries(one)
    assert {"weird/x", "internal/secret-plan.md"} <= {e["path"] for e in entries}
    lines = sorted(e["path"] + "\t" + e["rule"] + "\n" for e in entries)
    assert hashlib.sha256("".join(lines).encode("utf-8")).hexdigest() == manifest(one)["excluded"]["sha256"]
    assert len(entries) == manifest(one)["excluded"]["count"]
    assert excluded_file(one).read_bytes() == excluded_file(two).read_bytes()          # reproducible like the manifest
    assert not any("EXCLUDED" in p.name for p in one.rglob("*"))                      # never inside the tree, so no ZIP of the tree can hold it
    assert excluded_file(one).parent == one.parent


# ===================================================================================================== T8 scan blocks
def test_t8_a_hit_outside_the_allowed_exceptions_blocks_and_the_report_names_path_line_pattern_only(tmp_path, capsys):
    repo, sha = make_repo(tmp_path, {"copilot/leak.py": "a = 1\n# contact " + EMAIL + "\nb = 2\n"})
    dest = tmp_path / "out"
    code, out = run(repo, dest, sha, terms_file(tmp_path), capsys)
    assert code == 1
    text = report(tmp_path)                                                           # next to the tree, not inside it
    assert "BLOCK copilot/leak.py:2 email" in text and EMAIL not in text and "private-domain" not in text
    assert not list(dest.glob("*" + ep.REPORT_SUFFIX)) and not (dest / "MANIFEST.json").exists()   # blocked = no manifest
    assert (tmp_path / ("out" + ep.REPORT_SUFFIX)).is_file()                          # <DEST>.REDACT-SCAN.txt in the parent
    assert "BLOCKED" in out.err


def test_t8_each_blocked_kind_is_named_without_its_value(tmp_path):
    local = "/Us" + "ers/" + "someone" + "/x"
    repo, sha = make_repo(tmp_path, {"copilot/a.py": "p = '" + local + "'\n",
                                     "copilot/c.txt": '{"' + ACCOUNT_KEY + '": "' + ACCOUNT_VALUE + '"}\n'})
    assert run(repo, tmp_path / "out", sha, terms_file(tmp_path))[0] == 1
    text = report(tmp_path)
    assert "copilot/a.py:1 local_path_posix_users" in text
    assert "copilot/c.txt:1 account_number" in text
    assert local not in text and ACCOUNT_VALUE not in text


def test_t8_allowed_exceptions_alone_exit_zero_and_are_counted_with_their_justification(tmp_path):
    files = {
        "tests/local/test_context.py": "HOST = 'https://" + LIVE_HOST + "'\n",
        "tests/local/test_send.py": "HOST = '" + LIVE_HOST + "'\n",
        "pilot/eval/case.txt": "Address: " + STREET + "\n",
        "tools/test_scan_staged.py": "x = 'client_" + "secret=" + "A" * 24 + "'\n",
    }
    repo, sha = make_repo(tmp_path, files)
    dest = tmp_path / "out"
    assert run(repo, dest, sha, terms_file(tmp_path))[0] == 0
    scan = manifest(dest)["scan"]
    assert scan["hits"] == 0 and scan["allowed_exceptions"] == 4
    by_rule = {(e["path"], e["kind"]): e for e in scan["exceptions"]}
    assert by_rule[("tests/local/test_context.py", "paypal_live_host")]["hits"] == 1
    assert by_rule[("pilot/eval/**", "street_address")]["reason"] == "synthetic data"
    text = report(tmp_path)
    assert "allowed tests/local/test_context.py:1 paypal_live_host" in text and LIVE_HOST not in text


def test_t8_the_same_content_outside_the_exception_paths_blocks(tmp_path):
    repo, sha = make_repo(tmp_path, {"copilot/not_a_test.py": "HOST = '" + LIVE_HOST + "'\n",
                                     "copilot/addr.txt": "Address: " + STREET + "\n"})
    assert run(repo, tmp_path / "out", sha, terms_file(tmp_path))[0] == 1
    text = report(tmp_path)
    assert "BLOCK copilot/not_a_test.py:1 paypal_live_host" in text and "BLOCK copilot/addr.txt:1 street_address" in text


def _without_account_number_rule(text):
    """The shared patterns module minus the account_number key shape (removed by behaviour: the dict entry)."""
    entry = re.compile(r'^[ \t]*"account_number":[^\r\n]*\r?\n', re.M)
    assert len(entry.findall(text)) == 1
    return entry.sub("", text)


def test_the_scan_uses_the_patterns_of_the_exported_commit(tmp_path):
    """The identifier patterns are loaded from the commit's own bytes (tools/personal_data_patterns.py): a commit whose
    module lacks the account_number rule does not report that kind; one that has the rule does."""
    leak = {"copilot/c.txt": '{"' + ACCOUNT_KEY + '": ' + ACCOUNT_VALUE + '}' + LF}
    repo, sha = make_repo(tmp_path, leak)
    assert run(repo, tmp_path / "with-rule", sha, terms_file(tmp_path))[0] == 1
    assert "copilot/c.txt:1 account_number" in report(tmp_path)
    shared = REAL["tools/personal_data_patterns.py"].decode("utf-8")
    without = _without_account_number_rule(shared)
    assert '"account_number":' not in without and "account[_-]?number" not in without   # the key shape is gone
    repo2, sha2 = make_repo(tmp_path, {**leak, "tools/personal_data_patterns.py": without}, name="repo2")
    assert run(repo2, tmp_path / "without-rule", sha2, terms_file(tmp_path))[0] == 1   # the export's own rule still blocks
    text = report(tmp_path)
    assert "BLOCK copilot/c.txt:1 identifier_value" in text and "account_number" not in text   # the commit's rule is gone


def test_a_working_tree_change_to_the_shared_patterns_does_not_affect_the_export(tmp_path, monkeypatch):
    """6(b): redact.py imports personal_data_patterns; the export must use the commit's blob, never a copy found through
    sys.path or in the working tree, and must leave this process's own module table as it was."""
    leak = {"copilot/c.txt": '{"' + ACCOUNT_KEY + '": ' + ACCOUNT_VALUE + '}' + LF}
    repo, sha = make_repo(tmp_path, leak)
    terms = terms_file(tmp_path)
    assert run(repo, tmp_path / "before", sha, terms)[0] == 1
    before = report(tmp_path)
    # 1. an uncommitted change in the working tree of the repo being exported
    (repo / "tools" / "personal_data_patterns.py").write_text(
        "VALUE_PATTERNS = {}\nSAFE_SANDBOX_EMAIL_DOMAINS = frozenset()\n", encoding="utf-8")
    # 2. a different module of that name first on sys.path, and one already imported
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "personal_data_patterns.py").write_text(
        "VALUE_PATTERNS = {}\nSAFE_SANDBOX_EMAIL_DOMAINS = frozenset()\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(decoy))
    import types as _types
    marker = _types.ModuleType("personal_data_patterns")
    marker.VALUE_PATTERNS, marker.SAFE_SANDBOX_EMAIL_DOMAINS = {}, frozenset()
    monkeypatch.setitem(sys.modules, "personal_data_patterns", marker)
    path_before = list(sys.path)
    assert run(repo, tmp_path / "after", sha, terms)[0] == 1
    after = report(tmp_path)
    assert after == before.replace("before", "after") or after == before        # identical findings
    assert "copilot/c.txt:1 account_number" in after
    assert sys.modules["personal_data_patterns"] is marker and sys.path == path_before   # nothing leaked in or out


def test_the_export_does_not_leave_the_commits_modules_in_this_process(tmp_path):
    repo, sha = make_repo(tmp_path)
    had = "personal_data_patterns" in sys.modules
    assert run(repo, tmp_path / "out", sha, terms_file(tmp_path))[0] == 0
    assert ("personal_data_patterns" in sys.modules) is had


# ============================================================================================== T9 private terms
def test_t9_a_private_term_in_an_included_file_blocks_and_never_appears_anywhere(tmp_path):
    repo, sha = make_repo(tmp_path, {"copilot/notes.py": "# written for " + PRIVATE_TERM.upper() + " originally\n"})
    dest = tmp_path / "out"
    assert run(repo, dest, sha, terms_file(tmp_path))[0] == 1
    text = report(tmp_path)
    assert "BLOCK copilot/notes.py:1 private_term" in text                            # case-insensitive match
    assert PRIVATE_TERM not in text and PRIVATE_TERM.upper() not in text
    assert PRIVATE_TERM not in Path(ep.__file__).read_text(encoding="utf-8")
    assert PRIVATE_TERM not in Path(__file__).read_text(encoding="utf-8")


def test_t9_a_private_term_in_a_published_path_blocks_too(tmp_path):
    repo, sha = make_repo(tmp_path, {"copilot/" + PRIVATE_TERM + ".py": "x = 1\n"})
    assert run(repo, tmp_path / "out", sha, terms_file(tmp_path))[0] == 1
    assert "private_term" in report(tmp_path)


def test_t9_a_term_that_only_appears_in_an_excluded_file_does_not_block_and_does_not_reach_the_output(tmp_path):
    repo, sha = make_repo(tmp_path, {"docs/ENCARGO-X.md": "about " + PRIVATE_TERM + "\n"})
    dest = tmp_path / "out"
    assert run(repo, dest, sha, terms_file(tmp_path))[0] == 0
    assert all(PRIVATE_TERM.encode() not in data for data in tree(dest).values())     # manifest and index included


def test_t9_the_export_fails_closed_without_a_usable_terms_file(tmp_path, monkeypatch, capsys):
    repo, sha = make_repo(tmp_path)
    monkeypatch.delenv(ep.PRIVATE_TERMS_ENV, raising=False)
    for index, args in enumerate([[], ["--private-terms", str(tmp_path / "missing.txt")],
                                  ["--private-terms", str(terms_file(tmp_path, terms=(), name="empty.txt"))]]):
        dest = tmp_path / f"out{index}"
        assert ep.main([str(dest), "--commit", sha, "--repo", str(repo), *args]) == 2
        assert not dest.exists()
    assert "private-terms" in capsys.readouterr().err


def test_t9_the_terms_file_can_come_from_the_environment(tmp_path, monkeypatch):
    repo, sha = make_repo(tmp_path)
    monkeypatch.setenv(ep.PRIVATE_TERMS_ENV, str(terms_file(tmp_path)))
    dest = tmp_path / "out"
    assert ep.main([str(dest), "--commit", sha, "--repo", str(repo)]) == 0 and (dest / "MANIFEST.json").exists()


# ================================================================================ T13 evidence index
EVIDENCE = {"evidence/a.jsonl": '{"op": "read", "n": 1}\n{"op": "read", "n": 2}\n{"op": "read", "n": 3}\n',
            "evidence/b.json": '{"private": "content of the second evidence file, which must stay private"}\n',
            "evidence/uncited.jsonl": '{"op": "nothing cites this file"}\n'}


def _index_repo(tmp_path, claims, files=None):
    repo, sha = make_repo(tmp_path, {**EVIDENCE, "docs/evidence-claims.json": claims_doc(*claims), **(files or {})})
    return repo, sha


def test_t13_every_hash_count_and_cited_range_in_the_index_matches_the_source_blobs(tmp_path):
    repo, sha = _index_repo(tmp_path, [claim("EV-01", "evidence/a.jsonl", (1, 2)),
                                       claim("EV-02", "evidence/a.jsonl", (3, 3), proves="Third run, wording of ours.")])
    dest = tmp_path / "out"
    assert run(repo, dest, sha, terms_file(tmp_path))[0] == 0
    text = (dest / ep.INDEX_NAME).read_text(encoding="utf-8")
    assert f"Source commit: `{sha}`" in text and "never over a working-tree copy" in text
    for name in EVIDENCE:                                                              # files table: every file, cited or not
        blob = blob_bytes(repo, sha, name)
        newlines = blob.count(b"\n")
        label = Path(name).name + " (private file, hashed)"
        row = f"| {label} | {hashlib.sha256(blob).hexdigest()} | {newlines} | {len(blob)} |"
        assert row in text, name
        assert name not in text
    lines = blob_bytes(repo, sha, "evidence/a.jsonl").split(b"\n")
    assert hashlib.sha256(b"\n".join(lines[0:2])).hexdigest() in text                  # cited_lines_sha256: LF, no trailing LF
    assert hashlib.sha256(lines[2]).hexdigest() in text
    assert "| EV-01 | a.jsonl (private file, hashed) | 1-2 |" in text
    assert "| EV-02 | a.jsonl (private file, hashed) | 3 |" in text
    assert "uncited.jsonl (private file, hashed)" in text                             # hash and count, no path
    entry = next(f for f in manifest(dest)["files"] if f["path"] == ep.INDEX_NAME)
    assert entry["action"] == "generated" and entry["sha256"] == hashlib.sha256(text.encode()).hexdigest()


def test_t13_no_byte_of_any_evidence_line_reaches_the_index(tmp_path):
    repo, sha = _index_repo(tmp_path, [claim("EV-01", "evidence/b.json", (1, 1))])
    dest = tmp_path / "out"
    assert run(repo, dest, sha, terms_file(tmp_path))[0] == 0
    text = (dest / ep.INDEX_NAME).read_text(encoding="utf-8")
    for data in EVIDENCE.values():
        for line in data.splitlines():
            assert line not in text
    assert "which must stay private" not in text


def test_a3_generated_index_labels_hide_private_paths_and_preserve_hashes(tmp_path):
    private_path = "internal/reviews/synthetic-proof.txt"
    data = b"Synthetic observation one.\nSynthetic observation two.\n"
    repo, sha = make_repo(tmp_path, {private_path: data,
        "docs/evidence-claims.json": claims_doc(claim("EV-01", private_path, (1, 1)))})
    outputs, *_ = ep.build_outputs(repo, sha, None)
    text = outputs[ep.INDEX_NAME].decode("utf-8")
    assert private_path not in text and "internal/reviews/" not in text
    assert text.count("synthetic-proof.txt (private file, hashed)") == 2
    assert hashlib.sha256(data).hexdigest() in text
    assert hashlib.sha256(data.split(b"\n")[0]).hexdigest() in text
    assert "| 2 | " + str(len(data)) + " |" in text
    assert "Synthetic observation one." not in text


def test_a3_cd_env_template_keeps_published_draft_and_facts_only(tmp_path, monkeypatch):
    monkeypatch.setattr(ep, "PUBLISHED_NAMESPACES", ["account_cliff", "resolution", "draft", "delivery_facts"])
    template = (b"COPILOT_RULES_COUNTRY=MX\nCOPILOT_FEATURE_DRAFT=true\n"
                b"COPILOT_FEATURE_DELIVERY_FACTS=true\nCOPILOT_FEATURE_WEBHOOKS=false\n"
                b"COPILOT_FEATURE_WEBHOOKS_VERIFY=false\nCOPILOT_FEATURE_CONFORMANCE=false\n")
    files = {".env.example": template}
    for name in ("account_cliff", "resolution", "draft", "delivery_facts", "webhooks", "conformance"):
        files[f"copilot/features/{name}/__init__.py"] = b""
    repo, sha = make_repo(tmp_path, files)
    outputs, entries, *_ = ep.build_outputs(repo, sha, None)
    expected = (b"COPILOT_RULES_COUNTRY=MX\nCOPILOT_FEATURE_DRAFT=true\n"
                b"COPILOT_FEATURE_DELIVERY_FACTS=true\n")
    assert outputs[".env.example"] == expected
    record = next(item for item in entries if item["path"] == ".env.example")
    assert record["source_sha256"] == hashlib.sha256(template).hexdigest()
    assert record["sha256"] == hashlib.sha256(expected).hexdigest()


def test_t13_a_claim_whose_wording_copies_an_evidence_line_fails_the_export(tmp_path, capsys):
    copied = '{"private": "content of the second evidence file, which must stay private"}'
    repo, sha = _index_repo(tmp_path, [claim("EV-01", "evidence/b.json", (1, 1), proves="It shows " + copied)])
    dest = tmp_path / "out"
    code, out = run(repo, dest, sha, terms_file(tmp_path), capsys)
    assert code == 2 and not dest.exists() and "would copy line 1 of evidence/b.json" in out.err
    assert "must stay private" not in out.err + out.out


@pytest.mark.parametrize("bad,expect", [
    (claim("EV-01", "evidence/missing.jsonl", (1, 1)), "not an evidence file"),
    (claim("EV-01", "evidence/a.jsonl", (2, 9)), "has 3 lines"),
    (claim("EV-01", "evidence/a.jsonl", (0, 1)), "has 3 lines"),
    (claim("EV-01", "evidence/a.jsonl", (3, 2)), "has 3 lines"),
    (claim("EV-01", "../outside.jsonl", (1, 1)), "repo-relative path"),
    (claim("EV-01", "evidence/a.jsonl", (1,)), "lines must be"),
    (claim("EV 01", "evidence/a.jsonl", (1, 1)), "id is missing"),
    (claim("EV-01", "evidence/a.jsonl", (1, 1), proves="  "), "proves is missing"),
    (claim("EV-01", "evidence/a.jsonl", (1, 1), used_by="README.md"), "used_by")])
def test_t13_a_claim_that_does_not_match_its_file_fails_the_export_and_writes_nothing(tmp_path, capsys, bad, expect):
    repo, sha = _index_repo(tmp_path, [bad])
    dest = tmp_path / "out"
    code, out = run(repo, dest, sha, terms_file(tmp_path), capsys)
    assert code == 2 and not dest.exists() and expect in out.err


def test_t13_a_repeated_id_a_wrong_schema_or_invalid_json_fails_the_export(tmp_path, capsys):
    cases = [claims_doc(claim("EV-01"), claim("EV-01")),
             json.dumps({"schema": "other/1", "claims": []}), "{not json"]
    for index, doc in enumerate(cases):
        repo, sha = make_repo(tmp_path, {**EVIDENCE, "docs/evidence-claims.json": doc}, name=f"r{index}")
        dest = tmp_path / f"out{index}"
        assert run(repo, dest, sha, terms_file(tmp_path))[0] == 2 and not dest.exists()


def test_t13_a_commit_without_a_claims_file_fails_and_writes_nothing(tmp_path, capsys):
    repo, sha = make_repo(tmp_path, EVIDENCE)
    _git(repo, "rm", "-q", ep.CLAIMS_PATH)
    _git(repo, "commit", "-q", "-m", "no claims file")
    dest = tmp_path / "out"
    code, out = run(repo, dest, _git(repo, "rev-parse", "HEAD"), terms_file(tmp_path), capsys)
    assert code == 2 and not dest.exists() and "is required" in out.err


def test_t13_an_empty_claims_list_is_valid_and_still_lists_the_files_with_a_warning(tmp_path):
    repo, sha = make_repo(tmp_path, EVIDENCE)
    dest = tmp_path / "out"
    assert run(repo, dest, sha, terms_file(tmp_path))[0] == 0
    text = (dest / ep.INDEX_NAME).read_text(encoding="utf-8")
    assert "| (no claims) |" in text and "a.jsonl (private file, hashed)" in text
    assert "evidence/a.jsonl" not in text
    assert "has no claims" in report(tmp_path)


def test_t13_a_commit_with_no_evidence_exports_cleanly_with_a_warning(tmp_path):
    repo, sha = make_repo(tmp_path)
    dest = tmp_path / "out"
    assert run(repo, dest, sha, terms_file(tmp_path))[0] == 0
    assert "(no evidence files)" in (dest / ep.INDEX_NAME).read_text(encoding="utf-8")
    assert "no evidence files were found" in report(tmp_path)


def test_t13_hashes_are_over_the_git_blob_bytes_not_a_crlf_working_copy(tmp_path):
    repo, sha = _index_repo(tmp_path, [claim("EV-01", "evidence/a.jsonl", (1, 2))])
    blob = blob_bytes(repo, sha, "evidence/a.jsonl")
    (repo / "evidence" / "a.jsonl").write_bytes(blob.replace(b"\n", b"\r\n"))          # a Windows-style working copy
    dest = tmp_path / "out"
    assert run(repo, dest, sha, terms_file(tmp_path))[0] == 0
    text = (dest / ep.INDEX_NAME).read_text(encoding="utf-8")
    assert hashlib.sha256(blob).hexdigest() in text
    assert hashlib.sha256(blob.replace(b"\n", b"\r\n")).hexdigest() not in text


def test_t13_an_external_evidence_directory_is_hashed_as_stored_and_recorded_only_as_external(tmp_path):
    repo, sha = make_repo(tmp_path, {"docs/evidence-claims.json": claims_doc(claim("EV-01", "evidence/x.jsonl", (1, 1)))})
    outside = tmp_path / "private-evidence-location"
    outside.mkdir()
    stored = b'{"op": "kept outside the repository"}\r\n{"op": "second"}\r\n'
    (outside / "x.jsonl").write_bytes(stored)
    (outside / "ignored.txt").write_bytes(b"not an evidence file")
    dest = tmp_path / "out"
    assert run(repo, dest, sha, terms_file(tmp_path), extra=["--evidence-dir", str(outside)])[0] == 0
    text = (dest / ep.INDEX_NAME).read_text(encoding="utf-8")
    assert hashlib.sha256(stored).hexdigest() in text and "hashed exactly as stored" in text
    assert "ignored.txt" not in text and "private-evidence-location" not in text and str(tmp_path) not in text
    data = manifest(dest)
    assert data["evidence_dir"] == "external"
    assert "private-evidence-location" not in json.dumps(data)


def test_t13_a_missing_external_evidence_directory_fails_closed(tmp_path, capsys):
    repo, sha = make_repo(tmp_path)
    dest = tmp_path / "out"
    code, out = run(repo, dest, sha, terms_file(tmp_path), capsys, extra=["--evidence-dir", str(tmp_path / "nope")])
    assert code == 2 and not dest.exists() and "evidence directory" in out.err


# ------------------------------------------------------------------ evidence outside the commit
def _repo_without_evidence(tmp_path, name="repo"):
    """The state when the claims file names evidence files, but the commit holds none of them."""
    return make_repo(tmp_path, {"docs/evidence-claims.json": claims_doc(claim("EV-01", "evidence/a.jsonl", (1, 2)),
                                                                          claim("EV-02", "evidence/b.jsonl", None))},
                     name=name)


def test_evd2_a_plain_export_fails_closed_naming_evidence_dir_when_the_commit_has_no_evidence(tmp_path, capsys):
    repo, sha = _repo_without_evidence(tmp_path)
    dest = tmp_path / "out"
    code, out = run(repo, dest, sha, terms_file(tmp_path), capsys)
    assert code == 2 and not dest.exists()                         # nothing written, and exit 2 (cannot be built)
    assert "--evidence-dir" in out.err and "EV-01" in out.err and "evidence/a.jsonl" in out.err


def test_evd2_the_same_commit_exports_with_evidence_dir(tmp_path):
    repo, sha = _repo_without_evidence(tmp_path)
    frozen = tmp_path / "frozen"
    frozen.mkdir()
    (frozen / "a.jsonl").write_bytes(b'{"n": 1}' + bytes([10]) + b'{"n": 2}' + bytes([10]))
    (frozen / "b.jsonl").write_bytes(b'{"n": 3}' + bytes([10]))
    dest = tmp_path / "out"
    assert run(repo, dest, sha, terms_file(tmp_path), extra=["--evidence-dir", str(frozen)])[0] == 0
    text = (dest / ep.INDEX_NAME).read_text("utf-8")
    assert manifest(dest)["evidence_dir"] == "external" and "a.jsonl (private file, hashed)" in text
    assert "evidence/a.jsonl" not in text


def test_evd2_the_index_from_an_external_folder_equals_the_index_from_a_commit_that_holds_the_files(tmp_path):
    claims = [claim("EV-01", "evidence/a.jsonl", (1, 2)), claim("EV-02", "evidence/a.jsonl", (3, 3), proves="Third run, our wording.")]
    with_files, sha_a = make_repo(tmp_path, {**EVIDENCE, "docs/evidence-claims.json": claims_doc(*claims)}, name="with")
    without, sha_b = make_repo(tmp_path, {"docs/evidence-claims.json": claims_doc(*claims)}, name="without")
    frozen = tmp_path / "frozen"
    frozen.mkdir()
    for name in EVIDENCE:
        (frozen / name.split("/", 1)[1]).write_bytes(blob_bytes(with_files, sha_a, name))   # byte-identical snapshot
    out_a, out_b = tmp_path / "out_a", tmp_path / "out_b"
    assert run(with_files, out_a, sha_a, terms_file(tmp_path))[0] == 0
    assert run(without, out_b, sha_b, terms_file(tmp_path), extra=["--evidence-dir", str(frozen)])[0] == 0
    a = (out_a / ep.INDEX_NAME).read_text(encoding="utf-8").splitlines()
    b = (out_b / ep.INDEX_NAME).read_text(encoding="utf-8").splitlines()
    tables_a, tables_b = [x for x in a if x.startswith("| ")], [x for x in b if x.startswith("| ")]
    assert tables_a == tables_b and len(tables_a) > 4               # every hash, count and cited range is identical
    differing = [x for x in a + b if x not in a or x not in b]
    assert all(sha_a in x or sha_b in x or "external" in x.lower() or "as stored" in x or "git blob" in x
               for x in differing), differing                     # only the source label may differ


def test_t13_the_index_text_itself_goes_through_the_final_scan(tmp_path):
    repo, sha = _index_repo(tmp_path, [claim("EV-01", "evidence/a.jsonl", (1, 1), proves="Reached " + EMAIL + " somehow.")])
    assert run(repo, tmp_path / "out", sha, terms_file(tmp_path))[0] == 1
    assert "BLOCK EVIDENCE-INDEX.md" in report(tmp_path) and EMAIL not in report(tmp_path)


# =============================================================================== T14 private commit ids (warning)
def test_t14_a_private_commit_id_in_a_public_md_file_is_a_warning_not_a_block(tmp_path):
    repo, old = make_repo(tmp_path)
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "AI-USAGE.md").write_text("Intro.\nSee commit " + old[:9] + " for details.\n", encoding="utf-8")
    (repo / "docs" / "MCP.md").write_text("Full id " + old + " here.\n", encoding="utf-8")
    (repo / "README.md").write_text("A hash that is no commit: " + "deadbeefcafe" + ".\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "docs citing an earlier commit")
    sha = _git(repo, "rev-parse", "HEAD")
    dest = tmp_path / "out"
    assert run(repo, dest, sha, terms_file(tmp_path))[0] == 0                          # warns, does not block
    text = report(tmp_path)
    assert "warning: docs/AI-USAGE.md:2 looks like a private commit id (T14)" in text
    assert "warning: docs/MCP.md:1 looks like a private commit id (T14)" in text
    assert "README.md:1" not in text


def test_t14_the_index_and_the_manifest_are_not_checked_and_a_non_commit_hash_is_ignored(tmp_path):
    repo, sha = make_repo(tmp_path, {"docs/AI-USAGE.md": "sha256 " + hashlib.sha256(b"x").hexdigest()[:40] + "\n"})
    dest = tmp_path / "out"
    assert run(repo, dest, sha, terms_file(tmp_path))[0] == 0
    assert "(T14)" not in report(tmp_path)
    assert sha in (dest / ep.INDEX_NAME).read_text(encoding="utf-8")                  # the index names the source commit


# ============================================================================================== more spec behaviour
def test_the_table_2_decision_and_the_e13_pending_files(tmp_path):
    repo, sha = make_repo(tmp_path, {
        "docs/DESIGN.md": "d\n", "docs/brand/mark.svg": "<svg/>\n", "docs/brand/preview.html": "<p/>\n",
        "docs/ARCHITECTURE.md": "a\n", "docs/DECISIONS.md": "d\n", "internal/reports/INFORME-C1-CIERRE.md": "i\n",
        "pilot/eval/EXPOSURE.md": "e\n", "pilot/eval/cases/c.json": "{}\n", "tools/video/make_captions.py": "v\n",
        "tools/check_video.ps1": "c\n", "tools/estimate_narration.py": "n\n", "tools/test_estimate_narration.py": "t\n",
        "tools/hosted_smoke.py": "h\n", "tools/check_pending.py": "p\n", "tools/test_check_pending.py": "t\n",
        "pilot/step1_auth.py": "s\n", "pilot/step2_order.py": "o\n", "render.yaml": "r\n", "render.free.yaml": "f\n",
        "tools/test_other.py": "t\n"})
    dest = tmp_path / "out"
    assert run(repo, dest, sha, terms_file(tmp_path))[0] == 0
    present = set(tree(dest))
    assert {"docs/DESIGN.md", "docs/brand/mark.svg", "pilot/eval/cases/c.json", "tools/test_other.py"} <= present
    # The translated files are published. The curated docs/DECISIONS.md is public and
    # the closing report moved under internal/, so nothing is pending any more. The video tools, the hosted smoke test,
    # the pending-placeholder gate, the first pilot step, the brand preview page and the paid-plan Blueprint stay
    # private; DESIGN.md, step 2 and the diskless Blueprint are published
    assert {"docs/ARCHITECTURE.md", "docs/DECISIONS.md", "pilot/eval/EXPOSURE.md", "pilot/step2_order.py",
            "render.free.yaml"} <= present
    assert not present & {"docs/brand/preview.html", "tools/video/make_captions.py", "tools/check_video.ps1",
                          "tools/estimate_narration.py", "tools/test_estimate_narration.py", "tools/hosted_smoke.py",
                          "tools/check_pending.py", "tools/test_check_pending.py", "pilot/step1_auth.py", "render.yaml"}
    assert "internal/reports/INFORME-C1-CIERRE.md" not in present
    rules = {e["path"]: e["rule"] for e in excluded_entries(dest)}
    assert rules["internal/reports/INFORME-C1-CIERRE.md"] == "exclude:internal/**"
    assert not hasattr(ep, "E13_PENDING") and not hasattr(ep, "TABLE2_DECIDES")      # retired by the folder rules


def test_malformed_pdf_png_are_copied_byteexact_but_block_the_manifest(tmp_path):
    blob = bytes(range(256)) * 4
    repo, sha = make_repo(tmp_path, {"demo/documents/x.pdf": blob, "demo/documents/y.png": blob})
    dest = tmp_path / "out"
    assert run(repo, dest, sha, terms_file(tmp_path))[0] == 1
    assert (dest / "demo/documents/x.pdf").read_bytes() == blob
    assert "binary_files_not_scanned=0" in report(tmp_path)
    assert not (dest / "MANIFEST.json").exists()


def test_a_text_file_that_is_not_utf8_blocks_instead_of_being_skipped(tmp_path):
    repo, sha = make_repo(tmp_path, {"copilot/latin.txt": b"caf\xe9 \xff\xfe not utf-8\n"})
    assert run(repo, tmp_path / "out", sha, terms_file(tmp_path))[0] == 1
    assert "BLOCK copilot/latin.txt:0 unscannable" in report(tmp_path)


def test_symlinks_and_submodule_entries_are_excluded_and_never_followed(tmp_path):
    repo, sha = make_repo(tmp_path, {"copilot/real.py": "x = 1" + LF})
    target = tmp_path / "target.txt"
    target.write_text("../../secret-outside", encoding="utf-8")
    oid = _git(repo, "hash-object", "-w", str(target))
    _git(repo, "update-index", "--add", "--cacheinfo", f"120000,{oid},copilot/link.py")
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{sha},copilot/sub")
    _git(repo, "commit", "-q", "-m", "symlink and submodule entries")
    new_sha = _git(repo, "rev-parse", "HEAD")
    dest = tmp_path / "out"
    assert run(repo, dest, new_sha, terms_file(tmp_path))[0] == 0
    assert not (dest / "copilot" / "link.py").exists() and not (dest / "copilot" / "sub").exists()
    rules = {e["path"]: e["rule"] for e in excluded_entries(dest)}
    assert rules["copilot/link.py"] == "symlink" and rules["copilot/sub"] == "submodule"
    assert (dest / "copilot" / "real.py").exists()


def test_the_repository_argument_must_be_a_git_repository(tmp_path):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    assert ep.main([str(tmp_path / "out"), "--commit", "HEAD", "--private-terms", str(terms_file(tmp_path)),
                    "--repo", str(not_a_repo)]) == 2
    assert not (tmp_path / "out").exists()


def test_a_commit_without_the_sanitiser_or_the_scanner_cannot_be_exported(tmp_path):
    repo = tmp_path / "bare"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "no tools")
    assert ep.main([str(tmp_path / "out"), "--commit", "HEAD", "--private-terms", str(terms_file(tmp_path)),
                    "--repo", str(repo)]) == 2
    assert not (tmp_path / "out").exists()


def test_rules_version_changes_when_a_rule_changes(monkeypatch):
    before = ep.rules_version()
    monkeypatch.setattr(ep, "PUBLIC_DOCS", ["docs/brand/**"])
    assert ep.rules_version() != before
    monkeypatch.undo()
    monkeypatch.setattr(ep, "SANITIZED", ["evidence/*.json"])
    assert ep.rules_version() != before


def test_rules_version_does_not_depend_on_the_hash_seed():
    """MANIFEST.json is documented as deterministic: the rule set hash must not change with the per-process string hash
    seed (the allowed list of effective_exclude() was once built by iterating a set)."""
    code = "import sys; sys.path.insert(0, sys.argv[1]); import export_public as ep; print(ep.rules_version())"
    values = {}
    for seed in ("1", "2", "3", "4"):
        done = subprocess.run([sys.executable, "-c", code, str(Path(__file__).resolve().parent)], check=True,
                              capture_output=True, text=True, env={**os.environ, "PYTHONHASHSEED": seed})
        values[seed] = done.stdout.strip()
    assert len(set(values.values())) == 1, values


def test_counts_only_prints_the_counts_from_the_tree_without_building_or_needing_the_terms_file(tmp_path, capsys,
                                                                                                monkeypatch):
    monkeypatch.delenv(ep.PRIVATE_TERMS_ENV, raising=False)
    repo, sha = make_repo(tmp_path, {"evidence/a.jsonl": "{}\n", "docs/NEW-INTERNAL.md": "x\n", "docs/AI-USAGE.md": "u\n"})
    before = sorted(p.name for p in tmp_path.iterdir())
    assert ep.main(["--counts-only", "--commit", sha, "--repo", str(repo)]) == 0
    tally = dict(kv.split("=") for kv in capsys.readouterr().out.split())
    assert tally["unmatched"] == "0" and tally["sanitized"] == "0"        # docs/NEW-INTERNAL.md is docs:not-public
    assert int(tally["published"]) >= 5 and int(tally["excluded"]) >= 2
    assert sorted(p.name for p in tmp_path.iterdir()) == before                      # nothing was written
    assert ep.main(["--commit", sha, "--repo", str(repo)]) == 2                      # a destination is needed to build


def test_the_tool_source_holds_no_local_path_literal_or_private_term():
    source = Path(ep.__file__).read_text(encoding="utf-8")
    assert "/Us" + "ers/" not in source and "/ho" + "me/" not in source and ":\\" + "Us" + "ers" not in source
    assert "C:\\" not in source


# ================================================= what an exported tool needs must be exported with it
def _flag_guarded_imports(tree_):
    """Import/ImportFrom nodes nested at any depth inside an `if` whose test calls `feature_enabled`."""
    import ast
    guarded = set()
    for node in ast.walk(tree_):
        if isinstance(node, ast.If) and any(
                isinstance(c, ast.Call) and (getattr(c.func, "id", None) == "feature_enabled"
                                             or getattr(c.func, "attr", None) == "feature_enabled")
                for c in ast.walk(node.test)):
            guarded.update(n for n in ast.walk(node) if isinstance(n, (ast.Import, ast.ImportFrom)))
    return guarded


def dependency_gaps(paths, read):
    """[(file, needed path)] for every INCLUDEd .py whose repo-local import resolves to an excluded tracked file, or
    that names (as a string constant) a tracked data file that is excluded. `read(path)` returns the bytes."""
    import ast
    tracked = set(paths)
    published = {p for p in paths if ep.classify(p)[0] != "excluded"}
    by_name = {}
    for p in tracked:
        by_name.setdefault(p.rsplit("/", 1)[-1], []).append(p)
    gaps = []
    for path in sorted(published):
        if not path.endswith(".py"):
            continue
        try:
            tree_ = ast.parse(read(path).decode("utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        here = path.rsplit("/", 1)[0] if "/" in path else ""
        guarded = _flag_guarded_imports(tree_)
        for node in ast.walk(tree_):
            names = []
            if node in guarded:                                  # transitional namespace behind a feature flag
                continue
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for name in names:
                rel = name.replace(".", "/")
                for base in (here, "", "tools", "pilot"):
                    prefix = base + "/" if base else ""
                    for candidate in (f"{prefix}{rel}.py", f"{prefix}{rel}/__init__.py"):
                        if candidate in tracked and candidate not in published:
                            gaps.append((path, candidate))
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value
                if re.fullmatch(r"[\w.-]+\.(sha256|json|txt|csv|yaml|yml)", value) and value in by_name:
                    homes = by_name[value]
                    if homes and not any(h in published for h in homes) and all(
                            h.startswith(("tools/", "pilot/", "copilot/")) for h in homes):
                        gaps.extend((path, h) for h in homes)
    return sorted(set(gaps))


def test_a2_an_included_tool_that_imports_or_reads_an_excluded_file_is_reported():
    paths = ["copilot/a.py", "copilot/helper.py", "copilot/data.sha256", "copilot/b.py"]
    sources = {"copilot/a.py": b"import helper" + b"\n", "copilot/helper.py": b"x = 1" + b"\n",
               "copilot/b.py": b"N = 'data.sha256'" + b"\n", "copilot/data.sha256": b""}
    saved = ep.EXCLUDE[:]
    try:
        ep.EXCLUDE[:] = saved + [("copilot/helper.py", []), ("copilot/data.sha256", [])]
        gaps = dependency_gaps(paths, sources.__getitem__)
    finally:
        ep.EXCLUDE[:] = saved
    assert ("copilot/a.py", "copilot/helper.py") in gaps and ("copilot/b.py", "copilot/data.sha256") in gaps
    assert dependency_gaps(paths, sources.__getitem__) == []                     # nothing excluded: no gap


def test_a2_a_flag_guarded_import_of_an_excluded_module_is_not_a_gap():
    paths = ["copilot/a.py", "copilot/helper.py"]
    guarded = b'if config.feature_enabled("x"):\n    import helper\n'
    nested = b'if feature_enabled("x"):\n    for _ in [1]:\n        from helper import y\n'
    bare = b"import helper\n"
    not_flag = b"if True:\n    import helper\n"
    saved = ep.EXCLUDE[:]
    try:
        ep.EXCLUDE[:] = saved + [("copilot/helper.py", [])]
        def gaps_for(src):
            return dependency_gaps(paths, {"copilot/a.py": src, "copilot/helper.py": b"x = 1\n"}.__getitem__)
        assert gaps_for(guarded) == [] and gaps_for(nested) == []
        assert gaps_for(bare) == [("copilot/a.py", "copilot/helper.py")]
        assert gaps_for(not_flag) == [("copilot/a.py", "copilot/helper.py")]           # `if True:` does not exempt
    finally:
        ep.EXCLUDE[:] = saved


def test_a2_everything_an_included_python_file_imports_or_reads_is_itself_included_on_the_real_repo():
    if not in_git_checkout(ROOT):
        pytest.skip(NEEDS_CHECKOUT)
    listing = subprocess.run(["git", "-C", str(ROOT), "ls-tree", "-r", "--name-only", "HEAD"], check=True,
                             capture_output=True, text=True).stdout.splitlines()
    changed = set(subprocess.run(["git", "-C", str(ROOT), "diff", "--name-only", "HEAD"], check=True,
                                 capture_output=True, text=True).stdout.splitlines())
    def candidate_or_committed(path):
        return (ROOT / path).read_bytes() if path in changed else _committed(path)
    gaps = dependency_gaps(listing, candidate_or_committed)
    assert gaps == [], gaps


def test_cd_copy_cases_follow_namespace_policy_without_a_dependency_waiver():
    """The same import must fail in the unconditional bucket, not be ignored."""
    old = 'tests/local/test_cd_copy.py'
    owned = 'tests/features/delivery_facts/test_cd_copy.py'
    package = 'copilot/features/resolution/__init__.py'
    source = b'from copilot.features.resolution import actions, order\n'
    sources = {old: source, owned: source, package: b''}
    assert ep.PUBLISHED_NAMESPACES == []
    assert ep.classify(old)[0] == 'copied'
    assert ep.classify(owned)[0] == 'excluded'
    assert dependency_gaps([old, package], sources.__getitem__) == [(old, package)]
    assert dependency_gaps([owned, package], sources.__getitem__) == []
    ep.PUBLISHED_NAMESPACES[:] = ['account_cliff', 'active_approval', 'carrier_required', 'claims', 'clauses',
                                'deadlines', 'delivery_facts', 'draft', 'events', 'fulfilment',
                                'ledger', 'outcome', 'package_print', 'resolution', 'triage']
    assert ep.classify(owned)[0] == ep.classify(package)[0] == 'copied'
    assert dependency_gaps([owned, package], sources.__getitem__) == []


# ======================================================================== decisions after the first dry export
def _lines(n, at, text):
    """n lines where line `at` (1-based) is `text`."""
    return LF.join(text if i == at else "filler line" for i in range(1, n + 1)) + LF


def test_611_an_allowed_exception_matches_only_on_path_kind_and_line(tmp_path):
    path = "pilot/fixtures/order_confirmation.txt"
    repo, sha = make_repo(tmp_path, {path: _lines(20, 15, "Address: " + STREET)})
    dest = tmp_path / "ok"
    assert run(repo, dest, sha, terms_file(tmp_path))[0] == 0
    entry = next(e for e in manifest(dest)["scan"]["exceptions"] if e["path"] == path)
    assert entry["kind"] == "street_address" and entry["line"] == 15 and entry["hits"] == 1 and entry["reason"]
    assert f"allowed {path}:15 street_address" in report(tmp_path)
    repo2, sha2 = make_repo(tmp_path, {path: _lines(20, 14, "Address: " + STREET)}, name="repo2")     # another line
    assert run(repo2, tmp_path / "wrong-line", sha2, terms_file(tmp_path))[0] == 1
    assert f"BLOCK {path}:14 street_address" in report(tmp_path)
    repo3, sha3 = make_repo(tmp_path, {"pilot/fixtures/other.txt": _lines(20, 15, "Address: " + STREET)}, name="repo3")
    assert run(repo3, tmp_path / "wrong-path", sha3, terms_file(tmp_path))[0] == 1


def test_611_an_exception_that_matched_nothing_is_a_stale_warning_not_a_failure(tmp_path):
    repo, sha = make_repo(tmp_path)
    assert run(repo, tmp_path / "out", sha, terms_file(tmp_path))[0] == 0
    text = report(tmp_path)
    assert "warning: stale exception: tools/sandbox_orders.py:34 street_address matched nothing" in text
    # the detector's own test is excepted by a path rule, so a moved line can not block the export again
    assert not any(e["path"] == "tools/test_redact.py" for e in ep.ALLOWED_EXCEPTIONS)
    assert any(path == "tools/test_redact.py" and kind == "street_address" for path, kind, _why in ep.SCAN_EXCEPTIONS)
    assert len(ep.ALLOWED_EXCEPTIONS) == len({(e["path"], e["kind"], e["line"]) for e in ep.ALLOWED_EXCEPTIONS})
    assert all(set(e) == {"path", "kind", "line", "reason"} and isinstance(e["line"], int) for e in ep.ALLOWED_EXCEPTIONS)


def _mail(local, domain):
    return local + "@" + domain                                   # no literal address in this file


@pytest.mark.parametrize("address,blocks", [
    (_mail("a", "example.com"), False), (_mail("a", "example.org"), False), (_mail("a", "example.net"), False),
    (_mail("a", "personal.example.com"), False), (_mail("a", "deep.sub.example.org"), False),
    (_mail("a", "notexample.com"), True), (_mail("a", "example.com.evil.io"), True),
    (_mail("sb-" + "abc123", "example.com"), True), (_mail("SB-" + "Abc12345", "business.example.com"), True),
    (_mail("sb-" + "ab", "example.com"), False)])
def test_611_email_domains_and_the_generated_sandbox_login_rule(tmp_path, address, blocks):
    repo, sha = make_repo(tmp_path, {"copilot/d.txt": "x " + address + LF})
    code = run(repo, tmp_path / "out", sha, terms_file(tmp_path))[0]
    assert (code == 1) is blocks, address


def test_611_emails_are_not_allowed_in_the_example_configuration_beyond_the_allowed_domains(tmp_path):
    repo, sha = make_repo(tmp_path, {".env.example": "OWNER=you@example.com" + LF})
    assert run(repo, tmp_path / "ok", sha, terms_file(tmp_path))[0] == 0
    repo2, sha2 = make_repo(tmp_path, {".env.example": "OWNER=" + EMAIL + LF}, name="repo2")
    assert run(repo2, tmp_path / "blocked", sha2, terms_file(tmp_path))[0] == 1


@pytest.mark.parametrize("line,blocks", [
    ('"merchantId": "' + MERCHANT_VALUE + '"', True), ('payerId = "' + MERCHANT_VALUE + '"', True),
    ("accountId: " + MERCHANT_VALUE, True), ("?account_id=" + MERCHANT_VALUE, True),
    ('"account' + 'Number": "123456"', True), ("account_number=123", False),
    ('"email' + 'Address": "' + EMAIL + '"', True), ('"email' + '_address": "' + _mail("a", "example.com") + '"', False),
    ('"merchant' + '_id": "abcdefghjklmn"', True),                           # lower case: the commit's own broader rule
    ("bare token " + MERCHANT_VALUE, False)])                           # bare tokens are the private terms' job
def test_611_keyed_identifier_forms_and_the_shapes_that_are_not_detected(tmp_path, line, blocks):
    repo, sha = make_repo(tmp_path, {"copilot/k.txt": line + LF})
    code = run(repo, tmp_path / "out", sha, terms_file(tmp_path))[0]
    assert (code == 1) is blocks, line


def test_611_private_term_hits_are_reported_by_number_never_by_text(tmp_path):
    repo, sha = make_repo(tmp_path, {"copilot/n.py": "# " + PRIVATE_TERM + LF})
    terms = terms_file(tmp_path, terms=("another-term", PRIVATE_TERM))
    assert run(repo, tmp_path / "out", sha, terms)[0] == 1
    text = report(tmp_path)
    assert "BLOCK copilot/n.py:1 private_term #2" in text and PRIVATE_TERM not in text and "another-term" not in text


def test_611_a_private_term_shorter_than_three_characters_fails_closed(tmp_path):
    repo, sha = make_repo(tmp_path)
    dest = tmp_path / "out"
    assert run(repo, dest, sha, terms_file(tmp_path, terms=("ab", PRIVATE_TERM)))[0] == 2 and not dest.exists()


def test_611_the_report_sits_beside_the_export_named_after_it(tmp_path):
    repo, sha = make_repo(tmp_path)
    for name in ("first", "second"):
        assert run(repo, tmp_path / name, sha, terms_file(tmp_path))[0] == 0
    assert (tmp_path / ("first" + ep.REPORT_SUFFIX)).is_file() and (tmp_path / ("second" + ep.REPORT_SUFFIX)).is_file()
    assert not list((tmp_path / "first").rglob("*REDACT-SCAN*"))


def test_611_a_claim_may_cite_any_excluded_tracked_file_and_lines_null_means_the_whole_file(tmp_path):
    raw = '{"round": 1, "private": "kept-private-content-xyz"}' + LF
    repo, sha = make_repo(tmp_path, {
        "research/eval-results/r.json": raw,
        ep.CLAIMS_PATH: claims_doc(claim("EV-06", "research/eval-results/r.json", None, proves="Our own summary."))})
    dest = tmp_path / "out"
    assert run(repo, dest, sha, terms_file(tmp_path))[0] == 0
    text = (dest / ep.INDEX_NAME).read_text(encoding="utf-8")
    blob = blob_bytes(repo, sha, "research/eval-results/r.json")
    assert f"| r.json (private file, hashed) | {hashlib.sha256(blob).hexdigest()} |" in text
    assert "| EV-06 | r.json (private file, hashed) | all |" in text and "kept-private-content-xyz" not in text
    assert "research/eval-results/r.json" not in text
    assert not (dest / "research").exists()


def test_611_a_claim_may_not_cite_a_published_or_an_untracked_file(tmp_path, capsys):
    for index, cited in enumerate(("README.md", "research/missing.json")):
        repo, sha = make_repo(tmp_path, {ep.CLAIMS_PATH: claims_doc(claim("EV-01", cited, None))}, name=f"r{index}")
        code, out = run(repo, tmp_path / f"out{index}", sha, terms_file(tmp_path), capsys)
        assert code == 2 and not (tmp_path / f"out{index}").exists() and "cites" in out.err


def test_611_the_files_the_exported_tools_need_are_included():
    for path in ("tools/personal_data_patterns.py", "tools/evidence_binaries.sha256", "tools/redact.py",
                 "tools/scan_staged.py"):
        assert ep.classify(path)[0] == "copied", path


def test_611_demo_prefixed_synthetic_ids_pass_in_their_listed_paths_only(tmp_path):
    demo = '{"merchant' + '_id": "DEMOMERCH0001"}' + LF
    repo, sha = make_repo(tmp_path, {"pilot/fixtures/synthetic/d.json": demo})
    assert run(repo, tmp_path / "ok", sha, terms_file(tmp_path))[0] == 0
    repo2, sha2 = make_repo(tmp_path, {"copilot/other/d.json": demo}, name="repo2")
    assert run(repo2, tmp_path / "blocked", sha2, terms_file(tmp_path))[0] == 1
    lower = '{"merchant' + '_id": "demomerch0001"}' + LF
    repo3, sha3 = make_repo(tmp_path, {"pilot/fixtures/synthetic/d.json": lower}, name="repo3")
    assert run(repo3, tmp_path / "lower", sha3, terms_file(tmp_path))[0] == 1       # the DEMO prefix is upper case only


# ====================================================== internal/** rule, T15 (warning), T16 and T17 (blocking)
def test_internal_is_an_exclude_rule_that_wins_and_is_not_default_deny():
    for path in ("internal/README.md", "internal/briefs/ENCARGO-X.md", "internal/process/a/b/c.md"):
        assert ep.classify(path) == ("excluded", "exclude:internal/**"), path
    assert ep.classify("docs/NEW-INTERNAL.md")[1] == "docs:not-public"        # the docs gate; unmatched keeps meaning "nobody classified"


def test_t16_internal_is_never_copied_even_when_an_include_rule_matches_it(tmp_path, monkeypatch):
    monkeypatch.setattr(ep, "INCLUDE", ep.INCLUDE + ["internal/**"])
    repo, sha = make_repo(tmp_path, {"internal/x.md": "x\n", "internal/briefs/y.md": "y\n", "README.md": "ok\n"})
    dest = tmp_path / "out"
    assert run(repo, dest, sha, terms_file(tmp_path))[0] == 0
    present = set(tree(dest))
    assert not [p for p in present if p.startswith("internal/")]
    rules = {e["path"]: e["rule"] for e in excluded_entries(dest)}
    assert rules["internal/x.md"] == "exclude:internal/**" and rules["internal/briefs/y.md"] == "exclude:internal/**"
    assert ep.classify("internal/x.md")[0] == "excluded"


def test_t16_blocking_a_copy_of_an_internal_file_fails_the_export_and_writes_nothing(tmp_path, monkeypatch, capsys):
    """If the classification ever regressed, the guard after it still stops the export (exit 2, nothing written)."""
    repo, sha = make_repo(tmp_path, {"internal/x.md": "x\n"})
    real = ep.classify
    monkeypatch.setattr(ep, "classify", lambda p: ("copied", "include") if p.startswith("internal/") else real(p))
    dest = tmp_path / "out"
    code, out = run(repo, dest, sha, terms_file(tmp_path), capsys)
    assert code == 2 and not dest.exists() and "T16" in out.err and not list(tmp_path.glob("*" + ep.REPORT_SUFFIX))


def test_t17_the_public_docs_list_is_the_spec_list_with_table_2s_decision():
    # T17; DESIGN.md and brand/** are public (both are on the public-docs list)
    assert ep.PUBLIC_DOCS == ["docs/AI-USAGE.md", "docs/JUDGE-RUN.md", "docs/MCP.md", "docs/ARCHITECTURE.md",
                              "docs/DESIGN.md", "docs/brand/**", "docs/DECISIONS.md", "docs/CONFORMANCE.md", "docs/impact-1/**"]


def test_t17_only_the_listed_docs_are_published_and_any_other_docs_file_is_absent(tmp_path):
    files = {f"docs/{n}": "d\n" for n in ("AI-USAGE.md", "JUDGE-RUN.md", "MCP.md", "ARCHITECTURE.md", "DECISIONS.md", "DESIGN.md",
                                                    "CONFORMANCE.md")}
    files.update({"docs/brand/m.svg": "<svg/>\n", "docs/OTHER.md": "o\n", "docs/notes/x.md": "x\n",
                  "docs/COMPLIANCE.md": "c\n"})
    repo, sha = make_repo(tmp_path, files)
    dest = tmp_path / "out"
    assert run(repo, dest, sha, terms_file(tmp_path))[0] == 0
    published = {p for p in tree(dest) if p.startswith("docs/")}
    assert published == {"docs/AI-USAGE.md", "docs/JUDGE-RUN.md", "docs/MCP.md", "docs/ARCHITECTURE.md",
                         "docs/DECISIONS.md", "docs/DESIGN.md", "docs/CONFORMANCE.md", "docs/brand/m.svg"}


def test_t17_blocking_an_unlisted_docs_file_that_would_be_copied_fails_the_export(tmp_path, monkeypatch, capsys):
    real = ep.classify                                                                # the gate regressed: all of docs/ copied
    monkeypatch.setattr(ep, "classify", lambda p: ("copied", "include") if p.startswith("docs/") else real(p))
    repo, sha = make_repo(tmp_path, {"docs/AI-USAGE.md": "a\n", "docs/OTHER.md": "o\n"})
    dest = tmp_path / "out"
    code, out = run(repo, dest, sha, terms_file(tmp_path), capsys)
    assert code == 2 and not dest.exists() and "T17" in out.err and "docs/OTHER.md" in out.err


def test_t15_a_plain_text_path_to_an_excluded_file_is_a_warning_by_path_and_line(tmp_path):
    readme = ("# Title\n"
              "See the brief in internal/briefs/ENCARGO-X.md for details.\n"      # plain text, not a link
              "A [link](internal/process/DECISIONS.md) and the file internal/process/DECISIONS.md.\n"      # Markdown link and plain text
              "A published file docs/AI-USAGE.md is fine, and so is notinternal/briefs/ENCARGO-X.mdx.\n")
    repo, sha = make_repo(tmp_path, {"README.md": readme, "internal/briefs/ENCARGO-X.md": "x\n",
                                     "internal/process/DECISIONS.md": "d\n", "docs/AI-USAGE.md": "a\n"})
    dest = tmp_path / "out"
    assert run(repo, dest, sha, terms_file(tmp_path))[0] == 0                         # a warning, not a block
    text = report(tmp_path)
    assert "warning: README.md:2 names excluded file internal/briefs/ENCARGO-X.md (T15)" in text
    assert "warning: README.md:3 names excluded file internal/process/DECISIONS.md (T15)" in text
    assert "docs/AI-USAGE.md (T15)" not in text and "README.md:4 names" not in text
    assert "See the brief" not in text                                                # path and line only, never the line


def test_t15_the_index_is_not_checked_and_a_clean_export_has_no_t15_warning(tmp_path):
    repo, sha = make_repo(tmp_path, {"evidence/a.jsonl": '{"x": 1}\n'})
    dest = tmp_path / "out"
    assert run(repo, dest, sha, terms_file(tmp_path))[0] == 0
    text = (dest / ep.INDEX_NAME).read_text(encoding="utf-8")
    assert "a.jsonl (private file, hashed)" in text                                 # neutral label, not private path
    assert "evidence/a.jsonl" not in text
    assert "(T15)" not in report(tmp_path)           # the exported tool names its own rule paths: skipped


def test_t15_the_function_reports_each_pair_once_and_sorted():
    outputs = {"b.md": b"x docs/Y.md docs/Y.md\n", "a.md": b"line one\nsee docs/Y.md\n", "EVIDENCE-INDEX.md": b"docs/Y.md\n"}
    assert ep.excluded_path_warnings(outputs, ["docs/Y.md", ""]) == [
        "a.md:2 names excluded file docs/Y.md (T15)", "b.md:1 names excluded file docs/Y.md (T15)"]
    assert ep.excluded_path_warnings(outputs, []) == []


def test_t15_scope_b_the_exporter_and_its_test_are_skipped_but_the_same_hit_elsewhere_is_reported(tmp_path):
    """T15 covers every published text file except tools/export_public.py and
    tools/test_export_public.py, which name their own rule paths by nature."""
    named = "see internal/process/DECISIONS.md for the rules\n"
    repo, sha = make_repo(tmp_path, {"tools/test_export_public.py": named, "README.md": named, "pilot/other.py": "# " + named,
                                     "internal/process/DECISIONS.md": "d\n"})
    assert run(repo, tmp_path / "out", sha, terms_file(tmp_path))[0] == 0
    text = report(tmp_path)
    assert "warning: README.md:1 names excluded file internal/process/DECISIONS.md (T15)" in text
    assert "warning: pilot/other.py:1 names excluded file internal/process/DECISIONS.md (T15)" in text
    t15 = [line for line in text.splitlines() if "(T15)" in line]          # the T18 lines about the real exporter text are not T15
    assert not any("tools/test_export_public.py" in line or "tools/export_public.py" in line for line in t15)
    assert ep.T15_SKIP == ("tools/export_public.py", "tools/test_export_public.py")
    outputs = {"tools/export_public.py": named.encode(), "tools/test_export_public.py": named.encode(),
               "docs/X.md": named.encode()}
    assert ep.excluded_path_warnings(outputs, ["internal/process/DECISIONS.md"]) == [
        "docs/X.md:1 names excluded file internal/process/DECISIONS.md (T15)"]


def test_t15_by_rule_a_name_that_does_not_exist_in_the_commit_is_still_a_warning(tmp_path):
    """T15 also matches path-shaped strings against the rules, so a published file naming a private
    script that is not in the exported commit (added later, or never committed) still warns."""
    readme = ("# Title\n"
              "Run internal/x.ps1 first.\n"                                          # internal/: excluded by X1, absent
              "Notes in scratch-lane9/out.txt and the folder D:\\Private\\specs\\a.json.\n"
              "Not private: docs/AI-USAGE.md, pilot/client.py, notinternal/x.ps1, and copilot/internal/y.py.\n")
    repo, sha = make_repo(tmp_path, {"README.md": readme, "docs/AI-USAGE.md": "a\n"})
    assert run(repo, tmp_path / "out", sha, terms_file(tmp_path))[0] == 0                 # a warning, not a block
    text = report(tmp_path)
    assert "warning: README.md:2 names excluded file internal/x.ps1 (T15)" in text
    assert "warning: README.md:3 names excluded file scratch-lane9/out.txt (T15)" in text
    assert "warning: README.md:3 names excluded file D:\\Private\\specs\\a.json (T15)" in text
    assert "README.md:4 names" not in text and "Run internal" not in text
    outputs = {"pilot/a.py": b"# see internal/x.ps1 and internal/x.ps1\n"}
    assert ep.excluded_path_warnings(outputs, []) == ["pilot/a.py:1 names excluded file internal/x.ps1 (T15)"]
    both = {"pilot/a.py": b"# see internal/x.ps1\n"}                                    # existing and by rule: one warning
    assert ep.excluded_path_warnings(both, ["internal/x.ps1"]) == ["pilot/a.py:1 names excluded file internal/x.ps1 (T15)"]


def test_t15_by_rule_keeps_the_exemption_table_and_does_not_match_evidence_names_by_shape(monkeypatch):
    entry = {"path": "pilot/table.py", "literal": "FILES = ['internal/later.ps1']", "names": "internal/later.ps1", "reason": "path table the test looks up"}
    monkeypatch.setattr(ep, "T15_FUNCTIONAL_LITERALS", [entry])
    hits = []
    assert ep.excluded_path_warnings({"pilot/table.py": b"FILES = ['internal/later.ps1']\n"}, [], hits) == []
    assert hits == [0] and ep.stale_t15(hits) == []
    # the app names its own run-time evidence files; without the file in the commit, evidence/ is not matched by shape
    assert ep.excluded_path_warnings({"pilot/w.py": b"LOG = 'evidence/run.jsonl'\n"}, []) == []
    assert ep.excluded_path_warnings({"pilot/w.py": b"LOG = 'evidence/run.jsonl'\n"}, ["evidence/run.jsonl"]) == [
        "pilot/w.py:1 names excluded file evidence/run.jsonl (T15)"]


def test_lane2_namespaces_are_excluded_and_agents_md_is_not_published():
    # feature namespaces that are not published yet: one rule per namespace
    for path in ("copilot/features/account_cliff/service.py",
                 "copilot/web/templates/features/account_cliff/panel.html",
                 "tests/features/account_cliff/test_service.py"):
        assert ep.classify(path)[0] == "excluded", path
    assert ep.classify("copilot/service.py")[0] == "copied"   # the namespace rule is narrow
    assert ep.classify("AGENTS.md")[0] == "excluded"          # not in INCLUDE: default-deny, internal rules


# ============================================================================================ folder rules
def _classify_all(tmp_path, files):
    repo, sha = make_repo(tmp_path, files)
    return repo, sha, {p: ep.classify(p) for p in files}


def test_h1_diskless_blueprint_is_published_without_opening_other_root_files():
    assert ep.classify('render.free.yaml') == ('copied', 'include')
    assert ep.classify('render.yaml') == ('excluded', 'exclude:render.yaml')        # the paid-plan Blueprint stays private
    assert ep.classify('render.private.yaml') == ('excluded', 'default-deny')
    assert ep.classify('internal/release/RUNBOOK-RENDER-J16.md')[0] == 'excluded'


def test_f1_the_docs_gate_excludes_a_new_docs_file_by_a_named_rule_and_does_not_count_it_as_unmatched(tmp_path, capsys):
    repo, sha = make_repo(tmp_path, {"docs/new.md": "x\n"})
    base = dict(kv.split("=") for kv in (ep.main(["--counts-only", "--commit", sha, "--repo", str(repo)]),
                                         capsys.readouterr().out)[1].split())
    assert ep.classify("docs/new.md") == ("excluded", "docs:not-public")      # fails without the gate: default-deny
    assert ep.classify("docs/AI-USAGE.md")[0] == "copied" and ep.classify("docs/brand/a/b.svg")[0] == "copied"
    repo2, sha2 = make_repo(tmp_path, {"docs/new.md": "x\n", "docs/other.md": "y\n"}, name="repo2")
    ep.main(["--counts-only", "--commit", sha2, "--repo", str(repo2)])
    more = dict(kv.split("=") for kv in capsys.readouterr().out.split())
    assert more["unmatched"] == base["unmatched"]                                # unmatched unchanged by docs files


def test_f2_a_new_file_in_a_public_root_is_published_and_the_author_folders_are_excluded(tmp_path):
    assert ep.classify("tools/new.py") == ("copied", "include")                  # fails without the tools/** root
    assert ep.classify("tools/env/new.ps1") == ("excluded", "exclude:tools/env/**")
    assert ep.classify("tools/hooks/new.py") == ("excluded", "exclude:tools/hooks/**")
    assert ep.classify("copilot/features/x.py")[1] == "exclude:copilot/features/**"       # a feature namespace rule wins over the root


def test_f3_a_belt_inside_a_public_root_wins_over_the_root():
    assert ep.classify("pilot/fixtures/recorded/x.json") == ("excluded", "exclude:pilot/fixtures/recorded/**")
    assert ep.classify("pilot/other.json")[0] == "copied"                        # the root itself still matches


def test_f4_the_root_is_default_deny_and_requirements_files_are_root_only():
    assert ep.classify("NOTES.md") == ("excluded", "default-deny")               # fails if the root were allowed
    assert ep.classify("requirements-extra.txt")[0] == "copied"
    assert ep.classify("sub/requirements-extra.txt") == ("excluded", "default-deny")
    assert ep.classify("AGENTS.md") == ("excluded", "exclude:AGENTS.md")         # an X3 row since the default-deny era


def test_f4_unmatched_counts_the_new_root_note(tmp_path, capsys):
    repo, sha = make_repo(tmp_path, {"NOTES.md": "x\n"})
    ep.main(["--counts-only", "--commit", sha, "--repo", str(repo)])
    tally = dict(kv.split("=") for kv in capsys.readouterr().out.split())
    assert tally["unmatched"] == "1"


def test_f5_a_new_file_in_internal_or_in_docs_outside_public_docs_is_never_published(tmp_path):
    repo, sha = make_repo(tmp_path, {"internal/new-note.md": "x\n", "docs/new-note.md": "y\n", "docs/AI-USAGE.md": "u\n"})
    dest = tmp_path / "out"
    assert run(repo, dest, sha, terms_file(tmp_path))[0] == 0
    present = set(tree(dest))
    assert "internal/new-note.md" not in present and "docs/new-note.md" not in present and "docs/AI-USAGE.md" in present
    rules = {e["path"]: e["rule"] for e in excluded_entries(dest)}
    assert rules["internal/new-note.md"] == "exclude:internal/**" and rules["docs/new-note.md"] == "docs:not-public"


def test_rc42_the_publish_set_rows_name_each_removed_file_and_keep_the_decided_ones():
    removed = ["tools/video/make_captions.py", "tools/video/captions.json", "tools/check_video.ps1",
               "tools/estimate_narration.py", "tools/test_estimate_narration.py", "tools/hosted_smoke.py",
               "tools/check_pending.py", "tools/test_check_pending.py", "pilot/step1_auth.py",
               "docs/brand/preview.html", "render.yaml"]
    for path in removed:
        assert ep.classify(path)[0] == "excluded" and ep.classify(path)[1].startswith("exclude:"), path
    for path in ("docs/DESIGN.md", "docs/DECISIONS.md", "docs/brand/proofie-mark.svg", "pilot/step2_order.py",
                 "render.free.yaml", "tools/export_public.py", "tools/sandbox_seed.py"):
        assert ep.classify(path)[0] == "copied", path
    rows = [pattern for pattern, _ in ep.EXCLUDE]
    assert max(rows.index(p) for p in ("tools/check_pending.py", "render.yaml")) < rows.index("copilot/features/**")


def test_f5_the_rule_rows_follow_the_folder_rules():
    # The hosted blueprint include, the three draft-evaluation feature-scoped exclusions and the ten publish-set rows compose.
    assert len(ep.INCLUDE) + len(ep.PUBLIC_DOCS) == 26 and len(ep.EXCLUDE) == 82 and ep.EXCLUDE[0][0] == "internal/**"
    assert ep.DRAFT_EVAL_PATHS == ("tools/eval_draft.py", "tools/test_eval_draft.py", "pilot/eval/draft_cases/**")
    assert [row for row in ep.EXCLUDE if row[0] in ep.DRAFT_EVAL_PATHS] == [(path, []) for path in ep.DRAFT_EVAL_PATHS]
    assert [p for p, _ in ep.EXCLUDE if p in ("copilot/features/**", "copilot/web/templates/features/**",
                                              "tests/features/**")] == ["copilot/features/**",
                                                                         "copilot/web/templates/features/**",
                                                                         "tests/features/**"]


# ================================================================================================== fold-ins
def test_exp7_x3_agents_md_and_pre_push_are_excluded_by_named_x3_rules_not_by_default_deny():
    assert ep.classify("AGENTS.md") == ("excluded", "exclude:AGENTS.md")
    assert ep.classify("tools/pre-push") == ("excluded", "exclude:tools/pre-push")
    assert ep.classify("tools/other-hook") == ("copied", "include")                  # the row is narrow
    assert ep.classify("NOTES.md") == ("excluded", "default-deny")                   # a new root file still is default-deny


def test_exp7_x3_the_real_tracked_tree_has_no_unmatched_file():
    if not (in_git_checkout(ROOT) and is_private_checkout(ROOT)):   # a public clone has git too, plus the two generated files
        pytest.skip(NEEDS_CHECKOUT)
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files"], check=True, capture_output=True, text=True).stdout.split("\n")
    assert [p for p in tracked if p and ep.classify(p)[1] == "default-deny"] == []   # no unmatched file is left


def test_exp7_the_t15_table_has_the_ten_known_functional_literals_each_with_a_reason():
    # the seven entries that named the pending-placeholder gate's files left with the gate; one entry was added for the
    # host test's row that names the gate (the private repository still needs that row)
    table = ep.T15_FUNCTIONAL_LITERALS
    assert len(table) == 10 and len({(e["path"], e["literal"], e["names"]) for e in table}) == 10
    assert all(set(e) == {"path", "literal", "names", "reason"} and e["reason"] for e in table)


def test_exp7_an_exempt_t15_hit_is_not_a_warning_and_is_counted():
    entry = ep.T15_FUNCTIONAL_LITERALS[0]
    text = (entry["literal"] + "\n").encode()
    hits = []
    assert ep.excluded_path_warnings({entry["path"]: text}, [entry["names"]], hits) == []
    assert hits == [0]


def test_exp7_an_inserted_line_keeps_the_literal_exemption_but_changed_text_does_not():
    entry = ep.T15_FUNCTIONAL_LITERALS[0]
    moved = ("# inserted above the table\n" + entry["literal"] + "\n").encode()
    changed = ("see " + entry["names"] + "\n").encode()
    other = b"see docs/OTHER.md\n"
    hits = []
    assert ep.excluded_path_warnings({entry["path"]: moved}, [entry["names"]], hits) == []
    assert hits == [0]
    hits.clear()
    assert ep.excluded_path_warnings({entry["path"]: changed}, [entry["names"]], hits) == [
        f"{entry['path']}:1 names excluded file {entry['names']} (T15)"]
    assert ep.excluded_path_warnings({entry["path"]: other}, ["docs/OTHER.md"], hits) == [
        f"{entry['path']}:1 names excluded file docs/OTHER.md (T15)"]
    assert hits == []


def test_exp7_an_entry_that_matched_nothing_is_reported_as_stale_and_the_report_prints_the_counts(tmp_path):
    assert ep.stale_t15([]) == [f"stale T15 exemption: {e['path']} {e['names']} matched nothing"
                                for e in ep.T15_FUNCTIONAL_LITERALS]
    assert ep.stale_t15(list(range(1, 10))) == [
        f"stale T15 exemption: {ep.T15_FUNCTIONAL_LITERALS[0]['path']} "
        f"{ep.T15_FUNCTIONAL_LITERALS[0]['names']} matched nothing"]
    repo, sha = make_repo(tmp_path, {})
    assert run(repo, tmp_path / "out", sha, terms_file(tmp_path))[0] == 0
    text = report(tmp_path)
    assert "t15_exempt=0 stale=10" in text and text.count("warning: stale T15 exemption:") == 10


def test_exp7_an_export_that_holds_the_literal_skips_it_and_reports_exempt_one(tmp_path, monkeypatch):
    entry = {"path": "pilot/table.py", "literal": "FILES = ['docs/INTERNAL.md']", "names": "docs/INTERNAL.md", "reason": "path table the test looks up"}
    monkeypatch.setattr(ep, "T15_FUNCTIONAL_LITERALS", [entry])
    repo, sha = make_repo(tmp_path, {"pilot/table.py": "x = 1\nFILES = ['docs/INTERNAL.md']\n", "docs/INTERNAL.md": "i\n"})
    assert run(repo, tmp_path / "out", sha, terms_file(tmp_path))[0] == 0
    text = report(tmp_path)
    assert "t15_exempt=1 stale=0" in text and "(T15)" not in text and "stale T15" not in text


def test_exp7_every_table_entry_still_matches_its_literal_in_the_real_tree():
    if not in_git_checkout(ROOT):
        pytest.skip(NEEDS_CHECKOUT)
    for e in ep.T15_FUNCTIONAL_LITERALS:
        lines = (ROOT / e["path"]).read_text(encoding="utf-8").splitlines()
        assert e["literal"] in [line.strip() for line in lines] and e["names"] in e["literal"]


def test_exp7_removing_a_literal_hit_marks_its_entry_stale(monkeypatch):
    entry = ep.T15_FUNCTIONAL_LITERALS[0]
    monkeypatch.setattr(ep, "T15_FUNCTIONAL_LITERALS", [entry])
    hits = []
    assert ep.excluded_path_warnings({entry["path"]: b"# no table here\n"}, [entry["names"]], hits) == []
    assert ep.stale_t15(hits) == [f"stale T15 exemption: {entry['path']} {entry['names']} matched nothing"]


def test_exp7_the_rule_set_version_covers_the_t15_table(monkeypatch):
    before = ep.rules_version()
    monkeypatch.setattr(ep, "T15_FUNCTIONAL_LITERALS", ep.T15_FUNCTIONAL_LITERALS[:-1])
    assert ep.rules_version() != before


def test_publication_selection_is_empty_by_default_and_known_namespaces_only(monkeypatch):
    assert ep.PUBLISHED_NAMESPACES == []
    monkeypatch.setattr(ep, "PUBLISHED_NAMESPACES", ["account_cliff"])
    assert ep.classify("copilot/features/account_cliff/service.py")[0] == "copied"
    assert ep.classify("copilot/features/resolution/service.py") == ("excluded", "exclude:copilot/features/resolution/**")
    assert ep.classify("copilot/features/unknown/module.py")[0] == "excluded"
    assert ep.classify("copilot/features/__init__.py")[0] == "copied"
    assert ep.classify("internal/a.md")[0] == "excluded"
    monkeypatch.setattr(ep, "PUBLISHED_NAMESPACES", ["DEMO_UNKNOWN"])
    with pytest.raises(ep.ExportError, match="Unknown publication"):
        ep.classify("copilot/features/unknown/module.py")


def test_publication_t15_removes_only_published_targets_and_preserves_stale_checks(monkeypatch):
    assert len(ep.effective_t15()) == 10
    monkeypatch.setattr(ep, "PUBLISHED_NAMESPACES", ["account_cliff", "fulfilment", "outcome", "resolution", "webhooks", "conformance"])
    assert len(ep.effective_t15()) == 8
    assert all(e["names"] not in ("copilot/features/outcome/appeal.py", "copilot/features/conformance/pins.py") for e in ep.effective_t15())
    assert len(ep.stale_t15([])) == 8

@pytest.mark.parametrize("selection", [
    ["triage"],
    ["triage", "resolution", "account_cliff"],
    ["active_approval"],
    ["active_approval", "claims"],
    ["deadlines"],
    ["draft"],
    ["outcome"],
    ["conformance"],
    ["delivery_facts"],
])
def test_publication_refuses_incomplete_dependency_closure(monkeypatch, selection):
    monkeypatch.setattr(ep, "PUBLISHED_NAMESPACES", selection)
    with pytest.raises(ep.ExportError, match="closed under namespace dependencies"):
        ep.effective_exclude()


def test_publication_accepts_supervisor_corrected_transitive_closure(monkeypatch):
    monkeypatch.setattr(ep, "PUBLISHED_NAMESPACES", ["triage", "claims", "clauses", "resolution", "account_cliff"])
    assert ep.classify("copilot/features/triage/service.py")[0] == "copied"
    assert ep.classify("copilot/features/claims/model.py")[0] == "copied"
    assert ep.classify("copilot/features/resolution/service.py")[0] == "copied"
    assert ep.classify("copilot/features/account_cliff/service.py")[0] == "copied"
    assert ep.classify("copilot/features/ledger/service.py")[0] == "excluded"


# The committed publication selection: the 21 earlier namespaces plus the ten later lane namespaces (published on Producto's word, 2026-10-24).
_COMMITTED_CD_SELECTION = ['account_cliff', 'active_approval', 'appeal_workspace', 'bilingual', 'carrier_required', 'claims', 'clauses', 'conformance', 'deadlines', 'delivery_facts', 'demo_documents', 'draft', 'evaluation_workbench', 'events', 'evidence_dossier', 'explainable_queue', 'fulfilment', 'judge_walkthrough', 'ledger', 'local_autopilot', 'local_demo_hub', 'mcp_local_contracts', 'negotiation_drafts', 'outcome', 'package_print', 'replay', 'resolution', 'sale_readiness', 'timeline', 'triage', 'webhooks']


def test_publication_dependency_map_matches_supervisor_frozen_sets():
    frozen = {
        "account_cliff": frozenset(),
        "active_approval": frozenset({"claims", "carrier_required"}),
        "carrier_required": frozenset(),
        "claims": frozenset(),
        "conformance": frozenset({"account_cliff", "fulfilment", "outcome", "resolution", "webhooks"}),
        "deadlines": frozenset({"events"}),
        "delivery_facts": frozenset({"draft"}),
        "demo_documents": frozenset(),
        "draft": frozenset({"resolution"}),
        "events": frozenset(),
        "fulfilment": frozenset(),
        "ledger": frozenset(),
        "outcome": frozenset({"fulfilment", "resolution"}),
        "package_print": frozenset(),
        "resolution": frozenset({"account_cliff"}),
        "triage": frozenset({"claims", "resolution", "clauses"}),
        "webhooks": frozenset(),
        "bilingual": frozenset(),
        "clauses": frozenset(),
        "replay": frozenset(),
        "timeline": frozenset(),
        "appeal_workspace": frozenset(),
        "evaluation_workbench": frozenset(),
        "evidence_dossier": frozenset(),
        "explainable_queue": frozenset({"webhooks"}),
        "judge_walkthrough": frozenset(),
        "local_autopilot": frozenset({"resolution"}),
        "local_demo_hub": frozenset({"local_autopilot", "mcp_local_contracts"}),
        "mcp_local_contracts": frozenset(),
        "negotiation_drafts": frozenset({"local_autopilot", "resolution"}),
        "sale_readiness": frozenset({"fulfilment"}),
    }
    # the nivel2 namespaces were added after this freeze: the frozen sets must not change, new keys may exist
    assert all(ep.NAMESPACE_DEPENDENCIES[name] == deps for name, deps in frozen.items())
    assert set(ep.NAMESPACE_DEPENDENCIES) == set(ep.KNOWN_NAMESPACES)
    assert ep.PUBLISHED_NAMESPACES == []


@pytest.mark.parametrize("selection", [
    ["carrier_required"], ["package_print"],
    ["active_approval", "claims", "carrier_required"],
])
def test_publication_c3_new_namespaces_and_active_closure(monkeypatch, selection):
    monkeypatch.setattr(ep, "PUBLISHED_NAMESPACES", selection)
    for name in selection:
        for root in ("copilot/features/", "copilot/web/templates/features/", "tests/features/"):
            assert ep.classify(root + name + "/probe.py")[0] == "copied"
    assert ep.classify("copilot/features/unknown/probe.py")[0] == "excluded"


def test_delivery_facts_literal_complete_publication_closure(monkeypatch):
    monkeypatch.setattr(ep, "PUBLISHED_NAMESPACES", ["delivery_facts", "draft", "resolution", "account_cliff"])
    assert ep.classify("copilot/features/delivery_facts/service.py")[0] == "copied"
    assert ep.classify("copilot/features/draft/facts.py")[0] == "copied"
    assert ep.classify("copilot/features/resolution/service.py")[0] == "copied"
    assert ep.classify("copilot/features/account_cliff/service.py")[0] == "copied"
    assert ep.classify("copilot/features/claims/service.py")[0] == "excluded"


def test_temporary_selector_does_not_change_committed_cd_selection(monkeypatch):
    import ast
    tree = ast.parse(Path(ep.__file__).read_text(encoding="utf-8"))
    literals = [node.value for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))
                and (any(isinstance(t, ast.Name) and t.id == "PUBLISHED_NAMESPACES" for t in getattr(node, "targets", []))
                     or isinstance(getattr(node, "target", None), ast.Name) and node.target.id == "PUBLISHED_NAMESPACES")]
    assert len(literals) == 1 and sorted(ast.literal_eval(literals[0])) == sorted(ep.KNOWN_NAMESPACES)
    assert len(ep.KNOWN_NAMESPACES) == 61 and set(ep.KNOWN_NAMESPACES) == set(_COMMITTED_CD_SELECTION) | _NIVEL2_NAMESPACES
    assert ep.PUBLISHED_NAMESPACES == []
    monkeypatch.setattr(ep, "PUBLISHED_NAMESPACES", ["delivery_facts", "draft", "resolution", "account_cliff"])
    assert ep.PUBLISHED_NAMESPACES != []
    assert sorted(ast.literal_eval(literals[0])) == sorted(ep.KNOWN_NAMESPACES)
    ep.effective_exclude()


def test_pre_gate_matrix_runner_is_private_but_other_tools_stay_public():
    assert ep.classify("tools/flags_on_matrix.py") == ("excluded", "exclude:tools/flags_on_matrix.py")
    assert ep.classify("tools/new.py") == ("copied", "include")
    assert ep.PUBLISHED_NAMESPACES == []


AUTO_RUN_PRIVATE = ["tools/auto_run.py", "tools/auto_run_pages.py", "tools/auto_run_dry_app.py", "tests/tools/test_auto_run.py",
                    "tools/vendor/axe-core-4.10.2/axe.min.js", "tools/vendor/axe-core-4.10.2/LICENSE",
                    "tools/vendor/axe-core-4.10.2/README.md"]


def test_the_automated_walk_harness_and_its_vendored_axe_copy_are_private():
    for path in AUTO_RUN_PRIVATE:
        assert ep.classify(path) == ("excluded", f"exclude:{ep.classify(path)[1][len('exclude:'):]}"), path
        assert ep.classify(path)[1].startswith("exclude:tools/") or ep.classify(path)[1].startswith("exclude:tests/tools/"), path
    assert ep.classify("tools/new.py") == ("copied", "include")


def test_the_judge_self_check_and_its_test_ship_in_the_kit(tmp_path):
    shipped = ("tools/judge_check.py", "tests/tools/test_judge_check.py")
    for path in shipped:
        assert ep.classify(path) == ("copied", "include"), path
    repo, sha = make_repo(tmp_path, {**{p: "x\n" for p in shipped}, "tests/tools/test_auto_run.py": "x\n"})
    dest = tmp_path / "out"
    assert run(repo, dest, sha, terms_file(tmp_path))[0] == 0
    present = set(tree(dest))
    assert set(shipped) <= present and "tests/tools/test_auto_run.py" not in present


def test_an_export_contains_none_of_the_automated_walk_files(tmp_path):
    files = {path: "x\n" for path in AUTO_RUN_PRIVATE}
    files["tools/test_other.py"] = "t\n"
    repo, sha = make_repo(tmp_path, files)
    dest = tmp_path / "out"
    assert run(repo, dest, sha, terms_file(tmp_path))[0] == 0
    present = set(tree(dest))
    assert "tools/test_other.py" in present
    assert not present & set(AUTO_RUN_PRIVATE)
    assert not any(p.startswith("tools/vendor/axe") or p.startswith("tests/tools/") for p in present)
    rules = {e["path"]: e["rule"] for e in excluded_entries(dest)}
    assert all(path in rules for path in AUTO_RUN_PRIVATE)


# This test oracle is deliberately independent of published_env_template(): a fixed
# flag contract plus actual package files, not the exporter output used as expectation.
_A4_NAMES = frozenset({'account_cliff', 'active_approval', 'carrier_required', 'claims',
                       'conformance', 'deadlines', 'delivery_facts', 'demo_documents', 'draft', 'events',
                       'fulfilment', 'ledger', 'outcome', 'package_print', 'resolution',
                       'triage', 'webhooks', 'bilingual', 'clauses', 'replay', 'timeline'})
_O4_FLAGS = ('attention_model', 'local_preparation', 'gap_resolution', 'selected_preparation', 'incident_proposals', 'closure_summary')
_A4_PARENTS = {'fulfilment_register': 'fulfilment', 'resolution_writes': 'resolution',
               'outcome_appeal': 'outcome', 'webhooks_verify': 'webhooks',
               # O4 flags of published namespaces (on in .env.example)
               'attention_model': 'local_autopilot', 'local_preparation': 'local_autopilot', 'gap_resolution': 'local_autopilot',
               'selected_preparation': 'explainable_queue', 'incident_proposals': 'buyer_note', 'closure_summary': 'accounting_line',
               # D-101 P3: a section of the account page, switched by its own flag
               'account_cliff_path': 'account_cliff',
               # D-101 r1: the seeded-case proposal lives in the demo_documents package
               'demo_documents_seeded': 'demo_documents',
               # D-101 r2: the inbox watch box lives with the queue it watches
               'inbox_watch': 'explainable_queue',
               # D-101 B5: the prepared status message lives in the resolution package
               'resolution_status_message': 'resolution'}


_CURRENT_LANE_NAMES = frozenset({'evidence_dossier', 'explainable_queue', 'negotiation_drafts', 'sale_readiness', 'appeal_workspace',
                         'mcp_local_contracts', 'evaluation_workbench', 'judge_walkthrough', 'local_autopilot',
                         'local_demo_hub', 'accounting_line', 'buyer_note', 'call_script', 'carrier_directory',
                         'declared_limits', 'do_nothing_path', 'first_dispute_guide', 'judge_as_buyer',
                         'lessons_exchange', 'lost_package_paths', 'offline_market_mode', 'package_insert',
                         'presentation_mode', 'read_aloud_review', 'sale_lesson', 'season_prep', 'second_opinion',
                         'ship_moment_capture', 'ship_notice', 'third_party_statement', 'trade_profiles',
                         'transit_delivered_guide', 'two_clocks', 'mock_adjudicator', 'case_history', 'case_reentry', 'judge_pool', 'status_link', 'accept_claim'})
# Namespaces added by the nivel2 lane after the committed selection; mock_adjudicator is flag-only (no package).
_NIVEL2_NAMESPACES = frozenset({'accounting_line', 'buyer_note', 'buyer_page', 'call_script', 'carrier_directory',
                                'declared_limits', 'do_nothing_path', 'first_dispute_guide', 'judge_as_buyer',
                                'lessons_exchange', 'lost_package_paths', 'mock_adjudicator', 'offline_market_mode',
                                'package_insert', 'presentation_mode', 'read_aloud_review', 'sale_lesson',
                                'season_prep', 'second_opinion', 'ship_moment_capture', 'ship_notice',
                                'third_party_statement', 'trade_profiles', 'transit_delivered_guide', 'two_clocks',
                                'case_history', 'case_reentry', 'judge_pool', 'status_link', 'accept_claim'})
_RC43_LANE_NAMES = frozenset({'evidence_dossier', 'explainable_queue', 'negotiation_drafts', 'sale_readiness',
                              'appeal_workspace', 'mcp_local_contracts', 'evaluation_workbench', 'judge_walkthrough',
                              'local_autopilot', 'local_demo_hub'})
_LANE_DEFAULT_OFF = frozenset()     # the nivel2 flags are true in .env.example since 4bc9ef6b / 52cda2f7
_FLAG_ONLY_DISABLED = frozenset()      # the dead mock_adjudicator row was removed from .env.example
_CORE_FLAGS = frozenset({'rearm_note_ignored', 'seller_shortcuts'})   # read by copilot/core and by the approval routes, on in .env.example, kept by the public template


def _lane_present(tree_root):
    return {key for key in _CURRENT_LANE_NAMES if (tree_root / 'copilot' / 'features' / key / '__init__.py').is_file()}


def _assert_feature_template_contract(text, tree_root, enabled):
    flags = _A4_NAMES | _A4_PARENTS.keys()
    required = {key for key in flags
                if (tree_root / 'copilot' / 'features' / _A4_PARENTS.get(key, key)
                    / '__init__.py').is_file()} | _lane_present(tree_root) | _FLAG_ONLY_DISABLED
    # Compare against the immutable source checkout: tests temporarily monkeypatch ROOT
    # to a synthetic tree, so ROOT cannot distinguish source flags from fixture flags.
    source_root = Path(__file__).resolve().parents[1]
    if Path(tree_root).resolve() != source_root:
        required -= _FLAG_ONLY_DISABLED
    else:      # the source tree carries the core code that reads these settings; a trimmed profile tree does not
        required |= _CORE_FLAGS
        enabled = set(enabled) | _CORE_FLAGS
    rows = re.findall(r'^COPILOT_FEATURE_([A-Z_]+)\s*=\s*(.*?)\s*$', text, re.M)
    entries = {key.lower(): value for key, value in rows}
    assert len(rows) == len(entries), 'duplicate feature key'
    assert set(entries) == required, 'missing or extra feature key'
    assert set(enabled) <= required, 'enabled feature has no published parent'
    assert all(value in {'true', 'false'} for value in entries.values()), 'invalid flag value'
    assert {key for key, value in entries.items() if value == 'true'} == set(enabled)
    assert all(value == 'false' for key, value in entries.items() if key not in enabled)


def test_canonical_cd_selection_and_template_match_without_runtime_default_changes():
    import ast
    tree = ast.parse(Path(ep.__file__).read_text(encoding="utf-8"))
    selection = next(ast.literal_eval(n.value) for n in tree.body
                     if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", None) == "PUBLISHED_NAMESPACES")
    expected = _COMMITTED_CD_SELECTION
    assert selection == sorted(set(expected) | _NIVEL2_NAMESPACES) and len(set(selection)) == 61
    for name in selection:
        assert ep.NAMESPACE_DEPENDENCIES[name] <= set(selection)
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    enabled = (set(selection) - _CURRENT_LANE_NAMES - {'conformance', 'demo_documents', 'webhooks', 'buyer_page'}) | {'resolution_writes', 'fulfilment_register', 'outcome_appeal', 'account_cliff_path', 'demo_documents_seeded', 'resolution_status_message'} | ({'inbox_watch'} if (ROOT / 'copilot' / 'features' / 'explainable_queue' / '__init__.py').is_file() else set())
    # O4 flags of published namespaces are on wherever their namespace is present (a trimmed tree has no such parent)
    enabled |= {key for key in _O4_FLAGS if (ROOT / 'copilot' / 'features' / _A4_PARENTS[key] / '__init__.py').is_file()}
    # The source tree has conformance; the public trimmed tree does not.
    if (ROOT / 'copilot/features/conformance/__init__.py').is_file():
        enabled.add('conformance')
    enabled |= _lane_present(ROOT) - _LANE_DEFAULT_OFF
    _assert_feature_template_contract(text, ROOT, enabled)

# Binary metadata is checked, never exempted by synthetic document text rules.
def _rc4_kit_source_selection():
    import ast
    tree = ast.parse(Path(ep.__file__).read_text(encoding='utf-8'))
    selection = next(ast.literal_eval(node.value) for node in tree.body
                     if isinstance(node, ast.AnnAssign)
                     and getattr(node.target, 'id', None) == 'PUBLISHED_NAMESPACES')
    # RC4.3 was frozen before the RC4.4 hosted demo namespace and the original ten lane namespaces existed.
    return [name for name in selection if name not in {'demo_documents', 'buyer_page'} | _CURRENT_LANE_NAMES]


@pytest.mark.parametrize('namespace', ['conformance', 'webhooks'])
def test_rc4_kit_publication_includes_owned_modules_resources_and_tests(monkeypatch, namespace):
    selection = _rc4_kit_source_selection()
    assert namespace in selection
    monkeypatch.setattr(ep, 'PUBLISHED_NAMESPACES', selection)
    groups = [
        list((ROOT / 'copilot/features' / namespace).rglob('*.py')),
        list((ROOT / 'copilot/web/templates/features' / namespace).rglob('*.html')),
        list((ROOT / 'tests/features' / namespace).rglob('test_*.py')),
    ]
    if namespace == 'conformance':
        groups.append(list((ROOT / 'copilot/features/conformance/fixtures').glob('*.json')))
    for group in groups:
        assert group, namespace
        for path in group:
            assert ep.classify(path.relative_to(ROOT).as_posix())[0] == 'copied', path
    assert ep.classify('copilot/features/unknown/probe.py')[0] == 'excluded'
    assert ep.classify('internal/probe.md')[0] == 'excluded'


def test_rc43_kit_template_keeps_twenty_four_flags_twenty_one_enabled(monkeypatch):
    monkeypatch.setattr(ep, 'PUBLISHED_NAMESPACES', _rc4_kit_source_selection())
    published = {path.relative_to(ROOT).as_posix()
                 for path in (ROOT / 'copilot/features').rglob('*.py')
                 if ep.classify(path.relative_to(ROOT).as_posix())[0] == 'copied'}
    template = ep.published_env_template((ROOT / '.env.example').read_bytes(), published)
    rows = re.findall(rb'^COPILOT_FEATURE_([A-Z_]+)=(true|false)\s*$', template, re.M)
    assert len(rows) == len(dict(rows)) == 27      # 24 + the dated-path flag of account_cliff + seller_shortcuts + status-message
    assert sum(value == b'true' for _, value in rows) == 25      # 21 + the appeal call + the dated-path flag + seller_shortcuts + status-message
    assert {key for key, value in rows if value == b'false'} == {
        b'WEBHOOKS', b'WEBHOOKS_VERIFY'}
    assert b'DEMO_DOCUMENTS' not in dict(rows)  # RC4.3 kit keeps its original 24-flag contract.
    assert dict(rows)[b'CONFORMANCE'] == b'true'


def test_rc44_kit_template_carries_thirty_five_flags_thirty_one_enabled(monkeypatch):
    monkeypatch.setattr(ep, 'PUBLISHED_NAMESPACES', list(_COMMITTED_CD_SELECTION))
    published = {path.relative_to(ROOT).as_posix()
                 for path in (ROOT / 'copilot/features').rglob('*.py')
                 if ep.classify(path.relative_to(ROOT).as_posix())[0] == 'copied'}
    template = ep.published_env_template((ROOT / '.env.example').read_bytes(), published)
    rows = re.findall(rb'^COPILOT_FEATURE_([A-Z_]+)=(true|false)\s*$', template, re.M)
    assert len(rows) == len(dict(rows)) == 44      # 35 + the four O4 flags of local_autopilot and explainable_queue + the dated-path, seeded-demo, inbox-watch and status-message flags
    assert sum(value == b'true' for _, value in rows) == 41      # 35 + the appeal call + the dated-path, seeded-demo, inbox-watch and status-message flags
    assert {key for key, value in rows if value == b'false'} == {
        b'DEMO_DOCUMENTS', b'WEBHOOKS', b'WEBHOOKS_VERIFY'}
    lane = {name.upper().encode() for name in _RC43_LANE_NAMES}
    assert lane <= set(dict(rows)) and all(dict(rows)[key] == b'true' for key in lane)


def test_source_env_comment_describes_current_flag_counts():
    text = (ROOT / '.env.example').read_text(encoding='utf-8')
    comment = next(line for line in text.splitlines()
                   if line.startswith('# ') and 'feature' in line.lower() and 'flags' in line.lower())
    rows = re.findall(r'^COPILOT_FEATURE_[A-Z_]+=(true|false)\s*$', text, re.M)
    match = re.search(r'(\d+) settings, (\d+) enabled', comment)
    assert match and int(match.group(1)) == len(rows)
    assert int(match.group(2)) == rows.count('true')
    assert 'Code defaults stay off without configuration.' in comment


def _privacy_pdf(author='', xmp=False, compressed=False, info_key='Author'):
    import zlib
    objects = [b'<< /Type /Catalog /Pages 2 0 R' + (b' /Metadata 6 0 R' if xmp else b'') + b' >>',
               b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R >>',
               b'<< /Length 0 >>\nstream\n\nendstream',
               b'<< /' + info_key.encode() + b' (' + author.encode() + b') >>']
    if xmp:
        raw = ('<x:xmpmeta xmlns:x="adobe:ns:meta/"><author>' + author + '</author></x:xmpmeta>').encode()
        raw = zlib.compress(raw) if compressed else raw
        objects[4] = b'<< >>'
        objects.append(b'<< /Type /Metadata /Subtype /XML /Length ' + str(len(raw)).encode()
                       + (b' /Filter /FlateDecode' if compressed else b'') + b' >>\nstream\n' + raw + b'\nendstream')
    output = b'%PDF-1.4\n'; offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output += str(number).encode() + b' 0 obj\n' + obj + b'\nendobj\n'
    start = len(output)
    output += b'xref\n0 ' + str(len(offsets)).encode() + b'\n0000000000 65535 f \n'
    output += b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
    return (output + b'trailer\n<< /Size ' + str(len(offsets)).encode()
            + b' /Root 1 0 R /Info 5 0 R >>\nstartxref\n' + str(start).encode() + b'\n%%EOF\n')


def _privacy_png(author='', mode='text'):
    import io
    from PIL import Image, PngImagePlugin
    image = Image.new('RGB', (1, 1))
    output = io.BytesIO()
    if mode in {'exif', 'xp'}:
        exif = Image.Exif()
        exif[315 if mode == 'exif' else 40093] = author if mode == 'exif' else author.encode('utf-16-le')
        image.save(output, format='PNG', exif=exif)
    else:
        info = PngImagePlugin.PngInfo()
        if mode == 'itxt':
            info.add_itxt('Author', author, zip=True)
        else:
            info.add_text('Author', author, zip=mode == 'ztxt')
        image.save(output, format='PNG', pnginfo=info)
    return output.getvalue()


@pytest.mark.parametrize('format', ['pdf-info', 'pdf-xmp', 'pdf-xmp-flate', 'png-text', 'png-ztxt', 'png-itxt', 'png-exif', 'png-xp'])
def test_private_author_binary_metadata_blocks_with_unchanged_detectors(format):
    from tools import redact
    author = 'SYNTHETIC' + '-PRIVATE-AUTHOR'
    if format.startswith('pdf'):
        blob = _privacy_pdf(author, xmp='xmp' in format, compressed='flate' in format)
        suffix = 'pdf'
    else:
        blob = _privacy_png(author, format.split('-', 1)[1])
        suffix = 'png'
    scan = ep.scan_tree({'demo/documents/metadata.' + suffix: blob}, redact, [author.lower()])
    assert any(kind.startswith(ep.PRIVATE_TERM_KIND) for _, _, kind in scan['blocking'])
    assert scan['skipped_binary'] == 0 and scan['scanned_binary'] == 1


@pytest.mark.parametrize('suffix', ['pdf', 'png'])
def test_metadata_cannot_use_synthetic_document_address_exemption(suffix):
    from tools import redact
    author = str(10) + ' Fictional ' + 'Street'
    blob = _privacy_pdf(author) if suffix == 'pdf' else _privacy_png(author)
    scan = ep.scan_tree({'demo/documents/metadata.' + suffix: blob}, redact, ['unrelated-private-term'])
    assert any(kind == 'street_address' for _, _, kind in scan['blocking'])
    assert not scan['allowed']


@pytest.mark.parametrize('suffix', ['pdf', 'png'])
def test_clean_binary_metadata_is_scanned_without_modifying_bytes(suffix):
    from tools import redact
    blob = _privacy_pdf() if suffix == 'pdf' else _privacy_png()
    files = {'demo/documents/clean.' + suffix: blob}
    scan = ep.scan_tree(files, redact, ['unrelated-private-term'])
    assert scan['blocking'] == [] and scan['scanned_binary'] == 1 and scan['skipped_binary'] == 0
    assert files['demo/documents/clean.' + suffix] == blob


def test_env_example_placeholder_and_flag_values_only():
    from tools import redact
    scan = ep.scan_tree({'.env.example': b'PAYPAL_CLIENT_ID=your-sandbox-id\nCOPILOT_FEATURE_CLAIMS=true\n'},
                        redact, ['unrelated-private-term'])
    assert scan['blocking'] == []
    bad = ep.scan_tree({'.env.example': b'OTHER_SETTING=not-a-placeholder\nCOPILOT_FEATURE_CLAIMS=maybe\n'},
                       redact, ['unrelated-private-term'])
    assert sum(kind == 'template_assignment' for _, _, kind in bad['blocking']) == 2


def test_custom_pdf_info_key_is_not_a_metadata_blind_spot():
    from tools import redact
    author = 'SYNTHETIC' + '-PRIVATE-AUTHOR'
    scan = ep.scan_tree({'demo/documents/custom.pdf': _privacy_pdf(author, info_key='CustomOwner')},
                        redact, [author.lower()])
    assert any(kind.startswith(ep.PRIVATE_TERM_KIND) for _, _, kind in scan['blocking'])
    assert scan['scanned_binary'] == 1


def test_inline_pdf_info_is_blocked_when_metadata_cannot_be_extracted():
    from tools import redact
    author = 'SYNTHETIC' + '-PRIVATE-AUTHOR'
    pdf = _privacy_pdf(author, info_key='CustomOwner')
    inline = pdf.replace(b'/Info 5 0 R', b'/Info << /CustomOwner (' + author.encode() + b') >>')
    assert inline != pdf
    scan = ep.scan_tree({'demo/documents/inline.pdf': inline}, redact, [author.lower()])
    assert scan['blocking']
    assert scan['skipped_binary'] == 0


@pytest.mark.parametrize('escaped_name', [b'#49nfo', b'In#66o', b'#49#6e#66#6f'])
def test_escaped_pdf_info_container_cannot_hide_custom_metadata(escaped_name):
    from tools import redact
    import pypdfium2 as pdfium
    author = 'SYNTHETIC' + '-PRIVATE-REVIEWER'
    pdf = _privacy_pdf(author, info_key='CustomOwner')
    escaped = pdf.replace(b'/Info 5 0 R', b'/' + escaped_name + b' 5 0 R')
    assert escaped != pdf
    with pdfium.PdfDocument(escaped) as document:
        assert document.get_metadata_value('CustomOwner') == author
    scan = ep.scan_tree({'demo/documents/escaped-container.pdf': escaped}, redact, [author.lower()])
    assert scan['blocking']
    assert scan['skipped_binary'] == 0


def test_a4_canonical_profile_runs_in_a_synthetic_trimmed_cd_tree(tmp_path, monkeypatch):
    """Reproduce the carrier-gate cases through the existing canonical test, not an exporter oracle."""
    names = {'account_cliff', 'active_approval', 'bilingual', 'carrier_required', 'claims', 'clauses', 'deadlines', 'demo_documents',
             'delivery_facts', 'draft', 'events', 'fulfilment', 'ledger', 'outcome',
             'package_print', 'replay', 'resolution', 'timeline', 'triage'}
    for name in names:
        module = tmp_path / 'copilot' / 'features' / name / '__init__.py'
        module.parent.mkdir(parents=True)
        module.write_text('', encoding='utf-8')
    enabled = (names - {'demo_documents'}) | {'fulfilment_register', 'resolution_writes', 'outcome_appeal', 'account_cliff_path',
                                              'demo_documents_seeded', 'resolution_status_message'}
    text = ''.join('COPILOT_FEATURE_' + key.upper() + '=true\n' for key in sorted(enabled))
    text += 'COPILOT_FEATURE_DEMO_DOCUMENTS=false\n'
    assert len(text.splitlines()) == 25  # present namespaces plus subfeature keys (the appeal call is on); hosted demo documents remain off
    (tmp_path / '.env.example').write_text(text, encoding='utf-8')
    monkeypatch.setitem(globals(), 'ROOT', tmp_path)
    test_canonical_cd_selection_and_template_match_without_runtime_default_changes()


@pytest.mark.parametrize('profile,count', [('full', 28), ('cc', 17), ('cd', 19)])
@pytest.mark.parametrize('mutation', [None, 'missing', 'extra', 'invalid', 'wrong', 'duplicate'])
def test_a4_tree_oracle_keeps_missing_extra_and_value_guards(tmp_path, profile, count, mutation):
    cc = {'account_cliff', 'active_approval', 'carrier_required', 'claims', 'deadlines',
          'events', 'fulfilment', 'ledger', 'outcome', 'package_print', 'resolution', 'triage'}
    selected = cc | ({'delivery_facts', 'draft'} if profile != 'cc' else set())
    modules = _A4_NAMES if profile == 'full' else selected
    for name in modules:
        module = tmp_path / 'copilot' / 'features' / name / '__init__.py'
        module.parent.mkdir(parents=True)
        module.write_text('', encoding='utf-8')
    # Independent explicit fixture contract, not the helper under test or exporter.
    keys = (set(modules) | {'fulfilment_register', 'resolution_writes', 'outcome_appeal', 'account_cliff_path', 'resolution_status_message'}
            | ({'demo_documents_seeded'} if 'demo_documents' in modules else set())
            | ({'webhooks_verify'} if profile == 'full' else set()))
    enabled = (selected | {'account_cliff_path', 'resolution_status_message'} | ({'demo_documents_seeded'} if 'demo_documents' in modules else set())
               | ({'fulfilment_register', 'resolution_writes'} if profile != 'cc' else set()))
    rows = ['COPILOT_FEATURE_' + key.upper() + '=' + ('true' if key in enabled else 'false')
            for key in sorted(keys)]
    assert len(rows) == count
    if mutation == 'missing':
        rows.pop()
    elif mutation == 'extra':
        rows.append('COPILOT_FEATURE_UNPUBLISHED=false')
    elif mutation == 'invalid':
        rows[0] = rows[0].split('=')[0] + '=maybe'
    elif mutation == 'wrong':
        rows[0] = rows[0].split('=')[0] + '=false'  # ACCOUNT_CLIFF must be enabled
    elif mutation == 'duplicate':
        rows.append(rows[0])
    text = '\n'.join(rows) + '\n'
    if mutation is None:
        _assert_feature_template_contract(text, tmp_path, enabled)
    else:
        with pytest.raises(AssertionError):
            _assert_feature_template_contract(text, tmp_path, enabled)


@pytest.mark.parametrize('selected,published', [([], False), (['account_cliff', 'resolution', 'draft'], True)])
def test_eval_draft_publication_follows_its_owned_namespace(monkeypatch, selected, published):
    monkeypatch.setattr(ep, 'PUBLISHED_NAMESPACES', selected)
    for path in ('tools/eval_draft.py', 'tools/test_eval_draft.py', 'pilot/eval/draft_cases/complete_dev.json'):
        assert (ep.classify(path)[0] == 'copied') is published


def test_seed_demo_context_keeps_exact_approved_address_and_reports_manifest():
    import redact
    from tools import sandbox_seed
    source = (ROOT / 'tools/sandbox_seed.py').read_bytes()
    scan = ep.scan_tree({'tools/sandbox_seed.py': source}, redact, ['unmatched-private-marker'])
    assert not scan['blocking']
    assert any(a[3][0] == 'seed-demo-address-context' for a in scan['allowed'])
    assert hashlib.sha256(sandbox_seed.SHIPPING_ADDRESS['address_line_1'].encode()).hexdigest() == ep.SEED_DEMO_ADDRESS_CONTEXT['sha256']
    result = json.loads(ep.build_manifest('a' * 40, 'b' * 64, [], [], scan, False))
    assert any(e.get('context') == 'SHIPPING_ADDRESS.address_line_1' and e['hits'] == 1
               for e in result['scan']['exceptions'])


@pytest.mark.parametrize('change', ['value', 'marker', 'path', 'extra', 'credential'])
def test_seed_context_never_exempts_changed_private_data_or_other_kinds(change):
    import redact
    from tools import sandbox_seed
    source = (ROOT / 'tools/sandbox_seed.py').read_text(encoding='utf-8')
    path = 'tools/sandbox_seed.py'
    if change == 'value':
        source = source.replace(sandbox_seed.SHIPPING_ADDRESS['address_line_1'],
                                sandbox_seed.SHIPPING_ADDRESS['address_line_1'].replace('123', '98765'))
    elif change == 'marker':
        source = source.replace(ep.SEED_DEMO_ADDRESS_CONTEXT['marker'], '# No provenance marker')
    elif change == 'path':
        path = 'tools/other_seed.py'
    elif change == 'extra':
        source = source.replace("'admin_area_2':", "'extra_address': " + repr(sandbox_seed.SHIPPING_ADDRESS['address_line_1'].replace('123', '98765')) + ", 'admin_area_2':", 1)
    else:
        source += '\nclient' + '_secret = ' + repr('Z' * 32) + '\n'
    scan = ep.scan_tree({path: source.encode()}, redact, ['unmatched-private-marker'])
    assert scan['blocking'] and all(hit[2] != 'private_term' for hit in scan['blocking'])
    if change == 'credential':
        assert any(hit[2] == 'credential_assignment' for hit in scan['blocking'])
    else:
        assert any(hit[2] == 'street_address' for hit in scan['blocking'])


def test_reserved_fixture_email_does_not_weaken_real_domain_detection():
    import redact
    allowed = json.dumps({'email_address': 'buyer@example.com'})
    blocked = allowed.replace('example.com', 'elsewhere.invalid')
    assert not ep.scan_tree({'tests/local/fixture.py': allowed.encode()}, redact, ['never-present'])['blocking']
    assert ep.scan_tree({'tests/local/fixture.py': blocked.encode()}, redact, ['never-present'])['blocking']


def test_seed_context_never_overrides_the_explicit_private_terms_gate():
    import redact
    from tools import sandbox_seed
    source = (ROOT / 'tools/sandbox_seed.py').read_bytes()
    scan = ep.scan_tree({'tools/sandbox_seed.py': source}, redact,
                        [sandbox_seed.SHIPPING_ADDRESS['address_line_1'].casefold()])
    assert any(hit[2].startswith(ep.PRIVATE_TERM_KIND) for hit in scan['blocking'])


# ============================================================================== T18 hygiene guard, T15 old docs paths
def _t18(files, repo=None, allow=None, monkeypatch=None):
    if allow is not None:
        monkeypatch.setattr(ep, "T18_ALLOWLIST", allow)
    used = []
    found = ep.hygiene_warnings(repo, {p: (d if isinstance(d, bytes) else d.encode("utf-8")) for p, d in files.items()}, used)
    return found, used


def _w(path, line, family):
    return f"{path}:{line} {family} in a comment, docstring or prose (T18)"


@pytest.mark.parametrize("comment,family", [
    ("# G-SH-7: a file the attempt carried", "round-id"),
    ("# RC4.1 fix3 note", "release-label"),
    ("# RC42-L4-NAME-READBACK", "release-label"),
    ("# supervisor decision 2026-10-05", "role-word"),
    ("# the second verifier read it", "role-word"),
    ("# table 2 may reword it", "role-word"),
    ("# per the brief", "role-word"),
    ("# the brief's own wording", "role-word"),
    ("# the integrator found it", "role-word"),
    ("# the integral review", "role-word"),
    ("# Round 3 reclaimed the row", "role-word"),
    ("# refuerzo 2 prepares it", "role-word"),
    ("# one verification round", "role-word"),
    ("# stream 1 blocks", "role-word"),
    ("# the other stream flags stay unset", "role-word"),
    ("# Gate D1-b duplicate-send risk", "gate-name"),
    ("# R1 B3: atomic claim", "round-id"),
    ("# A5c: PayPal shows UNDER_REVIEW", "round-id"),
    ("# J8: the reason for a draft", "round-id"),
    ("# VER-INTEG3 finding", "round-id"),
    ("# AUD1 M3 and SEC2 item 2", "round-id"),
    ("# FIX-RONDA4 and INT-FIX2", "round-id"),
    ("# D2 block 1b F2 label", "round-id"),
    ("# OPS16cs next-task urls", "round-id"),
    ("# see point 2a of the list", "round-id"),
    ("# Point 2A of the list", "round-id"),
    ("# CODEX0: forbidden paths", "round-id"),
    ("# CRIT-9a rule", "round-id"),
    ("# the same window B3 already uses", "item-label"),
    ("# the model obeyed (G5/D8): detection", "item-label"),
    ("# older names (pre-SAN1; not schema)", "round-id"),
    ("# that is the end of S8.", "item-label"),
    ("# see H3-B for it", "item-label"),
    ("# keys named EVD1 and S10", "item-label"),
    ("# Jonas decided this", "owner-name"),
    ("# Jonás decided this", "owner-name"),
    ("# approved in D-2099-01-01-99", "decision-id"),
])
def test_t18_a_planted_id_or_role_word_in_a_comment_is_one_warning_per_family(comment, family):
    found, _ = _t18({"copilot/a.py": comment + "\nx = 1\n"})
    assert found == [_w("copilot/a.py", 1, family)]


@pytest.mark.parametrize("comment", [
    "# git diff --cached -U0 and the range [A-Z0-9]{10,}",
    "# the file S3.json, the version V2.1 and the path docs/AB1",
    "# 12 of 28 cases, 3 of 5 seeds, B2B and UTF-8, EV-12 shows it, ERRDIS047, PP-R-ABC-12345678",
    "# a pipeline of 4 steps and a table of 2 columns",
])
def test_t18_does_not_flag_paths_decimals_file_names_ranges_or_the_ids_it_keeps(comment):
    assert _t18({"copilot/a.py": comment + "\n"})[0] == []


def test_t18_reads_docstrings_but_never_a_string_literal_that_code_uses():
    source = ('"""Module note: the supervisor read it."""\n'
              'ROLE = "supervisor"          # the value a test compares\n'
              'LABEL = "G-SH-7"\n'
              'def f():\n'
              '    """Docstring with R1 B3."""\n'
              '    return "RC4.1"\n')
    found, _ = _t18({"pilot/a.py": source})
    assert found == [_w("pilot/a.py", 1, "role-word"), _w("pilot/a.py", 5, "round-id")]


def test_t18_reads_sql_comments_only_inside_a_create_table_string():
    sql = ('SCHEMA = """\nCREATE TABLE t (\n    a TEXT NOT NULL,   -- '
           + "R1" + ' A4: a shell\n    b TEXT\n);\n"""\n'
           'OTHER = """\n    x TEXT,   -- '
           + "R1" + ' A4: not sql\n"""\n')
    assert _t18({"copilot/db/s.py": sql})[0] == [_w("copilot/db/s.py", 3, "round-id")]


def test_t18_an_unparsable_python_file_is_one_warning_and_is_not_read_whole():
    found, _ = _t18({"copilot/bad.py": "def (:\n# supervisor\n"})
    assert found == ["copilot/bad.py does not parse, so T18 did not read it (T18)"]


def test_t18_reads_the_comment_shapes_of_other_files_including_the_ones_without_a_listed_suffix():
    files = {"copilot/web/t.html": "<p>FIX2 C8 is visible text</p>\n{# C3 label per field #}\n<!-- table 2 copy -->\n",
             "copilot/web/s.css": "/* RC4.1 Part U: next step */\n.a { color: red; }\n",
             "copilot/web/s.js": "// Gate D1-a note\nconst x = 'https://example.com//y';\n",
             "render.yaml": "name: x   # supervisor note\nkey: SH-1\n",
             "docs/N.md": "The supervisor read it. EV-12 stays.\n",
             "docs/NOTE.txt": "a note by Jonas\n",
             "tests/f/x.json": '{"old": "<!-- R1 B9: re-check -->", "k": "supervisor"}\n',
             ".gitignore": "/data/\n# Jonas cleanup list\n",
             "pytest.ini": "[pytest]\n# K8: bound a test\ntimeout = 600\n",
             "tools/pre-commit": "#!/bin/sh\n# the supervisor note\n",
             "tools/evidence_binaries.sha256": "# Maintained by Jonas only\n" + "a" * 64 + "  label with supervisor\n",
             ".env.example": "KEY=1  # SEC2 note\n",
             ".mcp.json.example": "{}\n",
             "docs/logo.svg": "<svg><!-- Astra --></svg>\n"}
    found, _ = _t18(files)
    assert found == [_w(".env.example", 1, "round-id"), _w(".gitignore", 2, "owner-name"),
                     _w("copilot/web/s.css", 1, "release-label/round-id"), _w("copilot/web/s.js", 1, "gate-name"),
                     _w("copilot/web/t.html", 2, "item-label"), _w("copilot/web/t.html", 3, "role-word"),
                     _w("docs/N.md", 1, "role-word"), _w("docs/NOTE.txt", 1, "owner-name"),
                     _w("docs/logo.svg", 1, "gate-name"), _w("pytest.ini", 2, "round-id"), _w("render.yaml", 1, "role-word"),
                     _w("tests/f/x.json", 1, "round-id"), _w("tools/evidence_binaries.sha256", 1, "owner-name"),
                     _w("tools/pre-commit", 2, "role-word")]


def test_t18_does_not_read_data_fixtures():
    files = {"pilot/fixtures/x.json": '{"a": "<!-- R1 B9 -->"}\n', "copilot/fixtures/m1/a.txt": "supervisor\n",
             "pilot/eval/cases/c.json": '<!-- Astra -->\n', "pilot/eval/draft_cases/d.json": '<!-- Astra -->\n'}
    assert _t18(files)[0] == []


def test_t18_keeps_ev_ids_resolvable_decision_headings_and_a_documents_own_labels():
    decisions = "## D-2099-01-01-01: Default model\nA body that mentions D-2099-01-01-02 without a heading.\n"
    files = {"docs/DECISIONS.md": decisions,
             "pilot/a.py": "# EV-12 shows it (D-2099-01-01-01)\n# but D-2099-01-01-02 has no heading\n",
             "docs/CONFORMANCE.md": "| L1 | the first source row | 1.12 |\nSee S3 of the guide and the RC4.2 release.\n"}
    assert _t18(files)[0] == [_w("docs/DECISIONS.md", 2, "decision-id"), _w("pilot/a.py", 2, "decision-id")]     # a body mention is not a heading


def test_t18_reads_the_exporter_and_its_test_too_but_exempts_only_their_real_rule_names_and_skips_the_index():
    own = {p: "# T15, X3, R9 and T18 are rule names\n# T19 and H1 are not rule names\n# supervisor G-SH-7\n"
           for p in ("tools/export_public.py", "tools/test_export_public.py")}
    found, _ = _t18({**own, ep.INDEX_NAME: "# supervisor G-SH-7\n"})
    assert found == [_w("tools/export_public.py", 2, "item-label"), _w("tools/export_public.py", 3, "role-word/round-id"),
                     _w("tools/test_export_public.py", 2, "item-label"), _w("tools/test_export_public.py", 3, "role-word/round-id")]
    assert ep.T18_SKIP == () and set(ep.T18_OWN_VOCAB) == set(ep.T15_SKIP)
    assert all(ep.T18_OWN_RULES.fullmatch(f"{k}{n}") for k, rng in (("T", range(1, 19)), ("X", range(1, 9)), ("R", range(1, 10)))
               for n in rng)
    assert not ep.T18_OWN_RULES.search("T19 X9 R10 H1 T0")


def test_t18_a_private_commit_id_in_a_non_markdown_file_is_a_warning_only_when_it_resolves(tmp_path):
    repo, sha = make_repo(tmp_path, {})
    files = {"copilot/a.py": f"# the fix of {sha}\n# and {sha[:9]} in short\n# a hash cafe1234bd that is not a commit\n",
             "docs/N.md": f"{sha[:10]}\n"}
    found, _ = _t18(files, repo=repo)
    assert found == [_w("copilot/a.py", 1, "commit-id"), _w("copilot/a.py", 2, "commit-id")]        # .md is T14's


def test_t18_allowlist_entries_by_line_literal_and_start_silence_a_hit_and_an_unused_one_is_stale(monkeypatch):
    files = {"tests/f/frozen.html": "<p>x</p>\n<!-- R1 B9: GET-only -->\n",
             "docs/AI-USAGE.md": "- **Opus** sessions acted as supervisor/reviewer: wrote frozen briefs\nand the Opus note\n"}
    assert len(_t18(files, allow=[], monkeypatch=monkeypatch)[0]) == 3
    allow = [{"path": "tests/f/frozen.html", "family": "round-id", "line": 2, "reason": "frozen bytes"},
             {"path": "docs/AI-USAGE.md", "family": "role-word", "starts": "- **Opus** sessions acted as supervisor", "reason": "disclosure"},
             {"path": "docs/AI-USAGE.md", "family": "role-word", "literal": "never matches", "reason": "unused"}]
    found, used = _t18(files, allow=allow, monkeypatch=monkeypatch)
    assert found == [_w("docs/AI-USAGE.md", 2, "role-word")] and sorted(set(used)) == [0, 1]       # a start entry covers its own line only
    assert ep.stale_t18(used) == ["stale T18 allowlist entry: docs/AI-USAGE.md never matches matched nothing"]
    moved = {"tests/f/frozen.html": "<p>x</p>\n<p>y</p>\n<!-- R1 B9: GET-only -->\n", "docs/AI-USAGE.md": "ok\n"}
    found, used = _t18(moved, allow=allow, monkeypatch=monkeypatch)
    assert len(found) == 1 and used == []                                  # a moved line is a warning again


def test_t18_an_allowlist_entry_for_a_file_the_rules_exclude_is_ignored_and_never_stale(monkeypatch):
    monkeypatch.setattr(ep, "T18_ALLOWLIST", [{"path": "internal/x.md", "family": "role-word", "literal": "x", "reason": "r"},
                                              {"path": "docs/AI-USAGE.md", "family": "role-word", "literal": "y", "reason": "r"}])
    assert [e["path"] for e in ep.effective_t18()] == ["docs/AI-USAGE.md"]
    assert ep.stale_t18([]) == ["stale T18 allowlist entry: docs/AI-USAGE.md y matched nothing"]


def test_t18_every_allowlist_entry_of_the_real_table_has_a_reason_a_family_and_a_way_to_find_its_line():
    assert ep.T18_ALLOWLIST
    for entry in ep.T18_ALLOWLIST:
        assert entry["reason"].strip() and entry["path"] and entry["family"] not in ("", "*")
        assert isinstance(entry.get("line"), int) or entry.get("literal") or entry.get("starts")
    frozen = {e["path"] for e in ep.T18_ALLOWLIST if e["reason"] == ep.T18_FROZEN_REASON}
    assert len(frozen) == 6 and all("fixtures/" in p for p in frozen)
    assert "tests/features/deadlines/fixtures/disputes_list-baseline-0.html" not in frozen     # not pinned by any test


def test_t18_a_dry_export_reports_the_hit_as_a_warning_path_and_line_never_the_text(tmp_path):
    repo, sha = make_repo(tmp_path, {"copilot/note.py": "x = 1\n# a build id G-SH-7 sits here\n"})
    dest = tmp_path / "out"
    assert run(repo, dest, sha, terms_file(tmp_path))[0] == 0                          # a warning, not a block
    text = report(tmp_path)
    assert "warning: copilot/note.py:2 round-id in a comment, docstring or prose (T18)" in text
    assert "G-SH-7" not in text


def test_t15_old_docs_paths_listed_in_the_path_map_are_warnings_even_though_the_files_no_longer_exist(tmp_path):
    path_map = ("# internal/ path map\n| Old path | New path |\n|---|---|\n"
                "| `docs/ENCARGO-OLD-BRIEF.md` | `internal/briefs/ENCARGO-OLD-BRIEF.md` |\n"
                "| `docs/bitacoras/construccion.md` | `internal/logs/construccion.md` |\n"
                "| `docs/DECISIONS.md` | `internal/process/DECISIONS.md` |\n")
    readme = ("# T\nThe rule is in docs/ENCARGO-OLD-BRIEF.md.\nSee docs/bitacoras/construccion.md and docs/DECISIONS.md.\n")
    repo, sha = make_repo(tmp_path, {"README.md": readme, "internal/PATH-MAP.md": path_map, "docs/DECISIONS.md": "d\n"})
    assert ep.old_docs_paths(repo, sha) == ["docs/ENCARGO-OLD-BRIEF.md", "docs/bitacoras/construccion.md"]
    assert run(repo, tmp_path / "out", sha, terms_file(tmp_path))[0] == 0
    text = report(tmp_path)
    assert "warning: README.md:2 names excluded file docs/ENCARGO-OLD-BRIEF.md (T15)" in text
    assert "warning: README.md:3 names excluded file docs/bitacoras/construccion.md (T15)" in text
    assert "docs/DECISIONS.md (T15)" not in text                                       # the public copy keeps its path
    bare, bare_sha = make_repo(tmp_path, {"README.md": readme}, name="bare")
    assert ep.old_docs_paths(bare, bare_sha) == []                                     # no map in the commit: nothing added


def test_mcp_protocol_tests_are_published_with_mcp_local_contracts(monkeypatch):
    probe = "tests/features/mcp_local_protocol/test_stdio.py"
    monkeypatch.setattr(ep, "PUBLISHED_NAMESPACES", ["mcp_local_contracts"])
    assert ep.classify(probe)[0] == "copied" and ep.classify("tests/features/mcp_local_protocol/fake_server.py")[0] == "copied"
    assert ep.classify("tests/features/mcp_local_protocolx/test_stdio.py")[0] == "excluded"
    assert ep.classify("internal/lanes/tests/features/mcp_local_protocol/test_stdio.py")[0] == "excluded"
    monkeypatch.setattr(ep, "PUBLISHED_NAMESPACES", ["claims"])
    assert ep.classify(probe)[0] == "excluded"       # without its namespace the tests stay private


def test_nivel2_namespaces_are_published_with_their_templates_tests_and_flags(monkeypatch):
    import ast
    tree = ast.parse(Path(ep.__file__).read_text(encoding="utf-8"))
    selection = next(ast.literal_eval(n.value) for n in tree.body
                     if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", None) == "PUBLISHED_NAMESPACES")
    assert _NIVEL2_NAMESPACES <= set(selection) and set(selection) == set(ep.KNOWN_NAMESPACES)
    # every feature package in the source tree is published: a new one must be added on purpose
    on_disk = {p.name for p in (ROOT / "copilot/features").iterdir() if (p / "__init__.py").is_file()}
    assert on_disk <= set(selection), sorted(on_disk - set(selection))
    monkeypatch.setattr(ep, "PUBLISHED_NAMESPACES", list(selection))
    for name in sorted(_NIVEL2_NAMESPACES):
        for root in ("copilot/features/", "tests/features/"):
            if (ROOT / root / name).is_dir():
                assert ep.classify(root + name + "/probe.py")[0] == "copied", (root, name)
        if (ROOT / "copilot/web/templates/features" / name).is_dir():
            assert ep.classify("copilot/web/templates/features/" + name + "/probe.html")[0] == "copied", name
    published = {path.relative_to(ROOT).as_posix()
                 for root in ("copilot/features", "tests/features")
                 for path in (ROOT / root).rglob("*.py")
                 if ep.classify(path.relative_to(ROOT).as_posix())[0] == "copied"}
    published.add("copilot/core/disputes.py")     # the real export publishes the core code that reads the re-arm setting
    template = ep.published_env_template((ROOT / ".env.example").read_bytes(), published)
    assert template.splitlines() == (ROOT / ".env.example").read_bytes().splitlines()      # nothing is dropped
    rows = re.findall(rb"^COPILOT_FEATURE_([A-Z_]+)=(true|false)\s*$", template, re.M)
    assert len(rows) == len(dict(rows)) == 75 and sum(v == b"true" for _, v in rows) == 72      # 69 rows (the dated-path flag of account_cliff is the 67th, the seeded-demo flag the 68th, the inbox-watch flag the 69th); before them: 66 rows (the dead mock_adjudicator row is gone): O4 adds case_reentry and six flags of published namespaces, all on; the appeal call is on too
    assert {k for k, v in rows if v == b"false"} == {b"DEMO_DOCUMENTS", b"WEBHOOKS",
                                                      b"WEBHOOKS_VERIFY"}
