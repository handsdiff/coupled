# Dataset storage

Raw capture is irreplaceable: retain original journals, session manifests,
screenshots, and other sensor evidence. Also retain annotations, model outputs,
scores, timings/costs, and the code/configuration used by experiments.

`examples.jsonl` and `packed-examples.jsonl` are derived artifacts. Superseded
copies can be deleted by an explicitly reviewed file plan. Keep the active
corpus and comparison inputs until their consumers no longer need them.
Rebuilding an old experiment requires its source evidence, versioned pipeline,
configuration, and tokenizer—not just today's code. Old dashboards/audits can
need reconstruction after their input artifacts are deleted.

## Avoid repeating entire histories on disk

The opt-in storage representation `phase1-shared-context-v1` stores history
once in `context-blocks.jsonl`, with ordered references and hashes in each
example. `JSONLSequence` reconstructs and verifies the exact `context` and
`modelInput` on access. Targets, masks, chronology, episode membership, and
semantic conversion versions do not change. Existing expanded files still work.

For new multi-session assembly:

```sh
python3 scripts/assemble-phase1-corpus.py \
  --input PATH_TO_FIRST_CAUSAL_SESSION --input PATH_TO_NEXT_CAUSAL_SESSION \
  --output PATH_TO_NEW_CORPUS --compact-examples
```

The closed-episode constructor inherits this storage mode. The updated packer,
corpus audits, experiment loader, and corpus/episode inspectors understand it.
Older custom scripts that directly parse JSON instead of `JSONLSequence` must
be updated before consuming this representation. It is deliberately opt-in;
existing frozen experiments are not migrated automatically.

To create an equivalent compact copy of an existing derived corpus:

```sh
python3 scripts/compact-phase1-corpus.py \
  --source PATH_TO_EXISTING_CORPUS --output PATH_TO_NEW_COMPACT_CORPUS
```

This checks source hashes, preserves other corpus evidence, and compares every
decoded record. The source is unchanged. Pack/audit the new artifact before
using it for an experiment; storage file hashes legitimately differ.

## Resource and retention policy

- Assembly, episode construction, and packing require a 20 GiB free-space
  reserve. Long writes recheck it. They fail without automatically pruning data.
- Keep only required canonical outputs and intentional comparison versions.
  Delete temporary replay/determinism outputs after their checks; keep the
  configuration and checksums needed to repeat them.
- Use explicit deletion plans, never recursive deletion of a session directory.
  `manage-phase1-storage.py delete-derived` defaults to validation only;
  `--apply` hashes each exact file and journals its deletion. Capture-session
  files and symlink targets are refused.
- Local Time Machine snapshots can retain deleted blocks. Removing those
  snapshots affects system-wide local backup history and requires separate
  permission. Never silently remove backups to satisfy a disk-space gate.

## September 10 cleanup

At the user's request, pre-September-2 expanded/packed examples were deleted,
not archived. Raw capture and recent comparison data were retained, including
September-1-named sessions that contain September 2 activity. The detailed file
plan, deletion journal, and matching before/after capture hashes are local in
`coupled-data/storage-maintenance-20260910/`. The two temporary archives created
before the user changed this policy were also deleted. Some optional historical
integration checks require those deleted derived inputs and now explicitly skip
when the inputs are unavailable; synthetic regression checks remain enabled.

After the verified cleanup, the user confirmed additional manual deletion.
This removed some newer derived inputs and the original
`normal-work-dry-run-5/raw.jsonl` (231,208,975 bytes). That raw file was never in
the agent's deletion plan. Its recorded SHA-256 is
`1ac516cd76ebac85363debd4fc8965ec42923f2f763f508c71f758179d6dd35e`.
Recovery from Trash or backup remains unconfirmed (Trash access was denied).
Do not assume it can be reconstructed from `examples.jsonl`.

## September 11 finalization

Two superseded `paired-v10-checked` preview `examples.jsonl` files were deleted
(1.778 GiB). The reviewed final source is `paired-v11-checked/new`. Exact paths,
pre-deletion hashes and outcomes are retained in the pipeline-era comparison's
`revision-v2/cleanup-plan.json` and `cleanup-journal.jsonl`. The intermediate
directory is intentionally no longer a complete corpus; its copied review
specification and parent/source provenance remain for reconstruction. No raw
capture, screenshots, final execution inputs or prior model results were deleted.
