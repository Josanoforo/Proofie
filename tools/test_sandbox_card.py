"""Offline tests for tools/sandbox_card.py and its auto_run step (no network, no credentials, no real card number)."""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auto_run_d101_b8 as step  # noqa: E402
import sandbox_card as sc  # noqa: E402

CARD = step.CARD


def test_order_body_is_a_card_order_with_a_billing_address_in_the_merchant_currency():
    body = sc.variant_body(CARD, 2)
    card = body["payment_source"]["card"]
    assert body["intent"] == "CAPTURE"
    assert body["purchase_units"][0]["amount"] == {"currency_code": "MXN", "value": "500.00"}
    assert card["billing_address"]["country_code"] == "MX" and card["expiry"] == "2030-12"
    assert card["attributes"]["verification"]["method"] == "SCA_WHEN_REQUIRED"
    assert sc.variant_body(CARD, 3)["purchase_units"][0]["amount"]["currency_code"] == "USD"
    with pytest.raises(sc.Stop):
        sc.variant_body(CARD, 4)


def test_the_card_number_is_masked_and_the_security_code_hidden_in_every_view():
    view = json.dumps(sc.masked_view(sc.variant_body(CARD, 2)))
    assert CARD["number"] not in view and CARD["cvv"] not in view and "***" in view


def test_the_fingerprint_binds_the_exact_request():
    a = sc.fingerprint(sc.variant_body(CARD, 2))
    assert a == sc.fingerprint(sc.variant_body(CARD, 2)) and a != sc.fingerprint(sc.variant_body(CARD, 3))
    assert a != sc.fingerprint(sc.variant_body(dict(CARD, number="4" + "1" * 15), 2))


def test_nothing_is_sent_without_the_matching_approval(tmp_path):
    client = step.StandIn()
    with pytest.raises(sc.Stop, match="approval does not match"):
        sc.run_order(client, sc.State(tmp_path / "s.json"), CARD, 2, "0" * 16, emit=lambda *_: None)
    assert client.calls == []
    state = sc.State(tmp_path / "t.json")
    state.save(capture={"id": "CAP1234567890", "merchant_id": "MERCH", "amount": {"currency_code": "MXN", "value": "1.00"}})
    with pytest.raises(sc.Stop, match="approval does not match"):
        sc.run_chargeback(client, state, "0" * 16, emit=lambda *_: None)
    assert client.calls == []


def test_the_chargeback_needs_a_completed_capture_with_its_merchant_id(tmp_path):
    with pytest.raises(sc.Stop, match="run order first"):
        sc.run_chargeback(step.StandIn(), sc.State(tmp_path / "s.json"), "x", emit=lambda *_: None)


def test_the_chargeback_body_follows_the_documented_shape_with_dates_computed_by_code():
    now = dt.datetime(2026, 11, 2, 12, 0, 0, tzinfo=dt.timezone.utc)
    body = sc.chargeback_body({"id": "CAP1234567890", "amount": {"currency_code": "MXN", "value": "500.00"}}, "MERCH", "visa", now=now)
    assert (body["adjacency"], body["file_layout"], body["financial_institution"]["processor"]) == ("PAYPAL", "ATCK_CB", "FDMS")
    assert body["transaction"]["id_enc"] == "CAP1234567890" and body["instrument"]["instrument_type"] == "CARD"
    assert body["dispute"]["receive_date"] == "2026-11-02T12:00:00.000Z"
    assert body["dispute"]["response_date"] == "2026-11-12T12:00:00.000Z"
    assert body["transaction"]["amount"]["value"] == "500.00"


def test_the_reason_code_is_one_of_two_and_bound_by_the_approval(tmp_path):
    capture = {"id": "CAP1234567890", "amount": {"currency_code": "MXN", "value": "500.00"}}
    assert sc.chargeback_body(capture, "MERCH", "visa")["dispute"]["processor_info"]["reason_code"] == "1310"
    assert sc.chargeback_body(capture, "MERCH", "visa", reason_code="1330")["dispute"]["processor_info"]["reason_code"] == "1330"
    assert sc.chargeback_body(capture, "MERCH", "visa", reason_code="1310")["dispute"]["processor_info"]["reason_code"] == "1310"
    with pytest.raises(sc.Stop):
        sc.chargeback_body(capture, "MERCH", "visa", reason_code="9999")
    state = sc.State(tmp_path / "s.json")
    state.save(capture=dict(capture, merchant_id="MERCH"))
    assert sc.chargeback_fingerprint(state, "1330") != sc.chargeback_fingerprint(state, "1310")
    client = step.StandIn()
    with pytest.raises(sc.Stop, match="approval does not match"):
        sc.run_chargeback(client, state, sc.chargeback_fingerprint(state, "1330"), emit=lambda *_: None, reason_code="1310")
    assert client.calls == []


def test_the_walk_prints_the_receipt_with_zero_buyer_hands_and_both_approvals():
    line = step.walk()
    assert line.startswith("disputa abierta sobre una venta pagada con tarjeta de prueba; manos del comprador: 0")
    assert "aprobaciones del vendedor mostradas: 2 de 2" in line


def _case(**kw):
    state_dir = kw.pop("state_dir")
    state = sc.State(state_dir / "case.json")
    client = step.StandIn(**kw)
    result = sc.run_case(client, state, CARD, 2, "1330", sc.case_fingerprints(CARD, 2, "1330"), emit=lambda *_: None, sleep=lambda *_: None)
    return client, state, result


def test_the_case_chain_runs_in_order_and_ends_ready_to_answer(tmp_path):
    client, state, result = _case(state_dir=tmp_path)
    assert result == {"ready": True, "hands": 0, "dispute_id": "PP-D-STANDIN-1", "status": "WAITING_FOR_SELLER_RESPONSE"}
    writes = [(m, p.split("/")[-1]) for m, p in client.calls if m == "POST"]
    assert writes == [("POST", "orders"), ("POST", "process-chargeback"), ("POST", "provide-supporting-info")]
    posted = [p for m, p in client.calls if m == "POST"]
    assert posted.index(sc.ORDER_PATH) < posted.index(sc.CHARGEBACK_PATH) < len(posted) - 1


def test_no_supporting_info_is_sent_unless_paypal_lists_the_link(tmp_path):
    client, state, result = _case(state_dir=tmp_path, offer_support_link=False)
    assert result["ready"] is False and result["stopped"] == "no provide_supporting_info link"
    assert not any(p.endswith("supporting-info") for _, p in client.calls)


def test_the_case_waits_a_bounded_time_for_the_link_then_sends_one_supporting_info(tmp_path):
    client = step.StandIn(link_after=3)
    sleeps = []
    result = sc.run_case(client, sc.State(tmp_path / "w.json"), CARD, 2, "1330", sc.case_fingerprints(CARD, 2, "1330"),
                         emit=lambda *_: None, sleep=sleeps.append)
    assert result["ready"] is True and len(sleeps) >= 3
    assert [p for m, p in client.calls if m == "POST" and p.endswith("supporting-info")] != []
    never = step.StandIn(offer_support_link=False)
    gave_up = sc.run_case(never, sc.State(tmp_path / "n.json"), CARD, 2, "1330", sc.case_fingerprints(CARD, 2, "1330"),
                          emit=lambda *_: None, sleep=lambda *_: None, link_polls=4)
    assert gave_up["ready"] is False
    assert len([1 for m, p in never.calls if m == "GET" and p.startswith(sc.DISPUTE_PATH)]) <= 1 + 1 + 4 + 0   # create read + first case read + 4 waits


def test_the_supporting_info_body_is_one_multipart_input_part_with_a_plain_notes_string(tmp_path):
    part = sc.support_multipart()
    assert list(part) == ["input"]
    filename, content, content_type = part["input"]
    assert filename is None and content_type == "application/json"
    assert json.loads(content) == {"notes": sc.SUPPORT_NOTE} and isinstance(json.loads(content)["notes"], str)
    seen = {}

    class Spy(step.StandIn):
        def call(self, method, path, op, **kw):
            if method == "POST" and path.endswith("supporting-info"):
                seen.update(kw)
            return super().call(method, path, op, **kw)

    sc.run_case(Spy(), sc.State(tmp_path / "shape.json"), CARD, 2, "1330", sc.case_fingerprints(CARD, 2, "1330"),
                emit=lambda *_: None, sleep=lambda *_: None)
    assert seen["multipart"] == part and "json_body" not in seen


def test_the_supporting_info_is_sent_only_while_the_dispute_is_under_review(tmp_path):
    class Opening(step.StandIn):
        def call(self, method, path, op, **kw):
            body = super().call(method, path, op, **kw)
            if method == "GET" and path.startswith(sc.DISPUTE_PATH) and body.get("status") == "UNDER_REVIEW" and self.dispute_reads < 3:
                body = dict(body, status="OPEN")          # the link is listed but the case is still OPEN
            return body

    client = Opening(link_after=0)
    sc.run_case(client, sc.State(tmp_path / "ur.json"), CARD, 2, "1330", sc.case_fingerprints(CARD, 2, "1330"),
                emit=lambda *_: None, sleep=lambda *_: None)
    posts = [i for i, (m, p) in enumerate(client.calls) if m == "POST" and p.endswith("supporting-info")]
    reads_before = [1 for m, p in client.calls[:posts[0]] if m == "GET" and p.startswith(sc.DISPUTE_PATH)]
    assert len(reads_before) >= 3


def test_after_a_timeout_the_case_says_it_is_still_preparing_and_a_rerun_makes_zero_posts(tmp_path):
    state = sc.State(tmp_path / "t.json")
    slow = step.StandIn(wait_reads=999)
    first = sc.run_case(slow, state, CARD, 2, "1310", sc.case_fingerprints(CARD, 2, "1310"), emit=lambda *_: None,
                        sleep=lambda *_: None, polls=3)
    assert first["ready"] is False and first["preparing"] is True
    posts_first = [p for m, p in slow.calls if m == "POST"]
    assert len(posts_first) == 3                       # order, chargeback, ONE supporting information
    again = step.StandIn(wait_reads=0)
    again.supported = True                             # PayPal already has the supporting information
    again.reads = 5
    second = sc.run_case(again, sc.State(tmp_path / "t.json"), CARD, 2, "1310", sc.case_fingerprints(CARD, 2, "1310"),
                         emit=lambda *_: None, sleep=lambda *_: None, polls=3)
    assert [p for m, p in again.calls if m == "POST"] == []        # the re-run keeps waiting and writes nothing
    assert second["ready"] is True


def test_the_cli_exits_with_a_distinct_code_while_the_case_is_still_preparing(tmp_path, monkeypatch, capsys):
    assert sc.EXIT_READY == 0 and sc.EXIT_PREPARING == 3 and sc.EXIT_PREPARING not in (sc.EXIT_READY, sc.EXIT_REFUSED)
    assert "run the same command again" in sc.PREPARING_LINE and "no new writes" in sc.PREPARING_LINE
    assert sc.POLLS == 36


def test_supporting_info_is_never_sent_twice(tmp_path):
    state = sc.State(tmp_path / "again.json")
    client = step.StandIn()
    sc.run_case(client, state, CARD, 2, "1330", sc.case_fingerprints(CARD, 2, "1330"), emit=lambda *_: None, sleep=lambda *_: None)
    assert state.data["support_posted"] is True
    client2 = step.StandIn()
    client2.supported = True                      # a second run with the flag already in the state must not post again
    state2 = sc.State(tmp_path / "again.json")
    sc.run_case(client2, state2, CARD, 2, "1330", sc.case_fingerprints(CARD, 2, "1330"), emit=lambda *_: None, sleep=lambda *_: None)
    assert [p for m, p in client2.calls if m == "POST"] == []      # no second order, chargeback or supporting information


def test_the_case_sends_nothing_without_all_three_exact_approvals(tmp_path):
    client = step.StandIn()
    good = sc.case_fingerprints(CARD, 2, "1330")
    for bad in ((good[0], good[1], "0" * 16), ("0" * 16, good[1], good[2]), (good[0], sc.case_fingerprints(CARD, 2, "1310")[1], good[2])):
        with pytest.raises(sc.Stop, match="approvals do not match"):
            sc.run_case(client, sc.State(tmp_path / "x.json"), CARD, 2, "1330", bad, emit=lambda *_: None, sleep=lambda *_: None)
    assert client.calls == []


def test_the_case_stops_on_a_paypal_refusal_and_the_dispute_read_is_bounded(tmp_path):
    class Refusing(step.StandIn):
        def call(self, method, path, op, **kw):
            if method == "POST" and path == sc.CHARGEBACK_PATH:
                raise RuntimeError("refused")
            return super().call(method, path, op, **kw)

    client = Refusing()
    with pytest.raises(RuntimeError):
        sc.run_case(client, sc.State(tmp_path / "r.json"), CARD, 2, "1330", sc.case_fingerprints(CARD, 2, "1330"),
                    emit=lambda *_: None, sleep=lambda *_: None)
    assert not any(p.endswith("supporting-info") for _, p in client.calls)
    slow = step.StandIn(wait_reads=99)
    result = sc.run_case(slow, sc.State(tmp_path / "s.json"), CARD, 2, "1330", sc.case_fingerprints(CARD, 2, "1330"),
                         emit=lambda *_: None, sleep=lambda *_: None, polls=5)
    assert result["ready"] is False and slow.reads == 5


def test_a_support_link_that_is_not_this_disputes_sandbox_path_is_not_followed():
    assert sc.support_path("https://api-m.sandbox.paypal.com/v1/customer/disputes/PP-D-1/provide-supporting-info", "PP-D-1").endswith("info")
    for bad in ("https://example.com/v1/customer/disputes/PP-D-1/provide-supporting-info",
                "https://api-m.sandbox.paypal.com/v1/customer/disputes/PP-D-2/provide-supporting-info",
                "https://api-m.sandbox.paypal.com/v1/customer/disputes/PP-D-1/accept-claim"):
        with pytest.raises(sc.Stop):
            sc.support_path(bad, "PP-D-1")


def test_the_case_step_prints_the_receipt_and_fails_above_zero_buyer_hands():
    line = step.walk_case()
    assert line.startswith("caso listo para responder; manos del comprador: 0; comandos del juez: 1")
    assert "aprobaciones del vendedor mostradas: 3 de 3" in line
    with pytest.raises(sc.Stop):
        step.walk_case(ask_payer=True)
    with pytest.raises(sc.Stop):
        step.walk_case(offer_support_link=False)


def test_the_case_step_fails_above_one_judge_command():
    assert step.walk_case(polls=36).startswith("caso listo para responder; manos del comprador: 0; comandos del juez: 1")
    with pytest.raises(sc.Stop, match="comandos del juez"):
        step.walk_case(polls=1)                       # PayPal not ready after one pass: the judge would have to type the command again


def test_case_output_holds_no_full_ids(tmp_path):
    lines = []
    sc.run_case(step.StandIn(), sc.State(tmp_path / "m.json"), CARD, 2, "1330", sc.case_fingerprints(CARD, 2, "1330"),
                emit=lines.append, sleep=lambda *_: None)
    text = "\n".join(lines)
    assert "STANDINCARDORDER1" not in text and "STANDINCARDCAP01" not in text and CARD["number"] not in text


def test_the_walk_fails_when_the_order_asks_the_payer():
    with pytest.raises(sc.Stop):
        step.walk(ask_payer=True)


def test_an_order_with_a_payer_action_link_counts_as_a_buyer_hand():
    assert sc.needs_buyer({"status": "CREATED", "links": [{"rel": "payer-action"}]})
    assert sc.needs_buyer({"status": "PAYER_ACTION_REQUIRED"})
    assert not sc.needs_buyer({"status": "COMPLETED", "links": [{"rel": "self"}]})


def test_the_helper_has_no_refund_path_and_prints_no_full_ids(tmp_path):
    for module in (sc, step):
        source = Path(module.__file__).read_text(encoding="utf-8").lower()
        assert "/refund" not in source and "refund(" not in source
    lines = []
    state = sc.State(tmp_path / "s.json")
    sc.run_order(step.StandIn(), state, CARD, 2, sc.fingerprint(sc.variant_body(CARD, 2)), emit=lines.append)
    text = "\n".join(lines)
    assert "STANDINCARDORDER1" not in text and "STANDINCARDCAP01" not in text and CARD["number"] not in text


def test_a_state_path_git_would_track_is_refused():
    root = Path(__file__).resolve().parents[1]
    assert sc._paths(root, None)[0].parts[-3:-1] == ("data", "sandbox-seed")
    with pytest.raises(sc.Stop):
        sc._paths(root, root / "tools" / "b8-state.json")


def test_the_amounts_are_fixed_in_code_as_integer_cents_and_become_strings_by_code():
    for spec in sc.VARIANTS.values():
        assert type(spec["amount_cents"]) is int and spec["amount_cents"] > 0
    for variant in sc.VARIANTS:
        value = sc.variant_body(CARD, variant)["purchase_units"][0]["amount"]["value"]
        assert isinstance(value, str) and value.count(".") == 1
    for bad in (25.5, "2500", True, 0, -5):
        with pytest.raises(ValueError):
            sc.order_body(CARD, bad, "MXN", "x")


def test_the_hosted_app_never_imports_or_calls_the_card_route():
    root = Path(__file__).resolve().parents[1]
    offenders = [str(path.relative_to(root)) for path in (root / "copilot").rglob("*.py")
                 if "sandbox_card" in path.read_text(encoding="utf-8", errors="ignore")]
    assert offenders == []
    assert (root / "render.free.yaml").is_file()   # the public kit ships this one; render.yaml only the private checkout
    for rel in ("render.yaml", "render.free.yaml"):
        if (root / rel).is_file():
            assert "sandbox_card" not in (root / rel).read_text(encoding="utf-8")


def test_the_cli_default_reason_code_is_1310_and_1330_stays_selectable():
    parser = sc.build_parser()
    assert sc.DEFAULT_REASON_CODE == "1310" and parser.parse_args(["plan-case"]).reason_code == "1310"
    assert parser.parse_args(["plan-case", "--reason-code", "1330"]).reason_code == "1330"
    with pytest.raises(SystemExit):
        parser.parse_args(["plan-case", "--reason-code", "9999"])
