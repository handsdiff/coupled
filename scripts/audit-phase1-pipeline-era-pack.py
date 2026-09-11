#!/usr/bin/env python3
"""Independently recheck saved Qwen rows through the execution adapter, locally."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
PREP = ROOT / "coupled-data/sep02-10-training-prep-20260910"
sys.path.insert(0, str(PREP / "runtime"))
from phase1_jsonl import JSONLSequence
from phase1_read_model_comparison import file_hash, fingerprint, canonical, target_text


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec); sys.modules[name] = mod; spec.loader.exec_module(mod); return mod


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--repeat", type=Path)
    ap.add_argument("--output", type=Path, required=True)
    a = ap.parse_args(); directory = a.input.resolve()
    report = json.loads((directory / "packing-audit.json").read_text())
    for path, digest in report["sourceFilesSHA256"].items(): assert file_hash(path) == digest, path
    for name, digest in report["artifactsSHA256"].items(): assert file_hash(directory / name) == digest, name
    equal = []
    if a.repeat:
        for name, digest in report["artifactsSHA256"].items():
            assert file_hash(a.repeat / name) == digest, "Repeat differs: " + name
            equal.append(name)
    from transformers import AutoTokenizer
    import tinker
    tok = AutoTokenizer.from_pretrained(PREP / "tokenizer", local_files_only=True, trust_remote_code=False)
    assert fingerprint(tok.get_vocab()) == report["tokenizerVocabularySHA256"]
    execution_path = ROOT / "scripts/phase1_qwen38_execution.py"
    execution = module("era_execution_adapter", execution_path)
    cohort = {r["exampleID"]: r for r in JSONLSequence(directory / "cohort.jsonl")}
    events = {r["sourceEventID"]: r for r in JSONLSequence(directory / "context-events.jsonl")}
    plans = {r["exampleID"]: r for r in JSONLSequence(directory / "context-plans.jsonl")}
    blocks = json.loads((directory / "blocks.json").read_text())
    order = [eid for b in blocks for eid in b["exampleIDs"]]
    assert order == list(cohort) and len(order) == len(set(order))
    assert not blocks[-1]["trainThisBlockAfterScoring"] and not blocks[0]["freeGenerationComparison"]
    seen = 0
    for b in blocks:
        assert b["trainedExamplesBeforeScoring"] == seen and b["scoreBeforeUpdate"]
        seen += b["optimizerStepsAfterScoring"]
    weighted = 0; count = 0
    for row in JSONLSequence(directory / "native-rows.jsonl"):
        assert row["exampleID"] == order[count]
        e = cohort[row["exampleID"]]; p = plans[row["exampleID"]]
        target = target_text(e["target"])
        assert target == e["targetText"]
        assert tok.decode(row["completionTokenIDs"][:-1], clean_up_tokenization_spaces=False) == target
        assert row["completionTokenIDs"][-1] == 248046 and row["completionTokenIDs"].count(248046) == 1
        refs = p["retainedBlocks"]
        forbid = {e["targetEventID"], *e["episode"]["memberWriteEventIDs"]}
        texts = []
        for ref in refs:
            assert ref["eventID"] not in forbid
            event = events[ref["eventID"]]
            assert ref["availableAt"] == event["availableAt"]
            if event["kind"] == "coverage_gap":
                assert event["availableAt"] <= e["targetBeganAt"]
            else:
                assert event["availableAt"] < e["targetBeganAt"]
            text = ref.get("serializedOverride", event["serialized"])
            assert hashlib.sha256(text.encode()).hexdigest() == ref["serializedSHA256"]
            texts.append(text)
        prompt = report["taskInstruction"] + "\n" + "".join(t + "\n" for t in texts) + e["query"]
        assert hashlib.sha256(prompt.encode()).hexdigest() == row["modelInputSHA256"] == p["modelInputSHA256"]
        native = tok.apply_chat_template([{"role": "user", "content": prompt}], tokenize=True, add_generation_prompt=True, enable_thinking=False)
        native = native["input_ids"] if hasattr(native, "keys") else native
        assert native == row["promptTokenIDs"]
        # Different code path from the packer's Cookbook conversation builder:
        # the execution adapter constructs the exact submitted pretokenized Datum.
        datum = execution.datum_from_row(row, tinker)
        ws = datum.loss_fn_inputs["weights"].to_numpy().tolist()
        assert ws == [0.] * (len(native) - 1) + [1.] * len(row["completionTokenIDs"])
        assert datum.loss_fn_inputs["target_tokens"].to_numpy().tolist()[-len(row["completionTokenIDs"]):] == row["completionTokenIDs"]
        weighted += int(sum(ws)); count += 1
    assert count == report["counts"]["examples"] == len(cohort)
    assert weighted == report["tokens"]["uniqueLossBearing"]
    result = {"status": "passed", "arm": report["arm"], "examples": count, "weightedPositions": weighted,
              "executionAdapterCheckedEveryRow": True, "allPromptsReconstructed": True,
              "deterministicRepeatedArtifacts": equal, "noProviderClientsCreated": True, "providerCalls": 0,
              "packingAuditSHA256": file_hash(directory / "packing-audit.json"),
              "executionAdapterSHA256": file_hash(execution_path), "auditScriptSHA256": file_hash(__file__)}
    with a.output.open("x") as f: json.dump(result, f, sort_keys=True, indent=2); f.write("\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__": main()
