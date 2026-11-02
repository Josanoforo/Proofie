"""Opt-in diskless hosted factory; local demo assets, never PayPal state, are reseeded."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import uuid

from copilot import config
from copilot.db.schema import ensure_schema

NOTICE = "Each restart, redeploy, or idle sleep starts this demo with a fresh session. The first request after idle may take about one minute."


def seed_local_assets(root: Path, *, session: Path | None = None) -> Path:
    """Create a fresh session without deleting or reusing any previous evidence or approvals."""
    root.mkdir(parents=True, exist_ok=True)
    if session is None:
        session = Path(tempfile.mkdtemp(prefix="proofie-session-", dir=root))
    else:
        if session.parent != root or not session.name.startswith("proofie-session-"):
            raise ValueError("Session must be directly inside the explicit ephemeral root")
        session.mkdir()  # A collision fails instead of mixing sessions.
    documents = session / "demo" / "documents"
    documents.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "demo" / "documents"
    for name in ("01_coherent_delivery.pdf", "02_wrong_order_tracking_and_postal_code.pdf",
                 "03_in_transit.pdf", "04_injected_instruction.pdf", "05_illegible_scan.png"):
        shutil.copyfile(source / name, documents / name)
    ensure_schema(session / "copilot.db")
    (session / "evidence").mkdir()
    return session


def create_app():
    """Reuse all original hosted access/write guards before creating any session data."""
    if not config.hosted_demo():
        raise RuntimeError("Ephemeral demo requires hosted mode")
    root = os.environ.get("COPILOT_EPHEMERAL_ROOT", "").strip()
    if not root or not Path(root).is_absolute():
        raise RuntimeError("COPILOT_EPHEMERAL_ROOT must be an explicit absolute path")
    root = Path(root).resolve()
    # Reserve only a name before imports: SDK module constants must see this run's session directory,
    # while the original startup guards still run before any asset/store writes.
    session = root / ("proofie-session-" + uuid.uuid4().hex)
    previous = {key: os.environ.get(key) for key in ("COPILOT_DATA_DIR", "PROOFIE_EVIDENCE_DIR")}
    os.environ["COPILOT_DATA_DIR"] = str(session)
    os.environ["PROOFIE_EVIDENCE_DIR"] = str(session / "evidence")
    try:
        from copilot.app import create_app as original_create_app
        app = original_create_app()
        seed_local_assets(root, session=session)
    except Exception:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        raise
    # Variant-local rendering: no shared Jinja/global-config mutation that changes a disk app.
    disk_sentence = ("The demo keeps its database and a log of its PayPal calls on the host's disk; "
                     "a host restart may erase them.")
    app.state.hosted_demo_sentence = config.HOSTED_DEMO_SENTENCE.replace(disk_sentence, NOTICE, 1)
    return app
