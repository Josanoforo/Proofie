import hashlib
import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from copilot import config

PROFILES = {
    "cautious": {"actions": ["message"], "max_cents": 5000, "min_evidence": 1},
    "standard": {"actions": ["message", "offer"], "max_cents": 15000, "min_evidence": 1},
    "review_only": {"actions": [], "max_cents": 0, "min_evidence": 99},
}
CENT_CURRENCIES = {"AUD", "BRL", "CAD", "CHF", "CZK", "DKK", "EUR", "GBP", "ILS", "MXN", "MYR", "NOK", "NZD", "PHP", "PLN", "RUB", "SGD", "SEK", "THB", "USD"}
EXAMPLES = [{"profile": "cautious", "case_id": "SYNTH-cautious", "action": "message", "amount_cents": 0, "currency": "USD", "policy_version": "sample-v1", "case_state": "WAITING_FOR_SELLER_RESPONSE", "state_version": "snapshot-1", "deadline": "2026-10-30T12:00:00-06:00", "observed_at": "2026-10-22T12:00:00-06:00", "recorded_allowed_actions": ["message"], "evidence": [{"id": "ev-1", "source": "synthetic fixture", "content": "order snapshot"}]}, {"profile": "standard", "case_id": "SYNTH-standard", "action": "offer", "amount_cents": 2500, "currency": "USD", "policy_version": "sample-v1", "case_state": "WAITING_FOR_SELLER_RESPONSE", "state_version": "snapshot-1", "deadline": "2026-10-30T12:00:00-06:00", "observed_at": "2026-10-22T12:00:00-06:00", "recorded_allowed_actions": ["message", "offer"], "evidence": [{"id": "ev-1", "source": "synthetic fixture", "content": "buyer order and shipping facts"}]}, {"profile": "review_only", "case_id": "SYNTH-review", "action": "message", "amount_cents": 0, "currency": "USD", "policy_version": "sample-v1", "case_state": "WAITING_FOR_SELLER_RESPONSE", "state_version": "snapshot-1", "deadline": "2026-10-30T12:00:00-06:00", "observed_at": "2026-10-22T12:00:00-06:00", "recorded_allowed_actions": [], "evidence": []}]


for _example in EXAMPLES:
    _example["dispute_amount_cents"] = 2500
    _example["dispute_currency"] = "USD"

AMOUNT_HELP = "Enter the amount as a plain number with at most 2 decimals, for example 15.00."


def parse_amount_cents(text):
    """'15.00' -> 1500 by integer arithmetic (no floats). ValueError(AMOUNT_HELP) for anything but digits with an
    optional point and 1-2 decimals."""
    match = re.fullmatch(r"([0-9]{1,9})(?:\.([0-9]{1,2}))?", text.strip()) if isinstance(text, str) else None
    if not match:
        raise ValueError(AMOUNT_HELP)
    return int(match.group(1)) * 100 + int((match.group(2) or "0").ljust(2, "0"))


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


_PROCESS_LOCK = threading.Lock()


def _lock(handle):
    """Exclusive advisory lock on the open lock file: msvcrt on Windows, fcntl elsewhere, and a process-local lock when
    neither module can be imported (an ImportError never escapes to the request)."""
    try:
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return "msvcrt"
    except (ImportError, AttributeError):
        pass
    try:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return "fcntl"
    except (ImportError, AttributeError):
        pass
    _PROCESS_LOCK.acquire()
    return "process"


def _unlock(handle, kind):
    if kind == "msvcrt":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    elif kind == "fcntl":
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    else:
        _PROCESS_LOCK.release()


def _append_locked(path, result):
    path = Path(path)
    owned_root = config.data_dir() / "mejoras10" / "09"
    if owned_root not in path.resolve().parents:
        raise ValueError("Simulation store must remain within the autopilot data folder.")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+b") as lock:
        if lock_path.stat().st_size == 0:
            lock.write(b"0")
            lock.flush()
        kind = _lock(lock)
        try:
            rows = []
            if path.exists():
                try:
                    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
                except (OSError, ValueError) as exc:
                    raise ValueError("Simulation store is corrupt; refusing to take any action.") from exc
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("Simulation store schema is invalid; refusing to take any action.")
            prior = [r for r in rows if r.get("case_id") == result["case_id"] and r.get("decision") == "simulated_action" and r.get("snapshot_version") == 2]
            if prior and any(r.get(k) != result.get(k) for k in ("state_sha256", "policy_sha256") for r in prior[-1:]):
                result = {**result, "decision": "exception", "flags": sorted(set(result["flags"] + ["case state or policy changed since prior simulated action"])), "result": "SIMULATED — NOT SENT; state/policy change requires human review"}
            if result["decision"] == "exception":
                if any(r.get("logical_id") == result["logical_id"] and r.get("decision") == "exception" for r in rows):
                    return next(r for r in rows if r.get("logical_id") == result["logical_id"] and r.get("decision") == "exception")
            else:
                if any(r.get("logical_id") == result["logical_id"] and r.get("decision") == "simulated_action" for r in rows):
                    return next(r for r in rows if r.get("logical_id") == result["logical_id"] and r.get("decision") == "simulated_action")
            with path.open("a", encoding="utf-8") as out:
                out.write(_canonical(result) + "\n")
                out.flush()
                os.fsync(out.fileno())
            return result
        finally:
            _unlock(lock, kind)


def load_inbox(path):
    path = Path(path)
    owned_root = config.data_dir() / "mejoras10" / "09"
    if owned_root not in path.resolve().parents:
        raise ValueError("Simulation inbox must remain within the autopilot data folder.")
    if not path.exists():
        return []
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        if lock_path.stat().st_size == 0:
            lock.write(b"0")
            lock.flush()
        kind = _lock(lock)
        try:
            try:
                rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
            except (OSError, ValueError) as exc:
                raise ValueError("Simulation inbox is corrupt; refusing to hide or reset its history.") from exc
            if any(not isinstance(row, dict) for row in rows):
                raise ValueError("Simulation inbox schema is invalid.")
            return rows
        finally:
            _unlock(lock, kind)


def evaluate(data):
    if not config.feature_enabled("local_autopilot"):
        raise PermissionError("Local autopilot simulation is disabled; enable its feature flag explicitly.")
    try:
        if not isinstance(data, dict) or len(_canonical(data)) > 30000:
            raise ValueError("Policy input must be an object under 30 KB.")
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("Policy"):
            raise
        raise ValueError("Policy input must contain JSON values.") from exc
    profile = data.get("profile")
    if not isinstance(profile, str) or profile not in PROFILES:
        raise ValueError("Unknown merchant profile.")
    policy = data.get("policy", PROFILES[profile])
    if not isinstance(policy, dict) or set(policy) != {"actions", "max_cents", "min_evidence"} or not isinstance(policy["actions"], list) or any(x not in {"message", "offer", "return", "replacement"} for x in policy["actions"]) or len(set(policy["actions"])) != len(policy["actions"]) or type(policy["max_cents"]) is not int or policy["max_cents"] < 0 or type(policy["min_evidence"]) is not int or policy["min_evidence"] < 0:
        raise ValueError("Policy schema is invalid.")
    policy_version = data.get("policy_version")
    case_id, action, currency = data.get("case_id"), data.get("action"), data.get("currency")
    if not all(isinstance(x, str) and x for x in (policy_version, case_id, action)) or len(case_id) > 100 or not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("Case, action, policy version and currency are required.")
    if currency not in CENT_CURRENCIES:
        raise ValueError("This local cent-based policy does not support the recorded case currency.")
    cents = data.get("amount_cents")
    if type(cents) is not int or cents < 0:
        raise ValueError("Amount must be non-negative integer cents.")
    dispute_cents = data.get("dispute_amount_cents")
    dispute_currency = data.get("dispute_currency")
    evidence = data.get("evidence")
    if not isinstance(evidence, list) or len(evidence) > 100 or any(not isinstance(x, dict) or not all(isinstance(x.get(k), str) and x[k] for k in ("id", "source", "content")) for x in evidence):
        raise ValueError("Evidence must contain actual content, source and unique IDs; count-only assertions are not accepted.")
    if len({x["id"] for x in evidence}) != len(evidence):
        raise ValueError("Evidence IDs must be unique.")
    recorded_actions = data.get("recorded_allowed_actions")
    if not isinstance(recorded_actions, list) or len(recorded_actions) > 10 or any(not isinstance(x, str) or x not in {"message", "offer", "return", "replacement"} for x in recorded_actions) or len(set(recorded_actions)) != len(recorded_actions):
        raise ValueError("Recorded allowed actions must be an explicit unique list from the current snapshot.")
    if "conflict" in data and type(data["conflict"]) is not bool:
        raise ValueError("Conflict marker must be boolean.")
    state = data.get("case_state")
    observed, due = data.get("observed_at"), data.get("deadline")
    try:
        observed_dt = datetime.fromisoformat(observed.replace("Z", "+00:00"))
        due_dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
        if observed_dt.tzinfo is None or due_dt.tzinfo is None:
            raise ValueError
    except (AttributeError, ValueError, TypeError):
        raise ValueError("Timezone-aware observed_at and deadline are required.")
    if not isinstance(state, str) or not state:
        raise ValueError("Current recorded case state is required.")
    flags = []
    allowed_actions = set(policy["actions"]) & set(recorded_actions)
    if action not in allowed_actions: flags.append("action not allowed by merchant policy")
    if cents > policy["max_cents"]: flags.append("amount unknown or over limit")
    if type(dispute_cents) is not int or dispute_cents < 0 or dispute_currency != currency:
        flags.append("disputed amount or compatible currency unknown")
    elif cents > dispute_cents:
        flags.append("amount exceeds disputed amount")
    if len(evidence) < policy["min_evidence"]: flags.append("evidence requirement not met")
    if state.upper() not in {"WAITING_FOR_SELLER_RESPONSE", "OPEN"} or data.get("conflict"): flags.append("current state conflict or action stage not open")
    deadline_passed = due_dt < observed_dt
    if deadline_passed: flags.append("deadline passed at current request observation")
    policy_hash = hashlib.sha256(_canonical(policy).encode()).hexdigest()
    evidence_manifest = [{"id": x["id"], "source": x["source"], "sha256": hashlib.sha256(x["content"].encode()).hexdigest()} for x in evidence]
    state_hash = hashlib.sha256(_canonical({"state": state, "dispute_amount_cents": dispute_cents, "dispute_currency": dispute_currency, "state_version": data.get("state_version"), "deadline": due, "deadline_passed": deadline_passed, "conflict": data.get("conflict", False), "recorded_allowed_actions": sorted(recorded_actions)}).encode()).hexdigest()
    dossier_hash = hashlib.sha256(_canonical({"case_id": case_id, "state": state_hash, "evidence": evidence_manifest}).encode()).hexdigest()
    logical = hashlib.sha256(_canonical({"case_id": case_id, "action": action, "amount_cents": cents, "currency": currency, "policy_version": policy_version, "policy_sha256": policy_hash, "state_sha256": state_hash, "dossier_sha256": dossier_hash}).encode()).hexdigest()
    decision = "exception" if flags else "simulated_action"
    return {"snapshot_version": 2, "decision": decision, "case_id": case_id, "logical_id": logical, "profile": profile, "flags": flags, "action": action, "amount_cents": cents, "currency": currency, "policy_version": policy_version, "observed_at": observed, "policy_sha256": policy_hash, "state_sha256": state_hash, "dossier_sha256": dossier_hash, "evidence_manifest": evidence_manifest, "result": "SIMULATED — NOT SENT; the dispute snapshot was read with a GET, nothing was written or sent to PayPal"}


def simulate(data, store_path=None):
    result = evaluate(data)
    return _append_locked(store_path, result) if store_path is not None else result
