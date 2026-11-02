"""Code, not the model, decides whether a Spanish version keeps the facts of its English statement.

parity_problems(en, es) returns plain sentences, empty when the Spanish passes: the same digit runs (numbers, dates, amounts,
the digits of a tracking number), the same months (English and Spanish month names; "may", "mar", "ago" and "sep" count only
next to a digit), every tracking-like code of one text present in the other (compared without spaces and hyphens), no link
or e-mail address, not empty, not over MAX_LENGTH. A word list, not a translator: the seller reviews every word."""
from __future__ import annotations

import re

MAX_LENGTH = 4000
DIGIT_RUN = re.compile(r"\d+")
WORD = re.compile(r"[A-Za-zÀ-ÿ]+")
CODE = re.compile(r"[A-Za-z0-9]+(?:[-][A-Za-z0-9]+)*")
LINK_MARKERS = ("http", "www.", "@")
_MONTHS = (
    ("january", "jan", "enero", "ene"), ("february", "feb", "febrero"), ("march", "marzo"), ("april", "apr", "abril", "abr"),
    ("june", "jun", "junio"), ("july", "jul", "julio"), ("august", "aug", "agosto"),
    ("september", "sept", "septiembre", "setiembre"), ("october", "oct", "octubre"), ("november", "nov", "noviembre"),
    ("december", "dec", "diciembre", "dic"))
MONTH_NUMBER = {name: n for n, names in zip((1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12), _MONTHS) for name in names}
# Words that are also ordinary words; a month only beside a digit.
AMBIGUOUS = {"may": 5, "mayo": 5, "mar": 3, "ago": 8, "sep": 9}


def _months(text: str) -> set:
    found = set()
    for m in WORD.finditer(text):
        word = m.group(0).lower()
        if word in MONTH_NUMBER:
            found.add(MONTH_NUMBER[word])
        elif word in AMBIGUOUS:
            before, after = text[max(0, m.start() - 3):m.start()], text[m.end():m.end() + 3]
            if re.search(r"\d", before + after):
                found.add(AMBIGUOUS[word])
    return found


def _squash(text: str) -> str:
    return re.sub(r"[\s\-]", "", text).upper()


def _codes(text: str) -> set:
    """Tracking-like codes: 10 or more letters and digits (hyphens allowed inside) with at least one digit."""
    out = set()
    for m in CODE.finditer(text):
        squashed = m.group(0).replace("-", "").upper()
        if len(squashed) >= 10 and any(ch.isdigit() for ch in squashed):
            out.add(squashed)
    return out


def _listed(values) -> str:
    return ", ".join(sorted(str(v) for v in values))


def parity_problems(en: str, es: str) -> list:
    es = es or ""
    if not es.strip():
        return ["The Spanish version is empty."]
    problems = []
    if len(es) > MAX_LENGTH:
        problems.append(f"The Spanish version is longer than {MAX_LENGTH} characters.")
    en_digits, es_digits = set(DIGIT_RUN.findall(en)), set(DIGIT_RUN.findall(es))
    if es_digits - en_digits:
        problems.append(f"The Spanish version has a number the English does not: {_listed(es_digits - en_digits)}.")
    if en_digits - es_digits:
        problems.append(f"The Spanish version leaves out a number of the English: {_listed(en_digits - es_digits)}.")
    en_months, es_months = _months(en), _months(es)
    if es_months - en_months:
        problems.append("The Spanish version names a month the English does not.")
    if en_months - es_months:
        problems.append("The Spanish version leaves out a month the English names.")
    en_flat, es_flat = _squash(en), _squash(es)
    new_codes = {c for c in _codes(es) if c not in en_flat}
    lost_codes = {c for c in _codes(en) if c not in es_flat}
    if new_codes:
        problems.append(f"The Spanish version has a tracking code the English does not: {_listed(new_codes)}.")
    if lost_codes:
        problems.append(f"The Spanish version leaves out a tracking code of the English: {_listed(lost_codes)}.")
    if any(marker in es.lower() and marker not in en.lower() for marker in LINK_MARKERS):
        problems.append("The Spanish version has a link or an e-mail address.")
    return problems
