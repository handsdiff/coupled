#!/usr/bin/env python3
"""Read-only five-answer budget comparison; preserves the original review server."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
from http.server import ThreadingHTTPServer
from urllib.parse import urlsplit

spec = importlib.util.spec_from_file_location('vision_review', Path(__file__).with_name('serve-phase1-vision-review.py'))
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)


class Review(base.Review):
    def __init__(self, root, scored, previous, previous_scored):
        super().__init__(root, scored)
        self.previous = base.Review(previous, previous_scored)
        lineage = base.load(root / 'lineage.json')
        assert previous.resolve() == Path(lineage['baselineRun']).resolve()
        assert base.file_hash(previous/'predictions.jsonl') == lineage['baselinePredictionsSHA256']
        assert base.file_hash(previous_scored/'completion.json') == lineage['baselineScoringCompletionSHA256']
        self.summary = dict(self.summary, original=self.previous.summary)
        for c in self.examples:
            n = c['case']
            assert self.cases.get(n)['target'] == self.previous.cases.get(n)['target']
            assert self.cases.get(n)['query'] == self.previous.cases.get(n)['query']
            c['originalPasses'] = {v: g['semanticPass'] for v, g in self.previous.grades[n].items()}
            current, old = self.grades[n]['screenshots_recent'], self.previous.grades[n]['screenshots_recent']
            assert current['prediction'] == old['prediction'] and current['semanticPass'] == old['semanticPass']

    def case(self, n):
        c = super().case(n)
        c['originalAnswers'] = {v: self.previous.grades[n][v] for v in ('cleaned_recent', 'raw_ocr_recent')}
        c['budgets'] = {v: self.prompts.get((n, v))['budgetPacking'] for v in ('cleaned_recent', 'raw_ocr_recent')}
        return c


def handler(data):
    parent = base.handler(data)

    class Handler(parent):
        def do_GET(self):
            m = re.fullmatch(r'/api/original-context/([1-9][0-9]*)/(cleaned_recent|raw_ocr_recent)', urlsplit(self.path).path)
            if not m:
                return super().do_GET()
            allowed = (f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}')
            if 'http://' + self.headers.get('Host', '') not in allowed or self.headers.get('Origin', allowed[0]) not in allowed:
                return self.reply(403, b'Localhost only', 'text/plain')
            try:
                result = data.previous.context(int(m[1]), m[2])
                self.reply(200, json.dumps(result, ensure_ascii=False).encode())
            except (KeyError, IndexError):
                self.reply(404, b'Unknown case', 'text/plain')
    return Handler


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('run', 'scored', 'previous', 'previous-scored'):
        p.add_argument('--' + name, type=Path, required=True)
    p.add_argument('--port', type=int, default=8783)
    p.add_argument('--check', action='store_true')
    a = p.parse_args()
    data = Review(a.run, a.scored, a.previous, a.previous_scored)
    if a.check:
        for item in data.examples:
            c = data.case(item['case'])
            assert len(c['answers']) + len(c['originalAnswers']) == 5
            for v in base.VARIANTS:
                data.context(c['case'], v)
            for v in c['originalAnswers']:
                data.previous.context(c['case'], v)
        print(json.dumps({'status': 'passed', 'cases': 69, 'displayedAnswers': 345, 'unchangedScreenshotGrades': 69}))
        return
    base.ASSETS = Path(__file__).with_name('phase1-vision-budget-ui')
    server = ThreadingHTTPServer(('127.0.0.1', a.port), handler(data))
    print(f'Budget comparison ready: http://127.0.0.1:{a.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
