import hashlib


def test_local_contract_idempotence_human_guard_and_failures(monkeypatch,tmp_path):
    from copilot import config
    from copilot.core import auto_prep
    # Ola4 D added auto_prep.prepare_dispute_local after this test was written (without it the default runner prepares a manual shell);
    # the "unavailable" branch is the missing-seam branch, so the seam is taken away here
    monkeypatch.setattr(auto_prep, "prepare_dispute_local", None, raising=False)
    from copilot.db import repo
    from copilot.core.raw_storage import write_raw
    from copilot.features.local_autopilot.observations import ObservationStore
    from copilot.features.local_autopilot.preparation import prepare_known_case
    monkeypatch.setattr(config,"_db_path_override",tmp_path / "db.sqlite")
    monkeypatch.setattr(config,"_uploads_dir_override",tmp_path / "uploads")
    db=config.db_path(); did="PP-R-DEMO-0000001"; norm={"dispute_id":did,"status":"WAITING_FOR_SELLER_RESPONSE","reason":"MERCHANDISE_OR_SERVICE_NOT_RECEIVED","update_time":"2026-10-31T12:00:00Z"}
    store=ObservationStore()
    def observed():
        token=store.issue(db,[{"norm":norm}],observed_at="2026-10-31T12:00:00+00:00",provenance="synthetic_fixture")
        return store.get(token,did)
    empty=observed(); assert prepare_known_case(db,empty)["state"] == "needs_input"
    data=b"case record"; sha=hashlib.sha256(data).hexdigest()
    repo.insert_document(db,did,"record.txt","text/plain",len(data),sha,"case record",True)
    write_raw(config.uploads_dir(),sha,data)
    assert prepare_known_case(db,empty)["state"] == "stale"
    ob=observed(); assert prepare_known_case(db,ob)["state"] == "unavailable"
    assert repo.get_auto_prep(db,did) is None
    calls=[]
    def fake_local(db_path,case_norm,version,claim_token):
        calls.append(case_norm["dispute_id"])
        pkg=repo.insert_package(db_path,did,"auto_manual",False,[])
        assert repo.set_auto_prep_result(db_path,did,version,claim_token,"ready",package_id=pkg)
    first=prepare_known_case(db,ob,runner=fake_local)
    second=prepare_known_case(db,ob,runner=fake_local)
    assert first["state"] == second["state"] == "ready" and first["package_id"] == second["package_id"] and calls == [did]
    assert "manual shell" in first["reasons"][0]
    human=repo.insert_package(db,did,"human_edit",True,[])
    repo.insert_claims(db,human,[{"evidence_index":0,"claim_index":0,"text":"Human text","source_ids":[],"basis":"seller_statement"}])
    assert prepare_known_case(db,ob,runner=fake_local)["state"] == "stale"
    current=prepare_known_case(db,observed(),runner=fake_local)
    assert current["state"] == "human_review" and current["package_id"] == human and repo.list_claims(db,human)[0]["text"] == "Human text"
    assert len(repo.list_packages(db,did)) == 2
    foreign={**observed(),"case_id":"PP-R-DEMO-0000002"}
    assert prepare_known_case(db,foreign,runner=fake_local)["state"] == "unavailable"


def test_failure_and_in_progress_are_safe(monkeypatch,tmp_path):
    from copilot import config
    from copilot.db import repo
    from copilot.core.raw_storage import write_raw
    from copilot.features.local_autopilot.observations import ObservationStore
    from copilot.features.local_autopilot.preparation import prepare_known_case
    monkeypatch.setattr(config,"_db_path_override",tmp_path / "db.sqlite")
    monkeypatch.setattr(config,"_uploads_dir_override",tmp_path / "uploads")
    db=config.db_path(); did="PP-R-DEMO-0000001"; norm={"dispute_id":did,"status":"WAITING_FOR_SELLER_RESPONSE","reason":"MERCHANDISE_OR_SERVICE_NOT_RECEIVED"}
    data=b"record"; repo.insert_document(db,did,"record.txt","text/plain",len(data),hashlib.sha256(data).hexdigest(),"record",True)
    write_raw(config.uploads_dir(),hashlib.sha256(data).hexdigest(),data)
    store=ObservationStore(); token=store.issue(db,[{"norm":norm}],observed_at="2026-10-31T12:00:00+00:00",provenance="synthetic_fixture"); ob=store.get(token,did)
    def fail(*args): raise RuntimeError("synthetic failure")
    assert prepare_known_case(db,ob,runner=fail)["state"] == "unavailable"
    assert repo.get_auto_prep(db,did)["status"] == "failed"
    assert prepare_known_case(db,ob,runner=fail)["state"] == "cooldown"


def test_excluded_source_and_existing_claim(monkeypatch,tmp_path):
    from copilot import config
    from copilot.db import repo
    from copilot.core.raw_storage import write_raw
    from copilot.features.local_autopilot.observations import ObservationStore
    from copilot.features.local_autopilot.preparation import prepare_known_case
    monkeypatch.setattr(config,"_db_path_override",tmp_path / "db.sqlite")
    monkeypatch.setattr(config,"_uploads_dir_override",tmp_path / "uploads")
    db=config.db_path(); did="PP-R-DEMO-0000001"; norm={"dispute_id":did,"status":"WAITING_FOR_SELLER_RESPONSE","reason":"MERCHANDISE_OR_SERVICE_NOT_RECEIVED"}
    data=b"record"; doc=repo.insert_document(db,did,"record.txt","text/plain",len(data),hashlib.sha256(data).hexdigest(),"record",True)
    repo.set_document_excluded(db,doc,True)
    store=ObservationStore()
    def observe():
        token=store.issue(db,[{"norm":norm}],observed_at="2026-10-31T12:00:00+00:00",provenance="synthetic_fixture")
        return store.get(token,did)
    assert prepare_known_case(db,observe())["state"] == "needs_input"
    repo.set_document_excluded(db,doc,False)
    write_raw(config.uploads_dir(),hashlib.sha256(data).hexdigest(),data)
    ob=observe(); assert repo.claim_auto_prep(db,did,ob["version"])
    def forbidden(*args): raise AssertionError("duplicate worker")
    assert prepare_known_case(db,ob,runner=forbidden)["state"] == "in_progress"


def test_changed_original_invalidates_observation(monkeypatch,tmp_path):
    from copilot import config
    from copilot.db import repo
    from copilot.core.raw_storage import write_raw
    from copilot.features.local_autopilot.observations import ObservationStore
    from copilot.features.local_autopilot.preparation import prepare_known_case
    monkeypatch.setattr(config,"_db_path_override",tmp_path / "db.sqlite")
    monkeypatch.setattr(config,"_uploads_dir_override",tmp_path / "uploads")
    db=config.db_path(); did="PP-R-DEMO-0000001"; norm={"dispute_id":did,"status":"WAITING_FOR_SELLER_RESPONSE","reason":"MERCHANDISE_OR_SERVICE_NOT_RECEIVED"}
    data=b"original"; sha=hashlib.sha256(data).hexdigest()
    repo.insert_document(db,did,"record.txt","text/plain",len(data),sha,"original",True); write_raw(config.uploads_dir(),sha,data)
    store=ObservationStore(); token=store.issue(db,[{"norm":norm}],observed_at="2026-10-31T12:00:00+00:00",provenance="synthetic_fixture"); ob=store.get(token,did)
    (config.uploads_dir() / sha).write_bytes(b"changed")
    assert prepare_known_case(db,ob)["state"] == "stale"
    assert repo.get_auto_prep(db,did) is None
    token=store.issue(db,[{"norm":norm}],observed_at="2026-10-31T12:01:00+00:00",provenance="synthetic_fixture")
    assert prepare_known_case(db,store.get(token,did))["state"] == "needs_input"


def test_human_edit_during_local_work_preserves_package_and_returns_human_link(monkeypatch, tmp_path):
    from copilot import config
    from copilot.db import repo
    from copilot.core.raw_storage import write_raw
    from copilot.features.local_autopilot.observations import ObservationStore
    from copilot.features.local_autopilot.preparation import prepare_known_case
    monkeypatch.setattr(config, "_db_path_override", tmp_path / "db.sqlite")
    monkeypatch.setattr(config, "_uploads_dir_override", tmp_path / "uploads")
    db = config.db_path(); did = "PP-R-DEMO-0000001"
    norm = {"dispute_id": did, "status": "WAITING_FOR_SELLER_RESPONSE", "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"}
    raw = b"synthetic case original"; sha = hashlib.sha256(raw).hexdigest()
    doc = repo.insert_document(db, did, "source.pdf", "application/pdf", len(raw), sha, "source", True)
    write_raw(config.uploads_dir(), sha, raw)
    store = ObservationStore()
    token = store.issue(db, [{"norm": norm}], observed_at="2026-10-31T12:00:00+00:00", provenance="synthetic_fixture")
    observation = store.get(token, did)
    recorded = {}
    def fake_runner(db_path, case_norm, version, claim_token):
        shell = repo.insert_package(db_path, did, "auto_manual", False, [])
        human = repo.insert_package(db_path, did, "human_edit", True, [])
        repo.insert_claims(db_path, human, [{"evidence_index": 0, "claim_index": 0, "text": "Original seller draft", "source_ids": [f"doc_{doc}"], "basis": "seller_statement"}])
        claim = repo.list_claims(db_path, human)[0]
        repo.mark_claim_edited(db_path, claim["id"], "Seller correction during preparation")
        repo.attach_document(db_path, human, doc)
        approval = repo.insert_approval(db_path, human, "synthetic-approval", "vendor")
        assert repo.set_auto_prep_result(db_path, did, version, claim_token, "ready", package_id=shell)
        recorded.update(shell=shell, human=human, approval=approval)
    result = prepare_known_case(db, observation, runner=fake_runner)
    assert result["state"] == "human_review" and result["package_id"] == recorded["human"]
    assert result["human_href"] == f"/disputes/{did}/packages/{recorded['human']}"
    assert result["package_id"] != recorded["shell"]
    assert repo.list_claims(db, recorded["human"])[0]["text"] == "Seller correction during preparation"
    assert repo.list_attachments(db, recorded["human"])[0]["document_id"] == doc
    approvals = repo.list_approvals(db, recorded["human"])
    assert approvals[0]["id"] == recorded["approval"] and approvals[0]["invalidated_at"] is None
    assert len(repo.list_packages(db, did)) == 2  # The contract makes no zero-orphan guarantee.
