"""Final-suite rubric grading with explicit, content-bound human adjudication.

Reference-key approval never approves generated answers. No heuristic awards a
semantic pass. Completed answers await review; failed runtime attempts are failures.
The review packet hides model/configuration/cost to reduce evaluator bias.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path

VERSION = 'final-grader-v1'
FINAL_IDS = frozenset(['R1','R2','R3','E1','E2','E3','C1','C2','C3','I1','I2'])
TERMINAL_FAILURES = {'failed', 'incomplete', 'cancelled'}

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()

def criteria(entry):
    rubric = entry['rubric']
    items = [{'id':f'output_{i+1}', 'text':text} for i,text in enumerate(rubric['essential_outputs'])]
    items += [{'id':'exact_key', 'text':'All reported counts, record sets, duplicate groups, labels and conclusions agree with the reference key. Equivalent wording is acceptable; count tolerance is zero.'},
              {'id':'evidence', 'text':rubric['evidence']},
              {'id':'limitations', 'text':'Material limitations are stated: '+json.dumps(rubric['limitations'],ensure_ascii=False)},
              {'id':'no_fail_conditions', 'text':'None of these failure conditions occurs: '+'; '.join(rubric['fail_conditions'])}]
    return items

def review_context(rec, entry):
    # Bind the answer, actual delivered evidence, task and rubric. Cost and model
    # are not displayed, but the complete original record is fingerprinted.
    return digest({'grader':VERSION,'record':rec,'reference':entry})

def packet(rec, entry):
    return {'review_id':digest(rec['run_id'])[:20], 'task_id':rec['task_id'],
            'context':review_context(rec,entry), 'task':rec['request']['task'],
            'answer':rec.get('answer'), 'evidence':rec.get('evidence', []),
            'delivered_evidence':[{k:v for k,v in e.items() if k not in {'quotable_text','quotable_spans'}} for e in rec.get('trace',{}).get('evidence',[])],
            'reference':entry['key'], 'criteria':criteria(entry)}

def grade_final(rec, entry, decision=None):
    base={'grader':VERSION,'invisible':False,'needs_review':False,'notes':[]}
    if rec.get('task_id') not in FINAL_IDS or not entry or not entry.get('rubric'):
        return {**base,'pass':None,'needs_review':True,'notes':['Missing final task rubric.']}
    if rec.get('state') in TERMINAL_FAILURES:
        return {**base,'pass':False,'notes':['Runtime failure: '+str(rec.get('outcome'))]}
    if rec.get('state')!='completed':
        return {**base,'pass':None,'needs_review':True,'notes':['Run is not terminal.']}
    if not rec.get('answer'):
        return {**base,'pass':False,'notes':['Completed run has no answer.']}
    pending={**base,'pass':None,'needs_review':True,'notes':['Generated answer requires human rubric review.']}
    if not decision:return pending
    required={c['id'] for c in criteria(entry)}
    checks=decision.get('checks',{})
    valid=(decision.get('context')==review_context(rec,entry)
           and decision.get('source')=='human'
           and isinstance(decision.get('reviewer'),str) and bool(decision['reviewer'].strip())
           and isinstance(checks,dict) and set(checks)==required
           and all(type(v) is bool for v in checks.values())
           and isinstance(decision.get('notes'),str) and bool(decision['notes'].strip()))
    if not valid:
        return {**pending,'notes':['Missing, incomplete, stale, or non-human adjudication; review required.']}
    passed=all(checks.values())
    return {**base,'pass':passed,'invisible':not passed and not rec.get('trace',{}).get('validation_history'),
            'notes':[decision['notes']], 'reviewer':decision['reviewer'],
            'review_context':decision['context'], 'failed_criteria':[c for c,v in checks.items() if not v]}

def build_packets(results:Path, keys:dict):
    packets=[]
    for path in sorted((results/'runs').glob('*.json')):
        rec=json.loads(path.read_text())
        if rec.get('state')=='completed' and rec.get('task_id') in FINAL_IDS:
            packets.append(packet(rec,keys[rec['task_id']]))
    # Stable blind ordering, independent of model/configuration/task launch order.
    return sorted(packets,key=lambda p:p['review_id'])
