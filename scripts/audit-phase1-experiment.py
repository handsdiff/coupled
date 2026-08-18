#!/usr/bin/env python3
"""Audit a no-network Phase 1 score-before-update experiment rehearsal."""

from __future__ import annotations

import argparse
from pathlib import Path

from phase1_experiment import audit_mock_experiment
from phase1_training_contract import TrainingContractError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, type=Path)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--packed", required=True, type=Path)
    arguments = parser.parse_args()
    manifest = audit_mock_experiment(
        arguments.experiment, arguments.corpus, arguments.packed
    )
    print(
        f"Phase 1 experiment audit passed: {manifest['counts']['scores']} scores, "
        f"{manifest['counts']['updates']} updates"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, TrainingContractError) as error:
        raise SystemExit(f"audit-phase1-experiment: {error}")
