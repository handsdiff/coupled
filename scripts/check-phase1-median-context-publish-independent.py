#!/usr/bin/env python3
"""Synthetic, no-network prepare/publish checks for the median-context study.

An independent supplement to check-phase1-median-context-analysis.py. It never
reads experimental predictions or changes frozen run artifacts.
"""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory


SPEC = importlib.util.spec_from_file_location(
    'median_context_analysis', Path(__file__).with_name('analyze-phase1-median-context.py')
)
analysis = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analysis)


def fixture(root, prior_root):
    (root / 'data').mkdir(parents=True)
    (root / 'code').mkdir()
    requests, predictions, cases, prior = [], [], [], []
    for case in range(1, 70):
        for arm in analysis.ARMS:
            ordinal = len(requests) + 1
            blocked = case == 41 and arm == 'time_images'
            request = {
                'sampleID': f'{case}-{arm}', 'case': case, 'arm': arm,
                'requestOrdinal': ordinal, 'partsSHA256': f'parts-{case}-{arm}',
                'exampleID': str(case), 'limits': ['capacity'] if blocked else [],
            }
            requests.append(request)
            cases.append({
                'case': case, 'arm': arm, 'query': f'query-{case}',
                'target': f'target-{case}', 'exampleID': str(case),
                'limits': request['limits'], 'historySpanMinutes': 1,
            })
            if blocked:
                continue
            prediction = {
                **request, 'prediction': f'answer-{case}', 'validCompletion': True,
                'timing': {'dispatchToCompletionSeconds': 1},
                'usage': {'input_tokens': 2, 'output_tokens': 1},
                'apiEquivalentCostUSD': 0.001,
            }
            if len(prior) < 82:
                prediction['reuse'] = {'source': 'synthetic-prior'}
                prior.append({
                    **prediction, 'target': f'target-{case}', 'decision': 'pass',
                    'reason': f'locked-reason-{ordinal}',
                })
            folder = root / 'results' / f'{ordinal:04d}'
            folder.mkdir(parents=True)
            analysis.save(folder / 'result.json', prediction)
            predictions.append(prediction)
    analysis.save_rows(root / 'requests.jsonl', requests)
    analysis.save_rows(root / 'predictions.jsonl', predictions)
    analysis.save(root / 'data/cases.json', cases)
    analysis.save(root / 'plan.json', {
        'version': 'phase1-median-context-executor-v2',
        'artifactsSHA256': {}, 'codeSHA256': {},
        'scoringContract': {'passBar': 'Intended thought; compatible elaboration.'},
    })
    analysis.save(root / 'audit.json', {
        'status': 'complete', 'planSHA256': analysis.file_hash(root / 'plan.json'),
        'predictionsSHA256': analysis.file_hash(root / 'predictions.jsonl'),
        'completed': 344, 'capacityBlocked': [{'case': 41, 'arm': 'time_images'}],
    })
    prior_root.mkdir()
    analysis.save_rows(prior_root / 'judgments.jsonl', prior)
    analysis.save(prior_root / 'completion.json', {
        'artifactsSHA256': {
            'judgments.jsonl': analysis.file_hash(prior_root / 'judgments.jsonl')
        }
    })
    return prior


def check():
    with TemporaryDirectory(prefix='coupled-median-publish-independent-') as td:
        base = Path(td)
        root, prior_root, review = base / 'run', base / 'prior', base / 'review'
        prior = fixture(root, prior_root)
        with contextlib.redirect_stdout(io.StringIO()):
            analysis.prepare(root, review, [prior_root])
        blind = list(analysis.rows(review / 'blinded.jsonl'))
        fixed = list(analysis.rows(review / 'fixed-grades.jsonl'))
        assert len(blind) == 262 and len(fixed) == 82
        assert {r['reason'] for r in fixed} == {r['reason'] for r in prior}
        grades = [
            {'blindIndex': r['blindIndex'], 'decision': 'pass', 'reason': 'Intended thought.'}
            for r in blind
        ]
        for name in ('primary', 'independent'):
            analysis.save_rows(review / f'{name}.jsonl', grades)
        analysis.save_rows(review / 'resolutions.jsonl', [])
        analysis.save(review / 'approval.json', {'approved': True})
        with contextlib.redirect_stdout(io.StringIO()):
            analysis.publish(
                root, review, review / 'primary.jsonl', review / 'independent.jsonl',
                review / 'resolutions.jsonl', review / 'approval.json', base / 'scored',
            )
        summary = analysis.load(base / 'scored/summary.json')
        judgments = list(analysis.rows(base / 'scored/judgments.jsonl'))
        assert len(judgments) == 344 and all(r['semanticPass'] for r in judgments)
        assert summary['arms']['time_images']['n'] == 68
        assert all(summary['arms'][a]['n'] == 69 for a in analysis.ARMS if a != 'time_images')
        assert all(p['n'] == (68 if 'time_images' in k else 69) for k, p in summary['paired'].items())
        assert not any(r['case'] == 41 and r['arm'] == 'time_images' for r in judgments)
        old = {r['sampleID']: r for r in prior}
        reused = [r for r in judgments if r.get('reuse')]
        assert len(reused) == 82
        assert all((r['decision'], r['reason']) ==
                   (old[r['sampleID']]['decision'], old[r['sampleID']]['reason']) for r in reused)

        # Case 17 has both locked and new identical answers. Even two agreeing
        # raters cannot contradict the locked decision for the same query/target.
        candidate = next(r for r in blind if r['case'] == 17)
        bad = [
            {**r, 'decision': 'fail'} if r['blindIndex'] == candidate['blindIndex'] else r
            for r in grades
        ]
        for name in ('bad-primary', 'bad-independent'):
            analysis.save_rows(review / f'{name}.jsonl', bad)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                analysis.publish(
                    root, review, review / 'bad-primary.jsonl', review / 'bad-independent.jsonl',
                    review / 'resolutions.jsonl', review / 'approval.json', base / 'bad-scored',
                )
        except AssertionError as error:
            assert 'Identical answer' in str(error)
        else:
            raise AssertionError('Contradictory identical-answer grade accepted')
    print(json.dumps({
        'status': 'passed', 'syntheticOnly': True, 'providerCalls': 0,
        'checks': ['prepare 262 new plus 82 fixed', 'all 82 exact prior grades retained',
                   'publish 344 predictions', 'capacity exception unscored',
                   '68 image pairs and 69 text pairs', 'identical-answer conflict rejected'],
    }))


if __name__ == '__main__':
    check()
