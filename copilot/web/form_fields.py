"""Shared server parsing for the explicit shipment date form contract."""
from datetime import date
import re
from copilot.core import carriers
from copilot.core.checklist_input import MANUAL_FIELD_NAMES

DATE_FIELDS = ("delivery_date", "ship_date", "capture_date")


def shipment_fields(form):
    values = {str(k): str(v).strip() for k, v in form.items()}
    fields, errors = {}, {}
    for name in MANUAL_FIELD_NAMES:
        if name in DATE_FIELDS:
            parts = [values.get(name + "_" + p, "") for p in ("day", "month", "year")]
            if values.get(name):
                # the native date control posts one ISO value, YYYY-MM-DD; a calendar date or an error, never a guess
                iso = values[name]
                try:
                    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", iso):
                        raise ValueError(iso)
                    fields[name] = {"value": date.fromisoformat(iso).isoformat(), "confidence": "read"}
                except ValueError:
                    errors[name] = "Enter a real date using day, month and year."
            elif any(parts):                # the earlier three-field post (a saved page, a script) still reads the same way
                if (not all(re.fullmatch(r"[0-9]{1,2}", p) for p in parts[:2])
                        or not re.fullmatch(r"[0-9]{4}", parts[2])):
                    errors[name] = "Enter a real date using day, month and year."
                else:
                    try: fields[name] = {"value": date(int(parts[2]), int(parts[1]), int(parts[0])).isoformat(), "confidence": "read"}
                    except ValueError: errors[name] = "Enter a real date using day, month and year."
        elif name == "tracking_number":
            # Receipts print the number in groups: spaces are removed before it is checked and stored, and what
            # was typed stays in `values` so the form shows it back next to its message.
            problem = carriers.tracking_number_problem(values.get(name, ""))
            if problem:
                errors[name] = problem
            elif values.get(name):
                fields[name] = {"value": carriers.normalize_tracking(values[name]), "confidence": "read"}
        elif name == "carrier":
            stored, error_field, message = carriers.carrier_from_form(values.get("carrier", ""),
                                                                      values.get("carrier_other_name", ""))
            if message:
                errors[error_field] = message
            elif stored:
                fields[name] = {"value": stored, "confidence": "read"}
        elif values.get(name):
            value = values[name]
            if len(value) > 1000 or not value.isprintable():
                errors[name] = "Enter a short value without control characters."
            else: fields[name] = {"value": value, "confidence": "read"}
    ship = fields.get("ship_date", {}).get("value")
    delivery = fields.get("delivery_date", {}).get("value")
    if ship and delivery and delivery < ship:
        # One message per field, each naming both dates: two identical lines in the summary look like a bug.
        errors["delivery_date"] = (f"Delivery date ({date_words(delivery)}) is before the ship date "
                                   f"({date_words(ship)}). Change one of the dates.")
        errors["ship_date"] = (f"Ship date ({date_words(ship)}) is after the delivery date "
                               f"({date_words(delivery)}). Change one of the dates.")
    # The summary lists problems in the order the form shows the fields: carrier first, then the tracking number.
    first = [key for key in ("carrier", "carrier_other_name", "tracking_number") if key in errors]
    errors = {**{key: errors[key] for key in first}, **{k: v for k, v in errors.items() if k not in first}}
    return fields, errors, values


def date_words(value):
    if not value: return ""
    try: parsed = date.fromisoformat(str(value))
    except ValueError: return "Not available"
    months = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")
    return f"{parsed.day} {months[parsed.month-1]} {parsed.year}"
