"""A calendar file (.ics) for the response due date PayPal gave.

Pure and standard-library only: text in, text out, no request, no file, no clock of its own. The due instant is PayPal's
own seller_response_due_date, passed in by the caller; this module never computes, shifts or guesses a deadline (the one
offset it writes is the fixed reminder, 24 hours before PayPal's instant). Lines end with CRLF, text values are escaped and
no content line is longer than 75 octets (RFC 5545 sections 3.1 and 3.3.11).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

PRODID = "-//Proofie//Dispute due date//EN"
SUMMARY = "Respond to PayPal dispute {dispute_id}"
DESCRIPTION = ("PayPal's response due date for this dispute, as read from PayPal. Open Proofie to review it. "
               "Proofie sends nothing on its own.")
ALARM_DESCRIPTION = "The response to PayPal dispute {dispute_id} is due in 24 hours."
ALARM_TRIGGER = "-PT24H"
EVENT_LENGTH = "PT15M"
LINK_TEXT = "Add the due date to a calendar (.ics file)"
REFUSAL_TITLE = "No due date to add"
REFUSAL_MESSAGE = ("PayPal gave no response due date for this dispute, so there is nothing to add to a calendar. "
                   "Nothing was changed.")
FILE_NAME = "proofie-due-{dispute_id}.ics"
MEDIA_TYPE = "text/calendar; charset=utf-8"
LIMIT = 75                                      # octets per content line, the line break not counted


def uid(dispute_id: str) -> str:
    """Stable for one dispute: the same file imported twice replaces the first entry. No host-like part."""
    return f"dispute-{dispute_id}-due"


def file_name(dispute_id: str) -> str:
    """The download name, built only from an id made of letters, digits and hyphens (matched whole); any other id, such as one
    with a trailing line break, is refused so it can never reach a header."""
    if not isinstance(dispute_id, str) or re.fullmatch(r"[A-Za-z0-9-]+", dispute_id) is None:
        raise ValueError("dispute_id must hold only letters, digits and hyphens")
    return FILE_NAME.format(dispute_id=dispute_id)


def escape(value: str) -> str:
    """TEXT value escaping: backslash first, then semicolon, comma and line breaks."""
    return (str(value).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n"))


def fold(line: str) -> str:
    """Folds one content line at 75 octets (continuation lines start with one space and carry at most 74 octets of text),
    never inside a multi-byte character."""
    raw = line.encode("utf-8")
    if len(raw) <= LIMIT:
        return line
    parts, room = [], LIMIT
    while raw:
        take = len(raw)
        if take > room:
            take = room
            while take > 0 and (raw[take] & 0xC0) == 0x80:
                take -= 1
        parts.append(raw[:take].decode("utf-8"))
        raw = raw[take:]
        room = LIMIT - 1
    return "\r\n ".join(parts)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def due_calendar(dispute_id: str, due_instant: datetime, now: datetime) -> str:
    """The calendar text for one dispute. `due_instant` is PayPal's response due instant and `now` the stamp of this file;
    both must carry a time zone (a naive instant is not a known instant, so it is refused rather than guessed)."""
    for name, moment in (("due_instant", due_instant), ("now", now)):
        if not isinstance(moment, datetime) or moment.tzinfo is None:
            raise ValueError(f"{name} must be a datetime with a time zone")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{escape(uid(dispute_id))}",
        f"DTSTAMP:{_stamp(now)}",
        f"DTSTART:{_stamp(due_instant)}",
        f"DURATION:{EVENT_LENGTH}",
        f"SUMMARY:{escape(SUMMARY.format(dispute_id=dispute_id))}",
        f"DESCRIPTION:{escape(DESCRIPTION)}",
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        f"TRIGGER:{ALARM_TRIGGER}",
        f"DESCRIPTION:{escape(ALARM_DESCRIPTION.format(dispute_id=dispute_id))}",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "".join(fold(line) + "\r\n" for line in lines)
