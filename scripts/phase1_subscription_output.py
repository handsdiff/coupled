"""Versioned, offline interpretation of terminal subscription response streams.

Some LiteLLM Responses streams finish with `output: []` despite complete
`response.output_item.done` messages. Preserve those messages, not just the
terminal wrapper. Never turn a partial stream into a completed prediction.
"""
import copy
import json

VERSION = "phase1-subscription-output-v2"


def require(test, why):
    if not test:
        raise ValueError(why)


def decode(wire):
    try:
        response = json.loads(wire)
        require(isinstance(response, dict), "Non-object response")
        return response, {"source": "terminal_json_output"}
    except json.JSONDecodeError:
        pass
    terminals, items, texts, deltas = [], {}, {}, {}
    for line in wire.splitlines():
        if not line.startswith(b"data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            continue
        event = json.loads(payload)
        kind = event.get("type")
        if kind in {"response.completed", "response.incomplete", "response.failed"}:
            terminals.append(event["response"])
        elif kind == "response.output_item.done":
            index = event["output_index"]
            require(type(index) is int and index >= 0, "Invalid item index")
            require(index not in items or items[index] == event["item"], "Conflicting completed output item")
            items[index] = event["item"]
        elif kind in {"response.output_text.done", "response.output_text.delta"}:
            key = (event["output_index"], event["content_index"])
            if kind.endswith(".done"):
                require(key not in texts or texts[key] == event["text"], "Conflicting completed text")
                texts[key] = event["text"]
            else:
                deltas[key] = deltas.get(key, "") + event["delta"]
    require(len(terminals) == 1, "Exactly one terminal response required; deltas alone are not completion")
    response = copy.deepcopy(terminals[0])
    output = response.get("output") or []
    if output:
        for index, item in items.items():
            require(index < len(output), "Stream item absent from populated terminal output")
            # Provider wrappers may omit annotations/extra metadata. Verify
            # identity, output kind and complete visible payload, not wrappers.
            a, b = output[index], item
            require(a.get("id") == b.get("id") and a.get("type") == b.get("type"), "Output identity mismatch")
            if item.get("type") == "message":
                require(visible(a) == visible(b), "Terminal and completed stream message disagree")
        source = "terminal_sse_output"
    else:
        require(not items or sorted(items) == list(range(max(items) + 1)), "Completed output-item sequence has holes")
        output = [items[i] for i in sorted(items)]
        response["output"] = output
        source = "sse_output_item_done" if items else "terminal_sse_empty"
    proven = {}
    for index, item in enumerate(output):
        if item.get("type") != "message":
            continue
        for content_index, part in enumerate(item.get("content", [])):
            if part.get("type") == "output_text":
                proven[(index, content_index)] = part["text"]
    for key, text in texts.items():
        require(proven.get(key) == text, "Text-done lacks matching completed message")
    for key, text in deltas.items():
        require(proven.get(key) == text, "Stream deltas disagree with completed message")
    return response, {"source": source, "completedItems": len(items),
                      "completedTextParts": len(texts), "deltaPartsVerified": len(deltas)}


def visible(item):
    return [(p.get("type"), p.get("text"), p.get("refusal")) for p in item.get("content", [])]


def interpret(wire, requested_model):
    response, evidence = decode(wire)
    require(response.get("model") in {requested_model, requested_model.removeprefix("chatgpt/")}, "Model mismatch")
    require(response.get("reasoning", {}).get("effort") == "xhigh", "Reasoning effort mismatch")
    require(response.get("status") in {"completed", "incomplete", "failed"}, "Response not terminal")
    require(isinstance(response.get("usage"), dict), "Final usage absent")
    text, refusals, unexpected = [], [], []
    for item in response.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    text.append(content["text"])
                elif content.get("type") == "refusal":
                    refusals.append(content.get("refusal", ""))
                else:
                    unexpected.append(content.get("type"))
        elif item.get("type") != "reasoning":
            unexpected.append(item.get("type"))
    return {"prediction": "".join(text), "refusals": refusals, "unexpectedOutputTypes": unexpected,
        "validCompletion": bool(response["status"] == "completed" and not response.get("error") and not refusals and not unexpected),
        "responseInterpretation": {"version": VERSION, **evidence}}
