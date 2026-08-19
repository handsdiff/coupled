#!/usr/bin/env python3
"""No-provider regression checks for the LiteLLM subscription request contract."""

from __future__ import annotations

import json
from phase1_subscription_responses import (
    FORBIDDEN_REQUEST_FIELDS,
    MODEL,
    SubscriptionResponseError,
    decode_response,
    request_completion,
    require_loopback_url,
)


class MockResponse:
    def __init__(self) -> None:
        self.body = json.dumps({
            "id": "mock-response",
            "model": MODEL,
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": "OK"}],
            }],
        }).encode()

    def __enter__(self) -> "MockResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class MockCompletedEmptyResponse(MockResponse):
    def __init__(self) -> None:
        self.body = json.dumps({
            "id": "mock-empty-response",
            "model": MODEL,
            "status": "completed",
            "error": None,
            "output": [],
            "usage": {
                "input_tokens": 12,
                "output_tokens": 7,
                "total_tokens": 19,
            },
        }).encode()


def main() -> int:
    received: dict = {}

    def mock_urlopen(request: object, timeout: float) -> MockResponse:
        del timeout
        received.update(json.loads(request.data))
        return MockResponse()

    output, _ = request_completion(
        "http://127.0.0.1:4000/v1/responses",
        "public fixture",
        urlopen=mock_urlopen,
    )
    if output != "OK":
        raise AssertionError("mocked subscription output was not parsed")
    empty, empty_response = request_completion(
        "http://127.0.0.1:4000/v1/responses",
        "public empty fixture",
        urlopen=lambda request, timeout: MockCompletedEmptyResponse(),
    )
    if empty != "" or empty_response.get("status") != "completed":
        raise AssertionError("completed empty prediction was not preserved")
    incomplete = {
        "status": "incomplete",
        "error": None,
        "output": [],
        "usage": {"input_tokens": 12, "output_tokens": 7, "total_tokens": 19},
    }
    try:
        from phase1_subscription_responses import extract_output_text
        extract_output_text(incomplete)
    except SubscriptionResponseError:
        pass
    else:
        raise AssertionError("incomplete empty response was accepted")
    if received.get("model") != MODEL:
        raise AssertionError("subscription model route changed")
    if received.get("input") != [{
        "role": "user",
        "content": [{"type": "input_text", "text": "public fixture"}],
    }]:
        raise AssertionError("subscription Responses input shape changed")
    if received.get("reasoning") != {"effort": "xhigh"}:
        raise AssertionError("subscription reasoning effort changed")
    if FORBIDDEN_REQUEST_FIELDS & set(received):
        raise AssertionError("subscription request contains a rejected field")
    if received.get("stream") is not True:
        raise AssertionError("subscription request is not explicitly streamed")
    sse = b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"O"}\n\nevent: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"K"}\n\ndata: [DONE]\n\n'
    if decode_response(sse).get("output_text") != "OK":
        raise AssertionError("subscription SSE output was not parsed")
    try:
        require_loopback_url("https://example.com/v1/responses")
    except SubscriptionResponseError:
        pass
    else:
        raise AssertionError("non-loopback provider endpoint was accepted")
    print("Phase 1 subscription Responses checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
