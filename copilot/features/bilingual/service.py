"""Bilingual evidence for a Mexican seller: a Spanish version beside each English statement of a package.

On only with COPILOT_FEATURE_BILINGUAL and the MX rule set (config.RULES_COUNTRY). The Spanish text is for the seller's own
review: PayPal's request and its read-back stay English and byte-identical (the send core never reads this table).

view(db_path, package_id) is the interface the package page calls (lane PK's include hook):
{rows: [{claim_id, position, en, es, es_state, stale, previous_es}], complete} or None when the feature is off. es_state is
"seller" (typed by the seller), "ai" (written by the local model and passed check.parity_problems; the seller reviews it) or
"missing". A Spanish text written for other English words is stale: the row reads "missing" and offers the old text for
editing.

The active approval binds [claim_id, es_state, es] of every row into its review_sha (flag on, MX): approving covers the
English and the Spanish; PayPal's request input never carries the Spanish.

draft_with_model(...) asks the local model for the Spanish of every statement still missing one (local mode only: the hosted
demo refuses before any network attempt). A version that fails the parity check is not stored."""
from __future__ import annotations

import hashlib

from copilot import config
from copilot.db import repo
from copilot.features.bilingual import check, model, store

FLAG = "bilingual"
COUNTRY = "MX"
MAX_LENGTH = check.MAX_LENGTH
EMPTY_MESSAGE = "Type the Spanish version before saving."
TOO_LONG_MESSAGE = f"The Spanish version must be {MAX_LENGTH} characters or fewer."
STATE_SELLER, STATE_AI, STATE_MISSING = "seller", "ai", "missing"


def enabled() -> bool:
    return config.feature_enabled(FLAG) and (config.RULES_COUNTRY or "").strip().upper() == COUNTRY


def english_sha256(text: str) -> str:
    """The fingerprint of the English words a Spanish version was written for (surrounding whitespace ignored)."""
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()


def _normalise(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def view(db_path, package_id) -> dict | None:
    if not enabled():
        return None
    package = repo.get_package(db_path, package_id) if str(package_id).isdigit() else None
    if package is None:
        return None
    claims = repo.list_claims(db_path, package["id"])
    hashes = {c["id"]: english_sha256(c["text"]) for c in claims}
    by_claim = store.latest_by_claim(db_path, hashes)
    versions = [p["id"] for p in repo.list_packages(db_path, package["dispute_id"])]
    by_english = store.latest_by_english(db_path, versions, set(hashes.values()))
    rows = []
    for position, claim in enumerate(claims, 1):
        en_hash = hashes[claim["id"]]
        own = by_claim.get(claim["id"])
        found = own if own and own["en_sha256"] == en_hash else by_english.get(en_hash)
        stale = found is None and own is not None
        rows.append({"claim_id": claim["id"], "position": position, "en": claim["text"],
                     "es": found["es"] if found else "",
                     "es_state": found["source"] if found else STATE_MISSING,
                     "stale": stale, "previous_es": own["es"] if stale else ""})
    return {"rows": rows, "complete": bool(rows) and all(r["es_state"] != STATE_MISSING for r in rows)}


def save_seller_spanish(db_path, dispute_id: str, package_id: int, claim_id: int, text: str) -> dict:
    """Stores the seller's Spanish version of one statement of this package. ValueError carries the field message;
    LookupError: the statement is not in this package; PermissionError: the feature is off."""
    if not enabled():
        raise PermissionError("the bilingual feature is off")
    claim = next((c for c in repo.list_claims(db_path, package_id) if c["id"] == int(claim_id)), None)
    if claim is None:
        raise LookupError("statement not in this package")
    es = _normalise(text)
    if not es:
        raise ValueError(EMPTY_MESSAGE)
    if len(es) > MAX_LENGTH:
        raise ValueError(TOO_LONG_MESSAGE)
    saved = store.save(db_path, package_id, claim["id"], english_sha256(claim["text"]), es, STATE_SELLER)
    repo.log_action(db_path, "vendor", "save_spanish_statement", dispute_id,
                    {"package_id": int(package_id), "claim_id": claim["id"], "source": STATE_SELLER})
    return saved


def draft_with_model(db_path, dispute_id: str, package_id: int, call=None, seed: int = 7) -> dict:
    """One model call per statement whose Spanish is missing (or stale), in statement order. Returns {made, refused
    (statement positions whose answer failed the parity check), error (model.UNAVAILABLE / model.MALFORMED or None)}. The
    first unavailable answer stops the run. One audit row per call (actor system): model, seed, raw hash, validation result,
    never the text."""
    if not enabled():
        raise PermissionError("the bilingual feature is off")
    shown = view(db_path, package_id)
    summary = {"made": 0, "refused": [], "error": None}
    for row in (shown or {}).get("rows", []):
        if row["es_state"] != STATE_MISSING:
            continue
        result = model.call_spanish(row["en"], seed=seed, call=call)
        problems = check.parity_problems(row["en"], result.es) if result.es is not None else []
        ok = result.es is not None and not problems
        repo.log_action(db_path, "system", "draft_spanish_statement", dispute_id,
                        {"package_id": int(package_id), "claim_id": row["claim_id"], "model": result.model,
                         "seed": result.seed, "raw_sha256": result.raw_sha256, "validation_ok": ok,
                         "problems": len(problems), "error": result.error})
        if result.error == model.UNAVAILABLE:
            summary["error"] = result.error
            break
        if result.error:
            summary["error"] = result.error
            summary["refused"].append(row["position"])
            continue
        if problems:
            summary["refused"].append(row["position"])
            continue
        store.save(db_path, package_id, row["claim_id"], english_sha256(row["en"]), _normalise(result.es), STATE_AI,
                   model=result.model)
        summary["made"] += 1
    return summary
