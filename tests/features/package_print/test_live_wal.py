"""Print the normal application WAL state without source writes; synthetic only."""
import sqlite3
import time
import hashlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import pytest
from copilot import config
from copilot.db import repo
from copilot.web.routes_disputes import get_client
from copilot.core.attempt_store import SQLiteAttemptStore
from copilot.features.package_print import service
from .test_print import prepared, files, url, DID


@pytest.fixture
def live_app(prepared):
    client,db,uploads,pid,doc,sha=prepared
    # Use the real app/page path with the existing structural synthetic GET double.
    from test_feature_global_and_anchors import Fake
    client.app.dependency_overrides[get_client]=lambda:Fake()
    response=client.get(f'/disputes/{DID}/packages/{pid}')
    assert response.status_code==200
    # A normal app writer is kept alive, as during a simultaneous send/record task.
    holder=SQLiteAttemptStore(db)
    assert __import__('pathlib').Path(str(db)+'-wal').is_file()
    assert __import__('pathlib').Path(str(db)+'-shm').is_file()
    claim=repo.list_claims(db,pid)[0]
    repo.mark_claim_edited(db,claim['id'],'A freshly committed synthetic statement in WAL.')
    try:yield client,db,uploads,pid,doc,sha
    finally:holder.close()


def durable_files(db):
    return {suffix: hashlib.sha256(Path(str(db)+suffix).read_bytes()).hexdigest()
            for suffix in ('', '-wal') if Path(str(db)+suffix).exists()}


def test_live_app_committed_wal_prints_without_business_mutation(live_app,record_property):
    client,db,uploads,pid,doc,sha=live_app
    before=durable_files(db)
    def shm_state():
        path=Path(str(db)+'-shm')
        if not path.exists():return {'exists':False}
        # Windows reserves bytes120:128 for mandatory SQLite native locks.
        with path.open('rb',buffering=0) as stream:
            data=stream.read(120);stream.seek(128);data+=stream.read()
        return {'exists':True,'size':path.stat().st_size,'data_sha256_excluding_lock_bytes':hashlib.sha256(data).hexdigest()}
    record_property('shm_before',str(shm_state()))
    response=client.get(url(pid))
    record_property('shm_after',str(shm_state()))
    assert response.status_code==200,response.text
    assert 'A freshly committed synthetic statement in WAL.' in response.text and sha in response.text
    assert durable_files(db)==before


def test_quiescent_wal_header_prints_with_standard_sqlite_coordination(prepared):
    client,db,uploads,pid,doc,sha=prepared
    from test_feature_global_and_anchors import Fake
    client.app.dependency_overrides[get_client]=lambda:Fake()
    assert client.get(f'/disputes/{DID}/packages/{pid}').status_code==200
    assert db.read_bytes()[18:20]==b'\x02\x02'
    before=hashlib.sha256(db.read_bytes()).hexdigest()
    response=client.get(url(pid))
    assert response.status_code==200,response.text
    assert 'Synthetic shipment proof.' in response.text and sha in response.text
    assert hashlib.sha256(db.read_bytes()).hexdigest()==before


def test_reader_is_dedicated_readonly_and_closed_before_documents(live_app,monkeypatch):
    _,db,uploads,pid,_,_=live_app
    original=sqlite3.connect;readers=[];statements=[]
    def connect(database,*a,**kw):
        assert database==db.absolute().as_uri()+'?mode=ro&cache=private'
        assert kw['uri'] and 0<kw['timeout']<=.25
        conn=original(database,*a,**kw);readers.append(conn)
        conn.set_trace_callback(statements.append)
        return conn
    raw=service.read_raw
    def checked_raw(*a):
        with pytest.raises(sqlite3.ProgrammingError):readers[0].execute('SELECT 1')
        return raw(*a)
    monkeypatch.setattr(service.sqlite3,'connect',connect)
    monkeypatch.setattr(service,'read_raw',checked_raw)
    assert service.read(db,uploads,DID,pid)['version']==1
    assert statements.count('BEGIN')==1 and 'PRAGMA query_only=ON' in statements
    assert not any(x.startswith(('UPDATE','INSERT','DELETE','CREATE')) for x in statements)


def test_concurrent_commit_between_queries_keeps_one_snapshot(live_app,monkeypatch):
    _,db,uploads,pid,_,_=live_app
    original=sqlite3.connect;commits=[]
    def connect(*a,**kw):
        conn=original(*a,**kw)
        if kw.get('uri'):
            def trace(sql):
                if sql.startswith('SELECT id,text'):
                    with original(db,timeout=.25) as writer:
                        writer.execute('UPDATE packages SET version=2 WHERE id=?',(pid,))
                        writer.execute('UPDATE claims SET text=? WHERE package_id=?',('Epoch 2',pid))
                    commits.append(2)
            conn.set_trace_callback(trace)
        return conn
    monkeypatch.setattr(service.sqlite3,'connect',connect)
    result=service.read(db,uploads,DID,pid)
    assert commits==[2] and result['version']==1
    assert result['pages'][0]['text']=='A freshly committed synthetic statement in WAL.'
    monkeypatch.setattr(service.sqlite3,'connect',original)
    result=service.read(db,uploads,DID,pid)
    assert result['version']==2 and result['pages'][0]['text']=='Epoch 2'


def test_pending_wal_write_reads_last_committed_data(live_app):
    client,db,uploads,pid,_,_=live_app
    writer=sqlite3.connect(db)
    try:
        writer.execute('BEGIN IMMEDIATE')
        writer.execute('UPDATE packages SET version=2 WHERE id=?',(pid,))
        before=durable_files(db)
        response=client.get(url(pid))
        assert response.status_code==200 and 'A freshly committed synthetic statement in WAL.' in response.text
        assert durable_files(db)==before
    finally:writer.rollback();writer.close()


def test_exclusive_rollback_lock_has_bounded_safe_failure(prepared,monkeypatch):
    client,db,_,pid,_,sha=prepared
    writer=sqlite3.connect(db)
    # The guarantee is checked from the code: every connect the print request makes waits at most .25 s on the
    # lock and there are few of them (today one, so the lock wait is <= .25 s). The rest of the elapsed time is
    # the request itself (cold app, template), which depends on machine load: alone it takes ~.8 s, and 2.2 s
    # was seen during a full run with another scan running. So the wall clock below only detects a hang (the
    # sqlite default wait is 5 s per connect; the pytest timeout is 600 s) and is not the bound.
    original=sqlite3.connect;connects=[]
    def connect(database,*a,**kw):
        connects.append(kw.get('timeout',a[0] if a else 5.0))
        return original(database,*a,**kw)
    monkeypatch.setattr(service.sqlite3,'connect',connect)
    try:
        writer.execute('BEGIN EXCLUSIVE')
        start=time.monotonic();response=client.get(url(pid));elapsed=time.monotonic()-start
        assert response.status_code==400
        assert 1<=len(connects)<=3 and all(0<t<=.25 for t in connects),connects
        assert elapsed<10,elapsed
        assert sha not in response.text and str(db) not in response.text
    finally:writer.rollback();writer.close()


@pytest.mark.parametrize('fault',['version','document_size','altered_document'])
def test_invalid_wal_metadata_and_documents_fail_closed(live_app,fault):
    client,db,uploads,pid,doc,sha=live_app
    if fault=='altered_document':(uploads/sha).write_bytes(b'altered synthetic document')
    else:
        with sqlite3.connect(db) as writer:
            if fault=='version':writer.execute('UPDATE packages SET version=? WHERE id=?',('not a numeric version',pid))
            else:writer.execute('UPDATE documents SET size_bytes=size_bytes+1 WHERE id=?',(doc,))
    before=durable_files(db);response=client.get(url(pid))
    assert response.status_code==400 and sha not in response.text and str(db) not in response.text
    assert durable_files(db)==before


@pytest.mark.parametrize('suffix',['-wal','-shm','-journal'])
def test_auxiliary_reparse_rejected_before_sqlite(prepared,monkeypatch,suffix):
    from types import SimpleNamespace
    client,db,_,pid,_,sha=prepared
    auxiliary=Path(str(db)+suffix)
    auxiliary.write_bytes(b'Synthetic linked auxiliary target')
    original=Path.lstat
    def linked(path):
        info=original(path)
        if path==auxiliary.absolute():
            return SimpleNamespace(st_mode=info.st_mode,st_file_attributes=0x400)
        return info
    def forbidden_connect(*a,**kw):
        raise AssertionError('SQLite must not follow linked auxiliary storage')
    before=files(db.parent)
    with monkeypatch.context() as patch:
        patch.setattr(Path,'lstat',linked)
        patch.setattr(service.sqlite3,'connect',forbidden_connect)
        response=client.get(url(pid))
    assert response.status_code==400
    assert sha not in response.text and str(db) not in response.text
    assert files(db.parent)==before
