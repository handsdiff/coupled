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
from phase1_read_surface_v3 import (
    SURFACE_RULE_VERSION as V3_SURFACE_RULE_VERSION,
    surface_region as v3_surface_region,
)
from phase1_read_surface_v4 import (
    SURFACE_RULE_VERSION as V4_SURFACE_RULE_VERSION,
    surface_region as v4_surface_region,
)
from phase1_read_surface_v5 import (
    SURFACE_RULE_VERSION as V5_SURFACE_RULE_VERSION,
    surface_region as v5_surface_region,
    surface_regions as v5_surface_regions,
)
from phase1_read_surface_v6 import (
    SURFACE_RULE_VERSION as V6_SURFACE_RULE_VERSION,
    PaneResolver as V6PaneResolver,
    surface_regions_from_review as v6_surface_regions_from_review,
)
from phase1_read_surface_v7 import (
    SURFACE_RULE_VERSION as V7_SURFACE_RULE_VERSION,
    PaneResolver as V7PaneResolver,
    surface_regions_from_review as v7_surface_regions_from_review,
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


def run_ocr(
    executable: Path, jobs: list[dict[str, Any]], *, batch_size: int = 16,
) -> list[dict[str, Any]]:
    if not jobs:
        return []
    rows: list[dict[str, Any]] = []
    for offset in range(0, len(jobs), batch_size):
        batch = jobs[offset:offset + batch_size]
        completed = subprocess.run(
            [str(executable)],
            input="".join(canonical(job) + "\n" for job in batch),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            raise EvidenceError(
                "surface OCR failed for batch "
                f"{offset // batch_size + 1} with {completed.returncode}: "
                f"{completed.stderr.strip()}"
            )
        batch_rows = [
            json.loads(line) for line in completed.stdout.splitlines()
            if line.strip()
        ]
        if len(batch_rows) != len(batch):
            raise EvidenceError(
                f"surface OCR returned {len(batch_rows)} rows for "
                f"{len(batch)} jobs in batch {offset // batch_size + 1}"
            )
        rows.extend(batch_rows)
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
        choices=[
            "auto", V1_SURFACE_RULE_VERSION, V2_SURFACE_RULE_VERSION,
            V3_SURFACE_RULE_VERSION, V4_SURFACE_RULE_VERSION,
            V5_SURFACE_RULE_VERSION, V6_SURFACE_RULE_VERSION,
            V7_SURFACE_RULE_VERSION,
        ],
        default="auto",
        help="surface rule; auto retains stateful dual-projection AX v6 until v7 review is promoted",
    )
    parser.add_argument(
        "--include-visual-observations",
        action="store_true",
        help="also reconstruct pane OCR for raw visual_ocr_observation records",
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
            V6_SURFACE_RULE_VERSION
            if raw_screen_schema >= 7
            else V1_SURFACE_RULE_VERSION
        )
    if rule_version in {
        V2_SURFACE_RULE_VERSION, V3_SURFACE_RULE_VERSION,
        V4_SURFACE_RULE_VERSION, V5_SURFACE_RULE_VERSION,
        V6_SURFACE_RULE_VERSION, V7_SURFACE_RULE_VERSION,
    } \
            and raw_screen_schema < 7:
        raise EvidenceError(
            f"{V2_SURFACE_RULE_VERSION} requires rawScreenOCR schema 7+"
        )
    surface_selector = {
        V1_SURFACE_RULE_VERSION: v1_surface_region,
        V2_SURFACE_RULE_VERSION: v2_surface_region,
        V3_SURFACE_RULE_VERSION: v3_surface_region,
        V4_SURFACE_RULE_VERSION: v4_surface_region,
        V5_SURFACE_RULE_VERSION: v5_surface_region,
    }.get(rule_version)
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
    stateful_resolver = {
        V6_SURFACE_RULE_VERSION: V6PaneResolver,
        V7_SURFACE_RULE_VERSION: V7PaneResolver,
    }.get(rule_version)
    stateful_reviews = (
        stateful_resolver().resolve_records([
            (raw_line, row) for raw_line, row in enumerate(raw_rows, 1)
            if row.get("recordType") in included_record_types
        ])
        if stateful_resolver is not None else {}
    )

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
        if rule_version in {V6_SURFACE_RULE_VERSION, V7_SURFACE_RULE_VERSION}:
            review = stateful_reviews.get(record_id)
            resolved = (
                (
                    v7_surface_regions_from_review(review)
                    if rule_version == V7_SURFACE_RULE_VERSION
                    else v6_surface_regions_from_review(review)
                )
                if isinstance(review, dict) else None
            )
            if resolved is None:
                proposal = (
                    review.get("proposal", {}) if isinstance(review, dict) else {}
                )
                unresolved.append({
                    "schemaVersion": EVIDENCE_SCHEMA_VERSION,
                    "ruleVersion": rule_version,
                    "sessionID": session_id,
                    "sourceRecordID": record_id,
                    "sourceRawLine": raw_line,
                    "reason": proposal.get(
                        "reason", "pane_resolution_evidence_missing"
                    ),
                    "surfaceSelection": {
                        key: proposal[key] for key in (
                            "method", "confidence", "reason", "resolution",
                            "physicalPointerClassification", "attemptedSelection",
                        ) if key in proposal
                    },
                })
                continue
            full, comparison, selection = resolved
            regions = [
                ("authoritative_full_pane", full),
                ("comparison_interior", comparison),
            ]
        elif rule_version == V5_SURFACE_RULE_VERSION:
            full, comparison, selection = v5_surface_regions(record)
            regions = [("authoritative_full_pane", full), ("comparison_interior", comparison)]
        else:
            assert surface_selector is not None
            region, selection = surface_selector(record)
            regions = [("authoritative", region)]
        for projection, region in regions:
            job_id = "surface_" + digest_text(canonical({
                "recordID": record_id,
                "screenshotSHA256": recorded_screenshot_hash,
                "region": region,
                "projection": projection,
                "ruleVersion": rule_version,
            }))
            jobs.append({
                "jobID": job_id,
                "projection": projection,
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
    reusable_by_region: dict[str, dict[str, Any]] = {}
    for reuse_path in arguments.reuse_results:
        for row in load_jsonl(reuse_path.expanduser().resolve()):
            job_id = row.get("jobID") or row.get("evidenceID")
            if isinstance(job_id, str) and not row.get("error"):
                reusable[job_id] = row
            screenshot_hash = row.get("screenshotSHA256")
            region = row.get("regionOfInterest")
            if isinstance(screenshot_hash, str) and isinstance(region, dict):
                reusable_by_region[canonical([screenshot_hash, region])] = row
            comparison_region = row.get("comparisonRegionOfInterest")
            comparison_content = row.get("comparisonContent")
            comparison_lines = row.get("comparisonLines")
            if (
                isinstance(screenshot_hash, str)
                and isinstance(comparison_region, dict)
                and isinstance(comparison_content, str)
                and isinstance(comparison_lines, list)
            ):
                reusable_by_region[canonical([
                    screenshot_hash, comparison_region,
                ])] = {
                    "content": comparison_content,
                    "lines": comparison_lines,
                }
    def reusable_result(job: dict[str, Any]) -> dict[str, Any] | None:
        return reusable.get(job["jobID"]) or reusable_by_region.get(canonical([
            job["screenshotSHA256"], job["regionOfInterest"],
        ]))

    missing_jobs = [job for job in jobs if reusable_result(job) is None]
    recognized: dict[str, dict[str, Any]] = {}
    if missing_jobs:
        with tempfile.TemporaryDirectory(prefix="phase1-read-surface-ocr-") as temporary:
            executable = Path(temporary) / "ocr-phase1-surface-regions"
            compile_ocr(ocr_source, executable)
            recognized = {row["jobID"]: row for row in run_ocr(executable, missing_jobs)}

    evidence: list[dict[str, Any]] = []
    jobs_by_source: dict[str, list[dict[str, Any]]] = {}
    for job in jobs:
        jobs_by_source.setdefault(job["sourceRecordID"], []).append(job)
    for source_id, source_jobs in jobs_by_source.items():
        results: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        failure: tuple[str, dict[str, Any], dict[str, Any]] | None = None
        for job in source_jobs:
            result = reusable_result(job) or recognized[job["jobID"]]
            content = result.get("content")
            lines = result.get("lines")
            if result.get("error"):
                failure = ("ocr_error", job, result)
                break
            if not isinstance(content, str) or not content.strip() or not isinstance(lines, list):
                failure = ("empty_surface_ocr", job, result)
                break
            results[job["projection"]] = (job, result)
        if failure:
            reason, job, result = failure
            disposition = {
                "schemaVersion": EVIDENCE_SCHEMA_VERSION,
                "ruleVersion": rule_version,
                "sessionID": session_id,
                "sourceRecordID": source_id,
                "sourceRawLine": job["sourceRawLine"],
                "jobID": job["jobID"],
                "reason": reason,
            }
            if result.get("error"):
                disposition["error"] = result["error"]
            unresolved.append(disposition)
            continue
        authoritative_key = (
            "authoritative_full_pane"
            if rule_version in {
                V5_SURFACE_RULE_VERSION, V6_SURFACE_RULE_VERSION,
                V7_SURFACE_RULE_VERSION,
            } else "authoritative"
        )
        authoritative_job, authoritative = results[authoritative_key]
        content = authoritative["content"]
        lines = authoritative["lines"]
        evidence_id = "evidence_" + digest_text(canonical([
            job["jobID"] for job in source_jobs
        ]))
        row = {
            "schemaVersion": EVIDENCE_SCHEMA_VERSION,
            "ruleVersion": rule_version,
            "evidenceID": evidence_id,
            "jobID": authoritative_job["jobID"],
            "jobIDs": [job["jobID"] for job in source_jobs],
            "sessionID": session_id,
            "sourceRecordID": source_id,
            "sourceRawLine": authoritative_job["sourceRawLine"],
            "capturedAt": read_by_id[source_id].get("capturedAt"),
            "screenshotRelativePath": authoritative_job["screenshotRelativePath"],
            "screenshotSHA256": authoritative_job["screenshotSHA256"],
            "surfaceSelection": authoritative_job["surfaceSelection"],
            "regionOfInterest": authoritative_job["regionOfInterest"],
            "content": content,
            "contentSHA256": digest_text(content),
            "recognizedLineCount": len(lines),
            "lines": lines,
        }
        if rule_version in {
            V5_SURFACE_RULE_VERSION, V6_SURFACE_RULE_VERSION,
            V7_SURFACE_RULE_VERSION,
        }:
            comparison_job, comparison = results["comparison_interior"]
            row.update({
                "comparisonRegionOfInterest": comparison_job["regionOfInterest"],
                "comparisonContent": comparison["content"],
                "comparisonContentSHA256": digest_text(comparison["content"]),
                "comparisonRecognizedLineCount": len(comparison["lines"]),
                "comparisonLines": comparison["lines"],
            })
        evidence.append(row)

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
