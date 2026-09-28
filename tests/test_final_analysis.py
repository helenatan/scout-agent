import pytest
from eval.final_analysis import completeness, paired_economics, FINAL_TASKS

def test_missing_or_extra_cell_cannot_be_final():
    r=[{'task':t,'cfg':c,'rep':rep} for t in FINAL_TASKS for c in 'ABCD' for rep in range(3)]
    assert completeness(r)['complete']
    assert not completeness(r[:-1])['complete']
    assert not completeness(r+[r[0]])['complete']
    r[1]['rep']=r[0]['rep']
    assert not completeness(r)['complete']

def test_paired_ratio_recomputes_totals_and_retains_zero_success():
    runs=[{'task':t,'cfg':c,'pass':p,'cost':cost} for t in ['one','two'] for c,p,cost in [('C',1,1),('B',1,5)]]
    r=paired_economics(runs,'C','B',resamples=100)
    assert r['cost_per_success_ratio']==pytest.approx(.2)
    assert r['ties']==2 and r['cost_per_success_ratio_ci']==pytest.approx([.2,.2])
    for row in runs:
        if row['cfg']=='C':row['pass']=0
    r=paired_economics(runs,'C','B',resamples=100)
    assert r['undefined_ratio_resamples']==100 and r['cost_per_success_ratio'] is None
