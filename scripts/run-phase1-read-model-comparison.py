#!/usr/bin/env python3
"""Frozen, serial subscription executor for the reviewed READ/model factorial.

`prepare` and `audit` are local. Only `run --authorize-plan-sha256 ...` can send
data. The runner owns a dedicated loopback proxy; it never uses an API key or
an already-running proxy with unknown configuration. No automatic retransmits.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from phase1_read_model_comparison import (
    MODELS, VERSION as DATA_VERSION, canonical, file_hash, fingerprint,
    request_plan, rows,
)

VERSION = "phase1-read-model-executor-v1"
ENDPOINT = "http://127.0.0.1:4000/v1/responses"
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
PROXY = {
    "model_list": [{"model_name": m, "litellm_params": {"model": m}} for m in MODELS],
    "router_settings": {"num_retries": 0, "fallbacks": []},
    "litellm_settings": {"num_retries": 0, "callbacks": [],
                         "success_callback": [], "failure_callback": []},
}


def require(test, message):
    if not test:
        raise ValueError(message)


def now():
    return datetime.now(timezone.utc).isoformat()


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".pending")
    with temporary.open("w") as out:
        os.chmod(temporary, 0o600)
        out.write(canonical(value) + "\n")
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)
    sync_dir(path.parent)


def read_json(path):
    return json.loads(Path(path).read_text())


def verify_files(base, hashes):
    for name, digest in hashes.items():
        require(file_hash(Path(base) / name) == digest, f"Bound file changed: {name}")


def payload(request, prompt):
    require(request["model"] in MODELS and request["reasoningEffort"] == "xhigh", "Unexpected model/effort")
    require(all(request[k] == prompt[k] for k in ("exampleID", "promptID", "variant")), "Prompt identity mismatch")
    body = {"model": request["model"], "input": [{"role": "user", "content": [
        {"type": "input_text", "text": prompt["modelInput"]}]}],
        "reasoning": {"effort": "xhigh"}, "tools": [], "stream": True}
    require(fingerprint(body) == request["requestSHA256"], "Frozen request hash mismatch")
    return body


def load_data(folder, expected_hash=None):
    folder = Path(folder)
    hashes = read_json(folder / "artifact-hashes.json")
    if expected_hash:
        require(file_hash(folder / "plan.json") == expected_hash, "Reviewed plan changed")
    verify_files(folder, hashes)
    plan = read_json(folder / "plan.json")
    require(plan["version"] == DATA_VERSION and plan["models"] == list(MODELS), "Unknown factorial plan")
    require(plan["providerContract"]["endpoint"] == ENDPOINT, "Unexpected endpoint")
    require(read_json(folder / "audit.json")["status"] == "passed", "Data audit did not pass")
    prompts = list(rows(folder / "prompts.jsonl"))
    cohort = list(rows(folder / "cohort.jsonl"))
    requests = list(rows(folder / "requests.planned.jsonl"))
    require(requests == request_plan(prompts, cohort, plan["selectionSeed"]), "Frozen schedule changed")
    require(len(requests) == plan["plannedPredictions"] == len(cohort) * 4, "Incomplete four-arm schedule")
    prompt_map = {p["promptID"]: p for p in prompts}
    for request in requests:
        payload(request, prompt_map[request["promptID"]])
    return plan, requests, prompt_map


def runtime_hashes(project):
    venv = project / ".build/litellm-venv"
    # Bind actual Python/native dependency code, not only version labels. Never
    # inspect the separate OAuth cache or environment credentials.
    files = [venv / "bin/litellm", venv / "bin/python"]
    for site in venv.glob("lib/python*/site-packages"):
        files.extend(p for p in site.rglob("*") if p.is_file() and
                     (p.suffix in {".py", ".so", ".dylib"} or p.name in {"METADATA", "entry_points.txt"}))
    require(len(files) > 100, "LiteLLM runtime not installed")
    return {str(p.absolute()): file_hash(p) for p in sorted(files)}


def prepare(frozen, output, project):
    data, _, _ = load_data(frozen)
    require(not output.exists(), "Use a fresh executor directory")
    output.mkdir(mode=0o700, parents=True)
    code = output / "code"
    code.mkdir()
    names = [Path(__file__).name, "phase1_read_model_comparison.py"]
    for name in names:
        shutil.copy2(Path(__file__).parent / name, code / name)
    runtime = runtime_hashes(project)
    # Confirm the exact provider transformation and native preamble reviewed
    # during data preparation are still installed.
    bound_provider = {p: h for p, h in data["sourceHashes"].items() if "/llms/chatgpt/" in p}
    require(len(bound_provider) >= 2, "Missing frozen provider implementation")
    verify_files(Path("/"), bound_provider)
    common = next(Path(p) for p in bound_provider if p.endswith("common_utils.py"))
    tree = ast.parse(common.read_text())
    instructions = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "CHATGPT_DEFAULT_INSTRUCTIONS" for t in n.targets))
    atomic_json(output / "proxy.json", PROXY)
    plan = {
        "version": VERSION, "dataPath": str(frozen.resolve()),
        "dataPlanSHA256": file_hash(frozen / "plan.json"),
        "dataArtifactIndexSHA256": file_hash(frozen / "artifact-hashes.json"),
        "codeSHA256": {n: file_hash(code / n) for n in names},
        "runtimeSHA256": runtime, "project": str(project.resolve()),
        "repositoryRevisionAtPreparation": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=project, text=True).strip(),
        "implementationPolicy": "isolated code snapshot and exact runtime hashes; concurrent reducer edits do not change execution",
        "nativePreambleSHA256": hashlib.sha256(instructions.encode()).hexdigest(),
        "proxySHA256": file_hash(output / "proxy.json"), "endpoint": ENDPOINT,
        "pythonVersion": sys.version, "plannedPredictions": data["plannedPredictions"],
        "measurementContract": data["measurementContract"],
        "requestTimeoutSeconds": 1800, "concurrency": 1, "automaticRetries": 0,
        "responseByteLimit": MAX_RESPONSE_BYTES,
        "recovery": "recover durable terminal response locally; uncertain calls pause without retransmission",
        "invalidOrEmptyPredictions": "retained, never filtered or automatically retried",
        "modelRevision": "unverified: exact requested/returned model name checked, server weights not pinned",
        "authorization": "run requires exact executor plan SHA256; prepare sends nothing",
        "training": False, "apiKeyFallback": False,
    }
    atomic_json(output / "executor-plan.json", plan)
    return {"status": "prepared_not_executed", "planSHA256": file_hash(output / "executor-plan.json"),
            "plannedPredictions": plan["plannedPredictions"], "personalProviderCalls": 0}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Proxy redirect refused; no external endpoint fallback")


def transport(body, attempt, timeout):
    """Persist wire evidence and timings before parsing. No credentials loaded."""
    started = time.monotonic()
    request = urllib.request.Request(ENDPOINT, data=canonical(body).encode(),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"}, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    first_byte = first_text = None
    size = 0
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error  # retain quota/auth/provider error body, then pause
    with response, (attempt / "response.body").open("xb") as out:
        os.chmod(attempt / "response.body", 0o600)
        while line := response.readline(64 * 1024):
            elapsed = time.monotonic() - started
            if first_byte is None:
                first_byte = elapsed
            out.write(line)
            size += len(line)
            if first_text is None and line.startswith(b"data:"):
                try:
                    event = json.loads(line[5:])
                    if event.get("type") == "response.output_text.delta" and event.get("delta"):
                        first_text = elapsed
                except (ValueError, AttributeError):
                    pass
            require(size <= MAX_RESPONSE_BYTES and elapsed <= timeout, "Response size/deadline guard reached")
        out.flush()
        os.fsync(out.fileno())
        status = response.status
    # A completed transport record is the durable recovery point. If the
    # process dies earlier, never infer that re-sending is safe.
    atomic_json(attempt / "transport.json", {"httpStatus": status,
        "bodySHA256": file_hash(attempt / "response.body"),
        "dispatchToCompletionSeconds": time.monotonic() - started,
        "timeToFirstWireByteSeconds": first_byte,
        "timeToFirstOutputTextSeconds": first_text,
        "firstTextTimingSource": "SSE delta" if first_text is not None else "not exposed",
        "completedAt": now()})


def parse_terminal(wire):
    try:
        value = json.loads(wire)
        require(isinstance(value, dict), "Non-object response")
        return value
    except json.JSONDecodeError:
        pass
    terminals = []
    for line in wire.splitlines():
        if not line.startswith(b"data:") or line[5:].strip() == b"[DONE]":
            continue
        event = json.loads(line[5:])
        if event.get("type") in {"response.completed", "response.incomplete", "response.failed"}:
            terminals.append(event["response"])
    require(len(terminals) == 1, "Stream lacks one authoritative terminal response; partial text is not completion")
    return terminals[0]


def derive_result(request, attempt, prices):
    timing = read_json(attempt / "transport.json")
    require(file_hash(attempt / "response.body") == timing["bodySHA256"], "Response evidence changed")
    require(timing["httpStatus"] == 200, "Provider HTTP error retained; pause without retry")
    response = parse_terminal((attempt / "response.body").read_bytes())
    require(response.get("model") in {request["model"], request["model"].removeprefix("chatgpt/")}, "Returned model mismatch")
    require(response.get("reasoning", {}).get("effort") == "xhigh", "Returned reasoning effort not verified")
    require(response.get("status") in {"completed", "incomplete", "failed"}, "Non-terminal provider status")
    usage = response.get("usage")
    require(isinstance(usage, dict), "Missing final usage")
    for k in ("input_tokens", "output_tokens", "total_tokens"):
        require(type(usage.get(k)) is int and usage[k] >= 0, f"Invalid usage: {k}")
    require(usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"], "Usage total mismatch")
    cached = usage.get("input_tokens_details", {}).get("cached_tokens")
    reasoned = usage.get("output_tokens_details", {}).get("reasoning_tokens")
    if cached is not None:
        require(type(cached) is int and 0 <= cached <= usage["input_tokens"], "Invalid cache usage")
    if reasoned is not None:
        require(type(reasoned) is int and 0 <= reasoned <= usage["output_tokens"], "Invalid reasoning usage")
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
    prediction = "".join(text)
    valid = response["status"] == "completed" and not response.get("error") and not refusals and not unexpected
    rates = prices[request["model"]]
    def cost(c):
        return ((usage["input_tokens"] - c) * rates["input"] + c * rates["cachedInput"]
                + usage["output_tokens"] * rates["output"]) / 1e6
    cache_writes = usage.get("input_tokens_details", {}).get("cache_write_tokens", 0)
    return {**request, "status": "recorded", "responseID": response.get("id"),
        "responseModel": response["model"], "responseStatus": response["status"],
        "prediction": prediction, "validCompletion": bool(valid),
        "refusals": refusals, "unexpectedOutputTypes": unexpected,
        "providerError": response.get("error"), "incompleteDetails": response.get("incomplete_details"),
        "usage": usage, "timing": timing,
        "apiEquivalentCostUSD": cost(cached) if cached is not None and not cache_writes else None,
        "apiEquivalentBaseRateUncachedEstimateUSD": cost(0),
        "costBasis": "frozen text-token API rates, output includes reasoning; not subscription billing",
        "cacheWriteTokens": cache_writes,
        "costCaveat": "separate cache-write rates not frozen; report requires recomputation" if cache_writes else None,
        "serverModelRevision": "unverified"}


def run_requests(requests, prompts, output, execution_hash, prices, timeout, sender=transport, limit=None):
    """One durable attempt per planned request. Existing results are rederived."""
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    results = []
    with (output / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        saved = sorted(p.name for p in output.iterdir() if p.is_dir())
        require(saved == [f"{i:04d}" for i in range(len(saved))], "Attempt journal has a gap or unexpected directory")
        require(len(saved) <= len(requests), "Journal exceeds planned request count")
        if (output / "progress.json").exists():
            progress = read_json(output / "progress.json")
            require(progress["executorPlanSHA256"] == execution_hash and progress["completed"] <= len(saved),
                    "Journal lost previously recorded attempts or plan changed")
        for request in requests:
            body = payload(request, prompts[request["promptID"]])
            attempt = output / f'{request["requestOrdinal"]:04d}'
            marker = {"executorPlanSHA256": execution_hash, "request": request}
            if attempt.exists():
                require(read_json(attempt / "inflight.json")["binding"] == marker, "Attempt binding changed")
                require((attempt / "transport.json").exists(), "Uncertain in-flight request: manual review required, never auto-replayed")
            else:
                if limit is not None and len(results) >= limit:
                    break
                attempt.mkdir(mode=0o700)
                atomic_json(attempt / "inflight.json", {"binding": marker, "beganAt": now()})
                sync_dir(output)
                try:
                    sender(body, attempt, timeout)
                except BaseException as error:
                    atomic_json(attempt / "interruption.json", {"at": now(), "exceptionType": type(error).__name__,
                        "disposition": "pause; no automatic retransmission"})
                    raise
            # Recover a terminal response even if interruption happened between
            # transport persistence and result persistence. No new model call.
            result = derive_result(request, attempt, prices)
            if (attempt / "result.json").exists():
                require(read_json(attempt / "result.json") == result, "Stored result differs from raw response")
            else:
                atomic_json(attempt / "result.json", result)
            results.append(result)
            atomic_json(output / "progress.json", {"completed": len(results), "planned": len(requests),
                "lastRequestOrdinal": request["requestOrdinal"], "executorPlanSHA256": execution_hash})
            print(f'Recorded {len(results)}/{len(requests)}: {request["model"]} / {request["variant"]}', flush=True)
            require(not result["providerError"] and result["responseStatus"] != "failed",
                    "Provider failure retained; pause for review instead of sending more requests")
    return results


@contextmanager
def owned_proxy(plan, directory):
    # Fail if port 4000 is occupied. Never replace an unrelated live process.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 4000))
    project = Path(plan["project"])
    auth = project / ".build/litellm-chatgpt-auth"
    require((auth / "auth.json").is_file(), "Subscription login absent")
    # Allowlist rather than inherit endpoint, key, callback or preamble overrides.
    env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL") if k in os.environ}
    env.update(CHATGPT_TOKEN_DIR=str(auth), LITELLM_LOCAL_MODEL_COST_MAP="true")
    command = [str(project / ".build/litellm-venv/bin/litellm"), "--config", str(directory / "proxy.json"),
               "--host", "127.0.0.1", "--port", "4000", "--num_workers", "1", "--telemetry", "False"]
    with (directory / f"proxy-{time.time_ns()}.log").open("x") as log:
        os.chmod(log.name, 0o600)
        child = subprocess.Popen(command, env=env, cwd=directory, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                require(child.poll() is None, "Proxy failed to start; inspect private log")
                try:
                    with socket.create_connection(("127.0.0.1", 4000), timeout=1):
                        break
                except OSError:
                    time.sleep(0.25)
            else:
                raise ValueError("Proxy startup timeout")
            yield child
        finally:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "run", "audit"])
    parser.add_argument("--frozen", type=Path)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--authorize-plan-sha256")
    parser.add_argument("--limit", type=int, help="pause after this many total saved requests (pilot)")
    args = parser.parse_args()
    root = args.directory.resolve()
    if args.action == "prepare":
        require(args.frozen is not None, "--frozen required")
        print(json.dumps(prepare(args.frozen, root, args.project), indent=2))
        return
    plan = read_json(root / "executor-plan.json")
    plan_hash = file_hash(root / "executor-plan.json")
    require(plan["version"] == VERSION, "Unknown executor version")
    require(file_hash(Path(__file__)) == plan["codeSHA256"][Path(__file__).name], "Runner code changed")
    verify_files(Path(__file__).parent, plan["codeSHA256"])
    verify_files(Path("/"), plan["runtimeSHA256"])
    require(sys.version == plan["pythonVersion"], "Python runtime changed")
    require(read_json(root / "proxy.json") == PROXY and file_hash(root / "proxy.json") == plan["proxySHA256"], "Proxy config changed")
    frozen = Path(plan["dataPath"])
    require(file_hash(frozen / "artifact-hashes.json") == plan["dataArtifactIndexSHA256"], "Data index changed")
    _, requests, prompts = load_data(frozen, plan["dataPlanSHA256"])
    prices = plan["measurementContract"]["pricesUSDPerMillion"]
    output = root / "results"
    if args.action == "audit":
        results = []
        saved = sorted(p.name for p in output.iterdir() if p.is_dir()) if output.exists() else []
        require(saved == [f"{i:04d}" for i in range(len(saved))] and len(saved) <= len(requests), "Non-prefix attempt journal")
        for request in requests:
            attempt = output / f'{request["requestOrdinal"]:04d}'
            if not attempt.exists():
                break
            require(read_json(attempt / "inflight.json")["binding"] == {"executorPlanSHA256": plan_hash, "request": request}, "Attempt binding changed")
            result = derive_result(request, attempt, prices)
            require(read_json(attempt / "result.json") == result, "Result mismatch")
            results.append(result)
        print(json.dumps({"status": "complete" if len(results) == len(requests) else "prepared_or_partial",
                          "recorded": len(results), "planned": len(requests), "providerCallsDuringAudit": 0}))
        return
    require(args.authorize_plan_sha256 == plan_hash, "Explicit authorization of the exact executor plan required")
    require(args.limit is None or 1 <= args.limit <= len(requests), "Invalid request limit")
    with owned_proxy(plan, root):
        results = run_requests(requests, prompts, output, plan_hash, prices, plan["requestTimeoutSeconds"], limit=args.limit)
    # Canonical aggregate is reconstructible from immutable per-request files.
    atomic_json(root / "execution.json", {"executorPlanSHA256": plan_hash, "recorded": len(results),
        "planned": len(requests), "status": "complete" if len(results) == len(requests) else "pilot_paused",
        "resultsSHA256": {f'{r["requestOrdinal"]:04d}/result.json': file_hash(output / f'{r["requestOrdinal"]:04d}' / "result.json") for r in results}})


if __name__ == "__main__":
    main()
