"""Static host test: every hostname written as a string in the tracked code is the sandbox host,
the local machine, or a documented documentation link. Prints the full table (file:line -> host) so
`pytest -s` output is the evidence. Hosts only; no request is made."""
import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

ROOT = Path(__file__).resolve().parents[2]
ROOTS = ("copilot", "pilot", "tools", "demo")
ALLOWED = {"api-m.sandbox.paypal.com", "127.0.0.1", "localhost"}
HOST_KEYWORDS = {"host", "base_url", "url"}
LIVE_HOST = "api-m" + ".paypal.com"

DOC_REASON = "documentation link, never requested"
# (file, host) -> reason. Each entry documents a host that is written in the code but never contacted.
DOC_URLS = {
    ("copilot/core/carriers.py", "developer.paypal.com"): DOC_REASON,
    ("copilot/core/context.py", "developer.paypal.com"): DOC_REASON,
    ("copilot/web/links.py", "www.sandbox.paypal.com"): DOC_REASON,
    ("tools/check_pending.py", "help.devpost.com"): DOC_REASON,
    ("copilot/features/outcome/appeal.py", "developer.paypal.com"): DOC_REASON,
    ("copilot/features/conformance/pins.py", "developer.paypal.com"): DOC_REASON,
    ("copilot/web/inbox_view.py", "www.paypal.com"): DOC_REASON,    # The PayPal Help HELP1083 link
    ("copilot/features/first_dispute_guide/service.py", "www.gob.mx"): DOC_REASON,
    ("copilot/features/first_dispute_guide/service.py", "www.usa.gov"): DOC_REASON,
    ("copilot/features/judge_as_buyer/service.py", "www.sandbox.paypal.com"): "human navigation link, never requested by the app",
}
PAYLOAD_REASON = "URL sent as data inside a PayPal request body, never requested by us"
PAYLOAD_HOSTS = {
    ("pilot/step2_order.py", "example.com"): PAYLOAD_REASON,
    ("tools/sandbox_orders.py", "example.com"): PAYLOAD_REASON,
    ("tools/sandbox_seed.py", "example.com"): PAYLOAD_REASON,  # same order body, inlined (one-file script)
    ("tools/sandbox_card.py", "example.com"): PAYLOAD_REASON,  # the card order body names a 3DS return page
}
# Scanner rule or fixture that must name the live host to detect it.
LIVE_HOST_FILES = {
    "tools/hooks/guard.py", "tools/hooks/test_guard.py", "tools/export_public.py",
    "tools/test_export_public.py", "tools/test_redact.py", "tools/test_scan_staged.py",
    "tools/auto_run.py",   # a refusal check: the harness refuses any base URL but the sandbox; excluded from the public kit by tools/export_public.py
    "tests/features/judge_pool/test_judge_pool.py",   # a refusal test: a link to the live host is not usable, so the test names it
    "tools/test_sandbox_dispute_state.py",   # a refusal test: the state helper refuses any host but the sandbox, so the test names the live ones
}

_URL_HOST = re.compile(r"://([^/\s:'\"?#\\{}]+)")
_BARE_HOST = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+(:\d+)?$")


def _actual_py(root):
    """Only the four source roots; never follow links or enter runtime/scratch directories."""
    paths = []
    for name in ROOTS:
        folder = root / name
        assert folder.is_dir() and not folder.is_symlink(), f"Missing source root: {name}"
        for directory, children, files in os.walk(folder, followlinks=False):
            children[:] = sorted(child for child in children if not child.startswith((".", "scratch-"))
                                 and child not in {"venv", "data", "__pycache__"})
            assert not any((Path(directory) / child).is_symlink() for child in children), "Linked source directory"
            for file in sorted(files):
                if file.endswith(".py"):
                    path = Path(directory) / file
                    assert not path.is_symlink(), "Linked Python source"
                    paths.append(path.relative_to(root).as_posix())
    return sorted(paths)


def tracked_py(root=None):
    root = ROOT if root is None else root
    manifest = root / "MANIFEST.json"
    if manifest.is_file():
        inventory = json.loads(manifest.read_text(encoding="utf-8"))
        assert inventory.get("schema") == "proofie-export/1", "Invalid export manifest schema"
        assert isinstance(inventory.get("files"), list), "Missing manifest source inventory"
        paths, seen = [], set()
        for entry in inventory["files"]:
            path = entry["path"]
            parts = PurePosixPath(path).parts
            assert (parts and path == PurePosixPath(path).as_posix() and not PurePosixPath(path).is_absolute()
                    and ".." not in parts and "\\" not in path and ":" not in path), "Unsafe manifest path"
            assert path not in seen, "Duplicate manifest source"
            seen.add(path)
            if parts[0] in ROOTS and path.endswith(".py"):
                paths.append(path)
        assert {PurePosixPath(path).parts[0] for path in paths} == set(ROOTS), "Incomplete source roots in manifest"
    else:
        assert (root / ".git").exists(), "Host inventory requires Git or a valid export manifest"
        out = subprocess.run(["git", "ls-files", "-z", "--", *ROOTS], cwd=root,
                             capture_output=True, check=True).stdout.decode("utf-8")
        paths = [path for path in out.split("\0") if path.endswith(".py")]
    assert len(paths) == len(set(paths)), "Duplicate source inventory"
    assert sorted(paths) == _actual_py(root), "Source inventory differs from actual Python files"
    return sorted(paths)


def is_test_path(rel):
    name = rel.rsplit("/", 1)[-1]
    return name.startswith("test_") or name == "conftest.py" or "/tests/" in "/" + rel


def _fold(node):
    """Constant-fold 'a' + 'b' chains so a host built from pieces is seen whole."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _fold(node.left), _fold(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def strings(tree):
    """Yield (line, text, via_keyword) for every string constant, folded concatenations and keyword values."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value, False
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            folded = _fold(node)
            if folded is not None:
                yield node.lineno, folded, False
        elif isinstance(node, ast.keyword) and node.arg in HOST_KEYWORDS:
            value = _fold(node.value)
            if value is not None:
                yield node.value.lineno, value, True


def hosts_in(text, via_keyword):
    found = [m.group(1).lower() for m in _URL_HOST.finditer(text)]
    if via_keyword and "://" not in text and (_BARE_HOST.match(text) or text == "localhost"):
        found.append(text.split(":")[0].lower())
    return found


def collect():
    rows = []
    for rel in tracked_py():
        if is_test_path(rel):
            continue
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"), filename=rel)
        for line, text, via_kw in strings(tree):
            for host in hosts_in(text, via_kw):
                rows.append((rel, line, host))
    return sorted(set(rows))


def allowed_by(rel, host):
    if host in ALLOWED:
        return "ALLOWED"
    if (rel, host) in DOC_URLS:
        return "DOC_URLS"
    if (rel, host) in PAYLOAD_HOSTS:
        return "PAYLOAD_HOSTS"
    return "UNLISTED"


def test_every_host_is_allowed_or_documented(capsys):
    rows = collect()
    with capsys.disabled():
        print("\nHOST TABLE (file:line -> host)")
        for rel, line, host in rows:
            status = allowed_by(rel, host)
            print(f"{rel}:{line} -> {host} [{status}]")
        print(f"distinct hosts: {sorted({h for _, _, h in rows})}")
    bad = [(rel, line, host) for rel, line, host in rows if allowed_by(rel, host) == "UNLISTED"]
    assert not bad, "unlisted hosts: " + "; ".join(f"{r}:{l} -> {h}" for r, l, h in bad)


def test_live_host_only_in_listed_files():
    hits = []
    for rel in tracked_py():
        if rel in LIVE_HOST_FILES:
            continue
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"), filename=rel)
        for line, text, _ in strings(tree):
            if LIVE_HOST in text.lower():
                hits.append(f"{rel}:{line}")
    assert not hits, "live host written outside the listed files: " + "; ".join(sorted(set(hits)))


def _export_fixture(root):
    files = []
    for name in ROOTS:
        path = root / name / "sample.py"
        path.parent.mkdir(parents=True)
        path.write_text("VALUE = 1\n", encoding="utf-8")
        files.append({"path": path.relative_to(root).as_posix()})
    inventory = {"schema": "proofie-export/1", "files": files}
    (root / "MANIFEST.json").write_text(json.dumps(inventory), encoding="utf-8")
    return inventory


@pytest.mark.parametrize("damage", [None, "missing-file", "omitted-file", "duplicate", "unsafe-path", "bad-schema"])
def test_export_source_inventory_matches_actual_paths_and_rejects_corruption(tmp_path, damage):
    inventory = _export_fixture(tmp_path)
    if damage == "missing-file":
        (tmp_path / inventory["files"][0]["path"]).unlink()
    elif damage == "omitted-file":
        inventory["files"].pop()
    elif damage == "duplicate":
        inventory["files"].append(inventory["files"][0])
    elif damage == "unsafe-path":
        inventory["files"][0]["path"] = "../outside.py"
    elif damage == "bad-schema":
        inventory["schema"] = "invalid"
    (tmp_path / "MANIFEST.json").write_text(json.dumps(inventory), encoding="utf-8")
    if damage is None:
        # Unlisted runtime files must never enter the source inventory.
        for name in (".venv", f"scratch-{tmp_path.name}", "data"):
            path = tmp_path / "tools" / name / "ignored.py"
            path.parent.mkdir(parents=True)
            path.write_text("VALUE = 2\n", encoding="utf-8")
        assert tracked_py(tmp_path) == sorted(entry["path"] for entry in inventory["files"])
    else:
        with pytest.raises(AssertionError):
            tracked_py(tmp_path)


@pytest.mark.parametrize("host", ["forbidden.example.invalid", LIVE_HOST], ids=["unlisted-host", "live-host"])
def test_original_host_assertions_reject_forbidden_scratch_source(tmp_path, monkeypatch, capsys, host):
    _export_fixture(tmp_path)
    (tmp_path / "copilot" / "sample.py").write_text(f'URL = "https://{host}/"\n', encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    with pytest.raises(AssertionError, match="unlisted hosts"):
        test_every_host_is_allowed_or_documented(capsys)
    if host == LIVE_HOST:
        with pytest.raises(AssertionError, match="live host written outside"):
            test_live_host_only_in_listed_files()
