import pytest

from copilot.features.evidence_dossier import service


def test_reason_matrix_chronology_annex_manifest_and_missing_sources():
    result = service.assemble(service.EXAMPLES["physical"])
    assert result["reason"] == "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"
    assert [row["proof_type"] for row in result["proof_matrix"]["items"]] == ["shipment", "tracking", "delivery record"]
    assert [row["id"] for row in result["chronology"]] == ["order", "delivery"]
    assert result["annexes"][0]["included"] is True
    assert result["preview"]["annexes"][0]["sha256"] == result["annexes"][0]["sha256"]
    incomplete = service.assemble(service.EXAMPLES["incomplete"])
    assert incomplete["claims"][0]["missing"] == ["missing-completion"]
    assert incomplete["annexes"][0]["included"] is False
    assert incomplete["annexes"][0]["sha256"] is None


def test_exact_reason_enum_unknown_and_unsupported_conflict_are_honest():
    assert service.assemble({"reason": "UNAUTHORISED", "kind": "physical"})["reason"] == "UNAUTHORISED"
    unknown = service.assemble({"reason": "UNAUTHORIZED_TRANSACTION", "kind": "service"})
    assert unknown["reason"] is None and unknown["reason_label"] == "Unknown reason"
    assert unknown["proof_matrix"]["items"] == []
    conflict = service.assemble({"facts": [{"id": "s", "status": "conflict", "text": "t", "source": "r"}], "claims": [{"text": "claim", "source_ids": ["s"]}]})
    assert conflict["claims"][0]["status"] == "conflict"
    assert "verified" not in conflict["claims"][0]["status"]


@pytest.mark.parametrize("payload", [[], {"facts": {}}, {"claims": ["bad"]}, {"annexes": [None]}, {"facts": [{"id": "x"}, {"id": "x"}]}, {"facts": [{"id": "x", "at": 7}]}])
def test_invalid_shapes_duplicate_ids_and_types_are_rejected(payload):
    with pytest.raises(ValueError):
        service.assemble(payload)


def test_safe_escaping_is_preserved_by_template_contract():
    raw = {"claims": [{"text": "<script>alert(1)</script>"}]}
    output = service.assemble(raw)
    assert output["claims"][0]["text"] == "<script>alert(1)</script>"
    from jinja2 import Environment, select_autoescape
    rendered = Environment(autoescape=select_autoescape(default=True)).from_string("{{ claim }}").render(claim=output["claims"][0]["text"])
    assert "&lt;script&gt;" in rendered and "<script>" not in rendered
