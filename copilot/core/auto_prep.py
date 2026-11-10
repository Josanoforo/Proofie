"""Automatic background preparation (approved 2026-10-05). When the inbox
is opened or refreshed, every new dispute in WAITING_FOR_SELLER_RESPONSE (except ids listed in
COPILOT_AUTOPREP_EXCLUDE, copilot/config.py - empty by default, our own demo-reservation data, never
hardcoded here) gets its PayPal context (fetch_context), its checklist (evaluate), and a draft evidence
package built in the background - with the AI if a document is already uploaded and the model/GPU are
free right now, otherwise in manual mode. The inbox shows "Preparing…", "Draft ready — review", or
"Needs documents: …" per dispute.

The rule that never changes: this module never approves and never sends. It only ever produces a draft
for a human to review (copilot/web/routes_package.py's existing approve/send routes are untouched and
still required). Idempotent: one preparation per dispute AND per data version
(copilot/db/repo.py's claim_auto_prep/auto_prep table) - revisiting the inbox with nothing new uploaded
and no PayPal-side change schedules nothing again. Every step is audited as actor='system'.

Interpretation of the three inbox states (the choice is documented here so
it is visible): "needs_documents" means literally zero documents AND zero manual fields exist yet for the
dispute - there is nothing to build even a manual draft FROM meaningfully, so none is created and the
inbox names what is missing. Once at least one document or manual field exists, a draft is always built
(AI if possible, else an empty manual shell) and the status is "ready" - the checklist's own per-field
gaps (e.g. a still-missing tracking number) show on the review screen, not as a separate inbox state.
"""
from __future__ import annotations

import hashlib
import json

from copilot import config
from copilot.core.checklist import UnknownCountryError, evaluate
from copilot.core.checklist_input import cached_rules, docs_for_dispute, extracted_docs_for
from copilot.core.context import fetch_context
from copilot.core.disputes import humanize_evidence_type
from copilot.core.evidence_model import (claims_from_output,
                                         describe_delivery_problem,
                                         find_delivery_claims_citing_conflicting_sources,
                                         find_delivery_claims_citing_untrusted_documents,
                                         find_unsupported_delivery_assertions, propose_package,
                                         untrusted_document_ids)
from copilot.core.vision_model import claims_for_dispute_images
from copilot.db import repo

# the one wording for 'the AI could not be used, continue by hand', shared with the manual
# route (copilot/web/routes_package.py), so a draft made in the background says the same thing.
AI_UNAVAILABLE_FLAG = ("The AI assistant is not available right now. Continuing in manual mode - "
                       "type the values yourself, or try again in a moment.")
HOSTED_AI_OFF_FLAG = "The AI assistant is switched off in this demo. Type the shipment facts yourself."


PREPARED_REASON = "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"       # the one reason a draft is prepared automatically


def ai_unavailable_flag() -> str:
    """The one place the 'AI not used' note is chosen: the hosted demo says the assistant is off (trying again
    cannot help); everywhere else the original wording."""
    return HOSTED_AI_OFF_FLAG if config.hosted_demo() else AI_UNAVAILABLE_FLAG


def compute_version_hash(db_path, dispute_norm: dict) -> str:
    """Fingerprint of everything a preparation would be built from: PayPal's own update_time for the
    dispute, plus this app's own uploaded (non-excluded) document contents and manual-typed fields. Equal
    hash across two visits means nothing a human or PayPal changed since the last preparation."""
    dispute_id = dispute_norm["dispute_id"]
    docs = [d for d in repo.list_documents(db_path, dispute_id) if not d["excluded"]]
    doc_part = sorted((d["id"], d["sha256"]) for d in docs)
    manual = repo.get_manual_fields(db_path, dispute_id)
    payload = {"update_time": dispute_norm.get("update_time"), "documents": doc_part, "manual_fields": manual}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def should_prepare(dispute_norm: dict) -> bool:
    """WAITING_FOR_SELLER_RESPONSE only, and never a dispute listed in COPILOT_AUTOPREP_EXCLUDE
    (copilot/config.py). Reads config.AUTOPREP_EXCLUDE_IDS as a module attribute (config.X), not via
    `from copilot.config import AUTOPREP_EXCLUDE_IDS` - this data must never be hardcoded demo IDs baked
    into product logic, and a bare import would also re-introduce the frozen-at-import-time bug a
    module-level db_path already caused.

    Reversible default: only for the reason the draft is built for, "item or service not received" (the
    package is proof of shipment). For any other reason Proofie starts nothing by itself and the inbox says so; the
    seller can still open the dispute and start by hand."""
    return (dispute_norm.get("status") == "WAITING_FOR_SELLER_RESPONSE"
           and dispute_norm.get("reason") == PREPARED_REASON
           and dispute_norm.get("dispute_id") not in config.AUTOPREP_EXCLUDE_IDS)


# What the seller does to a package by hand (copilot/web/routes_package.py audits each as actor "vendor").
SELLER_PACKAGE_ACTIONS = frozenset({"add_claim", "edit_claim", "remove_claim", "attach_document", "detach_document",
                                    "start_manual_package", "start_manual_package_no_documents",
                                    "start_manual_package_ai_unavailable", "approve_package", "approve_despite_conflict"})


def seller_package(db_path, dispute_id: str) -> int | None:
    """The id of the latest package when it is the seller's own: she approved it (a valid approval), started it by hand,
    or changed it by hand. The background preparation never creates a version on top of such a package: a new version
    would leave her statement or her file on a superseded one, and her approval without a send. None otherwise (no
    package yet, or a draft only the system has touched)."""
    packages = repo.list_packages(db_path, dispute_id)
    if not packages:
        return None
    latest = max(packages, key=lambda p: (p["version"], p["id"]))
    if latest.get("source") == "human_edit" or repo.get_latest_valid_approval(db_path, latest["id"]):
        return latest["id"]
    for row in repo.list_audit(db_path, dispute_id):
        detail = row.get("detail")
        if (row.get("actor") == "vendor" and row.get("action") in SELLER_PACKAGE_ACTIONS
                and isinstance(detail, dict) and detail.get("package_id") == latest["id"]):
            return latest["id"]
    return None


def _kept_for_the_seller(db_path, dispute_id: str, version_hash: str, claim_token: str, stage: str) -> bool:
    held = seller_package(db_path, dispute_id)
    if held is None:
        return False
    repo.set_auto_prep_result(db_path, dispute_id, version_hash, claim_token, "ready", package_id=held)
    repo.log_action(db_path, "system", "auto_prep_kept_seller_package", dispute_id, {"package_id": held, "stage": stage})
    return True


def prepare_dispute(dispute_id: str, dispute_norm: dict, client, db_path, version_hash: str,
                    claim_token: str, *, local_context=None) -> None:
    """The actual background work for one dispute - called from a FastAPI BackgroundTask AFTER the inbox
    response has already been sent, never from the request path itself (so it never blocks the page).
    Every step is audited as actor='system'. Never approves, never sends.

    `version_hash`/`claim_token` are the EXACT values the caller already claimed
    via repo.claim_auto_prep() - every repo.set_auto_prep_result() call below passes both straight
    through, so a result from a slow/crashed/RECLAIMED run can never overwrite a newer claim's row (the
    UPDATE simply matches nothing and is silently a no-op). Also heartbeats (repo.touch_auto_prep)
    between stages and after each vision-processed image, so claim_auto_prep's staleness check measures
    silence, not total runtime - a genuinely slow run (model timeout 300s, ~180s per image with no cap on
    how many) must never be mistaken for a crashed one just because it takes a while."""
    if _kept_for_the_seller(db_path, dispute_id, version_hash, claim_token, "start"):
        return                       # her package stays the latest; she attaches the new file to it herself
    docs, names = docs_for_dispute(db_path, dispute_id)
    manual = repo.get_manual_fields(db_path, dispute_id)
    has_images = any(d["content_type"].startswith("image/") and not d["excluded"]
                     for d in repo.list_documents(db_path, dispute_id))
    if not docs and not manual and not has_images:
        # Name what PayPal is actually asking for (evidence_requested_from_seller), in human
        # language - not just "no documents uploaded yet", which says nothing about WHAT to upload.
        requested = dispute_norm.get("evidence_requested_from_seller") or []
        detail = ("No documents uploaded yet for this dispute.")
        if requested:
            detail = (f"PayPal is asking for: {', '.join(humanize_evidence_type(e) for e in requested)}. "
                      f"No documents uploaded yet.")
        repo.set_auto_prep_result(db_path, dispute_id, version_hash, claim_token, "needs_documents",
                                  detail=detail)
        repo.log_action(db_path, "system", "auto_prep_needs_documents", dispute_id,
                        {"evidence_requested_from_seller": requested})
        return

    try:
        context = local_context if local_context is not None else fetch_context(client, dispute_norm)
    except Exception as exc:   # fetch_context itself never raises for expected PayPal failures (missing
                               # scope, 4xx, timeout - those become limitations); a real programming error
                               # here must be recorded, never crash the background task silently
        repo.set_auto_prep_result(db_path, dispute_id, version_hash, claim_token, "failed",
                                  detail=f"Could not read PayPal context: {exc}")
        repo.log_action(db_path, "system", "auto_prep_failed", dispute_id,
                        {"stage": "context", "error": str(exc)})
        return
    repo.log_action(db_path, "system", "auto_prep_context_fetched", dispute_id,
                    {"limitations": [lim.message for lim in context.limitations]})
    repo.touch_auto_prep(db_path, dispute_id, version_hash, claim_token)   # heartbeat: context done

    try:
        extracted_docs = extracted_docs_for(db_path, dispute_id)
        checks = evaluate(context, extracted_docs, cached_rules())
    except UnknownCountryError as exc:
        repo.set_auto_prep_result(db_path, dispute_id, version_hash, claim_token, "failed", detail=str(exc))
        repo.log_action(db_path, "system", "auto_prep_failed", dispute_id,
                        {"stage": "checklist", "error": str(exc)})
        return
    repo.log_action(db_path, "system", "auto_prep_checklist_built", dispute_id,
                    {"conflicts": sum(1 for c in checks if c.state == "conflict")})
    repo.touch_auto_prep(db_path, dispute_id, version_hash, claim_token)   # heartbeat: checklist done

    def _heartbeat():
        repo.touch_auto_prep(db_path, dispute_id, version_hash, claim_token)

    package_id = None
    ai_failed = False
    if docs and local_context is None:
        try:
            rec = propose_package(dispute_norm, docs, names, label=f"auto_prep:{dispute_id}")
        except Exception as exc:   # the model/GPU being unavailable (pilot's preflight() raises
                                   # RuntimeError when busy/unreachable) or any other model-path failure -
                                   # never a reason to leave the vendor with nothing; fall back to manual
            ai_failed = True
            repo.log_action(db_path, "system", "auto_prep_ai_unavailable", dispute_id, {"error": str(exc)})
        else:
            _heartbeat()   # heartbeat: model call done
            out = rec.get("_output")
            claims = claims_from_output(out)
            next_evidence_index = len(out.get("proposed_evidence", [])) if out else 0
            vision_claims = claims_for_dispute_images(db_path, dispute_id, evidence_index=next_evidence_index,
                                                      on_image_done=_heartbeat)
            claims = claims + vision_claims

            # Surface the SAME things approve_package would otherwise block on, right here - the
            # vendor sees it on the review screen before even trying to approve, not only after.
            docs_sources = {"paypal_dispute": "{}", **docs}
            out_for_check = out or {"proposed_evidence": []}
            # a document the checks do not trust (injection, illegible) can never source a delivery claim
            untrusted = untrusted_document_ids(checks, extracted_docs)
            unsupported = (find_unsupported_delivery_assertions(out_for_check, docs_sources, untrusted) +
                          find_delivery_claims_citing_conflicting_sources(claims, checks) +
                          find_delivery_claims_citing_untrusted_documents(claims, checks, extracted_docs))
            review_flags = list(rec.get("review_flags", []))
            if unsupported:
                review_flags += list(dict.fromkeys(describe_delivery_problem(u) for u in unsupported))

            # the model took its time: if she typed or attached something meanwhile, her package stays the latest
            if _kept_for_the_seller(db_path, dispute_id, version_hash, claim_token, "before_ai_draft"):
                return
            package_id = repo.insert_package(
                db_path, dispute_id, source="ai", validation_ok=rec["validation_ok"],
                review_flags=review_flags, model=rec.get("model"),
                prompt_sha256=rec.get("prompt_sha256"), raw_output=rec.get("raw_output"),
                effective_next_step=rec.get("effective_next_step"))
            if claims:
                repo.insert_claims(db_path, package_id, claims)
            repo.log_action(db_path, "system", "auto_prep_ai_draft", dispute_id,
                            {"package_id": package_id, "validation_ok": rec["validation_ok"],
                             "vision_claims": len(vision_claims),
                             "unsupported_delivery_assertions": len(unsupported)})

    if package_id is None:
        # 'auto_manual' (not 'human_edit') - this shell was created BY THE SYSTEM, with nothing
        # typed into it yet; a human explicitly choosing manual mode (copilot/web/routes_package.py's
        # create_package) still uses 'human_edit'. Lets the inbox (copilot/web/routes_disputes.py) tell "a
        # system-made, still-empty shell" apart from "a human-made or AI-made draft with content in it".
        shell_flags = ["Draft prepared automatically - nothing has been entered yet."]
        if ai_failed:
            # the AI WAS tried and could not be used; the seller must not read this shell as
            # "nobody asked the AI". With no readable document the AI is never tried and no such note is added.
            shell_flags.append(ai_unavailable_flag())
        if _kept_for_the_seller(db_path, dispute_id, version_hash, claim_token, "before_manual_draft"):
            return
        package_id = repo.insert_package(
            db_path, dispute_id, source="auto_manual", validation_ok=False, review_flags=shell_flags)
        repo.log_action(db_path, "system", "auto_prep_manual_draft", dispute_id, {"package_id": package_id})
        # No readable text document (the AI branch above never ran), but there may
        # still be an image on file - a purely additive vision pass, never a reason this draft would not
        # otherwise exist (it already does, as a manual shell).
        vision_claims = [] if local_context is not None else claims_for_dispute_images(db_path, dispute_id, on_image_done=_heartbeat)
        if vision_claims:
            repo.insert_claims(db_path, package_id, vision_claims)
            repo.log_action(db_path, "system", "vision_claims_suggested", dispute_id,
                            {"package_id": package_id, "count": len(vision_claims)})

    repo.set_auto_prep_result(db_path, dispute_id, version_hash, claim_token, "ready", package_id=package_id)


def prepare_dispute_local(db_path, dispute_norm, version_hash, claim_token):
    """Prepare a local manual shell with explicit unknown provider context; no network or model."""
    from copilot.core.context import TransactionContext, Limitation
    dispute_id = dispute_norm.get("dispute_id")
    current = repo.get_auto_prep(db_path, dispute_id) or {}
    if (not should_prepare(dispute_norm) or compute_version_hash(db_path, dispute_norm) != version_hash
            or current.get("status") != "preparing" or current.get("version_hash") != version_hash
            or current.get("claim_token") != claim_token):
        raise ValueError("Local preparation is stale or no longer claimed.")
    for package in repo.list_packages(db_path, dispute_id):
        pid = package["id"]
        if (package.get("source") not in ("ai", "auto_ai", "auto_manual")
                or any(row.get("edited_by_human") for row in repo.list_claims(db_path, pid))
                or repo.list_attachments(db_path, pid) or repo.list_approvals(db_path, pid)):
            raise ValueError("A human package needs review before local preparation.")
    context = TransactionContext({}, (Limitation("local", "Provider context was not refreshed for local preparation."),))
    return prepare_dispute(dispute_id, dispute_norm, None, db_path, version_hash, claim_token, local_context=context)


__all__ = ["compute_version_hash", "should_prepare", "prepare_dispute"]
