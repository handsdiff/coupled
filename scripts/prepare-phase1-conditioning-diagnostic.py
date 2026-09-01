#!/usr/bin/env python3
"""Build a target-blind paired context diagnostic from retained Phase 1 evidence."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import math
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any


DIAGNOSTIC_VERSION = "phase1-conditioning-diagnostic-v6"
SURFACE_RULE_VERSION = "pointer-local-read-v1"
DESTINATION_RULE_VERSION = "normalized-destination-surface-v2"
DEFAULT_ISSUE_ANCHORS = (51, 65)


class DiagnosticError(RuntimeError):
    pass


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(canonical(row) + "\n")


def iso8601() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--semantic-pack", required=True, type=Path)
    parser.add_argument("--baseline-scores", required=True, type=Path)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--ocr-source", required=True, type=Path)
    parser.add_argument("--tokenizer", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--reuse-ocr-results",
        action="append",
        type=Path,
        default=[],
        help="Prior diagnostic ocr-results.jsonl whose matching job IDs may be reused.",
    )
    parser.add_argument("--quantiles-per-application", type=int, default=7)
    parser.add_argument("--recent-read-limit", type=int, default=12)
    parser.add_argument("--input-token-budget", type=int, default=32_768)
    parser.add_argument(
        "--issue-anchor",
        action="append",
        type=int,
        dest="issue_anchors",
        help="One-based corpus/UI ordinal to include in addition to app quantiles.",
    )
    return parser.parse_args()


def choose_quantiles(rows: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    if count <= 0 or not rows:
        return []
    if len(rows) <= count:
        return rows[:]
    positions = [round(index * (len(rows) - 1) / (count - 1)) for index in range(count)]
    return [rows[position] for position in positions]


def select_examples(
    scored: list[dict[str, Any]],
    quantiles_per_application: int,
    issue_anchors: tuple[int, ...],
) -> tuple[list[dict[str, Any]], set[str], dict[str, list[int]]]:
    by_application: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in scored:
        by_application[str(row.get("application") or "Unknown")].append(row)
    selected: dict[str, dict[str, Any]] = {}
    quantile_ids: set[str] = set()
    anchor_ordinals: dict[str, list[int]] = defaultdict(list)
    for application in sorted(by_application):
        for row in choose_quantiles(by_application[application], quantiles_per_application):
            selected[row["exampleID"]] = row
            quantile_ids.add(row["exampleID"])
    for ordinal in issue_anchors:
        matches = [row for row in scored if row["corpusOrdinal"] == ordinal]
        if len(matches) != 1:
            raise DiagnosticError(
                f"issue anchor corpus ordinal {ordinal} is not one scored example"
            )
        row = matches[0]
        selected[row["exampleID"]] = row
        anchor_ordinals[row["exampleID"]].append(ordinal)
    ordered = [row for row in scored if row["exampleID"] in selected]
    return ordered, quantile_ids, anchor_ordinals


def find_session_directories(data_root: Path, session_ids: set[str]) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for manifest_path in sorted(data_root.glob("*/session.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        session_id = manifest.get("sessionID")
        if session_id in session_ids:
            found[session_id] = manifest_path.parent.resolve()
    missing = session_ids - set(found)
    if missing:
        raise DiagnosticError(f"source session directories are missing: {sorted(missing)}")
    return found


def clamp(value: float, lower: float, upper: float) -> float:
    return min(max(value, lower), upper)


def profile_region(bundle: str) -> tuple[float, float, float, float]:
    if bundle == "com.openai.codex":
        return (0.18, 0.05, 0.80, 0.90)
    if bundle == "com.google.Chrome":
        return (0.08, 0.10, 0.84, 0.82)
    if bundle == "md.obsidian":
        return (0.18, 0.05, 0.80, 0.90)
    return (0.08, 0.08, 0.84, 0.84)


def pointer_dimensions(bundle: str) -> tuple[float, float]:
    if bundle == "com.microsoft.VSCode":
        return (0.68, 0.50)
    if bundle == "com.openai.codex":
        return (0.72, 0.72)
    return (0.72, 0.68)


def surface_region(record: dict[str, Any]) -> tuple[dict[str, float], dict[str, Any]]:
    bundle = str(record.get("bundleIdentifier") or "")
    bounds = record.get("windowBounds") or {}
    trigger_types = set(record.get("triggerTypes") or [])
    pointer_trigger = bool(
        trigger_types
        & {"click", "scroll", "pointer_moved", "pointer_dragged", "pointer_settled"}
    )
    try:
        width = float(bounds["width"])
        height = float(bounds["height"])
        normalized_x = (float(record["x"]) - float(bounds["x"])) / width
        normalized_top = (float(record["y"]) - float(bounds["y"])) / height
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        normalized_x = math.nan
        normalized_top = math.nan
    pointer_inside = (
        pointer_trigger
        and math.isfinite(normalized_x)
        and math.isfinite(normalized_top)
        and -0.02 <= normalized_x <= 1.02
        and -0.02 <= normalized_top <= 1.02
    )
    if pointer_inside:
        region_width, region_height = pointer_dimensions(bundle)
        left = clamp(normalized_x - region_width / 2, 0, 1 - region_width)
        top = clamp(normalized_top - region_height / 2, 0, 1 - region_height)
        method = "pointer_local"
        confidence = "interaction_locus"
    else:
        left, top, region_width, region_height = profile_region(bundle)
        method = "application_profile"
        confidence = "activation_or_missing_pointer_fallback"
    region = {
        "x": round(left, 8),
        "y": round(1 - top - region_height, 8),
        "width": round(region_width, 8),
        "height": round(region_height, 8),
    }
    provenance = {
        "ruleVersion": SURFACE_RULE_VERSION,
        "method": method,
        "confidence": confidence,
        "triggerTypes": sorted(trigger_types),
        "pointerNormalized": (
            {"x": round(normalized_x, 8), "top": round(normalized_top, 8)}
            if math.isfinite(normalized_x) and math.isfinite(normalized_top)
            else None
        ),
        "regionOfInterest": region,
    }
    return region, provenance


def normalize_destination(query: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    updated = json.loads(json.dumps(query))
    destination = updated.get("destination")
    if not isinstance(destination, dict):
        return updated, {"ruleVersion": DESTINATION_RULE_VERSION, "surfaceKind": "unknown"}
    app = str(destination.get("appName") or "")
    bundle = str(destination.get("bundleIdentifier") or "")
    description = str(destination.get("fieldDescription") or "")
    label = str(destination.get("fieldLabel") or "")
    window = str(destination.get("windowTitle") or "")
    lowered = f"{description} {label}".lower()
    resource_title: str | None = window or None
    surface_label: str | None = None
    if bundle == "com.microsoft.VSCode":
        if "terminal" in lowered and "coupled" in lowered:
            surface_kind = "codex_cli_terminal"
        elif "terminal" in lowered and "zsh" in lowered:
            surface_kind = "shell_terminal"
        elif "terminal" in lowered:
            surface_kind = "integrated_terminal"
        else:
            surface_kind = "code_editor"
        if "terminal" in lowered:
            # A VS Code window title names the file behind the panel, not the
            # terminal/Codex surface receiving this write. Keep it as raw
            # evidence in windowTitle, but do not promote it to resourceTitle.
            resource_title = None
            surface_label = description.split(" Use ", 1)[0].strip() or None
    elif bundle == "com.openai.codex":
        surface_kind = "chat_prompt"
    elif bundle == "md.obsidian":
        surface_kind = "obsidian_note_editor"
        resource_title = window.split(" - Notes - ", 1)[0] if " - Notes - " in window else window
    elif bundle == "com.google.Chrome":
        if "gemini" in lowered:
            surface_kind = "gemini_prompt"
        elif "claude" in lowered:
            surface_kind = "claude_prompt"
        elif "search" in lowered:
            surface_kind = "browser_search"
        elif "post text" in lowered:
            surface_kind = "social_post_composer"
        elif "question" in lowered or "required" in lowered:
            surface_kind = "web_form"
        else:
            surface_kind = "browser_text_field"
    else:
        surface_kind = "editable_text_field"
    destination["surfaceKind"] = surface_kind
    if resource_title:
        destination["resourceTitle"] = resource_title
    else:
        destination.pop("resourceTitle", None)
    if surface_label:
        destination["surfaceLabel"] = surface_label
    provenance = {
        "ruleVersion": DESTINATION_RULE_VERSION,
        "application": app,
        "surfaceKind": surface_kind,
        "surfaceLabel": surface_label,
        "resourceTitle": resource_title,
        "evidence": {
            "bundleIdentifier": bundle,
            "fieldDescription": description,
            "fieldLabel": label,
            "role": destination.get("role"),
            "windowTitle": window,
        },
    }
    return updated, provenance


def normalized_line(value: str) -> str:
    return " ".join(value.split()).casefold()


def remove_adjacent_line_overlap(
    previous_lines: list[str], current_lines: list[str]
) -> tuple[list[str], int]:
    maximum = min(len(previous_lines), len(current_lines))
    normalized_previous = [normalized_line(value) for value in previous_lines]
    normalized_current = [normalized_line(value) for value in current_lines]
    for count in range(maximum, 0, -1):
        if normalized_previous[-count:] == normalized_current[:count]:
            return current_lines[count:], count
    return current_lines, 0


def compile_ocr(source: Path, output: Path) -> None:
    command = [
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
    ]
    subprocess.run(command, check=True)


def load_packer(project: Path) -> Any:
    path = project / "scripts/pack-phase1-dataset.py"
    specification = importlib.util.spec_from_file_location(
        "phase1_conditioning_diagnostic_packer", path
    )
    if specification is None or specification.loader is None:
        raise DiagnosticError(f"cannot load tokenizer packer: {path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def repack_semantic_input(
    value: str,
    tokenizer: Any,
    packer: Any,
    token_budget: int,
) -> tuple[str, dict[str, Any]]:
    lines = value.splitlines()
    if len(lines) < 2:
        raise DiagnosticError("semantic input lacks instruction/query boundaries")
    instruction = lines[0]
    context = lines[1:-1]
    query = lines[-1]
    instruction_count = len(packer.encode_plain_text(tokenizer, instruction + "\n"))
    query_count = len(packer.encode_plain_text(tokenizer, query))
    remaining = token_budget - instruction_count - query_count
    if remaining < 0:
        raise DiagnosticError("instruction and query exceed the diagnostic token budget")
    retained_reversed: list[tuple[int, str]] = []
    partial_index: int | None = None
    for semantic_line_index in range(len(lines) - 2, 0, -1):
        serialized = lines[semantic_line_index]
        token_count = len(packer.encode_plain_text(tokenizer, serialized + "\n"))
        if token_count <= remaining:
            retained_reversed.append((semantic_line_index, serialized))
            remaining -= token_count
            continue
        truncated = packer.truncate_oldest_event(serialized, remaining, tokenizer)
        if truncated is not None:
            truncated_text, token_ids, _ = truncated
            retained_reversed.append((semantic_line_index, truncated_text))
            partial_index = semantic_line_index
            remaining -= len(token_ids)
        break
    retained = list(reversed(retained_reversed))
    body = "\n".join(text for _, text in retained)
    packed = instruction + "\n" + (body + "\n" if body else "") + query
    actual_count = len(packer.encode_plain_text(tokenizer, packed))
    if actual_count > token_budget:
        raise DiagnosticError(
            f"diagnostic repack produced {actual_count} tokens for budget {token_budget}"
        )
    return packed, {
        "algorithm": "event_aware_left_truncation_v1",
        "inputTokenBudget": token_budget,
        "modelInputTokenCount": actual_count,
        "sourceContextLineCount": len(context),
        "retainedContextLineCount": len(retained),
        "droppedContextLineCount": len(context) - len(retained),
        "retainedOriginalSemanticLineIndices": [index for index, _ in retained],
        "partiallyRetainedOriginalSemanticLineIndex": partial_index,
        "instructionAndRightEdgeQueryPreserved": True,
    }


def run_ocr(executable: Path, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload = "".join(canonical(job) + "\n" for job in jobs)
    completed = subprocess.run(
        [str(executable)],
        input=payload,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise DiagnosticError(
            f"surface OCR failed with {completed.returncode}: {completed.stderr.strip()}"
        )
    rows = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    if len(rows) != len(jobs):
        raise DiagnosticError(f"surface OCR returned {len(rows)} rows for {len(jobs)} jobs")
    errors = [row for row in rows if row.get("error")]
    if errors:
        raise DiagnosticError(f"surface OCR produced errors: {errors[:3]}")
    return rows


def variant_input(
    source_row: dict[str, Any],
    surface_by_block: dict[str, dict[str, Any]],
    allowed_surface_blocks: set[str],
    normalize_query: bool,
) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None]:
    lines = source_row["semanticModelInput"].splitlines()
    blocks = source_row["retainedContextBlocks"]
    if len(lines) != len(blocks) + 2:
        raise DiagnosticError(
            f"{source_row['exampleID']} has {len(lines)} lines for {len(blocks)} blocks"
        )
    changed_reads: list[dict[str, Any]] = []
    previous_changed_read: tuple[tuple[str, str], list[str]] | None = None
    for index, block in enumerate(blocks, 1):
        block_id = block["contextBlockID"]
        value = json.loads(lines[index])
        if value.get("kind") != "read":
            previous_changed_read = None
            continue
        if block_id not in allowed_surface_blocks:
            previous_changed_read = None
            continue
        surface = surface_by_block.get(block_id)
        if not surface:
            previous_changed_read = None
            continue
        original_content = str(value.get("content") or "")
        full_surface_content = str(surface["ocr"]["content"])
        if not full_surface_content.strip():
            previous_changed_read = None
            continue
        full_lines = full_surface_content.splitlines()
        source = value.setdefault("source", {})
        identity = (
            str(source.get("application") or ""),
            str(source.get("window") or ""),
        )
        overlap_removed = 0
        replacement_lines = full_lines
        if previous_changed_read and previous_changed_read[0] == identity:
            replacement_lines, overlap_removed = remove_adjacent_line_overlap(
                previous_changed_read[1], full_lines
            )
        replacement_content = "\n".join(replacement_lines)
        value["content"] = replacement_content
        source["captureScope"] = "active_surface_proxy"
        lines[index] = canonical(value)
        previous_changed_read = (identity, full_lines)
        changed_reads.append({
            "originalSemanticLineIndex": index,
            "contextBlockID": block_id,
            "sourceRecordID": surface["sourceRecordID"],
            "originalCharacterCount": len(original_content),
            "replacementCharacterCount": len(replacement_content),
            "originalContentSHA256": digest_text(original_content),
            "replacementContentSHA256": digest_text(replacement_content),
            "fullSurfaceCharacterCount": len(full_surface_content),
            "adjacentOverlapRemovedLineCount": overlap_removed,
            "surfaceSelection": surface["surfaceSelection"],
            "screenshotPath": surface["screenshotPath"],
            "screenshotSHA256": surface["screenshotSHA256"],
            "capturedAt": surface["capturedAt"],
        })
    destination_provenance = None
    if normalize_query:
        query = json.loads(lines[-1])
        query, destination_provenance = normalize_destination(query)
        lines[-1] = canonical(query)
    return "\n".join(lines), changed_reads, destination_provenance


def main() -> int:
    arguments = parse_arguments()
    corpus = arguments.corpus.expanduser().resolve()
    semantic_pack = arguments.semantic_pack.expanduser().resolve()
    baseline_scores_path = arguments.baseline_scores.expanduser().resolve()
    data_root = arguments.data_root.expanduser().resolve()
    ocr_source = arguments.ocr_source.expanduser().resolve()
    tokenizer_path = arguments.tokenizer.expanduser().resolve()
    output = arguments.output.expanduser().resolve()
    issue_anchors = tuple(arguments.issue_anchors or DEFAULT_ISSUE_ANCHORS)
    if output.exists() and any(output.iterdir()):
        raise DiagnosticError(f"output must be absent or empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    project = Path(__file__).resolve().parent.parent
    packer = load_packer(project)
    tokenizer = packer.AutoTokenizer.from_pretrained(
        tokenizer_path, use_fast=True, split_special_tokens=True
    )

    pack_path = semantic_pack / "semantic-examples.jsonl"
    pack_rows = load_jsonl(pack_path)
    for corpus_ordinal, row in enumerate(pack_rows, 1):
        row["corpusOrdinal"] = corpus_ordinal
    scored = [row for row in pack_rows if row.get("experimentBlockID") != "block-0001"]
    for ordinal, row in enumerate(scored, 1):
        row["scoredOrdinal"] = ordinal
    selected, quantile_ids, anchor_ordinals = select_examples(
        scored,
        arguments.quantiles_per_application,
        issue_anchors,
    )
    selected_ids = {row["exampleID"] for row in selected}

    corpus_examples = {row["exampleID"]: row for row in load_jsonl(corpus / "examples.jsonl")}
    context_blocks = {
        row["contextBlockID"]: row for row in load_jsonl(corpus / "context-blocks.jsonl")
    }
    events = {
        row["sourceEventID"]: row for row in load_jsonl(corpus / "events.jsonl")
    }
    baseline_scores = {
        row["exampleID"]: row for row in load_jsonl(baseline_scores_path)
        if row["exampleID"] in selected_ids
    }
    if set(baseline_scores) != selected_ids:
        raise DiagnosticError("baseline GPT-5.6 scores do not cover the selected examples")

    read_blocks_needed: dict[str, str] = {}
    read_blocks_by_example: dict[str, set[str]] = {}
    for row in selected:
        reads: list[tuple[str, str]] = []
        for block in row["retainedContextBlocks"]:
            block_id = block["contextBlockID"]
            block_record = context_blocks.get(block_id)
            if not block_record:
                continue
            try:
                value = json.loads(block.get("serializedOverride") or block_record["serialized"])
            except json.JSONDecodeError:
                continue
            if value.get("kind") == "read":
                reads.append((block_id, block_record["sourceEventID"]))
        allowed = {block_id for block_id, _ in reads[-arguments.recent_read_limit :]}
        read_blocks_by_example[row["exampleID"]] = allowed
        for block_id, event_id in reads[-arguments.recent_read_limit :]:
            read_blocks_needed[block_id] = event_id

    raw_ids_by_session: dict[str, set[str]] = defaultdict(set)
    read_lineage: dict[str, tuple[str, str]] = {}
    for block_id, event_id in read_blocks_needed.items():
        event = events[event_id]
        source_ids = event.get("sourceRecordIDs") or []
        if not source_ids:
            continue
        source_record_id = source_ids[0]
        session_id = event["sessionID"]
        raw_ids_by_session[session_id].add(source_record_id)
        read_lineage[block_id] = (session_id, source_record_id)
    session_directories = find_session_directories(data_root, set(raw_ids_by_session))
    raw_records: dict[str, dict[str, Any]] = {}
    for session_id, record_ids in raw_ids_by_session.items():
        raw_path = session_directories[session_id] / "raw.jsonl"
        with raw_path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                value = json.loads(line)
                record_id = value.get("recordID")
                if record_id in record_ids:
                    raw_records[record_id] = value
        missing = record_ids - set(raw_records)
        if missing:
            raise DiagnosticError(f"raw records missing from {session_id}: {sorted(missing)}")

    jobs_by_key: dict[str, dict[str, Any]] = {}
    block_job: dict[str, str] = {}
    for block_id, (session_id, source_record_id) in read_lineage.items():
        record = raw_records[source_record_id]
        if record.get("recordType") != "screen_ocr_observation":
            continue
        relative_path = record.get("screenshotRelativePath")
        if not relative_path:
            continue
        screenshot = (session_directories[session_id] / relative_path).resolve()
        if not screenshot.exists():
            raise DiagnosticError(f"retained screenshot is missing: {screenshot}")
        region, provenance = surface_region(record)
        key = digest_text(canonical({
            "recordID": source_record_id,
            "screenshotSHA256": record.get("screenshotSHA256"),
            "region": region,
            "ruleVersion": SURFACE_RULE_VERSION,
        }))
        job_id = f"surface_{key}"
        jobs_by_key.setdefault(job_id, {
            "jobID": job_id,
            "imagePath": str(screenshot),
            "regionOfInterest": region,
            "sourceRecordID": source_record_id,
            "sourceSessionID": session_id,
            "screenshotSHA256": record.get("screenshotSHA256"),
            "surfaceSelection": provenance,
        })
        block_job[block_id] = job_id
    jobs = [jobs_by_key[key] for key in sorted(jobs_by_key)]
    write_jsonl(output / "ocr-jobs.jsonl", jobs)

    reusable: dict[str, dict[str, Any]] = {}
    for reuse_path in arguments.reuse_ocr_results:
        for row in load_jsonl(reuse_path.expanduser().resolve()):
            if not row.get("error"):
                reusable[row["jobID"]] = row
    missing_jobs = [job for job in jobs if job["jobID"] not in reusable]
    newly_recognized: dict[str, dict[str, Any]] = {}
    if missing_jobs:
        with tempfile.TemporaryDirectory(prefix="phase1-surface-ocr-") as temporary:
            executable = Path(temporary) / "ocr-phase1-surface-regions"
            compile_ocr(ocr_source, executable)
            newly_recognized = {
                row["jobID"]: row for row in run_ocr(executable, missing_jobs)
            }
    ocr_results = [
        reusable.get(job["jobID"]) or newly_recognized[job["jobID"]]
        for job in jobs
    ]
    write_jsonl(output / "ocr-results.jsonl", ocr_results)
    ocr_by_job = {row["jobID"]: row for row in ocr_results}
    surface_by_block: dict[str, dict[str, Any]] = {}
    for block_id, job_id in block_job.items():
        job = jobs_by_key[job_id]
        surface_by_block[block_id] = {
            "sourceRecordID": job["sourceRecordID"],
            "screenshotPath": job["imagePath"],
            "surfaceSelection": job["surfaceSelection"],
            "screenshotSHA256": job["screenshotSHA256"],
            "capturedAt": raw_records[job["sourceRecordID"]].get("capturedAt"),
            "ocr": ocr_by_job[job_id],
        }

    output_rows: list[dict[str, Any]] = []
    for row in selected:
        baseline = baseline_scores[row["exampleID"]]
        current = row["semanticModelInput"]
        allowed_blocks = read_blocks_by_example[row["exampleID"]]
        destination_only, _, destination_provenance = variant_input(
            row, {}, set(), True
        )
        surface_only, surface_changes, _ = variant_input(
            row, surface_by_block, allowed_blocks, False
        )
        combined, combined_changes, combined_destination = variant_input(
            row, surface_by_block, allowed_blocks, True
        )
        if surface_changes != combined_changes:
            raise DiagnosticError("surface-only and combined READ changes differ")
        current_token_count = len(packer.encode_plain_text(tokenizer, current))
        if current_token_count > arguments.input_token_budget:
            raise DiagnosticError("frozen current context exceeds the diagnostic token budget")
        current_line_count = len(current.splitlines())
        current_packing = {
            "algorithm": "frozen_32k_baseline",
            "inputTokenBudget": arguments.input_token_budget,
            "modelInputTokenCount": current_token_count,
            "sourceContextLineCount": current_line_count - 2,
            "retainedContextLineCount": current_line_count - 2,
            "droppedContextLineCount": 0,
            "retainedOriginalSemanticLineIndices": list(range(1, current_line_count - 1)),
            "partiallyRetainedOriginalSemanticLineIndex": None,
            "instructionAndRightEdgeQueryPreserved": True,
        }
        destination_only, destination_packing = repack_semantic_input(
            destination_only, tokenizer, packer, arguments.input_token_budget
        )
        surface_only, surface_packing = repack_semantic_input(
            surface_only, tokenizer, packer, arguments.input_token_budget
        )
        combined, combined_packing = repack_semantic_input(
            combined, tokenizer, packer, arguments.input_token_budget
        )
        changed_line_indices = {
            value["originalSemanticLineIndex"] for value in combined_changes
        }
        for variant_name, packing in (
            ("surface_only", surface_packing),
            ("combined", combined_packing),
        ):
            retained_indices = set(packing["retainedOriginalSemanticLineIndices"])
            if not changed_line_indices <= retained_indices:
                raise DiagnosticError(
                    f"{row['exampleID']} {variant_name} dropped a recent changed READ"
                )
            if packing["partiallyRetainedOriginalSemanticLineIndex"] in changed_line_indices:
                raise DiagnosticError(
                    f"{row['exampleID']} {variant_name} partially truncated a changed READ"
                )
        source_example = corpus_examples[row["exampleID"]]
        output_rows.append({
            "schemaVersion": 1,
            "diagnosticVersion": DIAGNOSTIC_VERSION,
            "sampleOrdinal": len(output_rows) + 1,
            "corpusOrdinal": row["corpusOrdinal"],
            "scoredOrdinal": row["scoredOrdinal"],
            "selection": {
                "applicationQuantile": row["exampleID"] in quantile_ids,
                "issueAnchorCorpusOrdinals": anchor_ordinals.get(row["exampleID"], []),
            },
            "exampleID": row["exampleID"],
            "targetEventID": row["targetEventID"],
            "targetBeganAt": source_example.get("targetBeganAt"),
            "application": row.get("application"),
            "target": row["target"],
            "pasteActionCount": int(row["pasteActionCount"]),
            "baseline": {
                "source": str(baseline_scores_path),
                "prediction": baseline["prediction"],
                "latencySeconds": baseline.get("latencySeconds"),
                "semanticModelInputSHA256": baseline["semanticModelInputSHA256"],
                "pasteActionCount": int(baseline["pasteActionCount"]),
            },
            "destinationNormalization": combined_destination or destination_provenance,
            "changedReads": combined_changes,
            "variants": {
                "current": {
                    "semanticModelInput": current,
                    "semanticModelInputSHA256": digest_text(current),
                    "packing": current_packing,
                },
                "destination_only": {
                    "semanticModelInput": destination_only,
                    "semanticModelInputSHA256": digest_text(destination_only),
                    "packing": destination_packing,
                },
                "surface_only": {
                    "semanticModelInput": surface_only,
                    "semanticModelInputSHA256": digest_text(surface_only),
                    "packing": surface_packing,
                },
                "combined": {
                    "semanticModelInput": combined,
                    "semanticModelInputSHA256": digest_text(combined),
                    "packing": combined_packing,
                },
            },
        })
    write_jsonl(output / "examples.jsonl", output_rows)

    application_counts: dict[str, int] = defaultdict(int)
    for row in output_rows:
        application_counts[row["application"]] += 1
    manifest = {
        "schemaVersion": 1,
        "artifactType": "phase1_conditioning_diagnostic",
        "diagnosticVersion": DIAGNOSTIC_VERSION,
        "status": "local_context_variants_complete_no_new_provider_calls",
        "createdAt": iso8601(),
        "purpose": (
            "Test whether normalized destination evidence and pointer-local READ OCR make "
            "the same next-write targets more intelligible and predictable."
        ),
        "selection": {
            "targetContentUsedForSelection": False,
            "rule": (
                "seven chronological quantiles per application plus predeclared "
                "corpus/UI issue-anchor ordinals"
            ),
            "quantilesPerApplication": arguments.quantiles_per_application,
            "issueAnchorOrdinals": list(issue_anchors),
            "sampleCount": len(output_rows),
            "applicationCounts": dict(sorted(application_counts.items())),
        },
        "interventions": {
            "packing": {
                "algorithm": "event_aware_left_truncation_v1",
                "inputTokenBudget": arguments.input_token_budget,
                "taskInstructionAndRightEdgeQueryPreserved": True,
            },
            "destination": {
                "ruleVersion": DESTINATION_RULE_VERSION,
                "usesOnlyExistingPreMutationQueryEvidence": True,
            },
            "readSurface": {
                "ruleVersion": SURFACE_RULE_VERSION,
                "recentReadLimitPerExample": arguments.recent_read_limit,
                "usesOnlyRetainedPretargetScreenshotsAndTriggerCoordinates": True,
                "fullWindowRawEvidenceMutated": False,
                "warning": (
                    "Pointer-local OCR is a deliberately simple attention proxy, not a claim "
                    "about gaze or a final pane-segmentation algorithm."
                ),
            },
        },
        "source": {
            "corpus": str(corpus),
            "corpusSHA256": sha256(corpus / "corpus.json"),
            "semanticPack": str(semantic_pack),
            "semanticExamplesSHA256": sha256(pack_path),
            "baselineScores": str(baseline_scores_path),
            "baselineScoresSHA256": sha256(baseline_scores_path),
            "ocrSource": str(ocr_source),
            "ocrSourceSHA256": sha256(ocr_source),
            "tokenizer": str(tokenizer_path),
            "tokenizerJSONSHA256": sha256(tokenizer_path / "tokenizer.json"),
        },
        "counts": {
            "examples": len(output_rows),
            "uniqueScreenshotsProcessed": len(jobs),
            "ocrResultsReused": len(jobs) - len(missing_jobs),
            "ocrResultsNew": len(missing_jobs),
            "changedReadInstances": sum(len(row["changedReads"]) for row in output_rows),
        },
        "artifactDigestsSHA256": {
            "examples.jsonl": sha256(output / "examples.jsonl"),
            "ocr-jobs.jsonl": sha256(output / "ocr-jobs.jsonl"),
            "ocr-results.jsonl": sha256(output / "ocr-results.jsonl"),
        },
        "nextStep": (
            "Run GPT-5.6 Sol xhigh only on the combined variant after the active 128K "
            "context-window arm and its audit finish; reuse the preserved 32K predictions "
            "as the current-context baseline."
        ),
    }
    write_json(output / "manifest.json", manifest)
    print(json.dumps({
        "output": str(output),
        "examples": len(output_rows),
        "applicationCounts": dict(sorted(application_counts.items())),
        "uniqueScreenshotsProcessed": len(jobs),
        "ocrResultsReused": len(jobs) - len(missing_jobs),
        "ocrResultsNew": len(missing_jobs),
        "changedReadInstances": manifest["counts"]["changedReadInstances"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
