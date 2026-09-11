"""Native Qwen3.8 SDK execution and resumable prequential state machine.

No ServiceClient is constructed here. Offline tests inject a strict fake service
into the SAME bridge used for live execution. A live launcher must separately
verify data review, code/data hashes, private project, budget and live preflight.

Copied from the Sep-10 reviewed execution core; the only schedule change is
independent, explicitly validated per-pipeline cohorts instead of shared IDs.
"""
from __future__ import annotations

import datetime as dt
import dataclasses
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import time
import uuid


class ContractError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise ContractError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_execution_binding(plan):
    """Run before constructing any provider client, and again on resume."""
    for path, digest in plan["sourceFilesSHA256"].items():
        require(file_hash(path) == digest, f"Frozen execution input changed: {path}")
    require(plan["contract"]["learningRateExperiment"] is False, "LR experiment is not authorized")
    require(plan["contract"]["optimizer"]["learningRate"] == .0002, "Learning rate changed")


def verify_paid_gate(plan, *, data_review, live_preflight, personal_data_transfer,
                     dedicated_project, maximum_usd, execute):
    """A launch wrapper must pass explicit approvals bound to this exact plan."""
    verify_execution_binding(plan)
    binding = fingerprint(plan)
    require(execute and personal_data_transfer and dedicated_project,
            "Explicit execution, data-transfer and private-project approvals are required")
    require(data_review.get("approved") is True and data_review.get("planSHA256") == binding,
            "Final semantic data review has not approved this plan")
    require(live_preflight.get("status") == "passed" and live_preflight.get("planSHA256") == binding,
            "Matching live execution preflight is required")
    require(maximum_usd >= plan["conservativeTokenCostUSD"], "Approved budget is below planned token cost")
    return binding


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def plain(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value):
        return {f.name: plain(getattr(value, f.name)) for f in dataclasses.fields(value)
                if not f.name.startswith("_")}
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if hasattr(value, "tolist"):
        return value.tolist()
    return value


def prior_order(ids, ordinal, seed=17):
    return sorted(ids, key=lambda eid: (
        hashlib.sha256(f"phase1-prequential-new-block:{seed}:{ordinal}:{eid}".encode()).digest(), eid))


class NativeRows:
    """Seek to one row at a time; don't retain gigabytes of repeated token IDs."""
    def __init__(self, path):
        self.path = Path(path)
        self.identity = self._stat()
        self.offsets = {}
        with self.path.open("rb") as f:
            while True:
                position = f.tell()
                line = f.readline()
                if not line:
                    break
                row = json.loads(line)
                eid = row["exampleID"]
                require(eid not in self.offsets, "Duplicate native example")
                self.offsets[eid] = position
        require(self._stat() == self.identity, "Native data changed during indexing")

    def _stat(self):
        s = self.path.stat()
        return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns

    def __getitem__(self, eid):
        require(self._stat() == self.identity, "Native data changed after preparation")
        with self.path.open("rb") as f:
            f.seek(self.offsets[eid])
            row = json.loads(f.readline())
        require(self._stat() == self.identity and row["exampleID"] == eid, "Native row identity changed")
        return row


def datum_from_row(row, sdk):
    import numpy as np
    prompt, target = row["promptTokenIDs"], row["completionTokenIDs"]
    require(prompt and target and target[-1] == 248046 and target.count(248046) == 1,
            "Missing or duplicate native terminator")
    require(len(prompt) == row["promptTokenCount"] and len(target) == row["lossBearingTokenCount"],
            "Native row length mismatch")
    ids = prompt + target
    require(len(ids) <= 65536 and len(ids) - 1 == row["trainingDatumPositions"], "Sequence length mismatch")
    require(fingerprint(ids) == row["fullSequenceTokenSHA256"], "Native token digest mismatch")
    weights = [0.] * (len(prompt) - 1) + [1.] * len(target)
    datum = sdk.Datum(
        model_input=sdk.ModelInput.from_ints(ids[:-1]),
        loss_fn_inputs={
            "target_tokens": sdk.TensorData.from_numpy(np.asarray(ids[1:], dtype=np.int64)),
            "weights": sdk.TensorData.from_numpy(np.asarray(weights, dtype=np.float32)),
        })
    require(datum.model_input.to_ints() == ids[:-1], "SDK changed inputs")
    require(datum.loss_fn_inputs["target_tokens"].to_numpy().tolist() == ids[1:], "SDK changed targets")
    require(datum.loss_fn_inputs["weights"].to_numpy().tolist() == weights, "SDK changed masks")
    return datum


def nll_result(logprobs):
    require(logprobs and all(x is not None and math.isfinite(float(x)) and x <= 0 for x in logprobs),
            "Invalid target log probabilities")
    values = [float(x) for x in logprobs]
    return {"targetLogprobs": values, "weightedNLLSum": -sum(values),
            "lossBearingTokens": len(values), "meanNLL": -sum(values) / len(values)}


BPB_DEFINITION = {
    "version": "phase1-target-text-bpb-v1",
    "numerator": "negative natural-log probabilities of target text tokens, divided by ln(2)",
    "denominator": "UTF-8 bytes of the exact target text, without Unicode normalization",
    "excludes": "prompt/history/query and the final native terminator (both probability and bytes)",
    "paste": "count the literal <|paste|> target string, not the resolved clipboard payload",
    "aggregation": "sum text bits / sum target bytes; never average per-example BPB",
    "comparison": "frozen versus personalized on the same examples within each pipeline arm",
}


def text_bpb_result(target_logprobs, target_text):
    """Target logprobs include one final terminator; text does not. No new inference."""
    checked = nll_result(target_logprobs)
    require(isinstance(target_text, str) and target_text and len(target_logprobs) > 1,
            "BPB requires nonempty target text plus a final terminator")
    byte_count = len(target_text.encode("utf-8"))
    text_nll = -math.fsum(checked["targetLogprobs"][:-1])
    bits = text_nll / math.log(2)
    return {"bpbVersion": BPB_DEFINITION["version"], "targetUTF8Bytes": byte_count,
            "textNLLSum": text_nll, "textBits": bits, "bitsPerByte": bits / byte_count}


def aggregate_likelihood(results):
    """Aggregate evaluation results by tokens for NLL and by bytes for BPB."""
    results = list(results)
    require(results and all(r.get("bpbVersion") == BPB_DEFINITION["version"] for r in results),
            "Cannot aggregate absent or incompatible BPB results")
    tokens = sum(r["lossBearingTokens"] for r in results)
    byte_count = sum(r["targetUTF8Bytes"] for r in results)
    require(tokens > 0 and byte_count > 0, "Empty likelihood denominator")
    nll = math.fsum(r["weightedNLLSum"] for r in results)
    text_nll = math.fsum(r["textNLLSum"] for r in results)
    bits = text_nll / math.log(2)
    return {"examples": sum(r.get("examples", 1) for r in results), "weightedNLLSum": nll,
            "lossBearingTokens": tokens, "meanNLL": nll / tokens,
            "bpbVersion": BPB_DEFINITION["version"], "targetUTF8Bytes": byte_count,
            "textNLLSum": text_nll, "textBits": bits, "bitsPerByte": bits / byte_count}


def evaluation_by_block(records):
    """Only held-out/pre-update NLL results, never optimizer-step losses."""
    groups = {}
    for record in records:
        if record["kind"] != "operation_result" or record.get("operation") != "nll":
            continue
        identity = record["identity"]
        key = (identity["pipeline"], identity["blockOrdinal"], identity["condition"])
        groups.setdefault(key, []).append(record["value"])
    return [{"pipeline": p, "blockOrdinal": b, "condition": c, **aggregate_likelihood(values)}
            for (p, b, c), values in sorted(groups.items())]


class TinkerBridge:
    def __init__(self, service, sdk, tokenizer, renderer, contract):
        self.service, self.sdk = service, sdk
        self.tokenizer, self.renderer, self.contract = tokenizer, renderer, contract

    def sampler(self, checkpoint=None):
        if checkpoint is None:
            return self.service.create_sampling_client(base_model=self.contract["model"])
        return self.service.create_sampling_client(model_path=checkpoint)

    def trainer(self, pipeline, parent=None):
        c = self.contract
        metadata = {"purpose": "phase1-qwen38-prequential", "pipeline": pipeline}
        if parent:
            return self.service.create_training_client_from_state_with_optimizer(
                parent["optimizerStatePath"], base_model=c["model"], user_metadata=metadata)
        return self.service.create_lora_training_client(
            base_model=c["model"], rank=c["rank"], seed=c["seed"],
            train_mlp=c["trainMLP"], train_attn=c["trainAttention"],
            train_unembed=c["trainUnembedding"], user_metadata=metadata)

    def generate(self, sampler, row):
        c = self.contract["generation"]
        started = time.monotonic()
        response = sampler.sample(
            prompt=self.sdk.ModelInput.from_ints(row["promptTokenIDs"]),
            num_samples=c["samplesPerExample"],
            sampling_params=self.sdk.SamplingParams(
                max_tokens=c["maximumTokens"], temperature=c["temperature"],
                seed=c["seed"], stop=c["stopTokenIDs"])).result()
        latency = time.monotonic() - started
        require(len(response.sequences) == 1, "Unexpected generation count")
        sequence = response.sequences[0]
        tokens = list(sequence.tokens)
        require(len(tokens) <= c["maximumTokens"], "Provider exceeded generation cap")
        stop = str(getattr(sequence.stop_reason, "value", sequence.stop_reason))
        parse_tokens = list(tokens)
        added = (not tokens or tokens[-1] != 248046) and stop in ("stop", "stop_sequence", "eos")
        if added:
            parse_tokens.append(248046)
        from tinker_cookbook.renderers import get_text_content
        message, termination = self.renderer.parse_response(parse_tokens)
        return {"prediction": get_text_content(message), "predictionTokenIDs": tokens,
                "rawDecodedPrediction": self.tokenizer.decode(tokens, clean_up_tokenization_spaces=False),
                "rawProviderResponse": plain(response), "stopReason": stop,
                "parseTermination": str(getattr(termination, "value", termination)),
                "parserOnlyTerminatorAdded": added, "latencySeconds": latency,
                "inputTokens": len(row["promptTokenIDs"]), "outputTokens": len(tokens)}

    def nll(self, sampler, row):
        target = row["completionTokenIDs"]
        require(len(target) > 1 and target[-1] == 248046 and target.count(248046) == 1,
                "BPB requires target text followed by exactly one native terminator")
        # Decode the complete text, not individual tokens: UTF-8 characters may
        # span tokens. Match the pack's original target bytes before any paid call.
        text = self.tokenizer.decode(target[:-1], clean_up_tokenization_spaces=False)
        require(text and hashlib.sha256(text.encode("utf-8")).hexdigest() == row["targetSHA256"],
                "BPB decoded target differs from the packed target text")
        ids = row["promptTokenIDs"] + row["completionTokenIDs"]
        started = time.monotonic()
        values = sampler.compute_logprobs(self.sdk.ModelInput.from_ints(ids)).result()
        require(len(values) == len(ids), "Full NLL response length mismatch")
        result = nll_result(values[len(row["promptTokenIDs"]):])
        result.update(latencySeconds=time.monotonic() - started, inputTokens=len(ids),
                      fullLogprobsSHA256=fingerprint(values))
        result.update(text_bpb_result(result["targetLogprobs"], text))
        return result

    def train(self, trainer, row):
        datum = datum_from_row(row, self.sdk)
        c = self.contract["optimizer"]
        optimizer = self.sdk.AdamParams(
            learning_rate=c["learningRate"], beta1=c["beta1"], beta2=c["beta2"],
            eps=c["epsilon"], weight_decay=c["weightDecay"], grad_clip_norm=c["gradientClipNorm"])
        started = time.monotonic()
        forward = trainer.forward_backward([datum], "cross_entropy")
        step = trainer.optim_step(optimizer)
        output, optimized = forward.result(), step.result()
        values = output.loss_fn_outputs[0]["logprobs"].tolist()
        require(len(values) == row["trainingDatumPositions"], "Training logprob length mismatch")
        result = nll_result(values[len(row["promptTokenIDs"]) - 1:])
        result.update(latencySeconds=time.monotonic() - started,
                      inputTokens=row["trainingDatumPositions"],
                      forwardMetrics=plain(output.metrics), optimizerMetrics=plain(optimized.metrics))
        return result

    def checkpoint(self, trainer, name):
        ttl = self.contract["checkpointTTLSeconds"]
        sample = trainer.save_weights_for_sampler(name + "-sampler", ttl_seconds=ttl).result()
        state = trainer.save_state(name + "-optimizer", ttl_seconds=ttl).result()
        return {"samplerCheckpointPath": sample.path, "optimizerStatePath": state.path}


class Journal:
    """Append-only evidence. Never silently truncate a crash-damaged last line."""
    def __init__(self, directory, binding):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "operations.jsonl"
        self.records = []
        with (self.directory / "writer.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ContractError("Another executor holds this output directory") from error
            try:
                if self.path.exists():
                    with self.path.open("rb") as f:
                        for line in f:
                            require(line.endswith(b"\n"), "Incomplete journal tail; preserve and repair explicitly")
                            self.records.append(json.loads(line))
                if self.records:
                    require(self.records[0] == {"kind": "binding", "value": binding}, "Execution binding changed")
                else:
                    self.append({"kind": "binding", "value": binding})
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def append(self, record):
        with self.path.open("ab") as f:
            f.write((canonical(record) + "\n").encode())
            f.flush()
            os.fsync(f.fileno())
        self.records.append(record)

    def results(self):
        result = {}
        for r in self.records:
            if r["kind"] == "operation_result":
                require(r["key"] not in result, "Duplicate committed operation")
                result[r["key"]] = r
        return result

    def commits(self, pipeline):
        return [r for r in self.records if r["kind"] == "block_commit" and r["pipeline"] == pipeline]

    @contextmanager
    def exclusive(self):
        with (self.directory / "writer.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ContractError("Another executor holds this output directory") from error
            try:
                # A second process could have finished between journal loading
                # and lock acquisition. Refuse its stale in-memory view.
                with self.path.open("rb") as f:
                    on_disk = [json.loads(line) for line in f]
                require(on_disk == self.records, "Journal changed before writer lock")
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


class Executor:
    def __init__(self, bridge, rows, blocks, journal, prices, maximum_usd,
                 retry_incomplete=False, max_blocks=None):
        self.bridge, self.rows, self.blocks = bridge, rows, blocks
        require(isinstance(blocks, dict) and set(blocks) == {"old", "new"},
                "Independent old/new block schedules are required")
        for pipeline, schedule in blocks.items():
            require(bool(schedule), "Empty pipeline schedule")
            ids = []
            for i, block in enumerate(schedule, 1):
                require(block["blockOrdinal"] == i and 0 < len(block["exampleIDs"]) <= 50,
                        "Invalid chronological schedule")
                require(block["trainThisBlockAfterScoring"] == (i < len(schedule)),
                        "Terminal block must not train")
                require(block["trainedExamplesBeforeScoring"] == len(ids),
                        "Incorrect per-arm training exposure")
                if i > 1:
                    require(schedule[i-2]["lastAvailableAt"] < block["firstBeganAt"],
                            "Update uses future target evidence")
                ids.extend(block["exampleIDs"])
            require(len(set(ids)) == len(ids), "Duplicate schedule membership")
            row_ids = rows[pipeline].offsets if isinstance(rows[pipeline], NativeRows) else rows[pipeline]
            require(set(ids) == set(row_ids), "Pack and per-arm schedule differ")
        self.journal, self.prices, self.maximum_usd = journal, prices, maximum_usd
        self.retry_incomplete, self.max_blocks = retry_incomplete, max_blocks
        self.completed = journal.results()

    def charge(self, kind, row):
        p = self.prices
        if kind == "generation":
            return (len(row["promptTokenIDs"]) * p["prefill"] +
                    self.bridge.contract["generation"]["maximumTokens"] * p["sample"]) / 1e6
        if kind == "nll":
            # SDK 0.25.0 implements compute_logprobs with a one-token sample.
            return ((len(row["promptTokenIDs"]) + len(row["completionTokenIDs"])) *
                    p["prefill"] + p["sample"]) / 1e6
        return row["trainingDatumPositions"] * p["train"] / 1e6

    def reserve(self, maximum):
        spent = sum(r.get("maximumUSD", 0.) for r in self.journal.records if r["kind"] == "operation_begin")
        require(spent + maximum <= self.maximum_usd, "Execution budget exhausted before request")

    def operation(self, key, kind, row, function, identity):
        if key in self.completed:
            require(self.completed[key]["identity"] == identity, "Checkpoint/request identity changed")
            return self.completed[key]["value"]
        attempts = [r for r in self.journal.records if r["kind"] == "operation_begin" and r["key"] == key]
        require(not attempts or (self.retry_incomplete and len(attempts) == 1),
                "Uncertain operation requires an explicit, bounded retry")
        maximum = self.charge(kind, row)
        self.reserve(maximum)
        self.journal.append({"kind": "operation_begin", "key": key, "operation": kind,
                             "identity": identity, "maximumUSD": maximum, "at": utc()})
        result = function()
        record = {"kind": "operation_result", "key": key, "operation": kind,
                  "identity": identity, "value": result, "at": utc()}
        self.journal.append(record)
        self.completed[key] = record
        return result

    def run(self):
        with self.journal.exclusive():
            return self._run()

    def _run(self):
        for pipeline in ("old", "new"):
            blocks = self.blocks[pipeline]
            commits = self.journal.commits(pipeline)
            for i, commit in enumerate(commits, 1):
                expected = prior_order(blocks[i - 1]["exampleIDs"], i, self.bridge.contract["seed"])
                require(commit["blockOrdinal"] == i and commit["exampleIDs"] == expected,
                        "Committed update order/membership differs")
                require(commit["parentOptimizerStatePath"] == (commits[i-2]["optimizerStatePath"] if i > 1 else None),
                        "Committed optimizer lineage differs")
            require(len(commits) < len(blocks), "Unexpected terminal-block training")
            base = self.bridge.sampler()
            trainer = None
            for block in blocks:
                ordinal, ids = block["blockOrdinal"], block["exampleIDs"]
                if self.max_blocks is not None and ordinal > self.max_blocks:
                    break
                require(ordinal >= 1 and len(ids) <= 50 and ids, "Invalid chronological block")
                parent = commits[ordinal - 2] if ordinal > 1 and len(commits) >= ordinal - 1 else None
                if ordinal > 1:
                    require(parent is not None, "No trained checkpoint for future block")
                samplers = [("frozen", base, None)]
                if ordinal > 1:
                    samplers.append(("personalized", self.bridge.sampler(parent["samplerCheckpointPath"]),
                                     parent["samplerCheckpointPath"]))
                for condition, sampler, checkpoint in samplers:
                    for eid in ids:
                        row = self.rows[pipeline][eid]
                        identity = {"pipeline": pipeline, "condition": condition, "blockOrdinal": ordinal,
                                    "exampleID": eid, "checkpointPath": checkpoint,
                                    "modelInputSHA256": row["modelInputSHA256"], "targetSHA256": row["targetSHA256"]}
                        for kind in (("generation", "nll") if ordinal > 1 else ("nll",)):
                            key = f"{pipeline}/{ordinal}/{condition}/{eid}/{kind}"
                            function = self.bridge.generate if kind == "generation" else self.bridge.nll
                            self.operation(key, kind, row, lambda: function(sampler, row), identity)
                if ordinal == len(blocks) or ordinal <= len(commits):
                    continue
                update_parent = commits[-1] if commits else None
                prior_attempts = [r for r in self.journal.records if r["kind"] == "block_begin" and
                                  r["pipeline"] == pipeline and r["blockOrdinal"] == ordinal]
                require(not prior_attempts or (self.retry_incomplete and len(prior_attempts) == 1),
                        "Uncommitted update requires explicit restart from its parent optimizer state")
                if trainer is None or prior_attempts:
                    trainer = self.bridge.trainer(pipeline, update_parent)
                order = prior_order(ids, ordinal, self.bridge.contract["seed"])
                self.reserve(sum(self.charge("train", self.rows[pipeline][eid]) for eid in order))
                attempt = str(uuid.uuid4())
                self.journal.append({"kind": "block_begin", "pipeline": pipeline, "blockOrdinal": ordinal,
                                     "attempt": attempt, "exampleIDs": order,
                                     "parentOptimizerStatePath": update_parent["optimizerStatePath"] if update_parent else None,
                                     "at": utc()})
                for position, eid in enumerate(order, 1):
                    row = self.rows[pipeline][eid]
                    identity = {"pipeline": pipeline, "blockOrdinal": ordinal, "attempt": attempt,
                                "position": position, "exampleID": eid}
                    self.operation(f"{pipeline}/{ordinal}/{attempt}/{position}/train", "train", row,
                                   lambda: self.bridge.train(trainer, row), identity)
                name = f"phase1-qwen38-{pipeline}-{ordinal}-{attempt}"
                saved = self.bridge.checkpoint(trainer, name)
                commit = {"kind": "block_commit", "pipeline": pipeline, "blockOrdinal": ordinal,
                          "attempt": attempt, "exampleIDs": order,
                          "parentOptimizerStatePath": update_parent["optimizerStatePath"] if update_parent else None,
                          **saved, "at": utc()}
                self.journal.append(commit)
                commits.append(commit)
        return {"committedSteps": {p: sum(len(c["exampleIDs"]) for c in self.journal.commits(p)) for p in ("old", "new")},
                "completedOperations": len(self.completed),
                "bpbDefinition": BPB_DEFINITION,
                "evaluationByBlock": evaluation_by_block(self.journal.records)}
