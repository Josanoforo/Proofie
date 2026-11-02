"""GET-only manual-copy page; auth remains on the app exterior."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from copilot import config
from copilot.web import status as web_status
from copilot.web.routes_disputes import templates, refusal_page
from copilot.features.delivery_facts.service import FactsError, FactsMissing, read
router=APIRouter()
@router.get("/delivery-facts/{capture_id}",response_class=HTMLResponse)
def page(request:Request,capture_id:str):
    try:
        facts=read(config.db_path(),capture_id)
    except FactsMissing:
        return refusal_page(request,404,"No local delivery facts","No local delivery facts for this capture. Nothing was changed or sent.",back_url="/fulfilment",back_label="Back to tracking files")
    except FactsError:
        return refusal_page(request,400,"Cannot display these local delivery facts safely","Cannot display these local delivery facts safely. Review the source record. Nothing was changed or sent.",back_url="/fulfilment",back_label="Back to tracking files")
    return templates.TemplateResponse(request,"features/delivery_facts/page.html",{"facts":facts,"file_view":web_status.tracking_status(facts["record_status"])},headers={"Cache-Control":"no-store"})
