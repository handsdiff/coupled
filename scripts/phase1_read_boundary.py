"""Causal evidence for composition boundaries, not READ-content rendering.

Neither a changed serialization nor a draft substring establishes novelty.
Explain the observed text using an earlier view and the observed composition;
any remainder stays blocking. This module never rewrites the READ stream.
"""
from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from difflib import SequenceMatcher
from functools import lru_cache
import hashlib
import json
import re
import unicodedata

VERSION = "phase1-read-boundary-evidence-v1"
MAX_PRIOR_VIEWS = 32
MIN_ANCHOR_WORDS = 3
APP_ALIASES = {"Code": "Visual Studio Code", "Chrome": "Google Chrome"}


def app_name(value):
    return APP_ALIASES.get(value, value)


@lru_cache(maxsize=256)
def tokens(text):
    return tuple(re.findall(r"\w+(?:['’]\w+)*", unicodedata.normalize("NFKC", text).casefold()))


def explain(reference, observed):
    """Coherent exact anchors, never a bag of individually familiar words.

    A complete short line can match as a whole. Short unmatched changes inside
    otherwise repeated text are deliberately not waved away as OCR noise.
    """
    covered = set()
    # OCR frequently interleaves adjacent wrapped lines. Match coherent local
    # phrases, preserving order *within* each anchor, without requiring every
    # paragraph to have the same global OCR order. Individual familiar words
    # cannot explain a new sentence.
    size = min(MIN_ANCHOR_WORDS, len(reference), len(observed))
    if not size:
        return covered
    anchors = {reference[i:i + size] for i in range(len(reference) - size + 1)}
    for i in range(len(observed) - size + 1):
        if observed[i:i + size] in anchors:
            covered.update(range(i, i + size))
    # An ordered alignment also retains short fragments (e.g. a two-word
    # wrapped heading) between the long anchors. Unlike a word-set comparison,
    # this cannot independently pick every familiar word from arbitrary order.
    for match in SequenceMatcher(None, reference, observed, autojunk=False).get_matching_blocks():
        covered.update(range(match.b, match.b + match.size))
    return covered


def residual_runs(observed, covered):
    runs = []
    start = None
    for index in range(len(observed) + 1):
        if index < len(observed) and index not in covered:
            if start is None:
                start = index
        elif start is not None:
            runs.append({"start": start, "end": index, "text": " ".join(observed[start:index])})
            start = None
    return runs


def text_key(text):
    return hashlib.sha256(" ".join(tokens(text)).encode()).hexdigest()


class ReadBoundaryEvidence:
    def __init__(self, events, raw_views=(), pane_views=()):
        self.events = defaultdict(list)
        self.views = defaultdict(list)
        self.raw_by_id = {}
        self.pane_by_raw_id = {}
        self.exact_first = {}
        for raw in raw_views:
            self.add_raw(raw)
        for pane in pane_views:
            source = self.raw_by_id.get(pane.get("sourceRecordID"))
            if source is None:
                continue
            if pane.get("capturedAt") != source["capturedAt"]:
                raise ValueError("pane/raw capture timestamp mismatch")
            if source.get("screenshotSHA256") and pane.get("screenshotSHA256") != source["screenshotSHA256"]:
                raise ValueError("pane/raw screenshot digest mismatch")
            row = dict(source, content=pane["content"], representation="full_pane_ocr",
                       recordID=pane["evidenceID"])
            self.views[(source["sessionID"], source["application"])].append(row)
            self.pane_by_raw_id[pane["sourceRecordID"]] = row
        for event in events:
            if event.get("kind") != "read":
                continue
            value = json.loads(event.get("serialized", "{}"))
            application = app_name((value.get("source") or {}).get("application"))
            self.events[event["sessionID"]].append(event)
            self.views[(event["sessionID"], application)].append({
                "recordID": event["sourceEventID"], "capturedAt": event["availableAt"],
                "content": value.get("content", ""), "representation": "semantic_read",
                "windowID": None,
            })
        for group in self.events.values():
            group.sort(key=lambda e: (e["availableAt"], e["sourceEventID"]))
        for group in self.views.values():
            group.sort(key=lambda e: (e["capturedAt"], e["recordID"]))
        for key, group in self.views.items():
            for row in group:
                self.exact_first.setdefault((*key, text_key(row["content"])), row)
        self.times = {key: [row["capturedAt"] for row in group] for key, group in self.views.items()}
        self.event_times = {key: [row["availableAt"] for row in group] for key, group in self.events.items()}

    def add_raw(self, raw):
        if raw.get("recordType") not in {"screen_ocr_observation", "visual_ocr_observation"}:
            return
        if not isinstance(raw.get("content"), str) or not raw.get("capturedAt"):
            return
        row = {key: raw.get(key) for key in (
            "recordID", "capturedAt", "content", "windowID", "screenshotSHA256",
            "contentWasTruncated", "sourceFrameRecordID",
        )}
        row["representation"] = "raw_ocr"
        row["sessionID"] = raw["sessionID"]
        row["application"] = app_name(raw.get("appName"))
        self.raw_by_id[row["recordID"]] = row
        self.views[(raw["sessionID"], app_name(raw.get("appName")))].append(row)

    def assess(self, event, onset, completion, application, initial_field=""):
        value = json.loads(event.get("serialized", "{}"))
        read_app = app_name((value.get("source") or {}).get("application"))
        key = (event["sessionID"], read_app)
        exact = self.exact_first.get((*key, text_key(value.get("content", ""))))
        if exact and exact["capturedAt"] < onset:
            return {"eventID": event["sourceEventID"], "availableAt": event["availableAt"],
                    "status": "exact_repeat_available_at_episode_onset",
                    "boundaryEvidence": {"policy": VERSION, "rule": "whole_text_repeat",
                        "observationID": event["sourceEventID"],
                        "priorObservationID": exact["recordID"], "priorCapturedAt": exact["capturedAt"],
                        "comparedAtOnset": onset, "unexplainedWords": 0, "unexplainedRuns": []}}
        group = self.views.get(key, [])
        end = bisect_left(self.times.get(key, []), onset)
        prior = group[max(0, end - MAX_PRIOR_VIEWS):end]
        semantic = {"content": value.get("content", ""), "recordID": event["sourceEventID"],
                    "representation": "semantic_read", "windowID": None}
        current_raw = [self.raw_by_id[rid] for rid in event.get("sourceRecordIDs", [])
                       if rid in self.raw_by_id and self.raw_by_id[rid]["capturedAt"] <= event["availableAt"]
                       and not self.raw_by_id[rid].get("contentWasTruncated")]
        # A whole raw-window comparison may prove that the processed difference
        # is OCR/cropping, but failure to explain that window does not make
        # unrelated out-of-pane pixels novel READ content.
        latest = sorted(current_raw, key=lambda r: (r["capturedAt"], r["recordID"]), reverse=True)[:1]
        panes = [self.pane_by_raw_id[row["recordID"]] for row in latest
                 if row["recordID"] in self.pane_by_raw_id]
        variants = [semantic, *panes, *latest]
        draft = tokens(completion) if read_app == app_name(application) else ()
        # The held editable's pre-mutation state is contemporaneous evidence,
        # not future knowledge. Only use it when this READ also contains a
        # coherent piece of the composition in the same application.
        field = tokens(initial_field) if draft and len(draft) >= MIN_ANCHOR_WORDS else ()
        best = None
        for current in variants:
            observed = tokens(current["content"])
            draft_coverage = explain(draft, observed) if draft else set()
            field_coverage = explain(field, observed) if len(draft_coverage) >= MIN_ANCHOR_WORDS else set()
            references = [None, *prior]
            for before in references:
                if before and current.get("windowID") is not None and before.get("windowID") is not None and current["windowID"] != before["windowID"]:
                    continue
                known = explain(tokens(before["content"]), observed) if before else set()
                covered = known | draft_coverage | field_coverage
                remaining = residual_runs(observed, covered)
                evidence = {
                    "policy": VERSION, "observationID": current["recordID"],
                    "observationRepresentation": current["representation"],
                    "priorObservationID": before["recordID"] if before else None,
                    "priorCapturedAt": before["capturedAt"] if before else None,
                    "comparedAtOnset": onset, "observedWords": len(observed),
                    "knownWords": len(known), "draftWords": len(draft_coverage - known),
                    "initialFieldWords": len(field_coverage - known - draft_coverage),
                    "initialFieldSHA256": hashlib.sha256(initial_field.encode()).hexdigest() if field_coverage else None,
                    "unexplainedWords": len(observed) - len(covered),
                    "unexplainedRuns": remaining,
                    "draftSHA256": hashlib.sha256(completion.encode()).hexdigest(),
                }
                if not remaining and current is not semantic:
                    # An older/poorer raw OCR must not erase text that the
                    # selected semantic READ actually contains. Require its
                    # complete textual payload to be grounded in this proof.
                    model_words = tokens(semantic["content"])
                    grounded = explain(observed, model_words)
                    grounded |= explain(draft, model_words) if draft else set()
                    grounded |= explain(field, model_words) if field_coverage else set()
                    grounded |= explain(tokens(before["content"]), model_words) if before else set()
                    if len(grounded) != len(model_words):
                        continue
                if not remaining:
                    return {"eventID": event["sourceEventID"], "availableAt": event["availableAt"],
                            "status": "self_derived_active_composition_read" if draft_coverage - known else "repeated_non_novel_read",
                            "boundaryEvidence": evidence}
                if current is semantic and (best is None or evidence["unexplainedWords"] < best["unexplainedWords"]):
                    best = evidence
        assert best is not None
        # A new coherent phrase, number or explicit negation is blocking
        # evidence. Small unexplained remnants are uncertain, not certified
        # new information and not permission to merge.
        substantial = any(run["end"] - run["start"] >= 4 for run in best["unexplainedRuns"])
        sensitive = any(re.search(r"\d|\b(?:not|no|never|without)\b", run["text"]) for run in best["unexplainedRuns"])
        return {"eventID": event["sourceEventID"], "availableAt": event["availableAt"],
                "status": "novel_causally_available_read" if substantial or sensitive else "unresolved_read_novelty",
                "boundaryEvidence": best}

    def between(self, session, onset, lower, upper, completion, application, initial_field=""):
        times = self.event_times.get(session, [])
        group = self.events.get(session, [])
        start, end = bisect_left(times, lower), bisect_left(times, upper)
        results = [self.assess(event, onset, completion, application, initial_field)
                   for event in group[start:end] if event["availableAt"] > lower]
        blocking_ids = {row["eventID"] for row in results if row["status"] in {
            "novel_causally_available_read", "unresolved_read_novelty"}}
        return results, [event for event in group[start:end] if event["sourceEventID"] in blocking_ids]
