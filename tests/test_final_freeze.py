import pytest
from eval.run_final import validate_freeze,GATES

def test_cannot_launch_with_partial_gates():
    with pytest.raises(ValueError,match='gates'):
        validate_freeze({'status':'frozen','gates':{'runtime_tests':True}})

def test_cannot_launch_without_fingerprints():
    with pytest.raises(ValueError,match='fingerprints'):
        validate_freeze({'status':'frozen','gates':{k:True for k in GATES}})
