import json
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from copilot import config
from copilot.db import repo
from copilot.features.fulfilment.tracker_request import validate_capture_id
from copilot.features.lost_package_paths import service
from copilot.features.fulfilment import store as fulfilment_store
from copilot.web.routes_disputes import templates
from copilot.web.security import new_csrf_token, rejected_dispute_id, set_csrf_cookie, verify_csrf

router = APIRouter()
ACTION = "lost_package_carrier_reference_recorded"


def _saved_carrier(capture_id: str) -> str:
    if not config.feature_enabled("fulfilment") or validate_capture_id(capture_id):
        return ""
    row = fulfilment_store.get_file(capture_id)
    try:
        tracker = (json.loads(row.get("request_json") or "{}").get("trackers") or [{}])[0] if row else {}
    except (TypeError, ValueError, IndexError):
        return ""
    raw = str(tracker.get("carrier") or "").casefold()
    aliases = {"fedex": "fedex_mx", "fx": "fedex_mx", "ups": "ups_us", "usps": "usps_us",
               "estafeta": "estafeta_mx", "dhl": "dhl_mx", "redpack": "redpack_mx",
               "correos": "correos_mx", "paquetexpress": "paquetexpress_mx",
               "99minutos": "99minutos_mx", "ivoy": "ivoy_mx"}
    return aliases.get(raw, "")


def _render(request: Request, dispute_id: str, capture_id: str, carrier_id: str, *, status_code: int = 200,
            error: str = ""):
    rows = service.carrier_rows()
    valid_ids = {row["id"] for row in rows}
    selected = carrier_id if carrier_id in valid_ids else ( _saved_carrier(capture_id) if _saved_carrier(capture_id) in valid_ids else rows[0]["id"] )
    view = service.paths_for(selected, dispute_id)
    audit = [row for row in repo.list_audit(config.db_path(), dispute_id) if row["action"] == ACTION]
    token = new_csrf_token()
    response = templates.TemplateResponse(request, "features/lost_package_paths/index.html", {
        "dispute_id": dispute_id, "capture_id": capture_id, "carrier_rows": rows,
        "carrier": view["carrier"], "paths": view["paths"], "audit": audit,
        "csrf_token": token, "error": error})
    response.status_code = status_code
    response.headers["Cache-Control"] = "no-store"
    set_csrf_cookie(response, token)
    return response


@router.get("/disputes/{dispute_id}/lost-package-paths", response_class=HTMLResponse)
def lost_package_paths(request: Request, dispute_id: str, capture_id: str = "", carrier: str = ""):
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    capture_id = capture_id if not validate_capture_id(capture_id) else ""
    return _render(request, dispute_id, capture_id, carrier)


@router.post("/disputes/{dispute_id}/lost-package-paths/reference", response_class=HTMLResponse)
def record_carrier_reference(request: Request, dispute_id: str, carrier_id: str = Form(default=""),
                             carrier_reference: str = Form(default=""), capture_id: str = Form(default=""),
                             csrf_token: str = Form(default="")):
    bad = rejected_dispute_id(dispute_id)
    if bad:
        return bad
    capture_id = capture_id if not validate_capture_id(capture_id) else ""
    if not verify_csrf(request, csrf_token):
        return HTMLResponse("Reload this page and try again.", status_code=403)
    try:
        carrier = next((item for item in service.carrier_rows() if item["id"] == carrier_id), None)
        if carrier is None:
            raise ValueError("Choose a carrier from the local directory.")
        reference = service.validate_reference(carrier_reference)
    except ValueError as exc:
        return _render(request, dispute_id, capture_id, carrier_id, status_code=400, error=str(exc))
    repo.log_action(config.db_path(), "vendor", ACTION, dispute_id,
                    {"carrier_id": carrier_id, "carrier_case_reference": reference,
                     "source": "seller-recorded carrier contact"})
    target = f"/disputes/{dispute_id}/lost-package-paths?" + urlencode({"capture_id": capture_id,
                                                                      "carrier": carrier_id,
                                                                      "recorded": "1"})
    return RedirectResponse(target, status_code=303)
