"""Pilot: build, send and verify an approved evidence package (sandbox only).

  python step5_send_evidence.py build --run-id ID --dispute dispute.json   -> evidence/package_<id>.json
  python step5_send_evidence.py send --package FILE --approval "text" [--confirm-review-flags]
                                                                             -> POST provide-evidence, then GET
  python step5_send_evidence.py verify --package FILE                      -> GET only (use after an unknown outcome)
  python step5_send_evidence.py negative --code ERRDIS047 --package FILE   -> simulated PayPal error, nothing real changes

Rules enforced here, in code:
- `send` refuses without --approval (the human's approval text is written to the evidence log with the package hash).
- `send` refuses if `review_flags` is non-empty unless `--confirm-review-flags` is also given.
- The approval is checked against `approval_sha256` = fingerprint(dispute_id, action, exact content); any edit
  to the package after `build` (including the target dispute) invalidates it.
- Preconditions are read from a fresh GET, not the build-time dispute.json: status WAITING_FOR_SELLER_RESPONSE,
  PayPal's own `provide_evidence` link present, requested type matches, reason/amount unchanged since build,
  nothing already submitted by the seller. The POST goes to the href PayPal gave.
- A persistent attempt record under evidence/send_attempts/ (pending -> sent_unconfirmed -> confirmed|failed),
  keyed by (dispute_id, package hash), means a second call - another process, a retry, a resumed run after a
  crash - never POSTs again blindly: it reconciles from PayPal's own GET first.
- The result always separates three facts: the HTTP response (2xx), the dispute's general `status`, and
  whether the exact content appears in `evidences[]` as SUBMITTED_BY_SELLER - only the third is "received".
Use pilot/lock_inspect.py for read-only lock facts and the verify command; it never authorizes a resend.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

import step4_evidence_ai as ai
from client import EVIDENCE, SANDBOX, PayPalError, SandboxClient, readback_audit, write_audit
from step3_dispute import _expected_capture, normalize

DEMO_PREFIX = "DEMO DATA (sandbox test with synthetic documents). "   # added by code, not by the model
REQUEST_NS = uuid.UUID("5b0e5c0a-3f55-4b62-9c3f-2c2f6a1d0b7e")
RETRYABLE = {429, 500, 502, 503, 504}
ATTEMPTS_DIR = EVIDENCE / "send_attempts"


# ------------------------------------------------------------------------------------------ pure helpers
def build_request_input(model_output: dict) -> dict:
    """PayPal `input` part from a validated model output. Only PROOF_OF_FULFILLMENT with tracking is supported."""
    ev = next((e for e in model_output["proposed_evidence"]
               if e["evidence_type"] == "PROOF_OF_FULFILLMENT" and e["tracking_info"]), None)
    if ev is None:
        raise ValueError("no PROOF_OF_FULFILLMENT with tracking_info in the package")
    t = ev["tracking_info"][0]
    notes = DEMO_PREFIX + ev["notes"].strip()
    if len(notes) > 2000:
        raise ValueError("notes exceed PayPal's 2000-character limit")
    return {"evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT",
                           "evidence_info": {"tracking_info": [{"carrier_name": t["carrier_name"],
                                                                "tracking_number": t["tracking_number"]}]},
                           "notes": notes}]}


def sha256_json(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def request_id_for(dispute_id: str, request_sha: str) -> str:
    return str(uuid.uuid5(REQUEST_NS, f"{dispute_id}:{request_sha}"))


def approval_fingerprint(dispute_id: str, action: str, req_input: dict) -> str:
    """The approval is recorded against the fingerprint of dispute target + action + exact
    content (notes, tracking fields). Any edit to dispute_id, action or content changes this hash, so a
    package tampered with after build() no longer matches the approval recorded against it."""
    return sha256_json({"dispute_id": dispute_id, "action": action, "paypal_request_input": req_input})


def check_preconditions(norm: dict, req_input: dict, dispute_id: str, expected_capture: str | None,
                        pkg_reason: str | None = None, pkg_amount_cents: int | None = None) -> list[str]:
    """pkg_reason/pkg_amount_cents: the reason and amount recorded in the package at build() time.
    When given, the fresh GET must still show the same reason and amount, not just the same status/link -
    the build-time dispute.json could be stale by the time `send` runs."""
    p = []
    ev_type = req_input["evidences"][0]["evidence_type"]
    if norm["dispute_id"] != dispute_id:
        p.append(f"dispute id mismatch: PayPal returned {norm['dispute_id']}")
    if norm["status"] != "WAITING_FOR_SELLER_RESPONSE":
        p.append(f"status is {norm['status']}, not WAITING_FOR_SELLER_RESPONSE")
    if not norm["can_provide_evidence"]:
        p.append("PayPal did not give a provide_evidence link")
    if ev_type not in norm["evidence_requested_from_seller"]:
        p.append(f"{ev_type} is not among the evidence PayPal requested from the seller "
                 f"({norm['evidence_requested_from_seller']})")
    if norm["evidence_submitted_by_seller"]:
        p.append(f"evidence already submitted by the seller: {norm['evidence_submitted_by_seller']}")
    if expected_capture and norm["capture_matches_expected"] is not True:
        p.append("disputed transaction is not the expected capture")
    if norm["overdue"]:
        p.append("response deadline already passed")
    if pkg_reason is not None and norm["reason"] != pkg_reason:
        p.append(f"reason changed since the package was built: now {norm['reason']!r}, was {pkg_reason!r}")
    if pkg_amount_cents is not None and norm["amount_cents"] != pkg_amount_cents:
        p.append(f"amount changed since the package was built: now {norm['amount_cents']!r}, "
                 f"was {pkg_amount_cents!r}")
    return p


def verify_after(raw_dispute: dict, req_input: dict) -> tuple[bool, dict]:
    """True only if PayPal's own GET lists the same evidence as submitted by the seller."""
    want = req_input["evidences"][0]
    want_trk = want["evidence_info"]["tracking_info"][0]["tracking_number"]
    for e in raw_dispute.get("evidences") or []:
        if e.get("source") != "SUBMITTED_BY_SELLER" or e.get("evidence_type") != want["evidence_type"]:
            continue
        trks = [t.get("tracking_number") for t in ((e.get("evidence_info") or {}).get("tracking_info") or [])]
        if want_trk in trks:
            return True, {"evidence_type": e["evidence_type"], "tracking_numbers": trks, "date": e.get("date"),
                          "notes_match": (e.get("notes") or "").strip() == want["notes"].strip()}
    return False, {"seller_submitted": [(e.get("evidence_type"), e.get("source"))
                                        for e in raw_dispute.get("evidences") or []]}


def classify(exc: PayPalError) -> str:
    return "retryable" if exc.status in RETRYABLE else "not_retryable"


# -------------------------------------------------------------------------------- persistent attempt registry
# Duplicate protection cannot depend on a disabled button in one process. A send attempt for a
# given (dispute_id, package_sha256) is tracked on disk through pending -> sent_unconfirmed -> confirmed|failed.
# Two concurrent processes, or the same process run twice, serialise on this file instead of both POSTing.
def _attempt_path(dispute_id: str, package_sha: str) -> Path:
    return ATTEMPTS_DIR / f"{dispute_id}__{package_sha}.json"


def _read_attempt(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


_REPLACE_TRIES = 10
_REPLACE_PAUSE_S = 0.02


def _write_attempt(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique per writer (pid + thread + uuid): threads of one process must not share a temp file.
    tmp = path.with_name(path.name + f".tmp{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex}")
    try:
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        for attempt in range(_REPLACE_TRIES):
            try:
                os.replace(tmp, path)  # atomic on both POSIX and Windows
                return
            except PermissionError:  # Windows: target briefly held open by a concurrent reader/replacer
                if attempt == _REPLACE_TRIES - 1:
                    raise
                time.sleep(_REPLACE_PAUSE_S)
    finally:
        tmp.unlink(missing_ok=True)  # only our own unique temp; after a successful replace it is already gone


def _claim_attempt(path: Path, dispute_id: str, package_sha: str) -> bool:
    """Atomically create a 'pending' record. False means one already exists (another process/run claimed
    it first) - the caller must reconcile from PayPal's GET, never POST blindly."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps({"dispute_id": dispute_id, "package_sha256": package_sha, "state": "pending",
                            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, ensure_ascii=False))
    return True


def _multipart(req_input: dict) -> dict:
    return {"input": (None, json.dumps(req_input, ensure_ascii=False), "application/json")}


def _get(c: SandboxClient, dispute_id: str, op: str, *, log_extra: dict | None = None) -> dict:
    return c.call("GET", f"/v1/customer/disputes/{dispute_id}", op,
                  **({"log_extra": log_extra} if log_extra is not None else {}))


def _norm(raw: dict) -> dict:
    return normalize(raw, datetime.now(timezone.utc), _expected_capture())


# --------------------------------------------------------------------------------------------- commands
def build(run_id: str, dispute_path: str, fixtures: Path = ai.FIXTURES) -> Path:
    rec = next((json.loads(l) for l in (EVIDENCE / "step4_ai.jsonl").read_text(encoding="utf-8").splitlines()
                if json.loads(l)["run_id"] == run_id), None)
    if rec is None or not rec["validation_ok"]:
        raise SystemExit(f"run {run_id} not found or not validated")
    dispute = {k: v for k, v in json.loads(Path(dispute_path).read_text(encoding="utf-8")).items()
               if k != "action_links"}
    docs, names = ai.load_docs(fixtures, None, [])
    if names != rec["documents"]:
        raise SystemExit("documents differ from the ones used in that run")
    prompt_sha = hashlib.sha256(json.dumps(ai.build_messages(dispute, docs, names), sort_keys=True).encode()).hexdigest()[:16]
    if prompt_sha != rec["prompt_sha256"]:
        raise SystemExit(f"inputs changed since the run (prompt {prompt_sha} != {rec['prompt_sha256']})")
    out = json.loads(rec["raw_output"])
    checks = ai.validate(out, dispute, docs)          # re-validated now, not trusted from the log
    if not all(c["ok"] for c in checks):
        raise SystemExit(f"re-validation failed: {[c for c in checks if not c['ok']]}")
    req_input = build_request_input(out)
    pkg = {"package_version": 1, "dispute_id": dispute["dispute_id"], "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "dispute_reason": dispute.get("reason"), "dispute_amount_cents": dispute.get("amount_cents"),
           "source_run": {k: rec[k] for k in ("run_id", "label", "seed", "model", "prompt_sha256")},
           "review_flags": rec.get("review_flags", []),
           "document_sha256": {n: hashlib.sha256((fixtures / n).read_bytes()).hexdigest()[:16] for n in names.values()},
           "paypal_request_input": req_input, "paypal_request_sha256": sha256_json(req_input),
           "approval_sha256": approval_fingerprint(dispute["dispute_id"], "provide_evidence", req_input),
           "claims_for_reviewer": [c for e in out["proposed_evidence"] for c in e["claims"]],
           "reviewer_note_draft_not_sent": out["reviewer_note_draft"]}
    path = EVIDENCE / f"package_{run_id}.json"
    path.write_text(json.dumps(pkg, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def send(c: SandboxClient, pkg: dict, approval: str, confirm_review_flags: bool = False) -> tuple[int, str]:
    """Own the entire lifecycle, including GET and clean-rejection retry, across processes.

    A crashed owner leaves its lock behind deliberately: absence of evidence is not proof
    that its POST did not land. Use verify; never steal an abandoned or live lock.
    """
    if not approval or not approval.strip():
        return 1, "REFUSED: --approval is required"
    path = _attempt_path(pkg["dispute_id"], pkg["paypal_request_sha256"])
    lock = path.with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return 3, "OUTCOME PENDING: live or abandoned owner exists; use verify, no automatic retry"
    try:
        return _send_owned(c, pkg, approval, confirm_review_flags)
    finally:
        os.close(fd)
        lock.unlink()


def _send_owned(c: SandboxClient, pkg: dict, approval: str, confirm_review_flags: bool = False) -> tuple[int, str]:
    """Returns (exit_code, message). 0 = sent and verified; 1 = not sent; 2 = 2xx but not confirmed (retry
    `verify` later); 3 = outcome pending, cannot safely determine, no automatic retry.

    Three distinct facts are never conflated: the HTTP response (2xx), the dispute's general
    `status`, and whether PayPal's own GET shows the exact content received (`evidences[]`,
    source=SUBMITTED_BY_SELLER, matching tracking/notes) - only the third counts as "evidence received"."""
    if not approval or not approval.strip():
        return 1, "REFUSED: --approval is required (the human's approval must be recorded)"
    flags = pkg.get("review_flags") or []
    if flags and not confirm_review_flags:
        return 1, ("REFUSED: review_flags is not empty, requires --confirm-review-flags after the human has "
                   f"seen them: {flags}")
    req_input, dispute_id = pkg["paypal_request_input"], pkg["dispute_id"]
    if sha256_json(req_input) != pkg["paypal_request_sha256"]:
        return 1, "REFUSED: package was modified after it was built (hash mismatch)"
    approval_sha = approval_fingerprint(dispute_id, "provide_evidence", req_input)
    if "approval_sha256" in pkg and approval_sha != pkg["approval_sha256"]:
        return 1, "REFUSED: approval fingerprint mismatch (dispute_id, action or content changed since build)"
    package_sha = pkg["paypal_request_sha256"]
    apath = _attempt_path(dispute_id, package_sha)

    before_raw = _get(c, dispute_id, "pre_send_get", log_extra=readback_audit(package_sha, approval_sha))
    before = _norm(before_raw)
    content_ok, content_details = verify_after(before_raw, req_input)

    try:
        existing = _read_attempt(apath)
        if existing is None and apath.exists():
            return 3, "OUTCOME PENDING: invalid null attempt record; no automatic retry"
        if existing is not None and (
            not isinstance(existing, dict)
            or existing.get("dispute_id") != dispute_id
            or existing.get("package_sha256") != package_sha
            or existing.get("state") not in {"pending", "sent_unconfirmed", "confirmed", "failed"}
        ):
            return 3, "OUTCOME PENDING: invalid attempt record; no automatic retry"
    except (ValueError, OSError):
        return 3, "OUTCOME PENDING: unreadable or incomplete attempt record; no automatic retry"
    if existing is not None:
        if content_ok:  # PayPal already has this exact content, from this or an earlier attempt/process
            _write_attempt(apath, {**existing, "state": "confirmed"})
            return 0, (f"ALREADY SENT AND VERIFIED (reconciled from a prior attempt, state was "
                       f"{existing['state']!r}; no new POST issued): {content_details}")
        if existing["state"] == "failed":
            pass  # a clean PayPal rejection: nothing was received, safe to reclaim and retry below
        else:
            return 3, (f"OUTCOME PENDING: a previous attempt (state {existing['state']!r}) exists, the fresh "
                       f"GET does not show this content - cannot "
                       f"determine safely; no automatic retry. Investigate manually (PayPal-Request-Id would "
                       f"be {request_id_for(dispute_id, package_sha)}).")
    else:
        if not _claim_attempt(apath, dispute_id, package_sha):
            return 3, ("OUTCOME PENDING: a concurrent attempt for this exact package just started; re-run "
                       "`verify` instead of sending again")

    problems = check_preconditions(before, req_input, dispute_id, _expected_capture(),
                                   pkg.get("dispute_reason"), pkg.get("dispute_amount_cents"))
    if problems:
        _write_attempt(apath, {"dispute_id": dispute_id, "package_sha256": package_sha, "state": "failed",
                               "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "reason": "preconditions"})
        return 1, "NOT SENT (preconditions): " + "; ".join(problems)
    link = before["action_links"]["provide-evidence"]
    if not link["href"].startswith(SANDBOX + "/") or link["method"].upper() != "POST":
        _write_attempt(apath, {"dispute_id": dispute_id, "package_sha256": package_sha, "state": "failed",
                               "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "reason": "unexpected link"})
        return 1, f"NOT SENT: unexpected provide-evidence link {link}"
    path, rid = link["href"][len(SANDBOX):], request_id_for(dispute_id, package_sha)
    c._log({"op": "approval_recorded", "dispute_id": dispute_id, "package_sha256": package_sha,
            "approval_sha256": approval_sha, "approval_text": approval.strip(), "request_id": rid,
            "status_before": before["status"], "review_flags": flags,
            "review_flags_confirmed": bool(flags and confirm_review_flags)})
    # Written BEFORE the network call: if the process dies mid-POST, the next run sees "sent_unconfirmed"
    # (not "pending") and reconciles from PayPal's GET instead of silently retrying.
    _write_attempt(apath, {"dispute_id": dispute_id, "package_sha256": package_sha, "state": "sent_unconfirmed",
                           "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "request_id": rid})
    try:
        resp = c.call("POST", path, "provide_evidence", multipart=_multipart(req_input), request_id=rid,
                      log_extra={"request_input": req_input, **write_audit(package_sha, approval_sha)})
    except PayPalError as e:
        _write_attempt(apath, {"dispute_id": dispute_id, "package_sha256": package_sha, "state": "failed",
                               "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "request_id": rid,
                               "http_status": e.status})
        return 1, f"NOT SENT: PayPal {e.status} issue={e.issue} debug_id={e.debug_id} ({classify(e)})"
    except httpx.TransportError as e:
        # state stays sent_unconfirmed on disk: outcome genuinely unknown, protocol says reconcile, no auto-retry
        return 3, (f"OUTCOME PENDING ({type(e).__name__}): do not assume it was sent or not sent; run `verify`, "
                   f"or re-run `send` later (it will reconcile via GET, never blindly re-POST) "
                   f"(PayPal-Request-Id {rid})")
    after_raw = _get(c, dispute_id, "post_send_get", log_extra=readback_audit(package_sha, approval_sha))
    content_ok, details = verify_after(after_raw, req_input)
    after = _norm(after_raw)
    http_ok = True  # c.call() already raised on status >= 400; reaching here means a 2xx response
    if not content_ok:
        return 2, (f"HTTP 2xx (http_ok={http_ok}) and status moved {before['status']} -> {after['status']}, "
                   f"but the follow-up GET does not yet show the submitted content: NOT CONFIRMED {details}")
    _write_attempt(apath, {"dispute_id": dispute_id, "package_sha256": package_sha, "state": "confirmed",
                           "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "request_id": rid})
    return 0, (f"EVIDENCE SENT AND VERIFIED (http_ok={http_ok}; status {before['status']} -> {after['status']}; "
               f"content confirmed by GET: {details}); request_id {rid}")


def verify_only(c: SandboxClient, pkg: dict) -> tuple[int, str]:
    raw = _get(c, pkg["dispute_id"], "verify_get", log_extra=readback_audit(pkg["paypal_request_sha256"],
                                                                            pkg.get("approval_sha256")))
    ok, details = verify_after(raw, pkg["paypal_request_input"])
    return (0, f"VERIFIED: {details}") if ok else (1, f"NOT SHOWN by PayPal: {details}")


def negative(c: SandboxClient, pkg: dict, code: str) -> tuple[int, str]:
    """POST the package body to a PayPal test path (ERRDIS...). Success here would be a bug: nothing is 'sent'."""
    if not re.fullmatch(r"ERRDIS\d{3}", code or ""):
        return 1, f"REFUSED: {code!r} is not an ERRDIS test value"
    before = verify_after(_get(c, pkg["dispute_id"], "neg_before_get"), pkg["paypal_request_input"])[0]
    try:
        c.call("POST", f"/v1/customer/disputes/{code}/provide-evidence", "provide_evidence_" + code,
               multipart=_multipart(pkg["paypal_request_input"]),
               log_extra={"simulated_error_code": code, "request_sha256": pkg["paypal_request_sha256"]})
        return 1, f"UNEXPECTED 2xx for {code}"
    except PayPalError as e:
        after = verify_after(_get(c, pkg["dispute_id"], "neg_after_get"), pkg["paypal_request_input"])[0]
        return 0, (f"{code}: handled as error, NOT marked sent. PayPal {e.status} issue={e.issue} "
                   f"debug_id={e.debug_id} class={classify(e)}; real dispute shows evidence before={before} "
                   f"after={after}")


def main(argv: list[str]) -> int:
    def opt(name, default=None):
        return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else default
    cmd = argv[0] if argv else ""
    if cmd == "build" and opt("--run-id") and opt("--dispute"):
        print("package written:", build(opt("--run-id"), opt("--dispute")))
        return 0
    if cmd in ("send", "verify", "negative") and opt("--package"):
        pkg = json.loads(Path(opt("--package")).read_text(encoding="utf-8"))
        c = SandboxClient("step5_send")
        if cmd == "send":
            code, msg = send(c, pkg, opt("--approval", ""), confirm_review_flags="--confirm-review-flags" in argv)
        elif cmd == "verify":
            code, msg = verify_only(c, pkg)
        else:
            code, msg = negative(c, pkg, opt("--code", ""))
        print(msg)
        return code
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
