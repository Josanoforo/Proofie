"""What Proofie checked before sending: ten rows on one screen, assembled from facts the app already stores or computes.

No new fact is made here. Each row reads one thing that another page already shows:
  1-3   the checklist of the package page (the tracking number, the delivery place, the proof PayPal's rule asks for);
  4     the statements of the package and their sources;
  5     whether the AI draft passed the program's checks (packages.validation_ok; AI drafts only);
  6     the next step the program allowed for the AI draft (packages.effective_next_step; AI drafts only);
  7     the approval record of the package;
  8, 9  the send record of the package (the attempt registry and PayPal's read-back);
  10    PayPal's due date for the seller's answer.
A row is ok (checked and fine), stop (checked, something needs her), todo (not reached yet), na (does not apply) or, for row 4 only,
own (a statement with no document behind it: the app does not block it, it goes as her own words)."""
from __future__ import annotations

from dataclasses import dataclass

OK, STOP, TODO, NA, OWN = "ok", "stop", "todo", "na", "own"
STATE_WORDS = {OK: "Checked", STOP: "Needs you", TODO: "Not yet", NA: "Does not apply", OWN: "Your own words"}
TAG_KIND = {OK: "ok", STOP: "bad", TODO: "todo", NA: "na", OWN: "info"}
NAMES = (
    "The tracking number is this order's",
    "The delivery address matches the sale in PayPal",
    "The proof PayPal asks for is all there",
    "Every sentence shows where it comes from",
    "Amounts, dates and order numbers checked by the program",
    "No proof of delivery? Proofie asks you",
    "PayPal gets exactly what you approved",
    "One answer per case",
    "You see PayPal received it",
    "No deadline passes unseen",
)
PROOF_CHECKS = ("delivery_status", "delivery_date", "signature")
_CHECK_STATE = {"verified": OK, "not_applicable": NA, "conflict": STOP, "missing": STOP, "present_unverifiable": STOP}


@dataclass(frozen=True)
class Row:
    number: int
    name: str
    state: str
    line: str

    @property
    def word(self) -> str:
        return STATE_WORDS[self.state]

    @property
    def kind(self) -> str:
        return TAG_KIND[self.state]


def _check(checks, check_id):
    return next((c for c in checks or [] if getattr(c, "id", None) == check_id), None)


def _check_row(number: int, checks, check_id: str) -> Row:
    check = _check(checks, check_id)
    if check is None:
        return Row(number, NAMES[number - 1], TODO, "Not checked yet: the checklist has no result for this package.")
    return Row(number, NAMES[number - 1], _CHECK_STATE[check.state], check.seller_message)


def _proof_row(checks, sent: bool = False) -> Row:
    found = [c for c in (_check(checks, check_id) for check_id in PROOF_CHECKS) if c is not None]
    if not found:
        return Row(3, NAMES[2], TODO, "Not checked yet: the checklist has no result for this package.")
    open_rows = [c for c in found if c.state not in ("verified", "not_applicable")]
    if not open_rows:
        return Row(3, NAMES[2], OK, "All there.")
    labels = ", ".join(c.label_for_seller for c in open_rows)
    if sent:
        # v1.1.2: the answer is with PayPal, so a proof that is not there is a plain fact and asks nothing of her
        return Row(3, NAMES[2], TODO, f"{len(open_rows)} of {len(found)} not there: {labels}. Your answer is already with PayPal.")
    return Row(3, NAMES[2], STOP, f"{len(open_rows)} of {len(found)} not there yet: {labels}.")


def _backed(claim: dict) -> bool:
    """A statement with a document or PayPal's record behind it (what she typed herself is her own word, not a paper)."""
    return any(sid != "doc_manual" for sid in (claim.get("source_ids") or []))


def _statements_row(claims) -> Row:
    claims = list(claims or [])
    if not claims:
        return Row(4, NAMES[3], TODO, "No statement in this package yet.")
    backed = sum(1 for c in claims if _backed(c))
    if backed == len(claims):
        return Row(4, NAMES[3], OK, f"{backed} of {len(claims)} statements name their source.")
    own = len(claims) - backed          # not blocked by the app: an unsourced statement goes as her own word, and the row says so
    return Row(4, NAMES[3], OWN, f"{own} of {len(claims)} {'statement has' if own == 1 else 'statements have'} no document behind "
                                 f"{'it; it goes' if own == 1 else 'them; they go'} to PayPal as your own words.")


def _ai_rows(package: dict) -> tuple[Row, Row]:
    if (package or {}).get("source") != "ai":
        line = "No AI draft in this package."
        return Row(5, NAMES[4], NA, line), Row(6, NAMES[5], NA, line)
    if not package.get("validation_ok"):
        return (Row(5, NAMES[4], STOP, "The AI draft did not pass the program's checks, so it was not used."),
                Row(6, NAMES[5], NA, "The AI draft was not used."))
    five = Row(5, NAMES[4], OK, "The AI draft passed the program's checks of its figures against the case data.")
    step = package.get("effective_next_step")
    if step == "submit_evidence_for_human_approval":
        six = Row(6, NAMES[5], OK, "Proof of delivery was found before the draft proposed sending.")
    elif step:
        six = Row(6, NAMES[5], STOP, "The draft was held for you: more information was needed before sending.")
    else:
        six = Row(6, NAMES[5], TODO, "No next step is recorded for this draft.")
    return five, six


def _approval_row(approval: dict | None, approval_stale: bool, approval_cleared: bool, approved_on: str | None, sent: bool) -> Row:
    if approval is not None and not approval_stale:
        return Row(7, NAMES[6], OK, f"You approved this package on {approved_on}. This is exactly the text and files you approved."
                   if approved_on else "You approved this package. This is exactly the text and files you approved.")
    if sent:
        # v1.1.2: a package PayPal received is a closed record; its approval is the approval of what was sent
        return Row(7, NAMES[6], OK, "PayPal received the package you approved.")
    if approval_stale or approval_cleared:
        return Row(7, NAMES[6], STOP, "The package changed after you approved it, so the approval was cleared. Nothing goes out "
                                      "until you approve it again.")
    return Row(7, NAMES[6], TODO, "Not approved yet.")


_SEND_LINES = {
    "none": (TODO, "Nothing was sent yet."),
    "confirmed": (OK, "One answer was sent for this package. A second send of the same package is refused."),
    "reserved": (STOP, "A send was started and its result is not known yet. Proofie does not send again until PayPal's record is read."),
    "attempted": (STOP, "A send was attempted and PayPal has not confirmed it yet. Proofie does not send again until PayPal's "
                        "record is read."),
    "partial": (STOP, "PayPal lists the statements but not every file. Check the package before anything else is sent."),
    "refused": (STOP, "PayPal refused the send. Nothing was changed."),
    "blocked": (STOP, "The send was stopped before it left. Nothing went to PayPal."),
    "abandoned": (STOP, "An earlier send was set aside. Nothing more is sent for it."),
}


def _send_rows(record, received_on: str | None) -> tuple[Row, Row]:
    state = getattr(record, "state", "none")
    kind, line = _SEND_LINES.get(state, (TODO, "Nothing was sent yet."))
    eight = Row(8, NAMES[7], kind, line)
    if state == "confirmed" and received_on:
        nine = Row(9, NAMES[8], OK, f"Received by PayPal on {received_on}, confirmed by reading the dispute back.")
    elif state in ("reserved", "attempted", "partial"):
        nine = Row(9, NAMES[8], STOP, "Not confirmed by PayPal yet.")
    else:
        nine = Row(9, NAMES[8], TODO, "Nothing was sent yet.")
    return eight, nine


def _deadline_row(due: dict | None, sent: bool) -> Row:
    if due is None:
        return Row(10, NAMES[9], NA if sent else STOP,
                   "PayPal shows no due date for your answer now." if sent else "No verified deadline available.")
    line = f"{due.get('text')}: {due.get('remaining')}."
    return Row(10, NAMES[9], STOP if due.get("passed") else OK, line)


def rows(*, checks, claims, package, approval, approval_stale=False, approval_cleared=False, approved_on=None, record=None,
         received_on=None, due=None) -> list[Row]:
    """The ten rows, in the card's order. Every argument is a fact another page already holds."""
    sent = getattr(record, "state", "none") == "confirmed"
    five, six = _ai_rows(package)
    eight, nine = _send_rows(record, received_on)
    return [_check_row(1, checks, "tracking_number"), _check_row(2, checks, "destination"), _proof_row(checks, sent),
            _statements_row(claims), five, six, _approval_row(approval, approval_stale, approval_cleared, approved_on, sent),
            eight, nine, _deadline_row(due, sent)]


def summary(card: list[Row]) -> str:
    counted = [r for r in card if r.state != NA]
    done = sum(1 for r in counted if r.state == OK)
    needs = sum(1 for r in counted if r.state == STOP)
    text = f"{done} of {len(counted)} checked"
    return text + (f"; {needs} {'needs' if needs == 1 else 'need'} you" if needs else "")
