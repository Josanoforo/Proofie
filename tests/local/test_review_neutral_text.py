"""The one sentence shown in place of an invitation to act while PayPal reviews a claim."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from copilot.web import dispute_state  # noqa: E402
from copilot.web.routes_disputes import templates  # noqa: E402

TEMPLATES = ("features/triage/review.html", "features/outcome/_gaps.html")


def test_the_neutral_text_is_one_shared_constant_and_a_template_global():
    assert dispute_state.REVIEWING_NEUTRAL_TEXT == "PayPal is reviewing this claim; nothing can be added now."
    assert templates.env.globals["REVIEWING_NEUTRAL_TEXT"] == dispute_state.REVIEWING_NEUTRAL_TEXT


def test_the_templates_render_the_global_and_hold_no_copy_of_the_sentence():
    for name in TEMPLATES:
        source = templates.env.loader.get_source(templates.env, name)[0]
        assert "REVIEWING_NEUTRAL_TEXT" in source, name
        assert "PayPal is reviewing this claim" not in source and "not checked further" not in source, name


def test_it_reads_well_after_the_label_and_the_tag():
    line = f"Tracking number [Missing needed detail]: {dispute_state.REVIEWING_NEUTRAL_TEXT}"
    assert line == "Tracking number [Missing needed detail]: PayPal is reviewing this claim; nothing can be added now."
    assert len(dispute_state.REVIEWING_NEUTRAL_TEXT.split()) < 25
