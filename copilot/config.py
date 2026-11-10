"""Shared app configuration. data/ is git-ignored (docs/ARCHITECTURE.md section 5); created on first use, never
assumed to pre-exist (a clean `git clone` has no data/ directory at all).

db_path()/uploads_dir() are FUNCTIONS, deliberately - not module-level constants imported by name. Two
real bugs (auto_prep.py's background task, and separately routes_send.py's attachment
reader) came from exactly the same mistake: `from copilot.config import DB_PATH` binds the value once, at
import time, in the IMPORTING module's own namespace - a test monkeypatching copilot.config.DB_PATH later
never reaches that frozen copy. Calling db_path()/uploads_dir() instead reads the current override every
time, so ONE test fixture (conftest.py's autouse _isolated_copilot_data) redirects every module in
copilot/ at once, with no per-module patching to remember or forget."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def load_env_file() -> None:
    """Load project-local settings before router registration; shell values win."""
    load_dotenv(ROOT / ".env", override=False)
    _refresh_rules_country()


def _refresh_rules_country() -> None:
    """RULES_COUNTRY below is read when this module is imported, which is before the .env file is loaded: a country set
    only in .env was ignored. After the load, a value now in the environment replaces it (and the rule set cached for the
    old value is dropped). Nothing changes when the variable is not set."""
    import sys
    global RULES_COUNTRY
    value = os.environ.get("COPILOT_RULES_COUNTRY")
    if value is None or value == RULES_COUNTRY:
        return
    RULES_COUNTRY = value
    module = sys.modules.get("copilot.core.checklist_input")
    if module is not None:
        module.RULES_COUNTRY = value
        module._RULES_CACHE.clear()

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DB_PATH = DATA_DIR / "copilot.db"
# Raw uploaded bytes, content-addressed by sha256 (the documents table stores only the extracted TEXT and
# that hash - the original bytes were discarded until a real send needed them again to actually attach a file to
# a real send). A re-upload of identical content just overwrites the same path with identical bytes.
UPLOADS_DIR = DATA_DIR / "uploads"

# Seller account country for copilot/core/checklist.py's rule set (see .env.example). MX is the default -
# this project's only sandbox seller account so far is Mexican; an unknown value is NOT validated here
# (copilot/core/checklist.py.load_rules raises UnknownCountryError, shown in plain language where used).
RULES_COUNTRY = os.environ.get("COPILOT_RULES_COUNTRY", "MX")

# Dispute ids the automatic background preparation must never touch (these are
# OUR demo data, not product logic - a judge running their own sandbox has none, and a hardcoded id list
# reads as a hack in the public repo). Comma-separated, empty by default (nothing excluded) - see
# .env.example. Auto-prep never approves or sends regardless of this list; it only ever builds a draft, so
# excluding a dispute here is about not spending a background preparation on it, nothing more.
AUTOPREP_EXCLUDE_IDS = frozenset(x.strip() for x in os.environ.get("COPILOT_AUTOPREP_EXCLUDE", "").split(",")
                                 if x.strip())

_db_path_override: Path | None = None
_uploads_dir_override: Path | None = None

# COPILOT_DATA_DIR moves every local write of the app out of the code folder (a host keeps its disk elsewhere): the
# database and the uploads below, and, through pilot/client.py's evidence_dir(), the PayPal call log. Read on every
# call; unset or blank keeps the default folder above. A relative value is resolved against the current directory.
DATA_DIR_ENV = "COPILOT_DATA_DIR"


def _data_dir_from_env() -> Path | None:
    value = os.environ.get(DATA_DIR_ENV, "").strip()
    return Path(value).expanduser().resolve() if value else None


def data_dir() -> Path:
    """The folder for the database and the uploads RIGHT NOW: COPILOT_DATA_DIR when set, otherwise DATA_DIR."""
    found = _data_dir_from_env()
    return found if found is not None else DATA_DIR


def db_path() -> Path:
    """The SQLite path to use RIGHT NOW - call this, never import DB_PATH by name (see module docstring)."""
    if _db_path_override is not None:
        return _db_path_override
    found = _data_dir_from_env()
    return found / DB_PATH.name if found is not None else DB_PATH


def uploads_dir() -> Path:
    """The uploads directory to use RIGHT NOW - call this, never import UPLOADS_DIR by name."""
    if _uploads_dir_override is not None:
        return _uploads_dir_override
    found = _data_dir_from_env()
    return found / UPLOADS_DIR.name if found is not None else UPLOADS_DIR


def set_db_path_override(path: Path | str | None) -> None:
    """Test-only (conftest.py). None restores the real DB_PATH."""
    global _db_path_override
    _db_path_override = Path(path) if path is not None else None


def set_uploads_dir_override(path: Path | str | None) -> None:
    """Test-only (conftest.py). None restores the real UPLOADS_DIR."""
    global _uploads_dir_override
    _uploads_dir_override = Path(path) if path is not None else None


def ensure_data_dir() -> None:
    data_dir().mkdir(parents=True, exist_ok=True)


# pilot/step4_evidence_ai.py hard-codes OLLAMA/MODEL as its own module constants -
# copilot/core/evidence_model.py (the only caller) overrides them on that module right before each call,
# reading these two functions fresh every time, never a value frozen at import (same reasoning as
# db_path()/uploads_dir() above). See .env.example for both variables.
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3.5:9b"


def ollama_url() -> str:
    return os.environ.get("COPILOT_OLLAMA_URL", DEFAULT_OLLAMA_URL)


def model_name() -> str:
    return os.environ.get("COPILOT_MODEL", DEFAULT_MODEL)


# Optional feature flags: every optional feature is off unless COPILOT_FEATURE_<NAME> is the literal
# "true" (case-insensitive, stripped). <NAME> is the feature name upper-cased with "-" turned into "_". Read on every
# call, like the functions above; callers use config.feature_enabled("<name>").
def feature_enabled(name: str) -> bool:
    key = "COPILOT_FEATURE_" + name.strip().upper().replace("-", "_")
    return os.environ.get(key, "").strip().lower() == "true"


# The hosted demo is the second route; the app itself is built to run on the seller's machine.
# COPILOT_HOSTED_DEMO is on only for the literal "true" (same reading as feature_enabled); read on every call.
def hosted_demo() -> bool:
    return os.environ.get("COPILOT_HOSTED_DEMO", "").strip().lower() == "true"


def access_code() -> str:
    """Read the hosted access code afresh, so rotation invalidates existing cookies."""
    return os.environ.get("COPILOT_ACCESS_CODE", "")


def server_host() -> str:
    """The module entry point stays loopback locally; hosted binding is explicit."""
    if not hosted_demo():
        return "127.0.0.1"
    return os.environ.get("COPILOT_HOST", "0.0.0.0").strip() or "0.0.0.0"


def server_port() -> int:
    """Provider PORT is read only in hosted mode; invalid ports fail before serving."""
    if not hosted_demo():
        return 8000
    value = int(os.environ.get("PORT", "8000"))
    if not 1 <= value <= 65535:
        raise ValueError("PORT must be between 1 and 65535")
    return value


HOSTED_UPLOAD_BUDGET_BYTES = 200 * 1024 * 1024


# The hosted-demo wording. Every sentence is under 25 words; the seven claims keep their order. Backed by code: the
# model claims by refuse_model_if_hosted() below (the three model call sites); the single-account claim by one
# account and one database; the redaction claim by pilot/client.py (key list `_SENSITIVE_KEYS` and the e-mail regex);
# the documents claim by routes_documents.py (bytes and extracted text kept); the token claim by pilot/client.py (_token is
# held on the per-request client; _log never receives it).
HOSTED_DEMO_SENTENCE = (
    "Proofie is built to run on your own computer. "
    "With its default settings, it connects only to PayPal's sandbox. "
    "An AI model, if you turn one on, also runs on your computer. "
    "This copy is the hosted demo of the same app. "
    "Here the AI assistant is switched off: you type the shipment facts yourself. "
    "The demo keeps its database and a log of its PayPal calls on the host's disk; a host restart may erase them. "
    "It uses one shared PayPal sandbox account, so everyone who opens it sees the same disputes and uploads. "
    "The app keeps a PayPal access token only in memory, until it expires; it never writes one to disk or to its log. "
    "It keeps a log of PayPal's answers. "
    "A fixed list of field names removes names, e-mail addresses, phone numbers, addresses, payer and account ids and bank numbers from that log. "
    "Tracking numbers, transaction ids and amounts stay in it. "
    "So does free text such as messages and item descriptions, except e-mail addresses typed in it. "
    "Files you upload, and the text read from them, are stored on this host. "
    "The app sends data only to PayPal's sandbox; the company that hosts this demo also carries the traffic and runs the server.")

# The one message of the hosted guard: every model call site raises it before any network attempt, and the handlers that
# already treat an unavailable model as "continue by hand" take the manual path.
HOSTED_MODEL_OFF_MESSAGE = "The AI assistant is switched off in the hosted demo"


def refuse_model_if_hosted() -> None:
    """Call first at every place that starts a model call (propose_package, propose_image_claims, the draft's
    default_call). Flag off: does nothing."""
    if hosted_demo():
        raise RuntimeError(HOSTED_MODEL_OFF_MESSAGE)


# The hosted demo's replay of a recorded local-model run (copilot/features/replay): a static page from a committed
# synthetic fixture. It takes effect only with the flag AND the hosted demo; the model call sites above keep refusing.
REPLAY_DEFAULT_CASE = "PP-R-DEMO-0000001"


def replay_enabled() -> bool:
    return hosted_demo() and feature_enabled("replay")


def replay_case() -> str | None:
    """The case the access page and the inbox link to, or None when the replay is off (read on every render)."""
    return REPLAY_DEFAULT_CASE if replay_enabled() else None
