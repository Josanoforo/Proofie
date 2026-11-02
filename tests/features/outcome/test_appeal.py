"""register_appeal on the fake PayPal (fixtures only: the function is never called for real)."""
import json

import httpx
import pytest

from copilot.features.outcome import appeal
from outcome_fakes import BASE, DISPUTE_ID, FakePayPal, dispute_raw, paypal_error

CLAIMS = [{"text": "Shipped on 2026-09-30 with FedEx"}, {"text": "Delivered 2026-10-02"}]
BODY = appeal.appeal_body(CLAIMS, "DEMO-FDX-000001", "FedEx")
ATTACH = [{"filename": "estafeta_demo_letter.pdf", "content_type": "application/pdf", "data": b"%PDF-1.4 demo"}]
SHA = appeal.approval_sha256(DISPUTE_ID, BODY, appeal.attachment_sha256s(ATTACH))
DECIDED_WITH_APPEAL = dict(status="RESOLVED", rels=("appeal",),
                           outcome={"outcome_code": "RESOLVED_BUYER_FAVOUR"})


def _fake(**kw):
    return FakePayPal(dispute_raw(**DECIDED_WITH_APPEAL), **kw)


def test_the_body_is_the_provide_evidence_shape_built_by_the_send_path_code():
    assert list(BODY) == ["evidences"] and BODY["evidences"][0]["evidence_type"] == "PROOF_OF_FULFILLMENT"
    tracking = BODY["evidences"][0]["evidence_info"]["tracking_info"][0]
    assert tracking == {"carrier_name": "FEDEX", "tracking_number": "DEMO-FDX-000001"}
    assert "Shipped on 2026-09-30" in BODY["evidences"][0]["notes"]
    with pytest.raises(ValueError):
        appeal.appeal_body(CLAIMS, None, "FedEx")


def test_the_body_hash_is_the_send_canon_and_stable():
    import hashlib
    canon = json.dumps(BODY, sort_keys=True, ensure_ascii=False).encode("utf-8")
    assert appeal.request_sha256(BODY) == hashlib.sha256(canon).hexdigest()
    assert SHA != appeal.request_sha256(BODY)                      # the approval also covers the attachment


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_with_zero_attachments_the_fingerprint_is_the_plain_send_path_fingerprint():
    from copilot.core.send import sha256_json
    plain = sha256_json({"dispute_id": DISPUTE_ID, "action": "appeal", "paypal_request_input": BODY})
    assert appeal.approval_sha256(DISPUTE_ID, BODY, []) == plain
    fake = _fake()
    assert appeal.register_appeal(fake, DISPUTE_ID, BODY, [], plain).request_sha256 == appeal.request_sha256(BODY)
    assert len(fake.posts()) == 1


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_one_attachment_changed_between_preview_and_register_is_refused_with_no_call():
    fake = _fake()
    changed = [{**ATTACH[0], "data": b"%PDF-1.4 demo EDITED"}]
    with pytest.raises(appeal.ApprovalMismatch):
        appeal.register_appeal(fake, DISPUTE_ID, BODY, changed, SHA)
    assert fake.calls == []


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_an_added_or_removed_attachment_is_refused_with_no_call():
    fake = _fake()
    extra = ATTACH + [{"filename": "b.png", "content_type": "image/png", "data": b"png"}]
    for attachments in (extra, []):
        with pytest.raises(appeal.ApprovalMismatch):
            appeal.register_appeal(fake, DISPUTE_ID, BODY, attachments, SHA)
    assert fake.calls == []


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_the_same_attachments_in_a_different_order_are_accepted():
    two = [{"filename": "a.pdf", "content_type": "application/pdf", "data": b"first"},
           {"filename": "b.png", "content_type": "image/png", "data": b"second"}]
    sha = appeal.approval_sha256(DISPUTE_ID, BODY, appeal.attachment_sha256s(two))
    fake = _fake()
    assert appeal.register_appeal(fake, DISPUTE_ID, BODY, list(reversed(two)), sha).status in ("sent_confirmed", "sent_unconfirmed")
    assert len(fake.posts()) == 1


def test_an_attachment_carrying_only_its_sha256_is_accepted_and_bytes_win_over_a_label():
    import hashlib
    digest = hashlib.sha256(b"x").hexdigest()
    assert appeal.attachment_sha256s([{"sha256": digest}]) == [digest]
    assert appeal.attachment_sha256s([{"sha256": "f" * 64, "data": b"x"}]) == [digest]


def test_the_fingerprint_also_depends_on_the_dispute_and_the_body():
    other_body = appeal.appeal_body(CLAIMS, "OTHER-NUMBER", "FedEx")
    assert appeal.approval_sha256(DISPUTE_ID, other_body, appeal.attachment_sha256s(ATTACH)) != SHA
    assert appeal.approval_sha256("PP-R-TST-0000009", BODY, appeal.attachment_sha256s(ATTACH)) != SHA


@pytest.mark.parametrize("wrong", ["", "0" * 64, SHA.upper()])
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_a_wrong_hash_raises_before_any_client_call(wrong):
    fake = _fake()
    with pytest.raises(appeal.ApprovalMismatch):
        appeal.register_appeal(fake, DISPUTE_ID, BODY, ATTACH, wrong)
    assert fake.calls == []


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_without_the_appeal_link_it_is_rejected_after_a_fresh_get_and_nothing_is_posted():
    fake = FakePayPal(dispute_raw(status="RESOLVED", rels=("send_message",)))
    r = appeal.register_appeal(fake, DISPUTE_ID, BODY, ATTACH, SHA)
    assert r.status == "rejected_link" and not fake.posts() and "does not offer an appeal" in r.reason
    assert [c[0] for c in fake.calls] == ["GET"]


@pytest.mark.parametrize("href", ["https://evil.example/v1/customer/disputes/PP-R-TST-0000002/appeal",
                                  "https://api-m.sandbox.paypal.com/v1/customer/disputes/PP-R-TST-0000002/accept-claim",
                                  "http://api-m.sandbox.paypal.com/v1/customer/disputes/PP-R-TST-0000002/appeal",
                                  "https://api-m.sandbox.paypal.com/v1/customer/disputes/PP-R-OTHER-0000001/appeal"])
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_a_link_off_the_allowlist_is_rejected(href):
    raw = dispute_raw(**DECIDED_WITH_APPEAL)
    next(link for link in raw["links"] if link["rel"] == "appeal")["href"] = href
    fake = FakePayPal(raw)
    assert appeal.register_appeal(fake, DISPUTE_ID, BODY, ATTACH, SHA).status == "rejected_link" and not fake.posts()


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_a_non_post_link_is_rejected():
    raw = dispute_raw(**DECIDED_WITH_APPEAL)
    next(link for link in raw["links"] if link["rel"] == "appeal")["method"] = "GET"
    fake = FakePayPal(raw)
    assert appeal.register_appeal(fake, DISPUTE_ID, BODY, ATTACH, SHA).status == "rejected_link" and not fake.posts()


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_happy_path_one_post_to_the_links_path_one_get_and_a_confirmed_change():
    after = dispute_raw(status="WAITING_FOR_SELLER_RESPONSE", stage="ARBITRATION", rels=(),
                        outcome={"outcome_code": "RESOLVED_BUYER_FAVOUR"})
    fake = _fake(after=after)
    r = appeal.register_appeal(fake, DISPUTE_ID, BODY, ATTACH, SHA)
    assert r.status == "sent_confirmed" and r.request_sha256 == appeal.request_sha256(BODY) and r.limitations == ()
    assert [(c[0], c[1], c[2]) for c in fake.calls] == [
        ("GET", BASE, "outcome_appeal_read"), ("POST", f"{BASE}/appeal", "outcome_appeal"),
        ("GET", BASE, "outcome_appeal_readback")]
    kw = fake.posts()[0][3]
    assert [p[0] for p in kw["multipart"]] == ["input", "evidence-file"] and kw["request_id"]
    assert json.loads(kw["multipart"][0][1][1]) == BODY
    assert r.readback == {"stage": "ARBITRATION", "status": "WAITING_FOR_SELLER_RESPONSE", "links": [],
                          "outcome": "RESOLVED_BUYER_FAVOUR"}


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_the_same_body_gets_the_same_request_id(monkeypatch, tmp_path):
    from copilot import config
    a, b = _fake(), _fake()
    appeal.register_appeal(a, DISPUTE_ID, BODY, ATTACH, SHA)
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "second-copy.db")    # a fresh send-once table, as on another host
    appeal.register_appeal(b, DISPUTE_ID, BODY, ATTACH, SHA)
    assert a.posts()[0][3]["request_id"] == b.posts()[0][3]["request_id"]


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_an_unchanged_readback_is_sent_unconfirmed_not_confirmed():
    r = appeal.register_appeal(_fake(), DISPUTE_ID, BODY, ATTACH, SHA)
    assert r.status == "sent_unconfirmed" and "refresh" in r.reason


@pytest.mark.parametrize("status", [400, 401, 403, 422, 500])
@pytest.mark.usefixtures("legacy_approval_scenario")
def test_a_paypal_4xx_or_5xx_is_failed_and_the_post_is_not_repeated(status):
    fake = _fake(post_raises=paypal_error(status))
    r = appeal.register_appeal(fake, DISPUTE_ID, BODY, ATTACH, SHA)
    assert (r.status, r.http_status) == ("failed", status) and len(fake.posts()) == 1
    assert "dbg-1" not in r.limitations[0].message
    assert [c[2] for c in fake.calls].count("outcome_appeal_readback") == 0


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_a_timeout_on_the_post_is_sent_unconfirmed():
    r = appeal.register_appeal(_fake(post_raises=httpx.ReadTimeout("slow")), DISPUTE_ID, BODY, ATTACH, SHA)
    assert r.status == "sent_unconfirmed" and r.http_status is None


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_a_failing_first_read_sends_nothing():
    fake = _fake(get_raises=paypal_error(500))
    r = appeal.register_appeal(fake, DISPUTE_ID, BODY, ATTACH, SHA)
    assert r.status == "failed" and not fake.posts() and r.limitations


@pytest.mark.usefixtures("legacy_approval_scenario")
def test_a_programming_error_still_raises():
    with pytest.raises(KeyError):
        appeal.register_appeal(_fake(post_raises=KeyError("bug")), DISPUTE_ID, BODY, ATTACH, SHA)


def test_domino_appeal_uses_the_same_dispute_mapping_as_provide_evidence():
    from copilot.core.send import build_request_input
    claims = [{"text": "DEMO shipped"}]
    body = appeal.appeal_body(claims, "TN1", "DOMINO")
    assert body == build_request_input(claims, "TN1", "DOMINO")
    assert body["evidences"][0]["evidence_info"]["tracking_info"][0] == {
        "carrier_name": "OTHER", "tracking_number": "TN1", "carrier_name_other": "Domino",
    }
