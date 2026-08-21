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
        assert plan["operations"]["trainingCalls"] == 20
        assert plan["operations"]["sampleCalls"] == 186
        assert Decimal(plan["pricing"]["projectedUSD"]["totalIncludingReserve"]) < Decimal(
            "17.00"
        )
        examples = {
            value["exampleID"]: value
            for value in load_jsonl(corpus / "examples.jsonl")
        }
        apps = [
            json.loads(examples[value]["query"])["destination"]["appName"]
            for value in plan["protocol"]["probeExampleIDs"]
        ]
        assert {value: apps.count(value) for value in set(apps)} == {
            "ChatGPT": 3,
            "Code": 3,
            "Google Chrome": 3,
            "Obsidian": 3,
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

        class Immediate:
            def __init__(self, value):
                self.value = value

            def result(self):
                return self.value

        fail_model_marker: str | None = None

        class SamplingClient:
            def __init__(self, fail: bool = False):
                self.fail = fail

            def sample(self, *, prompt, **_):
                sequence = types.SimpleNamespace(
                    tokens=[] if self.fail else row_by_prompt[tuple(prompt)],
                    stop_reason="stop",
                )
                return Immediate(types.SimpleNamespace(sequences=[sequence]))

        class TrainingClient:
            def forward_backward(self, *_):
                return Immediate(types.SimpleNamespace(loss_fn_outputs=[]))

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
                return SamplingClient(
                    fail=bool(fail_model_marker and fail_model_marker in model_path)
                )

            def create_lora_training_client(self, **_):
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
        runner.build_and_validate_sdk_datums = lambda contracts: (
            [object() for _ in contracts],
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
                assert runner.run() == 0
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
            "baseProbes": 12,
            "evaluationScores": 174,
            "samples": 186,
        }
        assert [value["accepted"] for value in manifest["stages"]] == [
            12,
            50,
            50,
            50,
            24,
        ]

        # A failed free-generation gate after update two must stop before the
        # third paid training block. This is the central spend-safety property
        # of the bounded probe.
        fail_model_marker = "stability-02-sampler"
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
                assert runner.run() == 2
        finally:
            sys.argv = prior_argv
            if prior_tinker is None:
                del sys.modules["tinker"]
            else:
                sys.modules["tinker"] = prior_tinker
        failed = json.loads((failed_output / "stability.json").read_text())
        assert failed["status"] == "complete_no_go"
        assert failed["counts"]["completedUpdates"] == 2
        assert failed["counts"]["completedStages"] == 3
        assert failed["stages"][-1]["status"] == "failed"
    print("phase1 Inkling stability checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
