#!/usr/bin/env python3
"""Check comparison projection, full context fidelity, filters and local HTTP.

No provider calls or browser automation. Real-corpus checks use indexed reads.
"""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from urllib.parse import urlsplit

script = Path(__file__).with_name('serve-phase1-model-comparison.py')
spec = importlib.util.spec_from_file_location('comparison_ui', script)
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--review')
    parser.add_argument('--repeatability', type=Path)
    parser.add_argument('--url', help='Optional running localhost UI, e.g. http://127.0.0.1:8788')
    args = parser.parse_args()
    data = ui.Comparison(args.run, args.review, args.repeatability)
    overview = data.overview()
    require = ui.require
    require(len(overview['examples']) == 377, 'Full cohort not exposed')
    require(sum(r['substantive'] for r in overview['examples']) == overview['summary']['substantiveExamples'], 'Scoring scope changed')
    if args.review == 'holistic-v2':
        require(overview['summary']['substantiveExamples'] == 231, 'Historical scoring scope changed')
    for model, name in ui.MODELS.items():
        for variant in ('old', 'new'):
            arm = model + '_' + variant
            expected = sorted(r['case'] for r in overview['examples'] if r['substantive'] and r['passes'][arm])
            current = overview['summary']['models'][name + ' / ' + variant]
            frozen = data.frozen_summary['models'][name + ' / ' + variant]
            require(current['passCases'] == expected and current['semanticPasses'] == len(expected), 'Overall score disagrees with cards/filters')
            for key in frozen.keys() - {'semanticPasses', 'semanticPassRate', 'passCases'}:
                require(current[key] == frozen[key], f'Unrelated metric changed: {key}')
        paired = overview['summary']['pairedPipelineComparisons'][name]
        old = set(overview['summary']['models'][name + ' / old']['passCases'])
        new = set(overview['summary']['models'][name + ' / new']['passCases'])
        require(paired['oldOnlyPassCases'] == sorted(old-new) and paired['newOnlyPassCases'] == sorted(new-old), 'Overall overlap stale')
    contexts = 0
    for example in overview['examples']:
        detail = data.case(example['case'])
        require(detail['target'] == example['target'], 'Displayed target mismatch')
        require(len(detail['predictions']) == 4, 'Four predictions required')
        for variant in ('old', 'new'):
            context = data.context(example['case'], variant)
            require(hashlib.sha256(context['modelInput'].encode()).hexdigest() == context['modelInputSHA256'], 'Prompt mismatch')
            for block in context['blocks']:
                require(block['serialized'] in context['modelInput'], 'Displayed context is not the model input')
            contexts += 1
    if data.repeatability:
        require(len(overview['repeatability']['cases']) == 70, 'Wrong repeated cohort')
        for case in overview['repeatability']['cases']:
            repeated = data.repeatability.case(case)
            require(set(repeated['arms']) == set(data.grades[case]), 'Repeatability arms changed')
            for arm, value in repeated['arms'].items():
                require([r['replicate'] for r in value['answers']] == [1, 2, 3], 'Repeated answer order wrong')
                require(value['answers'][0]['prediction'] == data.grades[case][arm]['prediction'], 'Original response altered')
                require(value['answers'][0]['semanticPass'] == data.case(case)['predictions'][arm]['semanticPass'], 'Original answer grade differs between views')
                if not repeated['progress']['scoringComplete']:
                    require(value['successes'] is None and all(r['semanticPass'] is None for r in value['answers'][1:]), 'Unreviewed answers falsely graded')
                else:
                    require(value['successes'] == sum(a['semanticPass'] for a in value['answers']), 'Repeated score count differs')
                    require(value['freshSuccesses'] == sum(a['semanticPass'] for a in value['answers'][1:]), 'Fresh score count differs')
        human_file = ui.scored_directory(args.repeatability / 'review-v1') / 'human-adjudications.json'
        if human_file.exists():
            for decision in ui.read(human_file)['decisions']:
                model = next(k for k, v in ui.MODELS.items() if v == decision['model'])
                answer = data.repeatability.case(decision['case'])['arms'][model + '_' + decision['variant']]['answers'][decision['replicate'] - 1]
                require(answer['semanticPass'] == (decision['decision'] == 'pass') and answer['reviewStatus'] == 'human_adjudication', 'Human judgment not displayed')
                if decision['replicate'] == 1:
                    require(data.case(decision['case'])['predictions'][model + '_' + decision['variant']]['semanticPass'] == answer['semanticPass'], 'Human original grade missing from overall comparison')
        if data.repeatability.summary:
            for model, name in ui.MODELS.items():
                for variant in ('old', 'new'):
                    counts = [data.repeatability.case(c)['arms'][model + '_' + variant]['successes'] for c in overview['repeatability']['cases']]
                    a = overview['repeatability']['summary']['arms'][name + ' / ' + variant]
                    require(a['countAllThree'] == counts.count(3) and a['countAnyOne'] == sum(c>0 for c in counts), 'Repeated overall counts stale')
                    require(a['singleAnswerPassRate'] == sum(counts)/(3*len(counts)), 'Repeated answer rate stale')

    # Recompute from one answer's changed decision; never increment cached totals.
    fixture_grades = copy.deepcopy(data.frozen_grades)
    case = next(c for c,g in fixture_grades.items() if g['astra_new']['substantive'] and not g['astra_new']['semanticPass'])
    fixture_grades[case]['astra_new']['semanticPass'] = True
    before = copy.deepcopy(data.frozen_summary)
    updated = ui.current_summary(before, fixture_grades)
    require(updated['models'][ui.MODELS['astra']+' / new']['semanticPasses'] == before['models'][ui.MODELS['astra']+' / new']['semanticPasses'] + 1, 'Original correction not counted exactly once')
    require(before == data.frozen_summary and data.frozen_grades[case]['astra_new']['semanticPass'] is False, 'Baseline mutated by scoring projection')
    require(ui.current_summary(updated, fixture_grades) == updated, 'Refreshing double-counts corrections')

    with tempfile.TemporaryDirectory(prefix='coupled-ui-check-') as tmp:
        fixture = Path(tmp) / 'index.jsonl'
        fixture.write_text('{"id":"one","value":"<script>literal data</script>"}\n')
        index = ui.Index(fixture, lambda row: row['id'], ui.sha(fixture))
        require(index.get('one')['value'] == '<script>literal data</script>', 'Text changed during indexing')
        try:
            ui.Index(fixture, lambda row: row['id'], '0' * 64)
            raise AssertionError('Tampered source accepted')
        except ValueError:
            pass
        fixture.write_text('{"id":"same"}\n{"id":"same"}\n')
        try:
            ui.Index(fixture, lambda row: row['id'], ui.sha(fixture))
            raise AssertionError('Duplicate source accepted')
        except ValueError:
            pass

    js_path = script.with_name('phase1-comparison-ui') / 'app.js'
    require('innerHTML' not in js_path.read_text(), 'Untrusted captured text must not be injected as HTML')
    node_check = r'''
      const assert = require('node:assert/strict');
      const fs = require('node:fs');
      const {filterExamples, adjacentCase} = require(process.argv[1]);
      const examples = JSON.parse(fs.readFileSync(0, 'utf8'));
      assert.equal(filterExamples(examples, {}).length, 377);
      assert.equal(filterExamples(examples, {eligibility:'substantive'}).length, examples.filter(r=>r.substantive).length);
      assert.equal(filterExamples(examples, {eligibility:'excluded'}).length, examples.filter(r=>!r.substantive).length);
      assert.deepEqual(filterExamples(examples, {outcome:'rescored'}), examples.filter(r=>r.scoringChanged));
      assert.deepEqual(filterExamples(examples, {search:'#338'}).map(r=>r.case), [338]);
      assert.equal(filterExamples(examples, {search:'not in the corpus 123xyq'}).length, 0);
      assert.equal(adjacentCase(examples, 1, -1), null);
      assert.equal(adjacentCase(examples, 377, 1), null);
      assert.equal(adjacentCase(examples, 1, 1), 2);
      const mismatches = filterExamples(examples, {outcome:'pipeline'});
      assert.ok(mismatches.length > 0);
      for (const r of mismatches) assert.ok(r.passes.astra_old !== r.passes.astra_new || r.passes.sol_old !== r.passes.sol_new);
      for (const r of filterExamples(examples, {outcome:'astra_only'})) assert.ok((r.passes.astra_old || r.passes.astra_new) && !r.passes.sol_old && !r.passes.sol_new);
      const app = examples[0].application;
      assert.ok(filterExamples(examples, {app}).every(r=>r.application===app));
      const chosen = examples.find(r=>r.target.length>30);
      assert.ok(filterExamples(examples, {search:chosen.target.slice(0,25)}).some(r=>r.case===chosen.case));
      examples[0].repeatabilitySelected = true;
      assert.deepEqual(filterExamples(examples, {outcome:'repeated'}).map(r=>r.case), [examples[0].case]);
      console.log('PASS: full-cohort search, eligibility, comparison filters and navigation');
    '''
    subprocess.run(['node', '--check', str(js_path)], check=True)
    subprocess.run(['node', '-e', node_check, str(js_path.resolve())], input=json.dumps(overview['examples']), text=True, check=True)

    http_checks = 0
    if args.url:
        parts = urlsplit(args.url)
        require(parts.scheme == 'http' and parts.hostname in ('localhost', '127.0.0.1') and parts.port, 'Only explicit localhost URLs allowed')
        base = args.url.rstrip('/')
        client = build_opener(ProxyHandler({}))
        def request(path, host=None, method='GET'):
            req = Request(base + path, headers={'Host': host} if host else {}, method=method)
            try:
                with client.open(req, timeout=15) as response:
                    return response.status, response.headers, response.read()
            except HTTPError as error:
                return error.code, error.headers, error.read()
        for path, kind in [('/', 'text/html'), ('/app.js', 'text/javascript'), ('/style.css', 'text/css'), ('/report', 'text/plain')]:
            status, headers, body = request(path)
            require(status == 200 and kind in headers['Content-Type'] and body, f'Failed asset: {path}')
            require(headers['Cache-Control'] == 'no-store', 'Private data must not be cached')
            require("frame-ancestors 'none'" in headers['Content-Security-Policy'], 'Missing framing guard')
            http_checks += 1
        status, _, body = request('/api/overview')
        served = json.loads(body)
        require(status == 200 and {k:v for k,v in served.items() if k != 'repeatability'} == {k:v for k,v in overview.items() if k != 'repeatability'}, 'Served overview differs from source')
        if data.current_scoring_hash:
            status, _, body = request('/report')
            require(status == 200 and body.decode() == data.report(), 'Full report still serves old scores')
            http_checks += 1
        if args.repeatability:
            require(served['repeatability']['cases'] == overview['repeatability']['cases'], 'Served repeatability cohort differs')
            require(served['repeatability']['summary'] == overview['repeatability']['summary'], 'Served repeated summary differs')
            for case in [7,108,109,132,133,136,204,260,338,354]:
                status, _, body = request(f'/api/cases/{case}/repeats')
                require(status == 200 and json.loads(body) == data.repeatability.case(case), 'Repeatability route differs from scored source')
                http_checks += 1
        http_checks += 1
        for number in [1, 16, 193, 338, 377]:
            status, _, body = request(f'/api/cases/{number}')
            require(status == 200 and json.loads(body) == data.case(number), 'Served prediction differs from source')
            http_checks += 1
            for variant in ('old', 'new'):
                status, _, body = request(f'/api/cases/{number}/context/{variant}')
                require(status == 200 and json.loads(body) == data.context(number, variant), 'Served context differs from source')
                http_checks += 1
        for path, host, method, expected in [('/api/overview', 'attacker.invalid', 'GET', 403),
                ('/api/cases/9999', None, 'GET', 404), ('/../../.env', None, 'GET', 404),
                ('/api/cases/1/context/other', None, 'GET', 404), ('/api/cases/1', None, 'POST', 501)]:
            require(request(path, host, method)[0] == expected, f'Unsafe route: {path}')
            http_checks += 1
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1048576 if sys.platform == 'darwin' else 1024)
    print(json.dumps({'status': 'passed', 'examples': 377, 'predictionMappings': 1508,
                      'fullPromptContextsChecked': contexts, 'httpChecks': http_checks,
                      'peakCheckProcessMiB': round(peak, 1), 'providerCalls': 0, 'datasetWrites': 0}))


if __name__ == '__main__':
    main()
