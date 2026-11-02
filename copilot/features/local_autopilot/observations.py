"""Bounded process-local handoff of already observed case data; never a provider cache."""
from __future__ import annotations

import copy
import hashlib
import json
import secrets
import threading
import time
from datetime import datetime

from copilot.core import auto_prep
from copilot.db import repo
from copilot import config
from copilot.core.raw_storage import read_raw
from copilot.web.security import validate_dispute_id

_FIELDS = ("dispute_id", "reason", "status", "update_time", "seller_response_due_date",
           "deadline_parse_ok", "amount_cents", "currency", "evidence_requested_from_seller")
MAX_COLLECTIONS = 20
MAX_CASES = 50
TTL_SECONDS = 300


def human_guard(db_path, case_id):
    """Digest current package identities and seller-visible contents; not an atomic lock."""
    packages = repo.list_packages(db_path, case_id)
    data = []
    for package in packages:
        pid = package["id"]
        claims = repo.list_claims(db_path, pid)
        attachments = repo.list_attachments(db_path, pid)
        approvals = repo.list_approvals(db_path, pid)
        if package.get("source") not in ("ai", "auto_ai", "auto_manual") or any(c.get("edited_by_human") for c in claims) or attachments or approvals:
            data.append({"id": pid, "version": package["version"], "source": package.get("source"),
                         "claims": claims, "attachments": attachments, "approvals": approvals})
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def source_guard(db_path, case_id):
    """Fingerprint current bytes without retaining them; a missing/changed original is not usable."""
    entries = []
    for doc in repo.list_documents(db_path, case_id):
        if doc.get("excluded"): continue
        sha = doc.get("sha256")
        if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            entries.append((doc.get("id"), "invalid", False)); continue
        try:
            raw = read_raw(config.uploads_dir(), sha)
        except OSError:
            raw = None
        actual = hashlib.sha256(raw).hexdigest() if isinstance(raw, bytes) else None
        entries.append((doc.get("id"), actual, actual == sha))
    digest = hashlib.sha256(json.dumps(entries, sort_keys=True).encode("utf-8")).hexdigest()
    return digest, all(entry[2] for entry in entries)


def _normalise(norm):
    if not isinstance(norm, dict) or validate_dispute_id(norm.get("dispute_id")):
        raise ValueError("Invalid case observation")
    result = {}
    for field in _FIELDS:
        value = norm.get(field)
        if field == "amount_cents":
            if value is not None and (type(value) is not int or value < 0): raise ValueError("Invalid recorded amount")
        elif field == "deadline_parse_ok":
            if value is not None and type(value) is not bool: raise ValueError("Invalid deadline state")
        elif field == "evidence_requested_from_seller":
            if value is not None and (not isinstance(value, list) or len(value) > 20 or any(not isinstance(x, str) or len(x) > 100 for x in value)): raise ValueError("Invalid evidence types")
        elif value is not None and (not isinstance(value, str) or len(value) > 150):
            raise ValueError("Invalid case field")
        result[field] = value
    return result


class ObservationStore:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._collections = {}

    def issue(self, db_path, cases, *, observed_at, provenance):
        if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_CASES:
            raise ValueError("Invalid case collection")
        try:
            dt = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            if dt.tzinfo is None: raise ValueError
        except (AttributeError, TypeError, ValueError):
            raise ValueError("Observation needs a timezone-aware timestamp")
        if provenance not in ("provider_read", "synthetic_fixture"):
            raise ValueError("Unknown observation provenance")
        items = {}
        for case in cases:
            norm = _normalise(case.get("norm") if isinstance(case, dict) else None)
            cid = norm["dispute_id"]
            if cid in items: raise ValueError("Duplicate case")
            actions = case.get("actions", [])
            if not isinstance(actions, list) or any(x not in ("offer", "message") for x in actions) or len(set(actions)) != len(actions):
                raise ValueError("Invalid observed actions")
            source_hash, originals_valid = source_guard(db_path, cid)
            items[cid] = {"case_id": cid, "norm": norm, "actions": list(actions), "observed_at": observed_at,
                          "provenance": provenance, "version": auto_prep.compute_version_hash(db_path, norm),
                          "human_guard": human_guard(db_path, cid), "source_guard": source_hash,
                          "originals_valid": originals_valid}
        with self._lock:
            now = self._clock()
            self._collections = {k:v for k,v in self._collections.items() if v[0] > now}
            if len(self._collections) >= MAX_COLLECTIONS: raise ValueError("Observation capacity reached; refresh later")
            token = secrets.token_urlsafe(32)
            self._collections[token] = (now + TTL_SECONDS, items)
            return token

    def get(self, token, case_id):
        with self._lock:
            entry = self._collections.get(token) if isinstance(token, str) else None
            if not entry: return None
            if entry[0] <= self._clock():
                self._collections.pop(token, None)
                return None
            item = entry[1].get(case_id)
            return copy.deepcopy(item) if item else None


_store = ObservationStore()
issue = _store.issue
get = _store.get
