"""Offline tests for tools/sandbox_vault.py and its auto_run step (no network, no credentials)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auto_run_d101_vault as step  # noqa: E402
import sandbox_vault as sv  # noqa: E402

VAULT = "VAULTID1234567890"


def test_order_body_pays_from_the_saved_method_with_no_approval_link_request():
    body = sv.order_body(VAULT, 2500, "VAULT-SALE-1")
    assert body["payment_source"] == {"paypal": {"vault_id": VAULT}}
    assert body["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "25.00"}
    assert body["intent"] == "CAPTURE"


def test_ids_are_masked_in_every_view():
    assert sv.mask(VAULT) == "VAUL...890"
    view = json.dumps(sv.masked_view(sv.plan_bodies(VAULT, 2, 2500)))
    assert VAULT not in view and "VAUL...890" in view


def test_the_fingerprint_binds_the_exact_requests():
    a = sv.fingerprint(sv.plan_bodies(VAULT, 2, 2500))
    assert a == sv.fingerprint(sv.plan_bodies(VAULT, 2, 2500))
    assert a != sv.fingerprint(sv.plan_bodies(VAULT, 3, 2500))
    assert a != sv.fingerprint(sv.plan_bodies(VAULT, 2, 2600))
    assert a != sv.fingerprint(sv.plan_bodies("OTHERVAULT123456", 2, 2500))


def test_orders_are_not_sent_without_the_matching_approval(tmp_path):
    state = sv.State(tmp_path / sv.STATE_NAME)
    state.save(payment_token_id=VAULT)
    client = step.StandIn()
    with pytest.raises(sv.Stop, match="approval does not match"):
        sv.run_orders(client, state, 2, 2500, "0000000000000000", emit=lambda *_: None)
    assert client.calls == []


def test_count_must_be_a_small_whole_number():
    for bad in (0, 11, True, 2.5):
        with pytest.raises(sv.Stop):
            sv.plan_bodies(VAULT, bad, 2500)


def test_the_walk_prints_the_receipt_with_zero_buyer_hands():
    line = step.walk()
    assert line.startswith("2 cobros sobre un método guardado tras una aprobación del comprador; manos del comprador por venta: 0")


def test_the_walk_fails_when_an_order_asks_the_buyer_again():
    with pytest.raises(sv.Stop):
        step.walk(ask_buyer_again=True)


def test_an_unapproved_setup_token_stops_before_any_payment_token(tmp_path):
    state = sv.State(tmp_path / sv.STATE_NAME)
    state.save(setup_token_id="SETUPTOKEN123456")

    class Pending(step.StandIn):
        def call(self, method, path, op, **kw):
            if path.startswith(sv.SETUP_PATH):
                return {"id": "SETUPTOKEN123456", "status": "PAYER_ACTION_REQUIRED"}
            return super().call(method, path, op, **kw)

    client = Pending()
    with pytest.raises(sv.Stop, match="not APPROVED"):
        sv.take_payment_token(client, state, emit=lambda *_: None)
    assert ("POST", sv.TOKEN_PATH) not in client.calls


def test_a_second_token_command_does_not_create_another_payment_token(tmp_path):
    state = sv.State(tmp_path / sv.STATE_NAME)
    state.save(setup_token_id="SETUPTOKEN123456")
    client = step.StandIn()
    created = sv.take_payment_token(client, state, emit=lambda *_: None)
    posts = [c for c in client.calls if c[0] == "POST"]
    assert sv.take_payment_token(client, state, emit=lambda *_: None) == created
    assert [c for c in client.calls if c[0] == "POST"] == posts


def test_adopt_reads_the_setup_id_from_the_probe_evidence(tmp_path):
    evidence = tmp_path / "e.jsonl"
    evidence.write_text(json.dumps({"method": "POST", "path": "/v3/vault/setup-tokens", "status": 201}) + "\n"
                        + json.dumps({"method": "GET", "path": "/v3/vault/setup-tokens/ABC123XYZ789", "status": 200}) + "\n",
                        encoding="utf-8")
    assert sv.adopt_setup_id(evidence) == "ABC123XYZ789"
    evidence.write_text("{}\n", encoding="utf-8")
    with pytest.raises(sv.Stop):
        sv.adopt_setup_id(evidence)


def test_a_state_path_git_would_track_is_refused():
    root = Path(__file__).resolve().parents[1]
    assert sv._state_path(root, None).parts[-3:-1] == ("data", "sandbox-seed")
    with pytest.raises(sv.Stop):
        sv._state_path(root, root / "tools" / "vault-state.json")


def test_a_client_not_pointed_at_the_sandbox_is_refused():
    class Http:
        def __init__(self, base):
            self.base_url = base

    class Client:
        def __init__(self, base):
            self._http = Http(base)

    sv.assert_sandbox_host(Client("https://api-m.sandbox.paypal.com"))
    for bad in ("https://{}.{}".format("api-m", "pay" "pal.com"), "https://example.com", "https://api-m.sandbox." + "pay" + "pal.com.example.com", ""):
        with pytest.raises(sv.Stop):
            sv.assert_sandbox_host(Client(bad))
    with pytest.raises(sv.Stop):
        sv.assert_sandbox_host(object())


def test_nothing_the_helper_prints_holds_a_full_id(tmp_path):
    state = sv.State(tmp_path / sv.STATE_NAME)
    state.save(setup_token_id="SETUPTOKEN123456")
    lines = []
    client = step.StandIn()
    sv.take_payment_token(client, state, emit=lines.append)
    bodies = sv.plan_bodies(state.data["payment_token_id"], 2, 2500)
    sv.run_orders(client, state, 2, 2500, sv.fingerprint(bodies), emit=lines.append)
    text = "\n".join(lines)
    for full in ("SETUPTOKEN123456", "STANDINTOKEN0001", "STANDINORDER0001", "STANDINCAPTURE001", "STANDINCUST0001"):
        assert full not in text
    assert text.count("request approved by the seller") == 2


def test_the_step_result_shows_the_sellers_approval_of_each_order_and_makes_no_write_by_default():
    line = step.walk()
    assert "aprobaciones del vendedor mostradas: 2 de 2" in line
    assert "CON ESCRITURAS" not in line
    class Args:  # the default run goes to the stand-in
        allow_vault_live = False
    class Runner:
        args = Args()
    class Ctx:
        runner = Runner()
    assert "CON ESCRITURAS" not in step.run(Ctx())


def test_the_helper_has_no_refund_path():
    for module in (sv, step):
        source = Path(module.__file__).read_text(encoding="utf-8").lower()
        assert "/refund" not in source and "refund(" not in source
    assert all("refund" not in path for path in (sv.SETUP_PATH, sv.TOKEN_PATH, sv.ORDER_PATH))


class _SandboxStandIn(step.StandIn):
    """The step's PayPal stand-in, pointed (as far as the host guard can tell) at the sandbox API."""

    def __init__(self):
        super().__init__()

        class Http:
            base_url = "https://api-m.sandbox.paypal.com"
        self._http = Http()


def _state_with_two_sales(tmp_path):
    state = sv.State(tmp_path / "vault-state.json")
    state.save(setup_token_id="SETUP0000000001", payment_token_id=VAULT, setup_status="APPROVED",
               sale1={"order_id": "ORDER000000001", "capture_id": "CAPTURE0000001", "capture_status": "COMPLETED"},
               sale2={"order_id": "ORDER000000002", "capture_id": "CAPTURE0000002", "capture_status": "COMPLETED"})
    return state


def test_vault_fresh_archives_the_state_in_code_keeps_the_token_and_says_so(tmp_path):
    import datetime as dt
    state = _state_with_two_sales(tmp_path)
    lines = []
    args = type("A", (), {"vault_fresh": True})()
    result = step.live(args, client=_SandboxStandIn(), state_path=state.path, emit=lines.append, now=dt.datetime(2026, 11, 1, 22, 0, 5))
    archive = tmp_path / "vault-state-20261101-220005.json"
    assert archive.exists() and "sale1" in json.loads(archive.read_text(encoding="utf-8"))      # the old state, untouched
    assert any("--vault-fresh" in line and archive.name in line and "payment token is kept" in line for line in lines)
    assert "manos del comprador por venta: 0" in result and "aprobaciones del vendedor mostradas: 2 de 2" in result
    now_state = json.loads(state.path.read_text(encoding="utf-8"))
    assert now_state["payment_token_id"] == VAULT and now_state["sale1"]["order_id"] != "ORDER000000001"   # new sales were charged


def test_without_vault_fresh_the_old_sales_are_not_charged_again(tmp_path):
    state = _state_with_two_sales(tmp_path)
    client = _SandboxStandIn()
    lines = []
    step.live(type("A", (), {})(), client=client, state_path=state.path, emit=lines.append)
    assert [c for c in client.calls if c[0] == "POST"] == []              # nothing written: both sales were already captured
    assert not list(tmp_path.glob("vault-state-2*.json"))


def test_the_run_alone_keeps_every_line_with_the_fingerprint_in_a_log_and_needs_no_dispute(tmp_path):
    state = _state_with_two_sales(tmp_path)
    log = tmp_path / "out" / "6.9-vault-live.log"
    args = type("A", (), {"vault_fresh": True})()
    code = step.run_alone(args, log, client=_SandboxStandIn(), state_path=state.path)
    text = log.read_text(encoding="utf-8")
    assert code == 0 and text.count("request approved by the seller before sending (fingerprint ") == 2
    fingerprint = sv.fingerprint(sv.plan_bodies(VAULT, step.N, 2500))
    assert fingerprint in text and "[PASA] 6.9" in text and "manos del comprador por venta: 0" in text
    assert "--vault-fresh" in text


def test_the_run_alone_fails_and_logs_when_an_order_asks_the_buyer_again(tmp_path):
    state = sv.State(tmp_path / "vault-state.json")
    state.save(payment_token_id=VAULT)
    client = _SandboxStandIn()
    client.ask_again = True
    log = tmp_path / "6.9.log"
    assert step.run_alone(type("A", (), {})(), log, client=client, state_path=state.path) == 1
    assert "[FALLA] 6.9" in log.read_text(encoding="utf-8")

