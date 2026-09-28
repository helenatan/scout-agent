"""Frozen dual-agent rubric review with human disagreements and task sampling."""
import json
from pathlib import Path
from eval.final_grade import grade_final,review_context,criteria,digest,packet,FINAL_IDS
VERSION='dual-agent-human-audit-v1'
REVIEWERS=('reviewer-a','reviewer-b')

def valid_agent(rec,key,d,reviewer):
    ids={c['id'] for c in criteria(key)}
    return bool(isinstance(d,dict) and d.get('source')=='agent' and d.get('reviewer')==reviewer
      and d.get('context')==review_context(rec,key) and d.get('method')==VERSION
      and isinstance(d.get('checks'),dict) and set(d['checks'])==ids
      and all(type(x) is bool for x in d['checks'].values())
      and isinstance(d.get('reasons'),dict) and set(d['reasons'])==ids
      and all(isinstance(x,str) and x.strip() for x in d['reasons'].values())
      and type(d.get('uncertain')) is bool)

def sample_ids(records):
    selected=set()
    for tid in FINAL_IDS:
        candidates=[r for r in records if r.get('task_id')==tid and r.get('state')=='completed']
        if candidates:
            r=min(candidates,key=lambda r:digest(f"sample-seed-0|{tid}|{r['config']}|{r['rep']}"))
            selected.add(digest(r['run_id'])[:20])
    return selected

def evaluate(records,keys,agent_reviews,human):
    sampled=sample_ids(records);expanded=set();base={};reasons={}
    for r in records:
        rid=digest(r['run_id'])[:20];key=keys[r['task_id']]
        ds=[agent_reviews.get(w,{}).get(rid) for w in REVIEWERS]
        if all(valid_agent(r,key,d,w) for d,w in zip(ds,REVIEWERS)) and ds[0]['checks']==ds[1]['checks'] and not any(d['uncertain'] for d in ds):
            h=grade_final(r,key,human.get(rid))
            if rid in sampled and not h['needs_review'] and isinstance(h['pass'],bool) and h['pass']!=all(ds[0]['checks'].values()):expanded.add(r['task_id'])
    for r in records:
        rid=digest(r['run_id'])[:20];key=keys[r['task_id']]
        h=grade_final(r,key,human.get(rid))
        if r['state']!='completed' or not h['needs_review']:
            base[r['run_id']]=h;continue
        ds=[agent_reviews.get(w,{}).get(rid) for w in REVIEWERS]
        if not all(valid_agent(r,key,d,w) for d,w in zip(ds,REVIEWERS)):
            h['notes']=['Awaiting two complete, current independent agent reviews.'];base[r['run_id']]=h;continue
        why=[]
        if rid in sampled:why.append('Predeclared sample: one completed answer per task.')
        if ds[0]['checks']!=ds[1]['checks']:why.append('Independent reviewers disagree on at least one criterion.')
        if any(d['uncertain'] for d in ds):why.append('At least one reviewer flagged uncertainty.')
        if r['task_id'] in expanded:why.append('Human sample overturned agent consensus; all answers for this task need human review.')
        if why:
            h['notes']=why;reasons[rid]=why;base[r['run_id']]=h;continue
        passed=all(ds[0]['checks'].values())
        base[r['run_id']]={'grader':VERSION,'pass':passed,'needs_review':False,'invisible':not passed,
         'notes':['Both independent agents agree on every criterion; not human-reviewed.'],
         'review_context':review_context(r,key),'reviewers':list(REVIEWERS),'grading_source':'agent_consensus'}
    return base,reasons,sorted(expanded)

def apply(folder,keys):
    records=[json.loads(p.read_text()) for p in sorted((folder/'runs').glob('*.json'))]
    records=[r for r in records if r.get('task_id') in FINAL_IDS]
    human=json.loads((folder/'human-answer-reviews.json').read_text()) if (folder/'human-answer-reviews.json').exists() else {}
    reviews={w:{} for w in REVIEWERS}
    for w in REVIEWERS:
        for p in (folder/'agent-reviews'/w).glob('*.json'):
            d=json.loads(p.read_text());reviews[w][p.stem]=d
    grades,reasons,expanded=evaluate(records,keys,reviews,human)
    queue=[]
    for r in records:
        rid=digest(r['run_id'])[:20]
        if rid in reasons:
            item=packet(r,keys[r['task_id']]);item['review_reasons']=reasons[rid];queue.append(item)
    status={'method':VERSION,'sample_ids':sorted(sample_ids(records)),'expanded_tasks':expanded,'human_queue':sorted(queue,key=lambda p:p['review_id']),
            'agent_review_count':{w:len(d) for w,d in reviews.items()},'all_runs_terminal':len(records)==132 and all(r['state'] in {'completed','incomplete','failed','cancelled'} for r in records),
            'disclosure':'Two fresh Codex agents, separate contexts, same model family; errors may be correlated. Model/configuration/cost metadata hidden, but tool response shape can reveal design. Human audit is not full human grading.'}
    return grades,status
