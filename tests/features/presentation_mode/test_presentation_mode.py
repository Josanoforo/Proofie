from copilot.features.presentation_mode.service import moments_for_case, progression_steps


def test_presentation_has_four_safe_live_flow_links_and_no_measurement():
    enabled = {"package_print": True, "evaluation_workbench": True}
    moments = moments_for_case("PP-DEMO-1", "12", enabled)
    assert len(moments) == 4
    assert all(m["kind"] == "live-flow" and m.get("available", True) for m in moments)
    assert moments[0]["href"] == "/disputes/PP-DEMO-1/packages/12/print"
    assert moments[1]["href"] == "/disputes/PP-DEMO-1"
    assert moments[2]["href"] == "/disputes/PP-DEMO-1/packages/12"
    assert "does not evaluate" in moments[2]["subtitle"]
    assert moments[3]["href"] == "/disputes/PP-DEMO-1/packages/12#evaluation-manifest"
    assert "20 failures (52/72)" in moments[3]["title"]
    assert "not this checkout" in moments[3]["subtitle"]
    assert "/.env" not in " ".join(m["href"] for m in moments)


def test_presentation_does_not_put_invalid_ids_in_links():
    assert all(".." not in item["href"] for item in moments_for_case("../private", "0"))


def test_keyboard_progression_is_case_bound_and_has_explicit_fifth_simulation():
    steps = progression_steps("PP-DEMO-1", "12", {"package_print": True,
                                                     "evaluation_workbench": True, "local_autopilot": True})
    assert len(steps) == 5
    assert [step["kind"] for step in steps] == ["live-flow"] * 4 + ["simulation"]
    assert all("PP-DEMO-1" in step["href"] for step in steps[:4])
    assert steps[4]["title"].startswith("Fifth moment: simulation only")
    assert "#evaluation-manifest" in steps[3]["href"]
    assert "20 failures" in steps[3]["title"]
    from pathlib import Path
    page = Path("copilot/web/templates/features/presentation_mode/page.html").read_text(encoding="utf-8")
    assert 'event.key === "ArrowRight"' in page and 'event.key === "ArrowLeft"' in page
    assert 'event.key === "Enter"' in page
    assert "not an action or PayPal outcome" in steps[4]["subtitle"]


def test_rendered_presentation_delivers_keyboard_progression_and_fifth_simulation():
    from copilot.web.routes_disputes import templates
    steps = progression_steps("PP-DEMO-1", "12", {"package_print": True,
                                                       "evaluation_workbench": True, "local_autopilot": True})
    rendered = templates.env.get_template("features/presentation_mode/page.html").render(
        steps=steps, eval_status="No evaluation is run here.")
    assert rendered.count('<li data-presentation-step') == 5
    assert 'event.key === "ArrowRight"' in rendered and 'event.key === "ArrowLeft"' in rendered
    assert 'event.key === "Enter"' in rendered
    assert rendered.index("Recorded package and its existing evidence hash") < rendered.index("Re-read the case from PayPal")
    assert rendered.index("Historic local-model result: 20 failures (52/72)") < rendered.index("Fifth moment: simulation only")
    assert "historical, not this checkout" in rendered
    assert "#evaluation-manifest" in rendered
