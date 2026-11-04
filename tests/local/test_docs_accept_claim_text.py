"""D-101 B3: the public text about accepting a claim matches the product (the way test_docs_flag_counts pins the counts).
The README and the judge guide carry strategy's sentence of record; the old "out of scope / never calls them / no accept-claim action"
wording is gone from the README and docs/ (the parked video and presentation drafts are excluded)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RECORD = ("accepting the claim is the seller's last-resort option in Proofie, shown only when PayPal offers it and sent after the "
          "seller's own approval; "
          "the assistant can send a package a person already approved, through the optional `send_approved_package` tool (off by default; it exists when `COPILOT_MCP_ALLOW_SEND=true` is set as the server starts, and it refuses a package that has no approval or that changed after it), and the MCP server has none to approve and none to accept a claim.")
STALE = ("no accept-claim action", "accepting a claim are out of scope", "never calls them", "accept-claim nowhere", "no accept-claim capability exists in any surface")
PARKED = {"VIDEO-SCRIPT.md", "PRESENTATION-PROPOSAL.md"}


def _text(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_readme_and_judge_guide_carry_the_sentence_of_record_and_the_refund_pointer():
    for name in ("README.md", "docs/JUDGE-RUN.md"):
        text = _text(ROOT / name)
        assert RECORD in text, name
        assert "Issuing a refund outside a dispute stays in PayPal's" in text, name
        assert "accepting the claim is available in Proofie as the seller's last option" in text, name
        assert "This refunds the buyer and closes the dispute. Proofie cannot undo it." in text, name
        assert "allowed on the judge's own pool case and refused on every other case" in text, name


def test_the_old_wording_is_gone_from_the_readme_and_docs():
    files = [ROOT / "README.md", *sorted(p for p in (ROOT / "docs").glob("*.md") if p.name not in PARKED)]
    for path in files:
        text = _text(path).lower()
        for phrase in STALE:
            assert phrase not in text, (path.name, phrase)


def test_the_mcp_statements_that_stay_true_are_still_there():
    assert "it has no send, approve or accept-claim tool" in _text(ROOT / "README.md")
    devpost = ROOT / "docs/DEVPOST.md"
    if devpost.is_file():   # the submission text is not part of the public kit; README and MCP.md are
        assert re.search(r"no tool to approve or accept a claim|no approve or accept-claim tool", _text(devpost))
    assert "The seller can accept a claim in Proofie's web page as a last option; the assistant cannot." in _text(ROOT / "docs/MCP.md")


OBSERVED = ("On the sandbox run of record, PayPal showed the refund at once, moved the case to its review, and resolved it in the "
            "buyer's favour within minutes")


def test_the_readme_states_the_observed_sandbox_sequence_in_the_past_tense_without_a_promise():
    """D-117: the full sequence of the run of record (copilot_web.jsonl:16 under review + REFUNDED, then RESOLVED at
    evidence-A/sandbox_dispute_state.jsonl:2), tied to that run; no timing is promised for other runs."""
    text = _text(ROOT / "README.md")
    assert OBSERVED in text and "the sent page names the state PayPal's read-back returns" in text
    sentence = text[text.index(OBSERVED):].split(". ")[0].lower()
    for banned in (" only", " first", "faster", "always", "within seconds", "will "):
        assert banned not in sentence, banned
    assert "sandbox" in sentence and "run of record" in sentence

ACCEPT_CLAIM_SENTENCE = ("Proofie helps the seller make their case. If they decide to accept the claim and refund the buyer, they do it "
                         "themselves, on a check page of its own; Proofie's role is to help them make an informed decision.")


def test_the_readme_and_the_judge_guide_open_the_accept_claim_bullet_with_the_sellers_own_decision():
    for name in ("README.md", "docs/JUDGE-RUN.md"):
        assert "- " + ACCEPT_CLAIM_SENTENCE + " **Issuing a refund outside a dispute" in _text(ROOT / name), name
