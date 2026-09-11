#!/usr/bin/env python3
"""Offline Sep2–10 frontier preparation. This program cannot send model requests.

Preserve the previous experiment's semantic 32K budget and completed requests.
Native model framing is added later, without independently selecting history.
Store shared events and per-example rendering overrides, not repeated prompts.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics

from phase1_read_model_comparison import (
    Privacy, canonical, file_hash, fingerprint, import_packer, make_prompt, rows,
)

VERSION = "phase1-frontier-completion-preparation-v1"
MODELS = ("chatgpt/gpt-5.5", "chatgpt/gpt-5.6-sol", "chatgpt/gpt-6-astra")


def load(path):
    return json.loads(Path(path).read_text())


def text_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def body(model, text):
    assert model in MODELS
    return {"model": model, "input": [{"role": "user", "content": [
        {"type": "input_text", "text": text}]}],
        "reasoning": {"effort": "xhigh"}, "tools": [], "stream": True}


def reconstruct(instruction, query, refs, events):
    chunks = [instruction + "\n"]
    for ref in refs:
        event = events[ref["eventID"]]
        value = ref.get("serializedOverride", event["serialized"])
        assert text_hash(value) == ref["serializedSHA256"], "Context text changed"
        assert ref["availableAt"] == event["availableAt"], "Context timing changed"
        chunks.append(value + "\n")
    return "".join(chunks) + query


def eligible_reuse(request, previous, prompt, example):
    """Reuse only the original answer to the exact input, model and target."""
    assert previous["variant"] == "new"
    for key in ("exampleID", "model", "requestSHA256", "reasoningEffort", "promptID"):
        assert request[key] == previous[key], f"Prior request differs: {key}"
    assert previous["responseModel"] in {request["model"], request["model"].removeprefix("chatgpt/")}
    assert prompt["query"] == example["query"]
    assert fingerprint(body(request["model"], prompt["modelInput"])) == request["requestSHA256"]
    return previous


def write_json(path, value):
    with path.open("x") as f:
        f.write(json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--training-prep", required=True, type=Path)
    ap.add_argument("--prior", required=True, type=Path)
    ap.add_argument("--initial", required=True, type=Path)
    ap.add_argument("--producer", required=True, type=Path)
    ap.add_argument("--reference-tokenizer", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    a = ap.parse_args()
    out = a.output.resolve()
    assert not out.exists(), "Use a new preparation directory"
    paired = a.training_prep.resolve() / "paired-qwen38"
    prior = a.prior.resolve()
    initial = a.initial.resolve()
    old_plan = load(prior / "frozen/plan.json")
    packer = import_packer(a.producer.resolve())
    assert old_plan["contextBudget"]["instruction"] == packer.DEFAULT_TASK_INSTRUCTION
    assert old_plan["contextBudget"]["referenceTokenizer"] == "Qwen/Qwen3.5-9B-Base"
    assert old_plan["contextBudget"]["revision"] == a.reference_tokenizer.name
    from transformers import AutoTokenizer
    reference = AutoTokenizer.from_pretrained(a.reference_tokenizer, local_files_only=True,
                                              split_special_tokens=True)
    native = AutoTokenizer.from_pretrained(a.training_prep / "tokenizer", local_files_only=True)
    events = {e["sourceEventID"]: e for e in rows(paired / "new-context-events.jsonl")}
    raw_events = {e["sourceEventID"]: e for e in rows(a.training_prep / "episodes/events.jsonl")}
    privacy = Privacy(raw_events, old_plan["privacyPolicy"])
    del raw_events
    old_examples = {e["exampleID"]: e for e in rows(prior / "frozen/cohort.jsonl")}
    prompt_sources = [initial / "frozen/prompts.jsonl",
                      prior / "expansion/remaining-frozen/prompts.jsonl"]
    prompts = {}
    for path in prompt_sources:
        for p in rows(path):
            if p["variant"] == "new":
                assert p["exampleID"] not in prompts
                prompts[p["exampleID"]] = p
    assert set(prompts) == set(old_examples)
    previous_path = prior / "execution-v1-final/final-output-v2/predictions.jsonl"
    previous = {(r["exampleID"], r["model"]): r for r in rows(previous_path) if r["variant"] == "new"}
    assert len(previous) == len(old_examples) * 2
    old_requests = {(r["exampleID"], r["model"]): r for r in rows(prior / "frozen/requests.planned.jsonl") if r["variant"] == "new"}
    gap_map = load(paired / "packing-audit.json")["corpusScopedGapIDRemapping"]
    native_plans = {p["exampleID"]: p for p in rows(paired / "new-context-plans.jsonl")}
    native_targets = {r["exampleID"]: r["completionTokenIDs"] for r in rows(paired / "new-native-rows.jsonl")}
    cache = {}; requests = []; counts = Counter(); input_lengths = []; changed = []
    out.mkdir(parents=True, mode=0o700)
    with (out / "context-events.jsonl").open("x") as f:
        for e in events.values():
            f.write(canonical(e) + "\n")
    with (out / "cohort.jsonl").open("x") as cohort_file, (out / "context-plans.jsonl").open("x") as context_file:
        for i, e in enumerate(rows(paired / "cohort.jsonl")):
            eid = e["exampleID"]
            if eid in prompts:
                old = old_examples[eid]
                for key in ("target", "targetText", "query", "targetBeganAt", "targetAvailableAt", "targetEventID"):
                    assert e[key] == old[key], f"Prior target/query changed: {key}"
                p = prompts[eid]
            else:
                p = make_prompt(e, "new", events, list(events), packer, reference, cache, 32768)
            refs = []
            for b in p["retainedBlocks"]:
                event_id = gap_map.get(b["eventID"], b["eventID"])
                event = events[event_id]
                assert event["availableAt"] < e["targetBeganAt"]
                assert event_id not in {e["targetEventID"], *e["episode"]["memberWriteEventIDs"]}
                ref = {"eventID": event_id, "availableAt": event["availableAt"],
                       "serializedSHA256": text_hash(b["serialized"])}
                if b["serialized"] != event["serialized"]:
                    ref["serializedOverride"] = b["serialized"]
                refs.append(ref)
            text = reconstruct(packer.DEFAULT_TASK_INSTRUCTION, e["query"], refs, events)
            assert text == p["modelInput"] and text_hash(text) == p["modelInputSHA256"]
            assert not privacy.unsafe(text), "Privacy check failed (payload not logged)"
            assert p["referenceInputTokens"] <= 32768
            ids = native.apply_chat_template([{"role": "user", "content": text}], tokenize=True,
                                             add_generation_prompt=True, enable_thinking=False)
            if hasattr(ids, "keys"):
                ids = ids["input_ids"]
            completion_ids = native.encode(e["targetText"], add_special_tokens=False) + [248046]
            assert completion_ids == native_targets[eid]
            assert len(ids) + len(completion_ids) <= 65536, "Native input/target exceeds served capacity"
            input_lengths.append(len(ids))
            if p["modelInputSHA256"] != native_plans[eid]["modelInputSHA256"]:
                changed.append(eid)
            compact = {k: e[k] for k in ("exampleID", "sessionID", "sourceOrdinal", "targetEventID",
                "targetBeganAt", "targetAvailableAt", "query", "target", "targetText", "modelFacingDestination")}
            compact.update(ordinal=i+1, blockOrdinal=i//50+1, postWarmup=i>=50,
                           memberWriteEventIDs=e["episode"]["memberWriteEventIDs"])
            cohort_file.write(canonical(compact) + "\n")
            context_file.write(canonical({"exampleID": eid, "promptID": p["promptID"],
                "modelInputSHA256": p["modelInputSHA256"], "referenceInputTokens": p["referenceInputTokens"],
                "nativeQwen38PrefixTokens": len(ids), "retainedBlocks": refs}) + "\n")
            for model in MODELS:
                request = {"exampleID": eid, "model": model, "variant": "new", "reasoningEffort": "xhigh",
                    "promptID": p["promptID"], "requestSHA256": fingerprint(body(model, text)),
                    "cohortOrdinal": i+1, "postWarmup": i>=50}
                found = previous.get((eid, model))
                if found:
                    eligible_reuse(request, found, p, e)
                    assert request["requestSHA256"] == old_requests[(eid, model)]["requestSHA256"]
                    request["reuse"] = {"source": str(previous_path), "rowSHA256": fingerprint(found),
                                        "responseID": found["responseID"]}
                counts[(model, "reused" if found else "new")] += 1
                requests.append(request)
            if (i+1) % 25 == 0:
                cache.clear()  # bounded token cache; avoid growth across the full corpus
                print(f"Locally verified {i+1} input plans; no provider calls", flush=True)
    assert len(input_lengths) == load(paired / "packing-audit.json")["pairedExamples"]
    # Mix models and cases over wall time, with reproducible order and no prompt nonce.
    requests.sort(key=lambda r: fingerprint([17, r["exampleID"], r["model"]]))
    with (out / "requests.planned.jsonl").open("x") as f:
        for i, r in enumerate(requests):
            f.write(canonical({**r, "requestOrdinal": i}) + "\n")
    source_paths = [paired / n for n in ("cohort.jsonl", "new-context-events.jsonl", "new-context-plans.jsonl", "new-native-rows.jsonl", "packing-audit.json")]
    source_paths += prompt_sources + [previous_path, prior / "frozen/plan.json", prior / "frozen/requests.planned.jsonl",
        prior / "frozen/cohort.jsonl", Path(__file__).resolve(), Path(__file__).with_name("phase1_read_model_comparison.py").resolve()]
    source_paths += list((a.producer / "scripts").glob("*.py"))
    source_paths += [p for p in a.reference_tokenizer.iterdir() if p.is_file() and p.suffix in {".json", ".txt", ".jinja"}]
    source_paths += [p for p in (a.training_prep / "tokenizer").iterdir() if p.is_file()]
    scoring_root = prior / "repeatability/run-v1/review-v1"
    scoring_pointer = load(scoring_root / "current-scoring.json")
    scoring_completion = scoring_root / scoring_pointer["directory"] / "completion.json"
    assert file_hash(scoring_completion) == scoring_pointer["completionSHA256"]
    source_paths += [scoring_root / "current-scoring.json", scoring_completion,
        scoring_root / "assistant-calibrations-20260909-v1.json", prior / "holistic-v3/policy.json"]
    plan = {
        "version": VERSION, "status": "offline_prepared_NOT_authorized_or_launched",
        "providerCalls": 0, "trainingLaunched": False, "examples": len(input_lengths),
        "postWarmupComparisonExamples": len(input_lengths)-50,
        "models": list(MODELS), "pipeline": old_plan["pipelineArms"]["new"],
        "contextBudget": {k:v for k,v in old_plan["contextBudget"].items() if k != "totalReferenceInputTokensPerModel"},
        "counts": {m: {k: counts[(m,k)] for k in ("reused", "new")} for m in MODELS},
        "nativeQwen38PrefixTokens": {"max": max(input_lengths), "median": statistics.median(input_lengths),
                                     "over32768": sum(x > 32768 for x in input_lengths)},
        "trainingAlignment": {"status": "reviewer_must_render_these_exact_new_contexts_without_repacking",
            "currentNativePackedInputsDiffer": len(changed), "differentExampleIDs": changed,
            "targetsAndQueriesUnchanged": True, "priorFrontierPromptsByteIdentical": len(old_examples),
            "doNotAlterLossContract": "Native assistant framing stays masked; exact authored/paste target and single terminator receive loss."},
        "providerContract": old_plan["providerContract"] | {
            "execution": "not wired by this preparation; freeze and mock-test resumable executor before confirmation",
            "retries": "one retry for explicit output-free 5xx; pause at two consecutive failures; uncertain dispatch, auth or quota requires review"},
        "scoring": {
            "bar": load(prior / "holistic-v3/policy.json")["passBar"],
            "calibration": load(scoring_root / "assistant-calibrations-20260909-v1.json")["principle"],
            "authority": "Explicit human judgments on identical answers are final; do not transfer grades to different outputs. No best-of-three selection.",
            "comparison": "Same post-warmup IDs across all arms; also report all-cohort frontier-only scores separately.",
            "eligibility": "Preserve existing target-only substantive classifications; classify new targets before reviewing new predictions. Report full and substantive denominators.",
            "invalidOrEmpty": "Retain and count as prediction failures; construction concerns recorded separately.",
            "uncertainty": "Paired win/loss counts and interval estimates; descriptive developmental evidence, not independent validation.",
            "secondary": ["exact match", "correct prefix", "character similarity", "paste-action correctness"]},
        "measurement": {"latency": "dispatch-to-final output; retain TTFT, mean/median/p95; retries separately",
            "cost": "actual subscription marginal billing recorded separately from API-equivalent usage estimates, including reasoning tokens",
            "aliasRevision": "server weights unverified; reused and newly sampled timestamps remain visible",
            "pricesChecked": "2026-09-10",
            "pricesUSDPerMillion": {"chatgpt/gpt-5.5": {"input":5,"cachedInput":.5,"output":30},
                "chatgpt/gpt-5.6-sol": {"input":4,"cachedInput":.4,"output":20},
                "chatgpt/gpt-6-astra": {"input":10,"cachedInput":1,"output":50}}},
        "hypotheses": ["GPT5.5 to GPT5.6 Sol to GPT6 Astra improves intended-thought prediction",
            "Qwen3.8-27B achieves useful quality at lower cost and latency",
            "New-pipeline continual training outperforms old-pipeline training; evaluate quality, paired NLL learning curves, cost and latency"],
        "interpretation": "Hypotheses are not assumed outcomes. Old/train-old/context versus new/train-new/context measures their combined effect, not training-only cleanup.",
        "remainingGates": ["confirm shared semantic context with training reviewer", "target-only substantive review for added cases",
            "freeze and mock-test three-model executor/resume", "explicit user confirmation before any provider call"],
        "sourceSHA256": {str(p.resolve()): file_hash(p) for p in source_paths},
    }
    write_json(out / "plan.json", plan)
    write_json(out / "artifact-hashes.json", {p.name:file_hash(p) for p in out.iterdir() if p.is_file()})
    print(json.dumps({k:plan[k] for k in ("status", "examples", "postWarmupComparisonExamples", "counts", "nativeQwen38PrefixTokens")}, indent=2))


if __name__ == "__main__":
    main()
