"""Pre-dispute checks for one capture. Reads only (Transaction Search and the tracker list), no AI, no new rule.

* The TransactionContext comes from copilot.core.context.fetch_context with a synthetic one-line dispute record
  (the capture id and "now"): the same Transaction Search and tracker reads the dispute page uses.
* copilot.core.checklist.evaluate runs with the rule set of COPILOT_RULES_COUNTRY and extracted_docs=[]: its checks
  are kept verbatim. Items that need documents come out missing, and PayPal's evidence requests exist only once a
  dispute is open, so those are listed under `not_evaluable` rather than invented.
* protection_eligibility is PayPal's raw code from the same Transaction Search answer (a recording wrapper keeps the
  body, so no extra call); the code is stored as PayPal sent it, without interpreting it.
* The address comparison is done here by code, between PayPal's shipping address for the transaction and the address
  the seller declared: checklist.evaluate has no input for it without a document (its destination check reads the
  document's location). Postal code first; else city and state; else it says it cannot compare."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from copilot import config
from copilot.core.checklist import UnknownCountryError, evaluate
from copilot.core.checklist_input import cached_rules
from copilot.core.context import fetch_context

NOT_EVALUABLE_BEFORE_DISPUTE = (
    {"id": "case_request", "reason": "PayPal's evidence requests exist only once a dispute is open."},
)
ADDRESS_KEYS = ("line1", "line2", "city", "state", "postal_code", "country_code")


@dataclass(frozen=True)
class ChecksResult:
    checks: tuple = ()                       # dicts: id, label, state, basis, sources, action, risk, message, paypal_sourced
    not_evaluable: tuple = ()
    protection_eligibility: str | None = None
    address_comparison: dict = field(default_factory=dict)
    limitations: tuple = ()                  # dicts: source, message
    rules: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"checks": list(self.checks), "not_evaluable": list(self.not_evaluable),
                "protection_eligibility": self.protection_eligibility,
                "address_comparison": self.address_comparison, "limitations": list(self.limitations),
                "rules": self.rules}


class _Recorder:
    """Passes every call through and keeps the Transaction Search answers."""
    def __init__(self, client):
        self._client = client
        self.search_bodies: list = []

    def call(self, method, path, op, **kw):
        body = self._client.call(method, path, op, **kw)
        if path.startswith("/v1/reporting/transactions"):
            self.search_bodies.append(body)
        return body


def _plain(s) -> str:
    s = unicodedata.normalize("NFKD", str(s))
    return re.sub(r"\s+", " ", "".join(c for c in s if not unicodedata.combining(c)).casefold()).strip()


def _postal(s) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(s or "")).upper()


def compare_addresses(paypal: dict | None, declared: dict | None) -> dict:
    """result: match | mismatch | cannot_compare | not_declared | no_paypal_address."""
    p = {k: v for k, v in (paypal or {}).items() if k in ADDRESS_KEYS and v}
    d = {k: str(v).strip() for k, v in (declared or {}).items() if k in ADDRESS_KEYS and str(v or "").strip()}
    base = {"paypal_address": p or None, "declared_address": d or None}
    if not d:
        return {**base, "result": "not_declared", "basis": "the seller declared no address"}
    if not p:
        return {**base, "result": "no_paypal_address", "basis": "PayPal returned no shipping address"}
    if _postal(p.get("postal_code")) and _postal(d.get("postal_code")):
        same = _postal(p["postal_code"]) == _postal(d["postal_code"])
        return {**base, "result": "match" if same else "mismatch", "basis": "postal code"}
    if p.get("city") and p.get("state") and d.get("city") and d.get("state"):
        same = _plain(p["city"]) == _plain(d["city"]) and _plain(p["state"]) == _plain(d["state"])
        return {**base, "result": "match" if same else "mismatch", "basis": "city and state"}
    return {**base, "result": "cannot_compare", "basis": "needs a postal code, or city and state, on both sides"}


def _eligibility(bodies: list, capture_id: str) -> str | None:
    for body in bodies:
        for row in (body or {}).get("transaction_details") or [] if isinstance(body, dict) else []:
            info = row.get("transaction_info") or {}
            if info.get("transaction_id") == capture_id and info.get("protection_eligibility") not in (None, ""):
                return str(info["protection_eligibility"])
    return None


def _check_dict(c) -> dict:
    d = asdict(c)
    return {"id": d["id"], "label": d["label_for_seller"], "state": d["state"], "basis": d["basis"],
            "sources": list(d["sources"]), "action": d["action"], "risk": d["risk"], "message": d["seller_message"],
            "paypal_sourced": any(str(s).startswith("paypal_") for s in d["sources"])}


def pre_dispute_checks(client, capture_id: str, seller_declared_address: dict | None,
                       now: datetime | None = None) -> ChecksResult:
    now = now or datetime.now(timezone.utc)
    recorder = _Recorder(client)
    norm = {"seller_transaction_ids": [capture_id], "create_time": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    context = fetch_context(recorder, norm)
    limitations = [{"source": lim.source, "message": lim.message} for lim in context.limitations]
    try:
        rules = cached_rules()
        checks = tuple(_check_dict(c) for c in evaluate(context, [], rules))
        rules_info = {"rule_set_id": rules.rule_set_id, "country": rules.country, "source_url": rules.source_url}
    except UnknownCountryError as exc:
        checks, rules_info = (), {"country": config.RULES_COUNTRY, "error": str(exc)}
        limitations.append({"source": "rules", "message": str(exc)})
    return ChecksResult(checks, NOT_EVALUABLE_BEFORE_DISPUTE, _eligibility(recorder.search_bodies, capture_id),
                        compare_addresses(context.get("shipping_address"), seller_declared_address),
                        tuple(limitations), rules_info)
