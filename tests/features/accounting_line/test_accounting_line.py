import csv
import io

import pytest

from copilot.features.accounting_line import service


def test_csv_has_one_reconciliation_row_and_separates_applied_fee_from_estimate():
    row = service.row("PP-R-TST-0000001", "12345678901234567", 1299, "MXN", 300, "MXN", None, "MX", 4)
    output = service.csv_text(row)
    parsed = list(csv.reader(io.StringIO(output)))
    assert len(parsed) == 2
    assert parsed[0][0] == "dispute_id"
    assert parsed[1][2:6] == ["1299", "MXN", "300", "MXN"]
    assert row["paypal_applied_dispute_fee_cents"] == ""
    assert "Not returned" in row["applied_fee_status"]
    assert row["account_cliff_fee_estimate_cents"] == ""
    assert "No verified Mexico fee schedule" in row["fee_estimate_status"]


def test_us_account_cliff_estimate_is_not_applied_fee():
    row = service.row("PP-R-TST-0000001", "12345678901234567", 1299, "USD", None, None, 1500, "US", 2)
    assert row["account_cliff_fee_estimate_cents"] == 1500
    assert row["fee_estimate_status"] == "US account-cliff schedule estimate only"
    assert row["paypal_applied_dispute_fee_cents"] == ""


def test_amounts_must_be_integer_cents():
    with pytest.raises(ValueError, match="integer cents"):
        service.row("PP-R-TST-0000001", "12345678901234567", 12.99, "USD", None, None, None, "US", 0)


def test_closed_case_flow_link_is_guarded():
    from pathlib import Path
    template = "".join(p.read_text(encoding="utf-8") for p in [Path("copilot/web/templates/dispute_detail.html"), Path("copilot/web/templates/case_task.html"), *sorted(Path("copilot/web/templates/case").glob("*.html"))])   # v3: the case page is split into task views
    assert 'feature_enabled("accounting_line")' in template
    assert "decided" in template


def test_accounting_headers_use_closed_shared_words():
    from pathlib import Path
    from copilot.web.field_words import UNKNOWN_FIELD_WORD, field_word
    template = Path("copilot/web/templates/features/accounting_line/index.html").read_text(encoding="utf-8")
    assert "key|field_word" in template
    assert all(field_word(key) != UNKNOWN_FIELD_WORD and "_" not in field_word(key) for key in service.HEADER)
