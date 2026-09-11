#!/usr/bin/env python3
"""Independent old/new Qwen3.8 packs, exhaustive native masks, no provider calls."""
import argparse
from collections import Counter
from functools import lru_cache
import hashlib
import importlib.util
import itertools
import json
from pathlib import Path
import resource
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
ERA = ROOT / "coupled-data/sep02-10-pipeline-era-comparison-20260911"
PREP = ROOT / "coupled-data/sep02-10-training-prep-20260910"
sys.path.insert(0, str(PREP / "runtime"))
from phase1_jsonl import JSONLSequence
from phase1_read_model_comparison import Privacy, canonical, file_hash, fingerprint, target_text
from phase1_storage import require_space


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec); sys.modules[name] = mod; spec.loader.exec_module(mod)
    return mod


def save(path, value):
    with path.open("x") as f: json.dump(value, f, sort_keys=True, indent=2, ensure_ascii=False); f.write("\n")


def write(f, value): f.write(canonical(value) + "\n")


def text_hash(text): return hashlib.sha256(text.encode()).hexdigest()


def causally_available(event, began):
    # Gap markers are known-at-onset environment metadata, not observations.
    # The first old-session target can start exactly at the gap's upper bound.
    if event["kind"] == "coverage_gap":
        return event["availableAt"] <= began
    return event["availableAt"] < began


def schedule(examples):
    blocks = []
    for start in range(0, len(examples), 50):
        subset = examples[start:start + 50]
        update = start + 50 < len(examples)
        blocks.append({"blockOrdinal": len(blocks) + 1, "exampleIDs": [e["exampleID"] for e in subset],
                       "firstBeganAt": subset[0]["targetBeganAt"], "lastAvailableAt": max(e["targetAvailableAt"] for e in subset),
                       "trainedExamplesBeforeScoring": start, "scoreBeforeUpdate": True,
                       "firstBlockRole": "warmup" if start == 0 else None,
                       "freeGenerationComparison": start > 0,
                       "freeGenerationModels": ["frozen", "personalized"] if start else [],
                       "nllModels": ["frozen", "personalized"] if start else ["frozen_shared_with_untrained_adapter"],
                       "trainThisBlockAfterScoring": update, "optimizerStepsAfterScoring": len(subset) if update else 0})
    # Do not silently train a still-active earlier target before later scoring.
    for before, after in zip(blocks, blocks[1:]):
        assert before["lastAvailableAt"] < after["firstBeganAt"], "Episode crosses score/update boundary; choose a reviewed temporal boundary"
    return blocks


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--arm", choices=["old", "new"], required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--comparison", type=Path, default=ERA / "comparison.json")
    a = ap.parse_args(); output = a.output.resolve()
    assert not output.exists(), "Use a fresh immutable pack"
    require_space(output)
    plan = json.loads(a.comparison.read_text())
    source = ERA / "historical/episodes" if a.arm == "old" else Path(plan["arms"]["new"]["artifactDirectory"])
    if a.arm == "old":
        replay = json.loads((ERA / "historical/replay.json").read_text())
        assert replay["status"] == "historical_replay_complete" and replay["gitCommit"] == plan["arms"]["old"]["gitCommit"]
    else:
        for p, digest in plan["arms"]["new"]["artifactsSHA256"].items(): assert file_hash(p) == digest
    packer_path = (ERA / "old-producer/scripts/pack-phase1-dataset.py" if a.arm == "old" else ROOT / "scripts/pack-phase1-dataset.py")
    packer = module("era_packer", packer_path)
    # Same token values; cache avoids repeatedly encoding the entire old history.
    packer.encode_plain_text = lru_cache(maxsize=16384)(packer.encode_plain_text)
    native = module("era_native_format", PREP / "check-native-format.py")
    native_report = json.loads((PREP / "native-format-checks.json").read_text())
    for name, digest in native_report["tokenizerFiles"].items(): assert file_hash(PREP / "tokenizer" / name) == digest
    assert file_hash(PREP / "check-native-format.py") == native_report["scriptSHA256"]
    from transformers import AutoTokenizer
    from tinker_cookbook.renderers import TrainOnWhat, get_text_content
    from tinker_cookbook.supervised.data import conversation_to_datum
    settings = json.loads((PREP / "paired-qwen38-reference32k/packing-audit.json").read_text())
    for p, digest in settings["referenceTokenizerFilesSHA256"].items(): assert file_hash(p) == digest
    reference = AutoTokenizer.from_pretrained(settings["contextBudget"]["referenceTokenizerDirectory"], local_files_only=True,
                                             trust_remote_code=False, split_special_tokens=True)
    tok = AutoTokenizer.from_pretrained(PREP / "tokenizer", local_files_only=True, trust_remote_code=False)
    renderer = native.ExactContentRenderer(tok)
    stops = renderer.get_stop_sequences(); assert stops == [248046]
    vocab_hash = fingerprint(tok.get_vocab())
    events = {r["sourceEventID"]: r for r in JSONLSequence(source / "events.jsonl")}
    # Existing privacy policy is a common safety layer, not new content curation.
    policy_path = ROOT / "coupled-data/clean-read-closed-write-review-20260911-r2/review.json"
    policy = json.loads(policy_path.read_text())["privacyPolicy"]
    seeds = {r["sourceEventID"]: r for r in JSONLSequence(PREP / "episodes/events.jsonl") if r["sourceEventID"] in policy["sensitiveEventIDs"]}
    privacy = Privacy(seeds, policy)
    blocks = {}
    for b in JSONLSequence(source / "context-blocks.jsonl"):
        eid = b["contextBlockID"]
        e = events.get(eid, {**b, "sourceEventID": eid, "kind": "coverage_gap", "availableAt": b.get("beforeAt")})
        blocks[eid] = privacy.filter_event(e)
    output.mkdir(parents=True)
    with (output / "context-events.jsonl").open("x") as f:
        for b in blocks.values(): write(f, b)
    examples, exclusions = [], []
    for row in JSONLSequence(source / "examples.jsonl"):
        if privacy.unsafe(row["query"] + canonical(row["target"])) or set(row.get("targetSourceRecordIDs", [])) & privacy.raw_ids:
            exclusions.append({"exampleID": row["exampleID"], "reason": "sensitive_query_or_target"}); continue
        # No target alignment, new-rule eligibility or new edits on the old arm.
        small = {k: v for k, v in row.items() if k not in {"context", "modelInput", "sourceRecordIDs", "contextSourceRecordIDs"}}
        small["targetText"] = target_text(row["target"])
        examples.append(small)
    assert len({r["exampleID"] for r in examples}) == len(examples)
    assert [r["targetBeganAt"] for r in examples] == sorted(r["targetBeganAt"] for r in examples)
    if a.arm == "new": assert len(examples) == plan["arms"]["new"]["counts"]["examples"], "New eligibility unexpectedly changed"
    blocks_schedule = schedule(examples)
    save(output / "blocks.json", blocks_schedule)
    with (output / "cohort.jsonl").open("x") as f:
        for e in examples: write(f, e)
    with (output / "exclusions.jsonl").open("x") as f:
        for e in exclusions: write(f, e)
    cache, stats = {}, []
    with (output / "native-rows.jsonl").open("x") as nf, (output / "context-plans.jsonl").open("x") as pf:
        for i, e in enumerate(examples):
            ids = e["contextBlockIDs"]
            forbidden = {e["targetEventID"], *e["episode"]["memberWriteEventIDs"]}
            assert not forbidden.intersection(ids)
            assert len(ids) == len(set(ids)) and all(causally_available(blocks[k], e["targetBeganAt"]) for k in ids)
            context = "\n".join(blocks[k]["serialized"] for k in ids)
            projection = {"exampleID": e["exampleID"], "contextBlockIDs": ids, "query": e["query"], "context": context,
                          "modelInput": (context + "\n" if context else "") + e["query"]}
            if a.arm == "old":
                packed = packer.pack_model_input(projection, blocks, reference, 32768, packer.DEFAULT_TASK_INSTRUCTION)
            else:
                packed = packer.pack_model_input(projection, blocks, reference, 32768, packer.DEFAULT_TASK_INSTRUCTION, cache, True)
            retained = []
            for span in packed["contextEventSpans"]:
                event = blocks[span["eventID"]]
                text = span.get("packedSerialized", event["serialized"])
                assert text_hash(text) == span["serializedSHA256"]
                retained.append({"eventID": span["eventID"], "serializedSHA256": text_hash(text), "availableAt": event["availableAt"],
                                 **({"serializedOverride": text} if text != event["serialized"] else {})})
            chunks = [packer.DEFAULT_TASK_INSTRUCTION + "\n"] + [r.get("serializedOverride", blocks[r["eventID"]]["serialized"]) + "\n" for r in retained] + [e["query"]]
            assert list(itertools.chain.from_iterable(packer.encode_plain_text(reference, c) for c in chunks)) == packed["inputIDs"]
            prompt = "".join(chunks); assert not privacy.unsafe(prompt)
            message = {"role": "user", "content": prompt}
            generated = renderer.build_generation_prompt([message]).to_ints()
            expected_prompt = tok.apply_chat_template([message], tokenize=True, add_generation_prompt=True, enable_thinking=False)
            expected_prompt = expected_prompt["input_ids"] if hasattr(expected_prompt, "keys") else expected_prompt
            assert generated == expected_prompt
            target_ids = tok.encode(e["targetText"], add_special_tokens=False)
            assert tok.decode(target_ids, clean_up_tokenization_spaces=False) == e["targetText"] and stops[0] not in target_ids
            parsed, _ = renderer.parse_response(target_ids + stops); assert get_text_content(parsed) == e["targetText"]
            messages = [message, {"role": "assistant", "content": e["targetText"]}]
            full, weights = renderer.build_supervised_example(messages, train_on_what=TrainOnWhat.LAST_ASSISTANT_MESSAGE)
            expected = generated + target_ids + stops
            expected_weights = [0.] * len(generated) + [1.] * (len(target_ids) + 1)
            assert full.to_ints() == expected and weights.tolist() == expected_weights
            assert len(expected) <= 65536 and len(generated) + 512 <= 65536
            datum = conversation_to_datum(messages, renderer, max_length=None, train_on_what=TrainOnWhat.LAST_ASSISTANT_MESSAGE, reduction="none")
            assert datum.model_input.to_ints() == expected[:-1]
            assert datum.loss_fn_inputs["target_tokens"].to_numpy().tolist() == expected[1:]
            assert datum.loss_fn_inputs["weights"].to_numpy().tolist() == expected_weights[1:]
            row = {"exampleID": e["exampleID"], "arm": a.arm, "blockOrdinal": i // 50 + 1,
                   "promptTokenIDs": generated, "completionTokenIDs": target_ids + stops,
                   "modelInputSHA256": text_hash(prompt), "querySHA256": text_hash(e["query"]), "targetSHA256": text_hash(e["targetText"]),
                   "fullSequenceTokenSHA256": fingerprint(expected), "promptTokenCount": len(generated),
                   "lossBearingTokenCount": len(target_ids) + 1, "referenceInputTokenCount": len(packed["inputIDs"]),
                   "trainingDatumPositions": len(expected) - 1}
            write(nf, row); write(pf, {"exampleID": e["exampleID"], "modelInputSHA256": text_hash(prompt),
                  "retainedBlocks": retained, "readRenderingCounts": packed.get("readRenderingCounts", {}), "historyBeforePacking": len(ids)})
            stats.append({k: row[k] for k in ["blockOrdinal", "promptTokenCount", "lossBearingTokenCount", "trainingDatumPositions"]})
            if (i + 1) % 25 == 0:
                require_space(output); cache.clear()
                rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024**2
                assert rss < 4500, "Packing process exceeded 4.5 GiB"
                print(f"{a.arm}: {i+1}/{len(examples)} native contracts passed; peak {rss:.0f} MiB", flush=True)
    assert fingerprint(tok.get_vocab()) == vocab_hash and "<|paste|>" not in tok.get_added_vocab()
    trained = [r for r in stats if r["blockOrdinal"] < len(blocks_schedule)]
    scored = [r for r in stats if r["blockOrdinal"] > 1]
    warmup = [r for r in stats if r["blockOrdinal"] == 1]
    # Bind all loaded local implementation dependencies, plus artifact sources.
    code = {Path(__file__), packer_path, PREP / "check-native-format.py"}
    for mod in list(sys.modules.values()):
        path = Path(getattr(mod, "__file__", "") or "/")
        if path.is_file() and path.suffix == ".py" and (path.is_relative_to(ROOT / "scripts") or path.is_relative_to(PREP / "runtime")): code.add(path)
    inputs = {source / n for n in ("events.jsonl", "examples.jsonl", "context-blocks.jsonl")}
    inputs |= {a.comparison.resolve(), policy_path, PREP / "episodes/events.jsonl", PREP / "native-format-checks.json"}
    report = {"version": "phase1-pipeline-era-native-pack-v2", "status": "passed_offline_native_contracts",
              "arm": a.arm, "source": str(source), "model": native_report["model"], "counts": {"examples": len(examples),
              "excludedForPrivacy": len(exclusions), "blocks": len(blocks_schedule), "trainingExamples": len(trained),
              "scoredExamples": len(scored), "historicalWrites": sum(b["kind"] == "write" for b in blocks.values()),
              "reads": sum(b["kind"] == "read" for b in blocks.values())},
              "tokens": {"uniqueLossBearing": sum(s["lossBearingTokenCount"] for s in stats),
                        "trainingPositions": sum(s["trainingDatumPositions"] for s in trained),
                        "trainingLossBearingPresentations": sum(s["lossBearingTokenCount"] for s in trained),
                        "generationPrefillOneModel": sum(s["promptTokenCount"] for s in scored),
                        "NLLSequenceTokensOneModel": sum(s["trainingDatumPositions"] + 1 for s in scored),
                        "warmupFrozenNLLSequenceTokens": sum(s["trainingDatumPositions"] + 1 for s in warmup),
                        "maxSequence": max(s["trainingDatumPositions"] + 1 for s in stats),
                        "medianPrompt": statistics.median(s["promptTokenCount"] for s in stats)},
              "contextBudget": {"referenceTokens": 32768, "nativeLimit": 65536, "targetTruncation": False},
              "causalRules": {"READ_WRITE": "availableAt < beganAt", "gap_metadata": "availableAt <= beganAt"},
              "encodingCacheEntries": 16384,
              "loss": "Exact authored content + literal paste markers + exactly one native terminator; all input/envelope masked; SDK shifted once",
              "allRowsVerifiedAgainstHFAndCookbook": True, "nativeTerminatorTokenID": stops[0],
              "tokenizerVocabularySHA256": vocab_hash, "tokenizerFilesSHA256": native_report["tokenizerFiles"],
              "referenceTokenizerFilesSHA256": settings["referenceTokenizerFilesSHA256"],
              "taskInstruction": packer.DEFAULT_TASK_INSTRUCTION,
              "sourceFilesSHA256": {str(p): file_hash(p) for p in sorted(code | inputs)},
              "artifactsSHA256": {p.name: file_hash(p) for p in sorted(output.iterdir()) if p.is_file()},
              "peakRSSMiB": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024**2,
              "providerCalls": 0, "trainingLaunched": False,
              "remaining": ["independent pack audit and deterministic replay", "execution rebinding and offline runner tests", "reviewed paid preflight and execution approval"]}
    save(output / "packing-audit.json", report)
    print(json.dumps(report["counts"], sort_keys=True), flush=True)


if __name__ == "__main__": main()
