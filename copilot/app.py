"""App entry point. Run with: .venv\\Scripts\\python.exe -m copilot.app  (binds 127.0.0.1 only; the
LocalhostOnlyMiddleware below is the defense-in-depth backstop if that host is ever
changed without reading why).
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import httpx
import re
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from urllib.parse import urlsplit
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from copilot import config
from copilot.core.disputes import PayPalError
from copilot.web.routes_disputes import CredentialsMissing, last_paypal_read_label, path_dispute_id, templates
from copilot.web.routes_disputes import router as disputes_router
from copilot.web.routes_documents import router as documents_router
from copilot.web.routes_package import router as package_router
from copilot.web.routes_send import router as send_router
from copilot.web.security import (ACCESS_COOKIE_NAME, AccessAttemptLimiter, HostedAccessMiddleware, HostedPoolMiddleware,
                                  LocalhostOnlyMiddleware, access_cookie_value, new_csrf_token, safe_next,
                                  set_csrf_cookie, verify_csrf)
from copilot.web import security

HOST = "127.0.0.1"
PORT = 8000

logger = logging.getLogger(__name__)

# The hosted demo's text for both credentials pages (missing and rejected): a visitor cannot fix the host's settings.
HOSTED_NO_CREDENTIALS_STATE = "hosted-no-credentials"
HOSTED_NO_CREDENTIALS_MESSAGE = ("The host has not set up working PayPal sandbox credentials yet, so the demo cannot read "
                               "PayPal right now; come back later.")


class RevalidatedStaticFiles(StaticFiles):
    """The stylesheet and the script change with the app: a browser that kept an older copy must ask again (the ETag answers 304 when
    nothing changed), so a redeploy never leaves a page half old and half new."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app() -> FastAPI:
    config.load_env_file()
    hosted = config.hosted_demo()
    if hosted:
        if len(config.access_code()) < 12:
            raise RuntimeError("COPILOT_ACCESS_CODE is required in the hosted demo (at least 12 characters)")
        # No /docs, /redoc or /openapi.json on the host: they list every route and make the browser load a CDN.
        app = FastAPI(title="Proofie", docs_url=None, redoc_url=None, openapi_url=None)
    else:
        app = FastAPI(title="Proofie")
    templates.env.globals["replay_case"] = config.replay_case          # the links to the hosted replay; None when it is off
    app.include_router(disputes_router)
    app.include_router(documents_router)
    app.include_router(package_router)
    app.include_router(send_router)
    if config.feature_enabled("account_cliff"):
        # lazy: off by default; imported only when its flag is on
        from copilot.features.account_cliff.routes import router as account_cliff_router
        app.include_router(account_cliff_router)
    if config.feature_enabled("fulfilment"):
        # lazy: off by default; imported only when its flag is on
        from copilot.features.fulfilment.routes import router as fulfilment_router
        app.include_router(fulfilment_router)
    if config.feature_enabled("resolution"):
        # lazy: off by default; imported only when its flag is on
        from copilot.features.resolution.routes import router as resolution_router
        app.include_router(resolution_router)
        # the prepared status message: the registered tracking as a message to review (or a not-sent note) (D-101 B5)
        if config.feature_enabled("resolution_status_message"):
            from copilot.features.resolution.status_message import router as status_message_router
            app.include_router(status_message_router)
    if config.feature_enabled("outcome"):
        # lazy: off by default; imported only when its flag is on
        from copilot.features.outcome.routes import router as outcome_router
        app.include_router(outcome_router)
        from copilot.features.outcome.money_routes import router as money_router; app.include_router(money_router)  # noqa: E702 - money kept, lost, held
    if config.feature_enabled("webhooks"):
        # lazy: off by default; imported only when its flag is on
        from copilot.features.webhooks.routes import router as webhooks_router
        app.include_router(webhooks_router)
    if config.feature_enabled("draft"):
        # lazy: off by default; imported only when its flag is on
        from copilot.features.draft.routes import router as draft_router
        app.include_router(draft_router)
    # --- feature blocks, first group: below this line ---
    if config.feature_enabled("claims"):
        from copilot.features.claims.routes import router as claims_router
        app.include_router(claims_router)
    if config.feature_enabled("active_approval"):
        from copilot.features.active_approval.routes import register as register_active_approval
        register_active_approval(app)
    if config.feature_enabled("triage"):
        from copilot.features.triage.routes import router as triage_router
        app.include_router(triage_router)
    if config.feature_enabled("ledger"):
        from copilot.features.ledger.routes import router as ledger_router
        app.include_router(ledger_router)

    if config.feature_enabled("package_print"):
        from copilot.features.package_print.routes import router as package_print_router
        app.include_router(package_print_router)

    # --- delivery facts for manual review ---
    if config.feature_enabled("delivery_facts"):
        from copilot.features.delivery_facts.routes import router as delivery_facts_router
        app.include_router(delivery_facts_router)

    # --- feature blocks, second group: below this line ---
    if config.feature_enabled("conformance"):
        from copilot.features.conformance.routes import router as conformance_router
        app.include_router(conformance_router)
    if config.feature_enabled("events"):
        from copilot.features.events.routes import router as events_router
        app.include_router(events_router)
    if config.feature_enabled("deadlines"):
        from copilot.features.deadlines.routes import router as deadlines_router
        app.include_router(deadlines_router)

    # --- carrier-required approval gate ---
    if config.feature_enabled("carrier_required"):
        from copilot.features.carrier_required.routes import register as register_carrier_required
        register_carrier_required(app)

    # --- hosted demo only: a labelled replay of a recorded local-model run (static page, no database) ---
    if config.feature_enabled("replay"):
        if config.hosted_demo():
            from copilot.features.replay.routes import router as replay_router
            app.include_router(replay_router)

    # --- hosted demo: explicitly labelled, synthetic attachments derived from the recorded sale date ---
    if config.feature_enabled("demo_documents"):
        if config.hosted_demo():
            from copilot.features.demo_documents.routes import router as demo_documents_router
            app.include_router(demo_documents_router)

    # --- hosted demo only: judge mode, a pool of open sandbox disputes (routes answer 404 while COPILOT_JUDGE_POOL is empty) ---
    if config.feature_enabled("judge_pool"):
        if config.hosted_demo():
            from copilot.features.judge_pool.routes import register as register_judge_pool
            register_judge_pool(app)

    # --- the inbox can watch for a case that has just been opened (D-101 r2, R2-D) ---
    if config.feature_enabled("inbox_watch"):
        from copilot.features.explainable_queue.watch import router as inbox_watch_router
        app.include_router(inbox_watch_router)

    # --- local, seeded demo case only: one confirmed click proposes the case's documents and shipment fields (D-101 r1) ---
    if config.feature_enabled("demo_documents_seeded"):
        if not config.hosted_demo():
            from copilot.features.demo_documents.seeded import router as seeded_demo_router
            app.include_router(seeded_demo_router)

    # --- Mexico: the seller's Spanish version of each statement, kept inside the approval (never sent to PayPal) ---
    if config.feature_enabled("bilingual"):
        from copilot.features.bilingual.routes import router as bilingual_router
        app.include_router(bilingual_router)

    # Local offline tools, each behind its own flag and lazily imported.
    if config.feature_enabled("evidence_dossier"):
        from copilot.features.evidence_dossier.routes import router as evidence_dossier_router
        app.include_router(evidence_dossier_router)
    if config.feature_enabled("explainable_queue"):
        from copilot.features.explainable_queue.routes import router as explainable_queue_router
        app.include_router(explainable_queue_router)
    if config.feature_enabled("negotiation_drafts"):
        from copilot.features.negotiation_drafts.routes import router as negotiation_drafts_router
        app.include_router(negotiation_drafts_router)
    if config.feature_enabled("sale_readiness"):
        from copilot.features.sale_readiness.routes import router as sale_readiness_router
        app.include_router(sale_readiness_router)
    if config.feature_enabled("appeal_workspace"):
        from copilot.features.appeal_workspace.routes import router as appeal_workspace_router
        app.include_router(appeal_workspace_router)
    if config.feature_enabled("mcp_local_contracts"):
        from copilot.features.mcp_local_contracts.routes import router as mcp_local_contracts_router
        app.include_router(mcp_local_contracts_router)
    if config.feature_enabled("evaluation_workbench"):
        from copilot.features.evaluation_workbench.routes import router as evaluation_workbench_router
        app.include_router(evaluation_workbench_router)
    if config.feature_enabled("judge_walkthrough"):
        from copilot.features.judge_walkthrough.routes import router as judge_walkthrough_router
        app.include_router(judge_walkthrough_router)
    if config.feature_enabled("local_autopilot"):
        from copilot.features.local_autopilot.routes import router as local_autopilot_router
        app.include_router(local_autopilot_router)
    if config.feature_enabled("local_demo_hub"):
        from copilot.features.local_demo_hub.routes import router as local_demo_hub_router
        app.include_router(local_demo_hub_router)

    # --- nivel2 additions ---
    if config.feature_enabled("judge_as_buyer"):
        from copilot.features.judge_as_buyer.routes import router as judge_as_buyer_router
        app.include_router(judge_as_buyer_router)
    if config.feature_enabled("buyer_note"):
        from copilot.features.buyer_note.routes import router as buyer_note_router
        app.include_router(buyer_note_router)

    if config.feature_enabled("ship_moment_capture"):
        from copilot.features.ship_moment_capture.routes import router as ship_moment_capture_router
        app.include_router(ship_moment_capture_router)

    if config.feature_enabled("carrier_directory"):
        from copilot.features.carrier_directory.routes import router as carrier_directory_router
        app.include_router(carrier_directory_router)

    if config.feature_enabled("sale_lesson"):
        from copilot.features.sale_lesson.routes import router as sale_lesson_router
        app.include_router(sale_lesson_router)

    if config.feature_enabled("call_script"):
        from copilot.features.call_script.routes import router as call_script_router
        app.include_router(call_script_router)

    if config.feature_enabled("read_aloud_review"):
        from copilot.features.read_aloud_review.routes import router as read_aloud_review_router
        app.include_router(read_aloud_review_router)

    if config.feature_enabled("second_opinion"):
        from copilot.features.second_opinion.routes import router as second_opinion_router
        app.include_router(second_opinion_router)

    if config.feature_enabled("accounting_line"):
        from copilot.features.accounting_line.routes import router as accounting_line_router
        app.include_router(accounting_line_router)
    if config.feature_enabled("offline_market_mode"):
        from copilot.features.offline_market_mode.routes import router as offline_market_mode_router
        app.include_router(offline_market_mode_router)
    if config.feature_enabled("declared_limits"):
        from copilot.features.declared_limits.routes import router as declared_limits_router
        app.include_router(declared_limits_router)
    if config.feature_enabled("presentation_mode"):
        from copilot.features.presentation_mode.routes import router as presentation_mode_router
        app.include_router(presentation_mode_router)
    if config.feature_enabled("two_clocks"):
        from copilot.features.two_clocks.routes import router as two_clocks_router
        app.include_router(two_clocks_router)
    if config.feature_enabled("do_nothing_path"):
        from copilot.features.do_nothing_path.routes import router as do_nothing_path_router
        app.include_router(do_nothing_path_router)
    if config.feature_enabled("package_insert"):
        from copilot.features.package_insert.routes import router as package_insert_router
        app.include_router(package_insert_router)
    if config.feature_enabled("ship_notice"):
        from copilot.features.ship_notice.routes import router as ship_notice_router
        app.include_router(ship_notice_router)
    if config.feature_enabled("first_dispute_guide"):
        from copilot.features.first_dispute_guide.routes import router as first_dispute_guide_router
        app.include_router(first_dispute_guide_router)
    if config.feature_enabled("third_party_statement"):
        from copilot.features.third_party_statement.routes import router as third_party_statement_router
        app.include_router(third_party_statement_router)
    if config.feature_enabled("trade_profiles"):
        from copilot.features.trade_profiles.routes import router as trade_profiles_router
        app.include_router(trade_profiles_router)
    if config.feature_enabled("lessons_exchange"):
        from copilot.features.lessons_exchange.routes import router as lessons_exchange_router
        app.include_router(lessons_exchange_router)
    if config.feature_enabled("season_prep"):
        from copilot.features.season_prep.routes import router as season_prep_router
        app.include_router(season_prep_router)

    if config.feature_enabled("transit_delivered_guide"):
        from copilot.features.transit_delivered_guide.routes import register as register_transit_delivered_guide
        register_transit_delivered_guide(app)
    if config.feature_enabled("lost_package_paths"):
        from copilot.features.lost_package_paths.routes import router as lost_package_paths_router
        app.include_router(lost_package_paths_router)
    if config.feature_enabled("case_history"):
        from copilot.features.case_history.routes import router as case_history_router
        app.include_router(case_history_router)
    if config.feature_enabled("status_link"):
        from copilot.features.status_link.routes import router as status_link_router
        app.include_router(status_link_router)
    if config.feature_enabled("accept_claim"):
        from copilot.features.accept_claim.routes import router as accept_claim_router
        app.include_router(accept_claim_router)
        from copilot.features.accept_claim import service as accept_claim_service
        app.state.accept_claim_allowed = lambda dispute_id, cookies: accept_claim_service.allowed(
            dispute_id, accept_claim_service.session_of(cookies) if config.hosted_demo() else None)

    @app.get("/", include_in_schema=False)
    def root():
        # uvicorn prints http://127.0.0.1:8000; the bare root used to answer {"detail":"Not Found"}.
        return RedirectResponse("/disputes", status_code=307)

    if hosted:
        app.add_middleware(HostedPoolMiddleware)         # inside the access gate: only the pool's dispute ids get past it
        app.add_middleware(HostedAccessMiddleware)
        app.state.access_limiter = AccessAttemptLimiter()
    else:
        app.add_middleware(LocalhostOnlyMiddleware)

    if hosted:
        # The host's health check: no database, no PayPal, no template. An access gate in front of the hosted demo
        # must let this path through without its cookie, or the host can never see the service as healthy.
        @app.get("/healthz", include_in_schema=False)
        def healthz():
            return {"status": "ok"}

        @app.head("/healthz", include_in_schema=False)
        def healthz_head():
            return HTMLResponse("")

        @app.get("/healthz/features", include_in_schema=False)
        def healthz_features():
            # Read-only proof of what is on: flag names only (the environment's COPILOT_FEATURE_* plus the names in
            # render.free.yaml, so a flag the host never set shows as off); no value of any other setting.
            import os
            from pathlib import Path
            names = {k for k in os.environ if k.startswith("COPILOT_FEATURE_")}
            try:
                names |= set(re.findall(r"^      - key: (COPILOT_FEATURE_[A-Z0-9_]+)\r?$",
                                        (Path(__file__).resolve().parents[1] / "render.free.yaml").read_text(encoding="utf-8"), re.M))
            except OSError:
                pass
            on = sorted(n for n in names if os.environ.get(n, "").strip().lower() == "true")
            return {"on": on, "off": sorted(names - set(on))}

        def access_page(request: Request, next_path: str, status: int = 200, *, token: str | None = None):
            token = new_csrf_token() if token is None else token
            access_error = {401: "The access code is not correct. Check it and enter it again.",
                            429: "Too many attempts. Wait before trying again.",
                            403: "Your session expired. Reload the access page and try again."}.get(status, "")
            # The ephemeral factory sets its own hosted sentence on the app state; its data line is the NOTICE.
            from copilot.hosted_ephemeral import NOTICE
            restart_line = (NOTICE if hasattr(request.app.state, "hosted_demo_sentence") else
                            "The demo keeps its database and uploads on the host's disk; a host restart may erase them.")
            response = templates.TemplateResponse(request, "access.html", {"csrf_token": token, "next": safe_next(next_path),
                                                   "access_error": access_error, "restart_line": restart_line},
                                                  status_code=status)
            set_csrf_cookie(response, token)
            return response

        @app.get("/access", include_in_schema=False)
        async def access_get(request: Request):
            token = request.cookies.get("csrf_token", "")
            # token_urlsafe(32) emits 43 URL-safe characters; malformed cookies are replaced securely.
            if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
                token = new_csrf_token()
            return access_page(request, request.query_params.get("next", "/disputes"), token=token)

        @app.post("/access", include_in_schema=False)
        async def access_post(request: Request):
            form = await request.form(max_files=0, max_fields=3, max_part_size=1024)
            token = form.get("csrf_token")
            if not isinstance(token, str) or not token.isascii() or not verify_csrf(request, token):
                return access_page(request, "/disputes", 403)
            address = request.client.host if request.client else "unknown"
            limiter = app.state.access_limiter
            next_path = form.get("next", "/disputes")
            if limiter.blocked(address):
                return access_page(request, str(next_path), 429)
            code = form.get("code", "")
            configured = config.access_code()
            if len(configured) < 12 or not secrets.compare_digest(str(code).encode("utf-8"), configured.encode("utf-8")):
                limiter.record_failure(address)
                await security.asyncio.sleep(1)
                return access_page(request, str(next_path), 401)
            response = RedirectResponse(safe_next(str(next_path)), status_code=303)
            response.set_cookie(ACCESS_COOKIE_NAME, access_cookie_value(), max_age=7 * 24 * 60 * 60,
                                secure=True, httponly=True, samesite="lax", path="/")
            return response

    app.mount("/static", RevalidatedStaticFiles(directory=str(Path(__file__).parent / "web" / "static")), name="static")

    def _read_key(request: Request) -> str:
        found = re.match(r"^/disputes/(PP-[A-Z0-9-]+)", request.url.path)
        return found.group(1) if found else "list"

    def _no_credentials_links() -> list:
        # Pages that render without any PayPal call, shown on the credentials-missing pages so the visitor can
        # still look around. Only fronts whose flag is on appear; the sample id is a fixed placeholder, not data.
        rows = [("presentation_mode", "/presentation-mode", "Presentation guide"),
                ("offline_market_mode", "/offline-market-mode/status", "Offline market status"),
                ("buyer_note", "/disputes/PP-D-12345/buyer-note", "Buyer note (sample dispute id PP-D-12345)"),
                ("lost_package_paths", "/disputes/PP-D-12345/lost-package-paths",
                 "Lost-package paths (sample dispute id PP-D-12345)"),
                ("judge_as_buyer", "/features/judge_as_buyer", "Judge as buyer"),
                ("judge_walkthrough", "/features/judge_walkthrough", "Judge walkthrough"),
                ("local_demo_hub", "/features/local_demo_hub", "Local demo hub")]
        return [{"href": href, "label": label} for flag, href, label in rows if config.feature_enabled(flag)]

    def _error_page(request: Request, status: int, state: str, title: str, message: str) -> HTMLResponse:
        # blocked/error responses are red, with the cause stated in words.
        # A local refusal is still distinct from PayPal rejecting a request.
        kind = "error" if status >= 400 else "info"
        # Plain language for the vendor: never a raw traceback or PayPal's own
        # issue code/debug_id in the visible page. Those stay in the server log (sanitised JSONL via
        # SandboxClient, as always), reachable by debug_id if support needs to look something up. Every error
        # says nothing was changed, shows when PayPal was last read successfully (so stale
        # content is never mistaken for current) and carries its action (Try again only for a GET - a
        # failed POST must never be silently repeated by a link).
        is_read = request.method == "GET"
        retry_url = request.url.path if is_read else None
        if hosted and state == HOSTED_NO_CREDENTIALS_STATE:
            title = "This demo is not connected to PayPal yet"
        if not is_read and state == "paypal-unreachable":
            # a POST may have written something locally before PayPal went away, so "nothing was
            # changed" would be a claim we cannot back - say what we do not know and where to find out.
            message = ("PayPal could not be reached while handling your request. Proofie could not confirm what "
                       "happened. Open the dispute again and use Check again before repeating anything.")
        # a failed POST offers no retry, only one plain GET link to the dispute when the path names one.
        dispute_id = path_dispute_id(request)
        no_credentials = state in ("credentials-missing", HOSTED_NO_CREDENTIALS_STATE)
        return templates.TemplateResponse(request, "error.html", {
            "state": state, "kind": kind, "title": title, "message": message, "retry_url": retry_url,
            "items": ["PayPal sandbox credentials not configured: dispute data unavailable"] if no_credentials else [],
            "offline_links": _no_credentials_links() if no_credentials else [],
            "dispute_id": dispute_id, "back_url": f"/disputes/{dispute_id}" if (dispute_id and not is_read) else None,
            "back_label": "Go back to the dispute",
            "last_read": last_paypal_read_label(_read_key(request))}, status_code=status)

    @app.exception_handler(StarletteHTTPException)
    async def not_found_page_handler(request: Request, exc: StarletteHTTPException):
        # Polish round: an unknown address used to answer Starlette's bare {"detail":"Not Found"}. A page address gets the
        # error page (header, breadcrumb, a way back); a deliberate HTTPException (own detail) and the /static files keep
        # the default answer.
        if request.url.path.startswith("/static/") or (exc.status_code, exc.detail) not in (
                (404, "Not Found"), (405, "Method Not Allowed")):
            return await http_exception_handler(request, exc)
        dispute_id = path_dispute_id(request)
        if exc.status_code == 405:
            # a GET (address bar, history, a copied link) to an address that only takes a form POST. The Allow header
            # stays; there is no Try again, because repeating a GET cannot do what the POST did.
            opened = request.method in ("GET", "HEAD")
            return templates.TemplateResponse(request, "error.html", {
                "state": "method-not-allowed", "kind": "error",
                "title": "This address does not open as a page" if opened else "This address does not take that kind of request",
                "message": ("This address only works when you press a button in Proofie, so it cannot be opened from the "
                            "address bar or a saved link. Use All disputes in the header to go back. Nothing was changed."
                            if opened else
                            "This address does not take that kind of request. Use All disputes in the header to go back. "
                            "Nothing was changed."),
                "retry_url": None, "dispute_id": dispute_id, "back_url": f"/disputes/{dispute_id}" if dispute_id else None,
                "back_label": "Go back to the dispute", "last_read": None, "show_last_read": False},
                status_code=405, headers=dict(exc.headers or {}))
        return templates.TemplateResponse(request, "error.html", {
            "state": "page-not-found", "kind": "error", "title": "That page was not found",
            "message": "There is no page at this address. Check the address, or use All disputes in the header. "
                       "Nothing was changed.",
            "retry_url": None, "dispute_id": dispute_id, "back_url": f"/disputes/{dispute_id}" if dispute_id else None,
            "back_label": "Go back to the dispute", "last_read": None, "show_last_read": False}, status_code=404)

    @app.exception_handler(RequestValidationError)
    async def form_validation_page_handler(request: Request, exc: RequestValidationError):
        # A form the page itself posts (an emptied box, a missing file) used to answer FastAPI's raw {"detail": [...]} JSON.
        # A browser form post gets the site's error page, naming what is missing, with a way back; any other caller (JSON
        # clients, query or path errors) keeps the default answer, status included.
        kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if request.method != "POST" or kind not in ("application/x-www-form-urlencoded", "multipart/form-data"):
            return await request_validation_exception_handler(request, exc)
        from copilot.web.routes_disputes import refusal_page
        names = {"data": "the box on this page", "csrf_token": "the page's security token (reload the page)",
                 "reminder_file": "the file to import"}
        missing = []
        for error in exc.errors():
            field = str((error.get("loc") or ("", ""))[-1])
            label = names.get(field, f"the field '{field}'")
            if label not in missing:
                missing.append(label)
        referer = request.headers.get("referer", "")
        parsed = urlsplit(referer)
        back = parsed.path if parsed.path.startswith("/") and not parsed.path.startswith("//") and parsed.netloc == request.url.netloc else request.url.path
        return refusal_page(request, 422, "Fill in what is missing",
                            "Something this form needs is empty or not valid. Go back, fill it in and send it again. Nothing was changed.",
                            items=[f"Missing or not valid: {label}." for label in missing], back_url=back, back_label="Go back to the form")

    @app.exception_handler(PayPalError)
    async def paypal_error_handler(request: Request, exc: PayPalError):
        if exc.status == 404:
            return _error_page(request, 404, "not-found", "That dispute was not found",
                               "It may not exist, or it may not belong to this account. Nothing was "
                               "changed.")
        if exc.status in (401, 403):
            # PayPal answered, and refused the app's own credentials. Saying "could not be
            # reached" here sends the seller to retry something that can never work. Fixed text: never a value
            # from .env, never PayPal's own error body. The sanitised client JSONL line and this logger line keep
            # the status for whoever looks.
            logger.warning("PayPal rejected the app credentials: HTTP %s", exc.status)
            state = "credentials-rejected"
            message = ("PayPal rejected this app's sandbox credentials. Check the Client ID and Secret in the "
                       ".env file in the app folder (both must be from the same sandbox app), restart the app "
                       "and reload this page. Retrying will not help until they are fixed.")
            if hosted:
                # A visitor of the hosted demo has no app folder and no settings to fix: only the host can act.
                state, message = HOSTED_NO_CREDENTIALS_STATE, HOSTED_NO_CREDENTIALS_MESSAGE
            if request.method != "GET":
                message += (" Proofie could not confirm what happened with your request. Open the dispute again and "
                            "use Check again before repeating anything.")
            else:
                message += " Nothing was changed."
            return _error_page(request, 502, state, "PayPal rejected the app's credentials", message)
        return _error_page(request, 502, "paypal-unreachable", "PayPal could not be reached right now",
                           "PayPal could not be reached right now. Try again in a moment. Proofie could "
                           "not read PayPal, and nothing was changed.")

    @app.exception_handler(CredentialsMissing)
    async def credentials_missing_handler(request: Request, exc: CredentialsMissing):
        # Never echoes anything from .env: the message below is fixed text.
        if hosted:
            return _error_page(request, 503, HOSTED_NO_CREDENTIALS_STATE,
                               "This demo is not connected to PayPal yet",
                               HOSTED_NO_CREDENTIALS_MESSAGE + " Nothing was sent to PayPal.")
        return _error_page(request, 503, "credentials-missing", "PayPal sandbox credentials are not set up yet",
                           "This app reads your own PayPal sandbox app's Client ID and Secret from a file named "
                           ".env in the app folder. Copy .env.example to .env, fill in both values, restart the "
                           "app and reload this page (see the README's Configure section). Nothing was sent to "
                           "PayPal.")

    @app.exception_handler(httpx.TransportError)
    async def transport_error_handler(request: Request, exc: httpx.TransportError):
        return _error_page(request, 502, "paypal-unreachable", "PayPal could not be reached right now",
                           "PayPal could not be reached right now. Try again in a moment. Proofie could "
                           "not read PayPal, and nothing was changed.")

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception):
        # anything not handled above used to reach the seller as Starlette's bare "Internal Server
        # Error", with a traceback only on the console. A fixed message and the exception CLASS only: never the
        # request body, headers, a token, document content or the exception's own text (it can carry any of them).
        # The specific handlers above keep priority; this one is only the last resort for a 500.
        logger.error("Unexpected error while handling %s %s: %s", request.method, request.url.path,
                     type(exc).__name__)
        return _error_page(request, 500, "internal-error", "Something went wrong inside Proofie",
                           "Something unexpected went wrong while handling your request, and it was recorded in "
                           "the app's log. Go back and try again; if it keeps happening, restart the app. Proofie "
                           "cannot confirm that your last action was completed.")

    @app.exception_handler(sqlite3.OperationalError)
    async def sqlite_error_handler(request: Request, exc: sqlite3.OperationalError):
        # Extended SQLite result codes keep the primary code in the low byte.
        # Never classify by message or disguise schema/disk errors as contention.
        code = getattr(exc, "sqlite_errorcode", None)
        if code is None or code & 0xff not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            return await unexpected_error_handler(request, exc)
        # Existing handlers can commit multiple local writes (and raw files) separately.
        # A busy response cannot promise rollback or suggest repeating a business send.
        return _error_page(request, 503, "local-update-busy", "A local update is temporarily busy",
                           "Proofie could not confirm that this action completed. Wait a moment, then reopen "
                           "the dispute to review what was saved. Retry only unsaved edits manually. "
                           "Do not repeat a send or payment.")

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.server_host(), port=config.server_port())
