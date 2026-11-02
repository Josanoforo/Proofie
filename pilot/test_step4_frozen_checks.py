"""The checks around the local model are frozen while its prompt is tuned: the validator functions, the sufficiency
ceiling, the regex patterns and constants they use, the schema's content (keys sorted, so only the order of its
properties may change) and the app-level checks in copilot/core/evidence_model.py. The digests below were taken
before the prompt was tuned; a change to what the code validates fails this test."""
import hashlib
import inspect
import json
import re
from pathlib import Path

import step4_evidence_ai as ai

ROOT = Path(__file__).resolve().parents[1]

FUNCTIONS = ("fact_tokens", "present", "ungrounded", "injection_followed", "injection_attempts", "model_strings",
             "validate", "validate_schema", "enforce_sufficiency", "review_flags_for")
CONSTANTS = ("VALID_TYPES_BY_REASON", "OFFERED_TYPES", "CARRIERS", "NEXT_STEPS", "DISPUTE_ID", "CLAIM_BASIS",
             "SUFFICIENCY_REASONS")

EXPECTED = {
    "functions": "5600ec8728a640e2ac8cc470392e7b60cb78a9b72eec7661640bcdf6b7ee8fa4",
    "patterns": "e5620f6b13aebab48f3cd37a3918048f6fc687108eb0fc4b18581ca7a4115790",
    "constants": "b0b06a5eec00252c17fb672dea48d73f3c688b1e629b0445c0dedb833ab0d461",
    "schema": "de324135f86935283c49ffd2bfbab8970bdad3fd11b341f2f905a693ff4a0bde",
    "evidence_model": "bfa67ef8a7615f3e4aad3333d4f44a4b315894c67d0c456beaeb0b11b2e721a1",
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def digests() -> dict:
    functions = "\n".join(inspect.getsource(getattr(ai, name)) for name in FUNCTIONS)
    patterns = "\n".join(f"{name}={obj.pattern!r}/{obj.flags}" for name, obj in sorted(vars(ai).items())
                         if isinstance(obj, re.Pattern))
    constants = json.dumps({name: sorted(v) if isinstance(v, (set, frozenset)) else v
                            for name, v in ((n, getattr(ai, n)) for n in CONSTANTS)},
                           sort_keys=True, default=lambda v: sorted(v))
    schema = json.dumps(ai.build_schema(["paypal_dispute", "doc_a", "doc_b"]), sort_keys=True)
    evidence_model = (ROOT / "copilot" / "core" / "evidence_model.py").read_text(encoding="utf-8")
    return {"functions": _sha(functions), "patterns": _sha(patterns), "constants": _sha(constants),
            "schema": _sha(schema), "evidence_model": _sha(evidence_model)}


def test_the_checks_around_the_model_are_unchanged():
    assert digests() == EXPECTED
