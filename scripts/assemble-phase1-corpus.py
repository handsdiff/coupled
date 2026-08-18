#!/usr/bin/env python3
"""Assemble chronological compatible Phase 1 datasets into one corpus."""

from __future__ import annotations

import argparse
from pathlib import Path

from phase1_corpus import assemble


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--block-size", type=int, default=50)
    arguments = parser.parse_args()
    manifest = assemble(arguments.input, arguments.output.expanduser().resolve(), arguments.block_size)
    print(
        f"Assembled {manifest['counts']['examples']} examples from "
        f"{manifest['counts']['sessions']} sessions into {arguments.output}"
    )
    print(
        f"Blocks: {manifest['blocking']['blockCount']} x up to "
        f"{manifest['blocking']['blockSize']}; gaps: {manifest['counts']['coverageGaps']}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f"assemble-phase1-corpus: {error}")
        raise SystemExit(1)
