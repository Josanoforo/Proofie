"""Record ONE run of the local model on the synthetic demo case into the replay fixture. Run by hand, never by the app.

    python -m copilot.features.replay.record [--out PATH]        one model call (needs a free GPU), then writes the fixture
    python -m copilot.features.replay.record --recompute          no model call: re-reads the fixture's own raw output
                                                                  through the validator and rewrites only the check fields

It uses the same unchanged functions as the app's AI step (pilot/step4_evidence_ai.py: messages, schema, one call with
think off and a JSON schema, then the validator and the sufficiency rule), on the synthetic dispute of
pilot/fixtures/synthetic and the first demo document. It refuses in the hosted demo and, through `preflight()`, whenever the
GPU or Ollama is busy. The fixture keeps the model's raw text as it came back; nothing is re-run to get a valid answer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from copilot import config
from copilot.core import document_ingest, evidence_model
from pilot.step3_dispute import normalize

ai = evidence_model.ai                  # pilot/step4_evidence_ai.py, imported with its own path set-up

ROOT = Path(__file__).resolve().parents[3]
CASE = "PP-R-DEMO-0000001"
DISPUTE_FIXTURE = ROOT / "pilot" / "fixtures" / "synthetic" / f"dispute_{CASE}_waiting_for_seller.json"
DOCUMENT = ROOT / "demo" / "documents" / "01_coherent_delivery.pdf"
OUT = ROOT / "copilot" / "fixtures" / "replay" / f"{CASE}.json"
SEED, TEMPERATURE = 7, 0.4


def _inputs() -> tuple[dict, dict, dict]:
    body = json.loads(DISPUTE_FIXTURE.read_text(encoding="utf-8"))["body"]
    dispute = {k: v for k, v in normalize(body, datetime.now(timezone.utc)).items() if k != "action_links"}
    text, ok = document_ingest.extract_text("application/pdf", DOCUMENT.read_bytes())
    if not ok:
        raise RuntimeError("the demo document could not be read")
    return dispute, {"doc_1": text}, {"doc_1": DOCUMENT.name}


def _checked(raw: str, dispute: dict, docs: dict) -> dict:
    """The check fields for a raw model answer: names, results and, for a failed check, the validator's detail."""
    try:
        out = json.loads(raw)
    except ValueError as exc:
        return {"checks": [{"check": "json_parse", "ok": False, "detail": [str(exc)]}], "validation_ok": False,
                "effective_next_step": None}
    checks = ai.validate(out, dispute, docs)
    entries = [{"check": c["check"], "ok": bool(c["ok"])} | ({} if c["ok"] else {"detail": [str(d) for d in c.get("detail", [])]})
               for c in checks]
    ok = bool(checks) and all(c["ok"] for c in checks)
    sources = {ai.DISPUTE_ID: json.dumps(dispute, ensure_ascii=False), **docs}
    return {"checks": entries, "validation_ok": ok,
            "effective_next_step": ai.enforce_sufficiency(dispute.get("reason"), out, sources)[0] if ok else None}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")


def record(out_path: Path = OUT) -> dict:
    config.refuse_model_if_hosted()
    dispute, docs, names = _inputs()
    ai.OLLAMA, ai.MODEL = config.ollama_url(), config.model_name()
    messages = ai.build_messages(dispute, docs, names)
    schema = ai.build_schema([ai.DISPUTE_ID, *docs])
    status = ai.preflight()                                    # raises when another model or the GPU is busy
    raw, _metrics = ai.call_model(messages, schema, SEED, TEMPERATURE)
    result = {"schema": "proofie-replay/1", "_note": "DEMO DATA - one recorded run of the local model on the synthetic demo case; "
              "nothing here is PayPal data or a real seller's.",
              "case": CASE, "model": ai.MODEL, "recorded_at": datetime.now().astimezone().replace(microsecond=0).isoformat(),
              "seed": SEED, "temperature": TEMPERATURE, "think": False,
              "prompt_sha256": hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest(),
              "documents": names, "gpu_free_before": {"other_models_loaded": status["other_models_loaded"],
                                                      "gpu_used_mib": status["gpu_used_mib"]},
              "raw_output": raw, **_checked(raw, dispute, docs)}
    _write(out_path, result)
    return result


def recompute(path: Path = OUT) -> dict:
    """No model call: the same validator over the fixture's own raw output; only the check fields change."""
    data = json.loads(path.read_text(encoding="utf-8"))
    dispute, docs, _names = _inputs()
    data.update(_checked(data["raw_output"], dispute, docs))
    data["checks_note"] = "The check fields were recomputed offline from raw_output with the validator in this repository."
    _write(path, data)
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--recompute", action="store_true")
    args = parser.parse_args(argv)
    result = recompute(args.out) if args.recompute else record(args.out)
    print(f"{'recomputed' if args.recompute else 'recorded'} {result['case']} with {result['model']} at {result['recorded_at']}; "
          f"validation_ok={result['validation_ok']}")
    return 0 if result["validation_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
