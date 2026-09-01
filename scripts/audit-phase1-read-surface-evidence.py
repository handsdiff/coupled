#!/usr/bin/env python3
"""Audit immutable Phase 1 READ-surface OCR evidence against raw screenshots."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from phase1_read_surface import SURFACE_RULE_VERSION, surface_region


class AuditError(RuntimeError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AuditError(f"expected object: {path}")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not all(isinstance(row, dict) for row in rows):
        raise AuditError(f"expected JSON objects: {path}")
    return rows


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AuditError(message)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    arguments = parser.parse_args()
    artifact = arguments.artifact.expanduser().resolve()
    manifest_path = artifact / "read-surface-evidence.json"
    jobs_path = artifact / "jobs.jsonl"
    evidence_path = artifact / "read-surfaces.jsonl"
    unresolved_path = artifact / "unresolved.jsonl"
    for path in [manifest_path, jobs_path, evidence_path, unresolved_path]:
        require(path.is_file(), f"required artifact is missing: {path}")
    manifest = load_json(manifest_path)
    require(manifest.get("ruleVersion") == SURFACE_RULE_VERSION, "rule version differs")
    source = Path(manifest["source"]["directory"]).resolve()
    session_path = source / "session.json"
    raw_path = source / "raw.jsonl"
    require(session_path.is_file() and raw_path.is_file(), "source session is unavailable")
    require(sha256(session_path) == manifest["source"]["digestsSHA256"]["session.json"], "session hash differs")
    require(sha256(raw_path) == manifest["source"]["digestsSHA256"]["raw.jsonl"], "raw hash differs")
    for name, path in {
        "jobs.jsonl": jobs_path,
        "read-surfaces.jsonl": evidence_path,
        "unresolved.jsonl": unresolved_path,
    }.items():
        require(sha256(path) == manifest["artifacts"]["digestsSHA256"][name], f"{name} hash differs")

    raw_rows = load_jsonl(raw_path)
    raw_by_id = {row.get("recordID"): row for row in raw_rows if isinstance(row.get("recordID"), str)}
    screen_ids = {
        row["recordID"] for row in raw_rows
        if row.get("recordType") == "screen_ocr_observation" and isinstance(row.get("recordID"), str)
    }
    jobs = load_jsonl(jobs_path)
    evidence = load_jsonl(evidence_path)
    unresolved = load_jsonl(unresolved_path)
    require(len({row["jobID"] for row in jobs}) == len(jobs), "duplicate job ID")
    require(len({row["sourceRecordID"] for row in jobs}) == len(jobs), "duplicate job source")
    require(len({row["sourceRecordID"] for row in evidence}) == len(evidence), "duplicate evidence source")

    jobs_by_id = {row["jobID"]: row for row in jobs}
    for job in jobs:
        record = raw_by_id.get(job["sourceRecordID"])
        require(record is not None, f"job source is missing: {job['sourceRecordID']}")
        region, selection = surface_region(record)
        require(region == job["regionOfInterest"], f"job region differs: {job['jobID']}")
        require(selection == job["surfaceSelection"], f"job selection differs: {job['jobID']}")
        expected_job_id = "surface_" + digest_text(canonical({
            "recordID": record["recordID"],
            "screenshotSHA256": record.get("screenshotSHA256"),
            "region": region,
            "ruleVersion": SURFACE_RULE_VERSION,
        }))
        require(expected_job_id == job["jobID"], f"job identity differs: {job['jobID']}")
        screenshot = (source / job["screenshotRelativePath"]).resolve()
        require(screenshot.is_file(), f"screenshot is missing: {screenshot}")
        require(sha256(screenshot) == job["screenshotSHA256"], f"screenshot hash differs: {job['jobID']}")

    for row in evidence:
        job = jobs_by_id.get(row["jobID"])
        require(job is not None, f"evidence job is missing: {row['jobID']}")
        require(row["sourceRecordID"] == job["sourceRecordID"], f"evidence source differs: {row['jobID']}")
        require(row["regionOfInterest"] == job["regionOfInterest"], f"evidence region differs: {row['jobID']}")
        require(row["surfaceSelection"] == job["surfaceSelection"], f"evidence selection differs: {row['jobID']}")
        require(digest_text(row["content"]) == row["contentSHA256"], f"content hash differs: {row['jobID']}")
        require(len(row["lines"]) == row["recognizedLineCount"], f"line count differs: {row['jobID']}")
        require("\n".join(line["text"] for line in row["lines"]) == row["content"], f"line content differs: {row['jobID']}")

    disposition_ids = {row["sourceRecordID"] for row in evidence} | {
        row["sourceRecordID"] for row in unresolved
    }
    require(disposition_ids == screen_ids, "screen observations lack exactly one evidence disposition")
    require(
        len(evidence) + len(unresolved) == len(screen_ids),
        "evidence and unresolved dispositions overlap",
    )
    counts = manifest["counts"]
    require(counts["rawRecords"] == len(raw_rows), "raw count differs")
    require(counts["screenObservations"] == len(screen_ids), "screen count differs")
    require(counts["jobs"] == len(jobs), "job count differs")
    require(counts["evidence"] == len(evidence), "evidence count differs")
    require(counts["unresolved"] == len(unresolved), "unresolved count differs")
    print(json.dumps({
        "artifact": str(artifact),
        "evidence": len(evidence),
        "jobs": len(jobs),
        "screenObservations": len(screen_ids),
        "status": "pass",
        "unresolved": len(unresolved),
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
