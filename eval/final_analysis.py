"""Predeclared final completeness and paired economic analysis."""
import random
import statistics
from collections import Counter

FINAL_TASKS={'R1','R2','R3','E1','E2','E3','C1','C2','C3','I1','I2'}

def completeness(runs):
    cells=Counter((r['task'],r['cfg']) for r in runs)
    expected={(t,c) for t in FINAL_TASKS for c in 'ABCD'}
    identities=[(r['task'],r['cfg'],r.get('rep')) for r in runs]
    repetitions_ok=len(set(identities))==len(identities) and all(r.get('rep') in {0,1,2} for r in runs)
    return {'complete':set(cells)==expected and all(n==3 for n in cells.values()) and repetitions_ok,
            'repetitions_valid':repetitions_ok,
            'expected_attempts':132,'observed_attempts':len(runs),
            'incorrect_cells':[{'task':t,'config':c,'attempts':cells[t,c]} for t,c in sorted(expected|set(cells)) if (t,c) not in expected or cells[t,c]!=3]}

def paired_economics(runs,x,y,*,seed=0,resamples=5000,margin=.15,saving=.2):
    tasks=sorted({r['task'] for r in runs})
    by={(t,c):[r for r in runs if r['task']==t and r['cfg']==c] for t in tasks for c in (x,y)}
    paired=[t for t in tasks if by[t,x] and by[t,y]]
    diffs=[statistics.mean(r['pass'] for r in by[t,x])-statistics.mean(r['pass'] for r in by[t,y]) for t in paired]
    def ratio(sample):
        xs=[r for t in sample for r in by[t,x]];ys=[r for t in sample for r in by[t,y]]
        px=sum(r['pass'] for r in xs);py=sum(r['pass'] for r in ys)
        cx=sum(r['cost'] for r in xs);cy=sum(r['cost'] for r in ys)
        return (cx/px)/(cy/py) if px and py and cy else None
    rng=random.Random(seed);ratios=[];undefined=0
    for _ in range(resamples):
        v=ratio([rng.choice(paired) for _ in paired]) if paired else None
        if v is None:undefined+=1
        else:ratios.append(v)
    ratios.sort()
    interval=[ratios[int(.025*len(ratios))],ratios[min(len(ratios)-1,int(.975*len(ratios)))]] if len(ratios)>=20 else None
    point=ratio(paired) if paired else None
    return {'wins':sum(d>0 for d in diffs),'losses':sum(d<0 for d in diffs),'ties':sum(d==0 for d in diffs),
            'cost_per_success_ratio':point,'cost_per_success_ratio_ci':interval,
            'undefined_ratio_resamples':undefined,'noninferiority_margin':margin,
            'cost_saving_target':saving,'observed_cost_target_met':point is not None and point<=1-saving}
