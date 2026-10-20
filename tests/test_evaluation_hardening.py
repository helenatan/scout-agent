"""Evaluation regressions: retain launched denominators, durable IDs, bounded billing refresh."""
import json
import sys
from pathlib import Path

import httpx
import pytest

from eval import report, run_matrix


def record(rid='run_1', settled=True):
    return {'run_id':rid,'state':'completed','config':'A','task_id':'DEV-R1','group':'R','suite_version':'test',
            'request':{'model':'haiku','tool_design':'baseline','max_cost_usd':2,'deadline_seconds':60},
            'cost':{'usd':1.,'settled':settled},'duration_seconds':2,'outcome':{'reason':'validated'}}


def invoke_report(tmp_path, monkeypatch, grades):
    out=tmp_path/'eval/results/test';(out/'runs').mkdir(parents=True)
    for i in range(3):
        (out/'runs'/f'run_{i}.json').write_text(json.dumps(record(f'run_{i}')))
    (out/'grades.json').write_text(json.dumps(grades))
    (out/'manifest.json').write_text(json.dumps({'launched':3,'runs':[{'run_id':f'run_{i}'} for i in range(3)]}))
    monkeypatch.setattr(report,'ROOT',tmp_path)
    monkeypatch.setattr(sys,'argv',['report','--label','test','--resamples','30'])
    report.main()
    return json.loads((out/'report.json').read_text())


def test_missing_and_null_grades_keep_costs_denominator_and_provisional(tmp_path,monkeypatch):
    r=invoke_report(tmp_path,monkeypatch,{'run_0':{'pass':True},'run_1':{'pass':None}})
    a=r['configurations']['A']
    assert a['launched']==3 and a['total_cost_usd']==3
    assert a['passes']==1 and a['grades_pending']==2 and a['confirmed_failures']==0
    assert a['success_rate']==pytest.approx(1/3)
    assert a['provisional'] and r['meta']['provisional'] and r['recommendation']['provisional']
    assert r['reconciliation']['runs']==3 and r['reconciliation']['records_total_usd']==3
    assert r['recommendation']['leading'] is None


def test_review_pending_and_fully_resolved_grades(tmp_path,monkeypatch):
    r=invoke_report(tmp_path,monkeypatch,{'run_0':{'pass':True},'run_1':{'pass':False},'run_2':{'pass':True}})
    assert not r['meta']['provisional']
    assert r['configurations']['A']['confirmed_failures']==1


def test_report_refuses_missing_launched_exports(tmp_path,monkeypatch):
    out=tmp_path/'eval/results/test';(out/'runs').mkdir(parents=True)
    (out/'manifest.json').write_text(json.dumps({'launched':1,'runs':[{'run_id':'missing'}]}))
    monkeypatch.setattr(report,'ROOT',tmp_path)
    monkeypatch.setattr(sys,'argv',['report','--label','test'])
    with pytest.raises(ValueError,match='Missing launched run exports'):
        report.main()


TASK={'id':'DEV-R1','group':'R','suite_version':'test','request':{'task':'count','snapshot_ids':['pai-2026-08'],'max_cost_usd':2,'deadline_seconds':60}}


def fake_api(out, *, settled=False, fail_get=False, fail_events=False):
    calls=[]
    def handle(req):
        calls.append((req.method,req.url.path))
        if req.url.path=='/healthz':
            return httpx.Response(200,json={'provider':'fake','settings':{}})
        if req.method=='POST':
            manifest=json.loads((out/'manifest.json').read_text())
            assert manifest['launch_intents'][-1]['status']=='submitting'
            assert req.headers['Idempotency-Key']==manifest['launch_intents'][-1]['idempotency_key']
            return httpx.Response(202,json={'run_id':'run_1'})
        manifest=json.loads((out/'manifest.json').read_text())
        assert manifest['runs'][0]['run_id']=='run_1' and manifest['launched']==1
        if fail_get and not req.url.path.endswith('/events'):
            raise httpx.ConnectError('lost GET',request=req)
        if req.url.path.endswith('/events'):
            assert (out/'runs/run_1.json').exists() # terminal answer saved before trace request
            if fail_events:
                raise httpx.ConnectError('lost trace',request=req)
            return httpx.Response(200,json={'requests':[{'request_id':'q','settled':settled,'usd':1 if settled else None,'reserved_usd':.5}],
                                           'evidence':[{'id':'ev_1','delivered_text':'payload'}]})
        return httpx.Response(200,json=record(settled=settled))
    return httpx.Client(base_url='http://test',transport=httpx.MockTransport(handle)),calls


def launch(client,out,**kwargs):
    return run_matrix.run_matrix(client,out,[TASK],label='test',models=['haiku'],designs=['baseline'],reps=1,cap_usd=5,**kwargs)


def test_terminal_unknown_billing_is_saved_and_reconciles_without_relaunch(tmp_path):
    out=tmp_path/'matrix';client,calls=fake_api(out)
    m=launch(client,out)
    assert m['reconciliation_pending']==['run_1']
    assert m['spent_usd']==1 and m['committed_usd']==.5
    assert json.loads((out/'runs/run_1.json').read_text())['trace']['evidence'][0]['id']=='ev_1'
    before=len(calls)
    with pytest.raises(FileExistsError):launch(client,out)
    assert len(calls)==before
    other,newcalls=fake_api(out,settled=True)
    run_matrix.reconcile(other,out,m,0)
    assert not m['reconciliation_pending'] and m['committed_usd']==0
    assert all(method!='POST' for method,_ in newcalls)


@pytest.mark.parametrize('fail_events',[False,True])
def test_accepted_id_survives_read_failure(tmp_path,fail_events):
    out=tmp_path/'matrix';client,_=fake_api(out,fail_get=not fail_events,fail_events=fail_events)
    with pytest.raises(httpx.ConnectError):launch(client,out)
    m=json.loads((out/'manifest.json').read_text())
    assert m['runs'][0]['run_id']=='run_1' and m['launched']==1
    if fail_events:
        assert json.loads((out/'runs/run_1.json').read_text())['state']=='completed'


def test_cap_reserves_full_next_budget_and_unknown_terminal_charge(tmp_path):
    out=tmp_path/'matrix';client,calls=fake_api(out)
    m=run_matrix.run_matrix(client,out,[TASK],label='test',models=['haiku'],designs=['baseline'],reps=2,cap_usd=3)
    assert m['launched']==1 and m['stop_reason']=='local_spending_cap'
    assert sum(method=='POST' for method,_ in calls)==1


def test_active_wait_is_bounded(tmp_path):
    out=tmp_path/'matrix';client,_=fake_api(out)
    original=run_matrix.fetch_record
    def active(*args):
        rec=original(*args)
        args[2]['state']='running'
        return rec
    from unittest.mock import patch
    with patch.object(run_matrix,'fetch_record',active):
        with pytest.raises(TimeoutError):launch(client,out,wait_seconds=0)
    assert json.loads((out/'manifest.json').read_text())['launched']==1


def test_provider_unavailable_stops_launching_and_resume_retries_once(tmp_path):
    """Subscription limits: stop the plan, keep the refused record, retry that cell once under a derived key."""
    out=tmp_path/'matrix';posts=[];state={'n':0}
    task2={**TASK,'id':'T2'}
    def handle(req):
        if req.url.path=='/healthz':
            return httpx.Response(200,json={'provider':'claude-code-cli','settings':{'x':1}})
        if req.method=='POST':
            state['n']+=1;posts.append(req.headers['Idempotency-Key'])
            return httpx.Response(202,json={'run_id':f"run_{state['n']}"})
        rid=req.url.path.split('/')[3]
        if req.url.path.endswith('/events'):
            return httpx.Response(200,json={'requests':[],'evidence':[]})
        rec=record(rid)
        if rid=='run_1':
            rec.update(state='failed',outcome={'reason':'provider_unavailable','detail':'usage limit'})
        return httpx.Response(200,json=rec)
    client=httpx.Client(base_url='http://test',transport=httpx.MockTransport(handle))
    m=run_matrix.run_matrix(client,out,[TASK,task2],label='t',models=['haiku'],designs=['baseline'],reps=1,cap_usd=5)
    assert m['stop_reason']=='provider_unavailable' and m['launched']==1 and len(posts)==1
    m=run_matrix.run_matrix(client,out,[TASK,task2],label='t',models=['haiku'],designs=['baseline'],reps=1,cap_usd=5,resume=True)
    assert posts[1]==posts[0]+'#r1' and len(posts)==3 and m['launched']==2
    assert (out/'runs-superseded/run_1.json').exists() and not (out/'runs/run_1.json').exists()
    assert [e['run_id'] for e in m['superseded_runs']]==['run_1']
    m=run_matrix.run_matrix(client,out,[TASK,task2],label='t',models=['haiku'],designs=['baseline'],reps=1,cap_usd=5,resume=True)
    assert len(posts)==3  # nothing left to launch; launched attempts are never relaunched
