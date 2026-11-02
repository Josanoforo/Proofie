"""The Spanish version of the statements (COPILOT_FEATURE_BILINGUAL, MX rule set). Registered in copilot/app.py behind the
flag (lazy import inside that flag block); outside the MX rule set both routes answer 404 like an unknown route.

- POST /disputes/{dispute_id}/packages/{package_id}/claims/{claim_id}/spanish: the seller types the Spanish version of one
  statement. The Spanish is inside the approval (the active approval binds it into its review_sha), so a save clears
  a valid approval, exactly as an English edit does.
- POST /disputes/{dispute_id}/packages/{package_id}/spanish/model: the local model drafts the Spanish of every statement still
  missing one; code checks each answer; the seller reviews. The hosted demo has the model off: no call is made.

Nothing here reads or writes PayPal; PayPal receives the English statements only."""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from copilot import config
from copilot.features.bilingual import model, service
from copilot.web.routes_disputes import csrf_expired_page, get_client, refusal_page
from copilot.web.routes_package import (_clear_approval, _is_sent_version, _own_package, _package_url,
                                        _sent_version_refusal, render_package)
from copilot.web.security import rejected_dispute_id, verify_csrf

router = APIRouter()


def get_model_call():
    """The model call to use; None means the real one (model.default_call). Tests override this."""
    return None


def _guard(request: Request, dispute_id: str, package_id: int, csrf_token: str):
    """(package, None) when the request may change this package's Spanish text, else (None, the response to return)."""
    if not service.enabled():
        raise HTTPException(status_code=404)
    if not verify_csrf(request, csrf_token):
        return None, csrf_expired_page(request)
    bad_id = rejected_dispute_id(dispute_id)
    if bad_id:
        return None, bad_id
    package, refusal = _own_package(request, dispute_id, package_id)
    if refusal is not None:
        return None, refusal
    if _is_sent_version(config.db_path(), dispute_id, package):
        return None, _sent_version_refusal(request, dispute_id, package_id)
    return package, None


@router.post("/disputes/{dispute_id}/packages/{package_id}/claims/{claim_id}/spanish", response_class=HTMLResponse)
def save_spanish(request: Request, dispute_id: str, package_id: int, claim_id: int, text: str = Form(default=""),
                 csrf_token: str = Form(default=""), client=Depends(get_client)):
    package, refusal = _guard(request, dispute_id, package_id, csrf_token)
    if refusal is not None:
        return refusal
    db_path = config.db_path()
    key = f"es-claim-{claim_id}"
    try:
        service.save_seller_spanish(db_path, dispute_id, package_id, claim_id, text)
    except LookupError:
        return refusal_page(request, 404, "Statement not found", "That statement could not be found in this package.",
                            back_url=_package_url(dispute_id, package_id) + "#statements",
                            back_label="Go back to this package")
    except ValueError as exc:
        return render_package(request, dispute_id, package_id, client, form_values={key: text},
                              form_errors={key: str(exc)})
    query = {"es_saved": claim_id}
    if _clear_approval(db_path, dispute_id, package_id, log=True):
        query["es_cleared"] = "1"
    return RedirectResponse(f"{_package_url(dispute_id, package_id)}?{urlencode(query)}#es-claim-{claim_id}", status_code=303)


@router.post("/disputes/{dispute_id}/packages/{package_id}/spanish/model", response_class=HTMLResponse)
def draft_spanish_with_model(request: Request, dispute_id: str, package_id: int, csrf_token: str = Form(default=""),
                             call=Depends(get_model_call)):
    package, refusal = _guard(request, dispute_id, package_id, csrf_token)
    if refusal is not None:
        return refusal
    page = _package_url(dispute_id, package_id)
    if config.hosted_demo():
        return RedirectResponse(f"{page}?es_model=off#statements-es", status_code=303)
    db_path = config.db_path()
    summary = service.draft_with_model(db_path, dispute_id, package_id, call=call)
    if summary["made"] and _clear_approval(db_path, dispute_id, package_id, log=True):
        cleared = {"es_cleared": "1"}
    else:
        cleared = {}
    if summary["error"] == model.UNAVAILABLE:
        query = {"es_model": "unavailable"}
    else:
        query = {"es_model": "done", "made": summary["made"], "refused": len(summary["refused"])}
    return RedirectResponse(f"{page}?{urlencode({**query, **cleared})}#statements-es", status_code=303)
