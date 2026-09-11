#!/usr/bin/env python3
"""No-network tests for exact reuse and shared semantic-context reconstruction."""
import importlib.util
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("prep", Path(__file__).with_name("prepare-phase1-frontier-completion.py"))
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


def rejected(fn):
    try:
        fn()
    except AssertionError:
        return
    raise AssertionError("Invalid provenance accepted")


def main():
    with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("No network")):
        event = {"serialized": "earlier READ", "availableAt": "2026-09-02T12:00:00Z"}
        ref = {"eventID": "read1", "serializedSHA256": p.text_hash(event["serialized"]),
               "availableAt": event["availableAt"]}
        text = p.reconstruct("instruction", "query", [ref], {"read1": event})
        assert text == "instruction\nearlier READ\nquery"
        rejected(lambda: p.reconstruct("instruction", "query", [ref], {"read1": event | {"serialized": "tampered"}}))
        rejected(lambda: p.reconstruct("instruction", "query", [ref], {"read1": event | {"availableAt": "later"}}))
        override = ref | {"serializedOverride": "novel READ", "serializedSHA256": p.text_hash("novel READ")}
        assert p.reconstruct("instruction", "query", [override], {"read1": event}) == "instruction\nnovel READ\nquery"
        for model in p.MODELS:
            body = p.body(model, text)
            assert body["tools"] == [] and body["reasoning"] == {"effort": "xhigh"}
            request = {"exampleID": "a", "model": model, "variant": "new", "reasoningEffort": "xhigh",
                       "promptID": "bound-prompt", "requestSHA256": p.fingerprint(body)}
            result = request | {"responseModel": model.removeprefix("chatgpt/"), "prediction": "answer"}
            prompt = {"modelInput": text, "query": "query"}
            example = {"query": "query"}
            assert p.eligible_reuse(request, result, prompt, example) == result
            for key, value in (("model", "other"), ("requestSHA256", "other"), ("promptID", "other"),
                               ("responseModel", "other"), ("reasoningEffort", "high")):
                rejected(lambda: p.eligible_reuse(request, result | {key: value}, prompt, example))
            rejected(lambda: p.eligible_reuse(request, result, prompt | {"modelInput": text + " "}, example))
            rejected(lambda: p.eligible_reuse(request, result, prompt, {"query": "different"}))
        rejected(lambda: p.body("chatgpt/other-model", "query"))
    print("PASS: three exact model routes, context/override reconstruction, request/query/model/effort/timing tamper rejection; zero provider calls")


if __name__ == "__main__":
    main()
