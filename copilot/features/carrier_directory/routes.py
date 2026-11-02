from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from copilot.features.carrier_directory import service
from copilot.web.routes_disputes import templates

router = APIRouter()


@router.get("/carrier-directory", response_class=HTMLResponse)
def directory_page(request: Request, q: str = ""):
    return templates.TemplateResponse(request, "features/carrier_directory/index.html",
                                      {"carriers": service.find(q), "query": q})
