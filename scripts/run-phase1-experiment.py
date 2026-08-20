#!/usr/bin/env python3
"""Run the chronological Phase 1 experiment with an explicitly selected backend."""

from __future__ import annotations

import argparse
from pathlib import Path

from phase1_experiment import run_mock_experiment
from phase1_training_contract import TrainingContractError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--packed", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--backend", required=True, choices=("mock",))
    parser.add_argument("--epochs-per-update", type=int, default=1)
    arguments = parser.parse_args()
    manifest = run_mock_experiment(
        arguments.corpus.expanduser().resolve(),
        arguments.packed.expanduser().resolve(),
        arguments.output.expanduser().resolve(),
        arguments.epochs_per_update,
    )
    print(
        f"Phase 1 mock experiment passed: {manifest['counts']['scores']} scores, "
        f"{manifest['counts']['updates']} incremental updates "
        "(first block is training-only warm-up)"
    )
    print("No model, network, authentication, or paid operation was used.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, TrainingContractError, ValueError) as error:
        raise SystemExit(f"run-phase1-experiment: {error}")
