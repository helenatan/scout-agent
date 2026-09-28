"""Launch the predeclared final matrix only from a complete, hash-bound freeze.

The freeze file is a reviewed artifact, not created automatically by this runner.
Use run_matrix.py --reconcile-only for an interrupted launch; never relaunch a label.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
import httpx
from eval.run_matrix import run_matrix,write_json
from eval.final_analysis import FINAL_TASKS

GATES={'verified_tasks','runtime_tests','development_rehearsal','independent_baseline_review','holdout_policy','grading_method','grader_validation','budget_and_models','qualification_rules'}

def validate_freeze(freeze, root=ROOT):
    if freeze.get('status')!='frozen' or freeze.get('gates')!={key:True for key in GATES}:
        raise ValueError('All freeze gates must be explicitly complete.')
    hashes=freeze.get('sha256',{})
    required={'scout/service/prompt.py','scout/service/engine.py','scout/service/validators.py','scout/service/evidence.py','scout/service/schemas.py','scout/service/budget.py','scout/service/providers.py','scout/service/store.py','scout/tools/pai_tools.py','scout/tools/mcp_server.py','scout/tools/mcp_client.py','eval/final_grade.py','eval/final_analysis.py','eval/report.py','eval/run_matrix.py','eval/run_final.py','eval/agent_grading.py','eval/grade.py','eval/grading/agent-policy-v1.json','eval/grading/reviewer-instructions-v1.md','eval/tasks/final/_keys.json','m0/data/pai_dump_2026-09-06.sqlite'} | {f'eval/tasks/final/{t}.json' for t in FINAL_TASKS}
    if not required<=set(hashes):raise ValueError('Freeze lacks required source/data/task fingerprints.')
    for relative,expected in hashes.items():
        p=(root/relative).resolve()
        if not p.is_relative_to(root.resolve()) or hashlib.sha256(p.read_bytes()).hexdigest()!=expected:
            raise ValueError(f'Frozen artifact changed: {relative}')
    if not isinstance(freeze.get('final_cap_usd'),(int,float)) or not 0<freeze['final_cap_usd']<=65:
        raise ValueError('Missing or invalid final spending cap.')
    if not freeze.get('qualification_source') or not freeze.get('grading_method'):
        raise ValueError('Missing declared qualification/grading method.')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--freeze',type=Path,required=True)
    parser.add_argument('--api',default='http://127.0.0.1:8011')
    parser.add_argument('--label',required=True)
    args=parser.parse_args()
    freeze=json.loads(args.freeze.read_text());validate_freeze(freeze)
    tasks=[json.loads((ROOT/f'eval/tasks/final/{t}.json').read_text()) for t in sorted(FINAL_TASKS)]
    out=ROOT/'eval/results'/args.label
    with httpx.Client(base_url=args.api,timeout=30) as client:
        h=client.get('/healthz');h.raise_for_status();health=h.json()
        if health.get('provider')!='anthropic' or health.get('tool_transport')!='mcp' or health.get('settings')!=freeze.get('service_settings'):
            raise ValueError('Service does not match the frozen provider/transport/settings.')
        # A dedicated empty store prevents historical runs or reservations changing the cap.
        if health['spend'].get('confirmed_usd',0) or health['spend'].get('committed_usd',0) or health['spend'].get('runs',0):
            raise ValueError('Use a dedicated empty final run store.')
        if health['spend']['cap_usd']!=freeze['final_cap_usd']:
            raise ValueError('Service cap differs from frozen final cap.')
        previous=[json.loads(p.read_text()) for p in (ROOT/'eval/results').glob('*/manifest.json')]
        used=sum(m.get('spent_usd',0)+m.get('committed_usd',0) for m in previous if m.get('provider')=='anthropic' and not m.get('label','').startswith('m0'))
        if used+freeze['final_cap_usd']>65+1e-9:
            raise ValueError('Final batch would exceed the $65 total study cap.')
        policy=json.loads((ROOT/'eval/grading/agent-policy-v1.json').read_text())
        experiment=dict(grading_policy=policy,kind='final',notes=['Frozen final evaluation; retain every launched attempt.'],qualification_source=freeze['qualification_source'],grading_method=freeze['grading_method'],freeze_sha256=hashlib.sha256(args.freeze.read_bytes()).hexdigest())
        manifest=run_matrix(client,out,tasks,label=args.label,models=['haiku','opus'],designs=['baseline','redesigned'],reps=3,cap_usd=freeze['final_cap_usd'],concurrency=1,experiment=experiment)
        write_json(out/'manifest.json',manifest)
    print(f"Launched {manifest['launched']}/132; confirmed ${manifest['spent_usd']:.4f}")

if __name__=='__main__':main()
