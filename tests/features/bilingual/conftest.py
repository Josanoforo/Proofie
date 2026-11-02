"""Shared set-up for the bilingual feature: the flag and the rule-set country, one package with two statements in the
test's own database (the root conftest isolates config.db_path() per test), read-only PayPal fakes and a fake model call."""
import copy
import json
from pathlib import Path

import pytest

from copilot import config
from copilot.db import repo

FLAG = "COPILOT_FEATURE_BILINGUAL"
DISPUTE_ID = "PP-R-DEMO-0000001"
EN_1 = "The parcel was delivered to the buyer's address on 7 Oct 2026, tracking 1Z999AA10123456784."
EN_2 = "The buyer's address on the order matches the delivery address."
ES_1 = "El paquete se entregó en la dirección del comprador el 7 oct 2026, guía 1Z999AA10123456784."
ES_2 = "La dirección del pedido coincide con la dirección de entrega."
ROOT = Path(__file__).resolve().parents[3]
WAITING = json.loads((ROOT / "pilot" / "fixtures" / "synthetic" / "dispute_PP-R-DEMO-0000001_waiting_for_seller.json")
                     .read_text(encoding="utf-8"))["body"]


@pytest.fixture
def mx_on(monkeypatch):
    monkeypatch.setenv(FLAG, "true")
    monkeypatch.setattr(config, "RULES_COUNTRY", "MX")


def make_package(dispute_id=DISPUTE_ID, texts=(EN_1, EN_2)) -> int:
    db = config.db_path()
    package_id = repo.insert_package(db, dispute_id, "human_edit", True, [])
    repo.insert_claims(db, package_id, [{"evidence_index": 0, "claim_index": i, "text": t, "source_ids": [],
                                         "basis": "seller_statement"} for i, t in enumerate(texts)])
    return package_id


def claim_ids(package_id) -> list:
    return [c["id"] for c in repo.list_claims(config.db_path(), package_id)]


class ReadOnlyPayPal:
    """Answers the reads the package and approval pages make (the recorded-shape synthetic dispute); any write, or any
    other call, fails the test."""
    def __init__(self, body=None):
        self.body = body or WAITING
        self.calls = []

    def call(self, method, path, op, **kw):
        assert method == "GET", f"PayPal write attempted: {op}"
        self.calls.append(op)
        if "transactions" in path or op == "context_transaction_search":
            return {"transaction_details": []}
        if "trackers" in path or op == "context_tracking":
            return {"trackers": []}
        if op == "show_dispute" or path.endswith(DISPUTE_ID):
            return copy.deepcopy(self.body)
        raise AssertionError(f"unexpected op {op}")


class FakeModel:
    """A model call: answers {"es": ...} from a table keyed by the English statement found in the prompt."""
    def __init__(self, answers=None, raises=None, raw=None):
        self.answers, self.raises, self.raw, self.calls = dict(answers or {}), raises, raw, []

    def __call__(self, messages, schema, seed):
        self.calls.append((messages, schema, seed))
        if self.raises:
            raise self.raises
        if self.raw is not None:
            return self.raw
        user = messages[-1]["content"]
        es = next((v for k, v in self.answers.items() if k in user), "")
        return json.dumps({"es": es}, ensure_ascii=False), {"wall_ms": 5}
