"""Synthetic fixtures for sections, faithful quotes, and completeness boundaries."""
import json
import pytest
from scout.tools import pai_tools as T
from scout.service.evidence import EvidenceStore
from scout.service.validators import validate_answer

TEXT = "introductory prose.\r\n\r\nWhat you’ll bring\r\n- Experience building things.\r\n\r\nBenefits:\r\nTime off.\r\n"


def test_sections_partition_and_literal_filter():
    out = T.sections(TEXT)
    assert out['sections_available']
    assert ''.join(p['text'] for p in out['sections']) == TEXT
    assert out['sections'][0]['heading'] is None
    assert all(p['text'] == TEXT[p['start']:p['end']] for p in out['sections'])
    filtered = T.sections(TEXT, ['BRING'])
    assert [p['heading'] for p in filtered['sections']] == ['What you’ll bring']
    assert sum(p['end']-p['start'] for p in filtered['sections']+filtered['omitted']) == len(TEXT)
    miss = T.sections(TEXT, ['required qualifications'])
    assert miss['sections'] == [] and miss['heading_filter_matched'] is False
    assert [p['heading'] for p in miss['omitted']] == [None, 'What you’ll bring', 'Benefits:']


@pytest.mark.parametrize('text,reason', [('Flat Qualifications: Things Benefits: Things','no_line_structure'),
    ('intro.\nbody.','fewer_than_two_headings'), ('Qualifications:\n- Build things.','fewer_than_two_headings'),
    ('- Bullet:\n\n1. List:\n\n2) List:\n\nSentence.\n\nEnds,\n\nEnds;','fewer_than_two_headings')])
def test_fallback_and_false_headings(text, reason):
    assert T.sections(text, ['missing']) == {'mode':'full','requested_mode':'sections','sections_available':False,'splitter_version':'sections-v1','reason':reason}


def test_sections_execution_preserves_same_call_fallback_and_metadata():
    class Fixture:
        sources = T.DEFAULT_SOURCES
        text = TEXT
        def get_job_posting(self, sid, pid):
            return {'posting_id':pid,'snapshot_id':sid,'company_normalized':'example inc.', 'description_text': self.text}
    d = Fixture(); args = {'snapshot_id':'pai-2026-08','posting_id':'synthetic','mode':'sections'}
    out = T.execute_tool(d, 'redesigned','get_job_posting',args)
    assert out['company_normalized'] == 'example inc.' and 'description_text' not in out
    d.text = 'Flattened source'
    out = T.execute_tool(d,'redesigned','get_job_posting',args)
    assert out['description_text'] == d.text and out['requested_mode']=='sections'
    assert T.execute_tool(d,'baseline','get_job_posting',args)['error']['code']=='invalid_argument'
    for extra in ({'headings':[]},{'headings':[' ']},{'headings':'title'},{'headings':['x'*101]}, {'query':['word']}):
        assert T.execute_tool(d,'redesigned','get_job_posting',{**args,**extra})['error']['code']=='invalid_argument'


def answer(eid, quote=None, scope='posting_text'):
    return {'summary':'Fixture conclusion','findings':[{'statement':'Fixture finding','evidence_ids':[eid],'evidence_scope':scope}],
            'limitations':[], 'quotes':[] if quote is None else [{'evidence_id':eid,'quote':quote}]}


def test_quotes_cannot_cross_omitted_spans_and_bad_findings_are_removed():
    ev = EvidenceStore()
    out = {'posting_id':'synthetic','title':'Example','mode':'excerpt','passages':[
        {'start':0,'end':5,'text':'Alpha'},{'start':20,'end':25,'text':'Omega'}]}
    _, ids = ev.register('get_job_posting',{'snapshot_id':'fixture'},out,'redesigned',1)
    eid = ids[0]
    failures, cleaned, _ = validate_answer(answer(eid,'Alpha Omega'),ev,require_complete=False)
    assert failures[0]['check']=='quote_not_found' and cleaned['findings']==[]
    assert validate_answer(answer(eid,'Omega'),ev,require_complete=False)[0]==[]
    _, ids = ev.register('get_dataset_context',{'snapshot_id':'fixture'},{'note':'Context text'},'baseline',2)
    failures, cleaned, _ = validate_answer(answer(ids[0],'Context text','context'),ev,require_complete=False)
    assert failures[0]['check']=='quote_not_found' and not cleaned['findings']


def test_sections_provenance_and_unique_overlapping_spans():
    ev = EvidenceStore()
    out = {'posting_id':'synthetic',**T.sections(TEXT,['bring'])}
    text, ids = ev.register('get_job_posting',{'snapshot_id':'fixture'},out,'redesigned',1)
    p = out['sections'][0]
    assert validate_answer(answer(ids[0],p['text']),ev,require_complete=False)[0]==[]
    assert ev.get(ids[0]).durable()['quotable_spans']==[p['text']]
    initial = p['end']-p['start']
    out2 = {'posting_id':'synthetic','mode':'excerpt','passages':[{'start':p['start']+3,'end':p['end']+3,'text':TEXT[p['start']+3:p['end']+3]}]}
    ev.register('get_job_posting',{'snapshot_id':'fixture'},out2,'redesigned',2)
    assert ev.unique_evidence_chars==initial+3
    ev.register('get_job_posting',{'snapshot_id':'fixture'},{'posting_id':'synthetic','description_text':TEXT},'baseline',3)
    assert ev.unique_evidence_chars==len(TEXT)
    ev.register('get_job_posting',{'snapshot_id':'fixture'},out,'redesigned',4)
    assert ev.unique_evidence_chars==len(TEXT)


def test_aggregate_cannot_exempt_record_or_text_claims():
    ev = EvidenceStore(); args = {'snapshot_id':'fixture'}
    _, (lid,) = ev.register('search_job_postings',args,{'total_count':3,'returned':1,'offset':0,'next_cursor':'next','postings':[{'posting_id':'synthetic'}]},'baseline',1)
    _, (aid,) = ev.register('search_job_postings',args,{'mode':'counts','counts':{'raw':3,'distinct':3},'aggregation':{'status':'complete'}},'baseline',2)
    a = answer(aid,scope='count'); a['findings'][0]['evidence_ids'].append(lid)
    assert validate_answer(a,ev,require_complete=True)[0]==[]
    for scope in ('records','posting_text'):
        a['findings'][0]['evidence_scope']=scope
        failures,cleaned,_ = validate_answer(a,ev,require_complete=True)
        assert 'unfinished_required_pagination' in [f['check'] for f in failures]
        assert cleaned['findings']==[]


def test_equal_stored_identity_without_full_fetch(data):
    for design in T.DESIGNS:
        out = T.execute_tool(data,design,'search_job_postings',{'snapshot_id':'pai-2026-08','company':'anthropic'})
        for p in out['postings']:
            stored = data.db.execute("select company_normalized from job_postings_enriched where raw_id=? and pipeline_version='v1.0'",(p['posting_id'],)).fetchone()[0]
            assert p['company_normalized']==stored
    assert 'do not strip suffixes' in T.IDENTITY_RULE
