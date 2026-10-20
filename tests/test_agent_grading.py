import copy
from eval.agent_grading import evaluate,REVIEWERS,VERSION,sample_ids
from eval.final_grade import digest,review_context,criteria

def fixtures():
 k={'R1':{'key':{'raw_count':3},'rubric':{'essential_outputs':['Correct count'],'evidence':'Matching citations','limitations':'Captured only','fail_conditions':['Wrong count']}}}
 records=[{'run_id':f'r{i}','task_id':'R1','config':c,'rep':0,'state':'completed','request':{'task':'Count'},'answer':{'summary':'3'},'evidence':[]} for i,c in enumerate('ABCD')]
 reviews={w:{} for w in REVIEWERS}
 for r in records:
  for w in REVIEWERS:reviews[w][digest(r['run_id'])[:20]]={'source':'agent','reviewer':w,'method':VERSION,'context':review_context(r,k['R1']),'checks':{c['id']:True for c in criteria(k['R1'])},'reasons':{c['id']:'Specific checked evidence.' for c in criteria(k['R1'])},'uncertain':False}
 return records,k,reviews

def test_agreement_still_requires_sample():
 r,k,ds=fixtures();g,q,e=evaluate(r,k,ds,{})
 assert len(q)==1 and sum(x['pass'] is True for x in g.values())==3

def test_disagreement_uncertainty_staleness_cannot_pass():
 for kind in ['disagree','uncertain','stale','missing']:
  r,k,ds=fixtures();rid=next(x for x in ds['reviewer-a'] if x not in sample_ids(r))
  if kind=='disagree':ds['reviewer-a'][rid]['checks']['evidence']=False
  if kind=='uncertain':ds['reviewer-a'][rid]['uncertain']=True
  if kind=='stale':ds['reviewer-a'][rid]['context']='old'
  if kind=='missing':del ds['reviewer-b'][rid]
  g,q,_=evaluate(r,k,ds,{})
  run=next(x for x in r if digest(x['run_id'])[:20]==rid)
  assert g[run['run_id']]['pass'] is None

def test_human_overturn_expands_whole_task():
 r,k,ds=fixtures();rid=next(iter(sample_ids(r)))
 d=copy.deepcopy(ds['reviewer-a'][rid]);d.update(source='human',reviewer='Human',notes='Reference mismatch');d['checks']['exact_key']=False
 g,q,e=evaluate(r,k,ds,{rid:d})
 assert e==['R1'] and len(q)==3 and sum(x['pass'] is False for x in g.values())==1

def test_sample_independent_of_order_and_answer_verdict():
 r,k,ds=fixtures();sample=sample_ids(r)
 r.reverse()
 for x in r:x['answer']['summary']='wrong'
 assert sample_ids(r)==sample
