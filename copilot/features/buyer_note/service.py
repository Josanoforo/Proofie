from __future__ import annotations

from datetime import date, datetime, timezone
import json
import re
import sqlite3
from pathlib import Path

from copilot import config
from copilot.db import repo
from copilot.features.carrier_directory.service import carriers
from copilot.core.carriers import KNOWN_CODES, map_carrier
from copilot.features.fulfilment.tracker_request import validate_capture_id
from copilot.web.security import validate_dispute_id

_PRIVATE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\bPP-[A-Z0-9-]{5,}\b|\d{9,}", re.I)
_MONTHS_ES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")
DELIVERY_INCIDENTS = {"delivered_not_received", "delay", "missing"}
DELIVERY_INCIDENT_RESPONSES = {
    "buyer_says_still_missing", "buyer_reports_delay", "buyer_says_arrived", "no_response_yet", "other",
}


def delivery_incident_content(incident: str, locale: str, note: dict, *, saved_status: str | None,
                              saved_status_source: str, estimate_source: str, photos: list[dict],
                              linked_to_case: bool, transit_guidance: dict | None = None,
                              carrier_paths: list[dict] | None = None,
                              lessons: list[dict] | None = None) -> dict:
    """Build deterministic copy/checklist from saved facts and read-only existing guide contracts."""
    if incident not in DELIVERY_INCIDENTS:
        raise ValueError("Choose delivered but not received, delay, or missing shipment.")
    if locale not in ("en", "es"):
        raise ValueError("Choose English or Spanish.")
    english = locale == "en"
    title = {
        "delivered_not_received": "Delivered status, buyer says not received" if english else "Estatus entregado, comprador dice que no recibió",
        "delay": "Shipment delay" if english else "Retraso del envío",
        "missing": "Shipment may be missing" if english else "Posible extravío del envío",
    }[incident]
    if incident == "delivered_not_received":
        checks = ([
            ("Compare the saved address", "Compare the saved ship-to details with the order and the buyer's specific report; record any mismatch."),
            ("Ask where it may have been left", "Ask the buyer to check household members, reception, parcel lockers, and the stated delivery location."),
            ("Preserve the carrier record", "If the purchased service exposes it, retain the scan time, location, recipient/signature, or delivery photo. Availability depends on carrier and service; a saved status alone is not proof."),
            ("Review shipment originals", "Compare the seller-entered label and parcel photos. Their contents have not been authenticated."),
            ("Keep the same case package", "Review existing source records in the selected package. Use its existing attachment controls only after human review."),
        ] if english else [
            ("Compara el domicilio guardado", "Compara el destino guardado con el pedido y el reporte concreto del comprador; registra cualquier diferencia."),
            ("Pregunta dónde pudo quedar", "Pide al comprador revisar con quienes viven allí, recepción, casilleros y el lugar indicado de entrega."),
            ("Conserva el registro del transportista", "Si el servicio contratado lo ofrece, conserva hora y lugar del escaneo, destinatario/firma o foto. Depende del transportista y servicio; el estatus guardado no es prueba por sí solo."),
            ("Revisa los originales del envío", "Compara las fotos de etiqueta y paquete ingresadas por el vendedor. Su contenido no está autenticado."),
            ("Conserva el mismo paquete del caso", "Revisa las fuentes existentes en el paquete seleccionado. Usa sus controles actuales de adjuntos solo después de revisión humana."),
        ])
        opening = ("Hello. I am reviewing your report that the parcel has not arrived. The saved shipment details are below; I have not independently confirmed delivery. Could you check with anyone at the address and tell me what you find? I will review the available shipment record and update you." if english else
                   "Hola. Estoy revisando tu reporte de que el paquete no ha llegado. Abajo están los datos guardados del envío; no he confirmado la entrega de forma independiente. ¿Puedes revisar con quienes están en el domicilio y contarme qué encuentras? Revisaré el registro disponible del envío y te actualizaré.")
    elif incident == "delay":
        checks = ([
            ("Compare the recorded estimate", "Compare the seller-recorded estimate with the date today and the saved shipment status; the estimate is not a carrier commitment."),
            ("Keep movement and exception records", "If available for the purchased service, retain carrier acceptance, movement timestamps, and any delay or exception notice."),
            ("Check the buyer's current need", "Ask whether the buyer can still use the item if it arrives later; do not promise a new date without a sourced update."),
            ("Review shipment originals", "Keep the label and parcel originals with their source hashes; image contents remain unauthenticated."),
            ("Continue in the same package", "Review the existing case documents and use the selected package's normal attachment controls if appropriate."),
        ] if english else [
            ("Compara la fecha estimada registrada", "Compara la fecha estimada ingresada por el vendedor con la fecha actual y el estatus guardado; no es compromiso del transportista."),
            ("Conserva movimientos y excepciones", "Si están disponibles para el servicio contratado, conserva aceptación del transportista, fechas de movimiento y avisos de retraso o excepción."),
            ("Confirma la necesidad actual del comprador", "Pregunta si todavía le sirve el artículo si llega después; no prometas otra fecha sin una actualización con fuente."),
            ("Revisa los originales del envío", "Conserva los originales de etiqueta y paquete con sus hashes de origen; las imágenes no están autenticadas."),
            ("Continúa en el mismo paquete", "Revisa los documentos actuales del caso y usa los controles normales de adjuntos del paquete seleccionado si corresponde."),
        ])
        opening = ("Hello. I see that the shipment may be taking longer than the estimate saved for this order. That estimate is seller-recorded and is not a confirmed carrier date. I am reviewing the available shipment details; is the item still useful if it arrives later?" if english else
                   "Hola. Veo que el envío podría tardar más que la fecha estimada guardada para este pedido. Esa fecha la registró el vendedor y no es una fecha confirmada por el transportista. Estoy revisando los datos disponibles del envío; ¿todavía te sirve si llega después?")
    else:
        checks = ([
            ("Find the last recorded movement", "Review the saved status and last available scan; do not label the shipment lost based only on elapsed time."),
            ("Check trace options for the purchased service", "A sender may request a carrier search only where the service contract provides it. Proofie does not contact the carrier."),
            ("Preserve shipment and sale sources", "Keep the seller-entered label/parcel originals, order record, tracking reference, and any carrier exception record."),
            ("Check claim or coverage terms", "Claim windows, insurance, and required documents depend on the carrier, policyholder, and purchased service; confirm the applicable contract."),
            ("Continue in the same package", "Review the current case sources and use the selected package's normal attachment controls after human review."),
        ] if english else [
            ("Ubica el último movimiento registrado", "Revisa el estatus guardado y el último escaneo disponible; no clasifiques el envío como extraviado solo por el tiempo transcurrido."),
            ("Revisa opciones de rastreo del servicio contratado", "El remitente puede solicitar una búsqueda solo si el contrato del servicio lo permite. Proofie no contacta al transportista."),
            ("Conserva fuentes del envío y la venta", "Guarda los originales de etiqueta/paquete ingresados por el vendedor, el pedido, la guía y cualquier excepción del transportista."),
            ("Revisa condiciones de reclamo o cobertura", "Plazos, seguro y documentos dependen del transportista, titular de póliza y servicio contratado; confirma el contrato aplicable."),
            ("Continúa en el mismo paquete", "Revisa las fuentes actuales del caso y usa los controles normales de adjuntos del paquete seleccionado tras revisión humana."),
        ])
        opening = ("Hello. I do not have a confirmed delivery for this shipment. I am reviewing the last details saved by the seller; they do not establish that the parcel is lost. I will share a sourced update when one is available." if english else
                   "Hola. No tengo una entrega confirmada para este envío. Estoy revisando los últimos datos guardados por el vendedor; estos no establecen que el paquete esté extraviado. Compartiré una actualización con fuente cuando esté disponible.")

    carrier = str(note.get("carrier_name") or note.get("carrier_name_other") or note.get("carrier_code") or "")
    saved_details = []
    if carrier:
        saved_details.append(("Carrier", carrier) if english else ("Transportista", carrier))
    if note.get("carrier_code"):
        saved_details.append(("Saved Tracking code", str(note["carrier_code"])) if english else ("Código Tracking guardado", str(note["carrier_code"])))
    if note.get("carrier_name_other"):
        saved_details.append(("Seller-supplied carrier name", str(note["carrier_name_other"])) if english else ("Nombre ingresado por el vendedor", str(note["carrier_name_other"])))
    if note.get("tracking_number"):
        saved_details.append(("Seller-supplied tracking reference", str(note["tracking_number"])) if english else ("Guía ingresada por el vendedor", str(note["tracking_number"])))
    # Reuse the already-validated seller note facts without treating a Tracking field as evidence classification.
    script = opening
    if carrier:
        script += (f" Carrier recorded: {carrier}." if english else f" Transportista registrado: {carrier}.")
    if note.get("tracking_number"):
        script += (f" Seller-supplied tracking reference: {note['tracking_number']}." if english else
                   f" Guía ingresada por el vendedor: {note['tracking_number']}.")
    if note.get("estimated_date"):
        script += (f" Seller-recorded estimate: {note['estimated_date']}." if english else f" Fecha estimada registrada por el vendedor: {note['estimated_date']}.")
    if note.get("carrier_warning"):
        script += (f" Carrier note: {note['carrier_warning']}" if english else f" Aviso del transportista: {note['carrier_warning']}")
    if saved_status:
        script += (f" Saved shipment status: {saved_status}; it is not independently verified." if english else
                   f" Estatus de envío guardado: {saved_status}; no está verificado de forma independiente.")
    safety = ("Copy-only seller draft. No message was sent; carrier response, acceptance, delivery, receipt, and proof-of-delivery availability are unverified." if english else
              "Borrador del vendedor solo para copiar. No se envió ningún mensaje; respuesta del transportista, aceptación, entrega, recepción y disponibilidad de comprobante no están verificadas.")
    return {
        "incident": incident, "title": title, "locale": locale, "checklist": [{"title": t, "text": x} for t, x in checks],
        "script": script, "safety": safety, "saved_status": saved_status,
        "saved_status_source": saved_status_source, "estimate_source": estimate_source,
        "saved_details": saved_details, "carrier_warning": note.get("carrier_warning"),
        "carrier_source_label": note.get("carrier_source_label"), "carrier_source_url": note.get("carrier_source_url"),
        "tracking_url": note.get("tracking_url"), "photos": photos or [],
        "linked_to_case": bool(linked_to_case), "transit_guidance": transit_guidance,
        "carrier_paths": carrier_paths or [], "lessons": lessons or [], "tracking_details_source": note.get("source_note"),
    }


def _date_text(value: str | None, locale: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("The recorded estimate is invalid.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("The recorded estimate is invalid.") from exc
    if locale == "es":
        return f"{parsed.day} {_MONTHS_ES[parsed.month - 1]} {parsed.year}"
    return parsed.strftime("%b %d, %Y").replace(" 0", " ")


def _directory_entry(carrier: str | None) -> dict | None:
    if not isinstance(carrier, str) or not carrier.strip():
        return None
    key = re.sub(r"[^a-z0-9]", "", carrier.casefold())
    for row in carriers():
        options = (row.get("id", ""), row.get("name", ""))
        if key == re.sub(r"[^a-z0-9]", "", row.get("name", "").casefold()) or key in {
                re.sub(r"[^a-z0-9]", "", str(item).casefold()) for item in options}:
            return row
    return None


def _carrier_destination(carrier: str | None) -> tuple[str | None, str | None]:
    row = _directory_entry(carrier)
    return (row.get("name"), row.get("tracking_url")) if row else (None, None)


def _tracking_carrier_context(code: str | None, carrier_name_other: str | None) -> dict:
    """Resolve the saved Tracking contract to a bundled directory entry without projecting evidence enums."""
    name = " ".join(carrier_name_other.split()) if isinstance(carrier_name_other, str) else ""
    if code == "OTHER":
        match = map_carrier(name or None)
        warning = ("The saved OTHER tracking code has no carrier_name_other; carrier identity is unknown and no provider response is verified."
                   if not name else match.note if match.aggregator else
                   "This seller-named carrier maps locally to OTHER; no provider acceptance, tracking status, or delivery is verified."
                   if match.risk else None)
        directory = _directory_entry(name)
        tracking_url = directory.get("tracking_url") if directory else None
        return {"name": name or "OTHER", "tracking_url": tracking_url, "warning": warning,
                "source_url": directory.get("source_url") if directory else None,
                "source_label": "OTHER and carrier_name_other come from the saved seller shipment request; no carrier response was checked."}

    directory = _directory_entry(name or code)
    if not directory and isinstance(code, str) and code.strip():
        for row in carriers():
            mapped = map_carrier(row.get("name"))
            if mapped.code == code:
                directory = row
                break
    display_name = directory.get("name") if directory else None
    label = name or display_name or (code if isinstance(code, str) else "")
    warning = None
    if code and code not in KNOWN_CODES and code != "USPS":
        warning = "The saved Tracking carrier code is outside the known catalogue; no carrier status or delivery is verified."
    return {"name": label, "tracking_url": directory.get("tracking_url") if directory else None,
            "source_url": directory.get("source_url") if directory else None, "warning": warning,
            "source_label": "Tracking API carrier code and display label come from the saved local shipment request; the directory is a static public reference, not live tracking."}


def build_note(*, description: str, tracking_number: str | None, carrier: str | None,
               expected_date: str | None, locale: str = "en", carrier_code: str | None = None,
               carrier_name_other: str | None = None, carrier_warning: str | None = None,
               carrier_source_label: str | None = None, carrier_source_url: str | None = None,
               tracking_url_override: str | None = None, carrier_display_name: str | None = None) -> dict:
    if locale not in ("en", "es"):
        raise ValueError("Choose English or Spanish.")
    description = " ".join(description.split()) if isinstance(description, str) else ""
    if description and (len(description) > 160 or _PRIVATE.search(description)):
        raise ValueError("Use a short item description without buyer or PayPal identifiers.")
    tracking = " ".join(tracking_number.split()) if isinstance(tracking_number, str) else ""
    if tracking and (len(tracking) > 64 or re.fullmatch(r"[A-Za-z0-9-]+", tracking) is None):
        raise ValueError("The recorded tracking number is not safe to include.")
    carrier_name, directory_url = _carrier_destination(carrier)
    if isinstance(carrier_display_name, str) and carrier_display_name.strip():
        carrier_name = " ".join(carrier_display_name.split())
    tracking_url = tracking_url_override if tracking_url_override is not None else directory_url
    eta = _date_text(expected_date, locale)
    if locale == "es":
        lines = ["Hola. Te comparto los datos de tu envío."]
        if description: lines.append(f"Artículo: {description}.")
        if tracking: lines.append(f"Guía: {tracking}.")
        if carrier_name: lines.append(f"Paquetería: {carrier_name}.")
        if tracking_url: lines.append(f"Rastreo público: {tracking_url}")
        lines.append(f"Fecha estimada registrada: {eta}." if eta else "No hay una fecha estimada registrada.")
        lines.extend(("¿Alguien más recibe paquetes en tu domicilio?", "¿Ya revisaste con un vecino o en recepción?"))
        text = " ".join(lines)
    else:
        lines = ["Hello. Here are the shipment details."]
        if description: lines.append(f"Item: {description}.")
        if tracking: lines.append(f"Tracking: {tracking}.")
        if carrier_name: lines.append(f"Carrier: {carrier_name}.")
        if tracking_url: lines.append(f"Public tracking: {tracking_url}")
        lines.append(f"Recorded estimated delivery: {eta}." if eta else "No estimated delivery date is recorded.")
        lines.extend(("Does anyone else receive packages at your address?", "Have you checked with a neighbor or reception?"))
        text = " ".join(lines)
    return {"locale": locale, "text": text, "tracking_url": tracking_url, "estimated_date": eta,
            "carrier_code": carrier_code, "carrier_name": carrier_name,
            "carrier_name_other": carrier_name_other, "carrier_warning": carrier_warning,
            "carrier_source_label": carrier_source_label, "carrier_source_url": carrier_source_url,
            "source_note": "Shipment details and estimate are seller-recorded local facts; the directory is not live tracking and no ETA is invented."}


def record_response(db_path, dispute_id: str, state: str) -> int:
    if validate_dispute_id(dispute_id) or state not in ("delivered", "not_received"):
        raise ValueError("Choose a valid case and response.")
    return repo.log_action(db_path, "vendor", "buyer_note_response", dispute_id,
                           {"response": state, "source": "seller_recorded"})


def responses(db_path, dispute_id: str) -> list[dict]:
    if validate_dispute_id(dispute_id):
        return []
    path = Path(db_path).absolute()
    if not path.is_file() or path.is_symlink():
        return []
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute("SELECT ts, detail FROM audit_log WHERE dispute_id=? AND action='buyer_note_response' ORDER BY id",
                            (dispute_id,)).fetchall()
        out = []
        for stamp, raw in rows:
            try: detail = json.loads(raw)
            except (TypeError, ValueError): continue
            if isinstance(detail, dict) and detail.get("response") in ("delivered", "not_received"):
                out.append({"at": stamp, "state": detail["response"]})
        return out
    except sqlite3.DatabaseError:
        return []
    finally:
        conn.close()


def sale_note(capture_id: str, shipment_record: dict, locale: str = "en",
              sale_terms: dict | None = None) -> dict:
    """Build a copy-only note from a saved local fulfilment row for this validated sale."""
    if validate_capture_id(capture_id) or not isinstance(shipment_record, dict) or shipment_record.get("capture_id") != capture_id:
        raise ValueError("A saved shipment record for this sale is required.")
    try:
        request = json.loads(shipment_record.get("request_json") or "{}")
    except (TypeError, ValueError) as exc:
        raise ValueError("The saved shipment record is unreadable.") from exc
    trackers = request.get("trackers") if isinstance(request, dict) else None
    tracker = next((item for item in trackers or [] if isinstance(item, dict) and item.get("transaction_id") == capture_id), None)
    if tracker is None:
        raise ValueError("The saved shipment record does not contain this sale reference.")
    tracking = tracker.get("tracking_number")
    carrier = tracker.get("carrier")
    carrier_name_other = tracker.get("carrier_name_other")
    if tracking is not None and not isinstance(tracking, str):
        raise ValueError("The saved tracking reference is invalid.")
    if carrier is not None and not isinstance(carrier, str):
        raise ValueError("The saved carrier is invalid.")
    if carrier_name_other is not None and not isinstance(carrier_name_other, str):
        raise ValueError("The saved carrier name is invalid.")
    context = _tracking_carrier_context(carrier, carrier_name_other)
    terms = sale_terms if isinstance(sale_terms, dict) else {}
    description = terms.get("description") if isinstance(terms.get("description"), str) else ""
    expected_date = terms.get("expected_date") if isinstance(terms.get("expected_date"), str) else None
    note = build_note(description=description, tracking_number=tracking, carrier=context["name"],
                      expected_date=expected_date, locale=locale, carrier_code=carrier,
                      carrier_name_other=carrier_name_other, carrier_warning=context["warning"],
                      carrier_source_label=context["source_label"], carrier_source_url=context["source_url"],
                      tracking_url_override=context["tracking_url"], carrier_display_name=context["name"])
    note["source_note"] = ("Copy-only seller note from the saved local shipment request and seller-recorded sale terms. "
                           "This is not live tracking. The public directory is static; no carrier status, delivery, or receipt is verified.")
    return note


def record_sale_response(db_path, capture_id: str, state: str) -> int:
    if validate_capture_id(capture_id) or state not in ("delivered", "not_received"):
        raise ValueError("Choose a valid sale and response.")
    return repo.append_sale_action(db_path, capture_id, "buyer_note_response",
                                   {"response": state, "source": "seller_recorded"}, actor="vendor")


def sale_responses(db_path, capture_id: str) -> list[dict]:
    if validate_capture_id(capture_id):
        return []
    return [{"id": row["id"], "at": row["ts"], "state": row["detail"]["response"],
             "actor": row["actor"]} for row in repo.list_sale_actions(db_path, capture_id)]
