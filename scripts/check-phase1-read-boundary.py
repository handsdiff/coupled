#!/usr/bin/env python3
"""Sanitized regressions for evidence-based composition READ boundaries."""
import json
import importlib.util
from pathlib import Path
import sys
import tempfile
from phase1_read_boundary import ReadBoundaryEvidence

def read(name, text, when, app="Code", raw_ids=()):
    return {"sourceEventID": name, "sessionID": "session", "kind": "read", "availableAt": when,
            "serialized": json.dumps({"kind": "read", "source": {"application": app}, "content": text}),
            "sourceRecordIDs": list(raw_ids)}

def raw(name, text, when):
    return {"recordID": name, "recordType": "screen_ocr_observation", "sessionID": "session",
            "appName": "Code", "capturedAt": when, "content": text, "windowID": 1}

BEFORE = "2026-01-01T00:00:00.000Z"
ONSET = "2026-01-01T00:00:01.000Z"
NOW = "2026-01-01T00:00:03.000Z"

def assess(old, new, draft="", raw_views=(), raw_ids=(), app="Code"):
    before, current = read("before", old, BEFORE), read("current", new, NOW, app, raw_ids)
    index = ReadBoundaryEvidence([before, current], raw_views)
    return index.assess(current, ONSET, draft, "Visual Studio Code")

def main():
    # Cropped prefix/suffix and metadata/app spelling do not require the full
    # draft as one substring. Neither case invents new external information.
    d = "hmm, so what is the current pipeline state that you're proposing?"
    row = assess("The report is complete.", "The report is complete. so what is the current pipeline state that you're", d)
    assert row['status'] == 'self_derived_active_composition_read'
    row = assess("The report is complete.", "The report is complete.", app="Visual Studio Code")
    assert row['status'] == 'exact_repeat_available_at_episode_onset'

    # Full draft match must NOT hide a fresh answer elsewhere in the READ.
    row = assess("The report is complete.", "The report is complete. The previous diagnosis is wrong; compare the actual rendered state. " + d, d)
    assert row['status'] == 'novel_causally_available_read'
    for old, new in [("Use 50 examples for the run", "Use 500 examples for the run"),
                     ("The model should improve", "The model should not improve"),
                     ("Do not forget the benchmark. The model should improve.", "The model should not improve."),
                     ("We have 500 reads. Use 50 examples for this run.", "We have 500 reads. Use 500 examples for this run.")]:
        assert assess(old, new)['status'] == 'novel_causally_available_read'

    # Before-typing raw evidence can establish already-visible content omitted
    # from the cleaned projection. A future raw observation cannot.
    full = "The first failure is clipping. The second failure is window identity."
    r = raw("raw-before", full, BEFORE)
    row = assess("The first failure is clipping.", full + " " + d, d, [r])
    assert row['status'] == 'self_derived_active_composition_read'
    future = raw("future", full, NOW)
    assert assess("The first failure is clipping.", full + " " + d, d, [future])['status'] == 'novel_causally_available_read'

    # Read lineages can include an earlier frame. It cannot erase a later
    # selected state containing new external information.
    rows = [raw("early", "The report is complete.", BEFORE),
            raw("late", "The report is complete. The diagnosis changed after investigation.", NOW)]
    row = assess("The report is complete.", rows[-1]['content'], raw_views=rows, raw_ids=["early", "late"])
    assert row['status'] == 'novel_causally_available_read'
    # Nor may poorer current raw OCR hide a new word in the semantic view.
    poor = raw("poor", "The model should improve", NOW)
    assert assess("The model should improve", "The model should not improve", raw_views=[poor], raw_ids=["poor"])['status'] == 'novel_causally_available_read'

    # A genuine lookup is retained. Matching the current draft in another app
    # is not proof of self-authored text in that surface.
    assert assess("Old response", d, d, app="Google Chrome")['status'] == 'novel_causally_available_read'
    uncertain = assess("The report is complete", "The report is complete unclear")
    assert uncertain['status'] == 'unresolved_read_novelty'
    a = assess("The report is complete", "The report is complete. " + d, d)
    assert a == assess("The report is complete", "The report is complete. " + d, d)

    # The same held field can establish older note text, but cannot explain a
    # new answer or an unrelated destination merely because the draft exists.
    current=read("field", "An earlier note remains here. " + d, NOW)
    index=ReadBoundaryEvidence([current])
    assert index.assess(current, ONSET, d, "Code", "An earlier note remains here.")['status']=='self_derived_active_composition_read'
    unrelated=read("other", "An earlier note remains here. " + d, NOW, "Google Chrome")
    assert index.assess(unrelated, ONSET, d, "Code", "An earlier note remains here.")['status']=='novel_causally_available_read'
    # Pane evidence is tied to the original raw capture, not its later OCR
    # processing time; mismatched timestamps/digests must fail closed.
    original=raw("pane-raw", "Old clipped text", BEFORE)
    pane={"sourceRecordID":"pane-raw", "capturedAt":BEFORE, "content":"The report is complete.", "evidenceID":"pane", "screenshotSHA256":None}
    current=read("pane-read", "The report is complete.", NOW)
    index=ReadBoundaryEvidence([current], [original], [pane])
    assert index.assess(current,ONSET,"","Code")['status']=='exact_repeat_available_at_episode_onset'
    # The bounded approximate-reference search must not forget exact older
    # repeats when more than 32 other views occurred before this episode.
    older=read("older", "An earlier distinctive source", BEFORE)
    unrelated=[read(str(i), f"Different source {i}", "2026-01-01T00:00:00.500Z") for i in range(40)]
    repeated=read("repeated", "An earlier distinctive source", NOW)
    index=ReadBoundaryEvidence([older,*unrelated,repeated])
    assert index.assess(repeated,ONSET,"","Code")['status']=='exact_repeat_available_at_episode_onset'
    try:
        ReadBoundaryEvidence([], [original], [{**pane, "capturedAt":NOW}])
        raise AssertionError('mismatched pane timestamp accepted')
    except ValueError:
        pass
    # The corpus auditor rejects altered boundary evidence before trusting
    # the new classification. Use a tiny synthetic journal, never real raw.
    project=Path(__file__).resolve().parent.parent
    spec=importlib.util.spec_from_file_location('boundary_auditor',project/'scripts/audit-phase1-closed-episode-corpus.py')
    auditor=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(auditor)
    with tempfile.TemporaryDirectory(prefix='read-boundary-check-',dir=project/'.build') as directory:
        root=Path(directory)
        raw_path=root/'raw.jsonl'
        raw_path.write_text('{}\n')
        manifest={
            'artifactType':'phase1_raw_authoritative_episode_corpus',
            'episodeVersion':'phase1-raw-episode-v10',
            'conversionVersion':'phase1-raw-episode-causal-v10',
            'rawEpisodeArchitecture':{
                'sourceAuthority':'immutable_raw_journals','productionConsumesRegressionFixture':False,
                'readBoundaryPolicy':'phase1-read-boundary-evidence-v1','navigationAloneClosesPrompt':False,
                'readBoundaryEvidenceRawSHA256':{str(raw_path.relative_to(project)):'0'*64},
            },
        }
        (root/'corpus.json').write_text(json.dumps(manifest))
        saved=sys.argv
        try:
            sys.argv=['audit',str(root)]
            try:
                auditor.main()
                raise AssertionError('tampered raw boundary evidence accepted')
            except ValueError as error:
                assert 'READ-boundary source digest mismatch' in str(error)
        finally:
            sys.argv=saved
    print("READ-boundary checks passed: draft clipping, real replies, numbers/negation, raw-onset provenance, uncertainty and determinism")

if __name__ == '__main__':
    main()
