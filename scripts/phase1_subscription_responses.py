#!/usr/bin/env python3
"""Fail-closed client contract for subscription-backed LiteLLM Responses."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable


MODEL = "chatgpt/gpt-5.6-sol"
REASONING_EFFORT = "xhigh"
FORBIDDEN_REQUEST_FIELDS = {
    "max_tokens", "max_output_tokens", "max_completion_tokens", "metadata"
}


class SubscriptionResponseError(ValueError):
    pass


def require_loopback_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if not (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        and parsed.path.rstrip("/") in {"/responses", "/v1/responses"}
        and not parsed.username
        and not parsed.password
    ):
        raise SubscriptionResponseError(
            "LiteLLM endpoint must be loopback HTTP ending in /responses"
        )
    return value


def build_request(model_input: str) -> dict[str, Any]:
    if not isinstance(model_input, str) or not model_input:
        raise SubscriptionResponseError("model input must be nonempty text")
    request = {
        "model": MODEL,
        "input": [{
            "role": "user",
            "content": [{"type": "input_text", "text": model_input}],
        }],
        "reasoning": {"effort": REASONING_EFFORT},
        "stream": True,
        "tools": [],
    }
    if FORBIDDEN_REQUEST_FIELDS & set(request):
        raise AssertionError("subscription request contains a forbidden field")
    return request


def extract_output_text(response: dict[str, Any]) -> str:
    direct = response.get("output_text")
    if isinstance(direct, str) and direct:
        return direct
    pieces: list[str] = []
    for item in response.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if content.get("type") == "output_text" and isinstance(text, str):
                pieces.append(text)
    result = "".join(pieces)
    if result:
        return result
    # A completed Responses result may legitimately contain no visible text
    # after using its tokens for reasoning.  That is an empty model prediction,
    # not a transport failure.  Fail closed unless the provider explicitly
    # reports successful completion with usage evidence.
    if (
        response.get("status") == "completed"
        and response.get("error") is None
        and isinstance(response.get("usage"), dict)
    ):
        return ""
    raise SubscriptionResponseError("LiteLLM response contains no output text")


def decode_response(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict):
        return value
    deltas: list[str] = []
    completed: dict[str, Any] | None = None
    for raw_line in body.decode(errors="replace").splitlines():
        if not raw_line.startswith("data:"):
            continue
        payload = raw_line.removeprefix("data:").strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError as error:
            raise SubscriptionResponseError("LiteLLM returned malformed SSE JSON") from error
        if not isinstance(event, dict):
            continue
        if event.get("type") == "response.output_text.delta" and isinstance(
            event.get("delta"), str
        ):
            deltas.append(event["delta"])
        if event.get("type") == "response.completed" and isinstance(
            event.get("response"), dict
        ):
            completed = event["response"]
    if completed is not None:
        if deltas and not isinstance(completed.get("output_text"), str):
            completed = {**completed, "output_text": "".join(deltas)}
        return completed
    if deltas:
        return {"output_text": "".join(deltas)}
    raise SubscriptionResponseError("LiteLLM response is neither JSON nor recognized SSE")


def request_completion(
    endpoint: str,
    model_input: str,
    local_proxy_key: str | None = None,
    timeout_seconds: float = 300.0,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
) -> tuple[str, dict[str, Any]]:
    endpoint = require_loopback_url(endpoint)
    payload = build_request(model_input)
    headers = {"Content-Type": "application/json"}
    if local_proxy_key:
        headers["Authorization"] = f"Bearer {local_proxy_key}"
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False, sort_keys=True).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = decode_response(response.read())
    except urllib.error.URLError as error:
        raise SubscriptionResponseError(f"LiteLLM request failed: {error}") from error
    if not isinstance(body, dict):
        raise SubscriptionResponseError("LiteLLM response is not an object")
    return extract_output_text(body), body
