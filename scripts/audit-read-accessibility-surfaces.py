#!/usr/bin/env python3
"""Audit prospective raw Accessibility surface evidence in one Coupled session."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


class AuditError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AuditError(message)


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(value, dict), f"expected a JSON object: {path}")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            require(isinstance(value, dict), f"expected an object at {path}:{line_number}")
            value["_auditLine"] = line_number
            rows.append(value)
    return rows


def numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    arguments = parser.parse_args()
    session_directory = arguments.session.expanduser().resolve()
    manifest_path = session_directory / "session.json"
    raw_path = session_directory / "raw.jsonl"
    require(manifest_path.is_file(), f"missing session manifest: {manifest_path}")
    require(raw_path.is_file(), f"missing raw journal: {raw_path}")

    manifest = load_json(manifest_path)
    require(
        manifest.get("schemas", {}).get("rawScreenOCR") == 7,
        "session does not declare rawScreenOCR schema 7",
    )
    reads = [
        row for row in load_jsonl(raw_path)
        if row.get("recordType") == "screen_ocr_observation"
    ]
    require(reads, "session contains no raw screen observations")

    terminations: Counter[str] = Counter()
    applications: Counter[str] = Counter()
    successful_chains = 0
    framed_chains = 0
    for read in reads:
        line = read["_auditLine"]
        require(read.get("schemaVersion") == 7, f"raw READ schema differs at line {line}")
        probe = read.get("accessibilitySurface")
        require(isinstance(probe, dict), f"missing Accessibility surface at line {line}")
        require(probe.get("schemaVersion") == 1, f"probe schema differs at line {line}")
        require(
            probe.get("source") == "accessibility_element_at_position",
            f"probe source differs at line {line}",
        )
        require(probe.get("pointerX") == read.get("x"), f"probe x differs at line {line}")
        require(probe.get("pointerY") == read.get("y"), f"probe y differs at line {line}")
        require(
            probe.get("expectedProcessIdentifier") == read.get("processIdentifier"),
            f"probe process differs at line {line}",
        )
        require(
            numeric(probe.get("durationMilliseconds"))
            and probe["durationMilliseconds"] >= 0,
            f"invalid probe duration at line {line}",
        )
        ancestors = probe.get("ancestors")
        require(isinstance(ancestors, list), f"invalid ancestors at line {line}")
        require(len(ancestors) <= 12, f"unbounded ancestor chain at line {line}")
        require(
            [node.get("depth") for node in ancestors] == list(range(len(ancestors))),
            f"non-contiguous ancestor depths at line {line}",
        )
        for node in ancestors:
            require(isinstance(node, dict), f"invalid ancestor at line {line}")
            forbidden = {"value", "text", "selectedText"}.intersection(node)
            require(not forbidden, f"AX content leaked into metadata at line {line}: {forbidden}")
            frame = node.get("frame")
            if frame is not None:
                require(
                    isinstance(frame, dict)
                    and all(numeric(frame.get(key)) for key in ["x", "y", "width", "height"]),
                    f"invalid ancestor frame at line {line}",
                )
        termination = probe.get("termination")
        require(isinstance(termination, str) and termination, f"missing termination at line {line}")
        terminations[termination] += 1
        applications[str(read.get("appName") or "Unknown")] += 1
        if ancestors:
            successful_chains += 1
        if any(node.get("frame") is not None for node in ancestors):
            framed_chains += 1

    print(json.dumps({
        "applications": dict(sorted(applications.items())),
        "framedChains": framed_chains,
        "rawScreenObservations": len(reads),
        "successfulChains": successful_chains,
        "terminations": dict(sorted(terminations.items())),
        "status": "pass",
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
