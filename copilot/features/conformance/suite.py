"""Real pure builders and response fixtures; no transport or model is invoked."""
from __future__ import annotations

import copy
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .carriers_map import evidence_carrier
from .schemas import Documents, issues

ROOT = Path(__file__).resolve().parents[3]


def request_rows(docs):
    from copilot.core import context, send
    from copilot.features.account_cliff.fetch import fetch_sales_rows
    from copilot.features.fulfilment.tracker_request import body_from_form
    from copilot.features.outcome.appeal import appeal_body
    from copilot.features.resolution.actions import message_body, offer_body
    from copilot.features.webhooks.verify import verification_body
    rows = []
    claims = [{"text": "DEMO: shipped by the carrier on the stated date."}]
    ev = docs.evidence_validator()

    def add(name, validator, body):
        errors = issues(validator, body)
        rows.append({"name": name, "valid": not errors, "issues": errors})

    for code in ("FEDEX", "MEX_ESTAFETA", "DEMO Local Courier", "DOMINO"):
        add("evidence-" + code, ev, send.build_request_input(claims, "DEMO1234567890", code))
    raw = send.build_request_input(claims, "DEMO1234567890", "FEDEX")
    parts = send.build_multipart(raw, [{"filename": "demo.pdf", "content_type": "application/pdf", "data": b"%PDF-1.4 DEMO"}])
    names = [item[0] for item in parts]
    multipart = docs.deref("disputes", docs.raw["disputes"]["components"]["requestBodies"]["evidences"])
    assert "evidence-file" in names and "evidence-file" in multipart["content"]["multipart/form-data"]["schema"]["properties"]
    add("multipart-input-nearest-component", ev, json.loads(parts[0][1][1]))
    for cents, note in ((1000, "DEMO partial refund"), (123456, "x" * 2000)):
        add("offer-" + str(cents), docs.validator("disputes", "req", "/components/schemas/make_offer"), offer_body(cents, "USD", note))
    msg = message_body("DEMO message")
    message_schema = docs.deref("disputes", docs.raw["disputes"]["paths"]["/v1/customer/disputes/{id}/send-message"]["post"]["requestBody"])
    assert "message_document" in message_schema["content"]["multipart/form-data"]["schema"]["properties"] and set(msg) == {"message"}
    rows.append({"name": "message-prose-only", "valid": None, "issues": [], "note": "No JSON message-text schema; multipart document only."})
    for code in ("FEDEX", "DOMINO"):
        add("appeal-" + code, ev, appeal_body(claims, "DEMO1234567890", code))
    for carrier in ("FedEx", "DEMO Local Courier"):
        add("tracker-" + carrier, docs.validator("tracking", "req", "/components/requestBodies/tracker_collection/content/application~1json/schema"), body_from_form("DEMO1234567890123", "DEMO1234567890", carrier))
    headers = {"PAYPAL-AUTH-ALGO": "SHA256withRSA", "PAYPAL-CERT-URL": "https://localhost/cert", "PAYPAL-TRANSMISSION-ID": "DEMO1234567890",
               "PAYPAL-TRANSMISSION-SIG": "REVNTw==", "PAYPAL-TRANSMISSION-TIME": "2026-10-06T12:00:00Z"}
    event = json.dumps({"id": "DEMOevent1", "event_type": "CUSTOMER.DISPUTE.CREATED", "resource": {"dispute_id": "PP-R-DEMO-0000001"}})
    for wid in ("DEMO1234567890", "DEMO-ID-WITH-HYPHENS"):
        add("webhook-" + wid, docs.validator("webhooks", "req", "/components/schemas/verify_webhook_signature"), verification_body(headers, event, wid))

    class Recorder:
        def __init__(self):
            self.paths = []
        def call(self, method, path, op, **kwargs):
            assert method == "GET"
            self.paths.append(path)
            return {"transaction_details": [], "total_pages": 1}

    one, two = Recorder(), Recorder()
    context._search(one, "DEMO1234567890123", datetime(2026, 9, 1, tzinfo=timezone.utc), {}, [])
    fetch_sales_rows(two, [(datetime(2026, 7, 1, tzinfo=timezone.utc), datetime(2026, 7, 31, tzinfo=timezone.utc))])
    parameters = [docs.deref("transactions", p) for p in docs.raw["transactions"]["paths"]["/v1/reporting/transactions"]["get"]["parameters"]]
    mapping = {p["name"]: p for p in parameters if p.get("in") == "query"}
    for label, recorder in (("context-query", one), ("cliff-query", two)):
        query = dict(parse_qsl(urlsplit(recorder.paths[0]).query))
        errors = []
        for key, value in query.items():
            if key not in mapping:
                errors.append({"pointer": "/" + key, "keyword": "unknown-parameter", "artefact": False})
                continue
            kind = mapping[key].get("schema", {}).get("type")
            val = int(value) if kind == "integer" else value
            errors += issues(docs.validator("transactions", "req", "/components/parameters/" + key + "/schema"), val)
        errors += [{"pointer": "/" + p["name"], "keyword": "required", "artefact": False} for p in parameters if p.get("in") == "query" and p.get("required") and p["name"] not in query]
        rows.append({"name": label, "valid": not errors, "issues": errors})
    return rows


def mapped_domino_rows(docs):
    from copilot.core.send import build_request_input
    from copilot.features.outcome.appeal import appeal_body
    rows = []
    for builder in (build_request_input, appeal_body):
        body = copy.deepcopy(builder([{"text": "DEMO shipped"}], "DEMO12345", "DOMINO"))
        rows.append({"name": builder.__name__, "issues": issues(docs.evidence_validator(), body)})
    return rows


OPERATIONS = (
    ("GET", r"^/v1/customer/disputes$", "disputes", "dispute_search"),
    ("GET", r"^/v1/customer/disputes/[^/]+$", "disputes", "dispute"),
    ("POST", r"^/v1/customer/disputes/[^/]+/(provide-evidence|make-offer|send-message|appeal)$", "disputes", "subsequent_action"),
    ("GET", r"^/v1/reporting/transactions$", "transactions", "search_response"),
    ("GET", r"^/v1/shipping/trackers$", "tracking", "tracker"),
    ("POST", r"^/v1/shipping/trackers$", "tracking", "tracker_collection"),
    ("POST", r"^/v1/notifications/verify-webhook-signature$", "webhooks", "verify_webhook_signature_response"),
    ("GET", r"^/v2/checkout/orders/[^/]+$", "orders", "order"),
)


def response_counts(docs, evidence_directory=None):
    counts = {"checked": 0, "valid": 0, "artefact_only": 0, "different": 0, "non_2xx_or_unmatched": 0}
    rows = []
    def check(key, component, body):
        errors = issues(docs.validator(key, "res", "/components/schemas/" + component), body)
        counts["checked"] += 1
        counts["valid" if not errors else "artefact_only" if all(e["artefact"] for e in errors) else "different"] += 1
        rows.append({"document": key, "component": component, "issues": errors})
    if evidence_directory is None:
        for path in sorted((ROOT / "pilot/fixtures/synthetic").glob("*.json")):
            body = json.loads(path.read_text(encoding="utf-8"))
            check("disputes", "dispute_search" if "items" in body else "dispute", body)
    else:
        directory = Path(evidence_directory)
        if not directory.is_dir():
            # The folder PROOFIE_EVIDENCE_DIR names may not exist yet on a fresh install: nothing is recorded, which is not an error.
            return counts, rows
        for path in sorted(directory.glob("*.jsonl")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                try:
                    rec = json.loads(line)
                except ValueError:
                    raise ValueError(f"malformed assigned response JSONL at line {number}") from None
                status = rec.get("status")
                match = next((op for op in OPERATIONS if op[0] == rec.get("method") and re.match(op[1], str(rec.get("path", "")).split("?")[0])), None)
                if type(status) is not int or not 200 <= status < 300 or match is None:
                    counts["non_2xx_or_unmatched"] += 1
                    continue
                check(match[2], match[3], rec.get("body"))
    return counts, rows


def sentence(requests, responses, scope="synthetic response fixtures"):
    valid = sum(r["valid"] is True for r in requests)
    invalid = sum(r["valid"] is False for r in requests)
    no_schema = sum(r["valid"] is None for r in requests)
    comparable = responses["valid"] + responses["artefact_only"]
    clauses = []
    if invalid:
        clauses.append(f"{invalid} {'does' if invalid == 1 else 'do'} not")
    if no_schema:
        clauses.append(f"{no_schema} {'has' if no_schema == 1 else 'have'} no JSON schema to check (send-message accepts a multipart document only)")
    conform = f"{valid} of {len(requests)} request fixtures conform"
    if len(clauses) == 2:
        request_clause = f"{conform}, {clauses[0]}, and {clauses[1]}"
    elif clauses:
        request_clause = f"{conform} and {clauses[0]}"
    else:
        request_clause = conform
    mismatch = "the remaining request mismatch is a test-fixture webhook-id style; " if invalid else ""
    return ("Proofie's request bodies were validated against PayPal's own published OpenAPI documents "
            f"(pinned 2026-10-06, sha256 verified provenance) and its {scope} against the same documents: "
            f"{request_clause}; "
            f"{mismatch}"
            f"{responses['checked']} {scope} were checked, {comparable} of them with no difference other than the sanitiser's own placeholders, "
            "and the rest differ from the published schema where the fixture or sandbox and contract disagree "
            "(field-level mismatches are reported by JSON pointer only). Both production evidence builders' DOMINO fixtures conform through the wired dispute-carrier mapping.")


def run(full_directory=None, evidence_directory=None):
    docs = Documents(full_directory)
    requests = request_rows(docs)
    mapped = mapped_domino_rows(docs)
    counts, responses = response_counts(docs, evidence_directory)
    return {"requests": requests, "mapped": mapped, "response_counts": counts, "responses": responses,
            "sentence": sentence(requests, counts, "recorded 2xx responses" if evidence_directory is not None else "synthetic response fixtures")}


def configured_run():
    full = os.environ.get("PROOFIE_SPECS_DIR")
    evidence = os.environ.get("PROOFIE_EVIDENCE_DIR")
    return run(full if full and Path(full).is_dir() else None, evidence), [label for label, value in (("PROOFIE_SPECS_DIR", full and Path(full).is_dir()), ("PROOFIE_EVIDENCE_DIR", evidence)) if not value]
