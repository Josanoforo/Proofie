"""Secret scanner run before every commit. Checks only files staged in git.

Usage: python tools/scan_staged.py            (run from the repo root; exits 0 = clean, 1 = BLOCKED -
                                                secret found OR the scan itself failed to complete,
                                                2 = FORBIDDEN PATH staged, checked before the content scan)
Hook: .git/hooks/pre-commit calls this with the project's .venv Python. To skip once: `git commit --no-verify`
(use only with the owner's explicit approval). To remove permanently: delete
`.git/hooks/pre-commit`.

Never prints a matched value: only the file, line number, and rule name. Two independent checks:
1. Generic patterns (private keys, AWS-style ids, bearer tokens, generic "secret/password/token = ...").
2. Literal values currently in `.env` (the sandbox client id/secret) appearing anywhere in staged content,
   so a credential that leaked into code/evidence/docs is caught even if no generic pattern matches it.

Forbidden paths: before the content scan, a staged path that may never be committed
(`.env`, `.env.*` except `.env.example`, `evidence/`, `reference/`, `scratch-ver/`) is reported as
`FORBIDDEN PATH <path>` and the script exits 2. `evidence/manual/` is the one tracked exception (hand-collected
documents, new binaries hash-gated below); the evidence-binary rule judges it instead. `--skip-path-check` exists
only so the tests of the content rules can stage files under evidence/ in a throwaway repo; no hook passes it.

Fail-closed: any error while scanning - a staged file that can't be read, `git diff` itself
failing, an unexpected exception - BLOCKS the commit exactly like a detected secret. A scanner that silently
skips what it cannot read is not a scanner the hook can be trusted to rely on.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # so `redact` imports however this file is launched
from personal_data_patterns import IDENTIFIER_KEYS  # noqa: E402  the identifier kinds; the patterns come from redact
from redact import DETECT_ONLY_PATTERNS, PATTERNS as REDACT_PATTERNS, _email_is_safe  # noqa: E402  single sources

ROOT = Path.cwd()  # git runs hooks with cwd = repo root; tests override this per temp repo

# (name, compiled pattern). Patterns look for the SHAPE of a secret, not any specific value.
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("generic_bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9\-_.]{20,}\b")),
    ("assignment_looking_like_secret",
     re.compile(r"(?i)\b(secret|password|passwd|api[_-]?key|access[_-]?token|client[_-]?secret)\b\s*[:=]\s*"
                r"['\"]?[A-Za-z0-9/+_\-.]{12,}['\"]?")),
    ("paypal_sandbox_bearer_prefix", re.compile(r"\bA21[A-Za-z0-9_-]{20,}\b")),  # PayPal sandbox token shape
]

BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".pyc", ".ico"}
# This file's own test fixtures contain fake secrets on purpose (to exercise the patterns above) and would
# otherwise block their own commit. Keep this list to exactly that: never add a file with real content here.
ALLOWLIST = {"tools/test_scan_staged.py"}

# The PayPal live API host is blocked only in the folders that ship or record app behaviour. Elsewhere
# (docs stating the prohibition, the editor's deny rules, tests proving the app rejects it) it is allowed.
LIVE_HOST_DIRS = ("evidence/", "pilot/", "copilot/", "demo/")
LIVE_HOST_RULE = "paypal_live_host"
LIVE_HOST_PATTERN = dict(DETECT_ONLY_PATTERNS)[LIVE_HOST_RULE]

# personal-data VALUES (an identifier key carrying a value, or a real-looking e-mail) are
# blocked in the folders that hold recorded or fixture data. The patterns are redact.py's own objects, never copies.
# The sanitiser placeholder, empty values and the sandbox e-mail domains are exempt inside those patterns. The whole
# staged file is scanned, except an EXISTING file under evidence/ (append-only): there only the lines the commit adds
# are judged, so old lines with values do not block a clean append.
PERSONAL_DATA_DIRS = ("evidence/", "pilot/fixtures/", "copilot/fixtures/")
PERSONAL_DATA_PATTERNS = [(kind, pat) for kind, pat in DETECT_ONLY_PATTERNS if kind in IDENTIFIER_KEYS]
EMAIL_PATTERN = dict(REDACT_PATTERNS)["email"]
EMAIL_RULE = "real_looking_email"

# New binaries under evidence/ need their sha256 in this file, which the owner maintains (a missing file blocks).
EVIDENCE_BINARIES_FILE = Path(__file__).resolve().parent / "evidence_binaries.sha256"
EVIDENCE_BINARY_DIR = "evidence/"
BINARY_RULE = "new_evidence_binary"


# Paths that may never be committed from any worktree (the hook lives in the common .git/hooks).
FORBIDDEN_PREFIXES = ("evidence/", "reference/", "scratch-ver/")
FORBIDDEN_ALLOWED_PREFIXES = ("evidence/manual/",)  # tracked since 2026-10-05; new binaries are hash-gated
FORBIDDEN_EXIT = 2


def forbidden_paths(files: list[str]) -> list[str]:
    """The staged paths (as given) that may never be committed. A pure function of the path text."""
    bad = []
    for rel in files:
        norm = rel.replace("\\", "/").removeprefix("./").lower()
        base = norm.rsplit("/", 1)[-1]
        is_env = base == ".env" or (base.startswith(".env.") and base != ".env.example")
        in_dir = norm.startswith(FORBIDDEN_PREFIXES) and not norm.startswith(FORBIDDEN_ALLOWED_PREFIXES)
        if is_env or in_dir:
            bad.append(rel)
    return bad


def staged_files() -> list[str]:
    # --no-renames: git detects renames by default and lists a moved file as R, which --diff-filter=ACM drops, so a
    # moved (and possibly edited) file was never scanned. Without rename detection a move is an added file at
    # its new path: whole-file checks, and new under evidence/ (the strict reading).
    out = subprocess.run(["git", "diff", "--cached", "--name-only", "--no-renames", "--diff-filter=ACM"],
                         cwd=ROOT, capture_output=True, text=True, check=True)
    return [l for l in out.stdout.splitlines() if l.strip()]


# The sandbox app credentials that must never reach a commit: the seller app's file and the buyer-side app's file (Round 3).
# Both stay out of git (forbidden_paths refuses any .env.* name); this list adds their VALUES to the literal check.
ENV_SECRET_FILES = (".env", ".env.buyer")
ENV_SECRET_KEYS = ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET")


def env_secret_values() -> list[str]:
    """Literal values of PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET from each file of ENV_SECRET_FILES that exists, read only for
    comparison. A missing file adds nothing; a file that exists but cannot be read raises, which blocks the commit (fail closed)."""
    values = []
    for name in ENV_SECRET_FILES:
        env_path = ROOT / name
        if not env_path.exists():
            continue
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            if key.strip() in ENV_SECRET_KEYS and len(val.strip().strip("'\"")) >= 8:
                values.append(val.strip().strip("'\""))
    return values


def live_host_scope(rel: str) -> bool:
    """True when the live-host rule applies to this repo-relative path (one of LIVE_HOST_DIRS)."""
    return rel.replace("\\", "/").removeprefix("./").lower().startswith(LIVE_HOST_DIRS)


def personal_data_scope(rel: str) -> bool:
    """True when the personal-data rule applies to this repo-relative path (one of PERSONAL_DATA_DIRS)."""
    return rel.replace("\\", "/").removeprefix("./").lower().startswith(PERSONAL_DATA_DIRS)


def evidence_binary_scope(rel: str) -> bool:
    return rel.replace("\\", "/").removeprefix("./").lower().startswith(EVIDENCE_BINARY_DIR)


def allowed_binary_hashes() -> set[str]:
    """sha256 values (lower case) listed in tools/evidence_binaries.sha256. A missing or unreadable file gives an
    empty set, which blocks every binary under evidence/ (fail-closed)."""
    try:
        text = EVIDENCE_BINARIES_FILE.read_text(encoding="utf-8")
    except OSError:
        return set()
    hashes = set()
    for line in text.splitlines():
        token = line.split("#", 1)[0].split(None, 1)[:1]
        if token and re.fullmatch(r"[0-9a-fA-F]{64}", token[0]):
            hashes.add(token[0].lower())
    return hashes


def staged_blob(rel: str) -> bytes:
    """The staged bytes of a file (what would be committed), not the working-tree copy."""
    return subprocess.run(["git", "show", f":{rel}"], cwd=ROOT, capture_output=True, check=True).stdout


def is_new_in_commit(rel: str) -> bool:
    """True when the file does not exist in HEAD (a brand new file, or the very first commit)."""
    return subprocess.run(["git", "cat-file", "-e", f"HEAD:{rel}"], cwd=ROOT, capture_output=True).returncode != 0


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def added_lines(rel: str) -> list[tuple[int, str]]:
    """[(line_number_in_the_new_file, text)] of the lines this commit ADDS to `rel` (`git diff --cached -U0`)."""
    out = subprocess.run(["git", "diff", "--cached", "-U0", "--no-color", "--no-ext-diff", "--", rel],
                         cwd=ROOT, capture_output=True, check=True).stdout.decode("utf-8", errors="replace")
    added: list[tuple[int, str]] = []
    number = 0
    for line in out.splitlines():
        hunk = _HUNK.match(line)
        if hunk:
            number = int(hunk.group(1))
        elif line.startswith("+") and not line.startswith("+++"):
            added.append((number, line[1:]))
            number += 1
    return added


def is_binary(rel: str, data: bytes) -> bool:
    return Path(rel).suffix.lower() in BINARY_EXT or b"\0" in data[:8192]


def scan_text(text: str, env_values: list[str], check_live_host: bool = False,
              check_personal_data: bool = False) -> list[tuple[int, str]]:
    """Return [(line_no, rule_name)] without ever returning the matched substring."""
    hits: list[tuple[int, str]] = []
    for i, line in enumerate(text.splitlines(), start=1):
        for name, pat in PATTERNS:
            if pat.search(line):
                hits.append((i, name))
        if check_live_host and LIVE_HOST_PATTERN.search(line):
            hits.append((i, LIVE_HOST_RULE))
        if check_personal_data:
            for kind, pat in PERSONAL_DATA_PATTERNS:
                if pat.search(line):
                    hits.append((i, kind))
            if any(not _email_is_safe(m) for m in EMAIL_PATTERN.finditer(line)):
                hits.append((i, EMAIL_RULE))
        for val in env_values:
            if val and val in line:
                hits.append((i, "env_credential_literal"))
    return hits


def _scan(files: list[str]) -> list[str]:
    """Return findings (secrets or scan failures), one string each. Never raises for a per-file problem:
    a failure to scan a file is itself appended as a blocking finding (fail-closed)."""
    env_values = env_secret_values()
    findings: list[str] = []
    for rel in files:
        if rel.replace("\\", "/") in ALLOWLIST:
            continue
        path = ROOT / rel
        if evidence_binary_scope(rel):
            # A binary under evidence/ is never regex-scanned, so it must be on the allow-list by hash.
            try:
                data = staged_blob(rel)
                if is_binary(rel, data) and hashlib.sha256(data).hexdigest() not in allowed_binary_hashes():
                    findings.append(f"{rel}: BLOCKED, new binary under evidence/ whose sha256 is not in "
                                    f"tools/evidence_binaries.sha256 ({BINARY_RULE})")
                    continue
            except Exception as exc:  # cannot read the staged blob: never skip silently
                findings.append(f"{rel}: BLOCKED, could not check this staged file ({type(exc).__name__}, fail-closed)")
                continue
        if path.suffix.lower() in BINARY_EXT:
            continue  # intentionally not scanned (no text patterns expected here)
        if not path.is_file():
            findings.append(f"{rel}: BLOCKED, staged but not found as a regular file on disk (fail-closed)")
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
            personal = personal_data_scope(rel)
            # Evidence is append-only, so an existing evidence file is judged on the lines this commit ADDS;
            # a new evidence file, and every other scoped path, is judged whole.
            added_only = personal and evidence_binary_scope(rel) and not is_new_in_commit(rel)
            hits = scan_text(text, env_values, check_live_host=live_host_scope(rel),
                             check_personal_data=personal and not added_only)
            if added_only:
                for number, line in added_lines(rel):
                    hits.extend((number, rule) for _, rule in scan_text(line, [], check_personal_data=True)
                                if rule in IDENTIFIER_KEYS or rule == EMAIL_RULE)
        except Exception as exc:  # unreadable, permission error, anything else: never skip silently
            findings.append(f"{rel}: BLOCKED, could not scan this staged file "
                            f"({type(exc).__name__}, fail-closed)")
            continue
        for line_no, rule in hits:
            if rule == LIVE_HOST_RULE:
                findings.append(f"{rel}:{line_no}: PayPal live host ({rule})")
            elif rule in IDENTIFIER_KEYS or rule == EMAIL_RULE:
                findings.append(f"{rel}:{line_no}: possible personal data ({rule})")
            else:
                findings.append(f"{rel}:{line_no}: possible secret ({rule})")
    return findings


def main(argv: list[str]) -> int:
    try:
        files = staged_files()
        forbidden = [] if "--skip-path-check" in argv else forbidden_paths(files)
        if forbidden:
            for rel in forbidden:
                print(f"FORBIDDEN PATH {rel}", file=sys.stderr)
            print(f"scan_staged: BLOCKED, {len(forbidden)} forbidden path(s) staged (exit {FORBIDDEN_EXIT}); "
                  "unstage them", file=sys.stderr)
            return FORBIDDEN_EXIT
        findings = _scan(files)
    except Exception as exc:  # git missing, permission denied, anything unexpected: fail-closed, not exit 0
        print(f"scan_staged: BLOCKED, the scan itself failed to complete ({type(exc).__name__}: {exc})",
              file=sys.stderr)
        return 1
    if findings:
        print("scan_staged: BLOCKED, possible secret(s) or scan failure(s) in staged files "
              "(values not shown):", file=sys.stderr)
        for f in findings:
            print(f"  {f}", file=sys.stderr)
        return 1
    print(f"scan_staged: clean ({len(files)} staged file(s) checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
