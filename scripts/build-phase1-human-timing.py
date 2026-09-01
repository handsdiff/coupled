#!/usr/bin/env python3
"""Build deterministic human/model timing for a Phase 1 corpus."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from phase1_human_timing import Gate, build_artifact


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--first-one-based-ordinal", type=int, default=51)
    parser.add_argument("--sample-delay-seconds", type=float, default=3.0)
    return parser.parse_args()


def main() -> None:
    arguments = parse_arguments()
    artifact = build_artifact(
        arguments.project.resolve(),
        arguments.corpus.resolve(),
        first_ordinal=arguments.first_one_based_ordinal,
        sample_delay_seconds=arguments.sample_delay_seconds,
        gate=Gate(),
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    print(f"Wrote {arguments.output}")
    print(json.dumps(artifact["counts"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
