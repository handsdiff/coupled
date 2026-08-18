#!/usr/bin/env python3
"""Prepare or execute a non-personal LiteLLM ChatGPT-subscription preflight."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from phase1_subscription_responses import (
    MODEL,
    REASONING_EFFORT,
    SubscriptionResponseError,
    build_request,
    request_completion,
    require_loopback_url,
)


PREFLIGHT_INPUT = "Reply with exactly the two uppercase letters OK and nothing else."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:4000/v1/responses")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    endpoint = require_loopback_url(args.endpoint)
    if args.output.exists():
        raise SubscriptionResponseError(f"output already exists: {args.output}")
    request = build_request(PREFLIGHT_INPUT)
    report = {
        "schemaVersion": 1,
        "preflightVersion": "phase1-litellm-subscription-preflight-v1",
        "status": "local_contract_only",
        "endpoint": endpoint,
        "model": MODEL,
        "reasoningEffort": REASONING_EFFORT,
        "requestSHA256": hashlib.sha256(
            json.dumps(request, sort_keys=True).encode()
        ).hexdigest(),
        "containsPersonalDataset": False,
        "usesOpenAIAPIKey": False,
        "tokenLimitFieldSent": False,
        "metadataFieldSent": False,
    }
    if args.execute:
        output, response = request_completion(
            endpoint,
            PREFLIGHT_INPUT,
            local_proxy_key=os.environ.get("LITELLM_PROXY_KEY"),
        )
        report.update({
            "status": "authenticated_nonpersonal_preflight_complete",
            "output": output,
            "exactOutputPassed": output.strip() == "OK",
            "responseID": response.get("id"),
            "responseModel": response.get("model"),
            "usage": response.get("usage"),
        })
        if not report["exactOutputPassed"]:
            raise SubscriptionResponseError("preflight did not return exact output OK")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {report['status']} to {args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, SubscriptionResponseError) as error:
        raise SystemExit(f"preflight-phase1-subscription: {error}")
