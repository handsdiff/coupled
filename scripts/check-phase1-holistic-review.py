#!/usr/bin/env python3
"""No-network regression of full-cohort scoring and preservation of pilot labels."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile

SCRIPT=Path(__file__).with_name('score-phase1-read-model-comparison.py')
spec=importlib.util.spec_from_file_location('holistic',SCRIPT)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def rejects(fn):
    try:
        fn()
    except AssertionError:
        return
    raise AssertionError('Expected rejection')


def main():
    source=Path('coupled-data/phase1-read-pipeline-factorial-20260907').resolve()
    prior=source/'holistic-v1'
    expected=m.read(prior/'scored/summary.json')
    with tempfile.TemporaryDirectory(prefix='phase1-holistic-check-') as tmp:
        folder=Path(tmp)/'review'
        with contextlib.redirect_stdout(io.StringIO()):
            m.prepare_targets(source,folder,prior)
            m.jsonl(folder/'substantiveness.new.jsonl',[])
            m.lock_classifications(folder)
            m.prepare(source,folder)
            grades=m.rows(folder/'judgments.imported.jsonl')
            m.jsonl(folder/'judgments.jsonl',grades)
            m.finish(folder)
        actual=m.read(folder/'scored/summary.json')
        assert (actual['cases'],actual['predictions'],actual['substantiveExamples'])==(100,400,61)
        assert actual['pairedPipelineComparisons']==expected['pairedPipelineComparisons']
        for arm,row in expected['models'].items():
            got=actual['models'][arm]
            for key in ('semanticPasses','substantiveExamples','semanticPassRate','passCases','deterministicSubstantive'):
                assert got[key]==row[key], (arm,key)
        # Changed target classifications cannot be hidden behind a new denominator.
        original=(folder/'substantiveness.jsonl').read_bytes()
        classes=m.rows(folder/'substantiveness.jsonl')
        classes[0]['reason']+=' changed'
        m.jsonl(folder/'substantiveness.jsonl',classes)
        rejects(lambda:m.verify_classification_lock(folder))
        (folder/'substantiveness.jsonl').write_bytes(original)
        # Imported grades cannot be changed under the same full-cohort review.
        original_grades=(folder/'judgments.jsonl').read_bytes()
        grades[0]['judgments']['A']['reason']+=' changed'
        m.jsonl(folder/'judgments.jsonl',grades)
        rejects(lambda:m.finish(folder))
        (folder/'judgments.jsonl').write_bytes(original_grades)
        # A duplicate must not silently replace a different annotation row.
        m.jsonl(folder/'judgments.jsonl',m.rows(folder/'judgments.jsonl')+[grades[0]])
        rejects(lambda:m.finish(folder))
    print('PASS: 100-case/400-prediction replay unchanged; classification tampering, prior-grade drift and duplicates rejected; no network')


if __name__=='__main__':
    main()
