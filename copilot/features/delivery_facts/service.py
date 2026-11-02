"""Coherent local fulfilment snapshot; no client, migrations, model or send."""
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path
from copilot.core.carriers import carriers_equal, display_name
from copilot.features.draft.facts import FactSheet, DIGIT_RUN
from copilot.features.draft.check import validate_draft

class FactsError(ValueError):
    pass
class FactsMissing(FactsError):
    pass

CAPTURE = re.compile(r"[A-Za-z0-9]{17}")
SHA = re.compile(r"[a-f0-9]{64}")
PRIVATE = re.compile(r"[^\s<>]+@[^\s<>]+|\bBearer\b|\b(?:password|client[_-]?secret|access[_-]?token|api[_-]?key)\s*[:=]|\b[A-Za-z]:[\\/](?!/)|/(?:home|Users|mnt|etc)/|\.env\b", re.I)
STATES = {"SHIPPED", "DELIVERED", "ON_HOLD", "CANCELLED", "LOCAL_PICKUP", "PARTIALLY_DELIVERED", "IN_TRANSIT"}
SAVED_STATES = {"registered", "registered_unconfirmed", "unknown", "failed", "saved_without_registration"}
# The same rule as copilot/features/fulfilment/store.effective_status (kept here, not imported: this reader keeps its own
# frozen feature dependencies; a test pins the two patterns equal). A legacy `failed` row whose limitation says the outcome is
# unknown (a transport failure, a 5xx or a 429 saved before the `unknown` status existed) is `unknown`: PayPal may have it.
_LEGACY_UNKNOWN = re.compile(r"what happened is unknown|\(error (?:5\d\d|429)\)", re.I)
MAX_JSON = 100000

def _effective_status(row):
    limitation=row["limitation_text"]
    if row["status"]=="failed" and isinstance(limitation,str) and _LEGACY_UNKNOWN.search(limitation):
        return "unknown"
    return row["status"]

def _fail():
    raise FactsError("The local delivery facts cannot be read safely.")

def _path(value):
    path = Path(value).absolute()
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            _fail()
    return path

def _sidecars(path, wal=False):
    # SQLite owns normal WAL locking/coordination; never follow linked storage.
    for suffix in ("-journal", "-wal", "-shm"):
        side = _path(Path(str(path) + suffix))
        if not side.exists():
            continue
        info = side.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 67108864:
            _fail()
        if suffix == "-journal" or not wal:
            _fail()
        if suffix == "-shm":
            if info.st_size < 32768 or info.st_size % 32768:
                _fail()
        elif info.st_size:
            # Validate only the container signature, not WAL frames or locks.
            with side.open("rb") as stream:
                signature = stream.read(4)
            if info.st_size < 32 or signature not in (b"\x37\x7f\x06\x82", b"\x37\x7f\x06\x83"):
                _fail()


def _header(path):
    with path.open("rb") as stream:
        header=stream.read(100)
    if (len(header)!=100 or header[:16]!=b"SQLite format 3\x00"
            or header[18:20] not in (bytes((1,1)), bytes((2,2)))):
        _fail()
    return header[18]


def _safe_tree(obj, depth=0):
    if depth > 12:
        _fail()
    if isinstance(obj, str):
        if len(obj) > 10000 or PRIVATE.search(obj) or any(ord(c) < 32 for c in obj):
            _fail()
    elif isinstance(obj, dict):
        if len(obj) > 200:
            _fail()
        for k,v in obj.items():
            _safe_tree(k,depth+1); _safe_tree(v,depth+1)
    elif isinstance(obj, list):
        if len(obj) > 200:
            _fail()
        for item in obj:
            _safe_tree(item,depth+1)
    elif obj is not None and type(obj) not in (bool, int, float):
        _fail()

def _pairs(pairs):
    out={}
    for k,v in pairs:
        if k in out:
            _fail()
        out[k]=v
    return out

def _json(text):
    if not isinstance(text,str) or len(text.encode("utf-8")) > MAX_JSON:
        _fail()
    def invalid(_):
        _fail()
    obj=json.loads(text,object_pairs_hook=_pairs,parse_constant=invalid)
    # Stored policy provenance is allowed only for the two shipped rule sets.
    # It is still hashed and scanned with the rest of the record, never rendered
    # as an arbitrary link or exempted from the private-text checks.
    if isinstance(obj,dict) and isinstance(obj.get("rules"),dict):
        source=obj["rules"].get("source_url")
        if source is not None:
            from copilot.core.checklist import load_rules
            if source not in {load_rules(country=c).source_url for c in ("MX","US")}:
                _fail()
    _safe_tree(obj)
    return obj

def _sha(obj):
    return hashlib.sha256(json.dumps(obj,sort_keys=True,ensure_ascii=False).encode("utf-8")).hexdigest()

def _token(value, pattern, maximum=64):
    if value in (None, ""):
        return None
    if not isinstance(value,str) or len(value)>maximum or re.fullmatch(pattern,value) is None:
        _fail()
    return value

def _status(value):
    if value in (None, ""):
        return None
    if not isinstance(value,str) or value not in STATES:
        _fail()
    return value

def _date(value, saved=False):
    if value in (None, ""):
        return None
    if not isinstance(value,str) or len(value)>32:
        _fail()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}",value):
        if saved:
            _fail()
        datetime.strptime(value,"%Y-%m-%d")
        return value
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:?\d{2})",value) is None:
        _fail()
    parsed=datetime.fromisoformat(value.replace("Z","+00:00"))
    if parsed.utcoffset() is None:
        _fail()
    return value

def _updated(value):
    if value in (None, ""):
        return None
    if isinstance(value,str) and re.fullmatch(r"[0-9]{1,15}",value):
        value=int(value)
    if type(value) is int and 0 <= value <= 253402300799999:
        seconds,ms=divmod(value,1000)
        return datetime.fromtimestamp(seconds,timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")+f".{ms:03d}Z"
    return _date(value)

def _tracker(obj, capture=None):
    if not isinstance(obj,dict):
        _fail()
    transaction=obj.get("transaction_id")
    if transaction is not None and (not isinstance(transaction,str) or transaction!=capture):
        _fail()
    return {"tracking":_token(obj.get("tracking_number"),r"[A-Za-z0-9-]+"),
            "carrier":_token(obj.get("carrier"),r"[A-Z_]+"),"status":_status(obj.get("status")),
            "shipment":_date(obj.get("shipment_date")),"updated":_updated(obj.get("last_updated_time")),
            "carrier_name":_name(obj.get("carrier_name_other"))}

def _name(value):
    """The carrier name the seller typed for OTHER (text only; the stored record was already scanned for private data)."""
    if value in (None, ""):
        return None
    if not isinstance(value,str) or len(value)>64 or not value.isprintable():
        _fail()
    return value

# Display words come from fixed tables, never from raw enum values. Dates stay ISO.
STATUS_EN={"SHIPPED":"shipped","DELIVERED":"delivered","ON_HOLD":"on hold","CANCELLED":"canceled","LOCAL_PICKUP":"local pickup",
           "PARTIALLY_DELIVERED":"partially delivered","IN_TRANSIT":"in transit"}
STATUS_ES={"SHIPPED":"enviado","DELIVERED":"entregado","ON_HOLD":"en pausa","CANCELLED":"cancelado","LOCAL_PICKUP":"recogida local",
           "PARTIALLY_DELIVERED":"entregado parcialmente","IN_TRANSIT":"en tránsito"}
FIELD_WORDS={"tracking":"Tracking number","carrier":"Carrier","status":"Status"}


def carrier_word(tracker,spanish=False):
    """The seller's word for the carrier: a catalogue code gives its display name, OTHER gives the typed name."""
    code=tracker.get("carrier")
    if code=="OTHER":
        return tracker.get("carrier_name") or ("otra paquetería" if spanish else "another carrier")
    return display_name(code) if code else None


def _clause(tracker,spanish,with_tracking,display=False):
    words=[carrier_word(tracker,spanish)]
    if with_tracking and tracker.get("tracking"):
        words.append(("guía " if spanish else "tracking ")+tracker["tracking"])
    status=tracker.get("status")
    words.append((STATUS_ES if spanish else STATUS_EN).get(status) if status else None)
    if tracker.get("shipment"):
        shipped=_display_day(tracker["shipment"],spanish) if display else tracker["shipment"]
        words.append(("fecha de envío " if spanish else "ship date ")+shipped)
    return ", ".join(w for w in words if w)


def comparison(request,readback):
    """[(field label, state, text)] for the three fields both records can carry. state: same | different | not_compared."""
    if not readback:
        return []
    rows=[]
    for key in ("tracking","carrier","status"):
        label=FIELD_WORDS[key]
        if not request.get(key) or not readback.get(key):
            rows.append((label,"not_compared",f"{label}: not compared (missing in one record)"))
            continue
        same=carriers_equal(request[key],readback[key]) if key=="carrier" else request[key]==readback[key]
        rows.append((label,"same" if same else "different",
                     f"{label}: same in both" if same else f"{label}: different - check both"))
    return rows


def _discrepancies(request, readback):
    if not readback:
        return []
    return [key for key in ("tracking", "carrier", "status", "shipment")
            if request[key] and readback[key]
            and (not carriers_equal(request[key], readback[key]) if key == "carrier"
                 else request[key] != readback[key])]


MONTHS_EN=("Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec")
MONTHS_ES=("ene","feb","mar","abr","may","jun","jul","ago","sep","oct","nov","dic")
_ISO_DAY=re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:T.*)?")

def _display_day(value,spanish):
    """The ship date as the seller reads it ('7 Oct 2026', Spanish '7 oct 2026'). Only a ship-date value is ever passed here, never
    free text, so a tracking number or carrier name that looks like a date is never rewritten. An unreadable value stays as saved."""
    match=_ISO_DAY.fullmatch(value) if isinstance(value,str) else None
    if match is None:
        return value
    year,month,day=(int(g) for g in match.groups())
    if not 1<=month<=12:
        return value
    return f"{day} {(MONTHS_ES if spanish else MONTHS_EN)[month-1]} {year}"

def _messages_text(request,readback,address,display=False):
    # Both languages use exactly the same literal facts, in the same order; no model paraphrase.
    en=["Shipment details."]
    es=["Detalles del envío."]
    entered_en,entered_es=_clause(request,False,True,display),_clause(request,True,True,display)
    if entered_en:
        en.append("You entered: "+entered_en+".");es.append("Indicaste: "+entered_es+".")
    if readback:
        answer_en,answer_es=_clause(readback,False,False,display),_clause(readback,True,False,display)
        if answer_en:
            en.append("PayPal's saved answer: "+answer_en+".");es.append("Respuesta guardada de PayPal: "+answer_es+".")
    if _discrepancies(request, readback):
        en.append("Records disagree; review both sources.");es.append("Los registros difieren; revisar ambas fuentes.")
    if any(t and t["status"]=="PARTIALLY_DELIVERED" for t in (request,readback)):
        en.append("Partial delivery reported.");es.append("Entrega parcial reportada.")
    if address=="mismatch":
        en.append("Address comparison disagrees.");es.append("La comparación de domicilio difiere.")
    if not readback or not all(readback[k] for k in ("tracking","carrier","status")):
        en.append("Tracker facts are incomplete.");es.append("Los hechos del tracker están incompletos.")
    en.append("A tracking number in PayPal does not by itself prove the parcel was shipped or delivered.")
    es.append("Un número de guía en PayPal no prueba por sí solo que el paquete se envió o se entregó.")
    return " ".join(en)," ".join(es)

def _messages(request,readback,address):
    en,es=_messages_text(request,readback,address)
    literals=tuple(v for t in (request,readback) if t for v in t.values() if v)
    # The validator expects the request tracking in both drafts; both records retain it.
    sheet=FactSheet(None,None,None,None,request["tracking"],None,None,None,None,None,
                    literals,tuple(dict.fromkeys(n for lit in literals for n in DIGIT_RUN.findall(lit))))
    validation=validate_draft(sheet,en,es)
    if not validation.ok:
        _fail()
    # The texts are validated with the facts' ISO dates (the validator accepts nothing else); the text the seller reads and pastes
    # is then built again with the display form of the ship dates only: '7 Oct 2026' / '7 oct 2026'.
    return _messages_text(request,readback,address,display=True)

def read(db_path,capture_id):
    if not isinstance(capture_id,str) or CAPTURE.fullmatch(capture_id) is None:
        raise FactsMissing("Capture not found.")
    conn=None
    try:
        path=_path(db_path)
        if not path.is_file():
            raise FactsMissing("No local delivery facts.")
        if path.stat().st_size>67108864:
            _fail()
        _sidecars(path, wal=_header(path)==2)
        conn=sqlite3.connect(path.as_uri()+"?mode=ro&cache=private",uri=True,timeout=0.25)
        conn.row_factory=sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        _sidecars(path, wal=_header(path)==2)
        # SQL length check avoids bringing unbounded JSON into memory; never reads another capture.
        size=conn.execute("SELECT length(CAST(request_json AS BLOB)), length(CAST(tracker_readback_json AS BLOB)), "
                          "length(CAST(checks_json AS BLOB)) FROM fulfilment_files WHERE capture_id=? ORDER BY id DESC LIMIT 1",
                          (capture_id,)).fetchone()
        if size is None:
            raise FactsMissing("No local delivery facts.")
        if any(type(x) is not int or not 0<x<=MAX_JSON for x in size):
            _fail()
        # limitation_text is read only to apply the legacy rule above; a table saved without that column has none.
        limitation=("limitation_text" if any(c[1]=="limitation_text" for c in conn.execute("PRAGMA table_info(fulfilment_files)"))
                    else "NULL")
        row=conn.execute("SELECT id,capture_id,created_at,request_json,request_sha256,approval_sha256,"
                         "tracker_readback_json,checks_json,file_sha256,status,"+limitation+" AS limitation_text FROM fulfilment_files "
                         "WHERE capture_id=? ORDER BY id DESC LIMIT 1",(capture_id,)).fetchone()
        _sidecars(path, wal=_header(path)==2)
        if type(row["id"]) is not int or not 0<row["id"]<=9223372036854775807 or row["capture_id"]!=capture_id:
            _fail()
        saved=_date(row["created_at"],saved=True)
        if saved is None or row["status"] not in SAVED_STATES:
            _fail()
        for key in ("request_sha256","approval_sha256","file_sha256"):
            if not isinstance(row[key],str) or SHA.fullmatch(row[key]) is None:
                _fail()
        request=_json(row["request_json"]);readback=_json(row["tracker_readback_json"]);checks=_json(row["checks_json"])
        if not isinstance(request,dict) or not isinstance(request.get("trackers"),list) or len(request["trackers"])!=1:
            _fail()
        if not isinstance(checks,dict) or readback is not None and not isinstance(readback,dict):
            _fail()
        if _sha(request)!=row["request_sha256"] or row["approval_sha256"]!=row["request_sha256"]:
            _fail()
        if _sha({"request":request,"tracker_readback":readback,"checks":checks})!=row["file_sha256"]:
            _fail()
        requested=_tracker(request["trackers"][0],capture_id)
        if request["trackers"][0].get("transaction_id")!=capture_id:
            _fail()
        observed=_tracker(readback,capture_id) if readback is not None else None
        address=checks.get("address_comparison")
        if address is not None and not isinstance(address,dict):
            _fail()
        address=(address or {}).get("result")
        if address is not None and address not in ("match","mismatch","cannot_compare","not_declared","no_paypal_address"):
            _fail()
        en,es=_messages(requested,observed,address)
        discrepancies = _discrepancies(requested, observed)
        return {"capture_id":capture_id,"saved_at":saved,"record_status":_effective_status(row),"request":requested,
                "readback":observed,"address":address,"discrepancies":discrepancies,
                "partial":any(t and t["status"]=="PARTIALLY_DELIVERED" for t in (requested,observed)),
                "comparison":comparison(requested,observed),
                "sha256":row["file_sha256"],"english":en,"spanish":es}
    except FactsError:
        raise
    except (sqlite3.Error,OSError,ValueError,TypeError,KeyError,OverflowError,RecursionError):
        raise FactsError("The local delivery facts cannot be read safely.") from None
    finally:
        if conn is not None:
            conn.close()
