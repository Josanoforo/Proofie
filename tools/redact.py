"""Privacy redaction helper and tree scanner.

Detects and replaces sensitive values: client id/secret-like values, OAuth access tokens (A21AA...),
Basic/Bearer headers, emails and street-address lines.

Library:  redact_text(text) -> text with sensitive values replaced by [REDACTED]; newline count preserved.
          scan_text(text) -> [(line_number, pattern_type), ...]  (also flags the PayPal live host, detection only)
CLI:      python tools/redact.py [PATH ...] [--tracked]
          Scans files under PATH (default: current dir) or, with --tracked, `git ls-files`.
          Prints `file:line type` per hit and a count summary. NEVER prints a matched value.
          Exit 0 = no hits, 1 = hits, 2 = scan error.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # so the shared module imports however this file is launched
from personal_data_patterns import SAFE_SANDBOX_EMAIL_DOMAINS, VALUE_PATTERNS  # noqa: E402

EXCLUDED_DIR_NAMES = {"private", "traces", "trace", "exports", "export", "snapshots", ".git", ".aws", ".ssh",
                      ".venv", "__pycache__", "node_modules"}
PRIVATE_NAMES = (".env", "trace.zip", "room.json", "full-session", "original-export", "passwords.", "auth.json",
                 "credentials")
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".pyc", ".ico", ".mp4", ".webm", ".woff", ".woff2"}
SAFE_EMAIL_DOMAINS = {"example.com", "example.org", "example.net", "anthropic.com", "invalid", "test", "localhost",
                      *SAFE_SANDBOX_EMAIL_DOMAINS}

_KEYS = (r"access[_-]?token|refresh[_-]?token|session[_-]?token|api[_-]?key|client[_-]?secret|client[_-]?id|"
         r"password|passwd|authorization|cookie|set-cookie|token|secret|credentials")

# (type, compiled pattern). Patterns describe the SHAPE of sensitive data, never a specific value.
# auth_header's minimum length raised from 12 to 20 (a real Basic/Bearer value - base64/JWT - is never
# as short as a single English word; this was the fix for a real false positive - "Basic Requirements"
# in copilot/rules/us_seller_protection_2026-01-26.json's own prose, a policy section name, where
# "Requirements" alone is exactly 12 word characters after "Basic ". A digit-required lookahead was tried
# first and rejected: real letters-only base64 (no digit at all) is common and legitimate, so it would
# have under-caught instead).
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("oauth_access_token", re.compile(r"\bA21AA[A-Za-z0-9_-]{20,}")),
    ("auth_header", re.compile(r"(?i)\b(?:Basic|Bearer)\s+[A-Za-z0-9+/=._~-]{20,}")),
    ("credential_assignment", re.compile(
        r"""(?i)\b(?:%s)\b["']?\s*[:=]\s*["']?[A-Za-z0-9+/_.\-]{16,}""" % _KEYS)),
    ("client_id_like", re.compile(r"\b[AE][A-Za-z0-9_-]{70,90}\b")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b")),
    ("street_address", re.compile(
        r"(?i)\b\d{1,6}\s+(?:[A-Za-z0-9.']+\s+){1,4}"
        r"(?:street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|way|court|ct|calle|avenida|calzada)\b"
        r"|\b(?:calle|avenida|av\.|paseo|calzada|blvd\.?)\s+[\wáéíóúñÁÉÍÓÚÑ.]+(?:\s+[\wáéíóúñÁÉÍÓÚÑ.]+){0,3}"
        r"\s+(?:no\.?\s*|#\s*)?\d{1,6}\b")),
]

# Detection-only rules: scan_text reports them but redact_text never rewrites them. The PayPal live API host must stay
# visible in sanitized evidence (redacting it would hide a live reference). The host is assembled from pieces, same
# convention as the internal guard hook, so this file does not contain the literal host. Sandbox, www and developer
# hosts do not match: after the first label the pattern requires the `.paypal.com` suffix directly.
_LIVE_HOST_LABELS = ["api-m", "api"]
_LIVE_HOST_SUFFIX = ".paypal" + ".com"
# a JSON key `account_number` that carries a value (a string or a number of 4+ digits) is a real account
# number in evidence/exports; the sanitiser now redacts it at the source, and this rule catches an old or hand-made
# file that still holds one. The sanitiser's own placeholder ("[redacted]") does not match. Detect-only: redact_text
# does not rewrite it (the export decides what to do with a flagged file).
# the same rule for `merchant_id`, `payer_id`, `account_id` and `email_address`, and a wider
# one for `account_number` (JSON, escaped JSON, Python/YAML and query-string forms). The patterns live in
# tools/personal_data_patterns.py and tools/scan_staged.py reuses these objects, so there is one definition.
DETECT_ONLY_PATTERNS: list[tuple[str, re.Pattern]] = [
    *VALUE_PATTERNS.items(),
    ("paypal_live_host", re.compile(
        r"(?i)(?<![A-Za-z0-9-])(?:%s)%s(?![A-Za-z0-9-])" % (
            "|".join(re.escape(label) for label in _LIVE_HOST_LABELS), re.escape(_LIVE_HOST_SUFFIX)))),
]


def _email_is_safe(match: re.Match) -> bool:
    return match.group(1).lower() in SAFE_EMAIL_DOMAINS


def _credential_assignment_is_safe(match: re.Match, line: str) -> bool:
    """Two real false positives found scanning this repo, both fixed here rather than by narrowing the
    key list (both are still genuinely useful to catch for a REAL secret):
    - A Python assignment whose right-hand side is a function/method call, never a literal secret
      (`token = secrets.token_urlsafe(32)`, `token = page.cookies.get(CSRF_COOKIE_NAME)` - common in this
      project's own CSRF-token test code). The allowed character class stops before "(" (not a valid
      identifier character), so a call's "(" is never INSIDE the match - it is the character right after it.
    - A URL query parameter, not a code/JSON assignment (`checkoutnow?token=<order id>` - PayPal's own
      checkout approval links, confirmed real: the order id, never a secret, reused as that URL's own
      "token" query parameter). Detected by the character immediately before the match being "?" or "&"."""
    end = match.end()
    if end < len(line) and line[end] == "(":
        return True
    start = match.start()
    return start > 0 and line[start - 1] in "?&"


def scan_text(text: str) -> list[tuple[int, str]]:
    """Return [(line_number, type)] for every sensitive-looking match. Values are never returned."""
    hits: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        for kind, pattern in PATTERNS + DETECT_ONLY_PATTERNS:
            for match in pattern.finditer(line):
                if kind == "email" and _email_is_safe(match):
                    continue
                if kind == "credential_assignment" and _credential_assignment_is_safe(match, line):
                    continue
                hits.append((number, kind))
                break  # one hit per type per line is enough
    return hits


def redact_text(text: str) -> str:
    """Replace sensitive values with [REDACTED]. Preserves the number of lines."""
    key = re.compile(r'(?i)("(?:%s)"\s*:\s*)' % _KEYS)
    spans, decoder = [], json.JSONDecoder()
    for match in key.finditer(text):  # whole JSON values, including multiline objects
        if spans and match.start() < spans[-1][1]:
            continue
        try:
            _, length = decoder.raw_decode(text[match.end():])
        except ValueError:
            continue
        spans.append((match.end(), match.end() + length))
    for start, end in reversed(spans):
        newlines = text[start:end].count("\n")
        text = text[:start] + '"[REDACTED]"' + "\n" * newlines + text[end:]
    for kind, pattern in PATTERNS:
        if kind == "credential_assignment":
            text = pattern.sub(
                lambda m: m.group(0) if _credential_assignment_is_safe(m, text)
                else re.split(r"[:=]", m.group(0), 1)[0] + '= "[REDACTED]"', text)
        elif kind == "email":
            text = pattern.sub(lambda m: m.group(0) if _email_is_safe(m) else "[REDACTED]", text)
        elif kind == "auth_header":
            text = pattern.sub(lambda m: m.group(0).split()[0] + " [REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return text


def is_private(path: Path) -> bool:
    parts = {p.casefold() for p in path.parts}
    name = path.name.casefold()
    named = name == ".env" or name.startswith(".env.") or any(n in name for n in PRIVATE_NAMES if not n.startswith("."))
    return bool(parts & EXCLUDED_DIR_NAMES) or named


def iter_files(paths: list[Path]):
    for base in paths:
        if base.is_file():
            yield base
        else:
            for p in sorted(base.rglob("*")):
                if p.is_file():
                    yield p


def scan_files(files, root: Path | None = None):
    """Scan files; returns (hits, skipped). hits = [(relative_path, line, type)]."""
    hits, skipped = [], 0
    for path in files:
        rel = path.relative_to(root) if root and path.is_relative_to(root) else path
        if is_private(rel) or path.suffix.lower() in BINARY_EXT:
            skipped += 1
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            skipped += 1
            continue
        hits.extend((rel.as_posix(), line, kind) for line, kind in scan_text(text))
    return hits, skipped


def tracked_files(root: Path) -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True)
    return [root / n for n in out.stdout.decode("utf-8").split("\0") if n]


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", type=Path, default=[Path(".")])
    ap.add_argument("--tracked", action="store_true", help="scan `git ls-files` of the current directory")
    a = ap.parse_args(argv)
    try:
        root = Path.cwd().resolve()
        files = tracked_files(root) if a.tracked else list(iter_files([p.resolve() for p in a.paths]))
        hits, skipped = scan_files(files, root)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"scan error: {type(exc).__name__}", file=sys.stderr)
        return 2
    for rel, line, kind in hits:
        print(f"{rel}:{line} {kind}")
    counts = Counter(kind for _, _, kind in hits)
    print(f"hits={len(hits)} skipped={skipped} by_type={dict(sorted(counts.items()))}")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
