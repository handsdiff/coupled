#!/usr/bin/env python3
"""No-network failure/resume tests for the frozen four-arm executor."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from phase1_read_model_comparison import fingerprint, request_plan

spec = importlib.util.spec_from_file_location("executor", Path(__file__).with_name("run-phase1-read-model-comparison.py"))
ex = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ex)
PRICES = {m: {"input": 10, "cachedInput": 1, "output": 50} for m in ex.MODELS}


def rejected(fn, why):
    try:
        fn()
    except (ValueError, OSError, KeyError):
        return
    raise AssertionError(why)


def response(model, text="hello", status="completed"):
    return {"id": "synthetic-response", "model": model.removeprefix("chatgpt/"),
        "status": status, "reasoning": {"effort": "xhigh"}, "error": None,
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
            "input_tokens_details": {"cached_tokens": 30}, "output_tokens_details": {"reasoning_tokens": 10}}}


def persist(attempt, value, http=200, sse=True):
    wire = (b'data: {"type":"response.output_text.delta","delta":"DO NOT USE THIS INSTEAD OF FINAL"}\n\n' +
            b'data: ' + json.dumps({"type": "response." + value.get("status", "completed"), "response": value}).encode() + b'\n\n') if sse else json.dumps(value).encode()
    (attempt / "response.body").write_bytes(wire)
    ex.atomic_json(attempt / "transport.json", {"httpStatus": http,
        "bodySHA256": ex.file_hash(attempt / "response.body"), "dispatchToCompletionSeconds": 1.25,
        "timeToFirstOutputTextSeconds": 0.4})


def main():
    cohort = [{"exampleID": "fixture-1"}, {"exampleID": "fixture-2"}]
    prompts = [{"exampleID": e["exampleID"], "variant": v, "promptID": e["exampleID"] + v,
                "modelInput": "public synthetic " + e["exampleID"] + v} for e in cohort for v in ("old", "new")]
    requests = request_plan(prompts, cohort, 17)
    lookup = {p["promptID"]: p for p in prompts}
    calls = []
    def send(body, attempt, timeout):
        assert (attempt / "inflight.json").is_file(), "Request sent before durable marker"
        assert set(body) == {"model", "input", "reasoning", "tools", "stream"}
        assert body["tools"] == [] and body["reasoning"] == {"effort": "xhigh"}
        calls.append(body)
        persist(attempt, response(body["model"]))
    with tempfile.TemporaryDirectory(prefix="coupled-factorial-executor-") as folder, \
            patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Network forbidden")), \
            patch("subprocess.Popen", side_effect=AssertionError("Provider launch forbidden")):
        root = Path(folder)
        run = lambda path, sender=send, limit=None, plan="bound-plan": ex.run_requests(
            requests, lookup, path, plan, PRICES, 30, sender=sender, limit=limit)
        first = run(root / "complete", limit=4)
        assert len(first) == len(calls) == 4
        full = run(root / "complete")
        assert len(full) == len(calls) == 8
        assert all(r["prediction"] == "hello" for r in full)
        assert full[0]["apiEquivalentCostUSD"] == (70 * 10 + 30 + 20 * 50) / 1e6
        assert run(root / "complete") == full and len(calls) == 8
        rejected(lambda: run(root / "complete", plan="different-plan"), "Plan drift accepted")
        # Terminal response was durable, but no result was saved: rederive, don't resend.
        (root / "complete/0003/result.json").unlink()
        assert run(root / "complete") == full and len(calls) == 8
        changed = copy.deepcopy(full[0]); changed["prediction"] = "tampered"
        ex.atomic_json(root / "complete/0000/result.json", changed)
        rejected(lambda: run(root / "complete"), "Result tampering accepted")
        def crash(body, attempt, timeout):
            calls.append(body)
            raise OSError("Synthetic crash after dispatch")
        rejected(lambda: run(root / "uncertain", crash), "Crash ignored")
        count = len(calls)
        rejected(lambda: run(root / "uncertain"), "Uncertain request replayed")
        assert len(calls) == count
        def quota(body, attempt, timeout):
            calls.append(body)
            persist(attempt, {"error": {"message": "synthetic quota"}}, http=429, sse=False)
        rejected(lambda: run(root / "quota", quota), "Quota error ignored")
        count = len(calls)
        rejected(lambda: run(root / "quota"), "Quota automatically retried")
        assert len(calls) == count
        def failed(body, attempt, timeout):
            calls.append(body)
            value = response(body["model"], "", "failed")
            value["error"] = {"code": "synthetic_provider_failure"}
            persist(attempt, value)
        rejected(lambda: run(root / "failed", failed), "HTTP-200 provider failure did not pause")
        assert (root / "failed/0000/result.json").exists()
        count = len(calls)
        rejected(lambda: run(root / "failed"), "Provider failure replayed or silently bypassed")
        assert len(calls) == count
        sample = root / "sample"; sample.mkdir()
        req = requests[0]
        derive = lambda: ex.derive_result(req, sample, PRICES)
        value = response(req["model"], "")
        persist(sample, value)
        assert derive()["prediction"] == "" and derive()["validCompletion"]
        value["output"] = [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]
        persist(sample, value)
        assert not derive()["validCompletion"] and derive()["refusals"] == ["no"]
        value = response(req["model"], "partial", "incomplete")
        persist(sample, value)
        assert not derive()["validCompletion"] and derive()["prediction"] == "partial"
        for change in ({"model": "wrong-model"}, {"reasoning": {"effort": "high"}}, {"usage": None}):
            persist(sample, response(req["model"]) | change)
            rejected(derive, "Invalid returned model/effort/usage accepted")
        partial = b'data: {"type":"response.output_text.delta","delta":"hello"}\n\n'
        rejected(lambda: ex.parse_terminal(partial), "Partial stream accepted")
        persist(sample, response(req["model"]))
        (sample / "response.body").write_bytes(b"tampered")
        rejected(derive, "Wire tampering accepted")
        bad_req = requests[0] | {"requestSHA256": "tampered"}
        rejected(lambda: ex.payload(bad_req, lookup[bad_req["promptID"]]), "Request tampering accepted")
        value = response(req["model"])
        value["usage"]["input_tokens_details"]["cache_write_tokens"] = 10
        persist(sample, value)
        assert derive()["apiEquivalentCostUSD"] is None and derive()["costCaveat"]
        rejected(lambda: ex.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com"), "Redirect allowed")
        transport_dir = root / "transport"; transport_dir.mkdir()
        body = ex.payload(req, lookup[req["promptID"]])
        wire = b'data: {"type":"response.output_text.delta","delta":"hello"}\n\n' + b'data: ' + json.dumps({
            "type": "response.completed", "response": response(req["model"])}).encode() + b'\n\n'
        class FakeHTTP(io.BytesIO):
            status = 200
        class FakeOpener:
            def open(self, actual, timeout):
                assert json.loads(actual.data) == body
                assert actual.full_url == ex.ENDPOINT
                assert "Authorization" not in actual.headers
                return FakeHTTP(wire)
        with patch("urllib.request.build_opener", return_value=FakeOpener()):
            ex.transport(body, transport_dir, 30)
        timed = ex.derive_result(req, transport_dir, PRICES)
        assert timed["prediction"] == "hello"
        assert 0 <= timed["timing"]["timeToFirstOutputTextSeconds"] <= timed["timing"]["dispatchToCompletionSeconds"]
    print(json.dumps({"status": "passed", "syntheticFourArmRequests": 8, "providerCalls": 0,
        "gates": ["exact request/model/effort", "durable before dispatch", "resumes without replay",
                  "terminal response recovery", "uncertain/quota pause", "partial SSE rejected",
                  "tamper rejection", "invalid/empty retained", "cache and reasoning accounting", "no redirects"]}))


if __name__ == "__main__":
    main()
