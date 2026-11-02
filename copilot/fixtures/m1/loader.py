"""Test helper for the SYNTHETIC / DEMO fixtures in this folder (offline only).

load_case(name) -> dict; FakeClient serves the recorded PayPal bodies; build_docs(case) -> list[ExtractedDoc];
build_context(case) -> TransactionContext through the real fetch_context (so the fixtures also exercise it).
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent


def load_case(name: str) -> dict:
    return json.loads((HERE / f"{name}.json").read_text(encoding="utf-8"))


def case_names() -> list:
    return sorted(p.stem for p in HERE.glob("*.json"))


class FakeHTTPError(RuntimeError):
    def __init__(self, status):
        super().__init__(f"fake PayPal {status}")
        self.status = status


class FakeClient:
    """Mimics SandboxClient.call for GET. A body {"__error": 403} raises a status error; {"__timeout": true}
    raises a timeout. Records every (method, path) so tests can assert on the allowlist."""

    def __init__(self, transactions=None, trackers=None):
        self.bodies = {"/v1/reporting/transactions": transactions, "/v1/shipping/trackers": trackers}
        self.calls = []

    def call(self, method, path, op, **kw):
        self.calls.append((method, path))
        key = next((k for k in self.bodies if path.startswith(k)), None)
        body = self.bodies.get(key)
        if body is None:
            return {}
        if isinstance(body, dict) and "__error" in body:
            raise FakeHTTPError(body["__error"])
        if isinstance(body, dict) and body.get("__timeout"):
            raise httpx.ReadTimeout("fake timeout")
        return body


def fake_client(case: dict) -> FakeClient:
    return FakeClient(case["paypal"].get("transactions"), case["paypal"].get("trackers"))


def build_docs(case: dict) -> list:
    from copilot.core.checklist import ExtractedDoc
    docs = []
    for d in case["docs"]:
        text = d.get("raw_text")
        if text is None and d.get("text_file"):
            text = (HERE / d["text_file"]).read_text(encoding="utf-8")
        docs.append(ExtractedDoc(d["doc_id"], d["fields"], text is not None, text))
    return docs


def build_context(case: dict):
    from copilot.core.context import fetch_context
    ctx = fetch_context(fake_client(case), case["dispute_norm"])
    for k, v in (case.get("seller_statement") or {}).items():
        ctx = ctx.with_seller_statement(k, v)
    return ctx


def today_of(case: dict) -> date:
    return date.fromisoformat(case["today"])
