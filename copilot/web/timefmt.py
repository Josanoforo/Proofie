"""One fixed, English time format for everything the seller sees: `2026-10-05 15:45 CST (UTC-6)`.

Never the operating system's own time-zone name (on this machine `%Z` rendered "Hora estandar central (Mexico)"),
always the zone's abbreviation plus its explicit UTC offset, always in America/Mexico_City (the project's one
convention). The "Checked PayPal" line, the due date PayPal gave and a confirmation time all go through
here so they can be compared at a glance. Presentation only: no decision anywhere depends on these strings.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    CDMX = ZoneInfo("America/Mexico_City")
except Exception:   # noqa: BLE001 - tzdata missing: fall back to the fixed offset Mexico City has used since 2022
    CDMX = timezone(timedelta(hours=-6), "CST")


def fmt(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone(CDMX)
    minutes = int(local.utcoffset().total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    hours, rest = divmod(abs(minutes), 60)
    offset = f"UTC{sign}{hours}" + (f":{rest:02d}" if rest else "")
    return f"{local:%Y-%m-%d %H:%M} {local.tzname()} ({offset})"


def now_label() -> str:
    return fmt(datetime.now(timezone.utc))


def fmt_iso(value) -> str:
    """Stored ISO timestamps and PayPal epoch milliseconds use the same explicit city zone."""
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "Not available"
    try:
        if isinstance(value,datetime):
            return fmt(value)
        if type(value) is int or isinstance(value,str) and re.fullmatch(r"[0-9]+",value):
            millis = int(value)
            if not 0 <= millis <= 253402300799999:
                return "Not available"
            seconds, fraction = divmod(millis,1000)
            return fmt(datetime.fromtimestamp(seconds,timezone.utc) + timedelta(milliseconds=fraction))
        return fmt(datetime.fromisoformat(value.replace("Z","+00:00")))
    except (ValueError,TypeError,OverflowError,OSError,AttributeError):
        return "Not available"


_TEXT_TIME = re.compile(r"(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}) (?:CDMX|CST|CDT|[A-Z]{3,5})\b(?! \(UTC)")


def localize_text(text: str | None) -> str | None:
    """Rewrites `YYYY-MM-DD HH:MM CST|CDMX` fragments produced elsewhere (PayPal's due date, the send banner) into
    the same fixed format. The wall-clock time in those fragments is already Mexico City time, so only the label
    changes; a fragment already in the fixed format is left alone."""
    if not text:
        return text

    def swap(match: re.Match) -> str:
        naive = datetime(int(match.group(1)[:4]), int(match.group(1)[5:7]), int(match.group(1)[8:10]),
                         int(match.group(2)), int(match.group(3)))
        return fmt(naive.replace(tzinfo=CDMX))
    return _TEXT_TIME.sub(swap, text)


# ---------------------------------------------------------------------------- Seller-facing display
# One readable English format for what the seller sees on screen: '16 Oct 2026, 10:10 pm (Mexico City time)'.
# Fixed month table (never month names from the platform's date formatter: those follow the operating system's language), 12-hour clock,
# lowercase am/pm. Evidence, MCP and stored strings keep fmt() / fmt_iso(); nothing decides anything from these.
from html import escape as _escape

from markupsafe import Markup

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
ZONE_NAME = "Mexico City time"
_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")
_LEGACY_LABEL = re.compile(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}) [A-Z]{2,5} \(UTC([+-])(\d{1,2})(?::(\d{2}))?\)")
_LEGACY_BARE = re.compile(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}) (?:CDMX|CST|CDT)")
_NOT_AVAILABLE = "Not available"


def _parse(value):
    """-> ('date', date) | ('time', aware datetime in Mexico City) | None. Never raises."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        if isinstance(value, datetime):
            moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            return ("time", moment.astimezone(CDMX))
        if isinstance(value, date):
            return ("date", value)
        if type(value) is int or isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
            millis = int(value)
            if not 0 <= millis <= 253402300799999:
                return None
            seconds, fraction = divmod(millis, 1000)
            moment = datetime.fromtimestamp(seconds, timezone.utc) + timedelta(milliseconds=fraction)
            return ("time", moment.astimezone(CDMX))
        if not isinstance(value, str):
            return None
        text = value.strip()
        if _DATE_ONLY.fullmatch(text):
            return ("date", date.fromisoformat(text))
        legacy = _LEGACY_LABEL.fullmatch(text)
        if legacy:
            y, mo, d, h, mi, sign, oh, om = legacy.groups()
            offset = timezone(timedelta(hours=int(oh), minutes=int(om or 0)) * (1 if sign == "+" else -1))
            return ("time", datetime(int(y), int(mo), int(d), int(h), int(mi), tzinfo=offset).astimezone(CDMX))
        bare = _LEGACY_BARE.fullmatch(text)
        if bare:
            y, mo, d, h, mi = (int(g) for g in bare.groups())
            return ("time", datetime(y, mo, d, h, mi, tzinfo=CDMX))
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            return None          # no offset = no known instant; core normalize() refuses it too, so no deadline is computed
        return ("time", moment.astimezone(CDMX))
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def instant(value):
    """The aware UTC instant of a timestamp that has one, else None: a date-only value, an ISO text with no offset (its
    instant is unknown, as for display()) and anything unreadable give None. For ordering items, never for a decision."""
    parsed = _parse(value)
    if parsed is None or parsed[0] != "time":
        return None
    return parsed[1].astimezone(timezone.utc)


def _date_text(day) -> str:
    return f"{day.day} {_MONTHS[day.month - 1]} {day.year}"


def display(value, zone: bool = True) -> str:
    """'16 Oct 2026, 10:10 pm (Mexico City time)'; a date-only value gives '16 Oct 2026'; '' when empty;
    'Not available' when it cannot be read, including an ISO text with no offset (its instant is unknown).
    A naive datetime object is UTC."""
    if value is None or value == "":
        return ""
    parsed = _parse(value)
    if parsed is None:
        return _NOT_AVAILABLE
    kind, moment = parsed
    if kind == "date":
        return _date_text(moment)
    hour = moment.hour % 12 or 12
    text = f"{_date_text(moment)}, {hour}:{moment.minute:02d} {'pm' if moment.hour >= 12 else 'am'}"
    return f"{text} ({ZONE_NAME})" if zone else text


def iso(value) -> str:
    """Machine value for <time datetime>: '2026-10-16T22:10:00-06:00' (date-only: '2026-10-16'); '' when unreadable."""
    parsed = _parse(value)
    if parsed is None:
        return ""
    kind, moment = parsed
    return moment.isoformat() if kind == "date" else moment.replace(microsecond=0).isoformat()


def when(value, zone: bool = True):
    """Jinja filter: <time datetime='...'>display</time>; plain text when empty or unreadable."""
    text = display(value, zone)
    machine = iso(value)
    if not text or not machine:
        return Markup(_escape(text))
    return Markup(f'<time datetime="{_escape(machine)}">{_escape(text)}</time>')


def when_text(value, zone: bool = True) -> str:
    """Jinja filter: the same display string without markup (for titles, aria-labels, plain text)."""
    return display(value, zone)


def _plural(n: int, unit: str) -> str:
    return f"{n} {unit}" if n == 1 else f"{n} {unit}s"


def due_display(norm, now=None):
    """The deadline PayPal gave (seller_response_due_date), in words computed here; None when PayPal gave none
    (the page then says 'No verified deadline available')."""
    due_raw = norm.get("seller_response_due_date") if isinstance(norm, dict) else None
    parsed = _parse(due_raw) if due_raw else None
    if parsed is None or parsed[0] != "time":
        return None
    due = parsed[1]
    current = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    current = (current if current.tzinfo else current.replace(tzinfo=timezone.utc)).astimezone(CDMX)
    seconds = int((due - current).total_seconds())
    passed = seconds < 0
    hours = abs(seconds) // 3600
    if passed:
        ago = f"{_plural(hours // 24, 'day')} ago" if hours >= 48 else (
            f"{_plural(hours, 'hour')} ago" if hours >= 1 else "less than an hour ago")
        remaining = f"The due date passed {ago}"
    elif hours >= 48:
        remaining = f"{hours // 24} days left"
    elif due.date() == current.date():
        remaining = f"Due today, in {_plural(hours, 'hour')}" if hours >= 1 else "Due today, in less than an hour"
    else:
        remaining = f"Due in {_plural(hours, 'hour')}" if hours >= 1 else "Due in less than an hour"
    return {"iso": iso(due), "text": f"Respond by {display(due)}", "remaining": remaining, "passed": passed,
            "hours_left": seconds // 3600}
