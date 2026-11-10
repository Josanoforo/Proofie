"""Seller words and six-state visual vocabulary. No business decisions or writes."""
import re
import string

from markupsafe import Markup

_STATES = {
    "verified": ("ok", "Verified"), "conflict": ("bad", "Does not match"),
    "missing": ("bad", "Missing needed detail"), "present_unverifiable": ("warn", "Cannot verify"),
    "not_applicable": ("na", "Not applicable"),
    "sent_confirmed": ("ok", "Confirmed by reading PayPal's record"),
    "sent_pending_settle": ("warn", "Sent; pending PayPal confirmation"),
    "refused_422": ("bad", "Refused while PayPal was updating the case"),
    "rejected_hash": ("bad", "Blocked: approval does not match"),
    "rejected_link": ("bad", "Blocked: PayPal offers no valid action"),
    "rejected_body": ("bad", "Blocked: request needs correction"),
    "read_failed": ("warn", "Cannot verify: PayPal could not be read"),
    "finished": ("info", "Send attempt finished; check its outcome"),
    "SHIPPED": ("info", "Shipped"),
    "DELIVERED": ("info", "Delivered, as recorded by PayPal"), "IN_TRANSIT": ("info", "In transit"),
    "PARTIALLY_DELIVERED": ("warn", "Partially delivered"), "ON_HOLD": ("warn", "On hold"),
    "LOCAL_PICKUP": ("info", "Local pickup"), "CANCELLED": ("info", "Canceled"),
}
_ACTIONS = {
    "upload_document": "Added a document", "exclude_document": "Excluded a document",
    "include_document": "Included a document", "set_manual_fields": "Saved shipment details",
    "confirm_document_reading": "Confirmed a document reading", "generate_package": "Prepared a package",
    "approve_package": "Approved a package", "add_claim": "Added a statement", "edit_claim": "Edited a statement",
    "send_attempt_registered": "Prepared a send attempt", "send_confirmed": "Confirmed the evidence by reading PayPal",
    "send_result_pending": "Awaiting PayPal confirmation", "send_outcome_unknown": "Could not confirm the send outcome",
    "send_missing_attachment": "Could not confirm every attachment", "send_failed": "PayPal refused the send",
    "active_send_sources_changed": "Detected a source change during sending",
    "invalidate_active_approval": "Cleared an outdated approval", "invalidate_approval_after_edit": "Cleared approval after an edit",
    "abandoned_attempt_later_confirmed_by_paypal": "Confirmed an earlier attempt by reading PayPal",
    "offer": "Offer a refund", "message": "Send message to buyer",
    'abandoned_by_vendor': 'Marked an attempt as not received',
    'approve_despite_conflict': 'Approved despite a reported difference',
    'approve_outcome_appeal': 'Approved an appeal request locally',
    'attach_document': 'Attached a document to the package',
    'auto_prep_ai_draft': 'Prepared a draft with the assistant',
    'auto_prep_ai_unavailable': 'Continued draft preparation by hand',
    'auto_prep_checklist_built': 'Checked the draft requirements',
    'auto_prep_claimed': 'Started draft preparation',
    'auto_prep_context_fetched': 'Read the case details for draft preparation',
    'auto_prep_crashed': 'Draft preparation stopped unexpectedly',
    'auto_prep_failed': 'Could not prepare a draft',
    'auto_prep_manual_draft': 'Prepared a manual draft',
    'auto_prep_needs_documents': 'Draft preparation needs documents',
    'confirm_vision_reading': 'Confirmed an image reading',
    'detach_document': 'Removed a document from the package',
    'draft_message': 'Prepared a message draft',
    'finish_outcome_appeal': 'Recorded an appeal attempt outcome',
    'fulfilment_registered': 'Recorded the tracking registration result',
    'fulfilment_checked': 'Checked the tracking in PayPal',
    'outcome_appeal': 'Recorded an appeal request',
    'reserve_outcome_appeal': 'Reserved an appeal attempt',
    'send_attempt_reclaimed_stale_pending': 'Checked an older pending send reservation',
    'send_attempt_superseded': 'Replaced an earlier send reservation',
    'start_manual_package': 'Started a package by hand',
    'start_manual_package_ai_unavailable': 'Started a package by hand while the assistant was unavailable',
    'start_manual_package_no_documents': 'Started a package without readable documents',
    'upload_pdf_reader_unavailable': 'Could not read the uploaded PDF',
    'vision_claims_suggested': 'Suggested statements from an image',
    'webhook_event': 'Recorded a PayPal notification',
    "resolution_offer": "Recorded a refund offer attempt",
    "resolution_message": "Recorded a message attempt",
    'mcp_list_disputes': 'An assistant requested the dispute list',
    'mcp_get_dispute_file': 'An assistant requested the case file',
    'mcp_what_is_missing': 'An assistant requested the missing details',
    'mcp_prepare_draft': 'An assistant requested a draft',
    'mcp_get_send_status': 'An assistant requested the send status',
    'mcp_send_approved_package': 'An assistant requested an approved send',
    'mcp_send_approved_package_result': 'Recorded the assistant send request outcome',

}


# The attempt, tracking and PayPal dispute states have ONE vocabulary, the tables of status.py (the package page
# reads them). state_kind and state_label no longer keep a second wording for them: an attempt-store state goes through
# status.evidence_status and a tracking state through status.tracking_status. _STATES keeps what status.py does not own
# (check states, message results, PayPal's closed dispute and tracking enums).
_ATTEMPT_STORE_STATE = {"pending": "reserved", "reserved": "reserved", "sent_unconfirmed": "attempted",
                        "confirmed": "confirmed", "failed": "refused", "blocked": "blocked", "abandoned": "abandoned"}
_TRACKING_STATE = ("registered", "registered_unconfirmed", "saved_without_registration")


def _owned_view(value):
    """The StatusView of status.py for an attempt-store or tracking state, or None when status.py does not own `value`."""
    from copilot.web import status
    if not isinstance(value, str):
        return None
    if value in _ATTEMPT_STORE_STATE:
        return status.evidence_status(status.SendRecord(state=_ATTEMPT_STORE_STATE[value]))
    if value in _TRACKING_STATE:
        return status.tracking_status(value)
    if value in status._PAYPAL_STATUS:       # PayPal's dispute status: the closed table of status.py, one class per word
        kind, label = status.DISPUTE_TABLE[status._PAYPAL_STATUS[value]]
        return status.StatusView(value, label, kind)
    return None


def state_kind(value):
    if value == "abandoned":
        return "bad"        # an abandoned attempt is red wherever it is listed (docs/DESIGN.md section 5); only its words are shared
    view = _owned_view(value)
    return view.kind if view is not None else _STATES.get(value, ("na", "Not available"))[0]


def state_label(value):
    view = _owned_view(value)
    return view.label if view is not None else _STATES.get(value, ("na", "Not available"))[1]


def action_label(value):
    return _ACTIONS.get(value, "Other saved app activity")


def stage_label(value):
    """PayPal's stage in the seller's words (stage_name): PRE_ARBITRATION and ARBITRATION have names, a missing stage reads
    'Not returned by PayPal', an unknown one 'Stage not recognized'."""
    return stage_name(value)


# Stage table: PayPal's own four dispute_lifecycle_stage values, in the order of the pinned
# disputes schema (copilot/features/conformance/fixtures/disputes.json), names only: nothing is derived from the chain and
# nothing is predicted (docs/CONFORMANCE.md row 14). CLAIM is not in PayPal's contract; it stays only as a legacy alias that
# names the fixtures' value and is never part of the strip. stage_label reads this table through stage_name.
STAGE_TABLE = (("INQUIRY", "Inquiry"), ("CHARGEBACK", "Chargeback"), ("PRE_ARBITRATION", "Pre-arbitration"),
               ("ARBITRATION", "Arbitration"))
STAGE_LEGACY_ALIASES = {"CLAIM": "Claim"}
STAGE_STRIP_LABEL = "Where this dispute is in PayPal's process"
STAGE_CURRENT_WORDS = "current stage"


STAGE_NOT_RETURNED = "Not returned by PayPal"
STAGE_UNKNOWN = "Stage not recognized"


def stage_name(value) -> str:
    """The seller's word for PayPal's stage: the table's name, or the legacy alias for CLAIM. A stage PayPal did not return
    (missing or empty) reads STAGE_NOT_RETURNED; a value that is present but is not one PayPal defines reads STAGE_UNKNOWN.
    Never the raw value."""
    if isinstance(value, str):
        for stage, name in STAGE_TABLE:
            if stage == value:
                return name
        if value in STAGE_LEGACY_ALIASES:
            return STAGE_LEGACY_ALIASES[value]
        return STAGE_NOT_RETURNED if not value.strip() else STAGE_UNKNOWN
    return STAGE_NOT_RETURNED if value is None else STAGE_UNKNOWN


def stage_strip(value):
    """The four PayPal stages in PayPal's order as a tuple of {"value", "name", "current"} dicts, exactly one with
    current=True; None when `value` is not one of the four (missing, unknown or the legacy CLAIM), so no strip shows."""
    if not isinstance(value, str) or value not in dict(STAGE_TABLE):
        return None
    return tuple({"value": stage, "name": name, "current": stage == value} for stage, name in STAGE_TABLE)


# --------------------------------------------------------------------- Shared seller wording
# The eight primary labels: always large (filled when the control is the page's next step, large outlined otherwise).
LARGE_LABELS = ("Attach to the response", "Review each statement and approve", "Approve this package",
                "Send evidence to PayPal", "Review the message", "Send message to buyer", "Add tracking in PayPal",
                "Resolution options and buyer messages")

GUIDED_STEPS = ("Upload documents", "Prepare evidence package", "Review each statement and approve",
                "Send evidence to PayPal")
READS_ONLY_NOTE = "Reads the dispute from PayPal. Sends nothing."


def step_caption(n) -> str:
    return f"Step {n} of {len(GUIDED_STEPS)}: {GUIDED_STEPS[int(n) - 1]}"


# One confirmation sentence per completed action. The facts are ints or names read from stored rows (never request
# text); a missing fact gives None and the page shows no confirmation rather than a guessed one.
_DONE = {
    "document_added": "Document added: {filename}. Next: generate the evidence package.",
    "document_added_package": "Document added: {filename}. Next: attach it to the response.",
    "document_excluded": ("{filename} is left out of the checks. It is not sent to PayPal either: it is no longer part of "
                          "a response you have not sent. You can use it in the checks again from Documents."),
    "document_included": "{filename} is used in the checks again.",
    "statement_added": "Statement {n} added.",
    "statement_saved": "Changes to statement {n} saved.",
    "statement_removed": "Statement {n} removed.",
    "shipment_saved": "Shipment details saved.",
    "reading_confirmed": "Reading confirmed: {field} {value}.",
    "attached": "{filename} is in the response.",
    "detached": "{filename} was removed from the response.",
    "approved": "Package approved at {when}. Nothing has been sent. Next: send it to PayPal.",
    "approval_cleared": "Your earlier approval was cleared because the content changed. Review and approve again.",
}


def _fill(template: str, facts: dict):
    needed = [name for _, name, _, _ in string.Formatter().parse(template) if name]
    if any(facts.get(name) is None or str(facts.get(name)).strip() == "" for name in needed):
        return None
    return template.format(**{name: facts[name] for name in needed})


def done_message(key, **facts):
    """The exact confirmation for a finished action, or None for an unknown key or a missing fact."""
    template = _DONE.get(key)
    return None if template is None else _fill(template, facts)


# Undo control of the one-time banner. Only these two keys are undoable; the opposite keys
# ('attached', 'document_included'), statement removal and 'approved' carry none, so there is no ping-pong. The value is the
# visually-hidden context that follows the visible word 'Undo' in the button.
_UNDO_CONTEXT = {
    "document_excluded": "leaving out {filename}",
    "detached": "removing {filename} from the response",
}


def done_undo(key, post, filename, fields=()):
    """The `undo` argument of the done_banner macro: {"post": url, "hidden": context, "fields": ((name, value), ...)}, or
    None for a key without undo, a missing url or a missing file name (the banner then shows no control)."""
    template = _UNDO_CONTEXT.get(key)
    if template is None or not post or filename is None or str(filename).strip() == "":
        return None
    return {"post": str(post), "hidden": template.format(filename=filename), "fields": tuple(tuple(f) for f in fields)}


_DELIVERY_BLOCK = {
    "unsupported": ("Statement {n} says “{phrase}”, but no document it cites shows that. Change the wording (for "
                    "example to “shipped”, if that is what happened), then approve."),
    "unreadable": ("Statement {n} says “{phrase}” and cites {filename}, which Proofie could not read. Change the "
                   "wording (for example to “shipped”, if that is what happened)."),
    "conflict": "Statement {n} relies on a document that does not match PayPal's data for the tracking number.",
    "untrusted": "Statement {n} cites {filename}, which Proofie cannot use as proof of delivery.",
}
DELIVERY_WORDS_HINT = ("These words claim the parcel arrived: delivered, signed for, received by, entregado, firmado, "
                       "recibido. Use “shipped” or “in transit” unless a carrier document shows delivery.")
# the same sentence with the Spanish words marked as Spanish and kept out of browser translation. Templates render it
# as is (it is Markup, so it is not escaped); the plain string above stays for text-only uses.
DELIVERY_WORDS_HINT_HTML = Markup(
    "These words claim the parcel arrived: delivered, signed for, received by, "
    '<span lang="es" translate="no">entregado</span>, <span lang="es" translate="no">firmado</span>, '
    '<span lang="es" translate="no">recibido</span>. Use “shipped” or “in transit” unless a carrier document shows delivery.')


_SPANISH_DELIVERY_WORD = re.compile(r"^\s*(entregad[oa]|firmad[oa]|recibid[oa])\s*$", re.I)


def delivery_block_message(number, phrase, kind, filename=None):
    """One sentence for a blocked delivery statement. A plain str, except when the quoted phrase is one of the Spanish
    delivery words (evidence_model matches entregado/a, firmado/a, recibido/a): then it is Markup with that word in a
    <span lang="es" translate="no">. Every other fact (the file name) is escaped when the Markup is built, so a
    template prints it as is: {{ item }} and never |safe or str() around it."""
    template = _DELIVERY_BLOCK.get(kind)
    if template is None:
        return None
    text = _fill(template, {"n": number, "phrase": phrase, "filename": filename})
    if text is None or "{phrase}" not in template or not _SPANISH_DELIVERY_WORD.match(str(phrase)):
        return text
    marked = Markup('<span lang="es" translate="no">{}</span>').format(str(phrase).strip())
    return Markup(template).format(n=number, phrase=marked, filename=filename)


# A standalone date only: never a run inside an identifier (a tracking number, a reference, a path or a timestamp).
_ISO_DATE = re.compile(r"(?<![\w/.-])(\d{4})-(\d{2})-(\d{2})(?![\w/-]|\.\w)")


def dates_in_words(text):
    """ISO dates inside a sentence read as '5 October 2026' (the display format of form_fields.date_words). Only a
    valid calendar date changes; anything else, and non-text values, pass through. For text produced by code that quotes a
    date (rule sets, sources); the stored value is untouched."""
    from copilot.web.form_fields import date_words
    if not isinstance(text, str):
        return text

    def word(match):
        words = date_words(match.group(0))
        return match.group(0) if words == "Not available" else words
    return _ISO_DATE.sub(word, text)


# Spanish phrases that rule sets put into English sentences: the Spanish source title, the Spanish page and section a
# quotation was read from, and the Spanish document names inside a check's citation. They are marked lang="es" so a screen
# reader switches voice and a browser can translate them (WCAG 3.1.2).
_CITED_SPANISH = ("Comprobante de entrega", "entregado", "ocurre")
_SPANISH_HINT = re.compile(r"[áéíóúñÁÉÍÓÚÑ]")
_SPANISH_FUNCTION_WORD = re.compile(r"\b(?:de|del|el|la|los|las|al|que|y)\b")


def _looks_spanish(text):
    return bool(_SPANISH_HINT.search(text) and _SPANISH_FUNCTION_WORD.search(text))


_SPANISH_CACHE = []


def _spanish_phrases():
    """Longest first, so a title is wrapped whole before any phrase inside it. Read from the shipped rule sets."""
    if not _SPANISH_CACHE:
        from copilot.core import checklist
        found = set(_CITED_SPANISH)
        for country in checklist.supported_countries():
            rules = checklist.load_rules(country=country)
            if _looks_spanish(rules.source_title):
                found.add(rules.source_title)
            for row in ((rules.raw or {}).get("eligibility_notes") or {}).get("rows") or []:
                if row.get("quote_lang") == "es":
                    found.update(str(row["citation"].get(key)) for key in ("page_title", "section") if row["citation"].get(key))
            for rule in (rules.checks or {}).values():                # rc4.3 W1-3: the clause behind a check cites the same way
                clause = rule.get("clause") or {}
                if clause.get("quote_lang") == "es":
                    found.update(str(clause[key]) for key in ("page_title", "section") if clause.get(key))
        _SPANISH_CACHE.append(sorted(found, key=len, reverse=True))
    return _SPANISH_CACHE[0]


def mark_spanish(text):
    """Text with every known Spanish phrase wrapped in <span lang="es">; the rest is escaped as usual. Markup. A value
    that is not text passes through. The phrases come from the shipped rule sets, so a Spanish rule title or section is
    marked wherever a template prints it through this filter."""
    if text is None or isinstance(text, Markup) or not isinstance(text, str):
        return text
    pattern = re.compile("|".join(re.escape(p) for p in _spanish_phrases()))
    out, last = [], 0
    for match in pattern.finditer(text):
        out.append(Markup.escape(text[last:match.start()]))
        out.append(Markup('<span lang="es">{}</span>').format(match.group(0)))
        last = match.end()
    out.append(Markup.escape(text[last:]))
    return Markup("").join(out)


def reason_words_filter(reason):
    from copilot.core import paypal_words
    return paypal_words.reason_words(reason)


def send_refusal_reason(technical):
    """One sentence for why PayPal refused an evidence send, from the stored technical line ('status=400 issue=X ...')."""
    from copilot.core import paypal_words
    _status, issue = paypal_words.parse_technical(technical)
    return paypal_words.cause_of(issue) or paypal_words.NOT_SAID


_SENTENCE_BREAK = re.compile(r"(?<=[.!?]) +(?=[A-Z])")


def note_lines(text):
    """The hosted privacy note with one sentence per line. Each sentence becomes a
    <span class="note-line"> (display:block in CSS) inside the existing <p>; a space follows every span, so the text with the
    tags removed equals `text` exactly. Sentences are escaped; the result is Markup. Splits only at '. ' + capital letter."""
    parts = _SENTENCE_BREAK.split(str(text))
    return Markup(" ").join(Markup('<span class="note-line">{}</span>').format(part) for part in parts)


def install(env) -> None:
    """Registers the shell's template helpers on one Jinja environment. Called once per environment by the shell
    bootstrap (routes_disputes.py, routes_package.py); calling it again changes nothing."""
    from copilot.core import carriers
    from copilot.web import field_words, links, status, timefmt
    from copilot.web.dispute_state import BUYER_CLOSED_LABEL, REVIEWING_NEUTRAL_TEXT
    env.filters["when"] = timefmt.when
    env.filters["when_text"] = timefmt.when_text
    env.filters["dates_in_words"] = dates_in_words              # ISO dates inside generated sentences read as words
    env.filters["mark_spanish"] = mark_spanish                  # Spanish phrases of the rule sets get lang="es"
    env.filters["reason_words"] = reason_words_filter           # a stored PayPal refusal reason, in words
    env.filters["send_refusal_reason"] = send_refusal_reason    # PayPal's reason for a refused send, in words
    env.filters["note_lines"] = note_lines                      # hosted note, one sentence per line
    env.filters["field_word"] = field_words.field_word          # seller words for a field key (a closed table)
    env.globals.update(tracking_file_label=lambda value: status.tracking_status(value).label,   # a tracking file status, closed table
                       field_word=field_words.field_word, resolution_center_link=links.resolution_center_link,
                       RESOLUTION_CENTER_URL=links.RESOLUTION_CENTER_URL, stage_name=stage_name, stage_strip=stage_strip,
                       STAGE_STRIP_LABEL=STAGE_STRIP_LABEL, STAGE_CURRENT_WORDS=STAGE_CURRENT_WORDS, done_undo=done_undo)
    env.globals.update(dispute_status=status.dispute_status, step_caption=step_caption, done_message=done_message,
                       LARGE_LABELS=LARGE_LABELS, READS_ONLY_NOTE=READS_ONLY_NOTE, DELIVERY_WORDS_HINT_HTML=DELIVERY_WORDS_HINT_HTML,
                       CARRIER_OPTIONS=carriers.SELECT_OPTIONS, carrier_display_name=carriers.display_name,
                       BUYER_CLOSED_LABEL=BUYER_CLOSED_LABEL, REVIEWING_NEUTRAL_TEXT=REVIEWING_NEUTRAL_TEXT)


def verdict_line(checks) -> str | None:
    """The one verdict sentence, in the seller's words: what is verified, then what is missing, what does not match and what
    cannot be checked, each named only when there is one. Counts of what is on the page (not-applicable rows left out), never a
    prediction of PayPal's decision. None when there is nothing to count."""
    counted = [c for c in checks or [] if c.state != "not_applicable"]
    if not counted:
        return None
    n = {state: sum(1 for c in counted if c.state == state) for state in ("verified", "missing", "conflict", "present_unverifiable")}
    line = f"{n['verified']} {'check' if n['verified'] == 1 else 'checks'} verified."
    if n["missing"]:
        line += f" {n['missing']} missing."
    if n["conflict"]:
        line += f" {n['conflict']} {'does' if n['conflict'] == 1 else 'do'} not match."
    if n["present_unverifiable"]:
        line += f" {n['present_unverifiable']} cannot be checked."
    return line
