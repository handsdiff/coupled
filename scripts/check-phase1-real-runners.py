#!/usr/bin/env python3
"""No-network invariants for the real Phase 1 provider runners."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from copy import deepcopy
from pathlib import Path

from phase1_experiment import (
    ARM_FROZEN_FRONTIER,
    ARM_FROZEN_QWEN,
    ARM_PERSONALIZED_QWEN,
    canonical_bytes,
    semantic_model_input,
    target_text,
    validate_inputs,
    write_jsonl,
)
from phase1_training_contract import TrainingContractError, sha256


def load_script(name: str, path: Path):
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


def main() -> int:
    project = Path(__file__).resolve().parent.parent
    corpus_path = (
        project
        / "coupled-data/phase1-experiment-1-corpus-v2-unredacted-canonical-20260818"
    )
    packed_path = Path(str(corpus_path) + "-qwen-pack-v7")
    if not (corpus_path.is_dir() and packed_path.is_dir()):
        print("Phase 1 real-runner checks skipped: local frozen artifacts unavailable")
        return 0

    corpus, examples, packed, plans = validate_inputs(corpus_path, packed_path)
    if len(examples) != 200 or len(packed.rows) != 200:
        raise AssertionError("frozen Phase 1 corpus count changed")
    for example in examples:
        example_id = example["exampleID"]
        if not semantic_model_input(corpus_path, example, plans[example_id]):
            raise AssertionError("semantic model input is empty")
        if not target_text(example["target"]):
            raise AssertionError("model target is empty")

    with tempfile.TemporaryDirectory(prefix="phase1-real-runner-check-") as raw:
        temporary = Path(raw)
        plan_path = temporary / "provider-plan.json"
        subprocess.run(
            [
                sys.executable,
                str(project / "scripts/prepare-phase1-experiment.py"),
                "--corpus", str(corpus_path),
                "--packed", str(packed_path),
                "--output", str(plan_path),
                "--tinker-project-id", "10b258ab-25fe-45e0-a54b-fef023154281",
                "--epochs-per-update", "1",
                "--qwen-generation-token-ceiling", "512",
                "--openai-max-output-tokens", "8192",
                "--frontier-transport", "litellm_chatgpt_subscription",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        frontier = load_script(
            "phase1_frontier_runner_check",
            project / "scripts/run-phase1-frontier-arm.py",
        )
        tinker = load_script(
            "phase1_tinker_runner_check",
            project / "scripts/run-phase1-tinker-prequential.py",
        )
        plan_digest = frontier.sha256(plan_path)
        plan, _ = frontier.validate_plan(
            plan_path, plan_digest, corpus_path, packed_path, len(examples)
        )
        tinker.validate_plan(
            plan_path,
            plan_digest,
            corpus_path,
            packed_path,
            "10b258ab-25fe-45e0-a54b-fef023154281",
        )
        if plan["tinker"]["hardExecutionCeilingUSD"] != "40.00":
            raise AssertionError("hard Tinker ceiling is not frozen")
        sequence = tinker.expected_score_sequence(corpus["blocking"]["blocks"])
        if len(sequence) != 400 or len(set(sequence)) != 400:
            raise AssertionError("Tinker score plan is not 2 x 200 unique operations")
        cumulative = [
            example_id
            for block in corpus["blocking"]["blocks"][:2]
            for example_id in block["exampleIDs"]
        ]
        if not (
            tinker.deterministic_order(cumulative, 2)
            == tinker.deterministic_order(cumulative, 2)
            and tinker.deterministic_order(cumulative, 1)
            != tinker.deterministic_order(cumulative, 2)
        ):
            raise AssertionError("cumulative training order is not deterministic")

        clean_implementation = {
            "codeRevision": "fixture-revision",
            "workingTreeDirtyAtStart": False,
            "fileDigestsSHA256": {"runner": "fixture-digest"},
            "tinkerSDKVersion": "0.25.0",
        }
        resumable = {"implementation": deepcopy(clean_implementation)}
        tinker.validate_resume_state(resumable, clean_implementation)
        for field in ("activeUpdate", "inflightOperation"):
            interrupted = deepcopy(resumable)
            interrupted[field] = {"fixture": True}
            try:
                tinker.validate_resume_state(interrupted, clean_implementation)
            except TrainingContractError:
                pass
            else:
                raise AssertionError(f"Tinker replay accepted {field}")
        changed = deepcopy(clean_implementation)
        changed["codeRevision"] = "changed-revision"
        try:
            tinker.validate_resume_state(resumable, changed)
        except TrainingContractError:
            pass
        else:
            raise AssertionError("Tinker resume accepted a changed revision")
        frontier.validate_resume_implementation(resumable, clean_implementation)
        changed_dependency = deepcopy(clean_implementation)
        changed_dependency["fileDigestsSHA256"]["runner"] = "changed"
        try:
            frontier.validate_resume_implementation(resumable, changed_dependency)
        except TrainingContractError:
            pass
        else:
            raise AssertionError("frontier resume accepted changed dependency code")

        previous_plan = deepcopy(plan)
        previous_plan["implementation"]["fileDigestsSHA256"] = {
            "scripts/run-phase1-frontier-arm.py": "old-runner-digest"
        }
        previous_plan_path = temporary / "previous-provider-plan.json"
        previous_plan_path.write_bytes(canonical_bytes(previous_plan))
        previous_plan_digest = sha256(previous_plan_path)
        prefix_path = temporary / "frontier-prefix.jsonl"
        write_jsonl(prefix_path, [{"exampleID": examples[0]["exampleID"]}])
        previous_implementation = {
            "codeRevision": "old-clean-revision",
            "workingTreeDirtyAtStart": False,
            "fileDigestsSHA256": previous_plan["implementation"][
                "fileDigestsSHA256"
            ],
        }
        current_frontier_implementation = {
            "codeRevision": "new-clean-revision",
            "workingTreeDirtyAtStart": False,
            "fileDigestsSHA256": plan["implementation"]["fileDigestsSHA256"],
        }
        interrupted_frontier = {
            "status": "interrupted",
            "runnerVersion": "phase1-frontier-arm-v1",
            "source": {"providerPlanSHA256": previous_plan_digest},
            "implementation": previous_implementation,
            "counts": {"completedCalls": 1},
            "failure": {
                "type": "SubscriptionResponseError",
                "message": "LiteLLM response contains no output text",
            },
        }
        adopted = frontier.adopt_interrupted_prefix(
            deepcopy(interrupted_frontier),
            prefix_path,
            current_frontier_implementation,
            plan,
            plan_path,
            plan_digest,
            previous_plan_path,
            previous_plan_digest,
        )
        if not (
            adopted["runnerVersion"] == "phase1-frontier-arm-v2"
            and adopted["implementation"] == current_frontier_implementation
            and adopted["source"]["providerPlanSHA256"] == plan_digest
            and adopted["implementationHistory"][0]["completedPrefixCount"] == 1
            and adopted["implementationHistory"][0]["scoresPrefixSHA256"]
            == sha256(prefix_path)
            and "failure" not in adopted
        ):
            raise AssertionError("frontier interrupted-prefix adoption is incomplete")
        altered_plan = deepcopy(plan)
        altered_plan["protocol"]["examples"] = 199
        try:
            frontier.adopt_interrupted_prefix(
                deepcopy(interrupted_frontier),
                prefix_path,
                current_frontier_implementation,
                altered_plan,
                plan_path,
                plan_digest,
                previous_plan_path,
                previous_plan_digest,
            )
        except TrainingContractError:
            pass
        else:
            raise AssertionError("frontier prefix adopted across a changed experiment")

        example_by_id = {value["exampleID"]: value for value in examples}
        packed_by_id = {value["exampleID"]: value for value in packed.rows}
        plans_by_id = {
            value["exampleID"]: value
            for value in (
                json.loads(line)
                for line in (packed_path / "context-plans.jsonl").read_text().splitlines()
                if line.strip()
            )
        }
        frontier_directory = temporary / "frontier"
        tinker_directory = temporary / "tinker"
        frontier_directory.mkdir()
        tinker_directory.mkdir()
        frontier_scores = []
        tinker_scores = []
        updates = []
        for block_index, block in enumerate(corpus["blocking"]["blocks"]):
            prior_checkpoint = updates[-1]["samplerCheckpointPath"] if updates else None
            for example_id in block["exampleIDs"]:
                example = example_by_id[example_id]
                target = target_text(example["target"])
                common = {
                    "blockID": block["blockID"],
                    "exampleID": example_id,
                    "target": target,
                    "pasteActionCount": sum(
                        segment.get("type") == "paste"
                        for segment in example["target"].get("segments", [])
                    ),
                    "prediction": target,
                    "exactMatch": True,
                    "normalizedExactMatch": True,
                    "characterSimilarity": 1.0,
                    "latencySeconds": 0.0,
                    "completedAt": f"2026-01-01T0{block_index + 1}:00:00Z",
                }
                frontier_scores.append({
                    **common,
                    "arm": ARM_FROZEN_FRONTIER,
                    "semanticModelInputSHA256": plans_by_id[example_id][
                        "semanticModelInputSHA256"
                    ],
                })
            for arm in (ARM_FROZEN_QWEN, ARM_PERSONALIZED_QWEN):
                for example_id in block["exampleIDs"]:
                    example = example_by_id[example_id]
                    row = packed_by_id[example_id]
                    target = target_text(example["target"])
                    per_token = 2.0 if arm == ARM_FROZEN_QWEN or block_index == 0 else 1.5
                    tinker_scores.append({
                        "blockID": block["blockID"],
                        "arm": arm,
                        "exampleID": example_id,
                        "target": target,
                        "pasteActionCount": row["pasteActionCount"],
                        "prediction": target,
                        "exactMatch": True,
                        "normalizedExactMatch": True,
                        "characterSimilarity": 1.0,
                        "latencySeconds": 0.0,
                        "completedAt": f"2026-01-01T0{block_index + 1}:00:00Z",
                        "checkpointID": prior_checkpoint if arm == ARM_PERSONALIZED_QWEN else None,
                        "weightedTokenCount": row["targetTokenCount"],
                        "weightedNLLSum": per_token * row["targetTokenCount"],
                        "meanNLL": per_token,
                    })
            updates.append({
                "afterBlockID": block["blockID"],
                "samplerCheckpointPath": f"tinker://fixture/block-{block_index + 1}",
                "completedAt": f"2026-01-01T0{block_index + 1}:59:00Z",
            })
        write_jsonl(frontier_directory / "scores.jsonl", frontier_scores)
        write_jsonl(tinker_directory / "scores.jsonl", tinker_scores)
        write_jsonl(tinker_directory / "updates.jsonl", updates)
        frontier_manifest = {
            "status": "complete",
            "source": {
                "corpusSHA256": sha256(corpus_path / "corpus.json"),
                "packingSHA256": sha256(packed_path / "packing.json"),
            },
            "artifactDigestsSHA256": {
                "scores.jsonl": sha256(frontier_directory / "scores.jsonl")
            },
        }
        tinker_manifest = {
            "status": "complete",
            "source": {
                "corpusSHA256": sha256(corpus_path / "corpus.json"),
                "packingSHA256": sha256(packed_path / "packing.json"),
            },
            "artifactDigestsSHA256": {
                "scores.jsonl": sha256(tinker_directory / "scores.jsonl"),
                "updates.jsonl": sha256(tinker_directory / "updates.jsonl"),
            },
        }
        (frontier_directory / "frontier.json").write_bytes(canonical_bytes(frontier_manifest))
        (tinker_directory / "tinker.json").write_bytes(canonical_bytes(tinker_manifest))
        audit_output = temporary / "audit"
        subprocess.run(
            [
                sys.executable,
                str(project / "scripts/audit-phase1-real-experiment.py"),
                "--corpus", str(corpus_path),
                "--packed", str(packed_path),
                "--frontier", str(frontier_directory),
                "--tinker", str(tinker_directory),
                "--output", str(audit_output),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        audit = json.loads((audit_output / "experiment.json").read_text())
        if not (
            audit["status"] == "passed_developmental_not_thesis_conclusion"
            and audit["auditVersion"] == "phase1-real-experiment-audit-v2"
            and audit["protocol"]["examples"] == 200
            and audit["summaries"][ARM_FROZEN_FRONTIER]["generatedCompletion"][
                "exactMatches"
            ] == 200
            and audit["summaries"][ARM_PERSONALIZED_QWEN]["generatedCompletion"][
                "correctPrefix"
            ]["microTargetCoverage"] == 1.0
            and audit["summaries"][ARM_FROZEN_QWEN]["generatedCompletion"][
                "pasteActions"
            ]["recall"] == 1.0
            and len(load_json_lines(audit_output / "comparisons.jsonl")) == 200
        ):
            raise AssertionError("synthetic final experiment audit failed")
        tampered_scores = load_json_lines(tinker_directory / "scores.jsonl")
        first_personalized = next(
            value for value in tampered_scores
            if value["arm"] == ARM_PERSONALIZED_QWEN
        )
        first_personalized["checkpointID"] = "tinker://fixture/leaked-checkpoint"
        write_jsonl(tinker_directory / "scores.jsonl", tampered_scores)
        tinker_manifest["artifactDigestsSHA256"]["scores.jsonl"] = sha256(
            tinker_directory / "scores.jsonl"
        )
        (tinker_directory / "tinker.json").write_bytes(canonical_bytes(tinker_manifest))
        rejected = subprocess.run(
            [
                sys.executable,
                str(project / "scripts/audit-phase1-real-experiment.py"),
                "--corpus", str(corpus_path),
                "--packed", str(packed_path),
                "--frontier", str(frontier_directory),
                "--tinker", str(tinker_directory),
                "--output", str(temporary / "tampered-audit"),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if rejected.returncode == 0:
            raise AssertionError("final audit accepted a leaked personalized checkpoint")
    print("Phase 1 real provider-runner checks passed")
    return 0


def load_json_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


if __name__ == "__main__":
    raise SystemExit(main())
