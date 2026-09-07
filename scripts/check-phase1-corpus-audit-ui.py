#!/usr/bin/env python3
"""Check exact packed-context review and nested artifact provenance."""

import importlib.util
import json
import tempfile
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "corpus_audit_ui", Path(__file__).with_name("serve-phase1-corpus-audit.py")
)
assert spec and spec.loader
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)

for version in (2, 7):
    for fallback in (True, False):
        event = {"readSurface": {"ruleVersion": f"ax-pane-read-v{version}", "surfaceSelection": {"isV1Fallback": fallback}}}
        assert ui.classify_read_surface(event)[0] == ("ax_fallback" if fallback else "ax_tree")
assert ui.classify_read_surface({})[0] == "legacy"

with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary).resolve()
    paths = {name: root / name for name in ("micro", "causal", "reduced", "episodes", "packed")}
    for path in paths.values():
        path.mkdir()

    def write(path, value):
        path.write_text(json.dumps(value) + "\n")

    def digest(path):
        return ui.sha256(path)

    semantic = {"kind": "read", "sessionID": "session", "eventID": "read", "readSurface": {"ruleVersion": "ax-pane-read-v7", "surfaceSelection": {"isV1Fallback": False, "method": "ax_semantic_container"}}}
    write(paths["reduced"] / "events.jsonl", semantic)
    write(paths["reduced"] / "reduction.json", {})
    write(paths["causal"] / "dataset.json", {"source": {"digestsSHA256": {"events.jsonl": digest(paths["reduced"] / "events.jsonl")}}})
    write(paths["micro"] / "corpus.json", {"sources": [{"sessionID": "session", "digestsSHA256": {"dataset.json": digest(paths["causal"] / "dataset.json")}}]})
    serialized = json.dumps({"kind": "read", "content": "old and new"})
    write(paths["episodes"] / "events.jsonl", {"sourceEventID": "read", "kind": "read", "serialized": serialized, "readSourceDerivation": {"category": "schema7_ax_selected"}})
    write(paths["episodes"] / "context-blocks.jsonl", {"contextBlockID": "read", "serialized": serialized, "availableAt": "2026-09-02T12:00:00.000Z"})
    example = {"exampleID": "example", "chronologicalOrdinal": 0, "sessionID": "session", "modelFacingDestination": {"application": "Code"}, "conditioningState": {}, "query": "{}", "contextBlockIDs": ["read"], "target": {"segments": [{"type": "authored_text", "content": "Please review "}, {"type": "paste"}]}, "targetMetadata": {"microWriteCount": 3}}
    write(paths["episodes"] / "examples.jsonl", example)
    corpus_digests = {name: digest(paths["episodes"] / name) for name in ("events.jsonl", "examples.jsonl", "context-blocks.jsonl")}
    write(paths["episodes"] / "corpus.json", {"source": {"path": str(paths["micro"])}, "sessionID": "corpus", "artifactDigestsSHA256": corpus_digests})
    plan = {"exampleID": "example", "qwenModelInputTokenCount": 10, "retainedContextBlocks": [{"contextBlockID": "read", "serializedOverride": json.dumps({"kind": "read", "content": "new"})}]}
    write(paths["packed"] / "context-plans.jsonl", plan)
    packing = {"source": {"sessionID": "corpus", "digestsSHA256": corpus_digests}, "artifactDigestsSHA256": {"context-plans.jsonl": digest(paths["packed"] / "context-plans.jsonl")}}
    write(paths["packed"] / "packing.json", packing)

    store = ui.AuditStore(project=root, corpus=paths["episodes"], packed=paths["packed"], sample_size=0, seed=17, artifact_root=root)
    assert store.sample_method == "complete loss-bearing corpus"
    assert store.event_read_counts == {"ax_tree": 1}
    detail = store.detail("example")
    assert detail["retainedEvents"][0]["projection"]["content"] == "new"
    assert detail["retainedEvents"][0]["readProvenance"]["ruleVersion"] == "ax-pane-read-v7"
    assert detail["targetText"] == "Please review <|paste|>"
    assert isinstance(store.plan_by_id, ui.JSONLIndex)
    assert store.plan_by_id.get("missing") is None

    packing["source"]["digestsSHA256"]["events.jsonl"] = "wrong"
    write(paths["packed"] / "packing.json", packing)
    try:
        ui.AuditStore(project=root, corpus=paths["episodes"], packed=paths["packed"], sample_size=0, seed=17, artifact_root=root)
        raise AssertionError("misbound pack accepted")
    except ui.AuditError as error:
        assert "not bound" in str(error)

    duplicate = root / "duplicate.jsonl"
    duplicate.write_text('{"id":"é"}\n{"id":"é"}\n')
    try:
        ui.JSONLIndex(duplicate, "id")
        raise AssertionError("duplicate plan accepted")
    except ui.AuditError as error:
        assert "duplicate" in str(error)

print("Phase 1 corpus audit UI checks passed")
