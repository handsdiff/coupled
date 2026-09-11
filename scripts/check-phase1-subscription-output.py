#!/usr/bin/env python3
"""Sanitized regression for actual pilot stream wrappers; no provider calls."""
import copy
import json
from phase1_subscription_output import interpret


def wire(events):
    return b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events)


def main():
    model = "chatgpt/gpt-6-astra"
    item = {"id": "msg_public", "type": "message", "role": "assistant", "status": "completed",
            "phase": "final_answer", "content": [{"type": "output_text", "text": "public prediction"}]}
    terminal = {"model": "gpt-6-astra", "status": "completed", "error": None,
                "reasoning": {"effort": "xhigh"}, "usage": {}, "output": []}
    events = [
        {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "public "},
        {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "prediction"},
        {"type": "response.output_text.done", "output_index": 0, "content_index": 0, "text": "public prediction"},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": terminal}]
    output = interpret(wire(events), model)
    assert output["prediction"] == "public prediction" and output["validCompletion"]
    assert output["responseInterpretation"]["source"] == "sse_output_item_done"
    full = copy.deepcopy(events)
    full[-1]["response"]["output"] = [item]
    assert interpret(wire(full), model)["prediction"] == "public prediction"
    assert interpret(json.dumps(full[-1]["response"]).encode(), model)["prediction"] == "public prediction"
    empty = interpret(wire([events[-1]]), model)
    assert empty["prediction"] == "" and empty["validCompletion"]
    cases = [events[:-1], events[:3] + events[-1:], copy.deepcopy(full), copy.deepcopy(events)]
    cases[2][-1]["response"]["output"][0]["content"][0]["text"] = "conflict"
    cases[3][0]["delta"] = "conflicting "
    for case in cases:
        try:
            interpret(wire(case), model)
        except ValueError:
            pass
        else:
            raise AssertionError("Incomplete or conflicting output accepted")
    incomplete = copy.deepcopy(events)
    incomplete[-1]["response"]["status"] = "incomplete"
    assert not interpret(wire(incomplete), model)["validCompletion"]
    print("PASS: terminal-empty-wrapper recovery, full output unchanged, delta agreement, genuine empty retained, partial/conflicting stream rejected")


if __name__ == "__main__":
    main()
