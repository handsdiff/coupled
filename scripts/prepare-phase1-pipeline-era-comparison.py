#!/usr/bin/env python3
"""Pin the pre-Sep-2 pipeline versus curated data; no training/provider path.

Unlike the READ-only experiment, the arms intentionally need not share targets,
queries, episode boundaries, eligibility, history, or example counts. This is a
preparation manifest, not an executable training plan or an old-corpus replay.
"""

import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile


ROOT = Path(__file__).resolve().parents[1]
VERSION = "phase1-pipeline-era-comparison-v1"
BASELINE = "f6a2e713c378adc4e2f989b76882d9a14159b526"
PREP = ROOT / "coupled-data/sep02-10-training-prep-20260910"
CURATED = ROOT / "coupled-data/sep02-10-curated-repair-20260911/paired-v9-checked"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def save(path, value):
    with path.open("x") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def export_baseline(output):
    """Export tracked source bytes, without checkout, build, signing, or overlays."""
    archive = git("archive", BASELINE, "Package.swift", "Sources", "scripts")
    output.mkdir()
    hashes = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar:
            relative = Path(member.name)
            require(not relative.is_absolute() and ".." not in relative.parts,
                    "Unsafe source archive path")
            target = output / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            require(member.isfile(), "Source export must not contain links/devices")
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as source, target.open("xb") as destination:
                destination.write(source.read())
            hashes[member.name] = sha(target)
    return hashes


def correction_inventory(curated):
    """Archive decision documents, not a claim that every older proposal was used.

    Applied membership is defined by the selected corpus/provenance. Older
    review proposals stay distinguishable, so a future rule implementation can
    trace both the decision and its evidence instead of guessing from filenames.
    """
    records = []
    for path in sorted(curated.parent.rglob("*")):
        if not path.is_file() or path.stat().st_size > 32 * 1024**2:
            continue
        name = path.name
        decision = (any(term in name for term in (
            "adjudication", "join-review", "prompt-onset-review", "prompt-restoration",
            "target-correction", "read-correction", "batch-review", "batch-change",
            "retained-boundar", "supplemental-boundar"))
            and path.suffix in {".json", ".jsonl"})
        if decision:
            records.append({"path": str(path.resolve()), "sha256": sha(path),
                            "bytes": path.stat().st_size,
                            "role": "decision_archive_check_selected_artifact_for_application"})
    # Some OCR adjudications are currently explicit before/after replacements
    # in a finite curation script. Preserve that source, not just the final text.
    for name in ("assemble-phase1-curated-review.py", "apply-phase1-reviewed-batch.py",
                 "curate-phase1-episode-joins.py", "restore-phase1-prompt-onsets.py",
                 "recover-phase1-reviewed-submissions.py"):
        path = ROOT / "scripts" / name
        records.append({"path": str(path), "sha256": sha(path),
                        "bytes": path.stat().st_size, "role": "curation_implementation"})
    return records


def validate_contract(plan):
    require(plan["comparison"] == "whole_pipeline_era_not_read_only", "Wrong comparison")
    old, new = plan["arms"]["old"], plan["arms"]["new"]
    require(old["gitCommit"] == BASELINE, "Old era is not the pre-Sep-2 commit")
    require(old["pipeline"] == {"pane": "ax-pane-read-v2", "semantic": "phase1-semantic-v13",
                               "causal": "phase1-causal-v15", "episode": "phase1-raw-episode-v8"},
            "Old semantic versions changed")
    require(old["applyNewWriteRepairs"] is False and old["applyNewReadRepairs"] is False,
            "Cleanup must not contaminate the historical arm")
    require(old["consumeDynamicVisualReadRecords"] is False, "Historical arm acquired a new sensor")
    require(new["manualCurationAllowed"] is True, "Manual curation is part of the new arm")
    require(Path(new["artifactDirectory"]).name == "new", "Selected hybrid old arm instead of cleaned arm")
    require(all(value is False for value in plan["pairingRequirements"].values()),
            "Do not force independent pipelines into a shared WRITE cohort")
    require(plan["trainingAuthorization"] == "not_authorized" and plan["providerCalls"] == 0,
            "Preparation is not training approval")


def prepare(output, curated=CURATED, prep=PREP):
    require(not output.exists(), "Use a fresh comparison directory; preserve existing artifacts")
    old_inputs = json.loads((prep / "inputs.json").read_text())
    review = json.loads((curated / "review.json").read_text())
    selected = {}
    for name in ("new/events.jsonl", "new/examples.jsonl", "new/context-blocks.jsonl"):
        path = curated / name
        digest = sha(path)
        require(digest == review["artifactsSHA256"][name], f"Changed cleaned artifact: {name}")
        selected[str(path)] = digest
    require(review["counts"]["new"]["examples"] > 0, "Empty cleaned dataset")
    # Stream the raw fingerprints. Never load whole journals into memory here.
    for path, digest in old_inputs["rawDigests"].items():
        require(sha(path) == digest, f"Raw source changed: {path}")
    last_commit = git("rev-list", "-1", "--before=2026-09-02T00:00:00-04:00", "HEAD").decode().strip()
    require(last_commit == BASELINE, "Pre-Sep-2 anchor needs explicit re-review")
    output.mkdir(parents=True)
    producer_hashes = export_baseline(output / "old-producer")
    inventory = correction_inventory(curated)
    save(output / "manual-correction-inventory.json", {
        "scope": "Preserved review decisions plus finite curation code; superseded proposals are not automatically applied",
        "selectedReviewManifest": str(curated / "review.json"),
        "selectedReviewSHA256": sha(curated / "review.json"),
        "selectedArtifactsSHA256": selected,
        "records": inventory,
    })
    plan = {
        "version": VERSION, "comparison": "whole_pipeline_era_not_read_only",
        "purpose": "Measure the overall benefit of data-construction work since before September 2",
        "status": "definition_pinned_old_replay_and_native_packing_pending",
        "rawSourcesSHA256": old_inputs["rawDigests"], "sessions": old_inputs["sessions"],
        "arms": {
            "old": {
                "label": "Before heavy reconstruction work (September 1)",
                "gitCommit": BASELINE,
                "commitDate": git("show", "-s", "--format=%cI", BASELINE).decode().strip(),
                "producerDirectory": str(output / "old-producer"),
                "producerFilesSHA256": producer_hashes,
                "pipeline": {"pane": "ax-pane-read-v2", "semantic": "phase1-semantic-v13",
                             "causal": "phase1-causal-v15", "episode": "phase1-raw-episode-v8"},
                "applyNewWriteRepairs": False, "applyNewReadRepairs": False,
                "consumeDynamicVisualReadRecords": False,
                "status": "source_frozen_full_corpus_replay_pending",
                "artifactDirectory": None,
                "laterRawCompatibility": "No silent schema adapters; report any unsupported evidence or compatibility changes",
            },
            "new": {
                "label": "Maximum-effort cleaned construction, including audited manual corrections",
                "artifactDirectory": str(curated / "new"),
                "artifactsSHA256": selected,
                "reviewManifest": str(curated / "review.json"),
                "reviewManifestSHA256": sha(curated / "review.json"),
                "counts": review["counts"]["new"], "manualCurationAllowed": True,
                "status": "curated_corpus_available_native_pack_pending",
                "manualCorrectionInventorySHA256": sha(output / "manual-correction-inventory.json"),
            },
        },
        "pairingRequirements": {"sameTargetText": False, "sameExampleIDs": False,
                                "sameQueries": False, "sameWriteHistory": False,
                                "sameEpisodeBoundaries": False, "sameEligibility": False,
                                "sameExampleCount": False},
        "evaluation": {
            "primary": "Holistic usefulness and intended thought, with target-construction problems reported separately",
            "targets": "Retain each arm's own outputs and their provenance; do not rewrite the old labels to the new labels",
            "NLL": "Frozen versus trained within each arm; absolute cross-arm NLL is not a paired same-target statistic",
            "report": ["eligible examples", "unique target tokens", "training presentations",
                       "chronological coverage", "cost", "latency", "holistic score and denominator"],
            "pairedSubset": "Optional secondary diagnostic only; never silently restrict the primary comparison",
        },
        "captureQualification": "Both arms process preserved Sep2-10 raw sessions. The old code ignores later visual-change READ types. This is not an exact counterfactual recording with the old collector.",
        "recipe": {"referenceOnly": str(prep / "training-contract.json"),
                   "referenceSHA256": sha(prep / "training-contract.json"),
                   "intent": "Same model and optimizer recipe; regenerate per-arm schedules, packs and costs after independent replay",
                   "freezeStatus": "pending_native_validation_and_review"},
        "supersededForPrimaryExperiment": [str(curated), str(prep / "execution-plan.json")],
        "retainedDiagnosticMeaning": "paired-v9 old/new is repaired WRITEs with differing READs, not the historical whole-pipeline baseline",
        "nextGates": ["Replay old source with bounded memory/storage and no semantic overlays",
                      "Audit independent old corpus and preserve all differences",
                      "Native model packing and per-arm loss-mask/EOS checks",
                      "Freeze revised schedules, exposure accounting and cost plan",
                      "Obtain execution approval"],
        "providerCalls": 0, "trainingAuthorization": "not_authorized",
        "preparationScriptSHA256": sha(Path(__file__)),
    }
    validate_contract(plan)
    save(output / "comparison.json", plan)
    save(output / "preparation-audit.json", {
        "status": "passed_definition_and_input_binding_only", "oldCorpusReplayComplete": False,
        "newExamples": plan["arms"]["new"]["counts"]["examples"],
        "exportedHistoricalFiles": len(producer_hashes), "correctionEvidenceFiles": len(inventory),
        "comparisonSHA256": sha(output / "comparison.json"), "providerCalls": 0,
    })
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--curated", type=Path, default=CURATED)
    parser.add_argument("--parent-comparison", type=Path,
                        help="Rebind only the cleaned arm; retain the existing historical export verbatim")
    args = parser.parse_args()
    if args.parent_comparison:
        result = json.loads(args.parent_comparison.read_text())
        validate_contract(result)
        curated = args.curated.resolve()
        review = json.loads((curated / 'review.json').read_text())
        selected = {}
        for name in ('new/events.jsonl', 'new/examples.jsonl', 'new/context-blocks.jsonl'):
            path = curated / name
            require(sha(path) == review['artifactsSHA256'][name], 'Changed cleaned artifact')
            selected[str(path)] = sha(path)
        args.output.mkdir(parents=True, exist_ok=False)
        save(args.output / 'manual-correction-inventory.json', {
            'selectedReviewManifest': str(curated / 'review.json'),
            'selectedReviewSHA256': sha(curated / 'review.json'),
            'selectedArtifactsSHA256': selected, 'records': correction_inventory(curated)})
        result['version'] = 'phase1-pipeline-era-comparison-v2'
        result['parentComparison'] = {'path': str(args.parent_comparison.resolve()), 'sha256': sha(args.parent_comparison)}
        result['arms']['new'].update(artifactDirectory=str(curated/'new'), artifactsSHA256=selected,
            reviewManifest=str(curated/'review.json'), reviewManifestSHA256=sha(curated/'review.json'),
            counts=review['counts']['new'], manualCorrectionInventorySHA256=sha(args.output/'manual-correction-inventory.json'))
        result['preparationScriptSHA256'] = sha(Path(__file__))
        validate_contract(result)
        save(args.output / 'comparison.json', result)
    else:
        result = prepare(args.output.resolve(), curated=args.curated.resolve())
    print(json.dumps({"status": result["status"], "oldCommit": BASELINE,
                      "newExamples": result["arms"]["new"]["counts"]["examples"], "providerCalls": 0}))


if __name__ == "__main__":
    main()
