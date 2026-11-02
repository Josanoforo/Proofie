"""Sequential orchestration of seller-selected cases using P1's trusted observations."""
from __future__ import annotations

from time import perf_counter

MAX_SELECTION = 3
PENDING_STATES = {"in_progress", "cooldown", "unavailable", "error"}
KNOWN_STATES = PENDING_STATES | {"ready", "needs_input", "stale", "not_eligible", "human_review"}


class InvalidSelection(ValueError):
    pass


def run_selected(db_path, token, case_ids, *, get_observation, prepare):
    """Resolve the entire selection before any work; dependencies are explicit for focused tests."""
    if not isinstance(token, str) or not token or len(token) > 256:
        raise InvalidSelection("The observation has expired. Reload the inbox.")
    if not isinstance(case_ids, (list, tuple)) or not 1 <= len(case_ids) <= MAX_SELECTION:
        raise InvalidSelection("Choose one to three cases.")
    if any(not isinstance(cid, str) or not cid or len(cid) > 64 for cid in case_ids) or len(set(case_ids)) != len(case_ids):
        raise InvalidSelection("Choose distinct cases from this inbox.")
    observations = []
    for cid in case_ids:
        observation = get_observation(token, cid)
        if not isinstance(observation, dict) or observation.get("case_id") != cid:
            raise InvalidSelection("A selected case is unavailable in this observation. Reload the inbox.")
        # A validates membership.  Never use posted case data as an observation.
        observations.append((cid, observation))
    results = []
    started = perf_counter()
    for cid, observation in observations:
        try:
            result = prepare(db_path, observation, expected_version=observation.get("version"))
            if not isinstance(result, dict) or result.get("case_id") != cid or result.get("state") not in KNOWN_STATES:
                raise ValueError("Invalid preparation result")
            # No arbitrary href supplied by a dependency is rendered as navigation.
            results.append({**result, "retryable": result["state"] in PENDING_STATES})
        except Exception:  # isolate a failed case; do not expose sensitive exception text
            results.append({"case_id": cid, "state": "error", "reasons": ["Preparation failed; review this case or retry pending work."], "retryable": True})
    return {"results": results, "selected": list(case_ids), "unprocessed": [], "elapsed_seconds": perf_counter() - started}
