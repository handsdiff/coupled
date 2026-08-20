#!/usr/bin/env python3
"""No-network invariants for the real Phase 1 provider runners."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from phase1_experiment import (
    ARM_FROZEN_FRONTIER,
    ARM_FROZEN_QWEN,
    ARM_PERSONALIZED_QWEN,
    canonical_bytes,
    prospective_example_ids,
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
    evaluation_ids = prospective_example_ids(corpus["blocking"]["blocks"])

    with tempfile.TemporaryDirectory(prefix="phase1-real-runner-check-") as raw:
        temporary = Path(raw)
        plan_path = temporary / "provider-plan.json"
        prepare_run = subprocess.run(
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
            check=False,
            capture_output=True,
            text=True,
        )
        if prepare_run.returncode != 0:
            raise AssertionError(f"provider plan preparation failed: {prepare_run.stderr}")
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
            plan_path, plan_digest, corpus_path, packed_path, len(evaluation_ids)
        )
        tinker.validate_plan(
            plan_path,
            plan_digest,
            corpus_path,
            packed_path,
            "10b258ab-25fe-45e0-a54b-fef023154281",
            Decimal("40.00"),
        )
        if plan["tinker"]["hardExecutionCeilingUSD"] != "40.00":
            raise AssertionError("hard Tinker ceiling is not frozen")
        sequence = tinker.expected_score_sequence(corpus["blocking"]["blocks"])
        if len(sequence) != 300 or len(set(sequence)) != 300:
            raise AssertionError("Tinker score plan is not 2 x 150 unique operations")
        if not (
            plan["protocol"]["warmupExamples"] == 50
            and plan["protocol"]["providerScoredExamples"] == 150
            and plan["openai"]["operations"]["responseCalls"] == 150
        ):
            raise AssertionError("provider plan did not exclude warm-up scoring")
        semantic_review = plan.get("evaluation", {}).get("semanticReview", {})
        if not (
            semantic_review.get("rubricVersion")
            == "phase1-blind-semantic-review-v1"
            and semantic_review.get("blindUntilJudgmentsFrozen") is True
            and semantic_review.get("relativePath")
            == "experiment/phase1-blind-semantic-review-v1.json"
            and semantic_review.get("sha256")
            == sha256(project / semantic_review["relativePath"])
        ):
            raise AssertionError("provider plan did not freeze the semantic rubric")
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

        class Immediate:
            def __init__(self, value):
                self.value = value

            def result(self):
                return self.value

        class Sequence:
            tokens = [9, 2]
            stop_reason = "stop"

        class Response:
            sequences = [Sequence()]

        class SamplingClient:
            def compute_logprobs(self, _):
                return Immediate([None, -0.5])

            def sample(self, **_):
                return Immediate(Response())

        class ModelInput:
            @staticmethod
            def from_ints(*, tokens):
                return tokens

        class SamplingParams:
            def __init__(self, **_):
                pass

        class Tinker:
            pass

        Tinker.ModelInput = ModelInput
        Tinker.SamplingParams = SamplingParams

        class Tokenizer:
            def decode(self, _, **__):
                return "x"

        timing_record = tinker.score_example(
            SamplingClient(),
            Tokenizer(),
            Tinker,
            ARM_FROZEN_QWEN,
            "fixture-block",
            {
                "exampleID": "fixture-example",
                "targetEventID": "fixture-event",
                "target": {
                    "segments": [{"type": "authored_text", "content": "x"}]
                },
            },
            {
                "inputIDs": [1, 2],
                "labels": [tinker.IGNORE_LABEL, 9],
                "targetTokenCount": 1,
                "modelInputTokenCount": 1,
                "eosTokenID": 2,
                "pasteActionCount": 0,
            },
            None,
            tinker.Usage(),
        )
        if not (
            timing_record["latencyInstrumentationVersion"]
            == "tinker-score-latency-v2-split-requests"
            and timing_record["targetLikelihoodLatencySeconds"] >= 0
            and timing_record["generationLatencySeconds"] >= 0
            and timing_record["latencySeconds"]
            >= timing_record["targetLikelihoodLatencySeconds"]
            + timing_record["generationLatencySeconds"]
        ):
            raise AssertionError("Tinker score latency components were not retained")

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
            adopted["runnerVersion"] == "phase1-frontier-arm-v3"
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
            if block_index > 0:
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
                        per_token = 2.0 if arm == ARM_FROZEN_QWEN else 1.5
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
                        "fullSequenceTokenCount": len(row["inputIDs"]),
                        "modelInputTokenCount": row["modelInputTokenCount"],
                        "predictionTokenIDs": [],
                        })
            updates.append({
                "afterBlockID": block["blockID"],
                "samplerCheckpointPath": f"tinker://fixture/block-{block_index + 1}",
                "completedAt": f"2026-01-01T0{block_index + 1}:59:00Z",
                "latencySeconds": 0.0,
                "submittedPositions": 0,
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
        audit_run = subprocess.run(
            [
                sys.executable,
                str(project / "scripts/audit-phase1-real-experiment.py"),
                "--corpus", str(corpus_path),
                "--packed", str(packed_path),
                "--frontier", str(frontier_directory),
                "--tinker", str(tinker_directory),
                "--output", str(audit_output),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if audit_run.returncode != 0:
            raise AssertionError(f"synthetic final audit failed: {audit_run.stderr}")
        audit = json.loads((audit_output / "experiment.json").read_text())
        if not (
            audit["status"] == "passed_developmental_not_thesis_conclusion"
            and audit["auditVersion"] == "phase1-real-experiment-audit-v7"
            and audit["protocol"]["providerScoredExamples"] == 150
            and audit["protocol"]["warmupExamples"] == 50
            and audit["protocol"]["warmupProviderScored"] is False
            and audit["protocol"]["prospectiveEvaluationExamples"] == 150
            and audit["summaries"][ARM_FROZEN_FRONTIER]["generatedCompletion"][
                "exactMatches"
            ] == 150
            and audit["summaries"][ARM_PERSONALIZED_QWEN]["generatedCompletion"][
                "correctPrefix"
            ]["microTargetCoverage"] == 1.0
            and audit["summaries"][ARM_FROZEN_QWEN]["generatedCompletion"][
                "pasteActions"
            ]["recall"] == 1.0
            and audit["costLatencyReportVersion"] == "phase1-cost-latency-v3"
            and (audit_output / "cost-latency.json").is_file()
            and (audit_output / "cost-latency.csv").is_file()
            and len(load_json_lines(audit_output / "comparisons.jsonl")) == 150
            and len(
                load_json_lines(audit_output / "evaluation-comparisons.jsonl")
            ) == 150
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

    raw_episode_corpus = (
        project
        / "coupled-data/phase1-raw-episode-corpus-v6-v10-review-20260820"
    )
    raw_episode_pack = (
        project
        / "coupled-data/phase1-raw-episode-pack-v6-v10-canonical-20260820"
    )
    if raw_episode_corpus.is_dir() and raw_episode_pack.is_dir():
        episode_manifest, episode_examples, _, _ = validate_inputs(
            raw_episode_corpus, raw_episode_pack
        )
        episode_blocks = episode_manifest["blocking"]["blocks"]
        if not (
            len(episode_examples) == 224
            and [block["exampleCount"] for block in episode_blocks]
            == [50, 50, 50, 50, 24]
            and len(prospective_example_ids(episode_blocks)) == 174
            and len(tinker.expected_score_sequence(episode_blocks)) == 348
            and episode_manifest["experimentAdapter"][
                "semanticInterpretationChanged"
            ] is False
        ):
            raise AssertionError("raw episode experiment adapter contract changed")
    print("Phase 1 real provider-runner checks passed")
    return 0


def load_json_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


if __name__ == "__main__":
    raise SystemExit(main())
