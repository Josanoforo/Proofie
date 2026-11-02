import base64
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from copilot import config
from copilot.core.context import fetch_context
from copilot.features.account_cliff import service as account_cliff
from copilot.features.accounting_line import service
from copilot.features.ledger import service as ledger
from copilot.features.outcome.read import read_outcome
from copilot.web.routes_disputes import get_client, templates
from copilot.web.security import rejected_dispute_id

router = APIRouter()


class CaptureChoiceRequired(Exception):
    def __init__(self, captures):
        self.captures = tuple(captures)


class InvalidCaptureSelection(ValueError):
    pass


def _record(client, dispute_id, capture_id=None, *, closure_summary=False):
    outcome = read_outcome(client, dispute_id)
    if not outcome.decided:
        raise ValueError("PayPal has not returned a closed case outcome.")
    norm = outcome.norm if isinstance(outcome.norm, dict) else {}
    ids = list(dict.fromkeys(value for value in (norm.get("seller_transaction_ids") or [])
                             if isinstance(value, str) and value))
    if closure_summary:
        if not ids:
            raise ValueError("PayPal did not return a sale reference for this case.")
        if capture_id is None:
            if len(ids) != 1:
                raise CaptureChoiceRequired(ids)
            capture_id = ids[0]
        elif capture_id not in ids:
            raise InvalidCaptureSelection("Choose a sale reference returned for this case.")
    else:
        capture_id = ids[0] if ids else ""
    now = datetime.now(timezone.utc)
    context_norm = dict(norm) if closure_summary else {"seller_transaction_ids": [capture_id] if capture_id else [],
                                                        "create_time": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    if closure_summary:
        context_norm["seller_transaction_ids"] = [capture_id]
    context = fetch_context(client, context_norm)
    country = (config.RULES_COUNTRY or "").strip().upper()
    estimate = None
    limitations = []
    if country == "US":
        try:
            estimate = account_cliff.compute(client, now=now).fee_now_cents
        except Exception:
            limitations.append("The account-cliff estimate could not be computed.")
    elif country != "US":
        limitations.append("No verified Mexico fee schedule is present in the account-cliff feature.")
    ledger_view = ledger.read(config.db_path(), dispute_id, ())
    line = service.row(dispute_id, capture_id, context.get("total_amount_cents"), context.get("total_currency"),
                       outcome.amount_refunded_cents, outcome.amount_refunded_currency, estimate, country,
                       len(ledger_view.get("records") or []), closure_summary=closure_summary,
                       sale_amount_source=context.source_of("total_amount_cents"),
                       sale_currency_source=context.source_of("total_currency"),
                       sale_date=context.get("payment_date"), sale_date_source=context.source_of("payment_date"),
                       outcome_code=outcome.outcome_code)
    return line, outcome, ledger_view, limitations


@router.get("/disputes/{dispute_id}/accounting-line", response_class=HTMLResponse)
def page(request: Request, dispute_id: str, capture_id: str | None = None, client=Depends(get_client)):
    if rejected_dispute_id(dispute_id):
        return HTMLResponse("Invalid case ID", status_code=400)
    enhanced = config.feature_enabled("closure_summary")
    try:
        line, outcome, ledger_view, limitations = _record(client, dispute_id, capture_id,
                                                           closure_summary=enhanced)
    except CaptureChoiceRequired as exc:
        return templates.TemplateResponse(request, "features/accounting_line/index.html",
            {"line": None, "dispute_id": dispute_id, "outcome": None, "ledger": None, "limitations": [], "capture_options": exc.captures,
             "download": None, "csv_data_uri": None}, status_code=200)
    except InvalidCaptureSelection as exc:
        return HTMLResponse(str(exc), status_code=400)
    except (OSError, ValueError, TypeError):
        return HTMLResponse("Accounting line is unavailable from the current records.", status_code=503)
    csv_data_uri = None
    download = f"/disputes/{dispute_id}/accounting-line.csv"
    if enhanced:
        encoded = base64.b64encode(service.csv_text(line).encode("utf-8")).decode("ascii")
        csv_data_uri = f"data:text/csv;charset=utf-8;base64,{encoded}"
        download = f"/disputes/{dispute_id}/accounting-line.csv?capture_id={line['capture_id']}"
    display = {}
    if enhanced:
        from copilot.features.draft.facts import money_text

        def formatted(cents, currency):
            if not isinstance(cents, int) or not currency:
                return None
            return money_text(cents, currency)
        display = {
            "sale_amount": formatted(line["sale_amount_cents"], line["sale_currency"]),
            "refund_amount": formatted(line["paypal_refund_cents"], line["paypal_refund_currency"]),
            "fee_estimate": formatted(line["account_cliff_fee_estimate_cents"], line["fee_estimate_currency"]),
        }
    return templates.TemplateResponse(request, "features/accounting_line/index.html",
        {"line": line, "dispute_id": dispute_id, "display": display, "outcome": outcome, "ledger": ledger_view, "limitations": limitations,
         "download": download, "csv_data_uri": csv_data_uri, "capture_options": ()})


@router.get("/disputes/{dispute_id}/accounting-line.csv")
def export_csv(dispute_id: str, capture_id: str | None = None, client=Depends(get_client)):
    if rejected_dispute_id(dispute_id):
        return Response("Invalid case ID", status_code=400)
    try:
        enhanced = config.feature_enabled("closure_summary")
        line, _outcome, _ledger_view, _limitations = _record(client, dispute_id, capture_id,
                                                             closure_summary=enhanced)
        content = service.csv_text(line)
    except CaptureChoiceRequired:
        return Response("Choose a sale reference on the accounting summary first.", status_code=409)
    except InvalidCaptureSelection as exc:
        return Response(str(exc), status_code=400)
    except (OSError, ValueError, TypeError):
        return Response("Accounting line unavailable", status_code=503)
    return Response(content, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="accounting-{dispute_id}.csv"'})
