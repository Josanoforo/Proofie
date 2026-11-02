import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from copilot import config

KINDS = {"followup", "information", "return", "replacement", "partial_offer"}
EXAMPLE = {"locale": "en", "recipient": "Buyer (recorded case contact)", "currency": "USD", "allowed_actions": ["message", "offer"], "facts": [{"id": "delivery", "text": "Tracking shows in transit", "source": "user-supplied recorded data"}], "proposals": [{"id": "offer-1", "kind": "partial_offer", "amount_cents": 2500, "conditions": "If acceptable, reply to this proposal.", "created_at": "2026-10-19T10:00:00Z"}, {"id": "offer-2", "kind": "partial_offer", "amount_cents": 3200, "conditions": "Available until 2026-10-30.", "created_at": "2026-10-21T10:00:00Z"}], "responses": [{"id": "reply-1", "proposal_id": "offer-1", "received_at": "2026-10-22T12:00:00Z", "outcome": "declined"}]}
TEMPLATES = {
    "en": {"followup": "Hello {recipient}, we are following up about {facts}. Please reply with any update.", "information": "Hello {recipient}, we can provide this recorded information: {facts}.", "return": "Hello {recipient}, we can discuss a return under these recorded conditions: {conditions}.", "replacement": "Hello {recipient}, we can discuss a replacement under these recorded conditions: {conditions}.", "partial_offer": "Hello {recipient}, we propose {currency} {amount} as a partial resolution, subject to: {conditions}. This is a proposal only."},
    "es": {"followup": "Hola {recipient}, damos seguimiento a {facts}. Por favor responde con cualquier novedad.", "information": "Hola {recipient}, podemos compartir esta información registrada: {facts}.", "return": "Hola {recipient}, podemos conversar sobre una devolución bajo estas condiciones registradas: {conditions}.", "replacement": "Hola {recipient}, podemos conversar sobre un reemplazo bajo estas condiciones registradas: {conditions}.", "partial_offer": "Hola {recipient}, proponemos {currency} {amount} como resolución parcial, sujeto a: {conditions}. Es solo una propuesta."},
}


def case_facts(norm):
    """A small whitelist of facts already present in Proofie's current dispute snapshot."""
    if not isinstance(norm, dict):
        return []
    facts = []
    for key, label in (("reason", "PayPal dispute reason"), ("status", "PayPal dispute status"),
                       ("stage", "PayPal dispute stage")):
        value = norm.get(key)
        if isinstance(value, str) and value.strip():
            facts.append({"id": key, "text": f"{label}: {value}",
                          "source": f"PayPal dispute snapshot field {key}; read by this Proofie request"})
    cents, currency = norm.get("amount_cents"), norm.get("currency")
    if type(cents) is int and cents >= 0 and isinstance(currency, str) and currency:
        facts.append({"id": "amount", "text": f"Recorded dispute amount: {currency} {cents // 100}.{cents % 100:02d}",
                      "source": "PayPal dispute snapshot fields amount_cents and currency; read by this Proofie request"})
    return facts


def case_history_path(dispute_id):
    from copilot.web.security import validate_dispute_id
    if validate_dispute_id(dispute_id):
        raise ValueError("Invalid dispute reference.")
    root = config.data_dir() / "mejoras10" / "03" / "history"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{dispute_id}.jsonl"
    if path.parent.resolve() != root.resolve() or path.is_symlink():
        raise ValueError("Local proposal history path is not owned by this feature.")
    return path


def read_case_history(dispute_id):
    path = case_history_path(dispute_id)
    if not path.exists():
        return []
    if path.stat().st_size > 1_000_000:
        raise ValueError("Local proposal history exceeds the 1 MB display limit.")
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or row.get("record_type", "proposal") != "proposal" or not isinstance(row.get("logical_id"), str):
            raise ValueError("Local proposal history is corrupt; refusing to display it.")
        rows.append(row)
        if len(rows) > 200:
            raise ValueError("Local proposal history exceeds the 200 record display limit.")
    return rows


def build_case_draft(norm, allowed_actions, *, kind, locale, recipient, amount_cents=0, conditions="", history_path=None,
                     source_simulation=None, source_plan=None):
    action = "offer" if kind == "partial_offer" else "message"
    if action not in allowed_actions:
        raise ValueError("PayPal's recorded dispute snapshot does not currently allow this action.")
    facts = case_facts(norm)
    if not facts:
        raise ValueError("The current dispute snapshot has no supported facts to include in a draft.")
    currency = norm.get("currency") if isinstance(norm, dict) else None
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("Currency is unavailable in the current dispute snapshot; no currency was inferred.")
    proposal = {"id": "draft-" + hashlib.sha256(
        json.dumps([kind, locale, recipient, amount_cents, conditions, facts], sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:24], "kind": kind, "amount_cents": amount_cents,
        "conditions": conditions, "created_at": datetime.now(timezone.utc).isoformat()}
    if source_simulation is not None:
        proposal["source_simulation"] = source_simulation
    if source_plan is not None:
        proposal["source_plan"] = source_plan
    return build({"locale": locale, "recipient": recipient, "currency": currency,
                  "allowed_actions": sorted(allowed_actions), "facts": facts, "proposals": [proposal], "responses": []},
                 history_path)


def build(data, history_path=None):
    try:
        if not isinstance(data, dict) or len(json.dumps(data, ensure_ascii=False)) > 30000:
            raise ValueError("Input must be an object under 30 KB.")
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("Input"):
            raise
        raise ValueError("Input must contain JSON values.") from exc
    locale = data.get("locale")
    if not isinstance(locale, str) or locale not in TEMPLATES:
        raise ValueError("Unsupported locale; human review and translation required.")
    recipient, currency = data.get("recipient"), data.get("currency")
    if not isinstance(recipient, str) or not recipient or recipient != recipient.strip() or len(recipient) > 200:
        raise ValueError("Exact recorded recipient is required; whitespace is preserved and rejected if ambiguous.")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("Currency must be a three-letter uppercase currency code.")
    actions, facts, proposals, responses = (data.get("allowed_actions", []), data.get("facts", []), data.get("proposals", []), data.get("responses", []))
    if not isinstance(actions, list) or any(not isinstance(x, str) for x in actions) or not isinstance(facts, list) or any(not isinstance(x, dict) for x in facts) or not isinstance(proposals, list) or len(proposals) > 50 or any(not isinstance(x, dict) for x in proposals) or not isinstance(responses, list) or len(responses) > 100 or any(not isinstance(x, dict) for x in responses):
        raise ValueError("Actions, facts, proposals and responses must be bounded lists with valid row shapes.")
    for f in facts:
        if set(f) != {"id", "text", "source"} or not all(isinstance(f.get(k), str) and f[k].strip() for k in ("id", "text", "source")) or len(f["id"]) > 100 or len(f["text"]) > 2000 or len(f["source"]) > 200:
            raise ValueError("Each fact must contain only bounded, non-empty id, text and source fields.")
    fact_ids = [f.get("id") for f in facts]
    if any(not isinstance(fid, str) or not fid for fid in fact_ids) or len(fact_ids) != len(set(fact_ids)):
        raise ValueError("Fact IDs must be non-empty and unique.")
    facts_text = "; ".join(f["text"] for f in facts) or "no recorded facts supplied"
    output, logical_ids = [], set()
    for p in proposals:
        kind, pid, conditions = p.get("kind"), p.get("id"), p.get("conditions", "")
        if not isinstance(kind, str) or kind not in KINDS or not isinstance(pid, str) or not pid or len(pid) > 100:
            raise ValueError("Every proposal requires an id and supported kind.")
        if not isinstance(conditions, str) or len(conditions) > 1000:
            raise ValueError("Conditions must be text of at most 1000 characters; longer conditions are rejected, never truncated.")
        action = "offer" if kind == "partial_offer" else "message"
        if action not in actions:
            raise ValueError("Recorded snapshot does not permit this action.")
        cents = p.get("amount_cents", 0)
        if type(cents) is not int or cents < 0 or kind == "partial_offer" and cents <= 0:
            raise ValueError("Money must be non-negative integer minor units; offers must be positive.")
        if kind != "partial_offer" and cents != 0:
            raise ValueError("Only partial offers may contain an amount.")
        amount = f"{cents // 100}.{cents % 100:02d}"
        created_at = p.get("created_at")
        if created_at is not None:
            if not isinstance(created_at, str):
                raise ValueError("Proposal created_at must be a timezone-aware timestamp.")
            try:
                created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                if created_dt.tzinfo is None: raise ValueError
            except ValueError as exc:
                raise ValueError("Proposal created_at must be a timezone-aware timestamp.") from exc
        else:
            created_dt = None
        source_simulation = p.get("source_simulation")
        source_plan = p.get("source_plan")
        source_incident = p.get("source_incident")
        seller_text = p.get("seller_text")
        if source_incident is not None:
            if not isinstance(source_incident, dict) or set(source_incident) != {"fingerprint", "incident", "locale"} or not isinstance(source_incident.get("fingerprint"), str) or not re.fullmatch(r"[a-f0-9]{64}", source_incident["fingerprint"]) or source_incident.get("locale") != locale or source_incident.get("incident") not in ("delivered_not_received", "delay", "missing", "buyer_question"):
                raise ValueError("Incident provenance is invalid.")
            if not isinstance(seller_text, str) or not seller_text.strip() or len(seller_text) > 2000:
                raise ValueError("The seller-reviewed message is required.")
            if action != "message":
                raise ValueError("Incident draft supports a message only.")
        elif seller_text is not None:
            raise ValueError("A seller-reviewed incident message requires provenance.")
        if source_plan is not None and (not isinstance(source_plan, str) or not re.fullmatch(r"[a-f0-9]{64}", source_plan)):
            raise ValueError("Plan fingerprint is invalid.")
        if source_simulation is not None:
            source_fields = {"logical_id", "action", "amount_cents", "currency", "policy_version",
                             "policy_sha256", "state_sha256", "dossier_sha256"}
            if not isinstance(source_simulation, dict) or set(source_simulation) != source_fields or \
                    not all(isinstance(source_simulation.get(key), str) and source_simulation[key]
                            for key in ("logical_id", "action", "currency", "policy_version", "policy_sha256",
                                        "state_sha256", "dossier_sha256")) or \
                    type(source_simulation.get("amount_cents")) is not int or source_simulation["amount_cents"] < 0:
                raise ValueError("Autopilot source must contain a bounded, complete simulation provenance record.")
            if source_simulation["action"] != action or source_simulation["currency"] != currency:
                raise ValueError("Autopilot source action and currency must match the local proposal.")
        template = TEMPLATES[locale][kind]
        draft = template.format(recipient=recipient, facts=facts_text, conditions=conditions or "conditions to be reviewed", currency=currency, amount=amount)
        if source_incident is not None:
            draft = seller_text
        supplied = p.get("translated_draft")
        if supplied is not None:
            if not isinstance(supplied, str) or len(supplied) > 2000 or supplied != draft:
                raise ValueError("Translation mismatch: translated draft must exactly match the approved locale template and immutable terms.")
        contract_data = {"id": pid, "kind": kind, "locale": locale, "recipient": recipient, "currency": currency, "amount_cents": cents, "conditions": conditions, "allowed_action": action, "facts": facts, "draft": draft, "source_simulation": source_simulation}
        if source_plan is not None:
            contract_data["source_plan"] = source_plan
        if source_incident is not None:
            contract_data["source_incident"] = source_incident
        contract = json.dumps(contract_data, sort_keys=True, ensure_ascii=False)
        logical_id = hashlib.sha256(contract.encode()).hexdigest()
        if logical_id in logical_ids:
            raise ValueError("Duplicate logical proposal.")
        logical_ids.add(logical_id)
        version = p.get("version", 1)
        if type(version) is not int or version < 1: raise ValueError("Proposal version must be a positive integer.")
        output.append({"id": pid, "logical_id": logical_id, "kind": kind, "action": action, "recipient": recipient, "currency": currency, "amount_cents": cents, "conditions": conditions, "draft": draft, "version": version, "created_at": created_at, "source_simulation": source_simulation, "outcome": "proposal only; no message or offer was sent", "facts_source": "recorded input; not verified"})
        if source_plan is not None:
            output[-1]["source_plan"] = source_plan
        if source_incident is not None:
            output[-1]["source_incident"] = source_incident
    known = {p["id"] for p in output}
    response_rows, response_ids = [], set()
    for row in responses:
        rid, pid = row.get("id"), row.get("proposal_id")
        if not isinstance(rid, str) or not rid or not isinstance(pid, str) or not isinstance(row.get("received_at"), str) or not isinstance(row.get("outcome"), str):
            raise ValueError("Recorded responses require id, proposal_id, received_at and outcome text.")
        try:
            received_dt = datetime.fromisoformat(row["received_at"].replace("Z", "+00:00"))
            if received_dt.tzinfo is None: raise ValueError
        except ValueError as exc:
            raise ValueError("Response received_at must be a timezone-aware timestamp.") from exc
        if rid in response_ids:
            raise ValueError("Duplicate response ID.")
        response_ids.add(rid)
        target = next((p for p in output if p["id"] == pid), None)
        later = [p for p in output if p["created_at"] and target and target["created_at"] and datetime.fromisoformat(p["created_at"].replace("Z", "+00:00")) > datetime.fromisoformat(target["created_at"].replace("Z", "+00:00"))]
        late = bool(target and target["created_at"] and later and received_dt > max(datetime.fromisoformat(p["created_at"].replace("Z", "+00:00")) for p in later))
        classification = "unmatched recorded response; review required" if pid not in known else "late response to an older proposal; newer proposal remains unchanged" if late else "linked recorded response; proposal remains unchanged"
        response_rows.append({"id": rid, "proposal_id": pid, "received_at": row["received_at"], "outcome": row["outcome"], "classification": classification, "changes_proposal": False})
    result = {"proposals": output, "responses": response_rows, "locale": locale, "source_label": "Local proposal only; recorded outcomes remain separate"}
    if history_path is not None:
        path = Path(history_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = []
        if path.exists():
            try:
                existing = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
            except (OSError, ValueError) as exc:
                raise ValueError("Local proposal history is corrupt; refusing to overwrite it.") from exc
            if any(not isinstance(item, dict) or (item.get("record_type", "proposal") == "proposal" and not isinstance(item.get("logical_id"), str)) or (item.get("record_type") == "response" and (not isinstance(item.get("id"), str) or not isinstance(item.get("proposal_id"), str))) for item in existing):
                raise ValueError("Local proposal history schema is corrupt; refusing to overwrite it.")
        seen = {item.get("logical_id") for item in existing if item.get("record_type", "proposal") == "proposal"}
        for proposal in output:
            if proposal["logical_id"] not in seen:
                proposal["version"] = len(existing) + 1
                line = json.dumps(proposal, ensure_ascii=False) + "\n"
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
                existing.append(proposal)
                seen.add(proposal["logical_id"])
        result["history_versions"] = [item for item in existing if item.get("record_type", "proposal") == "proposal"]
        response_history = [item for item in existing if item.get("record_type") == "response"]
        response_ids_seen = {item["id"]: json.dumps(item, sort_keys=True) for item in response_history}
        for response in response_rows:
            record = {"record_type": "response", **response}
            fingerprint = json.dumps(record, sort_keys=True, ensure_ascii=False)
            previous = response_ids_seen.get(response["id"])
            if previous is not None and previous != fingerprint:
                raise ValueError("Conflicting recorded response ID in local history.")
            if previous is None:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                existing.append(record)
                response_ids_seen[response["id"]] = fingerprint
        result["history_responses"] = [item for item in existing if item.get("record_type") == "response"]
    return result
