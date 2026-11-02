"""Synthetic read-only inspection; never invokes the pilot transport."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location("lock_inspector",Path(__file__).with_name("lock_inspect.py"))
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


def files(root):
    return {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("lock,attempt,expected",[(None,None,0),("",None,3),('{"pid":123}',None,3),(None,"sent_unconfirmed",3),(None,"confirmed",0),(None,"pending",3)])
def test_states_preserve_all_files(tmp_path,lock,attempt,expected):
    package=tmp_path/"package.json";sha="a"*64;did="PP-R-DEMO-0000001"
    package.write_text(json.dumps({"dispute_id":did,"paypal_request_sha256":sha}),encoding="utf-8")
    directory=tmp_path/"send_attempts";directory.mkdir();base=directory/(did+"__"+sha+".json")
    if lock is not None:base.with_suffix(".lock").write_text(lock,encoding="utf-8")
    if attempt is not None:base.write_text(json.dumps({"state":attempt,"private_unrelated_payload":"not printed"}),encoding="utf-8")
    before=files(tmp_path)
    code,row=module.inspect(package,liveness=lambda pid:"dead")
    assert code==expected and files(tmp_path)==before and row["retry_authorized"] is False
    assert row["sent_unconfirmed"]==(attempt=="sent_unconfirmed")
    assert "private_unrelated_payload" not in json.dumps(row)
    assert row["verify_argv"][2:]==["verify","--package",str(package)]
    if lock=="":assert row["owner_pid"] is None and row["owner_liveness"]=="unavailable"
    if lock:assert row["owner_liveness"]=="dead"


@pytest.mark.parametrize("content",["x","[]",'{"dispute_id":"bad"}', '{"dispute_id":"PP-R-DEMO-1","paypal_request_sha256":"bad"}'])
def test_malformed_is_controlled_and_readonly(tmp_path,capsys,content):
    p=tmp_path/"package.json";p.write_text(content,encoding="utf-8");before=files(tmp_path)
    assert module.main(["--package",str(p)])==2
    assert files(tmp_path)==before
    row=json.loads(capsys.readouterr().out);assert row["retry_authorized"] is False and row["invalid"]


def test_liveness_current_pid_without_signal_or_mutation():
    assert module.pid_liveness(os.getpid())=="alive"
    assert module.pid_liveness(True)=="unknown"


def test_duplicate_metadata_and_huge_pid_fail_closed(tmp_path,capsys):
    sha="a"*64;did="PP-R-DEMO-0000001";p=tmp_path/"package.json"
    p.write_text(json.dumps({"dispute_id":did,"paypal_request_sha256":sha}),encoding="utf-8")
    d=tmp_path/"send_attempts";d.mkdir();lock=d/(did+"__"+sha+".lock")
    for content in ('{"pid":1,"pid":2}', '{"pid":999999999999999999999999}'):
        lock.write_text(content,encoding="utf-8");before=files(tmp_path)
        assert module.main(["--package",str(p)])==2
        assert files(tmp_path)==before
        assert json.loads(capsys.readouterr().out)["invalid"]
    assert module.pid_liveness(2**64)=="unknown"
