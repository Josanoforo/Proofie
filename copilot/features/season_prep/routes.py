from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from copilot import config
from copilot.features.season_prep import service
from copilot.web.routes_disputes import templates

router = APIRouter()


@router.get("/season-prep/{capture_id}", response_class=HTMLResponse)
def season_prep_page(request: Request, capture_id: str):
    view = service.for_country(config.RULES_COUNTRY, date.today())
    return templates.TemplateResponse(request, "features/season_prep/index.html",
                                      {"capture_id": capture_id, **view})
