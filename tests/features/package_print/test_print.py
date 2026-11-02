import hashlib
import sqlite3
from pathlib import Path
import pytest
from copilot import config
from copilot.app import create_app
from copilot.db import repo
from copilot.core.raw_storage import write_raw
from _hosted import authenticated_client
from .privacy_cases import private_paths

DID="PP-R-DEMO-0000001"
@pytest.fixture
def prepared(web_mode,monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("COPILOT_FEATURE_"):monkeypatch.delenv(key)
    monkeypatch.setenv("COPILOT_FEATURE_PACKAGE_PRINT","true")
    db=config.db_path();uploads=config.uploads_dir()
    pid=repo.insert_package(db,DID,"human_edit",True,[])
    data=b"DEMO DATA: Synthetic shipment proof."
    sha=hashlib.sha256(data).hexdigest()
    doc=repo.insert_document(db,DID,"demo.txt","text/plain",len(data),sha,data.decode(),True)
    write_raw(uploads,sha,data)
    repo.attach_document(db,pid,doc)
    repo.insert_claims(db,pid,[{"evidence_index":0,"claim_index":0,"text":"Synthetic shipment proof.",
                              "source_ids":[f"doc_{doc}"],"basis":"seller_statement"}])
    app=create_app();client=authenticated_client(app)
    return client,db,uploads,pid,doc,sha

def files(root):
    return {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}

def url(pid,did=DID):
    return f"/disputes/{did}/packages/{pid}/print"

def test_print_valid_real_index_and_no_mutation(prepared):
    client,db,uploads,pid,doc,sha=prepared
    before=files(db.parent)
    response=client.get(url(pid))
    assert response.status_code==200,response.text
    assert "Exhibit 1" in response.text and sha in response.text
    assert "Synthetic shipment proof." in response.text
    assert "@page" in response.text and "Page 1" in response.text
    assert ("package&#39;s own" in response.text or "package's own" in response.text) and "Your browser" in response.text
    assert "https://" not in response.text and "<script" not in response.text
    assert files(db.parent)==before

def test_print_ownership_and_bad_ids_no_mutation(prepared):
    client,db,uploads,pid,doc,sha=prepared;before=files(db.parent)
    assert client.get(url(pid,"PP-R-DEMO-0000002")).status_code==404
    for bad in ("0","-1","bad","9223372036854775808","%2e%2e"):
        assert client.get(url(bad)).status_code in (400,404,422)
    assert files(db.parent)==before

@pytest.mark.parametrize("defect",["missing","rawhash","attachmenthash","excluded"])
def test_source_missing_or_inconsistent_is_safe_rejection(prepared,defect):
    client,db,uploads,pid,doc,sha=prepared
    if defect=="missing":(uploads/sha).unlink()
    elif defect=="rawhash":(uploads/sha).write_bytes(b"DEMO DATA changed")
    else:
        with sqlite3.connect(db) as c:
            if defect=="attachmenthash":c.execute("UPDATE package_attachments SET sha256=?",(hashlib.sha256(b"other").hexdigest(),))
            else:
                # An excluded file only CITED (not attached) is not sent: still refused
                c.execute("DELETE FROM package_attachments WHERE document_id=?",(doc,))
                c.execute("UPDATE documents SET excluded=1 WHERE id=?",(doc,))
    before=files(db.parent);response=client.get(url(pid))
    assert response.status_code==400
    assert sha not in response.text and str(uploads) not in response.text
    assert files(db.parent)==before

def test_an_attached_file_left_out_of_the_checks_is_printed_as_attached_not_as_sent(prepared):
    """An attached file that is left out of the checks goes with the package, so the
    printable copy lists it (with that note) instead of refusing the package. With no confirmed send of
    that file, the note says it is sent WITH the response (future), never that it was sent to PayPal."""
    client,db,uploads,pid,doc,sha=prepared
    with sqlite3.connect(db) as c:
        c.execute("UPDATE documents SET excluded=1 WHERE id=?",(doc,))
    before=files(db.parent);response=client.get(url(pid))
    assert response.status_code==200,response.text
    assert "Exhibit 1" in response.text and sha in response.text
    assert 'data-state="excluded-sent"' in response.text
    assert "Attached to the response (it is sent with it), but left out of the checks." in response.text
    # the header now says "Not sent to PayPal", so the claim is checked on the note itself
    note=response.text.split('data-state="excluded-sent">',1)[1].split("</p>",1)[0]
    assert "sent to PayPal" not in note
    assert files(db.parent)==before

def test_text_is_escaped_and_long_text_not_lost(prepared):
    client,db,uploads,pid,doc,sha=prepared
    hostile="<img src=x onerror=alert(1)> & "+("Synthetic word. "*500)
    with sqlite3.connect(db) as c:c.execute("UPDATE claims SET text=?",(hostile,))
    before=files(db.parent);response=client.get(url(pid))
    assert response.status_code==200
    assert "<img src=x" not in response.text and "&lt;img" in response.text
    assert response.text.count("Synthetic word.")==500
    assert response.text.count('class="sheet"')>2
    assert files(db.parent)==before

@pytest.mark.parametrize("text",["password" + "=" + "DEMO-private-value"])
def test_private_text_never_printed(prepared,text):
    client,db,uploads,pid,doc,sha=prepared
    with sqlite3.connect(db) as c:c.execute("UPDATE claims SET text=?",(text,))
    before=files(db.parent);response=client.get(url(pid))
    assert response.status_code==400 and text not in response.text
    assert files(db.parent)==before

def test_nonlocal_source_declares_no_local_exhibit(prepared):
    client,db,uploads,pid,doc,sha=prepared
    with sqlite3.connect(db) as c:c.execute("UPDATE claims SET source_ids_json=?", ('["paypal_dispute"]',))
    response=client.get(url(pid))
    assert response.status_code==200 and "No local exhibit" in response.text

def test_missing_db_creates_nothing(web_mode,monkeypatch,tmp_path):
    monkeypatch.setenv("COPILOT_FEATURE_PACKAGE_PRINT","true")
    root=tmp_path/"absent";config.set_db_path_override(root/"missing.db")
    client=authenticated_client(create_app())
    assert client.get(url(1)).status_code==404 and not root.exists()

@pytest.mark.hosted_spec
def test_hosted_auth_remains_exterior(prepared):
    from fastapi.testclient import TestClient
    client,db,uploads,pid,doc,sha=prepared
    naked=TestClient(client.app,base_url="https://testserver",client=("127.0.0.1",1234))
    before=files(db.parent)
    response=naked.get(url(pid),follow_redirects=False)
    assert response.status_code==303 and response.headers["location"].startswith("/access")
    assert sha not in response.text
    assert files(db.parent)==before

def test_flag_off_route_absent(prepared,monkeypatch):
    client,db,uploads,pid,doc,sha=prepared
    monkeypatch.delenv("COPILOT_FEATURE_PACKAGE_PRINT")
    off=authenticated_client(create_app());before=files(db.parent)
    assert off.get(url(pid)).status_code==404 and files(db.parent)==before

def test_malformed_database_refused_without_business_mutation(prepared):
    client,db,uploads,pid,doc,sha=prepared
    db.write_bytes(b"DEMO unsupported database")
    before=files(db.parent);response=client.get(url(pid))
    assert response.status_code==400 and files(db.parent)==before


def test_reader_keeps_normal_locks_and_refuses_concurrent_writer_commit(prepared,monkeypatch):
    from copilot.features.package_print import service
    client,db,uploads,pid,doc,sha=prepared
    connect=sqlite3.connect
    attempted=[];readers=[]
    def traced(*a,**kw):
        conn=connect(*a,**kw)
        if kw.get("uri"):
            readers.append(conn)
            def trace(sql):
                if sql.startswith("SELECT id,text"):
                    writer=connect(db,timeout=0)
                    try:
                        writer.execute("UPDATE claims SET text=?",("Changed writer text.",))
                        writer.commit()
                    except sqlite3.OperationalError as exc:
                        attempted.append(getattr(exc,"sqlite_errorcode",None))
                        writer.rollback()
                    finally:writer.close()
            conn.set_trace_callback(trace)
        return conn
    monkeypatch.setattr(service.sqlite3,"connect",traced)
    before=files(db.parent);result=service.read(db,uploads,DID,pid)
    assert attempted and all(code==sqlite3.SQLITE_BUSY for code in attempted)
    assert result["pages"][0]["text"]=="Synthetic shipment proof."
    assert files(db.parent)==before and readers

def test_storage_reparse_rejected_synthetically(prepared,monkeypatch):
    from copilot.features.package_print import service
    from types import SimpleNamespace
    client,db,uploads,pid,doc,sha=prepared
    original=Path.lstat
    def linked(path):
        info=original(path)
        if path==Path(db).absolute():return SimpleNamespace(st_mode=info.st_mode,st_file_attributes=0x400)
        return info
    monkeypatch.setattr(Path,"lstat",linked)
    assert client.get(url(pid)).status_code==400

@pytest.fixture(params=private_paths(), ids=lambda case: case.key)
def private_path(request):
    return request.param


def test_private_path_text_never_printed(prepared, private_path):
    client,db,uploads,pid,doc,sha=prepared
    value=private_path.path
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE claims SET text=?",(value,))
    before=files(db.parent)
    response=client.get(url(pid))
    assert response.status_code==400 and value not in response.text
    assert files(db.parent)==before


def test_private_version_metadata_is_rejected(prepared, private_path):
    client,db,uploads,pid,doc,sha=prepared
    value=private_path.path
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE packages SET version=? WHERE id=?",(value,pid))
    before=files(db.parent)
    response=client.get(url(pid))
    assert response.status_code==400 and value not in response.text
    assert files(db.parent)==before


@pytest.mark.parametrize("value",[0,-1,1.5,"DEMO-non-integer"], ids=["zero","negative","real","text"])
def test_invalid_version_type_or_range_is_rejected(prepared,value):
    client,db,uploads,pid,doc,sha=prepared
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE packages SET version=? WHERE id=?",(value,pid))
    before=files(db.parent)
    response=client.get(url(pid))
    assert response.status_code==400
    assert "Cannot print this package. Review its local sources first." in response.text
    assert f"Version {value}" not in response.text
    assert files(db.parent)==before


@pytest.mark.parametrize("value",[2,9223372036854775807], ids=["positive","int64-upper"])
def test_valid_integer_version_is_preserved(prepared,value):
    client,db,uploads,pid,doc,sha=prepared
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE packages SET version=? WHERE id=?",(value,pid))
    before=files(db.parent)
    response=client.get(url(pid))
    assert response.status_code==200 and str(value) in response.text
    assert files(db.parent)==before


def test_the_hosted_note_is_on_the_print_page_only_when_the_hosted_demo_is_on(prepared):
    import html as htmllib
    import re
    client,db,uploads,pid,doc,sha=prepared
    page=client.get(url(pid)).text
    blocks=re.findall(r'<div class="hosted-demo-note" data-state="hosted-demo">(.*?)</div>',page,flags=re.S)
    if not config.hosted_demo():
        assert 'data-state="hosted-demo"' not in page and blocks==[]
        return
    assert len(blocks)==1 and page.count('data-state="hosted-demo"')==1
    text=htmllib.unescape(re.sub(r"<[^>]+>","",blocks[0]))
    assert " ".join(text.split())==config.HOSTED_DEMO_SENTENCE
    from copilot.web.presentation import _SENTENCE_BREAK
    assert blocks[0].count('class="note-line"')==len(_SENTENCE_BREAK.split(config.HOSTED_DEMO_SENTENCE))
    import re as _re
    last_sheet=page[page.rindex('<section class="sheet">'):]
    assert 'data-state="hosted-demo"' in last_sheet                      # inside the last sheet, so it is part of its page
    assert last_sheet.index('data-state="hosted-demo"')<last_sheet.index('class="footer"')      # before that sheet's footer
    assert _re.search(r"</section>\s*</main>",page) and page.count("<section")==page.count("</section>")   # the last sheet stays the last child of main: no extra printed page
    assert "https://" not in page and "<script" not in page
