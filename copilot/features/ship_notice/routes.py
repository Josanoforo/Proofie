from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from copilot import config
from copilot.features.buyer_note.service import build_note
from copilot.features.fulfilment import store
from copilot.features.fulfilment.tracker_request import validate_capture_id
from copilot.web.routes_disputes import templates

router = APIRouter()


@router.get("/fulfilment/{capture_id}/check/{token}/ship-notice", response_class=HTMLResponse)
def ship_notice_page(request: Request, capture_id: str, token: str, locale: str = "en"):
    if validate_capture_id(capture_id):
        return HTMLResponse("This shipment check is unavailable.", status_code=404)
    check = store.get_check(capture_id, token)
    if not check:
        return HTMLResponse("This shipment check expired or is unavailable.", status_code=404)
    trackers = check.get("body", {}).get("trackers") or []
    if len(trackers) != 1 or not isinstance(trackers[0], dict):
        return HTMLResponse("This shipment check is unavailable.", status_code=404)
    tracker = trackers[0]
    description = expected_date = ""
    dispute_id = check.get("form", {}).get("from_dispute", "")
    if dispute_id and config.feature_enabled("sale_readiness"):
        from copilot.features.sale_readiness import service as readiness
        saved = readiness.load_case_terms(dispute_id, capture_id)
        terms = saved.get("terms", {}) if saved and isinstance(saved.get("terms"), dict) else {}
        description = terms.get("description", "")
        expected_date = terms.get("expected_date", "")
    note = build_note(description=description, tracking_number=tracker.get("tracking_number"),
                      carrier=tracker.get("carrier"), expected_date=expected_date, locale=locale if locale in ("en", "es") else "en")
    response = templates.TemplateResponse(request, "features/ship_notice/page.html",
        {"capture_id": capture_id, "note": note, "locale": note["locale"]})
    response.headers["Cache-Control"] = "no-store"
    return response
