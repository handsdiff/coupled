#!/usr/bin/env python3
"""Read-only, localhost-only review of a completed four-arm comparison.

No provider calls, rebuilding, grading, collection changes, or dataset writes.
Large prompt/cohort JSONL files are indexed and fetched one row at a time.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import copy
import hashlib
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from phase1_subscription_output import decode, interpret
from phase1_repeatability_review import scored_directory

ASSETS = Path(__file__).with_name('phase1-comparison-ui')
MODELS = {'astra': 'chatgpt/gpt-6-astra', 'sol': 'chatgpt/gpt-5.6-sol'}


def read(path):
    return json.loads(path.read_text())


def rows(path):
    with path.open() as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1048576), b''):
            digest.update(chunk)
    return digest.hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def current_summary(baseline, grades):
    """Recompute semantic totals from current original-answer grades only."""
    summary = copy.deepcopy(baseline)
    spec = importlib.util.spec_from_file_location('comparison_score', Path(__file__).with_name('score-phase1-read-model-comparison.py'))
    scorer = importlib.util.module_from_spec(spec); spec.loader.exec_module(scorer)
    n = summary['substantiveExamples']
    for model, name in MODELS.items():
        matched = {}
        for variant in ('old', 'new'):
            eligible = {case: row[model + '_' + variant] for case, row in grades.items()
                        if row[model + '_' + variant]['substantive']}
            require(len(eligible) == n, 'Current score denominator differs')
            passes = sorted(case for case, r in eligible.items() if r['semanticPass'])
            a = summary['models'][name + ' / ' + variant]
            a.update(semanticPasses=len(passes), semanticPassRate=len(passes) / n, passCases=passes)
            matched[variant] = {case: int(r['semanticPass']) for case, r in eligible.items()}
        old, new = matched['old'], matched['new']
        require(old.keys() == new.keys(), 'Unpaired current scoring')
        differences = [new[i] - old[i] for i in sorted(old)]
        summary['pairedPipelineComparisons'][name] = {
            'oldOnlyPassCases': [i for i in sorted(old) if old[i] and not new[i]],
            'newOnlyPassCases': [i for i in sorted(old) if new[i] and not old[i]],
            'newMinusOldRate': sum(differences) / n,
            'pairedBootstrap95PercentInterval': scorer.paired_interval(differences)}
    return summary


class Index:
    def __init__(self, path, key, expected_hash):
        self.path, self.offsets, self.metadata = path, {}, {}
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            while True:
                offset = handle.tell()
                line = handle.readline()
                if not line:
                    break
                digest.update(line)
                if not line.strip():
                    continue
                row = json.loads(line)
                ident = key(row)
                require(ident not in self.offsets, f'Duplicate index key in {path.name}')
                self.offsets[ident] = offset
                self.metadata[ident] = {k: row[k] for k in (
                    'targetBeganAt', 'targetAvailableAt', 'sessionID', 'originalTargetNumber',
                    'referenceInputTokens', 'modelInputSHA256', 'promptID') if k in row}
                if 'retainedBlocks' in row:
                    self.metadata[ident]['eventCounts'] = dict(Counter(b['kind'] for b in row['retainedBlocks']))
                    self.metadata[ident]['retainedEvents'] = len(row['retainedBlocks'])
        require(digest.hexdigest() == expected_hash, f'Hash mismatch: {path.name}')

    def get(self, key):
        with self.path.open('rb') as handle:
            handle.seek(self.offsets[key])
            return json.loads(handle.readline())


class Comparison:
    def __init__(self, root, review_name=None, repeatability=None):
        self.root = root.resolve()
        current = None
        if review_name is None:
            if (root / 'current-scoring.json').exists():
                current = read(root / 'current-scoring.json')
                review_name = current['reviewDirectory']
            else:
                review_name = 'holistic-v2'
        require(re.fullmatch(r'holistic-v[0-9]+', review_name), 'Invalid review version')
        self.review = review = root / review_name
        if current:
            require(sha(review / 'scoring-revision.json') == current['revisionSHA256'], 'Current scoring pointer changed')
        scored = review / 'scored'
        completion = read(root / 'completion.json')
        require(completion['status'] == 'complete_inference_scoring_and_audit', 'Run is not finalized')
        for name, digest in completion['artifactsSHA256'].items():
            require(sha(root / name) == digest, f'Completed artifact changed: {name}')
        self.reconciliation = None
        self.changes = {}
        if review_name != 'holistic-v2':
            self.reconciliation = read(review / 'scoring-revision.json')
            require(self.reconciliation['status'] == 'reviewed_and_audited', 'Scoring revision is not finalized')
            for name, digest in self.reconciliation['artifactsSHA256'].items():
                require(sha(review / name) == digest, f'Revised scoring artifact changed: {name}')
            self.changes = {r['case']: r for r in read(review / 'changes.json')['cases']}
        self.summary = read(scored / 'summary.json')
        self.plan = read(root / 'frozen/plan.json')
        self.supplement = read(scored / 'supplement.json')
        review_plan = read(review / 'review-plan.json')
        self.review_version = review_plan['version']
        for name, digest in review_plan['blindArtifactSHA256'].items():
            require(sha(review / name) == digest, f'Review source changed: {name}')
        hashes = read(root / 'frozen/artifact-hashes.json')
        require(sha(root / 'frozen/plan.json') == hashes['plan.json'], 'Plan hash mismatch')
        self.cohort = Index(root / 'frozen/cohort.jsonl', lambda r: r['exampleID'], hashes['cohort.jsonl'])
        self.prompts = Index(root / 'frozen/prompts.jsonl', lambda r: (r['exampleID'], r['variant']), hashes['prompts.jsonl'])
        self.targets = {r['case']: {k: v for k, v in r.items() if k != 'query'} for r in rows(review / 'targets.jsonl')}
        self.grades = defaultdict(dict)
        for row in rows(scored / 'judgments.unblinded.jsonl'):
            model = next((key for key, value in MODELS.items() if value == row['model']), None)
            require(model is not None and row['variant'] in ('old', 'new'), 'Unexpected comparison arm')
            arm = model + '_' + row['variant']
            require(arm not in self.grades[row['case']], 'Duplicate prediction arm')
            target = self.targets[row['case']]
            require(row['exampleID'] == target['exampleID'] and row['target'] == target['target'], 'Target/prediction join mismatch')
            self.grades[row['case']][arm] = row
        # Reused predictions and revised judgments are independent provenance.
        self.imported = {r['case'] for r in rows(root / 'holistic-v2/judgments.imported.jsonl')}
        self.examples = []
        expected_arms = {m + '_' + v for m in MODELS for v in ('old', 'new')}
        require(len(self.targets) == self.summary['cases'] == completion['targets'], 'Case count mismatch')
        for case, target in sorted(self.targets.items()):
            grades = self.grades[case]
            require(set(grades) == expected_arms, f'Incomplete four-arm case {case}')
            meta = self.cohort.metadata[target['exampleID']]
            substantive = next(iter(grades.values()))['substantive']
            require(all(g['substantive'] == substantive for g in grades.values()), 'Eligibility differs between arms')
            for variant in ('old', 'new'):
                require((target['exampleID'], variant) in self.prompts.offsets, 'Missing packed prompt')
            self.examples.append({
                'case': case, 'exampleID': target['exampleID'], 'originalTargetNumber': target['originalTargetNumber'],
                'application': target['destination'].get('application', 'Unknown'),
                'destination': target['destination'], 'beganAt': meta['targetBeganAt'],
                'day': meta['targetBeganAt'][:10], 'target': target['target'],
                'substantive': substantive, 'reused': case in self.imported,
                'scoringChanged': case in self.changes,
                'passes': {a: g['semanticPass'] for a, g in grades.items()},
                'searchText': '\n'.join([str(case), target['exampleID'], target['target'],
                                          *[g['prediction'] for g in grades.values()]]).lower(),
            })
        require(sum(r['substantive'] for r in self.examples) == self.summary['substantiveExamples'], 'Substantive denominator mismatch')
        for model, name in MODELS.items():
            for variant in ('old', 'new'):
                count = sum(r['passes'][model + '_' + variant] for r in self.examples)
                require(count == self.summary['models'][name + ' / ' + variant]['semanticPasses'], 'Score mismatch')
        self.repeatability = RepeatedAnswers(repeatability, self) if repeatability else None
        self.frozen_grades = copy.deepcopy(self.grades)
        self.frozen_summary = copy.deepcopy(self.summary)
        self.frozen_changes = copy.deepcopy(self.changes)
        self.current_scoring_hash = None
        self.refresh_scoring()

    def refresh_scoring(self):
        """Use one final answer grade in cards, filters, totals and reports.

        Replicate 1 updates the full-cohort score; fresh answers belong only to
        the separate selected-case repeatability summary. Frozen files stay intact.
        """
        if not self.repeatability:
            return
        self.repeatability.refresh_scores()
        if self.repeatability.scored is None or self.current_scoring_hash == self.repeatability.scoring_hash:
            return
        self.grades = copy.deepcopy(self.frozen_grades)
        self.changes = copy.deepcopy(self.frozen_changes)
        for case, row in self.repeatability.scored.items():
            for model, name in MODELS.items():
                for variant in ('old', 'new'):
                    score = row['models'][name][variant]['answers'][0]
                    current = self.grades[case][model + '_' + variant]
                    require(score['replicate'] == 1 and score['prediction'] == current['prediction']
                            and score['exampleID'] == current['exampleID'], 'Original grade binding differs')
                    if current['semanticPass'] != score['semanticPass']:
                        change = self.changes.setdefault(case, {'case': case, 'judgments': []})
                        change['judgments'].append({'arm': model + '_' + variant, 'before': current['decision'],
                            'after': score['decision'], 'reason': score['reason']})
                    for key in ('semanticPass', 'decision', 'reason', 'reviewOrigin', 'previousScoring'):
                        if key in score:
                            current[key] = score[key]
        for example in self.examples:
            example['passes'] = {arm: g['semanticPass'] for arm, g in self.grades[example['case']].items()}
            example['scoringChanged'] = example['case'] in self.changes
        self.summary = current_summary(self.frozen_summary, self.grades)
        self.summary['authority'] = 'reviewer_reconciled_with_user_judgments'
        self.summary['finalAnswerScoringSHA256'] = self.repeatability.scoring_hash
        self.current_scoring_hash = self.repeatability.scoring_hash

    def overview(self):
        self.refresh_scoring()
        return {'examples': self.examples, 'summary': self.summary, 'models': MODELS,
                'pipelines': self.plan['pipelineArms'], 'runName': self.root.name,
                'contextTokens': self.plan['contextBudget']['tokens'],
                'instruction': self.plan['contextBudget']['instruction'],
                'failures': self.supplement['failedProviderAttempts'],
                'scoringReview': self.review_version, 'scoringRevision': self.reconciliation,
                'currentJudgmentsApplied': self.current_scoring_hash is not None,
                'repeatability': self.repeatability.overview() if self.repeatability else None}

    def case(self, number):
        self.refresh_scoring()
        target = self.targets[number]
        raw = self.cohort.get(target['exampleID'])
        query = json.loads(raw['query']) if isinstance(raw['query'], str) else raw['query']
        return {**target, 'beganAt': raw['targetBeganAt'], 'availableAt': raw['targetAvailableAt'],
                'query': query, 'episode': raw['episode'], 'predictions': self.grades[number],
                'substantive': next(iter(self.grades[number].values()))['substantive'],
                'eligibilityReason': next(iter(self.grades[number].values()))['substantivenessReason'],
                'reused': number in self.imported,
                'scoringChange': self.changes.get(number),
                'context': {v: self.prompts.metadata[(target['exampleID'], v)] for v in ('old', 'new')}}

    def report(self):
        self.refresh_scoring()
        if self.current_scoring_hash is None:
            return (self.review / 'scored/report.md').read_text()
        summary = self.summary
        lines = ['# Current Astra / Sol scores', '', 'Reviewer reconciliation and your final answer judgments are applied. Earlier scoring files are preserved.', '',
                 f"## Original answers — full dataset ({summary['substantiveExamples']} scored targets)", '',
                 '| Model / pipeline | Passes | Rate |', '|---|---:|---:|']
        for name in MODELS.values():
            for variant in ('old', 'new'):
                a = summary['models'][name + ' / ' + variant]
                lines.append(f"| {name.removeprefix('chatgpt/')} / {variant} | {a['semanticPasses']}/{a['substantiveExamples']} | {a['semanticPassRate']:.2%} |")
        repeat = self.repeatability.summary
        lines += ['', f"## Repeated answers — {repeat['cases']} selected cases", '',
                  'This is a selected-case diagnostic, not full-dataset accuracy. Original + two fresh answers per case.', '',
                  '| Model / pipeline | All 3 pass | Any of 3 pass | Individual-answer pass rate |', '|---|---:|---:|---:|']
        for name in MODELS.values():
            for variant in ('old', 'new'):
                a = repeat['arms'][name + ' / ' + variant]
                lines.append(f"| {name.removeprefix('chatgpt/')} / {variant} | {a['countAllThree']}/{a['cases']} | {a['countAnyOne']}/{a['cases']} | {a['singleAnswerPassRate']:.2%} |")
        lines += ['', 'Only the original answer contributes to the full-dataset score. Fresh-answer grades do not replace or add samples to that denominator.', '',
                  'Predictions, prompts, timing, costs and eligibility are unchanged. No new provider calls.', '',
                  f'Final answer scoring SHA256: `{self.current_scoring_hash}`', '']
        return '\n'.join(lines)

    def context(self, number, variant):
        target = self.targets[number]
        row = self.prompts.get((target['exampleID'], variant))
        require(hashlib.sha256(row['modelInput'].encode()).hexdigest() == row['modelInputSHA256'], 'Prompt content hash mismatch')
        blocks = []
        for block in row['retainedBlocks']:
            require(hashlib.sha256(block['serialized'].encode()).hexdigest() == block['serializedSHA256'], 'Context block hash mismatch')
            blocks.append({**block, 'event': json.loads(block['serialized'])})
        return {'case': number, 'variant': variant, 'promptID': row['promptID'],
                'referenceInputTokens': row['referenceInputTokens'], 'blocks': blocks,
                'modelInput': row['modelInput'], 'modelInputSHA256': row['modelInputSHA256'],
                'nativePreambleSeparate': True}


class RepeatedAnswers:
    """Read completed atomic result files; never infer grades from answer text."""
    def __init__(self, root, comparison):
        self.root, self.comparison = root.resolve(), comparison
        self.plan = read(self.root / 'plan.json')
        self.plan_hash = sha(self.root / 'plan.json')
        require(self.plan['source'] == str(comparison.root), 'Repeatability source mismatch')
        for name, digest in self.plan['artifactsSHA256'].items():
            require(sha(self.root / name) == digest, f'Repeatability input changed: {name}')
        self.selection = {r['case']: r for r in read(self.root / 'selection.json')['cases']}
        self.requests = defaultdict(dict)
        ids = {r['exampleID']: r['case'] for r in rows(self.root / 'targets.jsonl')}
        for r in rows(self.root / 'requests.jsonl'):
            model = next(k for k, v in MODELS.items() if v == r['model'])
            key = (model + '_' + r['variant'], r['replicate'])
            require(key not in self.requests[ids[r['exampleID']]], 'Duplicate repeated request')
            self.requests[ids[r['exampleID']]][key] = r
        require(set(self.requests) == set(self.selection), 'Repeatability case mismatch')
        for r in rows(self.root / 'baseline.jsonl'):
            model = next(k for k, v in MODELS.items() if v == r['model'])
            require(r == comparison.grades[r['case']][model + '_' + r['variant']], 'Original grade changed')
        self.scored = None; self.scoring_hash = None; self.scoring_label = None; self.summary = None

    def refresh_scores(self):
        folder = scored_directory(self.root / 'review-v1')
        marker = folder / 'completion.json'
        if not marker.exists():
            return
        digest = sha(marker)
        if digest == self.scoring_hash:
            return
        completed = read(marker)
        require(completed['status'] in ('reviewer_agreed_and_audited', 'reviewer_reconciled_with_human_adjudications'), 'Repeatability scores not approved')
        require(sha(folder.parent / 'review-plan.json') == completed['reviewPlanSHA256'], 'Review plan changed')
        rp = read(folder.parent / 'review-plan.json')
        require(rp['runPlanSHA256'] == self.plan_hash, 'Scores for a different repeatability run')
        require(sha(folder.parent / 'judgments.jsonl') == completed['judgmentsSHA256'], 'Judgments changed')
        for name, expected in completed['artifactsSHA256'].items():
            require(sha(folder / name) == expected, f'Repeatability score artifact changed: {name}')
        self.scored = {r['case']: r for r in rows(folder / 'cases.jsonl')}
        self.scored_folder = folder
        self.summary = read(folder / 'summary.json')
        require(set(self.scored) == set(self.selection), 'Incomplete scored cases')
        self.scoring_hash = digest
        self.scoring_label = ('Reviewer reconciled + your judgments' if completed['status'] == 'reviewer_reconciled_with_human_adjudications'
                              else 'Reviewer-agreed scoring')

    def overview(self):
        self.refresh_scores()
        progress = read(self.root / 'progress.json') if (self.root / 'progress.json').exists() else {'status': 'starting', 'completed': 0}
        return {'cases': sorted(self.selection), 'plannedFreshAnswers': self.plan['freshRequests'],
                'completedFreshAnswers': progress.get('completed', progress.get('recorded', 0)),
                'status': 'scoring_complete' if self.scored else progress['status'],
                'scoringComplete': self.scored is not None, 'scoringLabel': self.scoring_label,
                'summary': self.summary}

    def case(self, number):
        selected = self.selection[number]
        self.refresh_scores()
        arms = {}
        for model in MODELS:
            for variant in ('old', 'new'):
                arm = model + '_' + variant
                original = self.comparison.grades[number][arm]
                answers = [{**original, 'replicate': 1, 'status': 'recorded', 'reviewStatus': 'original_scored'}]
                for replicate in (2, 3):
                    request = self.requests[number][(arm, replicate)]
                    folder = self.root / 'results' / f"{request['requestOrdinal']:04d}"
                    result_file = folder / 'result.json'
                    if not result_file.exists():
                        answers.append({'replicate': replicate, 'status': 'pending', 'semanticPass': None})
                        continue
                    result = read(result_file)
                    require(all(result[k] == request[k] for k in request if k != 'status') and result['status'] == 'recorded', 'Fresh answer mapping changed')
                    attempts = sorted(folder.glob('attempt-*'))
                    latest = attempts[-1]
                    require(read(latest / 'inflight.json')['binding'] == {'planSHA256': self.plan_hash, 'request': request}, 'Fresh answer plan mismatch')
                    require(sha(latest / 'response.body') == result['timing']['bodySHA256'], 'Fresh answer wire changed')
                    wire = (latest / 'response.body').read_bytes()
                    observed = interpret(wire, request['model'])
                    response, _ = decode(wire)
                    require(all(observed[k] == result[k] for k in observed) and response['id'] == result['responseID'], 'Fresh answer differs from observed response')
                    answers.append({**result, 'generationLatencySeconds': result['timing']['dispatchToCompletionSeconds'],
                                    'semanticPass': None, 'reviewStatus': 'not_yet_scored'})
                if self.scored:
                    scores = self.scored[number]['models'][MODELS[model]][variant]['answers']
                    for answer, score in zip(answers, scores):
                        require(answer['replicate'] == score['replicate'] and answer.get('prediction') == score['prediction'], 'Scored answer changed')
                        answer.update(semanticPass=score['semanticPass'], reason=score['reason'], reviewStatus=score['reviewOrigin'])
                        if score.get('previousScoring'):
                            answer['previousScoring'] = score['previousScoring']
                arms[arm] = {'answers': answers,
                    'successes': sum(a['semanticPass'] for a in answers) if self.scored else None,
                    'freshSuccesses': sum(a['semanticPass'] for a in answers[1:]) if self.scored else None}
        return {'case': number, 'selection': selected, 'arms': arms, 'progress': self.overview()}


def handler(data):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, body, content_type):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            port = self.server.server_address[1]
            if self.headers.get('Host') not in (f'127.0.0.1:{port}', f'localhost:{port}'):
                self.reply(403, b'Localhost access only', 'text/plain; charset=utf-8')
                return
            path = urlsplit(self.path).path
            try:
                assets = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'),
                          '/style.css': ('style.css', 'text/css')}
                if path in assets:
                    name, kind = assets[path]
                    self.reply(200, (ASSETS / name).read_bytes(), kind + '; charset=utf-8')
                    return
                if path == '/report':
                    self.reply(200, data.report().encode(), 'text/plain; charset=utf-8')
                    return
                if path == '/previous-report':
                    self.reply(200, (data.root / 'holistic-v2/scored/report.md').read_bytes(), 'text/plain; charset=utf-8')
                    return
                if path == '/api/overview':
                    result = data.overview()
                elif match := re.fullmatch(r'/api/cases/([1-9][0-9]*)/repeats', path):
                    if data.repeatability is None:
                        raise KeyError('No repeated answers')
                    result = data.repeatability.case(int(match[1]))
                elif match := re.fullmatch(r'/api/cases/([1-9][0-9]*)(?:/context/(old|new))?', path):
                    number, variant = int(match[1]), match[2]
                    result = data.context(number, variant) if variant else data.case(number)
                elif path == '/health':
                    result = {'status': 'ok', 'cases': len(data.examples), 'readOnly': True,
                              'scoringReview': data.review_version}
                else:
                    self.reply(404, b'Not found', 'text/plain; charset=utf-8')
                    return
                self.reply(200, json.dumps(result, ensure_ascii=False).encode(), 'application/json; charset=utf-8')
            except KeyError:
                self.reply(404, b'Unknown example', 'text/plain; charset=utf-8')
            except (ValueError, OSError) as error:
                self.reply(500, str(error).encode(), 'text/plain; charset=utf-8')

        def log_message(self, format, *args):
            # No prompt/target text in access logs.
            super().log_message(format, *args)

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8774)
    parser.add_argument('--review', help='Finalized scoring revision; defaults to current-scoring.json, or historical v2')
    parser.add_argument('--repeatability', type=Path, help='Optional frozen repeated-generation directory')
    parser.add_argument('--check', action='store_true', help='Verify and index artifacts without serving')
    args = parser.parse_args()
    data = Comparison(args.run, args.review, args.repeatability)
    if args.check:
        print(json.dumps({'status': 'passed', 'cases': len(data.examples),
                          'prompts': len(data.prompts.offsets), 'datasetWrites': 0, 'providerCalls': 0}))
        return
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler(data))
    print(f'Coupled comparison ready: http://127.0.0.1:{args.port}', flush=True)
    print(f'{len(data.examples)} cases; read-only; no provider calls', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
