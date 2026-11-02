"""Reuse the core fingerprint/writer and refuse unsafe or stale review submissions."""
from __future__ import annotations

import threading
import json
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone

from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.core.checklist import ACKNOWLEDGEABLE_DATE_CHECKS, UnknownCountryError, evaluate
from copilot.core.checklist_input import cached_rules, docs_for_dispute, extracted_docs_for
from copilot.core.context import Field, SRC_DISPUTE
from copilot.core.disputes import normalize
from copilot.core.evidence_model import (find_delivery_claims_citing_conflicting_sources,
                                         find_delivery_claims_citing_untrusted_documents,
                                         find_unsupported_delivery_assertions, untrusted_document_ids)
from copilot.core.send import (approval_fingerprint_with_attachments, build_request_input, pick_tracking,
                              sha256_json, verify_after_with_attachments)
from copilot.db import repo
from copilot.features.active_approval import rules, store
from copilot.features.claims.model import digest, is_private, snapshot
from copilot.features.claims.service import compose, document_names
from copilot.web import status
from copilot.web.routes_package import _attachment_shas, _checks_for, _no_rule_set_line, _open_flags

# Avoid redundant same-process reviews; SQLite also serializes separate processes.
_APPROVE_LOCK = threading.RLock()
SENT_VERSION_MESSAGE = "This version was sent to PayPal, so it cannot be approved again."
SOURCES_UNCHECKED_PART = "PayPal sources could not be checked again"


class ReviewError(ValueError):
    pass


@dataclass
class Review:
    package: dict
    claims: list
    checks: list
    request_input: dict | None
    fingerprint: str
    review_sha256: str
    reasons: tuple
    blocked: tuple
    flags: list
    approval: dict | None
    receipt: dict | None
    cleared: bool
    case_evidence: dict
    # one dict per blocked delivery statement (number, phrase, kind, filename, claim_id); `blocked`
    # itself stays a tuple of strings.
    blocked_details: tuple = ()
    # PayPal's own read lists the provide-evidence action (display only: the send re-checks it)
    can_provide_evidence: bool = True
    # the Spanish version of each statement, {claim_id: row of bilingual.view}; None unless COPILOT_FEATURE_BILINGUAL is on
    # with the MX rule set. Bound into review_sha below; never part of request_input or the send fingerprint.
    spanish: dict | None = None
    # Display-only comparison with the stale approval; populated after digest calculation.
    view_changes: tuple = ()
    # Read-only limitations from the same PayPal GET used to build checks; presentation only.
    context_limitations: tuple = ()
    # Existing PayPal read, used only for the case header; never included in the approval digest.
    case_display: tuple = ()


def sent_tracking_numbers(request_input: dict | None) -> list:
    """The tracking numbers inside the evidence request this app sends (evidence_info.tracking_info)."""
    return [t.get("tracking_number") for e in (request_input or {}).get("evidences", [])
            for t in ((e.get("evidence_info") or {}).get("tracking_info") or []) if t.get("tracking_number")]


def tracker_numbers(answer) -> list:
    """The tracking numbers of a PayPal trackers answer (`{"trackers": [...]}`)."""
    items = answer.get("trackers") if isinstance(answer, dict) else None
    return [t["tracking_number"] for t in items or [] if isinstance(t, dict) and t.get("tracking_number")]


class OwnTrackerMask:
    """PayPal registers the tracking number of a provide-evidence request as a tracker of the
    transaction (observed in the sandbox: GET trackers [] before the send, one MERCHANT tracker with
    exactly the number we sent, status SHIPPED, last_updated at the second of the POST, after it). That is the send's
    OWN effect, so the post-send comparison must not report it as a foreign change.

    The ONE field excluded: a tracker entry of the transaction's trackers answer (the context_tracking read) that was
    NOT present before the send (`baseline`) and whose tracking_number is one this app sent in the evidence
    (`sent`). Nothing else is touched: a tracker with another number, a tracker that already existed before the send
    (whatever changes in it), a changed transaction fact, document, statement or evidence entry still differs. The
    dispute's own status, stage, links and allowed options are NOT excluded because they never enter the checks or
    the review digest (verified by replaying the recorded answers: only the tracker flipped tracking_number, carrier and
    delivery_status). The pre-send check is not loosened: this wrapper is used only after the send or on a finished
    reservation whose approval recorded the baseline."""
    def __init__(self, client, sent, baseline):
        self._client, self._sent, self._baseline = client, set(sent), set(baseline)

    def call(self, method, path, op, **kwargs):
        result = self._client.call(method, path, op, **kwargs)
        if op == "context_tracking" and isinstance(result, dict) and isinstance(result.get("trackers"), list):
            kept = [t for t in result["trackers"] if not (
                isinstance(t, dict) and t.get("tracking_number") in self._sent
                and t.get("tracking_number") not in self._baseline)]
            if len(kept) != len(result["trackers"]):
                return {**result, "trackers": kept}
        return result


def proposed_note(review) -> str:
    """The note the approval page offers, already typed, for the open notices (COPILOT_FEATURE_SELLER_SHORTCUTS): built by
    code from the notices themselves (what they say), never from model text, so the seller edits it or replaces it instead of
    retyping. Empty when there is no open notice or the text would carry private contact or credential text. It is a
    proposal: the approval still needs the seller's own click on each statement and on Approve, and the note stays whatever
    the seller submits."""
    if not review.flags:
        return ""
    said = "; ".join(" ".join(str(flag).split()).rstrip(".") for flag in review.flags)
    note = ("Open notices read against the sources: " + said + ".")[:280].rstrip()
    return "" if is_private(note) else note


def _date_difference_flags(checks) -> list:
    return ["Review the difference: " + c.seller_message for c in checks
            if c.state == "conflict" and c.id in ACKNOWLEDGEABLE_DATE_CHECKS]


def _attempt_confirmed(db_path, dispute_id: str, request_input) -> bool:
    """True when the attempt registry holds this exact content as confirmed by PayPal's own read-back."""
    attempts = SQLiteAttemptStore(db_path)
    try:
        row = attempts.read(dispute_id, sha256_json(request_input))
    finally:
        attempts.close()
    return bool(row and row.get("state") == "confirmed")


def _blocked_details(db_path, dispute_id: str, rows: list, problems: list) -> tuple:
    """One entry per blocked delivery statement, found by claim id (never by matching text): the statement number in
    the package's own order, the word that claims delivery, why it is blocked, and the cited file where one is named."""
    numbers = {row["id"]: n for n, row in enumerate(rows, start=1)}
    names = {str(d["id"]): d["filename"] for d in repo.list_documents(db_path, dispute_id)}
    details, seen = [], set()
    for problem in problems:
        claim_id = problem.get("claim_id")
        if claim_id not in numbers:
            continue
        key = (claim_id, problem.get("matched_phrase", "").lower(), problem.get("kind"))
        if key in seen:
            continue
        seen.add(key)
        doc_id = next(iter(problem.get("doc_ids") or ()), None)
        details.append({"number": numbers[claim_id], "phrase": problem.get("matched_phrase", ""),
                        "kind": problem.get("kind", "unsupported"),
                        "filename": names.get(str(doc_id)) if doc_id is not None else None,
                        "claim_id": claim_id})
    return tuple(sorted(details, key=lambda d: d["number"]))


def _view_changes_since_approval(db_path, dispute_id: str, claims: list, receipt: dict | None) -> tuple:
    """Describe stale statement/document hashes for the page only; never feeds review or send fingerprints."""
    if not receipt:
        return ()
    try:
        confirmations = json.loads(receipt.get("confirmations_json") or "[]")
    except (TypeError, ValueError):
        return ()
    if not isinstance(confirmations, list):
        return ()
    prior = {row.get("key"): row for row in confirmations if isinstance(row, dict) and row.get("key")}
    documents = {str(row["id"]): row["filename"] for row in repo.list_documents(db_path, dispute_id)}
    changes = []
    for claim in claims:
        old = prior.get(claim.key)
        if not old:
            continue
        statement_changed = old.get("sentence_sha256") != digest(claim.text)
        old_sources = {(x.get("artifact_id"), x.get("field")): x for x in old.get("sources", [])
                       if isinstance(x, dict) and isinstance(x.get("artifact_id"), str)
                       and x["artifact_id"].startswith("doc_")}
        current_sources = {(x.artifact_id, x.field): x for x in claim.sources if x.artifact_id.startswith("doc_")}
        files = []
        for key, before in old_sources.items():
            now = current_sources.get(key)
            old_sha = before.get("sha256")
            new_sha = now.sha256 if now else None
            if old_sha == new_sha:
                continue
            artifact_id = key[0]
            filename = documents.get(artifact_id.removeprefix("doc_"), "Document no longer available")
            if is_private(filename):
                filename = "Private document name hidden"
            files.append({"filename": filename, "field": key[1], "old_sha256": old_sha, "current_sha256": new_sha})
        if statement_changed or files:
            changes.append({"claim_id": claim.claim_id, "statement": claim.text,
                            "statement_changed": statement_changed, "files": files})
    return tuple(changes)


def read(client, db_path, dispute_id: str, package_id: int) -> Review:
    package = repo.get_package(db_path, package_id)
    if not package or package["dispute_id"] != dispute_id:
        raise ReviewError("Open a package belonging to this dispute.")
    raw = client.call("GET", f"/v1/customer/disputes/{dispute_id}", "show_dispute")
    norm = normalize(raw, datetime.now(timezone.utc))
    digest_norm = norm
    rows = repo.list_claims(db_path, package_id)
    extracted = extracted_docs_for(db_path, dispute_id)
    shas = _attachment_shas(db_path, package_id)
    try:
        request_input = build_request_input(rows, *pick_tracking(extracted))
        fingerprint = approval_fingerprint_with_attachments(dispute_id, "provide_evidence", request_input, shas)
    except ValueError:
        request_input = None
        fingerprint = f"not-sendable-yet-{package_id}"
    approval = repo.get_latest_valid_approval(db_path, package_id)
    receipt = store.receipt(db_path, approval["id"]) if approval else None
    evidence_keys = ("evidence_requested_from_seller", "evidence_submitted_by_seller")
    mask_client = None
    # A verified landing is an outcome, not an edit to what the seller approved.
    # Restore only our own evidence categories in the review digest. New requests,
    # other evidence, amounts, transaction reads, files and local inputs still bind.
    if (receipt and request_input and approval["approval_sha256"] == fingerprint
            and store.reservation_state(db_path,approval["id"]) == "finished"):
        confirmations = json.loads(receipt["confirmations_json"])
        approved_case = confirmations[0].get("approved_case_evidence") if confirmations else None
        filenames = [a["filename"] for a in repo.list_attachments(db_path, package_id)]
        remaining = list(raw.get("evidences") or [])
        previous_entries = approved_case.get("entries") if isinstance(approved_case,dict) else None
        preserved = isinstance(previous_entries,list)
        for previous in previous_entries or []:
            if not isinstance(previous,dict):
                preserved = False
                break
            match = next((i for i,e in enumerate(remaining) if digest(e) == previous.get("sha256")), None)
            if match is not None:
                remaining.pop(match)
            elif not previous.get("replaceable_requested"):
                preserved = False
        landed = verify_after_with_attachments({"evidences":remaining}, request_input, filenames)
        if (isinstance(approved_case, dict) and all(isinstance(approved_case.get(k), list)
                and all(isinstance(v, str) for v in approved_case[k]) for k in evidence_keys)
                and preserved and len(remaining) == 1 and landed["content_ok"] and landed["files_ok"]):
            own_types = {e["evidence_type"] for e in request_input["evidences"]}
            digest_norm = dict(norm)
            for key in evidence_keys:
                digest_norm[key] = [v for v in (norm.get(key) or []) if v not in own_types]
                digest_norm[key] += [v for v in approved_case[key] if v in own_types]
            baseline = approved_case.get("trackers")
            if "trackers" not in approved_case and _attempt_confirmed(db_path, dispute_id, request_input):
                # an approval recorded by an earlier version has no trackers baseline. The attempt registry shows
                # this exact content was confirmed, so the send is what registered the tracker.
                baseline = []
            if isinstance(baseline, list) and all(isinstance(v, str) for v in baseline):
                # our own send made PayPal register the tracking we sent: not an edit of what was approved
                mask_client = OwnTrackerMask(client, sent_tracking_numbers(request_input), baseline)
    try:
        # what the seller sees (checks, statements, blocks, notices) is computed from PayPal's real answer.
        # The mask below only shapes the digest, which decides whether the approval still stands.
        checks, context = _checks_for(norm, dispute_id, client, db_path)
        digest_checks, digest_context = checks, context
        if mask_client is not None:
            digest_checks, digest_context = _checks_for(norm, dispute_id, mask_client, db_path)
    except UnknownCountryError:
        raise ReviewError(_no_rule_set_line()) from None
    docs, _ = docs_for_dispute(db_path, dispute_id)
    out = {"proposed_evidence": [{"notes": "", "claims": rows}]}
    unsupported = (find_unsupported_delivery_assertions(out, {"paypal_dispute": "{}", **docs},
                                                        untrusted_document_ids(checks, extracted))
                   + find_delivery_claims_citing_conflicting_sources(rows, checks)
                   + find_delivery_claims_citing_untrusted_documents(rows, checks, extracted))
    claims = compose(db_path, dispute_id, package_id, checks, norm)
    blocked = []
    from copilot import config
    if config.feature_enabled("carrier_required"):
        from copilot.features.carrier_required.service import MESSAGE, carrier_missing
        tracking_number, carrier = pick_tracking(extracted)
        if tracking_number and carrier_missing(carrier):
            blocked.append(MESSAGE)
    blocked_details = _blocked_details(db_path, dispute_id, rows, unsupported)
    if unsupported:
        blocked.append("A statement asserts delivery, signature or receipt without a trusted matching source. Edit it first.")
    if any(c.state == "conflict" and c.id not in ACKNOWLEDGEABLE_DATE_CHECKS for c in checks):
        blocked.append("A check conflicts with the source data. Review and fix the difference before approving.")
    if not claims:
        blocked.append("Add at least one statement before reviewing.")
    names = document_names(db_path, dispute_id, claims)
    if any(is_private(c.text) or any(is_private(s.artifact_id) or is_private(names[s.artifact_id]) or is_private(s.field)
                                   or (s.value and is_private(s.value)) for s in c.sources) for c in claims):
        blocked.append("A statement or source includes private contact or credential text. Edit it before approving.")
    open_flags = _open_flags(package, rows, repo.get_manual_fields(db_path, dispute_id))
    flags = [*open_flags, *_date_difference_flags(checks)]
    reasons = rules.typed_reasons(norm.get("amount_cents"), norm.get("currency"), [c.text for c in claims])
    if digest_norm is not norm:
        fields = dict(digest_context.fields)
        for norm_key, key in zip(evidence_keys, ("evidence_requested", "evidence_submitted")):
            values = digest_norm.get(norm_key)
            if values:
                fields[key] = Field(values, SRC_DISPUTE)
            else:
                fields.pop(key, None)
        digest_context = replace(digest_context, fields=fields)
        digest_checks = evaluate(digest_context, extracted, cached_rules())
    # The notices bound into the digest come from the same (masked) checks as the rest of the digest.
    digest_flags = [*open_flags, *_date_difference_flags(digest_checks)]
    # Current checks/claims remain visible; the verified outcome mask belongs
    # only to source-binding computation, not to the seller's current readback.
    digest_claims = (compose(db_path, dispute_id, package_id, digest_checks, digest_norm)
                     if digest_norm is not norm or mask_client is not None else claims)
    claim_snapshot = snapshot(digest_claims)
    stable_facts = {k: norm.get(k) for k in ("dispute_id", "amount_cents", "currency", "reason")}
    for claim in claim_snapshot:
        for source in claim["sources"]:
            if source["kind"] == "paypal_dispute" and source["field"] != "status":
                # A citation of the amount/id must not inherit an unrelated status
                # transition. A claim actually citing status remains status-bound.
                source["sha256"] = digest(stable_facts)
    digest_input = {"package_id": package_id, "claims": claim_snapshot, "checks": [asdict(c) for c in digest_checks],
                    "readings": [asdict(d) for d in extracted], "context": asdict(digest_context),
                    "fingerprint": fingerprint, "attachments": shas, "reasons": reasons,
                    "flags": digest_flags}
    spanish = None
    if config.feature_enabled("bilingual"):
        # lazy: off by default. Flag off or not MX: no "spanish" key, so the digest is exactly what it was before.
        from copilot.features.bilingual import service as bilingual
        spanish_view = bilingual.view(db_path, package_id)
        if spanish_view is not None:
            spanish = {r["claim_id"]: r for r in spanish_view["rows"]}
            digest_input["spanish"] = [[r["claim_id"], r["es_state"], r["es"]] for r in spanish_view["rows"]]
    review_sha = digest(digest_input)
    cleared = bool(approval and (approval["approval_sha256"] != fingerprint or not receipt
                                 or receipt["review_sha256"] != review_sha))
    view_changes = _view_changes_since_approval(db_path, dispute_id, claims, receipt) if cleared else ()
    # A source-only edit is not necessarily in the core send fingerprint; invalidate the standard row
    # whenever this review discovers stale provenance so the untouched send path refuses it.
    if cleared:
        repo.invalidate_approval(db_path, approval["id"])
        repo.log_action(db_path, "system", "invalidate_active_approval", dispute_id,
                        {"package_id": package_id, "approval_id": approval["id"]})
        approval, receipt = None, None
    return Review(package, claims, checks, request_input, fingerprint, review_sha, reasons,
                  tuple(blocked), flags, approval, receipt, cleared,
                  {**{k: list(digest_norm.get(k) or []) for k in evidence_keys},
                   "trackers": list(context.get("tracking_numbers_all") or
                                    ([context.get("tracking_number")] if context.get("tracking_number") else [])),
                   "entries": [{"sha256":digest(e), "replaceable_requested":
                                e.get("source") == "REQUESTED_FROM_SELLER" and
                                e.get("evidence_type") in {r["evidence_type"] for r in (request_input or {}).get("evidences",[])}}
                               for e in (raw.get("evidences") or [])]},
                  blocked_details=blocked_details,
                  can_provide_evidence=bool(norm.get("can_provide_evidence")) or "provide-evidence" in (norm.get("action_links") or {}),
                  spanish=spanish, view_changes=view_changes,
                  context_limitations=tuple(lim.message for lim in context.limitations),
                  case_display=(norm, raw))


def approve(client, db_path, dispute_id, package_id, review_sha, choices: dict, phrase: str, override: str, *, read_all=None):
    with _APPROVE_LOCK:
        package = repo.get_package(db_path, package_id)
        if not package or package["dispute_id"] != dispute_id:
            raise ReviewError("Open a package belonging to this dispute.")
        # the approval of a version PayPal holds statements of is spent: a confirmed send (also with a file
        # attached afterwards, which is not in PayPal) and a true partial send. The same rule the passive approval route
        # and every edit route use (status.is_sent_record).
        record = status.send_record(db_path, dispute_id, package)
        if status.is_sent_record(record):
            raise ReviewError(SENT_VERSION_MESSAGE)
        local_sha = store.local_snapshot(db_path, dispute_id, package_id)
        review = read(client, db_path, dispute_id, package_id)
        if review.review_sha256 != review_sha:
            raise ReviewError("The content or its sources changed. Review each statement again.")
        if review.blocked:
            raise ReviewError(" ".join(review.blocked))
        if read_all is not None:
            # one control (COPILOT_FEATURE_SELLER_SHORTCUTS): "I have read these N statements". N is the number the page showed;
            # the decisions are made by code, one per statement, exactly as a tick on each would have made them. The review
            # hash above already binds the statements themselves, so a statement more, less or edited is refused there.
            if read_all != len(review.claims):
                raise ReviewError("The number of statements changed. Read them again.")
            choices = {c.key: ("seller_statement" if c.verdict == "unverifiable" else "confirmed") for c in review.claims}
        keys = {c.key for c in review.claims}
        if set(choices) != keys:
            raise ReviewError("Confirm every statement individually against its source.")
        for c in review.claims:
            expected = "seller_statement" if c.verdict == "unverifiable" else "confirmed"
            if choices[c.key] != expected:
                raise ReviewError("An unverifiable statement must be acknowledged as your own statement or edited.")
        if review.reasons and phrase != rules.PHRASE:
            raise ReviewError("Type the displayed confirmation phrase exactly.")
        if review.flags and not override.strip():
            raise ReviewError("Open notices require a short note explaining your decision.")
        if is_private(override):
            raise ReviewError("Keep private contact and credential text out of the note.")
        confirmations = [{"key": c.key, "claim_id": c.claim_id, "sentence_sha256": digest(c.text),
                          "claim_basis": c.claim_basis, "verdict": c.verdict, "decision": choices[c.key],
                          "sources": [{"artifact_id": s.artifact_id, "field": s.field, "sha256": s.sha256,
                                       "value_sha256": digest(s.value), "check_basis": s.check_basis,
                                       "check_state": s.check_state} for s in c.sources]} for c in review.claims]
        acknowledged = [{"check_id": c.id, "note": override.strip()} for c in review.checks
                        if c.state == "conflict" and c.id in ACKNOWLEDGEABLE_DATE_CHECKS]
        if confirmations:
            confirmations[0]["acknowledged_differences"] = acknowledged
            confirmations[0]["approved_case_evidence"] = review.case_evidence
            if read_all is not None:
                confirmations[0]["read_control"] = {"statements_read": read_all}      # the receipt says it was one control
        with store.transaction(db_path) as conn:
            if store.local_snapshot(db_path, dispute_id, package_id, conn=conn) != local_sha:
                raise ReviewError("The content or its sources changed. Review each statement again.")
            import time
            live = conn.execute("SELECT s.created_at FROM active_send_reservations s "
                                "JOIN approvals a ON a.id=s.approval_id "
                                "WHERE a.package_id=? AND s.state='reserved'", (package_id,)).fetchall()
            if any(not (time.time() - row[0] > RESERVATION_MAX_AGE_SECONDS) for row in live):
                raise ReviewError("A send is still reserved. Check its outcome before approving again.")
            previous = store.previous(db_path, package_id, review_sha, conn=conn)
            if previous:
                return previous["id"]
            approval_id = repo.insert_approval(db_path, package_id, review.fingerprint, "vendor",
                                               override.strip() or None, conn=conn)
            store.save(db_path, approval_id, package_id, review_sha, review.reasons,
                       rules.PHRASE if review.reasons else None, confirmations, conn=conn)
            repo.log_action(db_path, "vendor", "approve_package", dispute_id,
                            {"package_id": package_id, "approval_id": approval_id,
                             "sufficiency_override": bool(override.strip()), "via": "active_approval"}, conn=conn)
        return approval_id


RESERVATION_MAX_AGE_SECONDS = 300


@dataclass
class SendReservation:
    changed: bool = False
    change_description: str = ""
    reread_failed: bool = False          # the sources could not be read again after the POST (nothing was compared)
    differences: tuple = ()              # what the re-read actually found different (empty when it could not read)

    def note(self) -> str | None:
        """The secondary line for a send PayPal confirmed although the approval no longer holds. By cause: a
        difference that was read, or sources that could not be checked again (never called a change)."""
        if self.differences:
            text = f"Changed after sending: {'; '.join(self.differences)}; nothing was re-sent."
            return text + (" The sources could not be checked again after sending." if self.reread_failed else "")
        if self.reread_failed:
            return "The sources could not be checked again after sending; review them. Nothing was re-sent."
        return None


class _Fixed:
    """A client that answers one recorded response (lets OwnTrackerMask filter an answer already read)."""
    def __init__(self, answer):
        self._answer = answer

    def call(self, method, path, op, **kwargs):
        return self._answer


def send_guard(client, db_path, dispute_id, package_id, approval_id):
    """Bind a short durable reservation; hold no database connection across the POST."""
    import time
    import uuid
    import copy
    initial_evidences = []
    initial_trackers = []
    evidence_changed = [False]
    class ReviewClient:
        def __init__(self, after=False):
            self.after = after
        def call(self, method, path, op, **kwargs):
            result = client.call(method, path, op, **kwargs)
            if op == "context_tracking":
                if not self.after:
                    initial_trackers[:] = tracker_numbers(result)
                else:
                    # PayPal registers the tracking we sent as a tracker; see OwnTrackerMask for the one field excluded
                    result = OwnTrackerMask(_Fixed(result), sent_tracking_numbers(review.request_input),
                                            initial_trackers).call(method, path, op)
                return result
            if op == "show_dispute" and isinstance(result, dict):
                if not self.after:
                    initial_evidences[:] = copy.deepcopy(result.get("evidences", []))
                else:
                    evidence_changed[0] = (result.get("evidences") or []) != initial_evidences
                    remaining = copy.deepcopy(result.get("evidences") or [])
                    preserved = True
                    for earlier in initial_evidences:
                        if earlier in remaining:
                            remaining.remove(earlier)
                        elif not (earlier.get("source") == "REQUESTED_FROM_SELLER"
                                  and earlier.get("evidence_type") in
                                  {e["evidence_type"] for e in review.request_input["evidences"]}):
                            preserved = False
                    filenames = [a["filename"] for a in repo.list_attachments(db_path, package_id)]
                    landed = verify_after_with_attachments({"evidences": remaining}, review.request_input, filenames)
                    # Only the newly submitted matching entry is an outcome. Earlier seller
                    # entries and every other source must remain unchanged; extra keys added
                    # by PayPal to our own readback follow the core verification semantics.
                    if preserved and len(remaining) == 1 and landed["content_ok"] and landed["files_ok"]:
                        result = {**result, "evidences": copy.deepcopy(initial_evidences)}
                        evidence_changed[0] = False
            return result
    local_sha = store.local_snapshot(db_path, dispute_id, package_id)
    review = read(ReviewClient(), db_path, dispute_id, package_id)
    if review.blocked or not review.approval or review.approval["id"] != approval_id:
        raise ReviewError("Review the current statements and sources before sending.")
    token = uuid.uuid4().hex
    state = SendReservation()

    @contextmanager
    def reserve_sources():
        with store.transaction(db_path) as conn:
            if store.local_snapshot(db_path, dispute_id, package_id, conn=conn) != local_sha:
                raise ReviewError("The content or its sources changed. Review each statement again.")
            current = conn.execute("SELECT id FROM approvals WHERE package_id=? AND invalidated_at IS NULL "
                                   "ORDER BY id DESC LIMIT 1", (package_id,)).fetchone()
            receipt = conn.execute("SELECT review_sha256 FROM active_approval_receipts WHERE approval_id=?",
                                   (approval_id,)).fetchone()
            if not current or current[0] != approval_id or not receipt or receipt[0] != review.review_sha256:
                raise ReviewError("Review the current statements and sources before sending.")
            existing = conn.execute("SELECT created_at FROM active_send_reservations WHERE approval_id=?",
                                    (approval_id,)).fetchone()
            if existing:
                raise ReviewError("This approval already reserved a send. Check its outcome; review again before a new send.")
            conn.execute("INSERT INTO active_send_reservations VALUES (?, ?, ?, ?, ?, 'reserved')",
                         (approval_id, token, time.time(), local_sha, review.review_sha256))
        try:
            with store.transaction(db_path) as conn:
                row = conn.execute("SELECT created_at, state FROM active_send_reservations WHERE token=?", (token,)).fetchone()
                if not row or row["state"] != "reserved" or not 0 <= time.time() - row["created_at"] <= RESERVATION_MAX_AGE_SECONDS:
                    raise ReviewError("The send reservation expired. Check the outcome and review again.")
                if store.local_snapshot(db_path, dispute_id, package_id, conn=conn) != local_sha:
                    raise ReviewError("The content or its sources changed. Review each statement again.")
            # Both reservation transactions and connections are closed before the POST.
            yield state
        finally:
            try:
                after = read(ReviewClient(after=True), db_path, dispute_id, package_id)
                remote_changed = evidence_changed[0] or after.review_sha256 != review.review_sha256
                changed_parts = []
                if remote_changed:
                    if snapshot(after.claims) != snapshot(review.claims):
                        changed_parts.append("statements or the sources they cite")
                    if after.checks != review.checks:
                        changed_parts.append("shipment checks or PayPal transaction facts")
                    if after.fingerprint != review.fingerprint:
                        changed_parts.append("the package content or attachments")
                    if not changed_parts:
                        changed_parts.append("the approved source record")
            except Exception:
                remote_changed = True
                state.reread_failed = True
                changed_parts = [SOURCES_UNCHECKED_PART]
            with store.transaction(db_path) as conn:
                row = conn.execute("SELECT created_at, state FROM active_send_reservations WHERE token=?", (token,)).fetchone()
                state.changed = (remote_changed or not row or row["state"] != "reserved"
                                 or not 0 <= time.time() - row["created_at"] <= RESERVATION_MAX_AGE_SECONDS
                                 or store.local_snapshot(db_path, dispute_id, package_id, conn=conn) != local_sha)
                if store.local_snapshot(db_path, dispute_id, package_id, conn=conn) != local_sha:
                    changed_parts.append("local package content, shipment details or source files")
                if not row or row["state"] != "reserved" or not 0 <= time.time() - row["created_at"] <= RESERVATION_MAX_AGE_SECONDS:
                    changed_parts.append("the send reservation is no longer current")
                state.change_description = "; ".join(dict.fromkeys(changed_parts))
                state.differences = tuple(part for part in dict.fromkeys(changed_parts) if part != SOURCES_UNCHECKED_PART)
                conn.execute("UPDATE active_send_reservations SET state=? WHERE token=?",
                             ("changed" if state.changed else "finished", token))
                if state.changed:
                    conn.execute("UPDATE approvals SET invalidated_at=? WHERE id=? AND invalidated_at IS NULL",
                                 (repo._now(), approval_id))
                    repo.log_action(db_path, "system", "active_send_sources_changed", dispute_id,
                                    {"package_id": package_id, "approval_id": approval_id,
                                     "requires_review": True, "automatic_retry": False}, conn=conn)
    return reserve_sources()
