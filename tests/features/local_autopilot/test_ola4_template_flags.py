from pathlib import Path
from types import SimpleNamespace
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader


def test_assistance_new_flags_off_preserves_baseline():
    # copilot/web/templates/features/local_autopilot/_assistance.html as of 4ae84240 (sha256 2d62dfbe...), kept as a fixture: a kit has no git history to show it from
    base=(Path(__file__).parent/"fixtures"/"_assistance_4ae84240.html").read_text(encoding="utf-8")
    from copilot.web.routes_disputes import templates
    env=templates.env.overlay(loader=ChoiceLoader([DictLoader({"baseline":base}),FileSystemLoader("copilot/web/templates")]))
    env.globals=env.globals.copy()
    env.globals["feature_enabled"]=lambda name:name == "local_autopilot"
    ctx={"assistance_context":{"preparation":"not started", "recommendation":None,"reason":"review", "facts":[],"attention_plan":None,"amount_display":None},"norm":{"dispute_id":"PP-R-DEMO-0000001"},"request":SimpleNamespace(query_params={}),"csrf_token":"token", "status_view":{"label":"Waiting"}, "local_autopilot_actions":[], "local_autopilot_currency_supported":True, "local_autopilot_history":[]}
    assert env.get_template("baseline").render(**ctx) == env.get_template("features/local_autopilot/_assistance.html").render(**ctx)
