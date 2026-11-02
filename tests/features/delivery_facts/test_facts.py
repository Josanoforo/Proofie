from email.headerregistry import Address
from email.utils import parseaddr
"""Synthetic real-route and storage boundaries, independent bilingual fact assertions."""
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from copilot import config
from copilot.app import create_app
from copilot.features.delivery_facts import service
from _hosted import authenticated_client

CAP="DEMO1234567890123"
OTHER="OTHER123456789012"
NUMBER="DEMO1234567890"
DDL="""CREATE TABLE fulfilment_files(id INTEGER PRIMARY KEY,capture_id TEXT,created_at TEXT,request_json TEXT,
request_sha256 TEXT,approval_sha256 TEXT,tracker_readback_json TEXT,checks_json TEXT,file_sha256 TEXT,status TEXT)"""

def digest(obj):
    return hashlib.sha256(json.dumps(obj,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def store(db,request=None,readback=...,checks=None,capture=CAP,status="registered",saved="2026-10-08T12:00:00+0000"):
    request=request if request is not None else {"trackers":[{"transaction_id":capture,"tracking_number":NUMBER,"carrier":"FEDEX","status":"SHIPPED"}]}
    readback=readback if readback is not ... else {"tracking_number":NUMBER,"carrier":"FEDEX","status":"SHIPPED","shipment_date":"2026-10-07","last_updated_time":1791299995286}
    checks=checks if checks is not None else {"address_comparison":{"result":"match"}}
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO fulfilment_files VALUES(?,?,?,?,?,?,?,?,?,?)",(None,capture,saved,json.dumps(request),digest(request),digest(request),json.dumps(readback),json.dumps(checks),digest({"request":request,"tracker_readback":readback,"checks":checks}),status))

def files(root):
    return {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}

def url(capture=CAP):
    return "/delivery-facts/"+capture


@pytest.mark.parametrize("country", ["MX", "US"])
def test_dr1b_real_rules_saved_through_fulfilment_render_facts(web_mode, monkeypatch, country):
    """Use the real store route and policy metadata, not the old minimal checks fixture."""
    from copilot.core.checklist import load_rules
    from copilot.features.fulfilment import checks as fulfilment_checks, store as real_store
    from copilot.features.fulfilment.tracker_request import body_from_form, request_sha256
    from copilot.web.routes_disputes import get_client
    from _hosted import set_test_csrf
    for name, enabled in {"fulfilment": True, "fulfilment_register": False, "delivery_facts": True}.items():
        monkeypatch.setenv("COPILOT_FEATURE_" + name.upper(), str(enabled).lower())
    rules = load_rules(country=country)
    monkeypatch.setattr(fulfilment_checks, "cached_rules", lambda: rules)
    class Fake:
        def __init__(self): self.calls = []
        def call(self, method, path, op, **kwargs):
            self.calls.append((method, path))
            assert method == "GET"
            if "/transactions" in path:
                return {"transaction_details": [{"transaction_info": {"transaction_id": CAP,
                    "transaction_initiation_date": "2026-10-01T10:00:00+0000",
                    "transaction_amount": {"currency_code": "USD", "value": "40.00"}}}]}
            if "/trackers" in path: return {"trackers": []}
            raise AssertionError(path)
    fake = Fake(); app = create_app(); app.dependency_overrides[get_client] = lambda: fake
    from copilot.web.security import new_csrf_token
    web = authenticated_client(app); token = new_csrf_token()
    set_test_csrf(web, token)
    body = body_from_form(CAP, NUMBER, "FEDEX", "SHIPPED")
    # Preview stores the check and answers 303; the confirm form carries only the check token and sha256
    prepared = web.post(f"/fulfilment/{CAP}/preview", data={"csrf_token": token, "tracking_number": NUMBER,
                                                           "carrier": "FEDEX", "status": "SHIPPED"},
                        follow_redirects=False)
    assert prepared.status_code == 303, prepared.text
    check_token = prepared.headers["location"].split("/check/")[1].split("#")[0]
    assert web.get(prepared.headers["location"]).status_code == 200
    set_test_csrf(web, token)
    response = web.post(f"/fulfilment/{CAP}/register", data={"csrf_token": token, "token": check_token,
                                                            "request_sha256": request_sha256(body)})
    assert response.status_code == 200, response.text
    saved = real_store.get_file(CAP)
    assert json.loads(saved["checks_json"])["rules"]["source_url"] == rules.source_url
    response = web.get(url())
    assert response.status_code == 200, response.text
    assert "Saved copy for transaction" in response.text
    assert service.read(config.db_path(), CAP)["record_status"] == "saved_without_registration"
    assert fake.calls and all(method == "GET" for method, _ in fake.calls)


def test_dr1b_string_epoch_milliseconds_are_not_a_typed_date():
    assert service._updated("1791299995286") == service._updated(1791299995286)


@pytest.mark.parametrize("value", ["Z:/synthetic-profile/private.txt", r"C:\synthetic-profile\private.txt"])
def test_dr1b_drive_paths_remain_private(value):
    with pytest.raises(service.FactsError):
        service._safe_tree({"extra": value})

@pytest.fixture
def prepared(web_mode,monkeypatch):
    for key in list(os.environ):
        if key.startswith("COPILOT_FEATURE_"):monkeypatch.delenv(key)
    monkeypatch.setenv("COPILOT_FEATURE_DELIVERY_FACTS","true")
    db=config.db_path()
    with sqlite3.connect(db) as c:c.execute(DDL)
    store(db)
    app=create_app()
    from copilot.web.routes_disputes import get_client
    def no_client():raise AssertionError("Q4 must never obtain a client")
    app.dependency_overrides[get_client]=no_client
    return authenticated_client(app),db

def test_usable_bilingual_facts_no_writes_client_or_overclaim(prepared):
    client,db=prepared;before=files(db.parent)
    response=client.get(url());assert response.status_code==200,response.text
    result=service.read(db,CAP)
    # Display words come from fixed tables, never raw codes such as FEDEX or SHIPPED
    for text,status_word,day in ((result["english"],"shipped","7 Oct 2026"),(result["spanish"],"enviado","7 oct 2026")):
        assert NUMBER in text and "FedEx" in text and status_word in text and day in text and "2026-10-07" not in text
        assert "FEDEX" not in text and "SHIPPED" not in text and "Stored tracker readback" not in text
        assert "http" not in text and len(text)<=600
    assert service.DIGIT_RUN.findall(result["english"])==service.DIGIT_RUN.findall(result["spanish"])
    assert "Review before copying" in response.text and "Nothing has been sent and this page cannot send anything." in response.text
    assert "does not by itself prove the package arrived." in response.text and "shows the copy was not altered; it does not show the content is true" in response.text
    assert "the day this copy was saved, not a delivery date" in response.text
    assert "last_updated" not in result["english"]
    assert "197" not in result["english"]
    assert response.headers["cache-control"]=="no-store"
    assert files(db.parent)==before
    assert client.post(url()).status_code==405

def test_latest_scoped_capture_not_other_record(prepared):
    client,db=prepared
    other="OTHER1234567890123"
    store(db,capture=other,readback=None)
    before=files(db.parent)
    assert service.read(db,CAP)["readback"] is not None
    assert client.get(url("MISSING12345678901")).status_code==404
    assert client.get(url("bad")).status_code==404
    assert files(db.parent)==before

@pytest.mark.parametrize("record_status",["registered_unconfirmed","failed","saved_without_registration"])
def test_absent_readback_never_confirmed(prepared,record_status):
    client,db=prepared;store(db,readback=None,status=record_status)
    response=client.get(url());assert response.status_code==200
    result=service.read(db,CAP)
    assert "incomplete" in result["english"] and "incompletos" in result["spanish"]
    assert "neither the tracking nor the delivery can be confirmed" in response.text
    assert result["record_status"]==record_status

@pytest.mark.parametrize("readback",[{}, {"tracking_number":NUMBER}, {"status":"DELIVERED"}])
def test_partial_facts_still_useful_and_missing_explicit(prepared,readback):
    client,db=prepared;store(db,readback=readback)
    response=client.get(url());assert response.status_code==200
    result=service.read(db,CAP)
    assert "incomplete" in result["english"] and "Not recorded" in response.text
    assert "does not by itself prove the parcel was shipped or delivered" in result["english"]

def test_discrepancy_and_partial_delivery_visible_both_languages(prepared):
    client,db=prepared
    store(db,readback={"tracking_number":"OTHER1234567890","carrier":"UPS","status":"PARTIALLY_DELIVERED"},checks={"address_comparison":{"result":"mismatch"}})
    result=service.read(db,CAP);response=client.get(url())
    assert response.status_code==200 and "OTHER1234567890" in response.text
    assert "Records disagree" in result["english"] and "Los registros difieren" in result["spanish"]
    assert "Partial delivery" in result["english"] and "Entrega parcial" in result["spanish"]
    assert "OTHER1234567890" not in result["english"] and "OTHER1234567890" not in result["spanish"]

@pytest.mark.parametrize("state",["DELIVERED","PARTIALLY_DELIVERED","CANCELLED"])
def test_status_conflict_retained_not_replaced(prepared,state):
    client,db=prepared;store(db,readback={"tracking_number":NUMBER,"carrier":"UPS","status":state},checks={"address_comparison":{"result":"mismatch"}})
    result=service.read(db,CAP);response=client.get(url())
    assert response.status_code==200 and "Your entry and PayPal's answer differ on:" in response.text
    assert result["discrepancies"]==["carrier","status"]
    words={"DELIVERED":("delivered","entregado"),"PARTIALLY_DELIVERED":("partially delivered","entregado parcialmente"),
           "CANCELLED":("canceled","cancelado")}[state]
    for text,shipped,answer in ((result["english"],"shipped",words[0]),(result["spanish"],"enviado",words[1])):
        assert shipped in text and answer in text and "FedEx" in text and "UPS" in text
    if state=="PARTIALLY_DELIVERED":
        assert "Partial delivery" in result["english"] and "Entrega parcial" in result["spanish"]
    assert "comparison disagrees" in result["english"]

@pytest.mark.parametrize("field,value",[("tracking_number",{}),("carrier",[]),("status",{}),("shipment_date","2026-02-31"),("last_updated_time",True),("last_updated_time",-1)])
def test_typed_metadata_rejected_without_echo(prepared,field,value):
    client,db=prepared;store(db,readback={field:value})
    before=files(db.parent);response=client.get(url())
    assert response.status_code==400 and files(db.parent)==before

PRIVATE=[str(PurePosixPath("/")/"home"/"synthetic-profile"/"private.txt"),str(PureWindowsPath("Z:/")/"synthetic-profile"/"private.txt"),Address(username="synthetic", domain="example.invalid").addr_spec,"<script>alert(1)</script>"]
@pytest.mark.parametrize("value",PRIVATE)
def test_hostile_selected_fields_never_render(prepared,value):
    if value == PRIVATE[2]:
        # Independent parser/oracle: the typed fixture must remain a valid reserved address.
        display, parsed = parseaddr(value)
        local, separator, domain = parsed.rpartition("@")
        assert not display and parsed == value and parsed
        assert local.isalnum() and separator and domain == "example.invalid"
        with pytest.raises(service.FactsError):
            service._safe_tree(value)
    client,db=prepared;store(db,readback={"tracking_number":value})
    before=files(db.parent);response=client.get(url())
    assert response.status_code==400 and value not in response.text and files(db.parent)==before

@pytest.mark.parametrize("field",["request_sha256","approval_sha256","file_sha256","created_at","status","checks_json","tracker_readback_json","request_json"])
def test_corrupt_record_rejected(prepared,field):
    client,db=prepared
    with sqlite3.connect(db) as c:c.execute("UPDATE fulfilment_files SET "+field+"=?",("malformed",))
    before=files(db.parent);response=client.get(url())
    assert response.status_code==400 and "malformed" not in response.text and files(db.parent)==before

@pytest.mark.parametrize("defect",["foreignrequest","foreignreadback","duplicate","oversize","checkslist"])
def test_scope_json_and_size_boundaries(prepared,defect):
    client,db=prepared
    if defect=="foreignrequest":store(db,request={"trackers":[{"transaction_id":"OTHER1234567890123"}]})
    elif defect=="foreignreadback":store(db,readback={"transaction_id":"OTHER1234567890123"})
    elif defect=="checkslist":store(db,checks=[])
    else:
        with sqlite3.connect(db) as c:c.execute("UPDATE fulfilment_files SET request_json=?",('{"trackers":[],"trackers":[]}' if defect=="duplicate" else 'x'*100001,))
    before=files(db.parent);assert client.get(url()).status_code==400 and files(db.parent)==before

@pytest.mark.parametrize("sidecar",["-journal","-wal","-shm"])
def test_sidecars_rejected_without_cleanup(prepared,sidecar):
    client,db=prepared;Path(str(db)+sidecar).write_bytes(b"synthetic unsupported")
    before=files(db.parent);assert client.get(url()).status_code==400 and files(db.parent)==before

def test_missing_parent_never_created(web_mode,monkeypatch,tmp_path):
    monkeypatch.setenv("COPILOT_FEATURE_DELIVERY_FACTS","true")
    root=tmp_path/"absent";config.set_db_path_override(root/"missing.db")
    client=authenticated_client(create_app())
    assert client.get(url()).status_code==404 and not root.exists()

def test_reparse_storage_rejected(prepared,monkeypatch):
    client,db=prepared;original=Path.lstat
    def linked(path):
        info=original(path)
        return SimpleNamespace(st_mode=info.st_mode,st_file_attributes=0x400) if path==db.absolute() else info
    before=files(db.parent);monkeypatch.setattr(Path,"lstat",linked)
    assert client.get(url()).status_code==400 and files(db.parent)==before

def test_normal_locking_blocks_concurrent_commit(prepared,monkeypatch):
    client,db=prepared;original=sqlite3.connect;attempts=[]
    def connect(*a,**kw):
        conn=original(*a,**kw)
        if kw.get("uri"):
            assert "mode=ro" in a[0] and "immutable" not in a[0] and "nolock" not in a[0]
            def trace(sql):
                if sql.startswith("SELECT id,capture_id"):
                    writer=original(db,timeout=0)
                    try:
                        writer.execute("UPDATE fulfilment_files SET status='failed'");writer.commit()
                    except sqlite3.OperationalError as exc:
                        attempts.append(exc.sqlite_errorcode);writer.rollback()
                    finally:writer.close()
            conn.set_trace_callback(trace)
        return conn
    monkeypatch.setattr(service.sqlite3,"connect",connect)
    before=files(db.parent);assert service.read(db,CAP)["record_status"]=="registered"
    assert attempts==[sqlite3.SQLITE_BUSY] and files(db.parent)==before

@pytest.mark.hosted_spec
def test_hosted_auth_exterior(prepared):
    client,db=prepared;before=files(db.parent)
    naked=TestClient(client.app,base_url="https://localhost",client=("127.0.0.1",12))
    response=naked.get(url(),follow_redirects=False)
    assert response.status_code==303 and response.headers["location"].startswith("/access")
    assert NUMBER not in response.text and files(db.parent)==before

def test_off_flag_no_route(prepared,monkeypatch):
    client,db=prepared;monkeypatch.delenv("COPILOT_FEATURE_DELIVERY_FACTS")
    before=files(db.parent);assert authenticated_client(create_app()).get(url()).status_code==404
    assert files(db.parent)==before

def test_no_sufficient_tracker_facts_is_honest(prepared):
    client,db=prepared
    store(db,request={"trackers":[{"transaction_id":CAP}]},readback=None,checks={})
    response=client.get(url());result=service.read(db,CAP)
    assert response.status_code==200 and "Not recorded" in response.text
    assert "incomplete" in result["english"] and "incompletos" in result["spanish"]
    assert NUMBER not in result["english"]

def test_shipment_dates_disagree_with_bilingual_warning(prepared):
    client,db=prepared
    request={"trackers":[{"transaction_id":CAP,"tracking_number":NUMBER,"carrier":"FEDEX","status":"SHIPPED","shipment_date":"2026-10-06"}]}
    readback={"tracking_number":NUMBER,"carrier":"FEDEX","status":"SHIPPED","shipment_date":"2026-10-07"}
    store(db,request=request,readback=readback)
    before=files(db.parent);result=service.read(db,CAP);response=client.get(url())
    assert result["discrepancies"]==["shipment"]
    assert "Your entry and PayPal's answer differ on: ship date" in response.text
    assert "Records disagree" in result["english"] and "Los registros difieren" in result["spanish"]
    for text,days in ((result["english"],("6 Oct 2026","7 Oct 2026")),(result["spanish"],("6 oct 2026","7 oct 2026"))):
        assert all(day in text for day in days) and "2026-10-0" not in text      # display dates, not ISO
    assert files(db.parent)==before

def test_clean_wal_header_reads_with_sqlite_coordination_without_business_writes(prepared,monkeypatch):
    client,db=prepared
    conn=sqlite3.connect(db)
    assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]=="wal"
    conn.close()
    assert db.read_bytes()[18:20]==bytes((2,2))
    assert not any(Path(str(db)+suffix).exists() for suffix in ("-wal","-shm","-journal"))
    with sqlite3.connect(db) as reader:
        before = tuple(reader.iterdump())
    response=client.get(url())
    assert response.status_code==200 and NUMBER in response.text
    with sqlite3.connect(db) as reader:
        assert tuple(reader.iterdump()) == before

def test_normal_app_wal_fulfilment_record_is_readable_without_business_writes(web_mode, monkeypatch):
    from copilot.db import repo
    from copilot.features.fulfilment import store as fulfilment_store
    from copilot.core.send import sha256_json
    monkeypatch.setenv("COPILOT_FEATURE_DELIVERY_FACTS", "true")
    db = config.db_path()
    repo.insert_package(db, "PP-R-DEMO-0000001", "human_edit", True, [])
    request = {"trackers": [{"transaction_id": CAP, "tracking_number": NUMBER,
                            "carrier": "FEDEX", "status": "SHIPPED"}]}
    readback = {"tracking_number": NUMBER, "carrier": "FEDEX", "status": "SHIPPED",
                "shipment_date": "2026-10-07"}
    fulfilment_store.save_file(CAP, request, sha256_json(request), sha256_json(request),
                              readback, {"address_comparison": {"result": "match"}}, "registered")
    # Active approval initializes the real attempt store in the same app database.
    from copilot.core.attempt_store import SQLiteAttemptStore
    attempts = SQLiteAttemptStore(db)
    attempts.close()
    assert db.read_bytes()[18:20] == bytes((2, 2))
    def rows():
        with sqlite3.connect(db) as conn:
            return tuple(conn.iterdump())
    before = rows()
    result = service.read(db, CAP)
    assert result["record_status"] == "registered" and result["request"]["tracking"] == NUMBER
    client = authenticated_client(create_app())
    response = client.get(url())
    assert response.status_code == 200 and NUMBER in response.text
    assert response.headers["cache-control"] == "no-store"
    assert rows() == before


@pytest.mark.parametrize("sidecar", ["-wal", "-shm", "-journal"])
def test_linked_auxiliary_storage_still_refused(prepared, monkeypatch, sidecar):
    client, db = prepared
    linked = Path(str(db) + sidecar)
    linked.write_bytes(b"synthetic unsupported")
    original = Path.lstat
    def lstat(path):
        info = original(path)
        return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400) if path == linked.absolute() else info
    before = files(db.parent)
    monkeypatch.setattr(Path, "lstat", lstat)
    assert client.get(url()).status_code == 400
    assert files(db.parent) == before


@pytest.mark.parametrize("requested,observed,mismatch", [
    ("MEX_ESTAFETA", "ESTAFETA", False),
    ("ESTAFETA", "MEX_ESTAFETA", False),
    ("MEX_REDPACK", "REDPACK", False),
    ("FEDEX", "DHL", True),
    ("UNLISTED_ONE", "UNLISTED_TWO", True),
    ("FEDEX_OTHER", "FEDEX", True),
])
def test_rc4_carrier_alias_comparison_preserves_raw_records(tmp_path, requested, observed, mismatch):
    db = tmp_path / "facts.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute(DDL)
    request = {"trackers": [{"transaction_id": CAP, "tracking_number": NUMBER,
                            "carrier": requested, "status": "SHIPPED"}]}
    readback = {"tracking_number": NUMBER, "carrier": observed, "status": "SHIPPED"}
    store(db, request=request, readback=readback)
    before = files(tmp_path)
    facts = service.read(db, CAP)
    assert facts["request"]["carrier"] == requested
    assert facts["readback"]["carrier"] == observed
    assert ("carrier" in facts["discrepancies"]) is mismatch
    assert ("Records disagree; review both sources." in facts["english"]) is mismatch
    assert ("Los registros difieren; revisar ambas fuentes." in facts["spanish"]) is mismatch
    assert files(tmp_path) == before


@pytest.mark.parametrize("requested,observed,verified", [
    ("MEX_ESTAFETA", "ESTAFETA", True),
    ("FEDEX", "DHL", False),
    ("FEDEX", None, False),
])
def test_rc4_carrier_verification_is_scoped_to_both_saved_values(prepared, requested, observed, verified):
    web, db = prepared
    request = {"trackers": [{"transaction_id": CAP, "tracking_number": NUMBER,
                            "carrier": requested, "status": "SHIPPED"}]}
    readback = {"tracking_number": NUMBER, "carrier": observed, "status": "SHIPPED"} if observed else None
    store(db, request=request, readback=readback)
    response = web.get(url())
    assert response.status_code == 200
    # The lone 'Carrier comparison: Verified' line is gone; the comparison list carries the carrier result
    assert 'data-state="carrier-comparison"' not in response.text and "Carrier comparison" not in response.text
    if verified:
        assert "Carrier: same in both" in response.text
        assert requested in response.text and observed in response.text
    elif observed:
        assert "Does not match" in response.text
