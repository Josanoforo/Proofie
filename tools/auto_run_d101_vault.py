"""D-101 R2-B step for tools/auto_run.py: charges on one saved method after one buyer approval, buyer hands counted from the walk.

The step runs tools/sandbox_vault.py's order chain against a stateful stand-in for PayPal (no network, no credentials): the vault
payment token is made from an approved setup token, N orders are created with payment_source.paypal.vault_id, each captured and read
back. The buyer's hands are counted from the stand-in's answers: an order that comes back PAYER_ACTION_REQUIRED is a hand. The step
prints the receipt line and FAILS above 0. The real charges (two captures on the sandbox) are in logs-RC44/D101/r2b/after/evidence."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sandbox_vault as sv  # noqa: E402

STEP = "6.9"
TITLE = "Varios cobros sobre un método guardado, manos del comprador contadas"
N = 2
EXPECTED = (f"{N} órdenes con vault_id se crean y se capturan sin enlace de aprobación; cada captura se lee por GET; "
            "las manos del comprador (órdenes que piden aprobación otra vez) se cuentan de las respuestas y fallan si pasan de 0")


class StandIn:
    """A PayPal Vault + Orders stand-in: stateful, answers like the sandbox did (orders on a saved method come back COMPLETED)."""

    def __init__(self, ask_buyer_again: bool = False):
        self.orders, self.ask_again, self.calls = {}, ask_buyer_again, []

    def call(self, method, path, op, *, json_body=None, request_id=None, log_extra=None):
        self.calls.append((method, path))
        if path.startswith(sv.SETUP_PATH):
            return {"id": path.rsplit("/", 1)[-1], "status": "APPROVED"}
        if method == "POST" and path == sv.TOKEN_PATH:
            assert json_body["payment_source"]["token"]["type"] == "SETUP_TOKEN"
            return {"id": "STANDINTOKEN0001", "customer": {"id": "STANDINCUST0001"}}
        if path.startswith(sv.TOKEN_PATH + "/"):
            return {"id": path.rsplit("/", 1)[-1], "customer": {"id": "STANDINCUST0001"}}
        if method == "POST" and path == sv.ORDER_PATH:
            paypal = json_body["payment_source"]["paypal"]
            assert paypal["vault_id"] and "experience_context" not in paypal
            n = len(self.orders) + 1
            order_id = f"STANDINORDER{n:04d}"
            status = "PAYER_ACTION_REQUIRED" if self.ask_again else "COMPLETED"
            captures = [] if self.ask_again else [{"id": f"STANDINCAPTURE{n:03d}", "status": "COMPLETED"}]
            self.orders[order_id] = {"id": order_id, "status": status, "purchase_units": [{"payments": {"captures": captures}}]}
            return self.orders[order_id]
        if path.startswith(sv.ORDER_PATH + "/"):
            return self.orders[path.rsplit("/", 1)[-1]]
        if path.startswith("/v2/payments/captures/"):
            return {"id": path.rsplit("/", 1)[-1], "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "25.00"}}
        raise AssertionError(f"unexpected call {method} {path}")


def walk(ask_buyer_again: bool = False) -> str:
    """Default mode: a stand-in, no write anywhere. The seller's approval of each order shows in the printed result."""
    lines: list[str] = []
    with tempfile.TemporaryDirectory() as folder:
        state = sv.State(Path(folder) / sv.STATE_NAME)
        state.save(setup_token_id="STANDINSETUP0001")
        client = StandIn(ask_buyer_again)
        sv.take_payment_token(client, state, emit=lambda *_: None)
        bodies = sv.plan_bodies(state.data["payment_token_id"], N, 2500)
        result = sv.run_orders(client, state, N, 2500, sv.fingerprint(bodies), emit=lines.append)
        writes = sum(1 for m, _ in client.calls if m == "POST")
        approvals = sum("approved by the seller" in line for line in lines)
        return (sv.receipt(result, N) + f"; aprobaciones del vendedor mostradas: {approvals} de {N}; "
                f"escrituras al simulador: {writes} (1 token + {N} órdenes)")


def archive_state(state: "sv.State", now=None) -> Path:
    """--vault-fresh: keep the state as it was in a timestamped file next to it, then clear the sales so a new run charges again.
    The payment token (and the setup token) stay. Done in code, so nobody edits the state file by hand."""
    import datetime as dt
    stamp = (now or dt.datetime.now()).strftime("%Y%m%d-%H%M%S")
    target = state.path.with_name(f"{state.path.stem}-{stamp}{state.path.suffix}")
    if target.exists():
        raise sv.Stop(f"The archive {target.name} already exists; nothing was changed.")
    target.write_bytes(state.path.read_bytes())
    for key in [k for k in state.data if k.startswith("sale")]:
        state.data.pop(key)
    state.path.write_text(json.dumps(state.data, indent=2), encoding="utf-8")
    return target


def live(args, *, client=None, state_path: Path | None = None, emit=print, now=None) -> str:
    """Explicit opt-in (--allow-vault-live): the real chain on the sandbox from the private state; every order request is approved by
    its exact fingerprint, and each 'Sale N: request approved by the seller ...' line goes through `emit` (auto_run keeps them in a log).
    --vault-fresh first archives the state in code (payment token kept). Needs the payment token already in the state."""
    root = Path(__file__).resolve().parents[1]
    if client is None:
        sys.path.insert(0, str(root))
        from pilot.client import SandboxClient
        client = SandboxClient("auto_run_vault")
    sv.assert_sandbox_host(client)
    state = sv.State(state_path or sv._state_path(root, None))
    if getattr(args, "vault_fresh", False):
        archived = archive_state(state, now)
        emit(f"--vault-fresh: the state was archived to {archived.name}; the payment token is kept and the sales are cleared; this run charges {N} new sales.")
    lines: list[str] = []

    def both(line):
        lines.append(line)
        emit(line)
    bodies = sv.plan_bodies(state.data.get("payment_token_id", ""), N, 2500)
    result = sv.run_orders(client, state, N, 2500, sv.fingerprint(bodies), emit=both)
    approvals = sum("approved by the seller" in line for line in lines)
    return sv.receipt(result, N) + f"; aprobaciones del vendedor mostradas: {approvals} de {N}; CON ESCRITURAS al sandbox"


def run_alone(args, log_path: Path, *, client=None, state_path: Path | None = None, now=None) -> int:
    """`auto_run.py --only 6.9 --allow-vault-live`: 6.9 needs no dispute and no app, so it runs by itself and the whole output
    (every line, with the seller's approval fingerprint) is written to `log_path`."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    out: list[str] = []

    def emit(line):
        out.append(line)
        print(line)
    try:
        result = live(args, client=client, state_path=state_path, emit=emit, now=now)
        status = "PASA"
    except sv.Stop as exc:
        result, status = str(exc), "FALLA"
    except Exception as exc:  # noqa: BLE001 - PayPal refusals and the like: kept in the log, exit 1
        result, status = f"{type(exc).__name__}: {exc}", "FALLA"
    out.append(f"[{status}] {STEP} {TITLE}: {result}")
    print(out[-1])
    log_path.write_text(chr(10).join(out) + chr(10), encoding="utf-8")
    return 0 if status == "PASA" else 1


def run(c) -> str:
    if getattr(c.runner.args, "allow_vault_live", False):
        return live(c.runner.args)
    return walk()
