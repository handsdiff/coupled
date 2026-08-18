#!/usr/bin/env python3
"""Audit a deterministic multi-session Phase 1 corpus."""

from __future__ import annotations

import argparse
from pathlib import Path

from phase1_corpus import audit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    arguments = parser.parse_args()
    manifest = audit(arguments.corpus)
    print(
        f"Phase 1 corpus audit passed: {manifest['counts']['examples']} examples, "
        f"{manifest['counts']['sessions']} sessions, "
        f"{manifest['blocking']['blockCount']} chronological blocks"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as error:
        raise SystemExit(f"audit-phase1-corpus: {error}")
