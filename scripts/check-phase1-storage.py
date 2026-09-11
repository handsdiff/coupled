#!/usr/bin/env python3
"""No-network safety checks for explicit derived-file deletion and disk gates."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

from phase1_storage import identity, require_space

with tempfile.TemporaryDirectory(prefix="phase1-storage-check-") as directory:
    root = Path(directory).resolve()
    source = root / "derived"
    source.mkdir()
    raw = root / "raw.jsonl"
    raw.write_text('original capture\n')
    example = source / "examples.jsonl"
    example.write_text('{}\n')
    plan_path, journal = root / "plan.json", root / "deletions.jsonl"
    command = [sys.executable, str(Path(__file__).with_name('manage-phase1-storage.py')), 'delete-derived', '--plan', str(plan_path), '--journal', str(journal)]
    def plan(path):
        plan_path.write_text(json.dumps({'version':'phase1-derived-deletion-v1', 'root':str(root), 'files':[{'path':str(path), 'identity':[str(v) for v in identity(path)]}]}))
    plan(raw)
    assert subprocess.run(command + ['--apply'], capture_output=True).returncode != 0
    assert raw.read_text() == 'original capture\n' and not journal.exists()
    link = root / 'examples.jsonl'
    link.symlink_to(example)
    plan(link)
    assert subprocess.run(command + ['--apply'], capture_output=True).returncode != 0
    plan(example)
    example.write_text('{"changed": true}\n')
    assert subprocess.run(command + ['--apply'], capture_output=True).returncode != 0
    plan(example)
    assert subprocess.run(command, capture_output=True).returncode == 0 and example.exists()
    assert subprocess.run(command + ['--apply'], capture_output=True).returncode == 0
    assert not example.exists() and raw.read_text() == 'original capture\n'
    rows = [json.loads(s) for s in journal.read_text().splitlines()]
    assert rows[0]['status'] == 'verified_before_deletion' and len(rows[0]['sha256']) == 64
    assert rows[1]['status'] == 'deleted'
    with patch('phase1_storage.shutil.disk_usage') as usage:
        usage.return_value.free = 1
        try:
            require_space(root, 2, 0)
            raise AssertionError('low space accepted')
        except ValueError:
            pass
        require_space(root, 0, 1)

print('Storage checks passed: dry-run, raw/symlink refusal, source mutation rejection, deletion journal, low-space gate')
