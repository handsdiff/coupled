#!/usr/bin/env python3
"""Audit immutable Phase 1 READ-surface OCR evidence against raw screenshots."""

from __future__ import annotations

import argparse
import hashlib
import json
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
    rule_version = manifest.get("ruleVersion")
    require(
        rule_version in {
            V1_SURFACE_RULE_VERSION, V2_SURFACE_RULE_VERSION,
            V3_SURFACE_RULE_VERSION, V4_SURFACE_RULE_VERSION,
            V5_SURFACE_RULE_VERSION, V6_SURFACE_RULE_VERSION,
            V7_SURFACE_RULE_VERSION,
        },
        "unsupported rule version",
    )
    source = Path(manifest["source"]["directory"]).resolve()
    session_path = source / "session.json"
    raw_path = source / "raw.jsonl"
    require(session_path.is_file() and raw_path.is_file(), "source session is unavailable")
    session = load_json(session_path)
    raw_screen_schema = session.get("schemas", {}).get("rawScreenOCR")
    if not isinstance(raw_screen_schema, int):
        raw_screen_schema = 0
    require(
        rule_version not in {
            V2_SURFACE_RULE_VERSION, V3_SURFACE_RULE_VERSION,
            V4_SURFACE_RULE_VERSION, V5_SURFACE_RULE_VERSION,
            V6_SURFACE_RULE_VERSION, V7_SURFACE_RULE_VERSION,
        }
        or raw_screen_schema >= 7,
        f"{rule_version} requires rawScreenOCR schema 7+",
    )
    surface_selector = {
        V1_SURFACE_RULE_VERSION: v1_surface_region,
        V2_SURFACE_RULE_VERSION: v2_surface_region,
        V3_SURFACE_RULE_VERSION: v3_surface_region,
        V4_SURFACE_RULE_VERSION: v4_surface_region,
    }.get(rule_version)
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
    included_record_types = manifest.get("ruleSelection", {}).get(
        "includedRecordTypes", ["screen_ocr_observation"]
    )
    require(
        isinstance(included_record_types, list)
        and included_record_types
        and set(included_record_types).issubset(
            {"screen_ocr_observation", "visual_ocr_observation"}
        ),
        "invalid included READ record types",
    )
    read_ids = {
        row["recordID"] for row in raw_rows
        if row.get("recordType") in included_record_types
        and isinstance(row.get("recordID"), str)
    }
    stateful_resolver = {
        V6_SURFACE_RULE_VERSION: V6PaneResolver,
        V7_SURFACE_RULE_VERSION: V7PaneResolver,
    }.get(rule_version)
    stateful_reviews = (
        stateful_resolver().resolve_records([
            (line, row) for line, row in enumerate(raw_rows, 1)
            if row.get("recordType") in included_record_types
        ])
        if stateful_resolver is not None else {}
    )
    jobs = load_jsonl(jobs_path)
    evidence = load_jsonl(evidence_path)
    unresolved = load_jsonl(unresolved_path)
    require(len({row["jobID"] for row in jobs}) == len(jobs), "duplicate job ID")
    dual_projection = rule_version in {
        V5_SURFACE_RULE_VERSION, V6_SURFACE_RULE_VERSION,
        V7_SURFACE_RULE_VERSION,
    }
    expected_jobs_per_source = 2 if dual_projection else 1
    require(
        len(jobs) == len({row["sourceRecordID"] for row in jobs})
            * expected_jobs_per_source,
        "unexpected jobs per source",
    )
    if dual_projection:
        jobs_by_source: dict[str, set[str]] = {}
        for row in jobs:
            jobs_by_source.setdefault(str(row["sourceRecordID"]), set()).add(
                str(row.get("projection"))
            )
        require(
            all(
                projections
                == {"authoritative_full_pane", "comparison_interior"}
                for projections in jobs_by_source.values()
            ),
            "v5 source lacks exactly one full-pane and one comparison job",
        )
    require(len({row["sourceRecordID"] for row in evidence}) == len(evidence), "duplicate evidence source")

    jobs_by_id = {row["jobID"]: row for row in jobs}
    for job in jobs:
        require(
            job.get("surfaceSelection", {}).get("ruleVersion") == rule_version,
            f"job rule version differs: {job['jobID']}",
        )
        record = raw_by_id.get(job["sourceRecordID"])
        require(record is not None, f"job source is missing: {job['sourceRecordID']}")
        if rule_version in {V6_SURFACE_RULE_VERSION, V7_SURFACE_RULE_VERSION}:
            review = stateful_reviews.get(str(record.get("recordID")))
            resolved = (
                (
                    v7_surface_regions_from_review(review)
                    if rule_version == V7_SURFACE_RULE_VERSION
                    else v6_surface_regions_from_review(review)
                )
                if isinstance(review, dict) else None
            )
            require(resolved is not None, f"job source is unresolved: {job['jobID']}")
            assert resolved is not None
            full, comparison, selection = resolved
            expected_regions = {
                "authoritative_full_pane": full,
                "comparison_interior": comparison,
            }
            projection = job.get("projection")
            require(projection in expected_regions, f"invalid projection: {job['jobID']}")
            region = expected_regions[projection]
        elif rule_version == V5_SURFACE_RULE_VERSION:
            full, comparison, selection = v5_surface_regions(record)
            expected_regions = {
                "authoritative_full_pane": full,
                "comparison_interior": comparison,
            }
            projection = job.get("projection")
            require(projection in expected_regions, f"invalid projection: {job['jobID']}")
            region = expected_regions[projection]
        else:
            assert surface_selector is not None
            region, selection = surface_selector(record)
            projection = "authoritative"
        require(region == job["regionOfInterest"], f"job region differs: {job['jobID']}")
        require(selection == job["surfaceSelection"], f"job selection differs: {job['jobID']}")
        identity = {
            "recordID": record["recordID"],
            "screenshotSHA256": record.get("screenshotSHA256"),
            "region": region,
        }
        if dual_projection:
            identity["projection"] = projection
        identity["ruleVersion"] = rule_version
        expected_job_id = "surface_" + digest_text(canonical(identity))
        require(expected_job_id == job["jobID"], f"job identity differs: {job['jobID']}")
        screenshot = (source / job["screenshotRelativePath"]).resolve()
        require(screenshot.is_file(), f"screenshot is missing: {screenshot}")
        require(sha256(screenshot) == job["screenshotSHA256"], f"screenshot hash differs: {job['jobID']}")

    for row in evidence:
        require(row.get("ruleVersion") == rule_version, f"evidence rule differs: {row['jobID']}")
        require(row.get("sessionID") == manifest.get("sessionID"), f"evidence session differs: {row['jobID']}")
        job = jobs_by_id.get(row["jobID"])
        require(job is not None, f"evidence job is missing: {row['jobID']}")
        require(row["sourceRecordID"] == job["sourceRecordID"], f"evidence source differs: {row['jobID']}")
        require(row["regionOfInterest"] == job["regionOfInterest"], f"evidence region differs: {row['jobID']}")
        require(row["surfaceSelection"] == job["surfaceSelection"], f"evidence selection differs: {row['jobID']}")
        require(digest_text(row["content"]) == row["contentSHA256"], f"content hash differs: {row['jobID']}")
        require(len(row["lines"]) == row["recognizedLineCount"], f"line count differs: {row['jobID']}")
        require("\n".join(line["text"] for line in row["lines"]) == row["content"], f"line content differs: {row['jobID']}")
        if rule_version in {V6_SURFACE_RULE_VERSION, V7_SURFACE_RULE_VERSION}:
            selection = row.get("surfaceSelection", {})
            require(selection.get("resolved") is True, "v6 evidence is not resolved")
            require(
                selection.get("method") != "v1_fallback"
                and selection.get("isV1Fallback") is False,
                "v6 promoted the pointer crop to authoritative evidence",
            )
            if selection.get("method") == "recovered_prior_ax_pane":
                recovery = selection.get("recovery", {})
                require(
                    recovery.get("sameCanonicalWindow") is True
                    and isinstance(recovery.get("sourceRecordID"), str),
                    "v6 recovered pane lacks same-window lineage",
                )
            if selection.get("method") == "canonicalized_prior_outer_ax_pane":
                canonicalization = selection.get("canonicalization", {})
                require(
                    rule_version == V7_SURFACE_RULE_VERSION
                    and canonicalization.get("policy")
                        == "near_simultaneous_cross_sensor_nested_pane_v1"
                    and isinstance(canonicalization.get("sourceRecordID"), str),
                    "v7 canonical pane lacks source lineage",
                )
        if dual_projection:
            job_ids = row.get("jobIDs")
            require(
                isinstance(job_ids, list) and len(job_ids) == 2
                and set(job_ids).issubset(jobs_by_id),
                f"v5 evidence jobs differ: {row['jobID']}",
            )
            comparison_jobs = [
                jobs_by_id[job_id] for job_id in job_ids
                if jobs_by_id[job_id].get("projection") == "comparison_interior"
            ]
            require(len(comparison_jobs) == 1, f"comparison job missing: {row['jobID']}")
            comparison_job = comparison_jobs[0]
            require(
                row.get("comparisonRegionOfInterest")
                    == comparison_job["regionOfInterest"],
                f"comparison region differs: {row['jobID']}",
            )
            require(
                digest_text(row["comparisonContent"])
                    == row["comparisonContentSHA256"],
                f"comparison content hash differs: {row['jobID']}",
            )
            require(
                len(row["comparisonLines"])
                    == row["comparisonRecognizedLineCount"],
                f"comparison line count differs: {row['jobID']}",
            )
            require(
                "\n".join(line["text"] for line in row["comparisonLines"])
                    == row["comparisonContent"],
                f"comparison line content differs: {row['jobID']}",
            )

    for row in unresolved:
        require(row.get("ruleVersion") == rule_version, "unresolved rule differs")
        require(row.get("sessionID") == manifest.get("sessionID"), "unresolved session differs")
        if rule_version in {
            V6_SURFACE_RULE_VERSION, V7_SURFACE_RULE_VERSION,
        } and row.get("reason") in {
            "no_trustworthy_current_or_prior_ax_pane",
            "window_identity_unavailable_for_pane_recovery",
        }:
            review = stateful_reviews.get(str(row.get("sourceRecordID")))
            require(
                isinstance(review, dict)
                and review.get("proposal", {}).get("resolved") is False
                and review.get("proposal", {}).get("regionOfInterest") is None,
                "v6 unresolved pane unexpectedly has an authoritative region",
            )

    disposition_ids = {row["sourceRecordID"] for row in evidence} | {
        row["sourceRecordID"] for row in unresolved
    }
    require(disposition_ids == read_ids, "READ observations lack exactly one evidence disposition")
    require(
        len(evidence) + len(unresolved) == len(read_ids),
        "evidence and unresolved dispositions overlap",
    )
    counts = manifest["counts"]
    require(counts["rawRecords"] == len(raw_rows), "raw count differs")
    require(counts.get("readObservations", counts["screenObservations"]) == len(read_ids), "READ count differs")
    require(
        counts["screenObservations"]
        == sum(row.get("recordType") == "screen_ocr_observation" for row in raw_rows),
        "screen count differs",
    )
    require(
        counts.get("visualObservations", 0)
        == sum(
            row.get("recordType") == "visual_ocr_observation"
            for row in raw_rows
            if "visual_ocr_observation" in included_record_types
        ),
        "visual count differs",
    )
    require(counts["jobs"] == len(jobs), "job count differs")
    require(counts["evidence"] == len(evidence), "evidence count differs")
    require(counts["unresolved"] == len(unresolved), "unresolved count differs")
    print(json.dumps({
        "artifact": str(artifact),
        "evidence": len(evidence),
        "jobs": len(jobs),
        "readObservations": len(read_ids),
        "status": "pass",
        "unresolved": len(unresolved),
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
