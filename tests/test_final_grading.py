import copy
import pytest
from eval.final_grade import grade_final, criteria, review_context, packet

@pytest.fixture
def sample():
    entry={'key':{'raw_count':3,'distinct_count':2},'rubric':{'essential_outputs':['Exact counts and complete records'], 'evidence':'Cite the matching evidence', 'limitations':'Captured postings only', 'fail_conditions':['Unsupported market demand']}}
    rec={'run_id':'test-run','task_id':'R1','state':'completed','request':{'task':'Count records'},'answer':{'summary':'3 records, 2 identities','findings':[]},'evidence':[],'trace':{}}
    decision={'context':review_context(rec,entry),'source':'human','reviewer':'Example reviewer','notes':'Checked every criterion against the evidence.','checks':{c['id']:True for c in criteria(entry)}}
    return rec,entry,decision

def test_key_approval_is_not_answer_approval(sample):
    rec,key,_=sample
    key['key']['reviewer_confirmed']=True
    g=grade_final(rec,key)
    assert g['pass'] is None and g['needs_review']

def test_reviewed_pass_and_negative_criterion(sample):
    rec,key,d=sample
    assert grade_final(rec,key,d)['pass'] is True
    d['checks']['exact_key']=False
    g=grade_final(rec,key,d)
    assert g['pass'] is False and not g['needs_review'] and g['invisible']

@pytest.mark.parametrize('mutation',['answer','evidence','reference','missing','string_bool','agent','reviewer','notes'])
def test_stale_or_incomplete_review_cannot_pass(sample,mutation):
    rec,key,d=copy.deepcopy(sample)
    if mutation=='answer':rec['answer']['summary']='Wrong number'
    elif mutation=='evidence':rec['evidence'].append({'evidence_id':'different'})
    elif mutation=='reference':key['key']['distinct_count']=9
    elif mutation=='missing':d['checks'].pop('evidence')
    elif mutation=='string_bool':d['checks']['evidence']='true'
    elif mutation=='agent':d['source']='agent'
    elif mutation=='reviewer':d['reviewer']=' '
    elif mutation=='notes':d['notes']=' '
    g=grade_final(rec,key,d)
    assert g['pass'] is None and g['needs_review']

def test_runtime_failure_cannot_be_overridden(sample):
    rec,key,d=sample
    rec['state']='incomplete'
    d['context']=review_context(rec,key)
    assert grade_final(rec,key,d)['pass'] is False

def test_in_flight_is_pending(sample):
    rec,key,d=sample
    rec['state']='running'
    assert grade_final(rec,key,d)['pass'] is None

def test_packet_hides_treatment(sample):
    rec,key,_=sample
    rec.update(config='A',model='expensive',cost={'usd':12},rep=3)
    p=packet(rec,key)
    assert not {'config','model','cost','rep','run_id'} & p.keys()
    assert p['reference']==key['key']
