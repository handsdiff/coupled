#!/usr/bin/env python3
"""Build a deterministic shadow artifact for manual AX pane review."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from phase1_read_surface_v2 import PROPOSAL_RULE_VERSION, proposal_for_record


class BuildError(RuntimeError):
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
        raise BuildError(f"expected JSON object: {path}")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise BuildError(f"expected object at {path}:{line_number}")
            value["_sourceLine"] = line_number
            rows.append(value)
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            handle.write("\n")


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
    completed = subprocess.run(
        [str(executable)],
        input="".join(canonical(job) + "\n" for job in jobs),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise BuildError(
            f"surface OCR failed with {completed.returncode}: {completed.stderr.strip()}"
        )
    results = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    if len(results) != len(jobs):
        raise BuildError(f"surface OCR returned {len(results)} rows for {len(jobs)} jobs")
    return results


def ocr_job(
    record_id: str,
    variant: str,
    screenshot: Path,
    screenshot_hash: str,
    region: dict[str, Any],
) -> dict[str, Any]:
    job_id = "review_" + digest_text(canonical({
        "recordID": record_id,
        "variant": variant,
        "screenshotSHA256": screenshot_hash,
        "regionOfInterest": region,
        "ruleVersion": PROPOSAL_RULE_VERSION,
    }))
    return {
        "jobID": job_id,
        "recordID": record_id,
        "variant": variant,
        "imagePath": str(screenshot),
        "regionOfInterest": region,
    }


def normalized_ocr(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("error"):
        raise BuildError(f"OCR job {result.get('jobID')} failed: {result['error']}")
    content = result.get("content")
    lines = result.get("lines")
    if not isinstance(content, str) or not isinstance(lines, list):
        raise BuildError(f"OCR job {result.get('jobID')} returned invalid content")
    return {
        "jobID": result.get("jobID"),
        "regionOfInterest": result.get("regionOfInterest"),
        "content": content,
        "contentSHA256": digest_text(content),
        "recognizedLineCount": len(lines),
        "lines": lines,
    }


def main() -> int:
    project = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--ocr-source",
        type=Path,
        default=project / "scripts/ocr-phase1-surface-regions.m",
    )
    arguments = parser.parse_args()

    session = arguments.session.expanduser().resolve()
    output = arguments.output.expanduser().resolve()
    ocr_source = arguments.ocr_source.expanduser().resolve()
    if output.exists():
        raise BuildError(f"output already exists: {output}")
    manifest_path = session / "session.json"
    raw_path = session / "raw.jsonl"
    manifest = load_json(manifest_path)
    if not ocr_source.is_file():
        raise BuildError(f"OCR source is missing: {ocr_source}")
    if manifest.get("schemas", {}).get("rawScreenOCR") != 7:
        raise BuildError("session must use rawScreenOCR schema 7")

    observations = [
        row for row in load_jsonl(raw_path)
        if row.get("recordType") == "screen_ocr_observation"
    ]
    if not observations:
        raise BuildError("session has no screen observations")

    rows = []
    jobs: list[dict[str, Any]] = []
    applications: Counter[str] = Counter()
    methods: Counter[str] = Counter()
    for ordinal, record in enumerate(observations, 1):
        screenshot_relative = record.get("screenshotRelativePath")
        if not isinstance(screenshot_relative, str) or not screenshot_relative:
            raise BuildError(f"observation has no screenshot path: {record.get('recordID')}")
        screenshot = session / screenshot_relative
        expected_screenshot_hash = record.get("screenshotSHA256")
        if not screenshot.is_file() or sha256(screenshot) != expected_screenshot_hash:
            raise BuildError(f"screenshot digest differs: {screenshot}")
        selection = proposal_for_record(record)
        record_id = record.get("recordID")
        if not isinstance(record_id, str) or not record_id:
            raise BuildError(f"observation at raw line {record['_sourceLine']} has no record ID")
        jobs.extend([
            ocr_job(
                record_id,
                "v1",
                screenshot,
                expected_screenshot_hash,
                selection["v1"]["regionOfInterest"],
            ),
            ocr_job(
                record_id,
                "v2",
                screenshot,
                expected_screenshot_hash,
                selection["proposal"]["regionOfInterest"],
            ),
        ])
        app = str(record.get("appName") or "Unknown")
        applications[app] += 1
        methods[selection["proposal"]["method"]] += 1
        rows.append({
            "ordinal": ordinal,
            "recordID": record_id,
            "sourceRawLine": record["_sourceLine"],
            "capturedAt": record.get("capturedAt"),
            "application": app,
            "bundleIdentifier": record.get("bundleIdentifier"),
            "windowTitle": record.get("windowTitle"),
            "triggerTypes": record.get("triggerTypes") or [],
            "screenshotPath": str(screenshot.resolve()),
            "screenshotRelativePath": screenshot_relative,
            "screenshotSHA256": expected_screenshot_hash,
            "selection": selection,
            "recordedV1OCR": {
                "content": str(record.get("content") or ""),
                "contentSHA256": digest_text(str(record.get("content") or "")),
                "recognizedLineCount": record.get("recognizedLineCount"),
            },
        })

    with tempfile.TemporaryDirectory(prefix="phase1-read-surface-v2-review-") as temporary:
        executable = Path(temporary) / "ocr-phase1-surface-regions"
        compile_ocr(ocr_source, executable)
        recognized = {result["jobID"]: result for result in run_ocr(executable, jobs)}
    jobs_by_record: dict[str, dict[str, dict[str, Any]]] = {}
    for job in jobs:
        jobs_by_record.setdefault(job["recordID"], {})[job["variant"]] = job
    for row in rows:
        record_jobs = jobs_by_record[row["recordID"]]
        row["ocrComparison"] = {}
        for variant in ("v1", "v2"):
            job = record_jobs[variant]
            row["ocrComparison"][variant] = normalized_ocr(recognized[job["jobID"]])
        row["ocrComparison"]["v1"]["recordedContentMatchesRerun"] = (
            row["recordedV1OCR"]["content"] == row["ocrComparison"]["v1"]["content"]
        )

    output.mkdir(parents=True)
    observations_path = output / "observations.jsonl"
    jobs_path = output / "ocr-jobs.jsonl"
    write_jsonl(observations_path, rows)
    write_jsonl(jobs_path, jobs)
    review = {
        "schemaVersion": 2,
        "artifactType": "phase1_read_surface_v2_shadow_review",
        "status": "shadow_review_only_not_training_authority",
        "ruleVersion": PROPOSAL_RULE_VERSION,
        "source": {
            "sessionDirectory": str(session),
            "sessionID": manifest.get("sessionID"),
            "rawJSONLSHA256": sha256(raw_path),
            "sessionManifestSHA256": sha256(manifest_path),
        },
        "counts": {
            "observations": len(rows),
            "ocrJobs": len(jobs),
            "applications": dict(sorted(applications.items())),
            "proposalMethods": dict(sorted(methods.items())),
        },
        "ocr": {
            "framework": "Apple Vision",
            "recognitionLevel": "accurate",
            "usesLanguageCorrection": True,
            "automaticallyDetectsLanguage": True,
            "platform": platform.platform(),
            "sourceSHA256": sha256(ocr_source),
            "comparison": "controlled_rerun_of_v1_and_v2_on_the_same_retained_screenshot",
        },
        "artifactDigestsSHA256": {
            "observations.jsonl": sha256(observations_path),
            "ocr-jobs.jsonl": sha256(jobs_path),
        },
    }
    (output / "review.json").write_text(
        json.dumps(review, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(output), **review["counts"]}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
