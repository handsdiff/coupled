"""Offline, paired READ-pipeline/model comparison. No provider client or credentials.

Semantic WRITEs and onset queries come from one frozen episode corpus. Only
READ histories differ. Privacy applies before packing and is checked afterward.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import itertools
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

VERSION = "phase1-read-model-factorial-v2-native-order"
MODELS = ("chatgpt/gpt-5.6-sol", "chatgpt/gpt-6-astra")
VARIANTS = ("old", "new")
TOKENIZER_REPO = "Qwen/Qwen3.5-9B-Base"
TOKENIZER_REVISION = "68c46c4b3498877f3ef123c856ecfde50c39f404"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def rows(path):
    with Path(path).open() as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def dump(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def dump_rows(path, values):
    with Path(path).open("w") as f:
        for value in values:
            f.write(canonical(value) + "\n")


def import_packer(frozen):
    folder = Path(frozen) / "scripts"
    sys.path.insert(0, str(folder.resolve()))
    spec = importlib.util.spec_from_file_location("factorial_frozen_packer", folder / "pack-phase1-dataset.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def target_text(target):
    result = []
    for segment in target["segments"]:
        if segment["type"] == "authored_text":
            result.append(segment["content"])
        elif segment["type"] == "paste":
            result.append("<|paste|>")
        else:
            raise ValueError("Unsupported target segment")
    return "".join(result)


class Privacy:
    """Known credential lineage + payload propagation + high-specificity scans.

    This is not anonymization or a guarantee of detecting arbitrary secrets.
    Values stay in memory; reports contain event IDs and reasons, not secrets.
    """
    patterns = (
        re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}"),
        re.compile(r"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}"),
        re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        re.compile(r"\b(?:Bearer)\s+[A-Za-z0-9_.-]{30,}", re.I),
        re.compile(r"\bpassword\s*:", re.I),
        re.compile(r"\bsk-\s*(?:\n|\\n)[A-Za-z0-9_-]{24,}"),
        re.compile(r'\b(?:api[_ -]?key|access[_ -]?token|secret)\b[\\"\s]*[:=][\\"\s]*[A-Za-z0-9_-]{20,}', re.I),
    )

    def __init__(self, event_by_id, policy):
        self.ids = set(policy["sensitiveEventIDs"])
        self.raw_ids = set()
        self.values = set()
        self.credential_sources = set()
        for key in sorted(self.ids):
            event = event_by_id[key]  # missing evidence must fail, never silently skip
            self.raw_ids.update(event.get("sourceRecordIDs", []))
            payload = json.loads(event["serialized"])
            if event["kind"] == "write":
                self.values.update(s["content"] for s in payload.get("authorshipSegments", []) if s.get("content"))
            else:
                source = payload.get("source", {})
                if source.get("application") and source.get("resourceTitle"):
                    self.credential_sources.add((source["application"], source["resourceTitle"]))
                # Credential page: retain long exposed key lines and labeled
                # password values in memory to detect copies into other surfaces.
                text = payload.get("content", "")
                self.values.update(re.findall(r"[A-Za-z0-9_-]{24,}", text))
                for match in re.finditer(r"(?i)password\s*:\s*([^\n]+)", text):
                    value = match.group(1).strip()
                    if len(value) >= 4:
                        self.values.add(value)
        if not self.values:
            raise ValueError("Privacy seeds did not resolve any payloads")

    def unsafe(self, text):
        return any(value in text for value in self.values) or any(p.search(text) for p in self.patterns)

    def filter_event(self, event):
        event = copy.deepcopy(event)
        source = json.loads(event["serialized"]).get("source", {})
        credential_source = (source.get("application"), source.get("resourceTitle")) in self.credential_sources
        flagged = (event["sourceEventID"] in self.ids
                   or credential_source
                   or bool(set(event.get("sourceRecordIDs", [])) & self.raw_ids)
                   or self.unsafe(event["serialized"])
                   or self.unsafe(canonical(event.get("readNovelty", {}))))
        if flagged:
            event["serialized"] = canonical({"kind": event["kind"], "privacy": "sensitive_content_redacted"})
            # No retained fallback/novelty payload can resurrect a redacted READ.
            event.pop("readNovelty", None)
            event["privacyRedacted"] = True
        # Model-context audit artifacts must not retain the unfiltered projection.
        return {k: event[k] for k in (
            "sourceEventID", "sessionID", "kind", "availableAt", "sourceRecordIDs",
            "sourceSessionOrdinal", "serialized", "readNovelty", "privacyRedacted",
        ) if k in event}

    def filter_stream(self, events):
        filtered = [self.filter_event(e) for e in events]
        redacted = {e["sourceEventID"] for e in filtered if e.get("privacyRedacted")}
        for event in filtered:
            if event.get("readNovelty", {}).get("dependsOnEventID") in redacted:
                # A redaction is not a visible, reconstructable predecessor.
                # Retain this safe complete observation instead of its delta.
                event.pop("readNovelty")
                event["privacyDependencyFallback"] = True
        return filtered


def select_cohort(examples, count, seed):
    groups = defaultdict(list)
    for row in examples:
        groups[row["modelFacingDestination"].get("application", "unknown")].append(row)
    total = len(examples)
    if count > total:
        raise ValueError("Insufficient eligible examples")
    quotas = {k: len(v) * count // total for k, v in groups.items()}
    residual = sorted(groups, key=lambda k: (-(len(groups[k]) * count % total), k))
    for k in residual[:count - sum(quotas.values())]:
        quotas[k] += 1
    chosen = []
    for app, values in sorted(groups.items()):
        chosen.extend(sorted(values, key=lambda r: fingerprint([seed, r["exampleID"]]))[:quotas[app]])
    return sorted(chosen, key=lambda r: (r["targetBeganAt"], r["exampleID"])), quotas


def timeline(events, gaps):
    result = {e["sourceEventID"]: e for e in events}
    if len(result) != len(events):
        raise ValueError("Duplicate event identity")
    for gap in gaps:
        result[gap["contextBlockID"]] = {
            "sourceEventID": gap["contextBlockID"], "kind": "coverage_gap",
            "availableAt": gap["beforeAt"], "serialized": gap["serialized"],
        }
    # Index only: callers supply pipeline-native order. Never sort by time here.
    return result, list(result)


def native_episode_order(source_events, episode_events, gaps):
    """Apply the frozen episode projection's first-occurrence order, no re-sort.

    This is the same ordering rule as normalized_context in the frozen closed
    episode constructor: replace micro-WRITEs by their closed WRITE, keep the
    first occurrence, and omit primitives excluded from normalized history.
    READs retain the source pipeline's order. Availability is checked separately.
    """
    by_id = {e["sourceEventID"]: e for e in episode_events}
    micro_to_episode = {}
    for event in episode_events:
        for member in event.get("memberWriteEventIDs", []):
            if member in micro_to_episode:
                raise ValueError("Micro-WRITE belongs to multiple closed WRITEs")
            micro_to_episode[member] = event["sourceEventID"]
    gaps_by_session = {g["toSessionID"]: g["contextBlockID"] for g in gaps}
    result, seen, sessions = [], set(), set()
    for source in source_events:
        session = source["sessionID"]
        if session not in sessions:
            if session in gaps_by_session:
                result.append(gaps_by_session[session])
                seen.add(result[-1])
            sessions.add(session)
        source_id = source["sourceEventID"]
        mapped = micro_to_episode.get(source_id) if source["kind"] == "write" else source_id
        if mapped is None or mapped not in by_id or mapped in seen:
            continue
        seen.add(mapped)
        result.append(mapped)
    return result


def make_prompt(example, variant, event_map, ordered, packer, tokenizer, cache, budget):
    cutoff = example["targetBeganAt"]
    native_ids = example.get("contextBlockIDs", ordered) if variant == "new" else ordered
    ids = [key for key in native_ids if event_map[key]["availableAt"] < cutoff]
    if variant == "new" and "contextBlockIDs" in example and ids != native_ids:
        raise ValueError("Frozen native context contains unavailable events; do not silently repair it")
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate native context block")
    forbidden = {example["targetEventID"], *example["episode"]["memberWriteEventIDs"]}
    if forbidden.intersection(ids):
        raise ValueError("Target/member WRITE entered its own context")
    context = "\n".join(event_map[key]["serialized"] for key in ids)
    query = example["query"]
    projection = {"exampleID": example["exampleID"], "query": query,
                  "contextBlockIDs": ids, "context": context,
                  "modelInput": context + "\n" + query if context else query}
    packed = packer.pack_model_input(
        projection, event_map, tokenizer, budget, packer.DEFAULT_TASK_INSTRUCTION,
        cache, dependency_aware_read_novelty=(variant == "new"))
    blocks = []
    for span in packed["contextEventSpans"]:
        e = event_map[span["eventID"]]
        blocks.append({**span, "kind": e["kind"], "availableAt": e["availableAt"],
                       "serialized": span.get("packedSerialized", e["serialized"]),
                       "privacyRedacted": e.get("privacyRedacted", False)})
    chunks = [packer.DEFAULT_TASK_INSTRUCTION + "\n"] + [b["serialized"] + "\n" for b in blocks] + [query]
    reconstructed_ids = list(itertools.chain.from_iterable(packer.encode_plain_text(tokenizer, c) for c in chunks))
    if reconstructed_ids != packed["inputIDs"]:
        raise AssertionError("Rendered prompt differs from packed chunks")
    model_input = "".join(chunks)
    return {
        "promptID": fingerprint([example["exampleID"], variant, model_input]),
        "exampleID": example["exampleID"], "variant": variant,
        "targetBeganAt": cutoff, "query": query, "modelInput": model_input,
        "modelInputSHA256": hashlib.sha256(model_input.encode()).hexdigest(),
        "referenceInputTokens": len(packed["inputIDs"]),
        "wholeStringReferenceTokens": len(packer.encode_plain_text(tokenizer, model_input)),
        "referenceTokenIDsSHA256": packer.token_ids_sha256(packed["inputIDs"]),
        "historyBeforePacking": len(ids), "retainedBlocks": blocks,
        "originalContextOrderPreserved": ids == example.get("contextBlockIDs", ids) if variant == "new" else None,
        "pipelineNativeOrderPreserved": True,
        "readRenderingCounts": packed["readRenderingCounts"],
    }


def request_plan(prompts, cohort, seed):
    by_key = {(p["exampleID"], p["variant"]): p for p in prompts}
    arms = list(itertools.product(VARIANTS, MODELS))
    order = sorted(cohort, key=lambda e: fingerprint([seed, "execution", e["exampleID"]]))
    schedule = []
    # Balanced cyclic order: each arm occupies every position equally at n=100.
    for index, example in enumerate(order):
        rotated = arms[index % 4:] + arms[:index % 4]
        for variant, model in rotated:
            p = by_key[(example["exampleID"], variant)]
            request = {"model": model, "input": [{"role": "user", "content": [{
                "type": "input_text", "text": p["modelInput"]}]}],
                "reasoning": {"effort": "xhigh"}, "tools": [], "stream": True}
            schedule.append({"requestOrdinal": len(schedule), "exampleID": example["exampleID"],
                             "promptID": p["promptID"], "variant": variant, "model": model,
                             "reasoningEffort": "xhigh", "requestSHA256": fingerprint(request),
                             "status": "planned_not_sent"})
    return schedule


def audit_records(cohort, prompts, requests, privacy, budget):
    examples = {r["exampleID"]: r for r in cohort}
    assert len(examples) == len(cohort)
    assert len(prompts) == 2 * len(cohort)
    assert len(requests) == 4 * len(cohort)
    seen = set()
    for p in prompts:
        e = examples[p["exampleID"]]
        assert e["targetSHA256"] == fingerprint(e["target"])
        assert e["querySHA256"] == fingerprint(e["query"])
        assert e["targetText"] == target_text(e["target"])
        key = (p["exampleID"], p["variant"])
        assert key not in seen
        seen.add(key)
        assert p["query"] == e["query"]
        assert p["modelInput"].endswith(e["query"])
        history_and_query = "".join(b["serialized"] + "\n" for b in p["retainedBlocks"]) + e["query"]
        assert p["modelInput"].endswith(history_and_query)
        assert p["modelInputSHA256"] == hashlib.sha256(p["modelInput"].encode()).hexdigest()
        assert p["referenceInputTokens"] <= budget
        assert not privacy.unsafe(p["modelInput"])
        assert not privacy.unsafe(e["query"] + canonical(e["target"]))
        for b in p["retainedBlocks"]:
            assert b["availableAt"] < e["targetBeganAt"]
            assert b["eventID"] not in {e["targetEventID"], *e["episode"]["memberWriteEventIDs"]}
            assert b["serializedSHA256"] == hashlib.sha256(b["serialized"].encode()).hexdigest()
    assert seen == set(itertools.product(examples, VARIANTS))
    assert len({(r["exampleID"], r["variant"], r["model"]) for r in requests}) == len(requests)
    assert requests == request_plan(prompts, cohort, 17)
    return {"status": "passed", "examples": len(cohort), "distinctPrompts": len(prompts),
            "plannedRequests": len(requests), "personalProviderCalls": 0,
            "maxReferenceInputTokens": max(p["referenceInputTokens"] for p in prompts),
            "queryTargetAndHistoryPrivacyScan": "passed_known_payload_and_high_specificity_patterns",
            "causalCutoffAndMemberExclusion": "passed", "modelArmsShareExactInputs": True}
