import pytest

from copilot.features.call_script import service


@pytest.mark.parametrize("locale", ["en", "es"])
def test_fixed_script_has_opening_questions_and_no_promises(locale):
    result = service.build(locale, "WAITING_FOR_SELLER_RESPONSE", "INQUIRY")
    assert all(result["sections"].values())
    assert "promise" in result["sections"]["avoid"].lower() or "prometa" in result["sections"]["avoid"].lower()
    assert result["paypal_status"] == "WAITING_FOR_SELLER_RESPONSE"


def test_script_requires_seller_to_choose_supported_buyer_language():
    with pytest.raises(ValueError, match="Choose English or Spanish"):
        service.build("fr", "INQUIRY", "INQUIRY")


def test_real_dispute_flow_links_only_during_consultation():
    from pathlib import Path
    template = "".join(p.read_text(encoding="utf-8") for p in [Path("copilot/web/templates/dispute_detail.html"), Path("copilot/web/templates/case_task.html"), *sorted(Path("copilot/web/templates/case").glob("*.html"))])   # v3: the case page is split into task views
    assert 'feature_enabled("call_script")' in template
    assert 'norm.stage == "INQUIRY"' in template
