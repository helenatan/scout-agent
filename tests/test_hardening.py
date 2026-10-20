"""Regression cases for admission races, uncertain billing, and restart-safe evidence."""
import asyncio
import json
import time

import httpx
import pytest

from scout.service.engine import DirectToolClient, RunEngine, Settings
from scout.service.providers import FakeProvider, ProviderError, generic_script, response, tool_use
from scout.service.schemas import RunRequest
from scout.service.store import Store
from scout.service.api import build_app

REQ = dict(task='Inspect captured jobs', snapshot_ids=['pai-2026-08'], max_cost_usd=0.5,
           deadline_seconds=60, model='haiku', tool_design='redesigned')


class TrackingClient(DirectToolClient):
    closed = False
    async def close(self):
        self.closed = True
        if hasattr(self, '_data'):
            self._data.db.close()


def engine(tmp_path, provider, **settings):
    clients = []
    def factory(design):
        c = TrackingClient(design)
        clients.append(c)
        return c
    return RunEngine(Store(tmp_path/'runs.sqlite'), provider, factory, Settings(max_output_tokens=1000, **settings)), clients


@pytest.mark.parametrize('stop', ['cancel', 'deadline', 'both'])
@pytest.mark.parametrize('phase', ['answer', 'repair', 'retry'])
def test_stop_during_estimation_never_dispatches(tmp_path, stop, phase):
    async def go():
        entered, release = asyncio.Event(), asyncio.Event()
        class PausedProvider(FakeProvider):
            counts = 0
            async def count_tokens(self, *args):
                self.counts += 1
                target = 2 if phase == 'answer' else 3  # prefix, answer, repair/retry
                if self.counts == target:
                    entered.set()
                    await release.wait()
                return 100
        def initial(*_):
            if phase == 'retry':
                raise ProviderError('rate limited', retryable=True, charge_known_zero=True)
            return response([{'type':'text', 'text':'no submission'}])
        provider = PausedProvider(steps=[initial])
        eng, clients = engine(tmp_path, provider, retry_wait_seconds=0)
        rec = eng.submit(RunRequest(**REQ)); rid = rec['run_id']
        await asyncio.wait_for(entered.wait(), 2)
        if stop in ('cancel', 'both'):
            await eng.cancel(rid)
        if stop in ('deadline', 'both'):
            eng.contexts[rid].deadline_mono = time.monotonic()-1
        release.set()
        await asyncio.wait_for(eng.tasks[rid], 2)
        assert len(provider.calls) == (0 if phase=='answer' else 1)
        final = eng.store.get_run(rid)
        assert final['outcome']['reason'] == ('cancel_requested' if stop=='cancel' else 'deadline_exceeded')
        assert final['cost']['settled']
        assert clients[0].closed
    asyncio.run(go())


@pytest.mark.parametrize('late', [False, True])
@pytest.mark.parametrize('error', ['connection', 'unexpected'])
def test_unknown_charge_survives_terminal_and_restart(tmp_path, late, error):
    async def go():
        async def fail(*_):
            if late:
                await asyncio.sleep(.05)
            if error=='connection':
                raise ProviderError('response lost', retryable=True)
            raise RuntimeError('unexpected provider failure')
        provider=FakeProvider(steps=[fail])
        eng,clients=engine(tmp_path, provider, request_timeout_seconds=.01 if late else 1)
        rec=eng.submit(RunRequest(**REQ));rid=rec['run_id']
        await eng.tasks[rid]
        await asyncio.sleep(.08)
        final=eng.store.get_run(rid)
        assert final['state']=='failed' and not final['cost']['settled']
        q=eng.store.requests(rid)[0]
        assert q['usd'] is None and not q['settled']
        assert len(provider.calls)==1 and clients[0].closed
        reopened=Store(tmp_path/'runs.sqlite')
        assert reopened.spend_summary()['committed_usd']==q['reserved_usd']
        other=RunEngine(reopened, FakeProvider(), DirectToolClient, spend_cap_usd=.5)
        assert not other.spending_headroom(RunRequest(**REQ))[0]
        assert not any(e['event']=='cost_settled' for e in eng.store.events(rid))
    asyncio.run(go())


def test_known_zero_rejection_allows_one_retry(tmp_path):
    def reject(*_):
        raise ProviderError('429 rejection', status=429, retryable=True, charge_known_zero=True)
    async def go():
        provider=FakeProvider(steps=[reject,reject])
        eng,_=engine(tmp_path,provider,retry_wait_seconds=0)
        rec=eng.submit(RunRequest(**REQ));rid=rec['run_id']
        await eng.tasks[rid]
        assert len(provider.calls)==2
        assert eng.store.get_run(rid)['cost']['settled']
        assert eng.store.spend_summary()['committed_usd']==0
    asyncio.run(go())


def test_late_usage_updates_cost_only(tmp_path):
    async def delayed(*_):
        await asyncio.sleep(.06)
        return response([{'type':'text','text':'late content must not replace answer'}])
    async def go():
        eng,_=engine(tmp_path,FakeProvider(steps=[delayed]),request_timeout_seconds=.01)
        rec=eng.submit(RunRequest(**REQ));rid=rec['run_id']
        await eng.tasks[rid]
        before=eng.store.get_run(rid)
        assert not before['cost']['settled']
        await asyncio.sleep(.08)
        after=eng.store.get_run(rid)
        for k in ['answer','state','outcome','duration_seconds']:
            assert after[k]==before[k]
        assert after['cost']['settled'] and after['cost']['usd']>0
        assert eng.store.spend_summary()['committed_usd']==0
    asyncio.run(go())


def test_evidence_resolves_after_restart_through_events_api(tmp_path):
    async def go():
        eng,_=engine(tmp_path,FakeProvider(steps=generic_script(['pai-2026-08'],'Anthropic')))
        rec=eng.submit(RunRequest(**REQ));rid=rec['run_id']
        await eng.tasks[rid]
        expected={k:v.durable() for k,v in eng.contexts[rid].ev.records.items()}
        assert expected
        st=Store(tmp_path/'runs.sqlite')
        app=build_app(store=st,provider=FakeProvider(),tool_client_factory=DirectToolClient)
        for startup in app.router.on_startup:
            await startup()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
            data=(await c.get(f'/v1/runs/{rid}/events')).json()
        actual={r['id']:r for r in data['evidence']}
        assert actual==expected
        for r in actual.values():
            assert json.loads(r['delivered_text'])['evidence_id']==r['id']
            assert r['dataset_version']
        pages=[r for r in actual.values() if r['source_type']=='posting_list']
        assert pages and pages[0]['posting_ids'] and pages[0]['query_fingerprint']

    asyncio.run(go())


def test_aggregate_and_group_provenance_survives_restart(tmp_path):
    async def go():
        from scout.service.evidence import EvidenceStore
        from scout.tools.pai_tools import PAIData
        data=PAIData(); ev=EvidenceStore(); st=Store(tmp_path/'evidence.sqlite')
        for mode in ['counts','duplicate_groups']:
            args={'snapshot_id':'pai-w-2026-08-24','text':'agent','mode':mode}
            out=data.search_job_postings(**args)
            _,ids=ev.register('search_job_postings',args,out,'redesigned',1)
            st.save_evidence('run_1',[ev.get(eid).durable() for eid in ids])
        restored=Store(tmp_path/'evidence.sqlite').evidence('run_1')
        aggregate=next(r for r in restored if r['source_type']=='aggregate')
        assert aggregate['raw']==33 and aggregate['distinct']==24
        assert aggregate['identity_rule_version'] and aggregate['dataset_version']
        assert json.loads(aggregate['delivered_text'])['effective_filters']['text']=='agent'
        group=next(r for r in restored if r['source_type']=='duplicate_group_list')
        assert group['posting_ids'] and group['total_groups']
        data.db.close()
    asyncio.run(go())


def test_deadline_during_startup_is_not_tool_failure(tmp_path):
    async def go():
        began,release=asyncio.Event(),asyncio.Event()
        class Client(TrackingClient):
            async def start(self):
                began.set();await release.wait()
                raise RuntimeError('startup interrupted')
        c=Client('redesigned')
        eng=RunEngine(Store(tmp_path/'runs.sqlite'),FakeProvider(),lambda _:c)
        rec=eng.submit(RunRequest(**REQ));rid=rec['run_id']
        await began.wait();eng.contexts[rid].deadline_mono=time.monotonic()-1;release.set()
        await eng.tasks[rid]
        assert eng.store.get_run(rid)['outcome']['reason']=='deadline_exceeded'
        assert c.closed
    asyncio.run(go())


def test_spending_does_not_double_count_active_cost_and_keeps_legacy_unknown(tmp_path):
    st=Store(tmp_path/'spend.sqlite')
    st.create_run({'run_id':'active','state':'running','created_at':'now','request':{'max_cost_usd':1},'cost':{'usd':.3,'settled':True}})
    assert st.spend_summary()['committed_usd']==.7
    st.create_run({'run_id':'legacy','state':'failed','created_at':'now','request':{'max_cost_usd':1},'cost':{'usd':.2,'settled':False}})
    assert st.spend_summary()['committed_usd']==1.5
    assert st.spend_summary()['confirmed_usd']==.5


@pytest.mark.parametrize('status,known_zero', [(429,True),(400,True),(408,False),(500,False),(503,False)])
def test_anthropic_adapter_distinguishes_rejection_and_unknown_errors(status,known_zero):
    from types import SimpleNamespace
    import anthropic
    from scout.service.providers import AnthropicProvider
    async def create(**kwargs):
        reply=httpx.Response(status,request=httpx.Request('POST','https://example.invalid'),headers={'request-id':'provider-1'})
        raise anthropic.APIStatusError('rejected or uncertain',response=reply,body={})
    provider=AnthropicProvider.__new__(AnthropicProvider)
    provider._anthropic=anthropic
    provider._client=SimpleNamespace(messages=SimpleNamespace(create=create))
    async def go():
        with pytest.raises(ProviderError) as caught:
            await provider.create('model','system',[],[],100,None)
        assert caught.value.charge_known_zero is known_zero
        assert caught.value.retryable is (status==429)
        assert caught.value.request_id=='provider-1'
    asyncio.run(go())


def test_expiry_during_reservation_persistence_releases_unsent_request(tmp_path):
    async def go():
        provider=FakeProvider()
        eng,_=engine(tmp_path,provider)
        sync=eng._sync_record
        def expire(ctx):
            sync(ctx)
            if ctx.requests:
                ctx.deadline_mono=time.monotonic()-1
        eng._sync_record=expire
        rec=eng.submit(RunRequest(**REQ));rid=rec['run_id']
        await eng.tasks[rid]
        assert provider.calls==[]
        assert eng.store.get_run(rid)['outcome']['reason']=='deadline_exceeded'
        assert eng.store.requests(rid)[0]['usd']==0
        assert eng.store.spend_summary()['committed_usd']==0
        assert not any(e.get('phase')=='sent' for e in eng.store.events(rid))
    asyncio.run(go())
