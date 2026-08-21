#!/usr/bin/env python3
"""No-network protocol checks for the four-arm Inkling runner."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from phase1_experiment import canonical_bytes, target_text
from phase1_inkling import (
    ARM_NAMES,
    GENERATION_CONTRACT,
    REASONING_CONDITIONS,
    load_experiment_blocks,
    load_jsonl,
    sha256,
)
from phase1_prediction_metrics import score_prediction


def load_runner(project: Path):
    path = project / "scripts/run-phase1-inkling-prequential.py"
    specification = importlib.util.spec_from_file_location(
        "phase1_inkling_runner_check", path
    )
    if specification is None or specification.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


class Immediate:
    def __init__(self, value):
        self.value = value

    def result(self):
        return self.value


class Sequence:
    def __init__(self, tokens, stop_reason="stop"):
        self.tokens = tokens
        self.stop_reason = stop_reason


class Response:
    def __init__(self, tokens):
        self.sequences = [Sequence(tokens)]


class SamplingClient:
    def __init__(self, completion_tokens, stop_reason="stop"):
        self.completion_tokens = completion_tokens
        self.stop_reason = stop_reason

    def compute_logprobs(self, model_input):
        return Immediate([-0.25] * len(model_input))

    def sample(self, **_):
        response = Response(self.completion_tokens)
        response.sequences[0].stop_reason = self.stop_reason
        return Immediate(response)


class ModelInput:
    @staticmethod
    def from_ints(*, tokens):
        return tokens


class SamplingParams:
    observed: list[dict] = []

    def __init__(self, **values):
        self.observed.append(values)


class Tinker:
    pass


Tinker.ModelInput = ModelInput
Tinker.SamplingParams = SamplingParams


def main() -> int:
    project = Path(__file__).resolve().parent.parent
    corpus_path = (
        project / "coupled-data/phase1-raw-episode-corpus-v6-v10-review-20260820"
    )
    pack_path = project / "coupled-data/phase1-inkling-pack-v1-20260820"
    if not corpus_path.is_dir() or not pack_path.is_dir():
        print("Phase 1 Inkling runner checks skipped: canonical artifacts unavailable")
        return 0

    runner = load_runner(project)
    blocks = load_experiment_blocks(corpus_path)
    examples = {
        value["exampleID"]: value for value in load_jsonl(corpus_path / "examples.jsonl")
    }
    score_sequence = runner.expected_score_sequence(blocks)
    update_sequence = runner.expected_update_sequence(blocks)
    evaluation_count = sum(len(value["exampleIDs"]) for value in blocks[1:])
    assert len(score_sequence) == 4 * evaluation_count == 696
    assert len(set(score_sequence)) == len(score_sequence)
    assert len(update_sequence) == 2 * (len(blocks) - 1) == 8
    assert all(value[0] != blocks[-1]["blockID"] for value in update_sequence)
    assert score_sequence[0][1] == ARM_NAMES["reasoning_off"]["frozen"]
    assert score_sequence[-1][1] == ARM_NAMES["reasoning_on"]["personalized"]

    first_id = blocks[1]["exampleIDs"][0]
    raw_contracts = [
        runner.datum_contract(value)
        for value in load_jsonl(pack_path / "reasoning_off-packed-examples.jsonl")[:10]
    ]
    normalized, target_tokens, token_weight = (
        runner.micro_normalized_batch_contracts(raw_contracts)
    )
    assert len(runner.optimizer_batches([value.example_id for value in raw_contracts])) == 1
    assert target_tokens == sum(value.weighted_positions for value in raw_contracts)
    assert 0 < token_weight < 1
    assert abs(sum(sum(value.weights) for value in normalized) - 1.0) < 1e-6
    assert all(
        {weight for weight in value.weights if weight} == {token_weight}
        for value in normalized
    )
    try:
        runner.optimizer_batches([value.example_id for value in raw_contracts[:8]])
    except runner.InklingContractError:
        pass
    else:
        raise AssertionError("partial optimizer batch was accepted")

    for condition, effort in REASONING_CONDITIONS.items():
        rows = {
            value["exampleID"]: value
            for value in load_jsonl(pack_path / f"{condition}-packed-examples.jsonl")
        }
        row = rows[first_id]
        completion = row["inputIDs"][row["modelInputTokenCount"] :]
        usage = runner.Usage()
        score = runner.score_example(
            sampling_client=SamplingClient(completion),
            tinker=Tinker,
            condition=condition,
            arm=ARM_NAMES[condition]["frozen"],
            block_id=blocks[1]["blockID"],
            example=examples[first_id],
            row=row,
            semantic_input="fixture semantic input",
            checkpoint_id=None,
            usage=usage,
            prices={"prefill": "0.58", "sample": "1.44"},
        )
        assert score["prediction"] == target_text(examples[first_id]["target"])
        assert score["reasoning"] == ""
        assert score["effort"] == effort
        assert score["weightedTokenCount"] == row["targetTokenCount"]
        assert score["meanNLL"] == 0.25
        assert score["generationTemperature"] == GENERATION_CONTRACT["temperature"]
        assert score["generationSeed"] == GENERATION_CONTRACT["seed"]
        assert score["generationTokenCeiling"] == GENERATION_CONTRACT[
            "maximumTokensByCondition"
        ][condition]
        assert score["generationDisposition"] == "accepted"
        assert score["generationEligibleForEvaluation"] is True
        assert score["predictionMetrics"] == score["validOnlyPredictionMetrics"]
        assert score["targetLikelihoodLatencySeconds"] >= 0
        assert score["generationLatencySeconds"] >= 0
        assert score["latencySeconds"] >= score["targetLikelihoodLatencySeconds"]
        assert usage.nll_calls == usage.sample_calls == 1
        assert usage.training_calls == 0

    assert SamplingParams.observed
    assert [value["max_tokens"] for value in SamplingParams.observed] == [
        GENERATION_CONTRACT["maximumTokensByCondition"][condition]
        for condition in REASONING_CONDITIONS
    ]
    assert all(
        value["temperature"] == GENERATION_CONTRACT["temperature"]
        and value["seed"] == GENERATION_CONTRACT["seed"]
        for value in SamplingParams.observed
    )

    incomplete = runner.score_example(
        sampling_client=SamplingClient([], stop_reason="length"),
        tinker=Tinker,
        condition="reasoning_on",
        arm=ARM_NAMES["reasoning_on"]["frozen"],
        block_id=blocks[1]["blockID"],
        example=examples[first_id],
        row=rows[first_id],
        semantic_input="fixture semantic input",
        checkpoint_id=None,
        usage=runner.Usage(),
        prices={"prefill": "0.58", "sample": "1.44"},
    )
    assert incomplete["generationDisposition"] == "truncated_without_valid_final"
    assert incomplete["generationEligibleForEvaluation"] is False
    assert incomplete["predictionMetrics"] is not None
    assert incomplete["predictionMetrics"]["predictionEmpty"] is True
    assert incomplete["predictionMetrics"]["exactMatch"] is False
    assert incomplete["validOnlyPredictionMetrics"] is None

    with tempfile.TemporaryDirectory(prefix="phase1-inkling-audit-check-") as raw:
        temporary = Path(raw)
        plan_path = temporary / "plan.json"
        prepared = subprocess.run(
            [
                sys.executable,
                str(project / "scripts/prepare-phase1-inkling-experiment.py"),
                "--corpus", str(corpus_path),
                "--inkling-pack", str(pack_path),
                "--output", str(plan_path),
                "--tinker-project-id", "10b258ab-25fe-45e0-a54b-fef023154281",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if prepared.returncode != 0:
            raise AssertionError(f"Inkling plan fixture failed: {prepared.stderr}")
        updates = []
        checkpoint_by_block_condition = {}
        parent = {condition: None for condition in REASONING_CONDITIONS}
        for ordinal, block in enumerate(blocks[:-1], 1):
            for condition, effort in REASONING_CONDITIONS.items():
                sampler = f"tinker://fixture/{condition}/sampler/{ordinal}"
                state = f"tinker://fixture/{condition}/state/{ordinal}"
                update = {
                    "condition": condition,
                    "effort": effort,
                    "afterBlockID": block["blockID"],
                    "parentOptimizerStatePath": parent[condition],
                    "optimizerStatePath": state,
                    "samplerCheckpointPath": sampler,
                    "optimizerBatchSizes": [10, 10, 10, 10, 10],
                    "trainingCalls": 5,
                    "optimizerSteps": 5,
                    "lossReduction": runner.TRAINING_CONTRACT["lossReduction"],
                }
                updates.append(update)
                checkpoint_by_block_condition[(block["blockID"], condition)] = sampler
                parent[condition] = state
        rows_by_condition = {
            condition: {
                value["exampleID"]: value
                for value in load_jsonl(pack_path / f"{condition}-packed-examples.jsonl")
            }
            for condition in REASONING_CONDITIONS
        }
        prior_block = {
            blocks[index]["blockID"]: blocks[index - 1]["blockID"]
            for index in range(1, len(blocks))
        }
        scores = []
        for block_id, arm, example_id in score_sequence:
            condition = next(
                value for value in REASONING_CONDITIONS if arm in ARM_NAMES[value].values()
            )
            kind = "personalized" if arm.startswith("personalized_") else "frozen"
            row = rows_by_condition[condition][example_id]
            target = target_text(examples[example_id]["target"])
            invalid = not scores
            prediction = "" if invalid else target
            scores.append({
                "blockID": block_id,
                "condition": condition,
                "effort": REASONING_CONDITIONS[condition],
                "arm": arm,
                "exampleID": example_id,
                "target": target,
                "semanticModelInputSHA256": row["semanticModelInputSHA256"],
                "weightedTokenCount": row["targetTokenCount"],
                "weightedNLLSum": float(row["targetTokenCount"]),
                "meanNLL": 1.0,
                "prediction": prediction,
                "predictionMetrics": score_prediction(
                    target, prediction, target_paste_actions=row["pasteActionCount"]
                ),
                "validOnlyPredictionMetrics": (
                    None
                    if invalid
                    else score_prediction(
                        target,
                        target,
                        target_paste_actions=row["pasteActionCount"],
                    )
                ),
                "checkpointID": (
                    None
                    if kind == "frozen"
                    else checkpoint_by_block_condition[(prior_block[block_id], condition)]
                ),
                "reasoning": "" if condition == "reasoning_off" else "fixture",
                "responseParse": {
                    "status": "parse_failed" if invalid else "parsed",
                    "prediction": prediction,
                },
                "generationTokenCeiling": GENERATION_CONTRACT[
                    "maximumTokensByCondition"
                ][condition],
                "stopReason": "length" if invalid else "stop",
                "generationDisposition": (
                    "truncated_without_valid_final" if invalid else "accepted"
                ),
                "generationEligibleForEvaluation": not invalid,
                "latencySeconds": 1.0,
                "generationLatencySeconds": 0.75,
                "targetLikelihoodLatencySeconds": 0.25,
                "estimatedProviderCostUSDAtFrozenRates": "0.010000",
            })
        run_path = temporary / "run"
        run_path.mkdir()
        scores_path = run_path / "scores.jsonl"
        updates_path = run_path / "updates.jsonl"
        scores_path.write_bytes(b"".join(canonical_bytes(value) for value in scores))
        updates_path.write_bytes(b"".join(canonical_bytes(value) for value in updates))
        manifest = {
            "status": "complete",
            "runnerVersion": runner.INKLING_RUNNER_VERSION,
            "provider": {"model": runner.INKLING_MODEL},
            "source": {
                "corpusSHA256": sha256(corpus_path / "corpus.json"),
                "inklingPackingSHA256": sha256(pack_path / "packing.json"),
                "providerPlanSHA256": sha256(plan_path),
            },
            "artifactDigestsSHA256": {
                "scores.jsonl": sha256(scores_path),
                "updates.jsonl": sha256(updates_path),
            },
            "usage": {"fixture": True},
            "estimatedCost": {"fixture": True},
        }
        (run_path / "inkling.json").write_bytes(canonical_bytes(manifest))
        audit_path = temporary / "audit"
        audited = subprocess.run(
            [
                sys.executable,
                str(project / "scripts/audit-phase1-inkling-experiment.py"),
                "--corpus", str(corpus_path),
                "--inkling-pack", str(pack_path),
                "--provider-plan", str(plan_path),
                "--run", str(run_path),
                "--output", str(audit_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if audited.returncode != 0:
            raise AssertionError(f"Inkling synthetic audit failed: {audited.stderr}")
        report = json.loads((audit_path / "audit.json").read_text())
        assert report["status"] == "passed"
        assert report["protocol"]["scoreRows"] == 696
        assert report["protocol"]["updates"] == 8
        assert report["protocol"]["terminalBlockUpdated"] is False
        first_arm = ARM_NAMES["reasoning_off"]["frozen"]
        assert report["summaries"][first_arm]["generationExcludedExamples"] == 1
        assert report["summaries"][first_arm]["generatedCompletion"]["examples"] == 174
        assert report["summaries"][first_arm][
            "generatedCompletionConditionalOnValid"
        ]["examples"] == 173
        assert set(report["summaries"]) == {
            arm for condition in REASONING_CONDITIONS for arm in ARM_NAMES[condition].values()
        }
    print("phase1 Inkling runner checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
