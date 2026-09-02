#!/usr/bin/env python3
"""Build immutable Phase 1 READ-surface OCR evidence for one raw session."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from phase1_read_surface import (
    SURFACE_RULE_VERSION as V1_SURFACE_RULE_VERSION,
    surface_region as v1_surface_region,
)
from phase1_read_surface_v2 import (
    SURFACE_RULE_VERSION as V2_SURFACE_RULE_VERSION,
    surface_region as v2_surface_region,
)


EVIDENCE_SCHEMA_VERSION = 1


class EvidenceError(RuntimeError):
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
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EvidenceError(f"could not read JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise EvidenceError(f"expected a JSON object: {path}")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise EvidenceError(f"expected an object at {path}:{line_number}")
                rows.append(value)
    except (OSError, json.JSONDecodeError) as error:
        raise EvidenceError(f"could not read JSONL {path}: {error}") from error
    return rows


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(canonical(row) + "\n")


def compile_ocr(source: Path, output: Path) -> None:
    subprocess.run(
        [
            "clang",
            "-fobjc-arc",
            "-fblocks",
            "-framework",
            "Foundation",
            "-framework",
            "Vision",
            "-framework",
            "ImageIO",
            "-framework",
            "CoreGraphics",
            str(source),
            "-o",
            str(output),
        ],
        check=True,
    )


def run_ocr(executable: Path, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not jobs:
        return []
    completed = subprocess.run(
        [str(executable)],
        input="".join(canonical(job) + "\n" for job in jobs),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise EvidenceError(
            f"surface OCR failed with {completed.returncode}: {completed.stderr.strip()}"
        )
    rows = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    if len(rows) != len(jobs):
        raise EvidenceError(f"surface OCR returned {len(rows)} rows for {len(jobs)} jobs")
    return rows


def parse_arguments() -> argparse.Namespace:
    project = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="raw Coupled session")
    parser.add_argument("--output", required=True, type=Path, help="absent or empty output directory")
    parser.add_argument(
        "--ocr-source",
        type=Path,
        default=project / "scripts/ocr-phase1-surface-regions.m",
    )
    parser.add_argument(
        "--reuse-results",
        action="append",
        type=Path,
        default=[],
        help="prior ocr-results.jsonl or read-surfaces.jsonl",
    )
    parser.add_argument(
        "--rule-version",
        choices=["auto", V1_SURFACE_RULE_VERSION, V2_SURFACE_RULE_VERSION],
        default="auto",
        help="surface rule; auto selects AX v2 for raw screen schema 7+",
    )
    parser.add_argument(
        "--include-visual-observations",
        action="store_true",
        help="also reconstruct pane OCR for raw visual_ocr_observation records (semantic v14)",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    source = arguments.input.expanduser().resolve()
    output = arguments.output.expanduser().resolve()
    ocr_source = arguments.ocr_source.expanduser().resolve()
    session_path = source / "session.json"
    raw_path = source / "raw.jsonl"
    for path in [session_path, raw_path, ocr_source]:
        if not path.is_file():
            raise EvidenceError(f"required file is missing: {path}")
    if output.exists() and any(output.iterdir()):
        raise EvidenceError(f"output must be absent or empty: {output}")
    output.mkdir(parents=True, exist_ok=True)

    session = load_json(session_path)
    session_id = session.get("sessionID")
    if not isinstance(session_id, str) or not session_id:
        raise EvidenceError("session.json lacks sessionID")
    raw_screen_schema = session.get("schemas", {}).get("rawScreenOCR")
    if not isinstance(raw_screen_schema, int):
        raw_screen_schema = 0
    rule_version = arguments.rule_version
    if rule_version == "auto":
        rule_version = (
            V2_SURFACE_RULE_VERSION
            if raw_screen_schema >= 7
            else V1_SURFACE_RULE_VERSION
        )
    if rule_version == V2_SURFACE_RULE_VERSION and raw_screen_schema < 7:
        raise EvidenceError(
            f"{V2_SURFACE_RULE_VERSION} requires rawScreenOCR schema 7+"
        )
    surface_selector = (
        v2_surface_region
        if rule_version == V2_SURFACE_RULE_VERSION
        else v1_surface_region
    )
    raw_rows = load_jsonl(raw_path)
    included_record_types = ["screen_ocr_observation"]
    if arguments.include_visual_observations:
        included_record_types.append("visual_ocr_observation")
    read_rows = [
        row for row in raw_rows if row.get("recordType") in included_record_types
    ]
    read_by_id = {
        row["recordID"]: row for row in read_rows
        if isinstance(row.get("recordID"), str)
    }

    jobs: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for raw_line, record in enumerate(raw_rows, 1):
        if record.get("recordType") not in included_record_types:
            continue
        record_id = record.get("recordID")
        if not isinstance(record_id, str) or not record_id:
            raise EvidenceError(f"READ observation at raw line {raw_line} lacks recordID")
        relative = record.get("screenshotRelativePath")
        if not isinstance(relative, str) or not relative:
            unresolved.append({
                "schemaVersion": EVIDENCE_SCHEMA_VERSION,
                "ruleVersion": rule_version,
                "sessionID": session_id,
                "sourceRecordID": record_id,
                "sourceRawLine": raw_line,
                "reason": "screenshot_not_retained",
            })
            continue
        screenshot = (source / relative).resolve()
        try:
            screenshot.relative_to(source)
        except ValueError as error:
            raise EvidenceError(f"screenshot escapes source session: {relative}") from error
        if not screenshot.is_file():
            raise EvidenceError(f"retained screenshot is missing: {screenshot}")
        actual_screenshot_hash = sha256(screenshot)
        recorded_screenshot_hash = record.get("screenshotSHA256")
        if actual_screenshot_hash != recorded_screenshot_hash:
            raise EvidenceError(f"screenshot hash differs for {record_id}")
        region, selection = surface_selector(record)
        job_id = "surface_" + digest_text(canonical({
            "recordID": record_id,
            "screenshotSHA256": recorded_screenshot_hash,
            "region": region,
            "ruleVersion": rule_version,
        }))
        jobs.append({
            "jobID": job_id,
            "imagePath": str(screenshot),
            "regionOfInterest": region,
            "sourceRecordID": record_id,
            "sourceRawLine": raw_line,
            "sourceSessionID": session_id,
            "screenshotRelativePath": relative,
            "screenshotSHA256": recorded_screenshot_hash,
            "surfaceSelection": selection,
        })

    reusable: dict[str, dict[str, Any]] = {}
    for reuse_path in arguments.reuse_results:
        for row in load_jsonl(reuse_path.expanduser().resolve()):
            job_id = row.get("jobID") or row.get("evidenceID")
            if isinstance(job_id, str) and not row.get("error"):
                reusable[job_id] = row
    missing_jobs = [job for job in jobs if job["jobID"] not in reusable]
    recognized: dict[str, dict[str, Any]] = {}
    if missing_jobs:
        with tempfile.TemporaryDirectory(prefix="phase1-read-surface-ocr-") as temporary:
            executable = Path(temporary) / "ocr-phase1-surface-regions"
            compile_ocr(ocr_source, executable)
            recognized = {row["jobID"]: row for row in run_ocr(executable, missing_jobs)}

    evidence: list[dict[str, Any]] = []
    for job in jobs:
        result = reusable.get(job["jobID"]) or recognized[job["jobID"]]
        if result.get("error"):
            unresolved.append({
                "schemaVersion": EVIDENCE_SCHEMA_VERSION,
                "ruleVersion": rule_version,
                "sessionID": session_id,
                "sourceRecordID": job["sourceRecordID"],
                "sourceRawLine": job["sourceRawLine"],
                "jobID": job["jobID"],
                "reason": "ocr_error",
                "error": result["error"],
            })
            continue
        content = result.get("content")
        lines = result.get("lines")
        if not isinstance(content, str) or not content.strip() or not isinstance(lines, list):
            unresolved.append({
                "schemaVersion": EVIDENCE_SCHEMA_VERSION,
                "ruleVersion": rule_version,
                "sessionID": session_id,
                "sourceRecordID": job["sourceRecordID"],
                "sourceRawLine": job["sourceRawLine"],
                "jobID": job["jobID"],
                "reason": "empty_surface_ocr",
            })
            continue
        evidence.append({
            "schemaVersion": EVIDENCE_SCHEMA_VERSION,
            "ruleVersion": rule_version,
            "evidenceID": job["jobID"],
            "jobID": job["jobID"],
            "sessionID": session_id,
            "sourceRecordID": job["sourceRecordID"],
            "sourceRawLine": job["sourceRawLine"],
            "capturedAt": read_by_id[job["sourceRecordID"]].get("capturedAt"),
            "screenshotRelativePath": job["screenshotRelativePath"],
            "screenshotSHA256": job["screenshotSHA256"],
            "surfaceSelection": job["surfaceSelection"],
            "regionOfInterest": job["regionOfInterest"],
            "content": content,
            "contentSHA256": digest_text(content),
            "recognizedLineCount": len(lines),
            "lines": lines,
        })

    jobs_path = output / "jobs.jsonl"
    evidence_path = output / "read-surfaces.jsonl"
    unresolved_path = output / "unresolved.jsonl"
    write_jsonl(jobs_path, jobs)
    write_jsonl(evidence_path, evidence)
    write_jsonl(unresolved_path, sorted(unresolved, key=lambda row: row["sourceRawLine"]))
    manifest = {
        "schemaVersion": EVIDENCE_SCHEMA_VERSION,
        "ruleVersion": rule_version,
        "ruleSelection": {
            "requested": arguments.rule_version,
            "rawScreenOCRSchema": raw_screen_schema,
            "includedRecordTypes": included_record_types,
        },
        "sessionID": session_id,
        "source": {
            "directory": str(source),
            "digestsSHA256": {
                "session.json": sha256(session_path),
                "raw.jsonl": sha256(raw_path),
                "ocrSource": sha256(ocr_source),
            },
        },
        "ocr": {
            "framework": "Apple Vision",
            "recognitionLevel": "accurate",
            "usesLanguageCorrection": True,
            "automaticallyDetectsLanguage": True,
            "platform": platform.platform(),
        },
        "counts": {
            "rawRecords": len(raw_rows),
            "screenObservations": sum(
                row.get("recordType") == "screen_ocr_observation"
                for row in read_rows
            ),
            "visualObservations": sum(
                row.get("recordType") == "visual_ocr_observation"
                for row in read_rows
            ),
            "readObservations": len(read_rows),
            "jobs": len(jobs),
            "evidence": len(evidence),
            "unresolved": len(unresolved),
            "ocrResultsReused": len(jobs) - len(missing_jobs),
            "ocrResultsNew": len(missing_jobs),
        },
        "artifacts": {
            "digestsSHA256": {
                "jobs.jsonl": sha256(jobs_path),
                "read-surfaces.jsonl": sha256(evidence_path),
                "unresolved.jsonl": sha256(unresolved_path),
            },
        },
    }
    write_json(output / "read-surface-evidence.json", manifest)
    print(json.dumps({
        "output": str(output),
        "sessionID": session_id,
        **manifest["counts"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
