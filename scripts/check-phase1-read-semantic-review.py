#!/usr/bin/env python3
"""Regression checks for the semantic READ review projection."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import weakref
from pathlib import Path


scripts = Path(__file__).resolve().parent
sys.path.insert(0, str(scripts))
spec = importlib.util.spec_from_file_location(
    "phase1_read_semantic_review",
    scripts / "serve-phase1-read-semantic-review.py",
)
assert spec is not None and spec.loader is not None
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)

for decision in review.EMPTY_NOVELTY_DECISIONS:
    content, label, suppressed = review.review_projection(
        {"decision": decision, "content": ""}, "OCR THAT MUST NOT APPEAR",
    )
    assert content == "[No new READ text]", decision
    assert label.startswith("No newly available text"), decision
    assert suppressed is True, decision
    assert review.novelty_projection_changed(decision), decision

for decision in review.POSITIVE_NOVELTY_DECISIONS:
    content, label, suppressed = review.review_projection(
        {
            "decision": decision,
            "content": "new material",
            "lineAlignment": {"matchedLineCount": 2},
        },
        "complete OCR",
    )
    assert content == "new material", decision
    assert "retained" in label, decision
    if decision not in {
        "emit_stable_interior_after_clipped_boundary", "emit_scroll_new_edge",
    }:
        assert "2 repeated lines removed" in label, decision
    assert suppressed is False, decision
    assert review.novelty_projection_changed(decision), decision

content, label, suppressed = review.review_projection(
    {"decision": "full_state", "content": "complete OCR"}, "complete OCR",
)
assert content == "complete OCR"
assert label == "Full READ retained · substantial/new state"
assert suppressed is False
assert not review.novelty_projection_changed("full_state")

content, label, suppressed = review.review_projection(
    {
        "decision": "full_state",
        "reason": "candidate_alignment_unproven_preserve_complete_state",
        "content": "complete OCR",
    },
    "complete OCR",
)
assert content == "complete OCR"
assert label == "Full READ retained · safe delta unproven"
assert suppressed is False

for decision in review.MODEL_REWRITTEN_RENDER_DECISIONS:
    assert review.model_rendering_changed(decision), decision
for decision in review.MODEL_FALLBACK_RENDER_DECISIONS:
    assert not review.model_rendering_changed(decision), decision

# The browser consumes the server-authoritative projection and label instead of
# maintaining a second, drift-prone decision table.
assert "return r.reviewProjectedContent||'[No new READ text]'" in review.HTML
assert "return r.reviewOutcomeLabel||'READ projection unavailable'" in review.HTML
assert "r.novelty.decision" not in review.HTML

# Multi-day routing keeps only the chosen expanded store, even during a load.
class StubStore:
    def __init__(self, name):
        self.summary = {"day": name}
        self.by_id = {"same-id": {"day": name}}
        self.images = {}

    def index(self):
        return [{"id": "same-id", "day": self.summary["day"]}]


loaded = []
weak_stores = []


def make_store(name):
    assert all(reference() is None for reference in weak_stores)
    result = StubStore(name)
    loaded.append(name)
    weak_stores.append(weakref.ref(result))
    return result


days = review.ReviewSessions([
    {"id": "sep2", "label": "September 2", "arguments": ("sep2",)},
    {"id": "sep3", "label": "September 3", "arguments": ("sep3",)},
], factory=make_store)
assert loaded == []
assert days.get().summary == {"day": "sep2"}
assert days.get("sep2").summary == {"day": "sep2"}
assert loaded == ["sep2"]
assert days.get("sep3").summary == {"day": "sep3"}
assert loaded == ["sep2", "sep3"]


def request(path):
    result = []
    h = review.handler(days).__new__(review.handler(days))
    h.path = path
    h.send_body = lambda body, kind, status=200: result.append((status, json.loads(body)))
    h.send_error = lambda status: result.append((status, None))
    h.do_GET()
    return result[0]


assert request("/api/read?session=sep2&id=same-id") == (200, {"day": "sep2"})
assert request("/api/read?session=sep3&id=same-id") == (200, {"day": "sep3"})
assert request("/api/sessions")[1] == days.index()
assert request("/api/read?session=missing&id=same-id")[0] == 409
assert loaded == ["sep2", "sep3", "sep2", "sep3"]

with tempfile.TemporaryDirectory() as temporary:
    directory = Path(temporary).resolve()
    raw = directory / "raw.jsonl"
    raw.write_text("\n".join(json.dumps(value) for value in [
        {"recordID": "write", "recordType": "active_tap_write_attempt", "before": {"value": "private large editor"}},
        {"recordID": "ocr", "recordType": "screen_ocr_observation", "content": "visible", "accessibilityTree": ["large tree"]},
        {"recordID": "frame", "recordType": "visual_frame_observation", "screenshotRelativePath": "images/test.png", "surface": {"windowBounds": {"x": 1}, "accessibilityTree": ["large tree"]}},
    ]) + "\n")
    raw_index = review.raw_review_index(raw)
    assert set(raw_index) == {"ocr", "frame"}
    assert raw_index["ocr"]["content"] == "visible"
    assert "accessibilityTree" not in raw_index["ocr"]
    assert raw_index["frame"]["surface"] == {"windowBounds": {"x": 1}}

    manifest = directory / "review.json"
    manifest.write_text(json.dumps({
        "compiled": "shared-episodes", "packed": "shared-pack",
        "sessions": [{"id": "sep2", "label": "September 2", "baseline": "baseline", "candidate": "candidate", "source": "raw", "readSurfaceEvidence": "surfaces"}],
    }))
    configured = review.ReviewSessions.from_manifest(manifest)
    args = configured.configurations["sep2"]["arguments"]
    assert args[0] == directory / "baseline"
    assert args[-2:] == (directory / "shared-episodes", directory / "shared-pack")
    assert configured.cached_store is None
    value = json.loads(manifest.read_text())
    value["sessions"].append(value["sessions"][0])
    manifest.write_text(json.dumps(value))
    try:
        review.ReviewSessions.from_manifest(manifest)
        raise AssertionError("duplicate day accepted")
    except review.ReviewError as error:
        assert "duplicate" in str(error)

    # Each day binds its exact raw/reduced/evidence hashes, while both days use
    # one shared compiled corpus and pack. Occurrences must not cross sessions.
    compiled = directory / "compiled"
    packed = directory / "packed"
    compiled.mkdir()
    packed.mkdir()
    event_rows = []
    plans = []
    day_arguments = []

    def write_json(path, value):
        path.write_text(json.dumps(value))

    for day in ("a", "b"):
        source, baseline, candidate, surfaces = [directory / f"{day}-{kind}" for kind in ("source", "baseline", "candidate", "surfaces")]
        for path in (source, baseline, candidate, surfaces):
            path.mkdir()
        raw_record = {"recordID": f"raw-{day}", "recordType": "screen_ocr_observation", "content": day, "capturedAt": "2026-09-02T12:00:00.000Z"}
        write_json(source / "raw.jsonl", raw_record)
        write_json(surfaces / "read-surfaces.jsonl", {"sourceRecordID": f"raw-{day}", "content": day, "surfaceSelection": {}})
        (surfaces / "unresolved.jsonl").write_text("")
        surface_hash = review.sha256(surfaces / "read-surfaces.jsonl")
        write_json(surfaces / "read-surface-evidence.json", {"artifacts": {"digestsSHA256": {"read-surfaces.jsonl": surface_hash}}})
        event = {"eventID": f"event-{day}", "kind": "read", "sessionID": day, "sourceRecordIDs": [f"raw-{day}"], "sequence": 1, "content": day, "capturedAt": raw_record["capturedAt"]}
        for path, version in ((baseline, "phase1-semantic-v14"), (candidate, "phase1-semantic-v23")):
            write_json(path / "events.jsonl", event)
            (path / "unresolved.jsonl").write_text("")
            write_json(path / "reduction.json", {"sessionID": day, "reducerVersion": version, "source": {"digestsSHA256": {"raw.jsonl": review.sha256(source / "raw.jsonl")}, "readSurfaceEvidence": {"readSurfacesSHA256": surface_hash}}, "artifacts": {"digestsSHA256": {name: review.sha256(path / name) for name in ("events.jsonl", "unresolved.jsonl")}}})
        event_rows.append({"sourceEventID": event["eventID"], "sessionID": day, "serialized": json.dumps({"kind": "read", "content": day})})
        plans.append({"exampleID": f"example-{day}", "retainedContextBlocks": [{"contextBlockID": event["eventID"], "readRendering": {"decision": "render_complete_state"}}]})
        day_arguments.append((baseline, None, candidate, surfaces, source, compiled, packed))
    (compiled / "events.jsonl").write_text("\n".join(json.dumps(row) for row in event_rows))
    (packed / "context-plans.jsonl").write_text("\n".join(json.dumps(row) for row in plans))
    write_json(packed / "packing.json", {"packerVersion": "phase1-token-pack-v12", "packing": {"readNoveltyRendering": {"status": "shadow_opt_in"}}, "source": {"digestsSHA256": {"events.jsonl": review.sha256(compiled / "events.jsonl")}}, "artifactDigestsSHA256": {"context-plans.jsonl": review.sha256(packed / "context-plans.jsonl")}})
    for day, args in zip(("a", "b"), day_arguments):
        store = review.ReviewStore(*args)
        assert store.by_id[f"event-{day}"]["modelOccurrences"][0]["modelFacingContent"] == day
        assert len(store.by_id) == 1

    # A consolidated READ must preserve every original preview observation,
    # even though none of those live-preview rows has an eventID.
    args = day_arguments[1]
    preview_path = args[4] / "events.preview.jsonl"
    preview_rows = [
        {"sessionID": "b", "kind": "read", "sequence": sequence,
         "sourceRecordIDs": [source_id], "content": content}
        for sequence, source_id, content in (
            (11, "raw-b", "initial preview"),
            (12, "raw-b2", "later preview"),
        )
    ]
    preview_path.write_text("\n".join(json.dumps(row) for row in preview_rows))
    raw_path = args[4] / "raw.jsonl"
    raw_path.write_text(raw_path.read_text() + "\n" + json.dumps({"recordID": "raw-b2", "recordType": "screen_ocr_observation", "content": "later preview"}))
    candidate_event_path = args[2] / "events.jsonl"
    candidate_event = json.loads(candidate_event_path.read_text())
    candidate_event["sourceRecordIDs"].append("raw-b2")
    write_json(candidate_event_path, candidate_event)
    candidate_manifest_path = args[2] / "reduction.json"
    candidate_manifest = json.loads(candidate_manifest_path.read_text())
    candidate_manifest["source"]["digestsSHA256"]["raw.jsonl"] = review.sha256(raw_path)
    candidate_manifest["artifacts"]["digestsSHA256"]["events.jsonl"] = review.sha256(candidate_event_path)
    write_json(candidate_manifest_path, candidate_manifest)
    preview_store = review.ReviewStore(None, preview_path, *args[2:])
    preview_row = preview_store.by_id["event-b"]
    assert preview_row["baselineSequences"] == [11, 12]
    assert [row["content"] for row in preview_row["baselineReads"]] == ["initial preview", "later preview"]
    assert preview_row["baselineKind"] == "collector_preview"
    assert "not the original pane-v2 review" in preview_row["baselineDescription"]
    assert preview_store.summary["baselineVersion"] == "collector-preview"
    assert review.baseline_event_key(preview_rows[0]) != review.baseline_event_key(preview_rows[1])

    # The frame may precede its OCR in raw lineage. Keep one image for that
    # exact frame, but bind the OCR's pane evidence and retain both source IDs.
    (args[4] / "screenshot.png").write_bytes(b"sanitized screenshot fixture")
    frame = {"recordID": "frame-b", "recordType": "visual_frame_observation",
             "screenshotRelativePath": "screenshot.png",
             "screenshotSHA256": review.sha256(args[4] / "screenshot.png"),
             "capturedAt": "2026-09-02T12:00:00.000Z"}
    ocr = {"recordID": "ocr-b", "recordType": "visual_ocr_observation",
           "sourceFrameRecordID": "frame-b", "capturedAt": frame["capturedAt"]}
    raw_path.write_text(raw_path.read_text() + "\n" + json.dumps(frame)
                        + "\n" + json.dumps(ocr))
    candidate_event["sourceRecordIDs"].extend(["frame-b", "ocr-b"])
    write_json(candidate_event_path, candidate_event)
    surfaces_path = args[3] / "read-surfaces.jsonl"
    surfaces_path.write_text(surfaces_path.read_text() + "\n" + json.dumps({
        "sourceRecordID": "ocr-b", "content": "visible fixture",
        "surfaceSelection": {"regionOfInterest": {
            "x": 0.1, "y": 0.2, "width": 0.8, "height": 0.6,
        }},
    }))
    surface_hash = review.sha256(surfaces_path)
    write_json(args[3] / "read-surface-evidence.json", {
        "artifacts": {"digestsSHA256": {"read-surfaces.jsonl": surface_hash}},
    })
    candidate_manifest["source"]["digestsSHA256"]["raw.jsonl"] = review.sha256(raw_path)
    candidate_manifest["source"]["readSurfaceEvidence"]["readSurfacesSHA256"] = surface_hash
    candidate_manifest["artifacts"]["digestsSHA256"]["events.jsonl"] = review.sha256(candidate_event_path)
    write_json(candidate_manifest_path, candidate_manifest)
    framed = review.ReviewStore(None, preview_path, *args[2:]).by_id["event-b"]
    assert len(framed["images"]) == 1
    shown = framed["images"][0]
    assert shown["sourceRecordID"] == "ocr-b"
    assert shown["sourceFrameRecordID"] == "frame-b"
    assert shown["sourceRecordIDs"] == ["frame-b", "ocr-b"]
    assert shown["recordType"] == "visual_ocr_observation"
    pane = next(row for row in framed["paneEvidence"] if row["sourceRecordID"] == "ocr-b")
    assert pane["imageKey"] == shown["key"]
    assert pane["pane"]["width"] == 0.8

    # Equal PNG bytes in a different capture do not prove equal pane/attention
    # evidence; only the frame-to-OCR identity is a safe UI image join.
    frame2 = {**frame, "recordID": "frame-b2"}
    ocr2 = {**ocr, "recordID": "ocr-b2", "sourceFrameRecordID": "frame-b2"}
    raw_path.write_text(raw_path.read_text() + "\n" + json.dumps(frame2)
                        + "\n" + json.dumps(ocr2))
    candidate_event["sourceRecordIDs"].extend(["frame-b2", "ocr-b2"])
    write_json(candidate_event_path, candidate_event)
    candidate_manifest["source"]["digestsSHA256"]["raw.jsonl"] = review.sha256(raw_path)
    candidate_manifest["artifacts"]["digestsSHA256"]["events.jsonl"] = review.sha256(candidate_event_path)
    write_json(candidate_manifest_path, candidate_manifest)
    repeated = review.ReviewStore(None, preview_path, *args[2:]).by_id["event-b"]
    assert len(repeated["images"]) == 2
    assert {item["sourceFrameRecordID"] for item in repeated["images"]} == {"frame-b", "frame-b2"}

    (day_arguments[0][4] / "raw.jsonl").write_text("{}\n")
    try:
        review.ReviewStore(*day_arguments[0])
        raise AssertionError("tampered raw accepted by multi-session review")
    except review.ReviewError as error:
        assert "raw journal differ" in str(error)

assert "id=\"session\"" in review.HTML
assert "api('/api/read?id='" in review.HTML
assert "api('/api/image?id='" in review.HTML
assert "fetchJSON(api('/api/index'))" in review.HTML
assert "Before · ${esc(baselineTitle)}" in review.HTML
assert "Recorded live collector preview" in review.HTML
assert "Unable to load READ: " in review.HTML
assert "Requested READ was not found in this session: " in review.HTML

# A long passive chain must remain reviewable without decoding every retained
# screenshot at once. Selection changes only the displayed image, not lineage.
node = shutil.which("node")
if node:
    image_renderer = review.HTML.split("function imagePanels(", 1)[1].split(
        "function apply(){", 1,
    )[0]
    javascript = r'''
const assert=require('node:assert/strict');
const esc=x=>String(x??'');
const api=x=>x;
const rect=()=>'';
const point=()=>'';
const samePoint=()=>false;
const paneSummary=()=>'';
''' + "function imagePanels(" + image_renderer + r'''
const row={images:Array.from({length:73},(_,i)=>({key:`image-${i}`,capturedAt:`time-${i}`})),paneEvidence:[]};
const final=imagePanels(row);
assert.equal((final.match(/<img /g)||[]).length,1);
assert.equal((final.match(/<option /g)||[]).length,73);
assert.ok(final.includes('id=image-72'));
assert.ok(!final.includes('src="/api/image?id=image-0"'));
assert.ok(imagePanels(row,0).includes('src="/api/image?id=image-0"'));
assert.equal(imagePanels({images:[]}), '');
'''
    subprocess.run([node], input=javascript, text=True, check=True,
                   capture_output=True)

print("Phase 1 semantic READ review checks passed")
