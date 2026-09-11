"""Raw full-window OCR history, with lazy local backfill and no semantic READs.

Selection is a chronological whole-timestamp suffix. No deduplication, pane
selection, target-text matching, or cleaned-READ fallback is allowed here.
"""
import importlib.util
from functools import lru_cache
import json
import os
from pathlib import Path
import subprocess
import time

from phase1_read_model_comparison import canonical, file_hash, fingerprint


@lru_cache(maxsize=1)
def pilot_module():
    spec = importlib.util.spec_from_file_location('raw_history_pilot', Path(__file__).with_name('prepare-phase1-vision-pilot.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class FullWindowOCR:
    def __init__(self, cache):
        self.cache = cache
        self.cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.source = Path(__file__).with_name('ocr-phase1-surface-regions.m').resolve()
        self.source_hash = file_hash(self.source)
        self.binary = self.cache / ('ocr-' + self.source_hash[:16])
        self.used = {}
        self.new_count = 0

    def ensure(self, frame):
        assert Path(frame['screenshotPath']).is_file(), 'Missing raw image; never substitute cleaned text'
        assert file_hash(frame['screenshotPath']) == frame['screenshotSHA256']
        ocr = frame.get('ocr')
        if ocr is not None:
            assert not ocr['contentWasTruncated'] and not any(ocr['cropFractions'].values())
            assert ocr['capturedAt'] == frame['capturedAt'] and ocr['screenshotSHA256'] == frame['screenshotSHA256']
            return ocr
        key = fingerprint([frame['recordID'], frame['screenshotSHA256'], self.source_hash])
        path = self.cache / (key + '.json')
        binding = {'frameRecordID': frame['recordID'], 'screenshotSHA256': frame['screenshotSHA256'],
                   'capturedAt': frame['capturedAt'], 'ocrSourceSHA256': self.source_hash}
        if path.exists():
            result = json.loads(path.read_text())
            assert result['binding'] == binding
        else:
            if not self.binary.exists():
                subprocess.run(['xcrun', 'clang', '-fobjc-arc', '-fblocks', '-framework', 'Foundation',
                    '-framework', 'Vision', '-framework', 'ImageIO', '-framework', 'CoreGraphics',
                    str(self.source), '-o', str(self.binary)], check=True, capture_output=True)
            roi = {'x': 0, 'y': 0, 'width': 1, 'height': 1}
            job = {'jobID': frame['recordID'], 'imagePath': frame['screenshotPath'], 'regionOfInterest': roi}
            began = time.monotonic()
            child = subprocess.run([str(self.binary)], input=canonical(job)+'\n', text=True,
                                   capture_output=True, check=True, timeout=90)
            value = json.loads(child.stdout)
            assert not value.get('error'), 'Local Vision OCR failed: '+str(value.get('error'))[:500]
            assert value['jobID'] == frame['recordID'] and value['regionOfInterest'] == roi
            assert isinstance(value['content'], str)
            result = {'binding': binding, 'origin': 'local_full_window_ocr_backfill',
                'content': value['content'], 'contentWasTruncated': False, 'lines': value['lines'],
                'capturedAt': frame['capturedAt'], 'screenshotSHA256': frame['screenshotSHA256'],
                'cropFractions': {'viewportSideCropFraction': 0, 'viewportTopCropFraction': 0, 'viewportBottomCropFraction': 0},
                'elapsedSeconds': time.monotonic()-began}
            with path.open('x') as out:
                os.chmod(path, 0o600)
                out.write(canonical(result)+'\n'); out.flush(); os.fsync(out.fileno())
            self.new_count += 1
            print(f'Local full-window OCR backfill {self.new_count}', flush=True)
        assert file_hash(frame['screenshotPath']) == frame['screenshotSHA256']
        self.used[str(path.resolve())] = file_hash(path)
        frame['ocr'] = result
        return result


def raw_block(frame, ocr, privacy):
    meta = {'kind': 'read_observation', 'capturedAt': frame['capturedAt'], 'source': frame['source'],
            'content': ocr['content'], 'ocrAvailable': True}
    redacted = (pilot_module().frame_is_sensitive(frame, privacy))
    if redacted:
        meta = {'kind': 'read_observation', 'capturedAt': frame['capturedAt'], 'privacy': 'sensitive_content_redacted'}
    assert not privacy.unsafe(canonical(meta))
    return {'eventID': frame['recordID'], 'kind': 'read_observation', 'availableAt': frame['capturedAt'],
            'serialized': canonical(meta), 'privacyRedacted': bool(redacted),
            'screenshotSHA256': frame['screenshotSHA256'], 'ocrContentSHA256': fingerprint(ocr['content'])}


def pack_raw_suffix(frames, events, allowance, count, observe):
    """Never skip an overflowing timestamp group to reach cheaper older data."""
    timeline = [(e['availableAt'], 0, e['sourceEventID'], e) for e in events]
    timeline += [(f['capturedAt'], 1, f['recordID'], f) for f in frames]
    timeline.sort(key=lambda x: x[:3])
    selected = []; cost = 0; pos = len(timeline); next_cost = None
    while pos:
        end = pos; at = timeline[pos-1][0]
        while pos and timeline[pos-1][0] == at:
            pos -= 1
        group = []
        for _, kind, ident, value in timeline[pos:end]:
            if kind:
                block = observe(value)
            else:
                assert value['kind'] != 'read', 'Cleaned READ in raw OCR history'
                block = {'eventID': ident, 'kind': value['kind'], 'availableAt': value['availableAt'],
                         'serialized': value['serialized']}
            group.append(block)
        group_cost = sum(count(b['serialized']+'\n') for b in group)
        if cost+group_cost > allowance:
            next_cost = cost+group_cost
            break
        selected = group+selected; cost += group_cost
    reason = 'next_whole_timestamp_exceeds_budget' if next_cost is not None else 'all_earlier_history_used'
    return selected, cost, reason, next_cost
