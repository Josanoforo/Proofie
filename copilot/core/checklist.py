"""Deterministic evidence checklist.

`evaluate(context, extracted_docs, rules)` -> list[Check]. No LLM, no network, no new dependencies. Each check
ends in one of five states, declares its basis, and tells the seller what to do next.

Document field names the checklist reads (ExtractedDoc.fields[name] = {"value": ..., "confidence":
"read" | "illegible" | "absent"}):
  tracking_number, carrier, delivery_status, delivery_date (YYYY-MM-DD), ship_date, postal_code, city, state, country,
  street, receiver_name, signature ("online" | "paper_only" | "aggregator_portal" | "none"),
  delivery_type ("home" | "branch_pickup"), capture_date. Any other field (e.g. invoice_date) is ignored: an
  invoice dated after the delivery is not a conflict by itself.

Interpretation rules, each covered by a test:
  * tracking not registered in PayPal != tracking contradicting PayPal's;
  * absent != illegible != unverifiable != conflict;
  * a seller-declared address (seller_statement) never replaces PayPal's silently;
  * a document without a full street is present_unverifiable, not a failure: postal code, or city + state, or
    city + country, is enough;
  * an unknown carrier is OTHER + a risk flag, not an invalid proof.

Optional extra input (defaulted): ExtractedDoc.raw_text (str | None) lets the checklist spot
text that looks like instructions to an AI. Such text is never obeyed; it only raises a review flag.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import copilot.core.disputes  # noqa: F401  (puts pilot/ on sys.path)
from step3_dispute import MX_TZ  # noqa: E402

from copilot.core.carriers import map_carrier
from copilot.core.context import SRC_DISPUTE, SRC_SEARCH, SRC_SELLER, TransactionContext

STATES = ("verified", "missing", "present_unverifiable", "conflict", "not_applicable")
BASES = ("case_request", "published_policy", "app_quality_rule")
ACTIONS = ("add_document", "fix_value", "review_difference", "check_status", "none")

RULES_DIR = Path(__file__).resolve().parents[1] / "rules"
# Seller account country -> rule set file. See copilot/rules/README.md before adding one.
RULE_SETS = {"MX": RULES_DIR / "mx_seller_protection_2025-01-13.json",
             "US": RULES_DIR / "us_seller_protection_2026-01-26.json"}
DEFAULT_COUNTRY = "MX"
DEFAULT_RULES_PATH = RULE_SETS[DEFAULT_COUNTRY]

# These date differences may be acknowledged; every other checklist conflict blocks approval.
ACKNOWLEDGEABLE_DATE_CHECKS = frozenset({"delivery_date", "dates_coherent"})


@dataclass(frozen=True)
class Check:
    id: str
    label_for_seller: str
    state: str
    basis: str
    basis_ref: str
    sources: list
    action: str
    risk: bool
    seller_message: str

    def __post_init__(self):
        if self.state not in STATES or self.basis not in BASES or self.action not in ACTIONS:
            raise ValueError(f"invalid check vocabulary: {self.id} {self.state} {self.basis} {self.action}")


@dataclass
class ExtractedDoc:
    doc_id: str
    fields: dict = field(default_factory=dict)   # name -> {"value": ..., "confidence": ...}
    raw_text_available: bool = False
    raw_text: str | None = None                  # optional, see module docstring


@dataclass(frozen=True)
class RuleSet:
    rule_set_id: str
    country: str
    effective_date: str
    source_url: str
    source_title: str
    signature_limits: dict          # currency -> {"limit_minor": int, "minor_units": int}
    capture_expiry_warning_days: int
    checks: dict
    raw: dict
    signature_required: bool = True   # False: the published terms have no signature requirement


class UnknownCountryError(ValueError):
    """No rule set exists for the seller account country. Never fall back silently in a UI path."""


def supported_countries() -> list:
    return sorted(RULE_SETS)


def load_rules(path: str | Path | None = None, country: str | None = None) -> RuleSet:
    """Load a rule set: an explicit `path`, else the one for `country` (ISO-2, case-insensitive), else MX.

    An unknown country raises UnknownCountryError; only a missing country (None) means the MX default."""
    if path is None:
        code = DEFAULT_COUNTRY if country is None else str(country).strip().upper()
        if code not in RULE_SETS:
            raise UnknownCountryError(f"No Seller Protection rule set for country {country!r}. Supported: "
                                      f"{', '.join(supported_countries())}.")
        path = RULE_SETS[code]
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    limits = {cur: {"limit_minor": int(v["limit_minor"]), "minor_units": int(v["minor_units"])}
              for cur, v in data["signature_limits"].items()}
    return RuleSet(data["rule_set_id"], data["country"], data["effective_date"], data["source_url"],
                   data["source_title"], limits, int(data["capture_expiry_warning_days"]), data["checks"], data,
                   bool(data.get("signature_required", True)))


# ---------------------------------------------------------------- helpers
def _plain(s) -> str:
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


_STATE_ALIASES = {"cdmx": "cdmx", "ciudad de mexico": "cdmx", "distrito federal": "cdmx", "df": "cdmx",
                  "mexico city": "cdmx", "edomex": "edomex", "estado de mexico": "edomex",
                  "jal": "jalisco", "jalisco": "jalisco", "nl": "nuevo leon", "n l": "nuevo leon",
                  "nuevo leon": "nuevo leon"}


def _state(s) -> str:
    p = _plain(s)
    return _STATE_ALIASES.get(p, p)


def _city(s) -> str:
    p = _plain(s)
    return "cdmx" if p in ("cdmx", "ciudad de mexico", "mexico city", "distrito federal", "df") else p


def _postal(s) -> str:
    return re.sub(r"\D", "", str(s))


_STREET_STOP = {"calle", "c", "av", "avenida", "num", "no", "numero", "col", "colonia", "int", "ext", "mx"}


def _street_tokens(s) -> set:
    return {t for t in _plain(s).split() if t not in _STREET_STOP}


def _to_date(v) -> date | None:
    if v is None:
        return None
    s = str(v).strip()
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", s)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return None


@dataclass
class _Obs:
    doc_id: str
    value: object
    confidence: str

    @property
    def source(self) -> str:
        return f"document:{self.doc_id}"


def _obs(docs, name) -> tuple:
    """(read, illegible) observations of one field across documents. Absent is simply not listed."""
    read, ill = [], []
    for d in docs:
        f = (d.fields or {}).get(name)
        if not f:
            continue
        conf, val = f.get("confidence"), f.get("value")
        if conf == "read" and val not in (None, ""):
            read.append(_Obs(d.doc_id, val, conf))
        elif conf == "illegible":
            ill.append(_Obs(d.doc_id, None, conf))
    return read, ill


def _srcs(*groups) -> list:
    out = []
    for g in groups:
        for o in g:
            s = o if isinstance(o, str) else o.source
            if s not in out:
                out.append(s)
    return out


_DELIVERED_NEG = re.compile(r"no entregad|not delivered|undelivered|fallid|failed|devuelt|returned")
_DELIVERED = re.compile(r"entregad|delivered")
_UNAVAILABLE = re.compile(r"no disponible|not available|expir|sin informacion|no encontrad|not found|"
                          r"unavailable|caduc")
_TRANSIT = re.compile(r"transito|in transit|en camino|shipped|enviado|picked|recolect|label|guia generada|"
                      r"on hold|en ruta|out for delivery")


def _status_category(v) -> str:
    p = _plain(v)
    if _UNAVAILABLE.search(p):
        return "unavailable"
    if _DELIVERED_NEG.search(p):
        return "not_delivered"
    if _DELIVERED.search(p):
        return "delivered"
    if _TRANSIT.search(p):
        return "in_transit"
    return "unknown"


_INJECTION = re.compile(
    r"ignore (all |any |the )?(previous|prior|above)|ignora (las |todas |tus )?(instrucciones|indicaciones)|"
    r"olvida (las |tus )?instrucciones|disregard (the |all )?(previous|above)|system prompt|"
    r"you are (now )?(an? )?(ai|assistant|language model)|as an ai|"
    r"(instructions?|instrucciones) (to|for|para) (the )?(ai|model|assistant|modelo|asistente)|"
    r"mark (everything|all|this)[^.\n]{0,40}(verified|delivered|signed)|<\|im_start\|>|"
    r"marca (todo|esto)[^.\n]{0,40}(verificado|entregado|firmado)", re.IGNORECASE)


class _Builder:
    def __init__(self, rules: RuleSet):
        self.rules, self.out = rules, []

    def add(self, cid: str, state: str, message: str, sources=(), action="none", risk=False,
            label: str | None = None, rule_key: str | None = None):
        rule = self.rules.checks.get(rule_key or cid)
        if rule is None:        # check switched off in this rule set
            return
        basis = rule["basis"]
        ref = rule["citation"]
        if basis == "published_policy":
            ref = f"{ref}; effective {self.rules.effective_date}; {self.rules.source_url}"
        self.out.append(Check(cid, label or rule["label"], state, basis, ref, list(sources), action, risk,
                              message))


def _missing_action(rules: RuleSet, cid: str) -> str:
    kind = rules.checks.get(cid, {}).get("kind")
    return "none" if kind in ("informational",) else "add_document"


# ---------------------------------------------------------------- evaluate
def evaluate(context: TransactionContext, extracted_docs: list, rules: RuleSet,
             today: date | None = None) -> list:
    docs = list(extracted_docs or [])
    today = today or datetime.now(timezone.utc).astimezone(MX_TZ).date()
    b = _Builder(rules)
    _tracking_number(b, context, docs)
    _carrier(b, context, docs)
    _delivery_status(b, context, docs)
    _delivery_date(b, context, docs)
    _dates_coherent(b, docs)
    _invoice_amount(b, context, docs)
    _destination(b, context, docs)
    _full_address(b, context, docs)
    _signature(b, context, docs, rules)
    _receiver(b, docs)
    _capture_date(b, docs, rules, today)
    _branch(b, docs)
    _integrity(b, docs)
    _case_requests(b, context)
    return b.out


def _norm_track(v) -> str:
    value = str(v).strip()
    value = re.sub(r"\.(?=\s*$)", "", value)  # terminal punctuation only; preserve interior characters
    return re.sub(r"[\s-]+", "", value).upper()


def _tracking_number(b, ctx, docs):
    read, ill = _obs(docs, "tracking_number")
    pp = ctx.get("tracking_number")
    pp_src = ctx.source_of("tracking_number")
    vals = {_norm_track(o.value) for o in read}
    if len(vals) > 1:
        b.add("tracking_number", "conflict", "Your documents show different tracking numbers. Check which one "
              "belongs to this order.", _srcs(read), "review_difference")
    elif read and pp:
        if _norm_track(pp) in vals:
            b.add("tracking_number", "verified", f"Tracking number {read[0].value} matches the one registered "
                  f"in PayPal.", _srcs(read, [pp_src]))
        else:
            b.add("tracking_number", "conflict", f"Your document shows tracking number {read[0].value} but "
                  f"PayPal has {pp} for this transaction. The document may belong to another order.",
                  _srcs(read, [pp_src]), "review_difference")
    elif read:
        # Not registered in PayPal is NOT the same as contradicting PayPal.
        b.add("tracking_number", "present_unverifiable", f"Tracking number {read[0].value} comes from your "
              f"document; PayPal has no tracking registered for this transaction, so it could not be "
              f"compared.", _srcs(read))
    elif pp:
        b.add("tracking_number", "verified", f"Tracking number {pp} is registered in PayPal.", [pp_src])
    elif ill:
        b.add("tracking_number", "present_unverifiable", "A document appears to show a tracking number but it "
              "is not legible. Type it in or upload a clearer copy.", _srcs(ill), "fix_value")
    else:
        b.add("tracking_number", "missing", "No tracking number found. Add the carrier's proof or type the "
              "number.", [], "add_document")


def _carrier(b, ctx, docs):
    read, ill = _obs(docs, "carrier")
    pp = ctx.get("carrier")
    pp_src = ctx.source_of("carrier")
    cands = [(o.value, o.source) for o in read] + ([(pp, pp_src)] if pp else [])
    if not cands:
        if ill:
            b.add("carrier", "present_unverifiable", "The carrier name is not legible. Type it in or upload "
                  "a clearer copy.", _srcs(ill), "fix_value")
        else:
            b.add("carrier", "missing", "No carrier found. Add the carrier's proof or choose the carrier.",
                  [], "add_document")
        return
    matches = [(map_carrier(n), s) for n, s in cands]
    known = {m.code for m, _ in matches if m.known}
    srcs = [s for _, s in matches]
    if len(known) > 1:
        b.add("carrier", "conflict", "The documents and PayPal name different carriers.", srcs,
              "review_difference")
        return
    m = matches[0][0]
    if m.known:
        b.add("carrier", "verified", f"Carrier recognized: {m.code}.", srcs)
    elif m.code == "OTHER":
        extra = f" Note: {m.note}." if m.aggregator else ""
        b.add("carrier", "verified", f"Carrier \"{m.carrier_name_other}\" is not in PayPal's carrier list, so "
              f"it is registered as OTHER. This is a risk flag, not a rejection.{extra}", srcs, "none",
              risk=True)
    else:
        b.add("carrier", "missing", "No carrier found.", srcs, "add_document")


def _delivery_status(b, ctx, docs):
    read, ill = _obs(docs, "delivery_status")
    cats = [(_status_category(o.value), o) for o in read]
    pp = ctx.get("tracking_status")
    if pp:
        cats.append((_status_category(pp), None))
    srcs = _srcs(read) + ([ctx.source_of("tracking_status")] if pp else [])
    kinds = {c for c, _ in cats}
    if "delivered" in kinds and kinds & {"in_transit", "not_delivered", "unavailable"}:
        b.add("delivery_status", "conflict", "One source says delivered and another says it is not. Review "
              "which proof is newer.", srcs, "review_difference")
    elif "delivered" in kinds:
        b.add("delivery_status", "verified", "The shipment shows as delivered.", srcs)
    elif "unavailable" in kinds:
        b.add("delivery_status", "missing", "The carrier no longer shows this tracking (proof not available "
              "online). Ask the carrier for a delivery letter and add it.", srcs, "add_document")
    elif kinds & {"in_transit", "not_delivered"}:
        b.add("delivery_status", "missing", "The shipment is not delivered yet (in transit or not completed). "
              "You can still reply honestly with the shipping proof; do not state it was delivered.", srcs,
              "check_status")
    elif "unknown" in kinds:
        b.add("delivery_status", "present_unverifiable", "A delivery status is shown but Proofie could not tell "
              "whether it means delivered. Check it.", srcs, "fix_value")
    elif ill:
        b.add("delivery_status", "present_unverifiable", "The delivery status is not legible. Type it in or "
              "upload a clearer copy.", _srcs(ill), "fix_value")
    else:
        b.add("delivery_status", "missing", "No delivery status found. Add the carrier's tracking capture or "
              "delivery letter.", [], "add_document")


def _payment_date(ctx) -> date | None:
    v = ctx.get("payment_date")
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(MX_TZ).date()


def _delivery_date(b, ctx, docs):
    read, ill = _obs(docs, "delivery_date")
    parsed = [(o, _to_date(o.value)) for o in read]
    good = [(o, d) for o, d in parsed if d]
    if len({d for _, d in good}) > 1:
        b.add("delivery_date", "conflict", "Your documents show different delivery dates.", _srcs(read),
              "review_difference")
        return
    if read and not good:
        b.add("delivery_date", "present_unverifiable", "A delivery date is shown but could not be understood. "
              "Type it in.", _srcs(read), "fix_value")
        return
    if good:
        o, d = good[0]
        pay = _payment_date(ctx)
        if pay is None:
            b.add("delivery_date", "present_unverifiable", f"Delivery date {d.isoformat()}; PayPal gave no "
                  f"payment date, so it could not be compared.", _srcs(read), "none")
        elif d < pay:
            b.add("delivery_date", "conflict", f"Delivery date {d.isoformat()} is before the payment date "
                  f"{pay.isoformat()}. The document may belong to another order (Proofie's own consistency check, "
                  f"not a PayPal requirement).",
                  _srcs(read, [ctx.source_of("payment_date")]), "review_difference")
        else:
            b.add("delivery_date", "verified", f"Delivered {d.isoformat()}, after the payment "
                  f"({pay.isoformat()}).", _srcs(read, [ctx.source_of("payment_date")]))
    elif ill:
        b.add("delivery_date", "present_unverifiable", "The delivery date is not legible. Type it in or upload "
              "a clearer copy.", _srcs(ill), "fix_value")
    else:
        b.add("delivery_date", "missing", "No delivery date found.", [], "add_document")


def _dates_coherent(b, docs):
    # Only ship date vs delivery date. An invoice dated after delivery is deliberately NOT looked at.
    ship, _ = _obs(docs, "ship_date")
    deliv, _ = _obs(docs, "delivery_date")
    s = [(o, _to_date(o.value)) for o in ship]
    d = [(o, _to_date(o.value)) for o in deliv]
    s = [x for x in s if x[1]]
    d = [x for x in d if x[1]]
    if not s or not d:
        b.add("dates_coherent", "not_applicable", "Needs both a ship date and a delivery date.",
              [], "none")
    elif min(x[1] for x in d) < max(x[1] for x in s):
        b.add("dates_coherent", "conflict", f"Delivery date {d[0][1].isoformat()} is before the ship date "
              f"{s[0][1].isoformat()}. Review the documents.", _srcs([x[0] for x in s + d]),
              "review_difference")
    else:
        b.add("dates_coherent", "verified", "Ship date is not after the delivery date.",
              _srcs([x[0] for x in s + d]))


def _invoice_amount(b, ctx, docs):
    """Proofie's own check: the invoice total in the documents equals the PayPal transaction amount (same currency,
    integer cents, never converted). A difference against the sale's own amount (PayPal's record of the transaction)
    is a conflict. When PayPal's record of the sale is not available, only the amount in dispute is known, and that may
    be part of the sale: a difference against it is a caution row that names the source and never blocks."""
    read, ill = _obs(docs, "invoice_total_cents")
    if not read and not ill:
        b.add("invoice_amount", "not_applicable", "No invoice total found in the documents.", [], "none")
        return
    if not read:
        b.add("invoice_amount", "present_unverifiable", "An invoice total is shown but could not be read. Type it "
              "in or upload a clearer copy.", _srcs(ill), "fix_value")
        return
    if len({int(o.value) for o in read}) > 1:
        b.add("invoice_amount", "conflict", "Your documents show different invoice totals.", _srcs(read),
              "review_difference")
        return
    total, cur = ctx.get("total_amount_cents"), ctx.get("total_currency")
    if total is None or not cur:
        b.add("invoice_amount", "present_unverifiable", "PayPal gave no amount for this sale, so the "
              "invoice total could not be compared.", _srcs(read), "none")
        return
    from_sale = ctx.source_of("total_amount_cents") != SRC_DISPUTE
    what = "PayPal transaction amount" if from_sale else "amount in dispute at PayPal"
    inv = int(read[0].value)
    cur_obs, _ = _obs(docs, "invoice_currency")
    inv_cur = str(cur_obs[0].value).upper() if cur_obs else None
    units = 2
    if not from_sale and ((inv_cur and inv_cur != str(cur).upper()) or inv != int(total)):
        shown = f"{_minor_str(inv, units)} {inv_cur or cur}"
        b.add("invoice_amount", "present_unverifiable", f"The invoice total {shown} differs from the {what} "
              f"{_minor_str(int(total), units)} {cur}. PayPal's record of the sale was not available, so the invoice "
              f"could not be compared with the sale itself; a dispute can cover part of a sale. Check it yourself.",
              _srcs(read, [ctx.source_of("total_amount_cents")]), "none", risk=True)
    elif inv_cur and inv_cur != str(cur).upper():
        b.add("invoice_amount", "conflict", f"The invoice is in {inv_cur} and the PayPal transaction is in {cur}. "
              f"Proofie never converts currencies; review the difference.",
              _srcs(read, cur_obs, [ctx.source_of("total_amount_cents")]), "review_difference")
    elif inv != int(total):
        b.add("invoice_amount", "conflict", f"The invoice total {_minor_str(inv, units)} {cur} differs from the "
              f"{what} {_minor_str(int(total), units)} {cur}. The document may belong to "
              f"another order (Proofie's own consistency check, not a PayPal requirement).",
              _srcs(read, [ctx.source_of("total_amount_cents")]), "review_difference")
    else:
        b.add("invoice_amount", "verified", f"The invoice total equals the {what} "
              f"({_minor_str(inv, units)} {cur}).", _srcs(read, [ctx.source_of("total_amount_cents")]))


def _doc_location(docs):
    pc, pc_i = _obs(docs, "postal_code")
    city, city_i = _obs(docs, "city")
    st, st_i = _obs(docs, "state")
    ctry, ctry_i = _obs(docs, "country")
    return pc, city, st, ctry, pc_i + city_i + st_i + ctry_i


_COUNTRY_ALIASES = {"mx": "mx", "mex": "mx", "mexico": "mx", "united mexican states": "mx",
                    "estados unidos mexicanos": "mx", "us": "us", "usa": "us", "united states": "us",
                    "estados unidos": "us", "united states of america": "us"}


def _country(s) -> str:
    p = _plain(s)
    return _COUNTRY_ALIASES.get(p, p)


def _destination(b, ctx, docs):
    pc, city, st, ctry, ill = _doc_location(docs)
    branch = any(str(o.value).casefold() == "branch_pickup" for o in _obs(docs, "delivery_type")[0])
    doc_srcs = _srcs(pc, city, st, ctry)
    if len({_postal(o.value) for o in pc}) > 1:
        b.add("destination", "conflict", "Your documents show different postal codes.", doc_srcs,
              "review_difference")
        return
    if not (pc or city or st or ctry):
        if ill:
            b.add("destination", "present_unverifiable", "The delivery place is not legible. Type it in or "
                  "upload a clearer copy.", _srcs(ill), "fix_value")
        else:
            b.add("destination", "missing", "No delivery place (postal code, or city and state, or city and country) found.", [],
                  "add_document")
        return
    addr, addr_src = ctx.get("shipping_address"), ctx.source_of("shipping_address")
    declared = False
    if not addr:
        addr, addr_src = ctx.get("seller_declared_shipping_address"), SRC_SELLER
        declared = bool(addr)
    if not addr:
        b.add("destination", "present_unverifiable", "PayPal returned no shipping address for this "
              "transaction, so the delivery place could not be compared.", doc_srcs, "fix_value")
        return
    d_pc = _postal(pc[0].value) if pc else ""
    p_pc = _postal(addr.get("postal_code", ""))
    result = None            # True match / False mismatch / None cannot compare
    detail = ""
    if d_pc and p_pc:
        result = d_pc == p_pc
        detail = f"postal code {d_pc} vs {p_pc}"
    elif city and st and addr.get("city") and addr.get("state"):
        result = (_city(city[0].value) == _city(addr["city"]) and
                  _state(st[0].value) == _state(addr["state"]))
        detail = f"city/state {city[0].value}, {st[0].value} vs {addr['city']}, {addr['state']}"
    elif city and ctry and addr.get("city") and addr.get("country_code"):
        result = (_city(city[0].value) == _city(addr["city"]) and
                  _country(ctry[0].value) == _country(addr["country_code"]))
        detail = f"city/country {city[0].value}, {ctry[0].value} vs {addr['city']}, {addr['country_code']}"
    srcs = doc_srcs + [addr_src]
    who = "the address you declared (not from PayPal)" if declared else "the transaction address in PayPal"
    if result is None:
        b.add("destination", "present_unverifiable", f"The document does not show enough to compare with {who} "
              f"(needs the postal code, or city and state, or city and country).", srcs, "fix_value")
    elif result and declared:
        b.add("destination", "present_unverifiable", f"Matches {who}, but PayPal gave no address of its own to "
              f"confirm it.", srcs)
    elif result:
        b.add("destination", "verified", f"Matches {who} ({detail}).", srcs)
    elif branch:
        b.add("destination", "present_unverifiable", f"Delivered at a branch, so the postal code is the "
              f"branch's and cannot be matched to the buyer's address ({detail}).", srcs, "review_difference",
              risk=True)
    elif d_pc and p_pc and not declared:
        # each value named by where it comes from, in the seller's words
        b.add("destination", "conflict", f"Your receipt says {d_pc}. PayPal says {p_pc}. The document may "
              f"belong to another order.", srcs, "review_difference")
    else:
        b.add("destination", "conflict", f"Does not match {who} ({detail}). The document may "
              f"belong to another order.", srcs, "review_difference")


def _full_address(b, ctx, docs):
    read, ill = _obs(docs, "street")
    if not read:
        if docs:
            b.add("full_address", "present_unverifiable", "The document does not show a street. That is not a "
                  "problem: the postal code, or city and state, is enough.", _srcs([]), "none")
        else:
            b.add("full_address", "not_applicable", "No document yet.", [], "none")
        return
    addr = ctx.get("shipping_address") or {}
    pp = " ".join(str(addr.get(k, "")) for k in ("line1", "line2")).strip()
    if not pp:
        b.add("full_address", "present_unverifiable", "PayPal returned no street for this transaction, so the "
              "street in your document could not be compared.", _srcs(read), "none")
        return
    a, p = _street_tokens(read[0].value), _street_tokens(pp)
    srcs = _srcs(read, [ctx.source_of("shipping_address")])
    if a and p and (a <= p or p <= a):
        b.add("full_address", "verified", "The street in the document matches the PayPal address.", srcs)
    else:
        b.add("full_address", "conflict", "The street in the document does not match the PayPal address.",
              srcs, "review_difference")


def _minor_str(minor: int, units: int) -> str:
    if units <= 0:
        return f"{minor:,}"
    scale = 10 ** units
    return f"{minor // scale:,}.{minor % scale:0{units}d}"


_SIG = {"online": "online", "viewable_online": "online", "signed_online": "online",
        "paper_only": "paper", "paper": "paper", "aggregator_portal": "aggregator",
        "none": "none", "no": "none"}


def _signature(b, ctx, docs, rules):
    if not rules.signature_required:
        b.add("signature", "not_applicable", f"The {rules.country} Seller Protection terms ({rules.effective_date}) "
              f"do not require a signature, whatever the amount.", [])
        return
    total, cur = ctx.get("total_amount_cents"), ctx.get("total_currency")
    total_src = ctx.source_of("total_amount_cents")
    if total is None or not cur:
        b.add("signature", "present_unverifiable", "The total amount or its currency is unknown, so Proofie cannot "
              "tell whether a signature is required.", [], "none")
        return
    lim = rules.signature_limits.get(str(cur).upper())
    if lim is None:
        b.add("signature", "present_unverifiable", f"There is no signed-receipt limit for {cur} in the rules "
              f"Proofie loaded and amounts are never converted. Check yourself whether a signature is required.",
              [total_src], "none")
        return
    limit_txt = _minor_str(lim["limit_minor"], lim["minor_units"])
    if total <= lim["limit_minor"]:       # the PDF says "supera": strictly greater
        b.add("signature", "not_applicable", f"The total is {limit_txt} {cur} or less; a signature is not "
              f"required by this rule.", [total_src])
        return
    read, ill = _obs(docs, "signature")
    kinds = {_SIG.get(_plain(o.value).replace(" ", "_"), "unknown") for o in read}
    srcs = [total_src] + _srcs(read)
    if "online" in kinds:
        b.add("signature", "verified", "Signature is viewable online on the carrier's site.", srcs)
    elif "paper" in kinds:
        b.add("signature", "present_unverifiable", "The signature exists only on paper. Above the limit the "
              "proof should be viewable on the carrier's site; ask the carrier for an online copy.", srcs,
              "add_document", risk=True)
    elif "aggregator" in kinds:
        b.add("signature", "present_unverifiable", "The signature is only on a shipping aggregator's portal, "
              "not the carrier's. Use the underlying carrier's proof.", srcs, "add_document", risk=True)
    elif "unknown" in kinds:
        b.add("signature", "present_unverifiable", "A signature is mentioned but Proofie could not tell if it can "
              "be viewed online. Check it.", srcs, "fix_value")
    elif ill:
        b.add("signature", "present_unverifiable", "The signature is not legible. Upload a clearer copy.",
              srcs + _srcs(ill), "fix_value")
    else:
        b.add("signature", "missing", f"The total is above {limit_txt} {cur} and no signed proof was found. Add the "
              "carrier's signed delivery proof.", srcs, "add_document")


def _receiver(b, docs):
    read, ill = _obs(docs, "receiver_name")
    if read:
        b.add("receiver_name", "verified", f"Received by {read[0].value} (for information only).",
              _srcs(read))
    elif ill:
        b.add("receiver_name", "present_unverifiable", "The receiver's name is not legible (for information "
              "only).", _srcs(ill), "none")
    else:
        b.add("receiver_name", "missing", "No receiver name found (for information only).", [], "none")


def _capture_date(b, docs, rules, today):
    read, ill = _obs(docs, "capture_date")
    d = _to_date(read[0].value) if read else None
    if d is None:
        b.add("capture_date", "missing" if not ill else "present_unverifiable",
              "No capture date found for the proof (for information only).", _srcs(ill), "none")
        return
    age = (today - d).days
    if age > rules.capture_expiry_warning_days:
        b.add("capture_date", "verified", f"The proof was captured {age} days ago ({d.isoformat()}). Carrier "
              f"sites may stop showing old trackings; consider capturing it again. This is Proofie's own caution, "
              f"not a PayPal requirement.", _srcs(read), "none", risk=True)
    else:
        b.add("capture_date", "verified", f"Proof captured on {d.isoformat()}.", _srcs(read))


def _branch(b, docs):
    read, _ = _obs(docs, "delivery_type")
    hit = [o for o in read if str(o.value).casefold() == "branch_pickup"]
    if hit:
        b.add("branch_pickup", "present_unverifiable", "Delivered at a branch (\"ocurre\"). Carrier proof of "
              "branch pickup may not match the buyer's address; this is a risk flag.", _srcs(hit),
              "review_difference", risk=True)
    else:
        b.add("branch_pickup", "not_applicable", "Not a branch pickup.", [])


def _integrity(b, docs):
    if not docs:
        b.add("document_integrity", "not_applicable", "No documents yet.", [])
        return
    inj = [d.doc_id for d in docs if d.raw_text and _INJECTION.search(d.raw_text)]
    ill = [d.doc_id for d in docs if any((f or {}).get("confidence") == "illegible"
                                         for f in (d.fields or {}).values())]
    if inj:
        b.add("document_integrity", "present_unverifiable", "A document contains text that reads like "
              "instructions to an AI. It was ignored and only its data fields were used. Review the "
              "document.", [f"document:{i}" for i in inj], "review_difference", risk=True)
    elif ill:
        b.add("document_integrity", "present_unverifiable", "Part of a document could not be read. Type in "
              "the missing values or upload a clearer copy.", [f"document:{i}" for i in ill], "fix_value")
    else:
        b.add("document_integrity", "verified", "Documents were readable and contained only data.",
              [f"document:{d.doc_id}" for d in docs])


def _case_requests(b, ctx):
    asked = ctx.get("evidence_requested") or []
    done = set(ctx.get("evidence_submitted") or [])
    for t in asked:
        human = copilot.core.disputes.evidence_type_words(t)
        if t in done:
            b.add(f"case_request:{t}", "verified", f"PayPal asked for \"{human}\" and you already sent it.",
                  [SRC_DISPUTE], label=f"PayPal asked for: {human}", rule_key="case_request")
        else:
            b.add(f"case_request:{t}", "missing", f"PayPal asked for \"{human}\" in this case and it has not "
                  f"been sent yet.", [SRC_DISPUTE], "add_document", label=f"PayPal asked for: {human}",
                  rule_key="case_request")


__all__ = ["Check", "ExtractedDoc", "RuleSet", "load_rules", "UnknownCountryError", "supported_countries", "evaluate", "STATES", "BASES", "ACTIONS"]
