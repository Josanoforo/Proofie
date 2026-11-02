"""Evidence is sent once per dispute, and a page offers a way to send only when a send is possible.

One cell per package state x what PayPal's read lists (nothing / other seller evidence / the statements of the earlier
send) x whether provide-evidence is offered. Every cell renders the package page and the dispute page and asserts:
  (a) a send, approve, mark, new-version or change-a-statement way out is offered only when a send is possible (the send
      path's own checks of the fresh read pass: a provide-evidence link and no precondition problem);
  (b) the sentence "PayPal already lists evidence from you" appears only when the read lists seller evidence, on the pages
      and on the result pages of the real POSTs (send, check, mark as not received);
  (c) when the read lists seller evidence, every banner, step, task row, summary and answer that would promise a send says
      the rule instead; a page never shows the send path's own precondition text;
  (d) both pages show the same next step, except the named differences below.
Offline only: fake PayPal clients and a temp database per test. A real POST of each route runs in every cell against the
fake (the number of PayPal writes and the answer are read back). Some cells carry a drift conflict after a confirmed send.
Promises are matched case-insensitively over the regions of the page and over the whole main text of each page and answer."""
import json

import pytest
from _dom import parse
from _hosted import set_test_csrf

from copilot.core import send_service
from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.core.send import check_preconditions, sha256_json, validate_send_link
from copilot.core.disputes import normalize
from copilot.db import repo
from copilot.web import next_step, routes_package

import test_routes_send as send_tests
from test_rc42_package import V2_CHANGED, forms_to
from test_ux_rc41_fix3_state3 import age_attempts
from test_ux_rc41_fix_state import LABEL_A, LABEL_B, OTHER, STATEMENT, add_file, posts, request_input, seed_confirmed
from test_ux_rc41_fix_state_status import add_late_file, panel, seed_attempt
from test_ux_rc41_package import DID, add_claims, approve_passive, draft, env, new_package, page  # noqa: F401

from datetime import datetime, timezone
from types import SimpleNamespace

pytestmark = [pytest.mark.usefixtures("web_mode")]

FACT = "PayPal already lists evidence from you"
RULE_ONLY = "Proofie sends evidence once per dispute"


# ------------------------------------------------------------------------------------------------ the read
def body_for(db, listed, offered, own_pkg=None, own_files=None, review=False):
    body = json.loads(json.dumps(send_tests.WAITING))
    evid = list(body.get("evidences") or [])
    if listed == "other":
        evid.append({"source": "SUBMITTED_BY_SELLER", "evidence_type": "PROOF_OF_FULFILLMENT",
                     "evidence_info": {"tracking_info": [{"tracking_number": "OTHER-TRK-1"}]},
                     "notes": "typed in PayPal", "date": "2026-10-05T00:00:00Z"})
    elif listed == "own":
        req = request_input(db, own_pkg)
        e = {"source": "SUBMITTED_BY_SELLER", "evidence_type": "PROOF_OF_FULFILLMENT",
             "evidence_info": {"tracking_info": [{"tracking_number": req["evidences"][0]["evidence_info"]
                                                  ["tracking_info"][0]["tracking_number"]}]},
             "notes": req["evidences"][0]["notes"], "date": "2026-10-05T00:00:00Z",
             "documents": [{"name": n} for n in (own_files or [])]}
        evid.append(e)
    body["evidences"] = evid
    if not offered:
        body["links"] = [x for x in body["links"] if x["rel"] != "provide_evidence"]
    if review:
        body["status"], body["dispute_life_cycle_stage"] = "UNDER_REVIEW", "CHARGEBACK"
        body["links"] = [x for x in body["links"] if x["rel"] == "self"] if not offered else body["links"]
    return body


def set_read(fake, body):
    fake.detail_body = fake.pre_send_get_body = fake.recovery_get_body = body


# ------------------------------------------------------------------------------------------------ the states
def st_no_earlier(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    approve_passive(c, token, v1)
    return {"page": v1, "own": v1, "own_files": []}


def st_refused(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    approve_passive(c, token, v1)
    seed_attempt(db, v1, "failed")
    return {"page": v1, "own": v1, "own_files": []}


def st_blocked_rule(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    approve_passive(c, token, v1)
    seed_attempt(db, v1, "blocked", reason="evidence already submitted by the seller: ['PROOF_OF_FULFILLMENT']")
    return {"page": v1, "own": v1, "own_files": []}


def st_own_unconfirmed(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    approve_passive(c, token, v1)
    seed_attempt(db, v1, "sent_unconfirmed")
    age_attempts(db)
    return {"page": v1, "own": v1, "own_files": []}


def st_other_unconf_same(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    add_file(c, db, token, v1, "a.pdf", LABEL_A)
    approve_passive(c, token, v1)
    seed_attempt(db, v1, "sent_unconfirmed")
    v2 = new_package(c, token)
    add_claims(db, v2, STATEMENT)
    add_file(c, db, token, v2, "a.pdf", LABEL_A)
    approve_passive(c, token, v2)
    age_attempts(db)
    return {"page": v2, "own": v1, "own_files": ["a.pdf"]}


def st_other_unconf_same_otherfiles(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    add_file(c, db, token, v1, "a.pdf", LABEL_A)
    approve_passive(c, token, v1)
    seed_attempt(db, v1, "sent_unconfirmed")
    v2 = new_package(c, token)
    add_claims(db, v2, STATEMENT)
    add_file(c, db, token, v2, "a.pdf", LABEL_A)
    add_file(c, db, token, v2, "b.pdf", OTHER)
    approve_passive(c, token, v2)
    age_attempts(db)
    return {"page": v2, "own": v1, "own_files": ["a.pdf"]}


def st_other_unconf_other(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    approve_passive(c, token, v1)
    seed_attempt(db, v1, "sent_unconfirmed")
    v2 = new_package(c, token)
    add_claims(db, v2, V2_CHANGED)
    approve_passive(c, token, v2)
    age_attempts(db)
    return {"page": v2, "own": v1, "own_files": []}


def st_own_partial(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    add_file(c, db, token, v1, "a.pdf", LABEL_A)
    add_file(c, db, token, v1, "b.pdf", LABEL_B)
    approve_passive(c, token, v1)
    seed_attempt(db, v1, "sent_unconfirmed", reason="missing_attachment:b.pdf")
    age_attempts(db)
    return {"page": v1, "own": v1, "own_files": ["a.pdf"]}


def _v1_partial(env, v2_text):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    add_file(c, db, token, v1, "a.pdf", LABEL_A)
    add_file(c, db, token, v1, "b.pdf", LABEL_B)
    approve_passive(c, token, v1)
    seed_attempt(db, v1, "sent_unconfirmed", reason="missing_attachment:b.pdf")
    v2 = new_package(c, token)
    add_claims(db, v2, v2_text)
    add_file(c, db, token, v2, "a.pdf", LABEL_A)
    add_file(c, db, token, v2, "b.pdf", LABEL_B)
    approve_passive(c, token, v2)
    age_attempts(db)
    return {"page": v2, "own": v1, "own_files": ["a.pdf"]}


def st_earlier_partial_changed(env):
    return _v1_partial(env, V2_CHANGED)


def st_earlier_partial_same(env):
    return _v1_partial(env, STATEMENT)


def _confirmed_twin(env, v2_files):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    add_file(c, db, token, v1, "a.pdf", LABEL_A)
    approve_passive(c, token, v1)
    seed_confirmed(db, v1)
    v2 = new_package(c, token)
    add_claims(db, v2, STATEMENT)
    for name, data in v2_files:
        add_file(c, db, token, v2, name, data)
    approve_passive(c, token, v2)
    return {"page": v2, "own": v1, "own_files": ["a.pdf"]}


def st_confirmed_same_files(env):
    return _confirmed_twin(env, [("a.pdf", LABEL_A)])


def st_confirmed_other_files(env):
    return _confirmed_twin(env, [("a.pdf", LABEL_A), ("b.pdf", OTHER)])


def st_later_file(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    approve_passive(c, token, v1)
    seed_confirmed(db, v1)
    add_late_file(db, v1)
    return {"page": v1, "own": v1, "own_files": []}


def st_abandoned_plain(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    approve_passive(c, token, v1)
    seed_attempt(db, v1, "abandoned", reason=send_service.ABANDONED_REASON)
    return {"page": v1, "own": v1, "own_files": []}


def st_abandoned_plain_then_v2(env):
    """The realistic order: v1 unconfirmed, v2 (changed) marks it as not received; v2 is the latest."""
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    approve_passive(c, token, v1)
    seed_attempt(db, v1, "abandoned", reason=send_service.ABANDONED_REASON)
    v2 = new_package(c, token)
    add_claims(db, v2, V2_CHANGED)
    approve_passive(c, token, v2)
    return {"page": v2, "own": v1, "own_files": [], "also": v1}


def st_abandoned_partial(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    add_file(c, db, token, v1, "a.pdf", LABEL_A)
    add_file(c, db, token, v1, "b.pdf", LABEL_B)
    approve_passive(c, token, v1)
    seed_attempt(db, v1, "abandoned", reason=send_service.ABANDONED_PARTIAL_REASON)
    return {"page": v1, "own": v1, "own_files": ["a.pdf"]}


def st_abandoned_partial_then_v2(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    add_file(c, db, token, v1, "a.pdf", LABEL_A)
    add_file(c, db, token, v1, "b.pdf", LABEL_B)
    approve_passive(c, token, v1)
    seed_attempt(db, v1, "abandoned", reason=send_service.ABANDONED_PARTIAL_REASON)
    v2 = new_package(c, token)
    add_claims(db, v2, V2_CHANGED)
    add_file(c, db, token, v2, "a.pdf", LABEL_A)
    add_file(c, db, token, v2, "b.pdf", LABEL_B)
    approve_passive(c, token, v2)
    return {"page": v2, "own": v1, "own_files": ["a.pdf"], "also": v1}


def st_newer_after_confirmed(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    approve_passive(c, token, v1)
    seed_confirmed(db, v1)
    v2 = new_package(c, token)
    add_claims(db, v2, V2_CHANGED)
    set_test_csrf(c, token)
    approve_passive(c, token, v2)
    return {"page": v2, "own": v1, "own_files": []}


def st_newer_after_confirmed_unapproved(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    approve_passive(c, token, v1)
    seed_confirmed(db, v1)
    v2 = new_package(c, token)
    add_claims(db, v2, V2_CHANGED)
    return {"page": v2, "own": v1, "own_files": []}


def st_cleared_approval(env):
    """v1 approved, then a statement added: the approval is cleared (the common 'approval cleared' banner)."""
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    approve_passive(c, token, v1)
    approval = repo.get_latest_valid_approval(db, v1)
    repo.invalidate_approval(db, approval["id"])
    return {"page": v1, "own": v1, "own_files": []}


def st_claim_under_review(env):
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    approve_passive(c, token, v1)
    return {"page": v1, "own": v1, "own_files": [], "review": True}


def st_confirmed_drift(env):
    """v1 confirmed and nothing changed since; a conflicting check on the dispute page shows the drift note."""
    c, db, token, fake = env
    v1 = draft(c, db, token, STATEMENT)
    approve_passive(c, token, v1)
    seed_confirmed(db, v1)
    return {"page": v1, "own": v1, "own_files": [], "conflict": True}


def st_later_file_drift(env):
    info = st_later_file(env)
    return {**info, "conflict": True}


STATES = {f.__name__[3:]: f for f in (
    st_no_earlier, st_refused, st_blocked_rule, st_own_unconfirmed, st_other_unconf_same,
    st_other_unconf_same_otherfiles, st_other_unconf_other, st_own_partial, st_earlier_partial_changed,
    st_earlier_partial_same, st_confirmed_same_files, st_confirmed_other_files, st_later_file, st_abandoned_plain,
    st_abandoned_plain_then_v2, st_abandoned_partial, st_abandoned_partial_then_v2, st_newer_after_confirmed,
    st_newer_after_confirmed_unapproved, st_cleared_approval, st_claim_under_review, st_confirmed_drift,
    st_later_file_drift)}

RULE = next_step.ONCE_PER_DISPUTE
TASK_TITLES = ("Upload documents", "Prepare evidence package", "Review each statement and approve",
               "Send evidence to PayPal")
# Every way out a page or a result page can offer. Matched case-insensitively, after the rule sentence (and, for a whole
# page, the four step titles) are masked: the rule itself says "will not send again" and the steps carry their titles.
WAY_OUT = ("change a statement", "edit the statements", "prepare a new version", "start a new submission",
           "mark as not received", "send evidence to paypal", "change it first", "generate evidence package",
           "review each statement and approve", "continue to send", "prepare a new package version", "send again",
           "approve again", "approve it again", "approve it first", "then send this package", "edit it first",
           "edit it (that makes a new version)", "edit it (this creates a new version)",
           "generate or start a package", "start package v", "nothing to do unless you prepare",
           "before sending a different package", "try sending this package", "makes a new version",
           "creates a new version", "new submission", "mark it as not received", "mark the earlier send as not received",
           "follow these steps", "follow the steps", "tick the box")
# Present-tense statements of what PayPal shows: with nothing listed they are told as what Proofie saw when it checked.
PRESENT_FACTS = ("PayPal shows its statements", "PayPal shows the statements", "PayPal shows your statements",
                 "PayPal shows these statements", "PayPal already holds these statements")
RAW_PRECONDITION = ("evidence already submitted by the seller", "is not among the evidence PayPal requested")
UPLOAD_INVITATION = "upload a document on the dispute page"
DOCUMENT_FORMS = {"documents", "include", "exclude"}
# The answers that need no rule although the read lists evidence: a wait, or a statement of what PayPal holds (states), and the
# answers decided before PayPal is read at all (titles); they promise nothing, and the gate checks that they do not.
NO_RULE_NEEDED = ("settling", "confirmed", "recovered", "partial", "result-uncertain")
BEFORE_THE_READ = ("Not approved", "Approval out of date", "Cannot send", "Not found", "Result pending — check again",
                   "Outcome pending", "Package already received")
# The differences between the two pages that are by design (named, not silent):
#  - the dispute page reconciles unconfirmed attempts with PayPal's answer while the package page only reads the stored
#    record, so a cell whose attempt rows changed during the dispute page request may differ;
#  - without the active-approval feature the package page keeps the legacy approve form ("approve_here") where the dispute
#    page links the review ("review");
#  - the package page of the sent version keeps saying it was sent ("confirmed") where the dispute page, once PayPal asks
#    for evidence again and no document exists, leads to the first step of a new version ("upload").
BY_DESIGN_KEYS = {("approve_here", "review"), ("confirmed", "upload")}


def mask(text, titles=True):
    """Whitespace-normalised, the rule sentence (and the step titles) replaced by a marker, lower-cased."""
    text = " ".join(str(text).split()).replace(RULE, "<R>")
    if titles:
        for title in TASK_TITLES:
            text = text.replace(title, "<T>")
    return text.lower()


def promises_in(text, titles=True):
    low = mask(text, titles)
    return sorted(w for w in WAY_OUT if w in low)


def scan(html):
    dom = parse(html)
    main = dom.find("main")
    mtext = " ".join((main.text() if main is not None else dom.text()).split())
    node, text = panel(html)
    out = {"key": node.attrs.get("data-next-step") if node is not None else None, "panel": text, "main": mtext,
           "forms": sorted({f.attrs.get("action", "").rsplit("/", 1)[-1].split("?")[0] for f in dom.find_all("form")}),
           "send_form": bool(forms_to(dom, "/send")), "approve_form": bool(forms_to(dom, "/approve")),
           "mark_form": bool(forms_to(dom, "/confirm-not-received")),
           "new_version_post": any(f.attrs.get("action", "").endswith(f"/disputes/{DID}/packages")
                                   for f in dom.find_all("form")),
           "fact": FACT in mtext, "rule_only": RULE_ONLY in mtext.replace(FACT + " for this dispute. " + RULE_ONLY, ""),
           "rule": RULE in mtext}
    out["banners"] = [n.text() for n in dom.walk() if "banner" in n.attrs.get("class", "").split()]
    sw = dom.find(data_state="send-withheld")
    out["send_withheld"] = sw.text() if sw is not None else None
    wb = dom.find("details", id="why-blocked")
    out["why_blocked"] = wb.text() if wb is not None else None
    nv = dom.find("details", data_state="new-version")
    out["new_version_details"] = nv.text() if nv is not None else None
    out["tasks"] = [(r.attrs.get("data-task"), r.attrs.get("data-status"), " ".join(r.text().split()))
                    for r in dom.find_all("li", class_="task-row")]
    summ = dom.find(id="summary") or dom.find(data_state="summary")
    out["summary"] = summ.text() if summ is not None else None
    drift = dom.find(data_state="drift")
    out["drift"] = drift.text() if drift is not None else None
    return out


def possible(db, fake, pkg):
    """What the send path would decide for a NEW version (changed statement) and for this package as it is."""
    norm = normalize(fake.pre_send_get_body, datetime.now(timezone.utc))
    try:
        req = request_input(db, pkg)
        problems = check_preconditions(norm, req, DID, None)
    except ValueError as e:
        problems = [f"no request: {e}"]
    link_err = validate_send_link(norm)
    store = SQLiteAttemptStore(db)
    try:
        attempts = store.list_by_dispute(DID)
        blocking = [a for a in attempts if a["state"] in ("pending", "sent_unconfirmed")]
    finally:
        store.close()
    return {"listed_norm": bool(norm.get("evidence_submitted_by_seller")), "link_err": link_err,
            "problems": problems,
            "new_version_send_possible": (not link_err and not problems),   # a mark clears an aged sent_unconfirmed row
            "blocking_rows": [(a["state"], str(a.get("reason") or "")[:40]) for a in blocking],
            "attempt_states": [(a["state"], str(a.get("reason") or "")[:60]) for a in attempts]}


CELLS = [(s, l, o) for s in STATES for l in ("none", "other", "own") for o in (True, False)
         if not (l == "own" and s in ("no_earlier", "refused", "blocked_rule", "cleared_approval", "claim_under_review"))]
CELL_IDS = [f"{s}-{l}-{'offered' if o else 'notoffered'}" for s, l, o in CELLS]


def page_violations(name, s, listed, can_send, link_offered):
    out = []
    if s["fact"] and not listed:
        out.append(f"{name}: the fact sentence without a listing read")
    if s["rule_only"] and not listed:
        out.append(f"{name}: the rule without a listing read")
    if not listed:
        shown = [p for p in PRESENT_FACTS if p in s["main"]]
        if shown:
            out.append(f"{name}: tells {shown} as present fact although nothing is listed")
    if any(raw in s["main"] for raw in RAW_PRECONDITION):
        out.append(f"{name}: shows the send path's own precondition text")
    regions = [s["panel"]] + s["banners"] + [s["send_withheld"] or "", s["why_blocked"] or "",
                                              s["new_version_details"] or "", s["summary"] or "", s["drift"] or ""]
    found = set(promises_in(" || ".join(regions), titles=False))
    found |= {w for w in promises_in(" || ".join(t[2] for t in s["tasks"]))}
    found |= set(promises_in(s["main"]))
    offered = sorted(found | ({"send form"} if s["send_form"] else set()) | ({"mark form"} if s["mark_form"] else set())
                     | ({"new version"} if s["new_version_post"] else set())
                     | ({"approve form"} if s["approve_form"] else set()))
    if offered and not can_send:
        out.append(f"{name}: offers {offered} although no send is possible")
    if listed and not s["rule"] and s["key"] not in ("claim_under_review", "no_action", "settling"):
        out.append(f"{name}: the read lists seller evidence but the page does not say the rule (key {s['key']})")
    for key, status, text in s["tasks"]:
        if key not in ("approve", "send"):
            continue
        if not can_send and status == "not_started":
            out.append(f"{name}: the {key} task row is 'not started' although no send is possible")
        if listed and link_offered and status in ("not_started", "blocked") and RULE not in text:
            out.append(f"{name}: the {key} task row ({status}) does not say the rule")
    return out


def violations(row):
    pkg, dsp = row["pkg"], row["dispute"]
    listed = row["before"]["listed_norm"]
    can_send = row["before"]["new_version_send_possible"]
    link_offered = row["before"]["link_err"] is None
    out = page_violations("package page", pkg, listed, can_send, link_offered)
    out += page_violations("dispute page", dsp, listed, can_send, link_offered)
    if listed:
        if DOCUMENT_FORMS & set(dsp["forms"]):
            out.append(f"dispute page: document forms {sorted(DOCUMENT_FORMS & set(dsp['forms']))} while listed")
        if UPLOAD_INVITATION in pkg["main"].lower():
            out.append("package page: invites an upload on the dispute page while listed")
    if row["conflict"] and not dsp["drift"]:
        out.append("dispute page: the cell has a drift conflict but the page shows no drift note")
    reconciled = row["before"]["attempt_states"] != row["after_dispute"]["attempt_states"]
    if pkg["key"] != dsp["key"] and (pkg["key"], dsp["key"]) not in BY_DESIGN_KEYS and not reconciled:
        out.append(f"the pages show different next steps: {pkg['key']} / {dsp['key']}")
    send = row["send"]
    if listed and send["posted"] != 0:
        out.append(f"the read lists seller evidence but a real POST wrote {send['posted']} times")
    if pkg["send_form"] and send["posted"] != 1:
        out.append(f"the package page offers Send but a real POST wrote {send['posted']} times: {send.get('msg', '')[:120]}")
    out += result_violations("send", send, listed, can_send, link_offered)
    return out


def result_violations(route, res, listed, can_send, link_offered):
    """The result page of a real POST on one route: no promise when no send is possible, the rule when the read lists
    evidence and PayPal offers the action (a wait or a statement of what PayPal holds needs none), the fact only when
    listed, and never the send path's own precondition text."""
    msg = res.get("msg", "")
    out = []
    if res.get("fact") and not listed:
        out.append(f"{route} result: the fact sentence without a listing read")
    if res.get("rule_only") and not listed:
        out.append(f"{route} result: the rule without a listing read")
    if any(raw in msg for raw in RAW_PRECONDITION):
        out.append(f"{route} result: shows the send path's own precondition text")
    if not listed:
        shown = [p for p in PRESENT_FACTS if p in msg]
        if shown:
            out.append(f"{route} result: tells {shown} as present fact although nothing is listed")
    found = promises_in(msg)
    if found and not can_send:
        out.append(f"{route} result: offers {found} although no send is possible: {msg[:240]}")
    if (route == "send" and listed and link_offered and RULE not in msg and res.get("state") not in NO_RULE_NEEDED
            and res.get("title") not in BEFORE_THE_READ):
        out.append(f"{route} result: the read lists seller evidence but the answer does not say the rule "
                   f"(state {res.get('state')}): {msg[:240]}")
    if res.get("posted") and route != "send":
        out.append(f"{route}: a real POST of the route wrote {res['posted']} times")
    return out


def post_result(c, fake, token, pkg, route, data=None):
    """POST one route as a browser would (CSRF cookie and field), follow its redirect and read the answer back."""
    set_test_csrf(c, token)
    before = len(posts(fake))
    r = c.post(f"/disputes/{DID}/packages/{pkg}/{route}", data={"csrf_token": token, **(data or {})},
               follow_redirects=False)
    res = {"status": r.status_code}
    html = r.text
    if r.status_code == 303:
        got = c.get(r.headers["location"])
        html = got.text
        res["status"] = got.status_code
    dom = parse(html)
    main = dom.find("main")
    msg = " ".join((main.text() if main is not None else dom.text()).split())
    banner = next((n for n in dom.walk() if "banner" in n.attrs.get("class", "").split()), None)
    h1 = dom.find("h1")
    res.update(title=h1.text() if h1 is not None else None, msg=msg, state=banner.attrs.get("data-state") if banner is not None else None,
               fact=FACT in msg, rule_only=RULE_ONLY in msg and FACT not in msg)
    res["posted"] = len(posts(fake)) - before
    return res


def prepared(env, monkeypatch, state, listed, offered):
    c, db, token, fake = env
    info = STATES[state](env)
    if info.get("review"):
        monkeypatch.setenv("COPILOT_FEATURE_OUTCOME", "true")
    set_read(fake, body_for(db, listed, offered, own_pkg=info["own"], own_files=info["own_files"],
                            review=bool(info.get("review"))))
    fake.post_send_get_bodies = [fake.pre_send_get_body] * 6
    return info, possible(db, fake, info["page"])


@pytest.mark.parametrize("state,listed,offered", CELLS, ids=CELL_IDS)
def test_the_principle_holds_in_every_cell(env, monkeypatch, state, listed, offered):
    c, db, token, fake = env
    info, before = prepared(env, monkeypatch, state, listed, offered)
    pkg = info["page"]
    pkg_html = page(c, pkg).text
    with monkeypatch.context() as m:
        if info.get("conflict"):
            m.setattr(routes_package, "_checks_for", lambda *args, **kwargs: ([SimpleNamespace(state="conflict")], None))
        dispute_html = "".join(c.get(f"/disputes/{DID}" + view).text for view in ("", "/workspace/prepare", "/workspace/send"))
    after_dispute = possible(db, fake, pkg)
    send = post_result(c, fake, token, pkg, "send")
    row = {"before": before, "after_dispute": after_dispute, "pkg": scan(pkg_html), "dispute": scan(dispute_html),
           "send": send, "conflict": bool(info.get("conflict"))}
    found = violations(row)
    assert not found, " | ".join(found)


@pytest.mark.parametrize("route, data", [("check", {}), ("confirm-not-received", {"confirmed": "on"})],
                         ids=["check", "confirm-not-received"])
@pytest.mark.parametrize("state,listed,offered", CELLS, ids=CELL_IDS)
def test_the_result_of_check_again_and_of_the_mark_follows_the_principle(env, monkeypatch, state, listed, offered,
                                                                         route, data):
    c, db, token, fake = env
    info, before = prepared(env, monkeypatch, state, listed, offered)
    res = post_result(c, fake, token, info["page"], route, data)
    found = result_violations(route, res, before["listed_norm"], before["new_version_send_possible"],
                              before["link_err"] is None)
    assert not found, " | ".join(found)
