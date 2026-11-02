from datetime import datetime, timezone

import pytest

from copilot.features.sale_lesson import service


def test_lesson_is_seller_authored_and_paypal_outcome_is_separate(tmp_path, monkeypatch):
    monkeypatch.setattr(service.config, "data_dir", lambda: tmp_path)
    record = service.record_lesson("PP-R-TST-0000001", "12345678901234567", "physical",
                                   "RESOLVED_BUYER_FAVOUR", "Take a parcel photo before pickup.",
                                   now=datetime(2026, 10, 25, tzinfo=timezone.utc))
    assert record["seller_lesson"] == "Take a parcel photo before pickup."
    assert record["paypal_outcome_code"] == "RESOLVED_BUYER_FAVOUR"
    assert record["lesson_source"] == "seller-entered"
    assert service.list_lessons("physical") == [record]
    assert service.list_lessons("digital") == []


@pytest.mark.parametrize("code", [None, "NONE", "PENDING"])
def test_unclosed_or_unknown_outcomes_cannot_be_saved(tmp_path, monkeypatch, code):
    monkeypatch.setattr(service.config, "data_dir", lambda: tmp_path)
    with pytest.raises(ValueError, match="closed PayPal-recorded"):
        service.record_lesson("PP-R-TST-0000001", "12345678901234567", "physical", code, "Note")
    assert service.list_lessons() == []


def test_one_lesson_per_case_and_single_line_length(tmp_path, monkeypatch):
    monkeypatch.setattr(service.config, "data_dir", lambda: tmp_path)
    args = ("PP-R-TST-0000001", "12345678901234567", "service", "DENIED", "Review the written acceptance.")
    service.record_lesson(*args)
    with pytest.raises(ValueError, match="already recorded"):
        service.record_lesson(*args)
    with pytest.raises(ValueError, match="one seller-authored"):
        service.record_lesson("PP-R-TST-0000002", "12345678901234567", "service", "DENIED", "x\ny")


def test_links_are_in_closed_case_and_next_sale_flows():
    from pathlib import Path
    detail = "".join(p.read_text(encoding="utf-8") for p in [Path("copilot/web/templates/dispute_detail.html"), Path("copilot/web/templates/case_task.html"), *sorted(Path("copilot/web/templates/case").glob("*.html"))])   # v3: the case page is split into task views
    readiness = Path("copilot/web/templates/features/fulfilment/form.html").read_text(encoding="utf-8")
    assert 'feature_enabled("sale_lesson")' in detail
    assert 'feature_enabled("sale_lesson")' in readiness
    assert "/sale-lessons?kind=" in readiness
    close = Path("copilot/web/templates/features/sale_lesson/close.html").read_text(encoding="utf-8")
    listing = Path("copilot/web/templates/features/sale_lesson/list.html").read_text(encoding="utf-8")
    assert "your own view, not a cause confirmed by PayPal" in close
    assert "not a PayPal-confirmed cause" in listing
