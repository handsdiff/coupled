#!/usr/bin/env python3
"""Wait for the frontier arc, then run GPT-5.6 context arms strictly sequentially."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

from phase1_context_window_ablation import ContextWindowError, atomic_json, sha256


SEQUENCE_VERSION = "phase1-gpt56-context-window-sequence-v1"


def iso8601() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def predecessor_complete(
    runs: Path, audit_root: Path, predecessor_plan_sha256: str
) -> tuple[bool, dict]:
    state = {"models": {}, "audit": None}
    for key in ("gpt-5.4-xhigh", "gpt-5.5-xhigh"):
        path = runs / key / "model.json"
        if not path.is_file():
            state["models"][key] = "absent"
            continue
        value = json.loads(path.read_text(encoding="utf-8"))
        state["models"][key] = value.get("status")
    for audit_path in audit_root.rglob("arc.json") if audit_root.exists() else []:
        value = json.loads(audit_path.read_text(encoding="utf-8"))
        if value.get("source", {}).get("planSHA256") == predecessor_plan_sha256:
            state["audit"] = {
                "path": str(audit_path),
                "status": value.get("status"),
            }
            break
    complete = (
        set(state["models"].values()) == {"complete"}
        and isinstance(state["audit"], dict)
        and state["audit"]["status"]
        in {"passed", "passed_with_unavailable_requested_models"}
    )
    return complete, state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--context-packs", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--predecessor-runs", required=True, type=Path)
    parser.add_argument("--predecessor-audit-root", required=True, type=Path)
    parser.add_argument("--predecessor-plan-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--audit-output", required=True, type=Path)
    parser.add_argument("--endpoint", default="http://127.0.0.1:4000/v1/responses")
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--confirm-personal-data-transfer", action="store_true")
    parser.add_argument("--confirm-subscription-usage", action="store_true")
    parser.add_argument("--execute", action="store_true")
    arguments = parser.parse_args()
    for value, flag in (
        (arguments.confirm_personal_data_transfer, "--confirm-personal-data-transfer"),
        (arguments.confirm_subscription_usage, "--confirm-subscription-usage"),
        (arguments.execute, "--execute"),
    ):
        if not value:
            parser.error(f"{flag} is required")
    if arguments.poll_seconds < 5:
        parser.error("--poll-seconds must be at least 5")
    plan_path = arguments.plan.expanduser().resolve()
    if sha256(plan_path) != arguments.plan_sha256:
        raise ContextWindowError("plan SHA-256 differs from sequence authorization")
    output = arguments.output.expanduser().resolve()
    audit_output = arguments.audit_output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    sequence_path = output / "sequence.json"
    state = {
        "schemaVersion": 1,
        "sequenceVersion": SEQUENCE_VERSION,
        "status": "waiting_for_frontier_arc",
        "startedAt": iso8601(),
        "planSHA256": arguments.plan_sha256,
        "requiredOrder": [
            "gpt-5.4-xhigh",
            "gpt-5.5-xhigh",
            "frontier-model-arc-audit",
            "gpt-5.6-sol-xhigh-8k",
            "gpt-5.6-sol-xhigh-16k",
            "gpt-5.6-sol-xhigh-64k",
            "context-window-audit",
        ],
    }
    atomic_json(sequence_path, state)
    predecessor_runs = arguments.predecessor_runs.expanduser().resolve()
    predecessor_audit_root = arguments.predecessor_audit_root.expanduser().resolve()
    last_observed = None
    while True:
        complete, observed = predecessor_complete(
            predecessor_runs,
            predecessor_audit_root,
            arguments.predecessor_plan_sha256,
        )
        if observed != last_observed:
            state["predecessorStatus"] = observed
            state["lastCheckedAt"] = iso8601()
            atomic_json(sequence_path, state)
            print(f"context-sequence waiting: {observed}", flush=True)
            last_observed = observed
        if complete:
            break
        time.sleep(arguments.poll_seconds)
    state["status"] = "running_context_windows"
    state["predecessorCompletedAt"] = iso8601()
    state["predecessorArtifactsSHA256"] = {
        key: sha256(predecessor_runs / key / "model.json")
        for key in ("gpt-5.4-xhigh", "gpt-5.5-xhigh")
    }
    predecessor_audit = Path(observed["audit"]["path"])
    state["predecessorArtifactsSHA256"]["arc.json"] = sha256(predecessor_audit)
    atomic_json(sequence_path, state)
    project = Path(__file__).resolve().parent.parent
    corpus = arguments.corpus.expanduser().resolve()
    packs = arguments.context_packs.expanduser().resolve()
    for key in ("8k", "16k", "64k"):
        command = [
            sys.executable,
            str(project / "scripts/run-phase1-context-window-ablation.py"),
            "--corpus", str(corpus),
            "--packed", str(packs / key),
            "--plan", str(plan_path),
            "--plan-sha256", arguments.plan_sha256,
            "--window-key", key,
            "--output", str(output / key),
            "--endpoint", arguments.endpoint,
            "--maximum-calls", "174",
            "--confirm-personal-data-transfer",
            "--confirm-subscription-usage",
            "--execute",
        ]
        completed = subprocess.run(command, check=False)
        if completed.returncode != 0:
            state["status"] = "interrupted"
            state["failedWindow"] = key
            state["failedAt"] = iso8601()
            atomic_json(sequence_path, state)
            return completed.returncode
        state.setdefault("completedWindows", []).append(key)
        state["lastCompletedAt"] = iso8601()
        atomic_json(sequence_path, state)
    completed = subprocess.run(
        [
            sys.executable,
            str(project / "scripts/audit-phase1-context-window-ablation.py"),
            "--corpus", str(corpus),
            "--plan", str(plan_path),
            "--runs", str(output),
            "--output", str(audit_output),
        ],
        check=False,
    )
    if completed.returncode != 0:
        state["status"] = "audit_failed"
        state["failedAt"] = iso8601()
        atomic_json(sequence_path, state)
        return completed.returncode
    state["status"] = "complete"
    state["completedAt"] = iso8601()
    state["auditSHA256"] = sha256(audit_output / "context-windows.json")
    atomic_json(sequence_path, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
