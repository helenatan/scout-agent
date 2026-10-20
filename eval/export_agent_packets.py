"""Export immutable blinded packets only for settled, runtime-completed answers."""
import argparse
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from eval.final_grade import packet

def export(folder):
    keys=json.loads((ROOT/'eval/tasks/final/_keys.json').read_text());dest=folder/'agent-packets';dest.mkdir(exist_ok=True)
    n=0
    for path in sorted((folder/'runs').glob('*.json')):
        rec=json.loads(path.read_text())
        if rec.get('state')!='completed' or not rec['cost'].get('settled'):continue
        p=packet(rec,keys[rec['task_id']]);out=dest/(p['review_id']+'.json')
        text=json.dumps(p,indent=2,ensure_ascii=False)+'\n'
        if out.exists() and out.read_text()!=text:raise ValueError('Settled review packet changed: '+p['review_id'])
        if not out.exists():out.write_text(text);n+=1
    return n
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--label',required=True);args=p.parse_args();print('New packets:',export(ROOT/'eval/results'/args.label))
