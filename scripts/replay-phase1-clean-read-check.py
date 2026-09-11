#!/usr/bin/env python3
"""Sequential, memory-bounded replay isolating removal of the session blacklist.

Uses frozen pane evidence (no OCR) and leaves capture and prior artifacts alone.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
PREP = ROOT/'coupled-data/sep02-10-training-prep-20260910'


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024*1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def run(command, log):
    peak = 0
    start = time.monotonic()
    with log.open('x') as handle:
        proc = subprocess.Popen(list(map(str, command)), stdout=handle, stderr=handle,
                                cwd=ROOT, start_new_session=True)
        try:
            while proc.poll() is None:
                table = subprocess.check_output(['ps','-axo','pid=,ppid=,rss='],text=True)
                rows = [tuple(map(int, line.split())) for line in table.splitlines()]
                ids = {proc.pid}
                for _ in range(12):
                    ids.update(pid for pid, parent, _ in rows if parent in ids)
                peak = max(peak, sum(rss for pid, _, rss in rows if pid in ids))
                if peak > 6*1024**2 or shutil.disk_usage(ROOT).free < 20*1024**3:
                    raise RuntimeError('6-GiB memory / 20-GiB free-disk guard reached')
                time.sleep(1)
        except BaseException:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait()
            raise
    if proc.returncode:
        raise RuntimeError(f'Local replay failed: {log}')
    return {'command': list(map(str, command)), 'peakRSSKiB': peak,
            'durationSeconds': time.monotonic()-start}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--surfaces', type=Path, help='Optional reviewed native-order evidence root')
    parser.add_argument('--compile', action='store_true', help='Also run the standard causal compiler')
    parser.add_argument('--days', type=int, nargs='+', choices=[2,3,4,7], default=[2,3,4,7])
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    pinned = json.loads((PREP/'inputs.json').read_text())
    manifest = {'binarySHA256': sha(args.binary), 'steps': []}
    for day in args.days:
        raw = ROOT/f'coupled-data/phase1-ordinary-work-2026-09-{day:02d}-1'
        if day == 7:
            surfaces = PREP/'new-sep7-surfaces-native'
            prior = PREP/'new-sep7-reduced'
        else:
            prior = Path(pinned['newExistingCausal'][[2,3,4].index(day)]).parent/f'sep{day}-reduced'
            surfaces = ROOT/f'coupled-data/sep02-04-semantic-v24-review-20260906/sep{day}-surfaces-r2'
        reduction = json.loads((prior/'reduction.json').read_text())
        assert sha(surfaces/'read-surfaces.jsonl') == reduction['source']['readSurfaceEvidence']['readSurfacesSHA256']
        if args.surfaces:
            updated = args.surfaces/f'sep{day}-surfaces'
            m = json.loads((updated/'read-surface-evidence.json').read_text())
            assert m['postOCR']['baselineManifestSHA256'] == sha(surfaces/'read-surface-evidence.json')
            surfaces = updated
        versions = ['phase1-semantic-v25','phase1-semantic-v26'] if day == 7 and not args.surfaces else ['phase1-semantic-v26']
        for version in versions:
            output = args.output/f'sep{day}-{version}'
            step = run([args.binary,'reduce','--input',raw,'--output',output,
                        '--reducer-version',version,'--read-surface-evidence',surfaces],
                       args.output/f'sep{day}-{version}.log')
            step.update(day=day, prior=str(prior), surfaces=str(surfaces),
                        eventsSHA256=sha(output/'events.jsonl'))
            if version == 'phase1-semantic-v25':
                assert step['eventsSHA256'] == sha(prior/'events.jsonl'), 'Legacy replay changed'
                assert sha(output/'unresolved.jsonl') == sha(prior/'unresolved.jsonl')
            manifest['steps'].append(step)
            print(json.dumps(step), flush=True)
            with (args.output/'progress.json').open('w') as handle:
                json.dump(manifest, handle, indent=2, sort_keys=True)
            if args.compile:
                causal = args.output/f'sep{day}-causal'
                compile_step = run([args.binary, 'compile', '--input', output,
                                    '--source', raw, '--output', causal],
                                   args.output/f'sep{day}-compile.log')
                compile_step['datasetSHA256'] = sha(causal/'dataset.json')
                manifest['steps'].append(compile_step)
                print(json.dumps(compile_step), flush=True)
    with (args.output/'complete.json').open('x') as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)


if __name__ == '__main__':
    main()
