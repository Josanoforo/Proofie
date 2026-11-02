from datetime import date

import pytest

import eval_v2_checklist as checklist_cli
from eval_v2_checklist import run_case


def test_product_text_arm_preserves_unicode_and_bypasses_pdf_ingest(monkeypatch):
    text = "Carrier: USPS\nTracking number: DEMO-TRACK-UNICODE\nDelivery note: 配送済み"
    original = text.encode("utf-8")
    case = {"case_id": "synthetic-product-text", "document_names": {"a": "a.txt", "b": "b.txt"},
            "documents": {"a": text, "b": text}}
    pdf_calls, ingest_calls, field_inputs, eval_calls = [], [], [], []
    monkeypatch.setattr(checklist_cli, "_pdf", lambda *args: pdf_calls.append(args))
    monkeypatch.setattr(checklist_cli, "extract_text", lambda *args: ingest_calls.append(args))
    real_extract_fields = checklist_cli.extract_fields
    monkeypatch.setattr(checklist_cli, "extract_fields",
                        lambda raw: (field_inputs.append(raw), real_extract_fields(raw))[1])
    real_evaluate = checklist_cli.evaluate
    def evaluate_spy(context, docs, rules, *, today=None):
        eval_calls.append((context, docs, rules, today))
        return real_evaluate(context, docs, rules, today=today)
    monkeypatch.setattr(checklist_cli, "evaluate", evaluate_spy)

    result = checklist_cli.run_product_case(case, today=date(2026, 10, 21))

    assert pdf_calls == [] and ingest_calls == []
    assert [value.encode("utf-8") for value in field_inputs] == [original, original]
    assert len(eval_calls) == 1 and len(eval_calls[0][1]) == 2
    assert [doc.raw_text.encode("utf-8") for doc in eval_calls[0][1]] == [original, original]
    assert result["arm"] == "checklist_product_text" and result["input_mode"] == "text"
    assert all({"id", "state", "reason", "action", "basis", "risk",
                "a checklist conflict that blocks approval"} <= row.keys() for row in result["checks"])
    blocking = set(result["conflicts_blocking_approval"])
    assert all(row["a checklist conflict that blocks approval"] == (row["id"] in blocking)
               for row in result["checks"])


def test_run_case_rejects_unrepresentable_unicode_before_pdf_or_ingest(monkeypatch):
    text = "Entrega: 配送済み"
    original_bytes = text.encode("utf-8")
    case = {"case_id": "synthetic-unicode", "document_names": {"a": "a.pdf", "b": "b.pdf"},
            "documents": {"a": "Carrier: USPS\nTracking number: DEMO-TRACK-001", "b": text}}
    pdf_calls, ingest_calls = [], []
    monkeypatch.setattr(checklist_cli, "_pdf", lambda lines: pdf_calls.append(lines))
    monkeypatch.setattr(checklist_cli, "extract_text", lambda *args: ingest_calls.append(args))

    with pytest.raises(checklist_cli.SyntheticPdfTextNotRepresentable, match="not representable by WinAnsi/CP1252"):
        checklist_cli.run_case(case, today=date(2026, 10, 21))

    assert pdf_calls == []
    assert ingest_calls == []
    assert case["documents"]["b"].encode("utf-8") == original_bytes


def test_checklist_arm_ingests_synthetic_pdfs_and_reports_existing_gate():
    case = {"case_id": "synthetic-x1", "document_names": {"a": "a.pdf", "b": "b.pdf"},
            "documents": {
                "a": "Carrier: USPS\nTracking number: DEMO-TRACK-001.\nStatus: Delivered\nDelivery date: 2026-10-20",
                "b": "Carrier: USPS\nTracking number: DEMO-TRACK-001\nStatus: Delivered\nDelivery date: 2026-10-20"}}
    result = run_case(case, today=date(2026, 10, 21))
    assert all(row["extraction_ok"] for row in result["ingestion"])
    assert any(row["id"] == "tracking_number" for row in result["checks"])
    assert "tracking_number" not in result["conflicts_blocking_approval"]
    case["documents"]["b"] = case["documents"]["b"].replace("DEMO-TRACK-001", "DEMO-TRACK-002")
    changed = run_case(case, today=date(2026, 10, 21))
    assert "tracking_number" in changed["conflicts_blocking_approval"]


def test_dev_cli_excludes_frozen_paths_before_opening_case_json(monkeypatch, capsys, tmp_path):
    import hashlib
    import json
    from pathlib import Path
    import eval_v2_checklist as cli

    case_dir = tmp_path / "cases"
    case_dir.mkdir()
    dev_payload = json.dumps({"case_id": "dev_synthetic", "set": "dev", "documents": {}}).encode("utf-8")
    heldout_payload = b"held-out payload sentinel; this test must not open it"
    (case_dir / "dev_synthetic.json").write_bytes(dev_payload)
    (case_dir / "heldout_synthetic.json").write_bytes(heldout_payload)
    (case_dir / "FROZEN.json").write_text(
        json.dumps({"heldout_synthetic": hashlib.sha256(heldout_payload).hexdigest()}), encoding="utf-8")
    monkeypatch.setattr(cli, "CASES_DIR", case_dir)

    original_read_bytes = Path.read_bytes
    opened = []
    def guarded_read_bytes(path, *args, **kwargs):
        if path.parent == case_dir and path.name != "FROZEN.json":
            if path.stem == "heldout_synthetic":
                raise AssertionError("dev CLI attempted to open a frozen held-out payload")
            opened.append(path.stem)
        return original_read_bytes(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    selected = []
    monkeypatch.setattr(cli, "run_case", lambda case, today=None:
                        (selected.append(case["case_id"]) or {"case_id": case["case_id"], "arm": "synthetic"}))
    assert cli.main(["--set", "dev", "--today", "2026-10-21"]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert selected == ["dev_synthetic"]
    assert opened == ["dev_synthetic"]
    assert rows == [{"case_id": "dev_synthetic", "arm": "synthetic"}]

def test_frozen_cli_accepts_partition_from_frozen_index(monkeypatch, capsys, tmp_path):
    import hashlib
    import json

    case_dir = tmp_path / "cases"
    case_dir.mkdir()
    frozen_bytes = json.dumps({"case_id": "frozen_synthetic", "documents": {}}).encode("utf-8")
    (case_dir / "frozen_synthetic.json").write_bytes(frozen_bytes)
    (case_dir / "dev_synthetic.json").write_text("not JSON; this partition must not be opened", encoding="utf-8")
    (case_dir / "FROZEN.json").write_text(
        json.dumps({"frozen_synthetic": hashlib.sha256(frozen_bytes).hexdigest()}), encoding="utf-8")
    monkeypatch.setattr(checklist_cli, "CASES_DIR", case_dir)
    selected = []
    monkeypatch.setattr(checklist_cli, "run_case",
                        lambda case, today=None: (selected.append(case["case_id"]) or
                                                  {"case_id": case["case_id"], "arm": "synthetic"}))

    assert checklist_cli.main(["--set", "frozen", "--today", "2026-10-21"]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert selected == ["frozen_synthetic"]
    assert rows == [{"case_id": "frozen_synthetic", "arm": "synthetic"}]

