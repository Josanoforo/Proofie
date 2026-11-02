"""Store the seller's shipment-time label and parcel photos with a code timestamp.

No OCR, model, provider access, or PayPal write is performed here.
"""
from __future__ import annotations

import json
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from copilot import config
from copilot.core import carriers
from copilot.core.content_type import detect_content_type
from copilot.core.raw_storage import write_raw
from copilot.db import repo
from copilot.web.security import validate_dispute_id
from copilot.features.fulfilment.tracker_request import validate_capture_id

CAPTURE_ID = re.compile(r"^[A-Za-z0-9]{17}$")
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MIMES = {"image/jpeg": (".jpg", lambda b: b.startswith(b"\xff\xd8\xff")),
         "image/png": (".png", lambda b: b.startswith(b"\x89PNG\r\n\x1a\n"))}


def _directory(capture_id: str) -> Path:
    if not isinstance(capture_id, str) or not CAPTURE_ID.fullmatch(capture_id):
        raise ValueError("Enter a valid 17-character transaction ID.")
    return config.uploads_dir() / "ship_moment_capture" / capture_id


def read(capture_id: str) -> dict | None:
    path = _directory(capture_id) / "record.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(data, dict) or data.get("capture_id") != capture_id:
        raise ValueError("Shipment record is invalid.")
    if data.get("dispute_id") is not None and validate_dispute_id(data.get("dispute_id")):
        raise ValueError("Shipment case reference is invalid.")
    return data


def clean_tracking_number(value) -> str | None:
    """The tracking number as the register step will send it: whitespace removed, 1 to 64 printable characters, or None."""
    number = "".join(str(value or "").split())
    if not number:
        return None
    if len(number) > carriers.TRACKING_MAX_LENGTH or not number.isprintable():
        raise ValueError(f"The tracking number must be 1 to {carriers.TRACKING_MAX_LENGTH} characters without spaces.")
    return number


def save(capture_id: str, carrier: str, label: tuple[str, str, bytes] | None,
         parcel: tuple[str, str, bytes] | None, *, now: datetime | None = None,
         dispute_id: str | None = None, tracking_number: str | None = None) -> dict:
    """Save one or both bounded, signature-checked seller images, a carrier name and, optionally, the tracking number."""
    directory = _directory(capture_id)
    if dispute_id is not None and validate_dispute_id(dispute_id):
        raise ValueError("Enter a valid case reference before linking shipment photos.")
    carrier = " ".join(str(carrier or "").split())
    if not carrier or len(carrier) > 80 or not carrier.isprintable():
        raise ValueError("Enter a carrier name of 1 to 80 printable characters.")
    number = clean_tracking_number(tracking_number)
    if label is None and parcel is None and number is None:
        raise ValueError("Choose a label photo, parcel photo, or both, or enter the tracking number.")
    prepared: list[tuple[str, bytes, str]] = []
    for slot, item in (("label", label), ("parcel", parcel)):
        if item is None:
            continue
        _filename, content_type, payload = item
        if not isinstance(payload, bytes) or not payload or len(payload) > MAX_IMAGE_BYTES:
            raise ValueError("Each photo must be between 1 byte and 5 MB.")
        signature = MIMES.get(content_type.lower().split(";")[0].strip())
        if signature is None or not signature[1](payload):
            raise ValueError("Photos must be valid JPEG or PNG images.")
        prepared.append((slot, payload, signature[0]))
    directory.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    for slot, payload, suffix in prepared:
        target = directory / (slot + suffix)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(payload)
        temporary.replace(target)
        files[slot] = target.name
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
    previous = read(capture_id)
    kept_files = dict(previous.get("files") or {}) if previous else {}
    kept_files.update(files)
    record = {"capture_id": capture_id, "carrier": carrier, "captured_at": stamp, "files": kept_files,
              "source": "seller-entered"}
    kept_number = number or (previous or {}).get("tracking_number")
    if kept_number:
        record["tracking_number"] = kept_number
    linked_dispute = dispute_id or (previous or {}).get("dispute_id")
    if linked_dispute:
        record["dispute_id"] = linked_dispute
    target = directory / "record.json"
    temporary = directory / "record.json.tmp"
    temporary.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temporary.replace(target)
    return record


def photo(capture_id: str, kind: str) -> tuple[bytes, str, str] | None:
    """Read one signature-checked seller original named in the capture record, never an arbitrary path."""
    if kind not in ("label", "parcel"):
        return None
    record = read(capture_id)
    filename = (record or {}).get("files", {}).get(kind)
    if not isinstance(filename, str) or Path(filename).name != filename:
        return None
    root = _directory(capture_id).resolve()
    path = root / filename
    if path.is_symlink() or path.resolve().parent != root or not path.is_file():
        return None
    payload = path.read_bytes()
    content_type = detect_content_type(payload)
    if content_type not in MIMES:
        return None
    return payload, content_type, filename


def case_documents(dispute_id: str, capture_id: str, record: dict) -> list[dict]:
    """Return only document rows whose hash and generated name match these exact capture originals."""
    if validate_dispute_id(dispute_id) or validate_capture_id(capture_id):
        return []
    if not isinstance(record, dict) or record.get("capture_id") != capture_id or record.get("dispute_id") != dispute_id:
        return []
    rows = repo.list_documents(config.db_path(), dispute_id)
    linked = []
    for kind in ("label", "parcel"):
        item = photo(capture_id, kind)
        if item is None:
            continue
        payload, content_type, _filename = item
        digest = hashlib.sha256(payload).hexdigest()
        suffix = ".jpg" if content_type == "image/jpeg" else ".png"
        filename = f"shipment-{kind}-{capture_id}-{digest[:12]}{suffix}"
        row = next((doc for doc in rows if doc.get("filename") == filename and doc.get("sha256") == digest), None)
        if row:
            linked.append({key: row.get(key) for key in ("id", "filename", "content_type", "size_bytes", "sha256")})
    return linked


def link_case_documents(dispute_id: str, capture_id: str, record: dict) -> list[dict]:
    """Expose exact shipment-photo bytes as seller-entered case documents; attachment remains a separate human action."""
    if validate_dispute_id(dispute_id) or validate_capture_id(capture_id):
        raise ValueError("The case or transaction reference is invalid.")
    if not isinstance(record, dict) or record.get("capture_id") != capture_id or record.get("dispute_id") != dispute_id:
        raise ValueError("Shipment photos are not linked to this case and transaction.")
    rows = repo.list_documents(config.db_path(), dispute_id)
    linked: list[dict] = []
    for kind in ("label", "parcel"):
        item = photo(capture_id, kind)
        if item is None:
            continue
        payload, content_type, _filename = item
        digest = hashlib.sha256(payload).hexdigest()
        suffix = ".jpg" if content_type == "image/jpeg" else ".png"
        filename = f"shipment-{kind}-{capture_id}-{digest[:12]}{suffix}"
        row = next((doc for doc in rows if doc.get("filename") == filename and doc.get("sha256") == digest), None)
        if row is None:
            write_raw(config.uploads_dir(), digest, payload)
            document_id = repo.insert_document(config.db_path(), dispute_id, filename, content_type,
                                               len(payload), digest, None, False)
            row = {"id": document_id, "filename": filename, "content_type": content_type,
                   "size_bytes": len(payload), "sha256": digest, "extraction_ok": False,
                   "source": "seller-entered shipment photo; contents not authenticated"}
            rows.append(row)
        linked.append({key: row.get(key) for key in ("id", "filename", "content_type", "size_bytes", "sha256")})
    return linked


REGISTERED_STATES = ("registered", "registered_unconfirmed", "unknown")


def registration_offer(capture_id: str, record: dict | None, dispute_id: str = "") -> dict | None:
    """What the saved shipment record lets the seller register with PayPal. Reads local records only: no PayPal call,
    nothing is sent. `href` opens the existing tracking form prefilled; the seller still reviews the exact body and
    approves it there (the check page and its hash), so this block never writes.

    state: no_number (nothing to register yet) | ready (catalogue carrier) | other_carrier (OTHER plus the typed name,
    the seller confirms it there) | registered (a stored file says it was sent) | None (no record)."""
    if not isinstance(record, dict) or record.get("capture_id") != capture_id:
        return None
    from copilot.features.fulfilment import store
    number, carrier_text = record.get("tracking_number"), record.get("carrier") or ""
    match = carriers.map_carrier(carrier_text)
    parts = ([f"from_dispute={dispute_id}"] if dispute_id else [])
    if number:
        parts += [f"prefill_tracking={quote(number, safe='')}", f"prefill_carrier={quote(match.code or '', safe='')}"]
        status_part = "prefill_status=SHIPPED"   # the shipment-time page records a shipment; the seller can still change it
        if match.code == "OTHER":
            parts.append(f"prefill_carrier_name={quote(match.carrier_name_other or carrier_text, safe='')}")
        parts.append(status_part)
    query = "?" + "&".join(parts) if parts else ""
    offer = {"tracking_number": number, "carrier_text": carrier_text, "code": match.code,
             "fields": {"tracking_number": number or "", "carrier": match.code or "", "status": "SHIPPED",
                        "carrier_other_name": (match.carrier_name_other or carrier_text) if match.code == "OTHER" else "",
                        "from_dispute": dispute_id or ""},
             "carrier_label": (carriers.display_name(match.code) if match.code and match.code != "OTHER" else carrier_text),
             "href": f"/fulfilment/{capture_id}{query}", "file_href": None, "file_status": None}
    saved = store.get_file(capture_id)
    status = store.effective_status(saved) if saved else None
    if status in REGISTERED_STATES:
        offer.update(state="registered", file_status=status, file_href=f"/fulfilment/{capture_id}/file/{saved['id']}")
        return offer
    if not number:
        offer["state"] = "no_number"
    else:
        offer["state"] = "ready" if match.code and match.code != "OTHER" else "other_carrier"
    return offer
