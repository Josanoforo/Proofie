"""Pure sentence provenance: literal readings are not semantic model judgments."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass

VERDICTS = ("verified", "unverifiable", "seller_statement", "conflict")
CLAIM_BASES = ("verified", "seller_statement", "buyer_statement", "model_interpretation")
CHECK_BASES = ("case_request", "published_policy", "app_quality_rule", "none")
_SHA = re.compile(r"[a-f0-9]{64}")
_PRIVATE = re.compile(r"[^\s<>]+@[^\s<>]+|\bBearer\b[^\n]*|\bPayPal-[\w-]+\b[^\n]*|"
                      r"\b(?:access[_-]?token|client[_-]?secret|password|api[_-]?key)\s*[:=]\s*\S+", re.I)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def safe_text(value) -> str:
    return _PRIVATE.sub("[redacted]", str(value))


def is_private(value) -> bool:
    return bool(_PRIVATE.search(str(value)))


@dataclass(frozen=True)
class Source:
    artifact_id: str
    field: str
    sha256: str | None
    value: str | None
    confidence: str
    check_basis: str = "none"
    check_state: str = "present_unverifiable"
    kind: str = "document"

    def __post_init__(self):
        if self.sha256 is not None and not _SHA.fullmatch(self.sha256):
            raise ValueError("Invalid artifact SHA256")
        if self.check_basis not in CHECK_BASES or self.check_state not in (
                "verified", "missing", "present_unverifiable", "conflict", "not_applicable"):
            raise ValueError("Invalid checklist provenance")
        if self.confidence not in ("read", "illegible", "absent", "untrusted"):
            raise ValueError("Invalid source confidence")


@dataclass(frozen=True)
class Claim:
    key: str
    claim_id: int
    sentence_index: int
    text: str
    claim_basis: str
    sources: tuple[Source, ...]
    verdict: str

    @property
    def bases(self):
        return sorted({s.check_basis for s in self.sources}) or ["none"]


def sentences(text: str) -> list[str]:
    # Keep wording intact; decimals and a newline inside a sentence are not new claims.
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÑ¿¡])", text) if part.strip()]


def validate(claim_id: int, text: str, basis: str, sources: tuple[Source, ...]) -> list[Claim]:
    if type(claim_id) is not int or claim_id < 1 or basis not in CLAIM_BASES or not isinstance(text, str):
        raise ValueError("Invalid stored claim")
    units = []
    for index, sentence in enumerate(sentences(text)):
        # Relevant readings are exact values contained in this sentence. A referenced artifact alone
        # never verifies unrelated text; unmapped/free-form prose remains unverifiable.
        relevant = tuple(s for s in sources if s.value and s.value.casefold() in sentence.casefold())
        cited = relevant or sources
        if any(s.check_state == "conflict" for s in cited):
            verdict = "conflict"
        elif basis == "seller_statement":
            verdict = "seller_statement"
        elif (basis == "verified" and relevant and all(
                s.sha256 and s.confidence == "read" and s.check_state == "verified"
                and s.field != "extracted_text" and s.kind != "seller_statement" for s in relevant)
              and sentence.casefold() in {s.value.casefold() for s in relevant}):
            # Only a sentence consisting of the exact verified value qualifies automatically.
            # Composite prose needs explicit human review, even if its dates/IDs appear in a source.
            verdict = "verified"
        else:
            verdict = "unverifiable"
        units.append(Claim(f"{claim_id}-{index}", claim_id, index, sentence, basis, cited, verdict))
    return units


def snapshot(claims: list[Claim]) -> list[dict]:
    return [asdict(c) for c in claims]
