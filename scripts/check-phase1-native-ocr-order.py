#!/usr/bin/env python3
"""Local same-image, same-region native Vision ordering diagnostic.

Reads the compact, already frozen case-study evidence, not whole raw journals.
No collector, corpus, or provider changes. Retains every actual OCR response.
"""
import argparse
from collections import Counter
import hashlib
import html
import json
from pathlib import Path
import resource
import select
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Use a fresh output directory')
    evidence = json.loads(args.evidence.read_text())
    source = ROOT / 'scripts/ocr-phase1-surface-regions.m'
    args.output.mkdir(parents=True)
    counters = Counter()
    review = []
    with tempfile.TemporaryDirectory(prefix='coupled-native-order-') as tmp, \
            (args.output / 'observations.jsonl').open('x') as output:
        binary = Path(tmp) / 'ocr'
        silicon = subprocess.run(['sysctl', '-n', 'hw.optional.arm64'], capture_output=True, text=True).stdout.strip() == '1'
        architecture = 'arm64' if silicon else 'x86_64'
        subprocess.run(['clang', '-arch', architecture, '-fobjc-arc', '-fblocks', '-framework', 'Foundation',
                        '-framework', 'Vision', '-framework', 'ImageIO', '-framework', 'CoreGraphics',
                        str(source), '-o', str(binary)], check=True, timeout=90)
        worker = subprocess.Popen([str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  text=True)
        for rid, pane in sorted(evidence['panes'].items()):
            raw = evidence['raw'][rid]
            image = ROOT / Path(raw['auditSourcePath']).parent / pane['screenshotRelativePath']
            assert sha(image) == pane['screenshotSHA256'], ('Image hash mismatch', rid)
            for label, region_key, line_key in [
                ('pane', 'regionOfInterest', 'lines'),
                ('comparison', 'comparisonRegionOfInterest', 'comparisonLines'),
            ]:
                if not pane.get(region_key):
                    continue
                job = {'jobID': f'{rid}:{label}', 'imagePath': str(image),
                       'regionOfInterest': pane[region_key]}
                worker.stdin.write(json.dumps(job)+'\n'); worker.stdin.flush()
                if not select.select([worker.stdout], [], [], 60)[0]:
                    worker.kill(); worker.wait()
                    raise TimeoutError('Local Vision request exceeded 60 seconds')
                native = json.loads(worker.stdout.readline())
                if native.get('error'):
                    worker.kill(); worker.wait()
                    raise RuntimeError(native['error'])
                before = [line['text'] for line in pane[line_key]]
                after = [line['text'] for line in native['lines']]
                status = ('identical' if before == after else 'order_only'
                          if Counter(before) == Counter(after) else 'recognition_changed')
                row = {'sourceRecordID': rid, 'label': label, 'status': status,
                       'screenshotSHA256': pane['screenshotSHA256'],
                       'baselineEvidenceID': pane['evidenceID'], 'baselineLines': pane[line_key],
                       'observation': native}
                output.write(json.dumps(row, ensure_ascii=False, sort_keys=True)+'\n')
                output.flush()
                counters[status] += 1
                if before != after:
                    review.append(f'<h2>{html.escape(rid)} · {label} · {status}</h2>'
                                  '<div class="pair"><pre>'+html.escape('\n'.join(before))+
                                  '</pre><pre>'+html.escape('\n'.join(after))+'</pre></div>')
                if sum(counters.values()) % 20 == 0:
                    print(json.dumps(dict(counters)), flush=True)
                if resource.getrusage(resource.RUSAGE_SELF).ru_maxrss > 1024**3:
                    worker.kill(); worker.wait()
                    raise MemoryError('Diagnostic exceeded 1 GiB RSS')
        worker.stdin.close()
        assert worker.wait(timeout=10) == 0
    summary = {'version': 'vision-native-order-v1', 'counts': dict(counters),
               'evidenceSHA256': sha(args.evidence), 'ocrSourceSHA256': sha(source),
               'observationsSHA256': sha(args.output/'observations.jsonl'),
               'peakRSSMiB': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024**2,
               'recognitionSettingsChanged': False, 'regionsChanged': False,
               'ocrArchitecture': architecture}
    with (args.output/'summary.json').open('x') as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)
    with (args.output/'review.html').open('x') as handle:
        handle.write('<meta charset="utf-8"><title>Before / native Vision order</title>'
                     '<style>body{font:15px system-ui;margin:24px}.pair{display:flex;gap:24px}'
                     'pre{white-space:pre-wrap;width:50%;background:#eee;padding:16px}</style>'
                     '<h1>Before / native Vision order</h1>'+''.join(review))
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
