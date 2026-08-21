#!/usr/bin/env python3
"""No-network checks for the bounded Inkling native-loss stability probe."""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import types
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path

from phase1_inkling import GENERATION_CONTRACT, TRAINING_CONTRACT, load_jsonl


def load_runner(project: Path):
    path = project / "scripts/run-phase1-inkling-stability.py"
    specification = importlib.util.spec_from_file_location(
        "phase1_inkling_stability_check", path
    )
    if specification is None or specification.loader is None:
        raise AssertionError("cannot load stability runner")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def main() -> int:
    project = Path(__file__).resolve().parent.parent
    corpus = project / "coupled-data/phase1-raw-episode-corpus-v6-v10-review-20260820"
    semantic = project / "coupled-data/phase1-raw-episode-pack-v6-v10-canonical-20260820"
    inkling = project / "coupled-data/phase1-inkling-pack-v2-native-v5-20260821"
    if not all(value.exists() for value in (corpus, semantic, inkling)):
        print("Phase 1 Inkling stability checks skipped: artifacts unavailable")
        return 0
    runner = load_runner(project)
    assert runner.probe_accepted(
        stop_reason="stop", parsed={"status": "parsed", "prediction": "answer"}
    )
    assert not runner.probe_accepted(
        stop_reason="length", parsed={"status": "parsed", "prediction": "answer"}
    )
    assert not runner.probe_accepted(
        stop_reason="stop", parsed={"status": "parsed", "prediction": ""}
    )
    assert not runner.probe_accepted(
        stop_reason="stop", parsed={"status": "parse_failed", "prediction": "answer"}
    )
    assert TRAINING_CONTRACT["optimizerBatchExamples"] == 10
    assert TRAINING_CONTRACT["optimizer"]["learningRate"] == 0.0002
    assert GENERATION_CONTRACT["maximumTokensByCondition"] == {"reasoning_off": 512}

    with tempfile.TemporaryDirectory(prefix="inkling-stability-check-") as raw:
        plan_path = Path(raw) / "plan.json"
        prepared = subprocess.run(
            [
                sys.executable,
                str(project / "scripts/prepare-phase1-inkling-stability.py"),
                "--corpus",
                str(corpus),
                "--semantic-pack",
                str(semantic),
                "--inkling-pack",
                str(inkling),
                "--output",
                str(plan_path),
                "--tinker-project-id",
                "10b258ab-25fe-45e0-a54b-fef023154281",
                "--hard-ceiling-usd",
                "20.00",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if prepared.returncode != 0:
            raise AssertionError(prepared.stderr)
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        assert plan["causalChange"]["changed"] == (
            "custom_partial_response_loss_to_native_full_response_loss"
        )
        assert plan["protocol"]["trainingExampleCountsAfterStage"] == [
            0,
            50,
            100,
            150,
            200,
        ]
        assert plan["protocol"]["evaluationExampleCountsAfterUpdate"] == [
            50,
            50,
            50,
            24,
        ]
        assert plan["protocol"]["targetLikelihoodCalls"] == 0
        assert plan["protocol"]["frozenDuplicateArm"] is False
        assert plan["protocol"]["reasoningOnArm"] is False
        assert plan["protocol"]["probeSource"] == "first_training_block_only_never_scored"
        assert plan["protocol"]["minimumAutomaticValidityRate"] == "0.98"
        assert plan["operations"]["trainingCalls"] == 20
        assert plan["operations"]["sampleCalls"] == 178
        assert Decimal(plan["pricing"]["projectedUSD"]["totalIncludingReserve"]) < Decimal(
            "20.00"
        )
        examples = {
            value["exampleID"]: value
            for value in load_jsonl(corpus / "examples.jsonl")
        }
        apps = [
            json.loads(examples[value]["query"])["destination"]["appName"]
            for value in plan["protocol"]["probeExampleIDs"]
        ]
        scored_ids = {
            value
            for block in load_jsonl(corpus / "episode-blocks.jsonl")[1:]
            for value in block["exampleIDs"]
        }
        assert not (set(plan["protocol"]["probeExampleIDs"]) & scored_ids)
        assert {value: apps.count(value) for value in set(apps)} == {
            "ChatGPT": 1,
            "Code": 1,
            "Google Chrome": 1,
            "Obsidian": 1,
        }
        rows = load_jsonl(inkling / "reasoning_off-packed-examples.jsonl")
        assert all(
            row["targetFormatTokenCount"] == 4
            and all(
                value != -100
                for value in row["labels"][row["modelInputTokenCount"] :]
            )
            for row in rows
        )
        row_by_prompt = {
            tuple(row["inputIDs"][: row["modelInputTokenCount"]]): row["inputIDs"][
                row["modelInputTokenCount"] :
            ]
            for row in rows
        }

        lock_path = runner.acquire_output_lock(Path(raw) / "lock-probe")
        lock_probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import fcntl,sys; "
                    "h=open(sys.argv[1],'a+'); "
                    "\ntry: fcntl.flock(h.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)"
                    "\nexcept BlockingIOError: raise SystemExit(17)"
                    "\nraise SystemExit(0)"
                ),
                str(lock_path),
            ],
            check=False,
        )
        assert lock_probe.returncode == 17

        class Immediate:
            def __init__(self, value):
                self.value = value

            def result(self):
                return self.value

        faults = {
            "generationExceptionRemaining": 1,
            "trainingExceptionRemaining": 1,
            "trainingCalls": 0,
            "invalidOnceMarker": "stability-02",
            "invalidOnceRemaining": 1,
            "invalidAllMarker": None,
        }

        class Logprobs(list):
            def tolist(self):
                return list(self)

        class SamplingClient:
            def __init__(self, model_path: str = ""):
                self.model_path = model_path

            def sample(self, *, prompt, **_):
                if (
                    faults["generationExceptionRemaining"]
                    and "stability-01" in self.model_path
                ):
                    faults["generationExceptionRemaining"] -= 1
                    raise RuntimeError("synthetic generation interruption")
                invalid = bool(
                    faults["invalidAllMarker"]
                    and faults["invalidAllMarker"] in self.model_path
                )
                if (
                    faults["invalidOnceRemaining"]
                    and faults["invalidOnceMarker"] in self.model_path
                ):
                    faults["invalidOnceRemaining"] -= 1
                    invalid = True
                sequence = types.SimpleNamespace(
                    tokens=[] if invalid else row_by_prompt[tuple(prompt)],
                    stop_reason="stop",
                )
                return Immediate(types.SimpleNamespace(sequences=[sequence]))

        class TrainingClient:
            def forward_backward(self, datums, *_):
                faults["trainingCalls"] += 1
                if (
                    faults["trainingExceptionRemaining"]
                    and faults["trainingCalls"] >= 6
                ):
                    faults["trainingExceptionRemaining"] -= 1
                    raise RuntimeError("synthetic training interruption")
                outputs = [
                    {"logprobs": Logprobs([-1.0] * len(value.weights))}
                    for value in datums
                ]
                return Immediate(types.SimpleNamespace(loss_fn_outputs=outputs))

            def optim_step(self, *_):
                return Immediate(None)

            def save_weights_for_sampler(self, name, **_):
                return Immediate(types.SimpleNamespace(path=f"tinker://fixture/{name}"))

            def save_state(self, name, **_):
                return Immediate(types.SimpleNamespace(path=f"tinker://fixture/{name}"))

        class ServiceClient:
            def __init__(self, **_):
                self.holder = types.SimpleNamespace(get_session_id=lambda: "fixture")

            def get_server_capabilities(self):
                model = types.SimpleNamespace(
                    model_name="thinkingmachines/Inkling-Small",
                    max_context_length=65536,
                )
                return types.SimpleNamespace(supported_models=[model])

            def create_sampling_client(self, **values):
                model_path = str(values.get("model_path", ""))
                return SamplingClient(model_path=model_path)

            def create_lora_training_client(self, **_):
                return TrainingClient()

            def create_training_client_from_state_with_optimizer(self, *_, **__):
                return TrainingClient()

        fake_tinker = types.SimpleNamespace(
            ServiceClient=ServiceClient,
            AdamParams=lambda **_: object(),
            ModelInput=types.SimpleNamespace(from_ints=lambda *, tokens: tokens),
            SamplingParams=lambda **_: object(),
        )
        output = Path(raw) / "run"
        runner.git_worktree_dirty = lambda _: False
        runner.validate_plan = lambda *_: plan
        runner.load_api_key = lambda _: "fixture"
        recorded_interruptions: list[str] = []
        original_record_interruption = runner.record_interruption

        def record_interruption(error):
            recorded_interruptions.append(f"{type(error).__name__}: {error}")
            return original_record_interruption(error)

        runner.record_interruption = record_interruption
        runner.build_and_validate_sdk_datums = lambda contracts: (
            contracts,
            [],
            "fixture",
        )
        prior_tinker = sys.modules.get("tinker")
        sys.modules["tinker"] = fake_tinker
        prior_argv = sys.argv
        sys.argv = [
            str(project / "scripts/run-phase1-inkling-stability.py"),
            "--corpus",
            str(corpus),
            "--semantic-pack",
            str(semantic),
            "--inkling-pack",
            str(inkling),
            "--plan",
            str(plan_path),
            "--plan-sha256",
            "fixture",
            "--output",
            str(output),
            "--env-file",
            str(Path(raw) / "env"),
            "--dedicated-private-project-id",
            "10b258ab-25fe-45e0-a54b-fef023154281",
            "--maximum-usd",
            "20.00",
            "--confirm-personal-data-transfer",
            "--confirm-dedicated-private-project",
            "--confirm-current-prices",
            "--execute",
        ]
        try:
            with redirect_stdout(io.StringIO()):
                result = runner.main()
            assert result == 0, recorded_interruptions
        except Exception:
            print(json.dumps(recorded_interruptions, indent=2), file=sys.stderr)
            raise
        finally:
            sys.argv = prior_argv
            if prior_tinker is None:
                del sys.modules["tinker"]
            else:
                sys.modules["tinker"] = prior_tinker
        manifest = json.loads((output / "stability.json").read_text())
        assert manifest["status"] == "complete_go"
        assert manifest["counts"] == {
            "completedStages": 5,
            "completedUpdates": 4,
            "baseProbes": 4,
            "evaluationScores": 174,
            "samples": 178,
            "trainingBatchRecords": 20,
        }
        observed_stage_acceptance = [value["accepted"] for value in manifest["stages"]]
        assert observed_stage_acceptance == [
            4,
            50,
            49,
            50,
            24,
        ], observed_stage_acceptance
        assert manifest["stages"][2]["automaticContinuationEligible"] is True
        assert len(manifest["abandonedAttempts"]) == 2
        assert {value["kind"] for value in manifest["abandonedAttempts"]} == {
            "generation_retry",
            "training_block_restart",
        }
        invalid = [value for value in load_jsonl(output / "scores.jsonl") if not value["accepted"]]
        assert len(invalid) == 1
        assert invalid[0]["predictionMetrics"]["normalizedLevenshteinSimilarity"] == 0.0
        assert len(load_jsonl(output / "training-batches.jsonl")) == 20
        assert all(
            value["meanPreUpdateNLL"] == 1.0
            for value in load_jsonl(output / "updates.jsonl")
        )

        # Material deterioration pauses at the block boundary without losing
        # the completed predictions. Explicit review can then resume from the
        # committed optimizer checkpoint.
        faults.update(
            {
                "generationExceptionRemaining": 0,
                "trainingExceptionRemaining": 0,
                "trainingCalls": 0,
                "invalidOnceMarker": "",
                "invalidOnceRemaining": 0,
                "invalidAllMarker": "stability-02-attempt-01-sampler",
            }
        )
        failed_output = Path(raw) / "run-failed-gate"
        sys.modules["tinker"] = fake_tinker
        sys.argv = [
            *sys.argv[:1],
            "--corpus",
            str(corpus),
            "--semantic-pack",
            str(semantic),
            "--inkling-pack",
            str(inkling),
            "--plan",
            str(plan_path),
            "--plan-sha256",
            "fixture",
            "--output",
            str(failed_output),
            "--env-file",
            str(Path(raw) / "env"),
            "--dedicated-private-project-id",
            "10b258ab-25fe-45e0-a54b-fef023154281",
            "--maximum-usd",
            "20.00",
            "--confirm-personal-data-transfer",
            "--confirm-dedicated-private-project",
            "--confirm-current-prices",
            "--execute",
        ]
        try:
            with redirect_stdout(io.StringIO()):
                assert runner.run() == 3
        finally:
            sys.argv = prior_argv
            if prior_tinker is None:
                del sys.modules["tinker"]
            else:
                sys.modules["tinker"] = prior_tinker
        failed = json.loads((failed_output / "stability.json").read_text())
        assert failed["status"] == "paused_for_validity_review"
        assert failed["counts"]["completedUpdates"] == 2
        assert failed["counts"]["completedStages"] == 3
        assert failed["counts"]["evaluationScores"] == 100
        assert failed["stages"][-1]["status"] == "material_deterioration"

        faults["invalidAllMarker"] = None
        sys.modules["tinker"] = fake_tinker
        sys.argv = [
            *sys.argv[:1],
            "--corpus",
            str(corpus),
            "--semantic-pack",
            str(semantic),
            "--inkling-pack",
            str(inkling),
            "--plan",
            str(plan_path),
            "--plan-sha256",
            "fixture",
            "--output",
            str(failed_output),
            "--env-file",
            str(Path(raw) / "env"),
            "--dedicated-private-project-id",
            "10b258ab-25fe-45e0-a54b-fef023154281",
            "--maximum-usd",
            "20.00",
            "--confirm-personal-data-transfer",
            "--confirm-dedicated-private-project",
            "--confirm-current-prices",
            "--authorize-continue-after-validity-review",
            "block-0003",
            "--execute",
        ]
        try:
            with redirect_stdout(io.StringIO()):
                assert runner.run() == 0
        finally:
            sys.argv = prior_argv
            if prior_tinker is None:
                del sys.modules["tinker"]
            else:
                sys.modules["tinker"] = prior_tinker
        resumed = json.loads((failed_output / "stability.json").read_text())
        assert resumed["status"] == "complete_with_generation_deterioration"
        assert resumed["counts"]["completedUpdates"] == 4
        assert resumed["counts"]["evaluationScores"] == 174
        assert resumed["validityReviewAuthorizations"][0]["blockID"] == "block-0003"
    print("phase1 Inkling stability checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
