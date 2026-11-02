from fastapi import APIRouter, Request
from copilot import config
from copilot.web.routes_disputes import templates
from copilot.features.presentation_mode.service import progression_steps, review_snapshot, compare_snapshot

router = APIRouter()


@router.get("/presentation-mode")
def presentation_mode(request: Request):
    case_id = request.query_params.get("case_id", "")
    package_id = request.query_params.get("package_id", "")
    steps = progression_steps(case_id, package_id, {name: config.feature_enabled(name) for name in
                               ("package_print", "evaluation_workbench", "local_autopilot")})
    proposal, proposal_error, comparison = None, None, None
    if case_id and package_id:
        try:
            if not package_id.isascii() or not package_id.isdecimal() or len(package_id) > 10:
                raise ValueError("Invalid package reference.")
            proposal = review_snapshot(config.db_path(), config.uploads_dir(), case_id, int(package_id))
            expected = request.query_params.get("snapshot")
            if expected is not None:
                comparison = compare_snapshot(config.db_path(), config.uploads_dir(), case_id, int(package_id), expected)
        except (ValueError, OSError):
            proposal_error = "This package is not available for this case. Return to the case and select it again."
    return templates.TemplateResponse(request, "features/presentation_mode/page.html",
                                      {"steps": steps, "proposal": proposal, "proposal_error": proposal_error, "comparison": comparison,
                                       "eval_status": "No evaluation is run here; inspect the recorded case state in each existing flow."})
