#!/usr/bin/env python3
"""Exercise production episode grouping and classification on synthetic trajectories."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


def load_reducer():
    path = Path(__file__).with_name("construct-phase1-raw-episode-corpus.py")
    spec = importlib.util.spec_from_file_location("episode_navigation_reducer", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


REDUCER = load_reducer()
PREFIX = "Please review the latest changes in the implementation"
SUFFIX = " and explain the consequences."


def stamp(second):
    return f"2026-01-01T00:00:{second:02d}.000Z"


class ProjectionSink:
    """Capture real grouping/classification; full projection is tested by corpus replay."""
    def construct(self, corpus, decisions, candidates, output):
        self.decisions = REDUCER.load_jsonl(decisions)
        self.candidates = [row for path in candidates for row in REDUCER.load_jsonl(path)]
        output.mkdir()
        REDUCER.write_jsonl(output / "episode-adjudications.jsonl", self.decisions)
        artifact = {"source": {}, "eligibility": {}, "artifactDigestsSHA256": {}}
        (output / "corpus.json").write_text(json.dumps(artifact))
        return artifact


def scenario(afters=None, *, boundaries=None, reads=(), befores=None,
             destinations=None, hints=None, prompt=True, starts=None):
    afters = afters or [PREFIX, PREFIX + SUFFIX]
    count = len(afters)
    boundaries = boundaries or ["selection_navigation"] * (count - 1) + ["return_pressed"]
    befores = befores or [""] + afters[:-1]
    starts = starts or [1 + 9 * i for i in range(count)]
    destinations = destinations or ["field-a"] * count
    hints = hints or [["typed"] for _ in afters]
    events, primitives, raw = [], [], []
    app = "Visual Studio Code" if prompt else "Obsidian"
    for i, (before, after) in enumerate(zip(befores, afters)):
        eid, rid = f"write-{i}", f"raw-write-{i}"
        destination = {"application": app, "surfaceKind": "integrated_terminal" if prompt else "note_editor"}
        conditioning = {
            "captureSemantics": "synchronous_before_application_mutation",
            "capturedAt": stamp(starts[i]),
            "cursorContext": {"leftContext": before, "selectedText": "", "rightContext": ""},
            "destination": destination,
        }
        edit = REDUCER.minimal_edit(before, after)
        member = {
            "writeEventID": eid, "sourceRecordIDs": [rid], "rawPath": "raw.jsonl",
            "rawLine": i+1, "beganAt": stamp(starts[i]), "availableAt": stamp(starts[i]+1),
            "boundaryReason": boundaries[i], "inputHints": hints[i],
            "beforeLogicalValue": before, "selectedTerminalLogicalValue": after,
            "logicalDestinationKey": destinations[i], "modelFacingDestination": destination,
            "conditioningState": conditioning, "targetIdentity": {
                "bundleIdentifier": "com.microsoft.VSCode" if prompt else "md.obsidian",
                "processIdentifier": 1, "elementHash": 1, "role": "AXTextField" if prompt else "AXTextArea",
                "windowTitle": "Fixture", "fieldDescription": "Terminal 1" if prompt else "",
            },
            **edit,
            "currentTarget": {"resolvedContent": edit["content"], "segments": [
                {"type": "authored_text", "content": edit["content"]}
            ]},
        }
        raw.append({"recordID": rid, "inputHints": hints[i]})
        primitives.append({"beganAt": member["beganAt"], "memberWriteEventIDs": [eid], "members": [member]})
        events.append({"sourceEventID": eid, "kind": "write", "sessionID": "session",
                       "sourceRecordIDs": [rid], "beganAt": member["beganAt"], "availableAt": member["availableAt"],
                       "serialized": json.dumps({"kind": "write", "content": edit["content"]})})
    for i, (second, text) in enumerate(reads):
        rid = f"raw-read-{i}"
        raw.append({"recordID": rid})
        events.append({"sourceEventID": f"read-{i}", "kind": "read", "sessionID": "session",
                       "sourceRecordIDs": [rid], "availableAt": stamp(second),
                       "serialized": json.dumps({"kind": "read", "source": {"application": "Browser"}, "content": text})})
    with tempfile.TemporaryDirectory(prefix="episode-navigation-check-") as directory:
        root = Path(directory)
        corpus, evidence = root / "corpus", root / "primitives"
        corpus.mkdir(); evidence.mkdir()
        REDUCER.write_jsonl(root / "raw.jsonl", raw)
        REDUCER.write_jsonl(corpus / "events.jsonl", events)
        (corpus / "corpus.json").write_text(json.dumps({
            "corpusID": "fixture", "conversionVersion": "phase1-causal-v16",
            "writeDestination": {"configuredTerminalAgentProgramMappings": {}},
        }))
        REDUCER.write_jsonl(evidence / "episode-candidates.jsonl", primitives)
        (evidence / "episode-review.json").write_text(json.dumps({"source": {
            "corpusID": "fixture", "eventsSHA256": REDUCER.sha256(corpus / "events.jsonl"),
        }}))
        sink = ProjectionSink()
        REDUCER.assemble(corpus, evidence, root / "output", root, sink)
        return sink


class NavigationTests(unittest.TestCase):
    def test_navigation_then_append_is_one_loss_target(self):
        result = scenario()
        self.assertEqual(len(result.candidates), 1)
        c, d = result.candidates[0], result.decisions[0]
        self.assertEqual(d["decision"], "closed_loss_episode")
        self.assertEqual(d["finalizedTarget"]["resolvedContent"], PREFIX + SUFFIX)
        self.assertEqual(c["beganAt"], stamp(1))
        self.assertEqual(c["candidateAvailableAt"], stamp(11))
        self.assertEqual(c["initialConditioningState"]["cursorContext"]["leftContext"], "")

    def test_new_read_preserves_split_and_new_onset(self):
        result = scenario(reads=[(5, "A newly arrived substantive response.")])
        self.assertEqual(len(result.candidates), 2)
        self.assertEqual(result.candidates[0]["episodeStateMachine"]["closeReason"], "novel_read")
        self.assertEqual(result.decisions[0]["finalizedTarget"]["resolvedContent"], PREFIX)
        self.assertEqual(result.decisions[1]["finalizedTarget"]["resolvedContent"], SUFFIX)
        self.assertTrue(all(d["decision"] == "closed_loss_episode" for d in result.decisions))

    def test_read_known_before_onset_does_not_split(self):
        result = scenario(reads=[(0, "Already available information."), (5, "Already available information.")])
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0]["causalEvidence"]["novelInterveningReadCount"], 0)

    def test_new_then_repeated_read_still_splits(self):
        result = scenario(reads=[(4, "New information."), (5, "New information.")])
        self.assertEqual(len(result.candidates), 2)

    def test_merge_before_later_new_read_only(self):
        result = scenario([PREFIX, PREFIX + SUFFIX, PREFIX + SUFFIX + " Another question."],
                          reads=[(15, "New information between the second and third writes.")])
        self.assertEqual([len(c["members"]) for c in result.candidates], [2, 1])
        self.assertEqual(result.decisions[0]["finalizedTarget"]["resolvedContent"], PREFIX + SUFFIX)
        self.assertEqual(result.candidates[1]["beganAt"], stamp(19))

    def test_navigation_gap_is_not_a_new_information_boundary(self):
        self.assertEqual(len(scenario(starts=[1, 40]).candidates), 1)

    def test_middle_revision_is_final_state_not_concatenation(self):
        corrected = PREFIX.replace("latest", "reviewed")
        result = scenario([PREFIX, corrected, corrected + SUFFIX])
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.decisions[0]["finalizedTarget"]["resolvedContent"], corrected + SUFFIX)

    def test_submission_still_splits(self):
        self.assertEqual(len(scenario(boundaries=["return_pressed", "return_pressed"]).candidates), 2)

    def test_different_destination_still_splits(self):
        self.assertEqual(len(scenario(destinations=["field-a", "field-b"]).candidates), 2)

    def test_broken_state_still_splits(self):
        result = scenario(befores=["", "Unrelated prefilled text"], afters=[PREFIX, "Unrelated prefilled text plus new words"])
        self.assertEqual(len(result.candidates), 2)
        self.assertNotEqual(result.decisions[1]["decision"], "closed_loss_episode")

    def test_unresolved_paste_remains_history_only(self):
        result = scenario(hints=[["typed", "paste"], ["typed"]])
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.decisions[0]["decision"], "closed_history_episode")
        self.assertEqual(result.decisions[0]["reason"], "paste_authorship_unresolved")

    def test_distinct_note_regions_still_split(self):
        base = "Existing document paragraph.\n\n"
        first = base + PREFIX
        result = scenario([first, first.replace("Existing", "Earlier")],
                          befores=[base, first], boundaries=["selection_navigation", "write_delay_elapsed"], prompt=False)
        self.assertEqual(len(result.candidates), 2)


if __name__ == "__main__":
    unittest.main()
