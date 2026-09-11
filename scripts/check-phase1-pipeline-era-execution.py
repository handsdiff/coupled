"""Offline tests of real SDK payloads and the production execution state machine."""
from __future__ import annotations
import copy
import argparse
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from types import SimpleNamespace

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT / "coupled-data/sep02-10-training-prep-20260910"
ERA = PROJECT / "coupled-data/sep02-10-pipeline-era-comparison-20260911"
sys.path.insert(0, str(ROOT / "runtime"))
import numpy as np
import tinker
from transformers import AutoTokenizer
from phase1_qwen38_execution import (BPB_DEFINITION, ContractError, Executor, Journal, NativeRows, TinkerBridge,
                             datum_from_row, file_hash, fingerprint, prior_order,
                             verify_execution_binding, verify_paid_gate)


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def rejected(fn):
    try:
        fn()
    except ContractError:
        return
    raise AssertionError("Expected fail-closed rejection")


class Future:
    def __init__(self, value=None, error=None):
        self.value, self.error = value, error
    def result(self):
        if self.error:
            raise self.error
        return self.value


class FakeSampler:
    def __init__(self, service, snapshot):
        self.service, self.snapshot = service, copy.deepcopy(snapshot)
    def _row(self, ids, target_included):
        table = self.service.full_rows if target_included else self.service.prompt_rows
        check(fingerprint(ids) in table, "Wrong generation/NLL token boundary")
        row = table[fingerprint(ids)]
        ids_before = self.snapshot["trained"]
        check(row["exampleID"] not in ids_before, "Evaluation sees a previously trained target")
        if self.snapshot["kind"] == "personalized":
            check(self.snapshot["pipeline"] == row["arm"], "Crossed pipeline checkpoint")
            expected = self.service.prefixes[row["arm"]][row["blockOrdinal"]]
            check(set(ids_before) == set(expected), "Evaluation checkpoint has wrong training prefix")
        self.service.calls.append(("nll" if target_included else "generation", row["arm"], row["exampleID"], tuple(ids_before)))
        return row
    def sample(self, prompt, num_samples, sampling_params):
        row = self._row(prompt.to_ints(), False)
        check(num_samples == 1 and sampling_params.max_tokens == 512, "Changed sampling cap")
        check(sampling_params.temperature == .6 and sampling_params.seed == 17, "Changed sampling settings")
        check(list(sampling_params.stop) == [248046], "Changed native stop")
        tokens = self.service.output_tokens
        if self.service.fail_generation and row["blockOrdinal"] == 2:
            self.service.fail_generation = False
            return Future(error=RuntimeError("injected generation interruption"))
        seq = tinker.types.SampledSequence(tokens_np=np.asarray(tokens), logprobs_np=np.asarray([-.25]*len(tokens)), stop_reason="stop")
        return Future(tinker.types.SampleResponse(sequences=[seq]))
    def compute_logprobs(self, prompt):
        ids = prompt.to_ints()
        row = self._row(ids, True)
        # Context sentinel probabilities MUST NOT enter target loss.
        values = [None] + [-99.] * (row["promptTokenCount"] - 1) + [-.25] * row["lossBearingTokenCount"]
        return Future(values)


class FakeTrainer:
    def __init__(self, service, snapshot):
        self.service, self.snapshot = service, copy.deepcopy(snapshot)
        self.pending = None
    def forward_backward(self, data, loss):
        check(loss == "cross_entropy" and len(data) == 1, "Changed objective or batch size")
        datum = data[0]
        ids = datum.model_input.to_ints()
        targets = datum.loss_fn_inputs["target_tokens"].to_numpy().tolist()
        weights = datum.loss_fn_inputs["weights"].to_numpy().tolist()
        full = ids + [targets[-1]]
        row = self.service.full_rows[fingerprint(full)]
        check(ids == full[:-1] and targets == full[1:], "Causal shift changed")
        check(weights == [0.] * (row["promptTokenCount"] - 1) + [1.] * row["lossBearingTokenCount"], "Wrong loss mask")
        check(row["arm"] == self.snapshot["pipeline"], "Wrong training pipeline")
        check(row["exampleID"] not in self.snapshot["trained"], "Duplicate optimizer exposure")
        expected = self.service.train_order[row["arm"]][len(self.snapshot["trained"])]
        check(row["exampleID"] == expected, "Wrong within-block optimization order")
        self.pending = row
        values = [-99.] * (row["promptTokenCount"] - 1) + [-.5] * row["lossBearingTokenCount"]
        return Future(SimpleNamespace(loss_fn_outputs=[{"logprobs": np.asarray(values)}], metrics={"loss:sum": .5 * row["lossBearingTokenCount"]}))
    def optim_step(self, params):
        c = self.service.contract["optimizer"]
        check(params.model_dump() == {"learning_rate": c["learningRate"], "beta1": c["beta1"],
            "beta2": c["beta2"], "eps": c["epsilon"], "weight_decay": c["weightDecay"],
            "grad_clip_norm": c["gradientClipNorm"]}, "Changed optimizer hyperparameters")
        row = self.pending
        check(row is not None, "Optimizer step without gradient")
        self.snapshot["trained"].append(row["exampleID"])
        # Distinct optimizer state detects a weights-only resume.
        self.snapshot["moment"] = self.snapshot["moment"] * .95 + .125
        self.snapshot["weight"] += self.snapshot["moment"] * params.learning_rate
        self.service.calls.append(("train", row["arm"], row["exampleID"]))
        self.pending = None
        if self.service.fail_train_at == (row["arm"], len(self.snapshot["trained"])):
            self.service.fail_train_at = None
            return Future(error=RuntimeError("injected interruption after optimizer applied"))
        return Future(SimpleNamespace(metrics={"optimizer_steps": len(self.snapshot["trained"])}))
    def save_weights_for_sampler(self, name, ttl_seconds):
        check(ttl_seconds == 604800, "Changed checkpoint retention")
        path = "mock://sampler/" + name
        self.service.saved[path] = copy.deepcopy(self.snapshot)
        return Future(SimpleNamespace(path=path))
    def save_state(self, name, ttl_seconds):
        check(ttl_seconds == 604800, "Changed checkpoint retention")
        if self.service.fail_save:
            self.service.fail_save = False
            return Future(error=RuntimeError("injected state-save interruption"))
        path = "mock://optimizer/" + name
        self.service.saved[path] = copy.deepcopy(self.snapshot)
        return Future(SimpleNamespace(path=path))


class FakeService:
    def __init__(self, rows, blocks, contract, output_tokens):
        self.contract, self.output_tokens = contract, output_tokens
        self.prompt_rows, self.full_rows = {}, {}
        for arm in ("old", "new"):
            for block in blocks[arm]:
                for eid in block["exampleIDs"]:
                    r = rows[arm][eid]
                    self.prompt_rows[fingerprint(r["promptTokenIDs"])] = r
                    self.full_rows[fingerprint(r["promptTokenIDs"] + r["completionTokenIDs"])] = r
        self.prefixes, self.train_order = {}, {}
        for arm, schedule in blocks.items():
            self.prefixes[arm], self.train_order[arm], prefix = {}, [], []
            for b in schedule:
                self.prefixes[arm][b["blockOrdinal"]] = list(prefix)
                if b["trainThisBlockAfterScoring"]:
                    ordered = prior_order(b["exampleIDs"], b["blockOrdinal"])
                    prefix.extend(ordered)
                    self.train_order[arm].extend(ordered)
        self.saved, self.calls, self.restores = {}, [], []
        self.fail_generation = False
        self.fail_train_at = None
        self.fail_save = False
    def create_sampling_client(self, base_model=None, model_path=None):
        if model_path:
            check(base_model is None and "sampler" in model_path, "Sampler uses wrong checkpoint type")
            snap = self.saved[model_path]
        else:
            check(base_model == self.contract["model"], "Wrong frozen model")
            snap = {"kind": "frozen", "trained": [], "moment": 0., "weight": 0.}
        return FakeSampler(self, snap)
    def create_lora_training_client(self, base_model, rank, seed, train_mlp, train_attn, train_unembed, user_metadata):
        check(base_model == self.contract["model"] and rank == 32 and seed == 17, "Wrong adapter configuration")
        check(train_mlp and train_attn and train_unembed, "Changed trainable scope")
        return FakeTrainer(self, {"kind": "personalized", "pipeline": user_metadata["pipeline"],
                                  "trained": [], "moment": 0., "weight": 0.})
    def create_training_client_from_state_with_optimizer(self, path, base_model, user_metadata):
        check("optimizer" in path and base_model == self.contract["model"], "Weights-only/wrong-model restore")
        snapshot = self.saved[path]
        check(snapshot["pipeline"] == user_metadata["pipeline"], "Wrong pipeline optimizer restore")
        self.restores.append(path)
        return FakeTrainer(self, snapshot)


def compact_rows(cohorts, tokenizer):
    result = {"old": {}, "new": {}}
    for arm, arm_id in (("old", 41), ("new", 42)):
        for i, e in enumerate(cohorts[arm]):
            text = f"fixture é {i}"
            prompt = [100, arm_id, i + 1000, 101]
            target = tokenizer.encode(text, add_special_tokens=False) + [248046]
            result[arm][e["exampleID"]] = {
                "exampleID": e["exampleID"], "arm": arm, "blockOrdinal": i // 50 + 1,
                "promptTokenIDs": prompt, "completionTokenIDs": target,
                "promptTokenCount": len(prompt), "lossBearingTokenCount": len(target),
                "trainingDatumPositions": len(prompt) + len(target) - 1,
                "fullSequenceTokenSHA256": fingerprint(prompt + target),
                "modelInputSHA256": fingerprint(prompt), "targetSHA256": hashlib.sha256(text.encode()).hexdigest()}
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--packs', type=Path, default=ERA/'packs-v2')
    ap.add_argument('--output', type=Path, default=ERA/'execution-checks.json')
    args = ap.parse_args()
    packs = {p: args.packs / p for p in ("old", "new")}
    metric_tests = PROJECT / "scripts/check-phase1-likelihood-metrics.py"
    pinned = [ROOT / "training-contract.json", PROJECT / "scripts/phase1_qwen38_execution.py", Path(__file__), metric_tests]
    for pack in packs.values():
        pinned.extend([*pack.glob("*.jsonl"), pack / "blocks.json", pack / "packing-audit.json", pack / "independent-audit.json"])
    pinned_hashes = {str(p):file_hash(p) for p in pinned}
    metric_spec = importlib.util.spec_from_file_location("likelihood_checks", metric_tests)
    metric_module = importlib.util.module_from_spec(metric_spec); metric_spec.loader.exec_module(metric_module)
    metric_result = unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromModule(metric_module))
    check(metric_result.wasSuccessful(), "Likelihood metric tests failed")
    contract = json.loads((ROOT / "training-contract.json").read_text())
    old = json.loads((ROOT.parents[1] / "coupled-data/phase1-qwen35-450-preflight-v2-generation-only-db17c81/provider-plan.json").read_text())
    for k in ("rank", "trainAttention", "trainMLP", "trainUnembedding", "seed", "optimizer",
              "batchExamplesPerForwardBackward", "epochsPerNewBlockUpdate", "checkpointTTLSeconds"):
        check(contract[k] == old["training"][k], f"Prior Qwen training parameter changed: {k}")
    for k in ("maximumTokens", "temperature", "seed"):
        check(contract["generation"][k] == old["generation"][k], f"Prior generation setting changed: {k}")
    check(contract["learningRateExperiment"] is False, "LR sweep remains enabled")
    tok = AutoTokenizer.from_pretrained(ROOT / "tokenizer", local_files_only=True)
    spec = importlib.util.spec_from_file_location("native_exact", ROOT / "check-native-format.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    renderer = mod.ExactContentRenderer(tok)
    def forbidden(*a, **k):
        raise AssertionError("Network or real provider construction in offline tests")
    tinker.ServiceClient = forbidden
    socket.create_connection = forbidden
    socket.socket.connect = forbidden
    cohorts = {p: [json.loads(l) for l in (pack / "cohort.jsonl").open()] for p, pack in packs.items()}
    blocks = {p: json.loads((pack / "blocks.json").read_text()) for p, pack in packs.items()}
    rows = compact_rows(cohorts, tok)
    expected_steps = {p: sum(len(b["exampleIDs"]) for b in bs if b["trainThisBlockAfterScoring"]) for p, bs in blocks.items()}
    expected_counts = {"generation": sum(2 * (len(c) - 50) for c in cohorts.values()),
                       "nll": sum(2 * len(c) - 50 for c in cohorts.values()), "train": sum(expected_steps.values())}
    prices = {"train": 4.103, "prefill": 1.86, "sample": 5.595}
    output_tokens = tok.encode("offline fixture", add_special_tokens=False) + [248046]
    checks = ["BPB_UTF8_paste_EOS_mask_aggregation_and_call_count_unit_checks",
              "prior_Qwen_hyperparameters_exact", "LR_experiment_disabled"]
    real_rows = {p: NativeRows(pack / "native-rows.jsonl") for p, pack in packs.items()}
    for arm in real_rows:
        for i, e in enumerate(cohorts[arm], 1):
            datum_from_row(real_rows[arm][e["exampleID"]], tinker)
            if i % 150 == 0:
                print(f"SDK datum audit: {arm} {i}/{len(cohorts[arm])}", flush=True)
    checks.append("every_independent_pack_row_SDK_masks_and_shift")
    # Test actual native prompts selected across each independent timeline.
    selected_ids = {p: [c[i]["exampleID"] for i in sorted({0, len(c)//4, len(c)//2, 3*len(c)//4, len(c)-1})] for p,c in cohorts.items()}
    selected_rows = {p: {eid: real_rows[p][eid] for eid in selected_ids[p]} for p in real_rows}
    selected_blocks = {p: [{"blockOrdinal": 1, "exampleIDs": ids, "trainThisBlockAfterScoring": False}] for p,ids in selected_ids.items()}
    service = FakeService(selected_rows, selected_blocks, contract, output_tokens)
    bridge = TinkerBridge(service, tinker, tok, renderer, contract)
    sampler = bridge.sampler()
    for arm in selected_rows:
        for row in selected_rows[arm].values():
            g, n = bridge.generate(sampler, row), bridge.nll(sampler, row)
            check(g["prediction"] == "offline fixture" and n["meanNLL"] == .25, "Generation/NLL decoding contaminated")
            text = tok.decode(row["completionTokenIDs"][:-1], clean_up_tokenization_spaces=False)
            check(n["targetUTF8Bytes"] == len(text.encode("utf-8")), "Wrong BPB byte denominator")
            expected_bpb = .25 * (row["lossBearingTokenCount"] - 1) / math.log(2) / len(text.encode("utf-8"))
            check(math.isclose(n["bitsPerByte"], expected_bpb), "BPB includes prompt or terminator")
    checks.append("10_real_chronological_prefix_only_generations_and_target_only_NLL")
    checks.append("real_native_targets_BPB_exact_UTF8_bytes_excludes_prompt_and_terminator_no_extra_calls")
    with tempfile.TemporaryDirectory(prefix="qwen38-execution-check-") as temp:
        temp = Path(temp)
        binding = {"contract": fingerprint(contract), "data": {p:file_hash(pack / "cohort.jsonl") for p,pack in packs.items()}}
        def execute(name, service, **kwargs):
            journal = Journal(temp / name, binding)
            b = TinkerBridge(service, tinker, tok, renderer, contract)
            result = Executor(b, rows, blocks, journal, prices, 100, **kwargs).run()
            return journal, result
        base = FakeService(rows, blocks, contract, output_tokens)
        journal, result = execute("full", base)
        check(result["committedSteps"] == expected_steps, "Incorrect independent schedules")
        counts = {k: sum(r[0] == k for r in base.calls) for k in ("generation", "nll", "train")}
        check(counts == expected_counts, "Wrong operation counts")
        check(len(result["evaluationByBlock"]) == sum(2 * len(bs) - 1 for bs in blocks.values()),
              "Missing block-level NLL/BPB aggregates")
        for summary in result["evaluationByBlock"]:
            check(math.isclose(summary["bitsPerByte"], summary["textBits"] / summary["targetUTF8Bytes"]),
                  "Block BPB is not byte-weighted")
        check(all(r["value"]["meanNLL"] == .25 for r in journal.records if r.get("operation") == "nll" and r["kind"] == "operation_result"), "Context loss included in NLL")
        checks.append("full_independent_schedules_score_before_update_no_final_training")
        for p in ("old", "new"):
            final_ids = set(blocks[p][-1]["exampleIDs"])
            check(not any(c[0] == "train" and c[1] == p and c[2] in final_ids for c in base.calls),
                  "Final block unexpectedly trained")
        mismatched = copy.deepcopy(blocks); mismatched["old"] = copy.deepcopy(blocks["new"])
        rejected(lambda: Executor(TinkerBridge(base, tinker, tok, renderer, contract), rows, mismatched,
                                   Journal(temp / "shared_schedule", binding), prices, 100))
        leaked = copy.deepcopy(blocks)
        leaked["old"][0]["lastAvailableAt"] = leaked["old"][1]["firstBeganAt"]
        rejected(lambda: Executor(TinkerBridge(base, tinker, tok, renderer, contract), rows, leaked,
                                   Journal(temp / "leaked_schedule", binding), prices, 100))
        checks.append("shared_cohort_substitution_and_cross_block_leakage_rejected")
        prior_count = len(base.calls)
        execute("full", base)
        check(len(base.calls) == prior_count, "Completed run repeated provider operations")
        checks.append("completed_run_resume_no_duplicate_requests")
        expected_states = {arm: base.saved[journal.commits(arm)[-1]["optimizerStatePath"]] for arm in ("old", "new")}
        for name, mode in (("train_interrupt", "train"), ("score_interrupt", "score"), ("checkpoint_interrupt", "save")):
            fake = FakeService(rows, blocks, contract, output_tokens)
            if mode == "train": fake.fail_train_at = ("old", 63)
            if mode == "score": fake.fail_generation = True
            if mode == "save": fake.fail_save = True
            try:
                execute(name, fake)
                raise AssertionError("Injected interruption not observed")
            except RuntimeError as error:
                check("injected" in str(error), str(error))
            rejected(lambda: execute(name, fake))
            resumed, _ = execute(name, fake, retry_incomplete=True)
            for arm in ("old", "new"):
                actual = fake.saved[resumed.commits(arm)[-1]["optimizerStatePath"]]
                check(actual == expected_states[arm], "Resume changed weights, optimizer or exposure")
            checks.append(name + "_explicit_recovery_matches_uninterrupted_state")
        paused = FakeService(rows, blocks, contract, output_tokens)
        execute("between_blocks", paused, max_blocks=2)
        resumed, _ = execute("between_blocks", paused)
        check(len(paused.restores) == 2, "Missing optimizer resume for both pipelines")
        for arm in ("old", "new"):
            check(paused.saved[resumed.commits(arm)[-1]["optimizerStatePath"]] == expected_states[arm], "Clean checkpoint resume changed state")
        checks.append("clean_block_checkpoint_resume_preserves_optimizer")
        rejected(lambda: Journal(temp / "full", {"contract":"changed"}))
        checks.append("changed_execution_binding_rejected")
        plan = {"contract":contract, "conservativeTokenCostUSD":447.,
                "sourceFilesSHA256":{str(ROOT / "training-contract.json"):file_hash(ROOT / "training-contract.json")}}
        verify_execution_binding(plan)
        gate = dict(data_review={"approved":True, "planSHA256":fingerprint(plan)},
                    live_preflight={"status":"passed", "planSHA256":fingerprint(plan)},
                    personal_data_transfer=True, dedicated_project=True, maximum_usd=500., execute=True)
        check(verify_paid_gate(plan, **gate) == fingerprint(plan), "Matching approval rejected")
        for key, value in (("data_review",{}), ("live_preflight",{}), ("personal_data_transfer",False),
                           ("dedicated_project",False), ("maximum_usd",100.), ("execute",False)):
            rejected(lambda key=key,value=value: verify_paid_gate(plan, **{**gate,key:value}))
        changed = copy.deepcopy(plan); changed["sourceFilesSHA256"][str(ROOT / "training-contract.json")] = "incorrect"
        rejected(lambda: verify_execution_binding(changed))
        checks.append("paid_gate_requires_matching_review_preflight_transfer_project_budget_and_GO")
        with journal.exclusive():
            def competing_writer():
                other = Journal(temp / "full", binding)
                with other.exclusive():
                    pass
            rejected(competing_writer)
        checks.append("concurrent_output_writer_rejected")
        mutated = copy.deepcopy(rows["old"][cohorts["old"][0]["exampleID"]])
        mutated["completionTokenIDs"].insert(0, 248046)
        rejected(lambda: datum_from_row(mutated,tinker))
        changed_row = copy.deepcopy(rows["old"][cohorts["old"][0]["exampleID"]])
        changed_row["promptTokenIDs"][0] += 1
        rejected(lambda: datum_from_row(changed_row,tinker))
        checks.append("changed_tokens_or_duplicate_terminator_rejected")
        j = Journal(temp / "budget", binding)
        f = FakeService(rows, blocks, contract, output_tokens)
        b = TinkerBridge(f, tinker, tok, renderer, contract)
        rejected(lambda: Executor(b, rows, blocks, j, prices, 0).run())
        check(not f.calls, "Budget failure occurred after provider call")
        checks.append("budget_exhaustion_stops_before_paid_operation")
        damaged = Journal(temp / "damaged", binding)
        with damaged.path.open("ab") as h: h.write(b'{"partial":')
        rejected(lambda: Journal(temp / "damaged", binding))
        checks.append("incomplete_journal_never_silently_discarded")
    check(all(file_hash(p) == digest for p,digest in pinned_hashes.items()), "Execution/data changed during test")
    report = {"status":"passed", "checks":checks, "testedFilesSHA256":pinned_hashes,
              "bpbDefinition": BPB_DEFINITION,
              "contractSHA256":file_hash(ROOT / "training-contract.json"),
              "executionSHA256":file_hash(PROJECT / "scripts/phase1_qwen38_execution.py"), "testSHA256":file_hash(Path(__file__)),
              "datasets":{p:str(pack) for p,pack in packs.items()}, "counts":expected_counts,
              "committedSteps":expected_steps, "providerCalls":0, "networkBlocked":True,
              "limitation":"Real SDK payloads and production call path, simulated provider outputs/optimizer. Does not prove live GPU behavior or data semantic fidelity."}
    with args.output.open("x") as f:
        json.dump(report, f, indent=2); f.write("\n")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
