from __future__ import annotations

import csv
import io


LEGACY_HEADER = ("dispute_id", "capture_id", "sale_amount_cents", "sale_currency", "paypal_refund_cents",
          "paypal_refund_currency", "paypal_applied_dispute_fee_cents", "applied_fee_status",
          "account_cliff_fee_estimate_cents", "fee_estimate_currency", "fee_estimate_status", "ledger_record_count")
CLOSURE_HEADER = ("dispute_id", "capture_id", "sale_amount_cents", "sale_currency", "sale_amount_source",
                  "sale_currency_source", "sale_amount_basis", "sale_date", "sale_date_source", "outcome_code", "outcome_source",
                  "paypal_refund_cents", "paypal_refund_currency", "paypal_refund_source",
                  "paypal_applied_dispute_fee_cents", "applied_fee_status", "account_cliff_fee_estimate_cents",
                  "fee_estimate_currency", "fee_estimate_status", "ledger_record_count")
# Keep the old public constant stable for existing callers/tests.
HEADER = LEGACY_HEADER


def row(dispute_id: str, capture_id: str, sale_amount_cents: int | None, sale_currency: str | None,
        refund_cents: int | None, refund_currency: str | None, fee_estimate_cents: int | None,
        country: str, ledger_record_count: int, *, closure_summary: bool = False,
        sale_amount_source: str | None = None, sale_currency_source: str | None = None, sale_date: str | None = None,
        sale_date_source: str | None = None, outcome_code: str | None = None) -> dict:
    for amount in (sale_amount_cents, refund_cents, fee_estimate_cents):
        if amount is not None and type(amount) is not int:
            raise ValueError("Money values must be integer cents.")
    result = {"dispute_id": dispute_id, "capture_id": capture_id,
            "sale_amount_cents": sale_amount_cents if sale_amount_cents is not None else "",
            "sale_currency": sale_currency or "", "paypal_refund_cents": refund_cents if refund_cents is not None else "",
            "paypal_refund_currency": refund_currency or "", "paypal_applied_dispute_fee_cents": "",
            "applied_fee_status": "Not returned in PayPal dispute read; no applied fee asserted",
            "account_cliff_fee_estimate_cents": fee_estimate_cents if fee_estimate_cents is not None else "",
            "fee_estimate_currency": "USD" if fee_estimate_cents is not None else "",
            "fee_estimate_status": ("US account-cliff schedule estimate only" if country == "US" and fee_estimate_cents is not None
                                    else "No verified Mexico fee schedule" if country == "MX"
                                    else "No verified fee schedule for this country" if country != "US"
                                    else "Estimate unavailable from account-cliff read"),
            "ledger_record_count": ledger_record_count}
    if not closure_summary:
        return result
    basis = ("Transaction Search gross (includes shipping and tax)" if sale_amount_source == "paypal_transaction_search"
             else "Disputed principal only; not gross sale" if sale_amount_source == "paypal_dispute"
             else "Unknown source; not classified as gross sale")
    return {"dispute_id": dispute_id, "capture_id": capture_id,
            "sale_amount_cents": result["sale_amount_cents"], "sale_currency": result["sale_currency"],
            "sale_amount_source": sale_amount_source or "", "sale_currency_source": sale_currency_source or "",
            "sale_amount_basis": basis,
            "sale_date": sale_date or "", "sale_date_source": sale_date_source or "",
            "outcome_code": outcome_code or "", "outcome_source": "PayPal dispute GET",
            "paypal_refund_cents": result["paypal_refund_cents"],
            "paypal_refund_currency": result["paypal_refund_currency"],
            "paypal_refund_source": "PayPal dispute GET" if refund_cents is not None else "",
            "paypal_applied_dispute_fee_cents": result["paypal_applied_dispute_fee_cents"],
            "applied_fee_status": result["applied_fee_status"],
            "account_cliff_fee_estimate_cents": result["account_cliff_fee_estimate_cents"],
            "fee_estimate_currency": result["fee_estimate_currency"],
            "fee_estimate_status": result["fee_estimate_status"],
            "ledger_record_count": ledger_record_count}


def csv_text(record: dict) -> str:
    keys = set(record)
    fieldnames = CLOSURE_HEADER if keys == set(CLOSURE_HEADER) else LEGACY_HEADER if keys == set(LEGACY_HEADER) else None
    if fieldnames is None:
        raise ValueError("Accounting row does not match the one-row CSV schema.")
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\r\n", extrasaction="raise")
    writer.writeheader()
    writer.writerow(record)
    return buffer.getvalue()
