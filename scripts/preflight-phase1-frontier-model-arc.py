#!/usr/bin/env python3
"""Run and retain non-personal subscription preflights for frontier-model routes."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time
from pathlib import Path

from phase1_frontier_model_arc import MODEL_SPECS, PLAN_VERSION, atomic_json, sha256
from phase1_subscription_responses import (
    SubscriptionResponseError,
    request_completion,
    require_loopback_url,
)


PREFLIGHT_INPUT = "Reply with exactly the two uppercase letters OK and nothing else."


def iso8601() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--endpoint", default="http://127.0.0.1:4000/v1/responses")
    parser.add_argument("--execute", action="store_true")
    arguments = parser.parse_args()
    plan_path = arguments.plan.expanduser().resolve()
    output = arguments.output.expanduser().resolve()
    endpoint = require_loopback_url(arguments.endpoint)
    if not arguments.execute:
        parser.error("--execute is required")
    if output.exists():
        raise RuntimeError(f"output already exists: {output}")
    if sha256(plan_path) != arguments.plan_sha256:
        raise RuntimeError("plan SHA-256 differs")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("planVersion") != PLAN_VERSION or plan.get("models") != list(MODEL_SPECS):
        raise RuntimeError("preflight plan differs from model contract")
    results = []
    for spec in MODEL_SPECS:
        started = time.monotonic()
        try:
            prediction, response = request_completion(
                endpoint,
                PREFLIGHT_INPUT,
                local_proxy_key=os.environ.get("LITELLM_PROXY_KEY"),
                model=spec["route"],
                reasoning_effort=spec["reasoningEffort"],
            )
            result = {
                "model": spec,
                "status": "passed" if prediction.strip() == "OK" else "failed_output",
                "output": prediction,
                "responseModel": response.get("model"),
                "responseID": response.get("id"),
                "latencySeconds": time.monotonic() - started,
                "usage": response.get("usage") or {},
            }
            if result["responseModel"] != spec["requestedModel"]:
                result["status"] = "failed_model_identity"
        except SubscriptionResponseError as error:
            result = {
                "model": spec,
                "status": "unsupported_or_provider_rejected",
                "error": str(error),
                "latencySeconds": time.monotonic() - started,
            }
        results.append(result)
        print(
            f"preflight {spec['key']} status={result['status']} "
            f"resolved={result.get('responseModel')}",
            flush=True,
        )
    report = {
        "schemaVersion": 1,
        "preflightVersion": "phase1-frontier-model-arc-preflight-v1",
        "status": "passed" if len(results) == len(MODEL_SPECS) and all(
            value["status"] == "passed" for value in results
        ) else "failed",
        "completedAt": iso8601(),
        "containsPersonalDataset": False,
        "usesOpenAIAPIKey": False,
        "planSHA256": arguments.plan_sha256,
        "endpoint": endpoint,
        "results": results,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(output, report)
    print(f"Wrote model-arc preflight to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
