"""v1.1.2: 'Prepare locally for review' is not offered when it cannot create anything (no package, no document, no typed
shipment detail); the page says what to add instead. Found by table 2: seven clicks, no package."""
from jinja2 import Environment, FileSystemLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _render(**ctx):
    env = Environment(loader=FileSystemLoader(str(ROOT / "copilot" / "web" / "templates")), autoescape=True)
    env.globals["feature_enabled"] = lambda name: True

    class Req:
        query_params = {}
    return env.get_template("features/local_autopilot/_preparation.html").render(request=Req(), norm={"dispute_id": "PP-R-AAA-10000001"},
                                                                                 csrf_token="t", **ctx)


def test_no_button_and_a_plain_line_when_there_is_nothing_to_prepare_from():
    page = _render(preparation_needs_input=True, preparation_observation_token="obs")
    assert "Prepare locally for review" not in page and "add a case document or enter a shipment detail first" in page


def test_the_button_is_offered_otherwise():
    page = _render(preparation_needs_input=False, preparation_observation_token="obs")
    assert "Prepare locally for review" in page and "Nothing to prepare from yet" not in page
