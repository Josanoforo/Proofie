"""Machine-readable, non-secret product limits."""

from pathlib import Path

RC44_FACTS_PATH = Path(__file__).resolve().parents[3] / "internal" / "release" / "RC44-FACTS-FOR-TABLES.md"

LIMITS = (
    "No automatic acceptance of a buyer claim.",
    "No action is sent without the existing human approval flow.",
    "A local language model is an assistant and can be wrong; a person reviews its text.",
    "Webhook behavior depends on the current runtime flags.",
    "Public figures stay withheld until the owner releases them; this page does not state a release status.",
)


def flags_snapshot(flag_reader):
    """Read only named boolean flags through the supplied runtime configuration accessor."""
    return {name: bool(flag_reader(name)) for name in
            ("outcome_appeal", "local_autopilot", "webhooks", "webhooks_verify", "bilingual")}


def rc44_snapshot(path: Path = RC44_FACTS_PATH) -> dict:
    """Expose only three checked RC44 facts; never copy source text or inspect credentials."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {"source_available": False, "public_figures_gated": None,
                "accuracy_claim_authorized": None,
                "measured_release_status_for_this_branch": "not established; the checked-in facts source is unavailable"}
    folded = " ".join(text.casefold().split())
    return {
        "source_available": True,
        "public_figures_gated": "public figures remain withheld pending t3/mesa 3 disposition" in folded,
        "accuracy_claim_authorized": False if ("no claim of full suite green, accuracy, release" in folded
                                               or "no accuracy or ground-truth claim" in folded) else None,
        "measured_release_status_for_this_branch": (
            "not established by the checked-in facts" if "not an owner acceptance" in folded and "formal freeze not reached" in folded
            else "not established; the checked-in facts do not identify this version's release status"),
    }
