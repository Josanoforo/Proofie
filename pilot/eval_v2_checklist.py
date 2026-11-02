"""Offline checklist arm for Eval v2 cases; does not score or call a model/provider."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot.core.checklist import (ACKNOWLEDGEABLE_DATE_CHECKS, DEFAULT_RULES_PATH, ExtractedDoc, evaluate, load_rules)
from copilot.core.context import TransactionContext
from copilot.core.document_ingest import extract_text
from copilot.core.field_extraction import extract_fields
from tools.make_demo_documents import _pdf

CASES_DIR = Path(__file__).resolve().parent / "eval" / "v2" / "cases"


class SyntheticPdfTextNotRepresentable(ValueError):
    """A synthetic case cannot be ingested faithfully by the shared WinAnsi-only PDF generator."""


def run_case(case: dict, today: date | None = None) -> dict:
    """Convert representable synthetic documents to PDF, ingest, then run the existing checklist once."""
    docs: list[ExtractedDoc] = []
    ingestion: list[dict] = []
    names = case.get("document_names", {})
    # Preflight the whole case before generating any PDF so a later unsupported document cannot leave
    # earlier documents appearing to have been ingested as a complete case.
    pdf_lines_by_doc = {}
    for doc_id, text in case["documents"].items():
        try:
            pdf_lines_by_doc[doc_id] = [line.encode("cp1252").decode("cp1252") for line in str(text).splitlines()]
        except UnicodeEncodeError:
            raise SyntheticPdfTextNotRepresentable(
                f"synthetic document {doc_id} contains text not representable by WinAnsi/CP1252; refusing lossy PDF ingestion"
            ) from None
    for doc_id, text in case["documents"].items():
        pdf_lines = pdf_lines_by_doc[doc_id]
        payload = _pdf(pdf_lines)
        extracted, ok = extract_text("application/pdf", payload)
        ingestion.append({"document_id": doc_id, "name": names.get(doc_id), "extraction_ok": ok})
        fields = extract_fields(extracted) if ok else {}
        docs.append(ExtractedDoc(doc_id=doc_id, fields=fields, raw_text_available=ok, raw_text=extracted if ok else None))
    checks = evaluate(TransactionContext(), docs, load_rules(DEFAULT_RULES_PATH), today=today)
    blocking = [c.id for c in checks if c.state == "conflict" and c.id not in ACKNOWLEDGEABLE_DATE_CHECKS]
    return {"case_id": case["case_id"], "arm": "checklist", "ingestion": ingestion,
            "checks": [{"id": c.id, "state": c.state, "reason": c.seller_message, "action": c.action,
                        "basis": c.basis, "risk": c.risk} for c in checks],
            "conflicts_blocking_approval": blocking}

def run_product_case(case: dict, today: date | None = None) -> dict:
    """Run the product-text arm directly, without PDF generation or document_ingest."""
    docs: list[ExtractedDoc] = []
    names = case.get("document_names", {})
    for doc_id, text in case["documents"].items():
        raw_text = str(text)
        docs.append(ExtractedDoc(doc_id=doc_id, fields=extract_fields(raw_text),
                                 raw_text_available=True, raw_text=raw_text))
    checks = evaluate(TransactionContext(), docs, load_rules(DEFAULT_RULES_PATH), today=today)
    blocking = [c.id for c in checks if c.state == "conflict" and c.id not in ACKNOWLEDGEABLE_DATE_CHECKS]
    return {"case_id": case["case_id"], "arm": "checklist_product_text", "input_mode": "text",
            "documents": [{"document_id": d.doc_id, "name": names.get(d.doc_id), "raw_text_available": d.raw_text_available}
                          for d in docs],
            "checks": [{"id": c.id, "state": c.state, "reason": c.seller_message, "action": c.action,
                        "basis": c.basis, "risk": c.risk,
                        "a checklist conflict that blocks approval": c.id in blocking} for c in checks],
            "conflicts_blocking_approval": blocking}


def _frozen_index(directory: Path) -> dict[str, str]:
    frozen = json.loads((directory / "FROZEN.json").read_text(encoding="utf-8"))
    if (not isinstance(frozen, dict)
            or not all(isinstance(case_id, str) and isinstance(digest, str)
                       and re.fullmatch(r"[0-9a-f]{64}", digest) for case_id, digest in frozen.items())):
        raise ValueError("FROZEN.json must map held-out case ids to SHA-256 hashes")
    return frozen


def case_paths_for_set(case_set: str, directory: Path = CASES_DIR,
                       frozen: dict[str, str] | None = None) -> list[Path]:
    """Select case paths from FROZEN metadata before opening any case JSON."""
    if case_set not in ("dev", "frozen"):
        raise ValueError("case set must be dev or frozen")
    directory = Path(directory)
    frozen = _frozen_index(directory) if frozen is None else frozen
    available = {path.stem: path for path in directory.glob("*.json") if path.name != "FROZEN.json"}
    if set(frozen) - set(available):
        raise ValueError("frozen case paths are missing")
    selected = sorted(frozen) if case_set == "frozen" else sorted(set(available) - set(frozen))
    return [available[case_id] for case_id in selected]


def dev_case_paths(directory: Path = CASES_DIR) -> list[Path]:
    return case_paths_for_set("dev", directory)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", choices=("dev", "frozen"), default="dev",
                        help="select dev or the frozen partition from FROZEN.json")
    parser.add_argument("--arm", choices=("ingest", "product"), default="ingest",
                        help="ingest runs PDF->extract_text; product runs ExtractedDoc->extract_fields directly")
    parser.add_argument("--today", type=date.fromisoformat, default=None)
    args = parser.parse_args(argv)
    try:
        frozen = _frozen_index(CASES_DIR)
        paths = case_paths_for_set(args.set, CASES_DIR, frozen)
        cases = []
        for path in paths:
            raw = path.read_bytes()
            if args.set == "frozen" and hashlib.sha256(raw).hexdigest() != frozen[path.stem]:
                raise ValueError("a frozen case does not match its FROZEN.json hash")
            case = json.loads(raw.decode("utf-8"))
            if case.get("case_id") != path.stem:
                raise ValueError("an indexed case path has a mismatched case id")
            if args.set == "dev" and case.get("set") != "dev":
                raise ValueError("an indexed development path is not a valid dev case")
            cases.append(case)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    for case in cases:
        try:
            result = (run_product_case if args.arm == "product" else run_case)(case, today=args.today)
        except SyntheticPdfTextNotRepresentable as exc:
            result = {"case_id": case["case_id"], "arm": "checklist", "status": "rejected",
                      "rejection": str(exc), "ingestion": []}
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
