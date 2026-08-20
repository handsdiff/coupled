#!/usr/bin/env python3
"""Verify the user/reviewer-adjudicated episode-construction regressions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected object: {path}")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--corpus", required=True, type=Path)
    args = parser.parse_args()

    policy = load_json(args.policy.resolve())
    root = args.corpus.resolve()
    manifest = load_json(root / "corpus.json")
    raw_authoritative = str(manifest.get("episodeVersion", "")).startswith(
        "phase1-raw-episode-v"
    )
    adjudication_rows = load_jsonl(root / "episode-adjudications.jsonl")
    adjudications = {row["label"]: row for row in adjudication_rows}
    adjudication_by_member = {
        event_id: row
        for row in adjudication_rows
        for event_id in row.get("memberWriteEventIDs", [])
    }
    neighborhoods = {
        row["label"]: row for row in policy.get("neighborhoods", [])
    }
    overrides = policy.get("rawEpisodeV1Overrides", {}) if raw_authoritative else {}
    if raw_authoritative:
        production_source = (
            Path(__file__).resolve().parent / "construct-phase1-raw-episode-corpus.py"
        ).read_text()
        forbidden = {
            value
            for row in neighborhoods.values()
            for value in [row.get("label"), *row.get("writeEventIDs", [])]
            if isinstance(value, str)
        }
        embedded = sorted(value for value in forbidden if value in production_source)
        if embedded:
            raise AssertionError(
                f"production raw episode compiler embeds oracle identities: {embedded[:3]}"
            )
    examples = load_jsonl(root / "examples.jsonl")
    example_candidates = {
        row["episode"]["candidateID"] for row in examples
    }
    expected_rows = policy.get("adjudications")
    if not isinstance(expected_rows, list) or not expected_rows:
        raise ValueError("policy has no adjudications")

    for expected in expected_rows:
        label = expected["label"]
        expected = {**expected, **overrides.get(label, {})}
        neighborhood = neighborhoods.get(label) or {}
        member_ids = neighborhood.get("writeEventIDs", [])
        if raw_authoritative:
            matched = [adjudication_by_member.get(value) for value in member_ids]
            if not member_ids or any(value is None for value in matched):
                raise AssertionError(f"missing regression lineage: {label}")
            candidate_ids = {value["candidateID"] for value in matched if value}
            if len(candidate_ids) != 1:
                raise AssertionError(
                    f"{label}: expected members span {len(candidate_ids)} episodes"
                )
            actual = matched[0]
        else:
            actual = adjudications.get(label)
        if actual is None:
            raise AssertionError(f"missing regression adjudication: {label}")
        if actual["decision"] != expected["decision"]:
            raise AssertionError(
                f"{label}: {actual['decision']} != {expected['decision']}"
            )
        is_example = actual["candidateID"] in example_candidates
        if is_example != (expected["decision"] == "closed_loss_episode"):
            raise AssertionError(f"{label}: loss-bearing example membership disagrees")
        target = actual.get("finalizedTarget") or {}
        resolved = target.get("resolvedContent")
        if not isinstance(resolved, str) or not resolved:
            raise AssertionError(f"{label}: missing resolved completion")
        if expected["targetPolicy"] == "proven_paste_authorship":
            paste = [
                segment for segment in target.get("segments", [])
                if segment.get("type") == "paste"
            ]
            if len(paste) != 1 or not paste[0].get("clipboardSnapshotID"):
                raise AssertionError(f"{label}: paste is not uniquely grounded")
            if expected["decision"] == "closed_loss_episode":
                authored = [
                    segment for segment in target["segments"]
                    if segment.get("type") == "authored_text" and segment.get("content")
                ]
                if len(authored) < 2:
                    raise AssertionError(f"{label}: mixed paste lost authored spans")
        if expected["targetPolicy"] == "grounded_opaque_paste_epoch":
            segments = target.get("segments", [])
            paste = [row for row in segments if row.get("type") == "paste"]
            authored = [
                row for row in segments if row.get("type") == "authored_text"
            ]
            if (
                len(paste) != 1
                or paste[0].get("directSemanticInsertionObserved") is not False
                or paste[0].get("deliverySemantics")
                    != "synchronous_cmd_v_with_opaque_post_paste_ax_epoch"
                or len(str(paste[0].get("historyContent") or "")) != 2436
                or len(authored) != 2
                or not str(authored[0].get("content") or "").endswith(
                    'not "merge_closed_episode". i agree with this. "'
                )
                or authored[1].get("content") != '"'
            ):
                raise AssertionError(f"{label}: opaque paste epoch was not reconstructed")
            rebuilt = "".join(
                str(row.get("content") or row.get("historyContent") or "")
                for row in segments
            )
            if rebuilt != resolved:
                raise AssertionError(f"{label}: opaque paste history does not round-trip")
        if expected["targetPolicy"] == "offset_grounded_repeated_paste":
            segments = target.get("segments", [])
            paste = [row for row in segments if row.get("type") == "paste"]
            authored = [
                row for row in segments if row.get("type") == "authored_text"
            ]
            expected_payload = (
                "captures the users actual next thought rather than their messy "
                "application of that next thought into the computer"
            )
            if (
                len(paste) != 1
                or paste[0].get("historyContent") != expected_payload
                or paste[0].get("pasteCheckpointID")
                    != "8F3A85D6-DF71-4407-A3F5-8C736A3DF579"
                or len(authored) != 2
                or expected_payload not in str(authored[0].get("content") or "")
                or not str(authored[0].get("content") or "").endswith(': "')
                or authored[1].get("content") != '"'
            ):
                raise AssertionError(
                    f"{label}: raw offset did not disambiguate repeated paste payload"
                )
            rebuilt = "".join(
                str(row.get("content") or row.get("historyContent") or "")
                for row in segments
            )
            if rebuilt != resolved:
                raise AssertionError(f"{label}: offset-grounded paste does not round-trip")
        if expected["targetPolicy"] == "exactly_canceled_paste":
            if (
                any(
                    row.get("type") in {"paste", "unresolved_paste_transition"}
                    for row in target.get("segments", [])
                )
                or [row.get("type") for row in target.get("segments", [])]
                    != ["authored_text"]
                or resolved != (
                    "2026-08-19T12:43:16:708Z in the new data, but editing a "
                    "sentence in between within a write delay should not be separate "
                    "write events sometimes. i initially added it after seeing a prior "
                    "example that i now forget but it feels net useless? or at the very "
                    "least it should be cleaned into a single training example for the "
                    "purposes of loss, and in the training set it can either stay a "
                    "single write or remain raw i dont necessary think it has a large impact"
                )
            ):
                raise AssertionError(
                    f"{label}: exactly undone paste contaminated authored target"
                )
        if expected["targetPolicy"] == "novel_read_same_prompt_partition":
            if (
                actual.get("closureStatus") != "closed_composition_region"
                or actual.get("closureReason") != "novel_causal_read_partition"
                or len(resolved) != 520
            ):
                raise AssertionError(
                    f"{label}: novel READ did not close the same-prompt thought"
                )

    required_targets = {
        "regression_loss_057": "what chain does stripe mpp run on",
        "regression_loss_059": "dont blockchains require burning",
        "regression_loss_060": "is being a tempo validator open source?",
        "regression_loss_124": "what is the cost of a node of b300",
        "regression_loss_blind_obsidian_124316": (
            "another interesting finding is that the best qwen example (131) feels "
            "materially stronger in terms of understanding my next write / how my brain "
            "works than the best gpt example (111)\n\n"
            "both qwen and gpt sol only get matches on things that are mode collapsing or "
            "short, like paste actions or URLs\n\n"
            "maybe LLM judge / cosine sim with RL was considered better for more "
            "intermediate learning steps? which is weird since RL is considered much LESS "
            "sample efficient than SFT, but that would indicate the opposite?"
        ),
        "regression_loss_blind_code_190505": (
            "this is better. take a look at the reviewer thread. it flagged a "
            "post-submission trigger to clean up some other gaps."
        ),
        "regression_loss_novel_read_same_prompt": (
            "i think it makes sense to stick with the model that predicts write "
            "content given history, clipboard, and cursor semantic state. for now "
            "we'll just collect a ton of data and do the gut check between qwen 3.5 "
            "9b and a frontier LLM in terms of predictive capacity. later, we can "
            "remove assumptions i.e. train the model to predict WHERE the write will "
            "occur. for now we want something simple that works, instead of something "
            "complex that doesnt. in terms of sampling, we can stick with sampling "
            "when a text field is focused."
        ),
    }
    def actual_for(label: str) -> dict[str, Any]:
        if not raw_authoritative:
            return adjudications[label]
        event_id = neighborhoods[label]["writeEventIDs"][0]
        return adjudication_by_member[event_id]

    for label, text in required_targets.items():
        if actual_for(label)["finalizedTarget"]["resolvedContent"] != text:
            raise AssertionError(f"{label}: concise submitted question changed")

    history_without_onset = {
        "regression_history_066_067",
        "regression_history_125",
    }
    for label in history_without_onset:
        if actual_for(label)["decision"] != "closed_history_episode":
            raise AssertionError(f"{label}: unavailable onset received loss")

    print(json.dumps({
        "status": "passed",
        "episodeVersion": manifest.get("episodeVersion"),
        "regressionEpisodes": len(expected_rows),
        "lossEpisodes": sum(
            row["decision"] == "closed_loss_episode" for row in expected_rows
        ),
        "historyOnlyEpisodes": sum(
            row["decision"] == "closed_history_episode" for row in expected_rows
        ),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
