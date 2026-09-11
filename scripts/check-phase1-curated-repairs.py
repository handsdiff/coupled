#!/usr/bin/env python3
"""No-network regression tests for reviewed, localized corpus curation."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), Path(__file__).with_name(name + ".py"))
    value = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = value
    spec.loader.exec_module(value)
    return value


h = load("curate-phase1-episode-joins")
raw = load("construct-phase1-raw-episode-corpus")
recovery = load("recover-phase1-reviewed-submissions")
assembly = load("assemble-phase1-curated-review")
onsets = load("restore-phase1-prompt-onsets")


def rejects(fn):
    try:
        fn()
    except (ValueError, KeyError):
        return
    raise AssertionError("invalid evidence accepted")


def candidate(eid, before, after, onset, available):
    member = {"writeEventID": eid, "beforeLogicalValue": before,
              "selectedTerminalLogicalValue": after, "logicalDestinationKey": "same-field"}
    return {"beganAt": onset, "candidateAvailableAt": available, "members": [member],
            "memberWriteEventIDs": [eid], "continuityEvidence": {"continuousReplayableState": True},
            "surfaceEvidence": {"logicalEditableIdentityStable": True},
            "closureEvidence": {"objectiveSubmissionBoundary": False}}


left = candidate("a", "", "befoe", "01", "02")
right = candidate("b", "befoe", "before after", "03", "04")
evidence = {"proofNowComplete": True, "onset": "01", "nextOnset": "03",
            "newAssessments": [{"status": "self_derived_active_composition_read"}]}
events = {k: {"kind": "write", "sessionID": "s", "sourceEventID": k,
               "beganAt": v["beganAt"], "availableAt": v["candidateAvailableAt"]} for k, v in (("a", left), ("b", right))}
h.require_join(left, right, evidence, raw, events)
for status in ("novel_causally_available_read", "unresolved_read_novelty"):
    bad = deepcopy(evidence); bad["newAssessments"][0]["status"] = status
    rejects(lambda: h.require_join(left, right, bad, raw, events))
bad = deepcopy(right); bad["members"][0]["beforeLogicalValue"] = "unrelated"
rejects(lambda: h.require_join(left, bad, evidence, raw, events))
bad = deepcopy(right); bad["members"][0]["logicalDestinationKey"] = "other-field"
rejects(lambda: h.require_join(left, bad, evidence, raw, events))
bad = deepcopy(left); bad["closureEvidence"]["objectiveSubmissionBoundary"] = True
rejects(lambda: h.require_join(bad, right, evidence, raw, events))
outside = {**events, "c": {"kind": "write", "sessionID": "s", "sourceEventID": "c", "beganAt": "02", "availableAt": "03"}}
rejects(lambda: h.require_join(left, right, evidence, raw, outside))
# Local state reconstruction removes intermediate typos; it never concatenates.
episode = raw.OpenEpisode([{"members": left["members"]}, {"members": right["members"]}], "session_start")
target, _, _ = raw.structured_target(episode, "", "before after", {})
assert target["resolvedContent"] == "before after"
assert "befoe" not in target["resolvedContent"]

# Every boundary of a longer composition must be proven independently, and
# its knowledge witness must precede the FIRST member, not just the last pair.
assert h.contiguous_join_groups([5, 2, 1, 2, 7, 8]) == [[1, 2], [5], [7, 8]]
third = candidate("c", "before after", "before after done", "05", "06")
left["members"][0]["sourceRecordID"] = "raw-a"
chain_reviews = {i: {"observations": [{"capturedAt": "00"}]} for i in (1, 2)}
h.require_chain_onset([1, 2], [left, right, third], chain_reviews, {})
rejects(lambda: h.require_chain_onset([1, 2], [left, right, third], {1: chain_reviews[1]}, {}))
bad = deepcopy(chain_reviews); bad[2]["observations"][0]["capturedAt"] = "02"
rejects(lambda: h.require_chain_onset([1, 2], [left, right, third], bad, {}))
rejects(lambda: h.require_chain_onset([1, 2], [left, right, third], chain_reviews, {2: {}}))
chain_events = {**events, "c": {"kind": "write", "sessionID": "s", "sourceEventID": "c", "beganAt": "05", "availableAt": "06"}}
edge2 = {**deepcopy(evidence), "onset": "03", "nextOnset": "05"}
h.require_join(left, right, evidence, raw, chain_events)
h.require_join(right, third, edge2, raw, chain_events)
bad = deepcopy(edge2); bad["newAssessments"][0]["status"] = "novel_causally_available_read"
rejects(lambda: h.require_join(right, third, bad, raw, chain_events))
chain = raw.OpenEpisode([{"members": c["members"]} for c in [left, right, third]], "session_start")
assert raw.structured_target(chain, "", "before after done", {})[0]["resolvedContent"] == "before after done"

text = "A complete observed prompt, long enough to be substantive."
obs = lambda value, at: {"value": value, "valueWasTruncated": False, "observedAt": at}
r = {"before": obs("Ask Gemini\n", "01"), "after": obs("Ask Gemini\n", "05"),
     "targetIdentity": {"bundleIdentifier": "com.google.Chrome", "role": "AXTextArea", "fieldDescription": "Enter a prompt for Gemini"},
     "conditioningState": {"cursorContext": {"fieldState": "unpopulated_prompt"}},
     "inputHints": ["typed", "return", "shortcut"],
     "inputEvents": [{"hint": "typed", "mutationCapable": True, "eventTimestampNanoseconds": 1},
                     {"hint": "return", "mutationCapable": True, "eventTimestampNanoseconds": 2}],
     "lastEventTimestampNanoseconds": 2,
     "returnCheckpoints": [{"eventTimestampNanoseconds": 2, "observation": obs(text, "04")}],
     "mutationCheckpoints": [{"eventTimestampNanoseconds": 1, "observation": obs(text, "03")}],
     "beforeAXErrors": [], "afterAXErrors": []}
assert recovery.prove(r)[0]["observation"]["value"] == text
for field, value in (("pasteCheckpoints", [{}]), ("tapTimeoutCountDuringBurst", 1), ("beforeAXErrors", ["invalid_ui_element"])):
    bad = deepcopy(r); bad[field] = value
    rejects(lambda: recovery.prove(bad))
bad = deepcopy(r); bad["before"]["value"] = "previous prompt"
rejects(lambda: recovery.prove(bad))
bad = deepcopy(r); bad["returnCheckpoints"][0]["observation"]["valueWasTruncated"] = True
rejects(lambda: recovery.prove(bad))
bad = deepcopy(r); bad["mutationCheckpoints"][0]["observation"]["value"] = "different final value"
rejects(lambda: recovery.prove(bad))

# Submitted native chat fields often invalidate after Return. The checkpoint,
# not that invalid terminal, must be the independently confirmed endpoint.
codex = deepcopy(r)
codex.update(after=None, afterAXErrors=["AXValue:invalid_ui_element"], terminalDecisionAt="05")
codex["before"]["value"] = "\nDo anything"
codex["targetIdentity"] = {"bundleIdentifier": "com.openai.codex", "role": "AXTextArea", "fieldDescription": "Do anything"}
assert recovery.prove(codex, True)[0]["observation"]["value"] == text
bad = deepcopy(codex); bad["afterAXErrors"] = ["AXValue:cannot_complete"]
rejects(lambda: recovery.prove(bad, True))
bad = deepcopy(codex); bad["terminalDecisionAt"] = "02"
rejects(lambda: recovery.prove(bad, True))
bad = deepcopy(codex); bad["before"]["value"] = "part of an earlier unsent prompt"
rejects(lambda: recovery.prove(bad, True))
interval = {"beganAt": "01", "terminalDecisionAt": "04"}
recovery.require_closed_interval(interval, [])
rejects(lambda: recovery.require_closed_interval(interval, [{"kind": "read", "availableAt": "03"}]))
rejects(lambda: recovery.require_closed_interval(interval, [{"kind": "write", "beganAt": "02", "availableAt": "05"}]))
rejects(lambda: h.apply_read_review(evidence, {"boundarySHA256": "wrong"}))

# A later clean screenshot cannot be substituted for pre-onset knowledge.
visual_boundary = {"onset": "02", "currentReads": [{"availableAt": "03", "sourceRecordIDs": ["later"]}]}
visual_raw = {"prior": {"capturedAt": "01", "screenshotSHA256": "p"},
              "later": {"capturedAt": "03", "screenshotSHA256": "q"}}
visual_review = {"screenshots": [{"sha256": "p"}, {"sha256": "q"}],
                 "observations": [{"recordID": k, **v} for k, v in visual_raw.items()]}
h.verify_read_observations(visual_boundary, visual_review, visual_raw)
bad = deepcopy(visual_review); bad["observations"] = bad["observations"][1:]
rejects(lambda: h.verify_read_observations(visual_boundary, bad, visual_raw))
bad = deepcopy(visual_review); bad["observations"] = bad["observations"][:1]
rejects(lambda: h.verify_read_observations(visual_boundary, bad, visual_raw))
bad = deepcopy(visual_raw); bad["prior"]["capturedAt"] = "04"
rejects(lambda: h.verify_read_observations(visual_boundary, visual_review, bad))
bad = deepcopy(visual_raw); bad["later"]["screenshotSHA256"] = "changed"
rejects(lambda: h.verify_read_observations(visual_boundary, visual_review, bad))

# Existing note text may instead be manually checked against the SAME episode's
# complete initial field. A later field or a changed value is not a witness.
initial_member = {"sourceRecordID": "field", "rawPath": "raw.jsonl"}
field_raw = {**visual_raw, "field": {"before": {"observationID": "before", "observedAt": "02", "value": "existing note"}}}
field_review = deepcopy(visual_review)
field_review["observations"] = field_review["observations"][1:]
field_review["initialFieldEvidence"] = {"recordID": "field", "rawPath": "raw.jsonl", "observationID": "before", "observedAt": "02",
                                      "valueSHA256": hashlib.sha256(b"existing note").hexdigest()}
h.verify_read_observations(visual_boundary, field_review, field_raw, initial_member)
rejects(lambda: h.verify_read_observations(visual_boundary, field_review, field_raw))
bad = deepcopy(field_review); bad["initialFieldEvidence"]["recordID"] = "later"
rejects(lambda: h.verify_read_observations(visual_boundary, bad, field_raw, initial_member))
bad = deepcopy(field_raw); bad["field"]["before"]["value"] = "changed"
rejects(lambda: h.verify_read_observations(visual_boundary, field_review, bad, initial_member))

# A rendered-text correction changes both target and history, never the query,
# and keeps the disagreeing AX value explicitly available for audit.
old_text, final_text = "hello old world tail", "hello corrected world"
query = "frozen initial query"
target_event = {"sourceEventID": "episode", "memberWriteEventIDs": ["micro"],
                "sourceRecordIDs": ["attempt"], "sessionID": "session", "availableAt": "05",
                "serialized": json.dumps({"kind": "write", "authorshipSegments": [{"type": "authored_text", "content": old_text}]}),
                "auditSerialized": "{}"}
target_example = {"targetEventID": "episode", "targetBeganAt": "01", "targetAvailableAt": "05", "query": query,
                  "target": {"resolvedContent": old_text, "segments": [{"type": "authored_text", "content": old_text}]}}
target_review = {"decision": "visually_verified_submitted_authored_text", "reviewedBy": "assistant_visual_evidence_review",
                 "targetEventID": "episode", "memberWriteEventIDs": ["micro"], "beganAt": "01", "originalAvailableAt": "05",
                 "querySHA256": hashlib.sha256(query.encode()).hexdigest(), "observedAXContent": old_text,
                 "resolvedContent": final_text, "attemptRecordIDs": ["attempt"], "availableAt": "06",
                 "observations": [{"recordID": "pre", "capturedAt": "03", "sha256": "image", "path": "fixture"},
                                  {"recordID": "post", "capturedAt": "06", "sha256": "image", "path": "fixture"}]}
target_raw = {"attempt": {"before": {"value": ""}, "inputHints": ["typed", "return"],
                          "inputEvents": [{"observedAt": "02", "mutationCapable": True}], "boundaryReason": "return_pressed",
                          "returnCheckpoints": [{"inputObservedAt": "04", "observation": {"value": old_text}}]},
              "pre": {"sessionID": "session", "capturedAt": "03", "screenshotSHA256": "image"},
              "post": {"sessionID": "session", "capturedAt": "06", "screenshotSHA256": "image"}}
original_sha = assembly.h.sha
assembly.h.sha = lambda path: "image"
try:
    fixed_event, fixed_example = assembly.repair_visual_target(target_event, target_example, target_review, target_raw)
    assert fixed_example["query"] == query and fixed_example["target"]["resolvedContent"] == final_text
    assert json.loads(fixed_event["serialized"])["authorshipSegments"] == fixed_example["target"]["segments"]
    assert json.loads(fixed_event["auditSerialized"])["observedAXCompletion"] == old_text
    assert target_example["target"]["resolvedContent"] == old_text
    for patch in ({"availableAt": "03"}, {"querySHA256": "wrong"}, {"observedAXContent": "different"}):
        rejects(lambda: assembly.repair_visual_target(target_event, target_example, {**target_review, **patch}, target_raw))
    bad = deepcopy(target_raw); bad["attempt"]["pasteCheckpoints"] = [{}]
    rejects(lambda: assembly.repair_visual_target(target_event, target_example, target_review, bad))
    bad = deepcopy(target_raw); bad["attempt"]["inputEvents"][0]["observedAt"] = "04"
    rejects(lambda: assembly.repair_visual_target(target_event, target_example, target_review, bad))
finally:
    assembly.h.sha = original_sha

e = {"sourceEventID": "read", "serialized": '{"kind":"read","content":"RHF"}',
     "readNovelty": {"content": "", "dependsOnEventID": "prior", "currentCharacterCount": 3, "overlapCharacterCount": 3}}
patched, _ = assembly.patch_read(e, [("RHF", "RLHF", 1)])
assert patched["readNovelty"]["content"] == ""
assert patched["readNovelty"]["dependsOnEventID"] == "prior"
assert patched["readNovelty"]["currentCharacterCount"] == 4
assert '"content":"RLHF"' in patched["serialized"]
assert e["serialized"].endswith('"RHF"}')
rejects(lambda: assembly.patch_read(e, [("RHF", "RLHF", 2)]))
bad = deepcopy(e); bad["readNovelty"]["content"] = "partial delta"
rejects(lambda: assembly.patch_read(bad, [("RHF", "RLHF", 1)]))
print("Reviewed joins, causal boundaries, submitted recovery, and READ fallback tests passed.")

# False-empty prompt recovery: observed final local edits, not concatenated
# checkpoints, from the genuine initial query. Same rule handles retained and
# post-final-input recovered prefix endpoints.
def epoch(number, states, terminal=None, submit=False):
    before = {"observationID": f"before-{number}", "value": "", "observedAt": f"{number}0"}
    inputs, checkpoints = [], []
    for i, (hint, value) in enumerate(states, 1):
        stamp = number * 100 + i
        at = f"{number}{i}"
        inputs.append({"hint": hint, "mutationCapable": True, "eventTimestampNanoseconds": stamp, "observedAt": at})
        checkpoints.append({"checkpointID": f"cp-{stamp}", "eventTimestampNanoseconds": stamp,
                            "observation": {"observationID": f"obs-{stamp}", "value": value, "observedAt": at + ".1"}})
    returns = []
    if submit:
        inputs.append({"hint": "return", "mutationCapable": False, "eventTimestampNanoseconds": number*100+99, "observedAt": f"{number}8"})
        returns = [{"eventTimestampNanoseconds": inputs[-1]["eventTimestampNanoseconds"],
                    "observation": {"observationID": f"return-{number}", "value": states[-1][1], "observedAt": f"{number}8.1"}}]
    return {"recordID": f"raw-{number}", "sessionID": "session", "beganAt": inputs[0]["observedAt"], "lastInputAt": inputs[-1]["observedAt"],
            "terminalDecisionAt": f"{number}9", "boundaryReason": "return_pressed" if submit else "pointer_selection_boundary",
            "before": before, "after": {"observationID": f"after-{number}", "value": states[-1][1] if terminal is None else terminal, "observedAt": f"{number}9"},
            "targetIdentity": {"bundleIdentifier": "com.microsoft.VSCode", "processIdentifier": 2, "elementHash": 4, "role": "AXTextField", "windowTitle": "same"},
            "conditioningState": {"sourceObservationID": before["observationID"], "clipboard": {"changeCount": 3, "textSHA256": "clip"}},
            "inputEvents": inputs, "inputEventCount": len(inputs), "mutationCheckpoints": checkpoints, "returnCheckpoints": returns}

prefix = epoch(1, [("typed", "wrong"), ("delete", ""), ("typed", "I don't think ")])
suffix = epoch(2, [("typed", "this passes")], submit=True)
assert onsets.reconstruct([prefix, suffix])[0] == "I don't think this passes"
reset_prefix = deepcopy(prefix); reset_prefix["after"]["value"] = ""
content, epochs = onsets.reconstruct([reset_prefix, suffix])
assert content == "I don't think this passes" and "wrong" not in content
assert epochs[0]["selectedObservationSource"] == "post_final_input_checkpoint_terminal_reset"
assert prefix["after"]["value"] == "I don't think "  # no raw mutation
for patch in ({"elementHash": 5}, {"windowTitle": "other"}):
    bad = deepcopy(suffix); bad["targetIdentity"].update(patch)
    rejects(lambda: onsets.reconstruct([prefix, bad]))
bad = deepcopy(prefix); bad["returnCheckpoints"] = suffix["returnCheckpoints"]
rejects(lambda: onsets.reconstruct([bad, suffix]))
bad = deepcopy(prefix); bad["after"]["value"] = "different nonempty document"
rejects(lambda: onsets.reconstruct([bad, suffix]))
bad = deepcopy(prefix); bad["mutationCheckpoints"] = bad["mutationCheckpoints"][:-1]
rejects(lambda: onsets.reconstruct([bad, suffix]))
bad = deepcopy(suffix); bad["conditioningState"]["clipboard"]["changeCount"] = 4
rejects(lambda: onsets.reconstruct([prefix, bad]))
bad = epoch(2, [("delete", ""), ("typed", "this passes")], submit=True)
rejects(lambda: onsets.reconstruct([prefix, bad]))
bad = deepcopy(prefix); bad["pasteCheckpoints"] = [{}]
rejects(lambda: onsets.reconstruct([bad, suffix]))
bad = epoch(1, [("typed", "draft"), ("delete", "")])
rejects(lambda: onsets.reconstruct([bad, suffix]))
outside = {"recordType": "active_tap_write_attempt", "recordID": "other", "beganAt": "15", "terminalDecisionAt": "19"}
rejects(lambda: onsets.require_interval([prefix, suffix], [outside], {"new": []}, []))
novel = {"kind": "read", "sourceEventID": "read", "availableAt": "20", "serialized": "new information", "sourceRecordIDs": ["capture"]}
rejects(lambda: onsets.require_interval([prefix, suffix], [], {"new": [novel]}, []))
review = {"arm": "new", "eventID": "read", "decision": "novel_read", "serializedSHA256": onsets.projection.stable_id("", novel["serialized"]), "observationRecordIDs": ["capture"]}
rejects(lambda: onsets.require_interval([prefix, suffix], [], {"new": [novel]}, [review]))
review["decision"] = "repeated_prior_response_plus_own_draft"
onsets.require_interval([prefix, suffix], [], {"new": [novel]}, [review])
review["serializedSHA256"] = "tampered"
rejects(lambda: onsets.require_interval([prefix, suffix], [], {"new": [novel]}, [review]))
print("Prompt onset/terminal reset, deleted wording, genuine submission, identity, clipboard, outside WRITE and READ boundary controls passed.")

# A durable continuation replaces its local endpoint; it must not append the
# whole suffix twice. Local typo correction still uses the final value only.
middle = epoch(2, [("typed", "this paszes")])
final = epoch(3, [("delete", "this pas"), ("typed", "this passes")], submit=True)
final["before"]["value"] = "this paszes"
content, proof = onsets.reconstruct([prefix, middle, final])
assert content == "I don't think this passes"
assert proof[-1]["compositionTransition"] == "continuous_local_revision"
assert [e["localEpochOrdinal"] for e in proof] == [0, 1, 1]
bad = deepcopy(final); bad["before"]["value"] = "unrelated field contents"
rejects(lambda: onsets.reconstruct([prefix, middle, bad]))
bad = deepcopy(final); bad["mutationCheckpoints"][0]["observation"]["value"] = ""
rejects(lambda: onsets.reconstruct([prefix, middle, bad]))

# Novel information prohibits a full target but not a proven historical
# reconstruction. The caller retains the READ and withholds target loss.
review["serializedSHA256"] = onsets.projection.stable_id("", novel["serialized"])
review["decision"] = "novel_read"
rejects(lambda: onsets.require_interval([prefix, suffix], [], {"new": [novel]}, [review]))
onsets.require_interval([prefix, suffix], [], {"new": [novel]}, [review], history_only=True)

# Submission observation is a sensor record type, not a semantic verdict.
# Only an exact, bound, non-submitting disposition can cross a restored span.
probe = {"recordID": "probe", "recordType": "prompt_submission_observation", "observedAt": suffix["beganAt"],
         "disposition": "superseded_by_subsequent_mutation", "sourceWriteRecordID": prefix["recordID"],
         "targetIdentity": deepcopy(prefix["targetIdentity"]), "referenceRetainedAt": prefix["terminalDecisionAt"],
         "terminalObservationID": prefix["after"]["observationID"],
         "terminalValueSHA256": hashlib.sha256(prefix["after"]["value"].encode()).hexdigest()}
def probe_review(p):
    return {"recordID": p["recordID"], "decision": "verified_non_submission_probe", "rawRecordSHA256": onsets.projection.stable_id("", p)}
onsets.require_interval([prefix, suffix], [probe], {"new": []}, [], [probe_review(probe)])
rejects(lambda: onsets.require_interval([prefix, suffix], [probe], {"new": []}, []))
for patch in ({"disposition": "submitted"}, {"terminalValueSHA256": "bad"}, {"sourceWriteRecordID": "other"}, {"action": {"kind": "return"}}):
    bad = {**probe, **patch}
    rejects(lambda: onsets.require_interval([prefix, suffix], [bad], {"new": []}, [], [probe_review(bad)]))
bad = {**probe, "disposition": "surface_changed", "action": {"kind": "pointer_click"}}
rejects(lambda: onsets.require_interval([prefix, suffix], [bad], {"new": []}, [], [probe_review(bad)]))

# Later visual evidence may corroborate the empty original query, never
# replace it with future content. Any input between checkpoint/image rejects.
first = epoch(1, [("typed", "h"), ("typed", "hi")])
ref = {"checkpointObservationID": "obs-102", "reviewedContent": "hi"}
onsets.require_onset_prefix(ref, {"capturedAt": "12.2"}, first)
rejects(lambda: onsets.require_onset_prefix({**ref, "reviewedContent": "hidden hi"}, {"capturedAt": "12.2"}, first))
bad = deepcopy(first); bad["inputEvents"].append({"eventTimestampNanoseconds": 103, "observedAt": "12.15"})
rejects(lambda: onsets.require_onset_prefix(ref, {"capturedAt": "12.2"}, bad))
pending = deepcopy(first)
pending["inputEvents"].append({"eventTimestampNanoseconds": 103, "observedAt": "12.15", "hint": "typed"})
pending["mutationCheckpoints"].append({"eventTimestampNanoseconds": 103,
                                      "observation": {"observationID": "obs-103", "observedAt": "12.3", "value": "hi!"}})
onsets.require_onset_prefix(ref, {"capturedAt": "12.2"}, pending)
bad = deepcopy(pending); bad["mutationCheckpoints"][-1]["observation"]["value"] = "bye"
rejects(lambda: onsets.require_onset_prefix(ref, {"capturedAt": "12.2"}, bad))
bad = deepcopy(pending); bad["inputEvents"][-1]["hint"] = "delete"
rejects(lambda: onsets.require_onset_prefix(ref, {"capturedAt": "12.2"}, bad))
print("Continuous revision, history-only novel READ, probe-disposition and visual-onset controls passed.")
