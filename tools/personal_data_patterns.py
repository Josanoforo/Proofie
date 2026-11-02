"""Shared patterns for personal-data and identifier VALUES.

One definition, imported by `tools/redact.py` (detection rules) and, through `redact`, by `tools/scan_staged.py`.
Nothing here ever returns or prints a matched value: the callers report file, line and rule name only.

What counts as a hit: one of the identifier keys below, a `:` or `=`, and then a VALUE, in any of these shapes
(the quotes may be backslash-escaped, as they are inside the JSON strings of evidence lines):
- JSON: the key and the value both in double quotes;
- Python or YAML: a quoted value, or a bare one;
- query string: the key, `=`, and a bare value.
(No literal example is written here: this file is exported and scanned with its own patterns.)
Not a hit: the sanitiser placeholder (`[redacted]`, any case), an empty value, `null`/`None`/`true`/`false`, a
bare value shorter than 4 characters, or a key that only starts with the name (`email_address_confirmed`).
"""
from __future__ import annotations

import re

# Output kind (what the scanners print) -> the key shapes that carry it. `[_-]?` accepts snake, kebab and run-together.
IDENTIFIER_KEYS: dict[str, str] = {
    "merchant_id": r"merchant[_-]?id",
    "payer_id": r"payer[_-]?id",
    "account_id": r"account[_-]?id",
    "account_number": r"account[_-]?number",
    "email_address": r"email[_-]?address",
}

# Between the key and the value: optional closing quote (possibly backslash-escaped) and a `:` or `=`.
_SEPARATOR = r"""(?:\\*["'])?\s*[:=]\s*"""
# The synthetic-value prefix. The patterns are compiled case-insensitively (key names, placeholders), so the prefix is
# scoped OUT of that flag: only an uppercase DEMO exempts a value; `demoabc12345` and `Demographic12` are still hits.
_DEMO = r"(?-i:DEMO)"
# A quoted value: not empty, not a bracketed placeholder (`[redacted]`, `[email]`, ...) and not a synthetic value (the
# project's fake ranges start with DEMO). Only the first characters are checked, the rest is irrelevant.
_QUOTED = r"""\\*["'](?!\[[A-Za-z _-]{1,20}\]\\*["']|""" + _DEMO + r"""|\\*["'])[^"'\\\s]"""
# A bare value (YAML, Python without quotes, query string): 4+ identifier-like characters that end at a word boundary
# (so `norm_value_from_code` is a variable, not an id), not a placeholder, a null-like word or a synthetic DEMO value,
# not the start of a quote, and not the start of a function call (`key = something.call(...)` is code that computes a
# value, not a value; redact.py's credential rule makes the same exception).
_BARE = (r"""(?!\\*["']|\[[A-Za-z _-]{1,20}\]|""" + _DEMO + r"""|(?:null|none|nil|true|false)\b|[A-Za-z0-9._@+/=-]*\()"""
         r"""[A-Za-z0-9][A-Za-z0-9.@+/=-]{3,}+(?![A-Za-z0-9_])""")   # no `_` inside: real ids never carry one


def _compile(key_regex: str) -> re.Pattern:
    return re.compile(r"(?i)(?<![A-Za-z0-9])(?:%s)%s(?:%s|%s)" % (key_regex, _SEPARATOR, _QUOTED, _BARE))


# kind -> compiled pattern. The kind name equals the key name, so `account_number` keeps the name it had in redact.py.
VALUE_PATTERNS: dict[str, re.Pattern] = {kind: _compile(rx) for kind, rx in IDENTIFIER_KEYS.items()}

# Sandbox e-mail domains allowed in synthetic fixtures. redact.py adds them to its
# safe-domain set, so the one `email` rule everywhere treats them the same way.
SAFE_SANDBOX_EMAIL_DOMAINS = frozenset({"personal.example.com", "business.example.com"})
