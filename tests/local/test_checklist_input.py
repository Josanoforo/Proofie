"""Offline tests for copilot/core/checklist_input.py (case: "an incomplete
or illegible document"). Confirms the real pipeline (document upload -> field_extraction.py ->
checklist_input.py -> checklist.py's unmodified evaluate()) reaches the same illegible/present_unverifiable
outcome that copilot/fixtures/m1/c06_illegible.json's hand-authored fixture already proves at the
checklist.py unit level (tests/local/test_checklist.py).
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from copilot.core.checklist import ExtractedDoc, evaluate, load_rules
from copilot.core.checklist_input import REQUIRED_FIELD_NAMES, docs_for_dispute, extracted_docs_for
from copilot.core.document_ingest import MIN_MEANINGFUL_TEXT_CHARS, extract_text
from copilot.core.field_extraction import extract_fields
from copilot.db import repo

_spec = importlib.util.spec_from_file_location("m1_loader", ROOT / "copilot" / "fixtures" / "m1" / "loader.py")
m1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m1)
RULES = load_rules(country="MX")

DID = "PP-D-TEST-0001"


def _run(case_name, docs):
    case = m1.load_case(case_name)
    return {c.id: c for c in evaluate(m1.build_context(case), docs, RULES, today=m1.today_of(case))}


# ---- a label present with an uninterpretable value reaches the same state c06 proves --------
def test_a_real_document_with_null_labelled_fields_reaches_c06s_illegible_states():
    text = ("Guia: NULL\n"
           "Estatus: Entregado\n"
           "Fecha de envio: 2026-09-11\n"
           "Fecha de entrega: NULL\n"
           "Codigo postal destino: NULL\n"
           "FedEx\n")
    extracted = extract_fields(text)
    assert extracted["tracking_number"] == {"value": None, "confidence": "illegible"}
    assert extracted["delivery_date"] == {"value": None, "confidence": "illegible"}
    assert extracted["postal_code"] == {"value": None, "confidence": "illegible"}
    assert extracted["delivery_status"]["confidence"] == "read"   # a real value is still read normally

    real_doc = ExtractedDoc(doc_id="doc_real", fields=extracted, raw_text_available=True, raw_text=text)
    real_checks = _run("c06_illegible", [real_doc])
    fixture_checks = _run("c06_illegible", m1.build_docs(m1.load_case("c06_illegible")))
    for check_id in ("tracking_number", "delivery_date", "destination"):
        assert real_checks[check_id].state == "present_unverifiable" == fixture_checks[check_id].state


def test_a_dash_or_question_mark_placeholder_is_also_illegible_not_missing():
    f = extract_fields("Tracking number: ---\nStatus: ???\n")
    assert f["tracking_number"] == {"value": None, "confidence": "illegible"}
    assert f["delivery_status"] == {"value": None, "confidence": "illegible"}


def test_a_label_with_nothing_after_it_is_illegible_not_missing():
    f = extract_fields("Guia: \nOther unrelated line.\n")
    assert f["tracking_number"] == {"value": None, "confidence": "illegible"}


def test_a_field_whose_label_never_appears_at_all_stays_truly_absent():
    f = extract_fields("Thanks for shopping with us.\n")
    assert "tracking_number" not in f


# ---- a whole document that could not be read at all, through the real upload pipeline --------
def test_an_image_that_extracts_no_text_becomes_illegible_on_every_required_field(tmp_path):
    db_path = tmp_path / "test.db"
    text, ok = extract_text("image/png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    assert ok is False and text is None
    doc_id = repo.insert_document(db_path, DID, "photo.png", "image/png", 40, "sha1", text, ok)
    assert repo.get_document(db_path, doc_id)["extraction_ok"] is False

    docs = extracted_docs_for(db_path, DID)
    assert len(docs) == 1
    for name in REQUIRED_FIELD_NAMES:
        assert docs[0].fields[name] == {"value": None, "confidence": "illegible"}


# ---- A vendor-confirmed vision reading of an image becomes "read" -------
def test_a_confirmed_reading_becomes_read_confidence_everything_else_stays_illegible(tmp_path):
    db_path = tmp_path / "test.db"
    doc_id = repo.insert_document(db_path, DID, "photo.png", "image/png", 40, "sha1", None, False)
    repo.confirm_document_reading(db_path, doc_id, "delivery_status", "Entregado")

    docs = extracted_docs_for(db_path, DID)
    assert docs[0].fields["delivery_status"] == {"value": "Entregado", "confidence": "read"}
    for name in REQUIRED_FIELD_NAMES:
        if name != "delivery_status":
            assert docs[0].fields[name] == {"value": None, "confidence": "illegible"}


def test_a_confirmed_reading_makes_the_image_citable_text_for_delivery_assertions(tmp_path):
    """The whole point of the feature: a claim citing this image can now be checked against real,
    vendor-vouched text, the same as copilot/core/evidence_model.find_unsupported_delivery_assertions
    already does for any other legible document."""
    db_path = tmp_path / "test.db"
    doc_id = repo.insert_document(db_path, DID, "photo.png", "image/png", 40, "sha1", None, False)
    repo.confirm_document_reading(db_path, doc_id, "delivery_status", "Entregado")

    docs, names = docs_for_dispute(db_path, DID)
    assert docs[f"doc_{doc_id}"] == "delivery_status: Entregado"
    assert names[f"doc_{doc_id}"] == "photo.png"


def test_an_unconfirmed_image_contributes_nothing_citable(tmp_path):
    db_path = tmp_path / "test.db"
    repo.insert_document(db_path, DID, "photo.png", "image/png", 40, "sha1", None, False)
    docs, names = docs_for_dispute(db_path, DID)
    assert docs == {} and names == {}


def test_a_legible_document_still_wins_over_an_illegible_one_for_the_same_field(tmp_path):
    """checklist.py's own per-field resolution (unmodified) already prefers 'read' over 'illegible' -
    this proves extracted_docs_for() does not get in the way of that for a real mixed pair of documents."""
    db_path = tmp_path / "test.db"
    legible_text = "Guia: DEMO123\nEstatus: Entregado\n" + "x" * MIN_MEANINGFUL_TEXT_CHARS
    repo.insert_document(db_path, DID, "good.pdf", "application/pdf", 10, "sha1", legible_text, True)
    repo.insert_document(db_path, DID, "bad.png", "image/png", 10, "sha2", None, False)

    docs = extracted_docs_for(db_path, DID)
    assert len(docs) == 2
    checks = _run("c01_complete", docs)
    # "verified"/"conflict" only ever arise from a real `read` value (checklist.py's own tracking_number
    # logic) - either proves good.pdf's read value was used, never suppressed by bad.png's illegible one.
    # "present_unverifiable" would mean the opposite (illegible won) and must never happen here.
    assert checks["tracking_number"].state in ("verified", "conflict")
