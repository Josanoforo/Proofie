
def test_manual_confirmed_tracking_preferred_with_visible_conflict(monkeypatch,tmp_path):
    from copilot import config
    from copilot.db import repo
    from copilot.features.sale_readiness import service
    monkeypatch.setattr(config,'_db_path_override',tmp_path/'data.sqlite')
    monkeypatch.setenv('COPILOT_FEATURE_GAP_RESOLUTION','true')
    did='PP-R-DEMO-0000001'; capture='DEMOTXN0000000001'
    repo.set_manual_fields(config.db_path(),did,{'tracking_number':{'value':'TRACK-2','confidence':'read'},'carrier':{'value':'FEDEX','confidence':'read'}})
    terms={**service.EXAMPLES['physical'], 'tracking_number':'TRACK-1','carrier':'FEDEX','ship_to':service.EXAMPLES['physical']['ship_to'],'evidence_kinds':{}}
    record={'dispute_id':did,'capture_id':capture,'terms':terms}
    result=service.case_view(did,capture,{},[],[],record,{'tracking_number':'TRACK-1','carrier':'FEDEX'})
    assert result['result']['source_warnings']
    assert result['result']['source_conflicts'][0]['seller_value']=='TRACK-2'
    assert 'TRACK-1'==result['result']['source_conflicts'][0]['other_value']
    assert terms['tracking_number']=='TRACK-1'


def test_flag_off_keeps_previous_projection_and_read_failure_holds_payload(monkeypatch,tmp_path):
    import sqlite3
    from copilot import config
    from copilot.db import repo
    from copilot.features.sale_readiness import service
    did='PP-R-DEMO-0000001'; cap='DEMOTXN0000000001'
    terms={**service.EXAMPLES['physical'], 'evidence_kinds':{}}
    record={'dispute_id':did,'capture_id':cap,'terms':terms}
    monkeypatch.delenv('COPILOT_FEATURE_GAP_RESOLUTION',raising=False)
    monkeypatch.setattr(repo,'get_manual_fields',lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError('blocked')))
    baseline = service.case_view(did,cap,{},[],[],record)['result']
    assert 'unavailable' not in baseline['status'] and not any('could not be read' in gap for gap in baseline['gaps'])
    monkeypatch.setenv('COPILOT_FEATURE_GAP_RESOLUTION','true')
    result=service.case_view(did,cap,{},[],[],record)['result']
    assert result['tracking_payload'] is None and 'unavailable' in result['status']
