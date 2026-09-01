#!/usr/bin/env python3
"""Audit a Phase 1 paired conditioning diagnostic without model calls."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


EXPECTED_DIAGNOSTIC_VERSION = "phase1-conditioning-diagnostic-v6"


class AuditError(RuntimeError):
    pass


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AuditError(message)


def parse_lines(value: str, example_id: str, variant: str) -> list[Any]:
    rows: list[Any] = []
    for ordinal, line in enumerate(value.splitlines(), 1):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as error:
            if ordinal == 1 and line.strip():
                rows.append(line)
                continue
            raise AuditError(
                f"{example_id} {variant} line {ordinal} is not JSON: {error}"
            ) from error
        require(isinstance(parsed, dict), f"{example_id} {variant} line {ordinal} is not an object")
        rows.append(parsed)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--tokenizer", type=Path)
    arguments = parser.parse_args()
    artifact = arguments.artifact.expanduser().resolve()
    manifest_path = artifact / "manifest.json"
    examples_path = artifact / "examples.jsonl"
    jobs_path = artifact / "ocr-jobs.jsonl"
    results_path = artifact / "ocr-results.jsonl"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tokenizer_path = (
        arguments.tokenizer.expanduser().resolve()
        if arguments.tokenizer
        else Path(manifest["source"]["tokenizer"])
    )
    try:
        from transformers import AutoTokenizer
    except ImportError as error:
        raise AuditError("run this audit with the repository tokenizer venv") from error
    tokenizer = AutoTokenizer.from_pretrained(
        tokenizer_path, use_fast=True, split_special_tokens=True
    )
    examples = load_jsonl(examples_path)
    jobs = load_jsonl(jobs_path)
    results = load_jsonl(results_path)

    require(manifest.get("artifactType") == "phase1_conditioning_diagnostic", "wrong artifact type")
    require(
        manifest.get("diagnosticVersion") == EXPECTED_DIAGNOSTIC_VERSION,
        f"wrong diagnostic version: expected {EXPECTED_DIAGNOSTIC_VERSION}",
    )
    require(digest_file(tokenizer_path / "tokenizer.json") == manifest["source"]["tokenizerJSONSHA256"], "tokenizer digest differs")
    require(manifest["counts"]["examples"] == len(examples), "example count differs from manifest")
    require(manifest["counts"]["uniqueScreenshotsProcessed"] == len(jobs), "job count differs from manifest")
    require(len(jobs) == len(results), "OCR job/result counts differ")
    require(len({row["exampleID"] for row in examples}) == len(examples), "duplicate example IDs")
    require(len({row["sampleOrdinal"] for row in examples}) == len(examples), "duplicate sample ordinals")
    require([row["sampleOrdinal"] for row in examples] == list(range(1, len(examples) + 1)), "sample ordinals are not contiguous")

    for name, expected in manifest["artifactDigestsSHA256"].items():
        require(digest_file(artifact / name) == expected, f"artifact digest differs: {name}")

    jobs_by_id = {row["jobID"]: row for row in jobs}
    results_by_id = {row["jobID"]: row for row in results}
    require(set(jobs_by_id) == set(results_by_id), "OCR job/result IDs differ")
    for job_id, job in jobs_by_id.items():
        screenshot = Path(job["imagePath"])
        require(screenshot.is_file(), f"missing screenshot for {job_id}")
        require(digest_file(screenshot) == job["screenshotSHA256"], f"screenshot digest differs for {job_id}")
        require(not results_by_id[job_id].get("error"), f"OCR result has an error for {job_id}")

    changed_instances = 0
    applications: dict[str, int] = {}
    for example in examples:
        example_id = example["exampleID"]
        applications[example["application"]] = applications.get(example["application"], 0) + 1
        paste_action_count = example.get("pasteActionCount")
        require(
            isinstance(paste_action_count, int)
            and not isinstance(paste_action_count, bool)
            and paste_action_count >= 0,
            f"{example_id} has no valid structured paste-action count",
        )
        require(
            example.get("baseline", {}).get("pasteActionCount")
            == paste_action_count,
            f"{example_id} baseline paste-action count differs",
        )
        variants = example["variants"]
        require(set(variants) == {"current", "destination_only", "surface_only", "combined"}, f"{example_id} has wrong variants")

        parsed: dict[str, list[Any]] = {}
        for variant_name, variant in variants.items():
            text = variant["semanticModelInput"]
            require(digest_text(text) == variant["semanticModelInputSHA256"], f"{example_id} {variant_name} text digest differs")
            parsed[variant_name] = parse_lines(text, example_id, variant_name)
            packing = variant["packing"]
            token_count = len(tokenizer.encode(text, add_special_tokens=False))
            require(token_count == packing["modelInputTokenCount"], f"{example_id} {variant_name} token count differs")
            require(token_count <= packing["inputTokenBudget"], f"{example_id} {variant_name} exceeds its token budget")
            require(packing["instructionAndRightEdgeQueryPreserved"] is True, f"{example_id} {variant_name} does not preserve fixed edges")

        current = parsed["current"]
        destination = parsed["destination_only"]
        surface = parsed["surface_only"]
        combined = parsed["combined"]
        line_count = len(current)
        require(line_count >= 2, f"{example_id} has too few semantic lines")
        require(variants["current"]["semanticModelInputSHA256"] == example["baseline"]["semanticModelInputSHA256"], f"{example_id} current context is not the baseline context")

        # The fixed task instruction and right-edge query survive event-aware
        # left truncation. Destination changes only that query; surface changes
        # only declared READ records in the mapped retained history.
        require(current[0] == destination[0] == surface[0] == combined[0], f"{example_id} task instruction differs")
        require(surface[-1] == current[-1], f"{example_id} surface intervention altered query")
        require(combined[-1] == destination[-1], f"{example_id} combined intervention differs from destination query")
        require(destination[-1].get("kind") == "write_conditioning_state", f"{example_id} final line is not a conditioning query")

        changed_indices = {
            value["originalSemanticLineIndex"] for value in example["changedReads"]
        }
        require(0 < len(changed_indices) <= manifest["interventions"]["readSurface"]["recentReadLimitPerExample"], f"{example_id} changed READ count is outside the declared bound")
        require(len(changed_indices) == len(example["changedReads"]), f"{example_id} changed READ metadata has duplicate indices")
        for index in changed_indices:
            require(current[index].get("kind") == "read", f"{example_id} declared change is not a baseline READ")

        for variant_name in ("destination_only", "surface_only", "combined"):
            variant = variants[variant_name]
            values = parsed[variant_name]
            packing = variant["packing"]
            retained_indices = packing["retainedOriginalSemanticLineIndices"]
            require(len(retained_indices) == len(values) - 2, f"{example_id} {variant_name} retained mapping differs")
            require(retained_indices == sorted(retained_indices), f"{example_id} {variant_name} retained mapping is unordered")
            partial = packing["partiallyRetainedOriginalSemanticLineIndex"]
            for output_index, original_index in enumerate(retained_indices, 1):
                actual = values[output_index]
                baseline = current[original_index]
                if variant_name in {"surface_only", "combined"} and original_index in changed_indices:
                    require(original_index != partial, f"{example_id} {variant_name} partially retained a changed READ")
                    require(actual.get("kind") == "read", f"{example_id} {variant_name} replacement is not a READ")
                    require(actual.get("source", {}).get("captureScope") == "active_surface_proxy", f"{example_id} {variant_name} replacement lacks capture scope")
                    require(baseline.get("source", {}).get("application") == actual.get("source", {}).get("application"), f"{example_id} {variant_name} changed READ application")
                    require(baseline.get("source", {}).get("window") == actual.get("source", {}).get("window"), f"{example_id} {variant_name} changed READ window")
                elif original_index != partial:
                    require(actual == baseline, f"{example_id} {variant_name} altered undeclared history")
                else:
                    require(actual.get("contentTruncatedForPacking") is True, f"{example_id} {variant_name} partial line lacks truncation marker")
            if variant_name in {"surface_only", "combined"}:
                require(changed_indices <= set(retained_indices), f"{example_id} {variant_name} dropped a changed READ")

        require(example.get("targetBeganAt"), f"{example_id} lacks target beganAt")
        for change in example["changedReads"]:
            changed_instances += 1
            require(change.get("capturedAt"), f"{example_id} changed READ lacks capturedAt")
            require(change["capturedAt"] < example["targetBeganAt"], f"{example_id} uses a READ captured at/after target beganAt")
            screenshot = Path(change["screenshotPath"])
            require(screenshot.is_file(), f"{example_id} changed READ screenshot is missing")
            require(digest_file(screenshot) == change["screenshotSHA256"], f"{example_id} changed READ screenshot digest differs")
            require(0 <= change["adjacentOverlapRemovedLineCount"], f"{example_id} has negative overlap count")

    require(changed_instances == manifest["counts"]["changedReadInstances"], "changed READ count differs from manifest")
    require(applications == manifest["selection"]["applicationCounts"], "application counts differ from manifest")
    require(manifest["selection"]["targetContentUsedForSelection"] is False, "selection is not declared target-blind")

    print(json.dumps({
        "status": "pass",
        "artifact": str(artifact),
        "examples": len(examples),
        "applications": applications,
        "changedReadInstances": changed_instances,
        "uniqueScreenshots": len(jobs),
        "providerCalls": 0,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
