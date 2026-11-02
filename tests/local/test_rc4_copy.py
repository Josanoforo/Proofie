"""Display-only copy and newest-package task links; synthetic local data only."""
from types import SimpleNamespace
import pytest
from _dom import parse
from copilot import config
from copilot.db import repo
from copilot.web import routes_disputes, routes_package, presentation


def test_newest_package_tasks_use_new_approved_version(monkeypatch):
    db = config.db_path(); did = "PP-R-RC4-DEMO"
    first = repo.insert_package(db, did, "human_edit", True, [])
    newest = repo.insert_package(db, did, "human_edit", True, [])
    repo.insert_claims(db, newest, [{"evidence_index": 0, "claim_index": 0, "text": "Shipped.", "source_ids": [], "basis": "seller_statement"}])
    repo.insert_approval(db, newest, "a" * 64, "synthetic seller")
    monkeypatch.setattr(routes_package, "_send_summary", lambda *a: {"approval_stale": False, "current_confirmation": None, "tracking_number": "DEMO1"})
    packages = repo.list_packages(db, did)
    assert [p["id"] for p in packages] == [first, newest]
    for ordered in (packages, list(reversed(packages))):
        rows = routes_disputes._case_tasks(db, {"dispute_id":did,"can_provide_evidence":True}, [], ordered, [])
        assert [row["key"] for row in rows] == ["upload", "prepare", "approve", "send"]       # Four guided steps
        linked = rows[1:]
        assert all(f"/packages/{newest}/" in row["href"] or f"/packages/{newest}#" in row["href"] or row["href"].endswith(f"/packages/{newest}") for row in linked)
        assert linked[1]["label"].startswith("Done: package v2 approved")


def test_package_not_found_has_one_title_and_one_back_link(web_mode, monkeypatch):
    from test_routes_package import _client, DID
    web, _, _ = _client(monkeypatch=monkeypatch)
    result = web.get(f"/disputes/{DID}/packages/999999")
    assert result.status_code == 404
    dom = parse(result.text)
    assert dom.find("h1").text() == "This package could not be found"
    assert len([a for a in dom.find_all("a") if a.attrs.get("href") == f"/disputes/{DID}"]) == 1


def test_generate_wait_hint_matches_actual_mode(web_mode, monkeypatch):
    from test_routes_package import _client, DID
    web, _, _ = _client(monkeypatch=monkeypatch)
    repo.insert_document(config.db_path(), DID, "a.pdf", "application/pdf", 10, "sha-a", "Carrier: FedEx", True)   # documents, no package
    page = parse(web.get(f"/disputes/{DID}").text)
    hint = page.find("p", data_state="generate-wait").text()
    if config.hosted_demo():
        assert hint == "Preparing the package. Keep this page open."
        assert "assistant" not in hint
    else:
        assert hint == "Preparing with the assistant. This can take a few minutes. Keep this page open."


@pytest.mark.parametrize("template", ["features/claims/review.html", "features/active_approval/review.html"])
def test_seller_statement_basis_does_not_add_unavailable(template):
    # The approval page uses the claims page's wording for a seller statement
    expected_basis = "Your own statement. No document is cited."
    claim = SimpleNamespace(text="Synthetic seller statement.", verdict="seller_statement", claim_basis="seller_statement",
                            bases=["none"], sources=[], key="demo", id=1)
    env = routes_package.templates.env
    context = dict(dispute_id="PP-R-RC4-DEMO", package_id=1, claims=[claim], safe_text=lambda value:value,
                   document_names={}, review=SimpleNamespace(claims=[claim],approval=None,request_input=None,blocked=[],cleared=False,review_sha256="a"*64,
                                              package={"id": 1, "version": 1}),
                   decisions={}, confirmed_count=0, csrf_token="synthetic",state="pending", package_version=1,
                       send_record=SimpleNamespace(state="none"))      # O4: the approval page states whether anything was sent
    output = env.get_template(template).render(context)
    basis = parse(output).find("p", class_="claim-basis").text()
    assert basis == expected_basis
    assert 'data-basis="none"' in output


@pytest.mark.parametrize("kind,label", [
    ("attach_document","Attached a document to the package"),
    ("start_manual_package","Started a package by hand"),
    ("auto_prep_context_fetched","Read the case details for draft preparation"),
    ("auto_prep_crashed","Draft preparation stopped unexpectedly"),
    ("fulfilment_registered","Recorded the tracking registration result"),
    ("abandoned_by_vendor","Marked an attempt as not received"),
    ("send_attempt_superseded","Replaced an earlier send reservation"),
    ("resolution_offer","Recorded a refund offer attempt"),
    ("resolution_message","Recorded a message attempt"),
])
def test_timeline_has_specific_plain_labels(kind,label):
    assert presentation.action_label(kind) == label
