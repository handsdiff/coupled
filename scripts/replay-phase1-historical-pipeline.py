#!/usr/bin/env python3
"""Bounded, resumable local replay of the unchanged pre-Sep-2 producer.

Adapters change only source discovery, exact OCR-cache addressing and lazy
JSONL reads. Historical semantic code and decisions are never overlaid.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "coupled-data/sep02-10-pipeline-era-comparison-20260911"
SRC = OUT / "old-producer"
WORK = OUT / "historical"
PREP = ROOT / "coupled-data/sep02-10-training-prep-20260910"
BIN = ROOT / ".build/pipeline-era-f6a2e71/release/coupled"
VERSION = "phase1-historical-replay-v2"


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""): h.update(b)
    return h.hexdigest()


def rows(path):
    with Path(path).open() as f:
        for line in f:
            if line.strip(): yield json.loads(line)


def save(path, value):
    with path.open("x") as f: json.dump(value, f, sort_keys=True, indent=2); f.write("\n")


def load_module(name):
    sys.path.insert(0, str(SRC / "scripts"))
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SRC / "scripts" / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def verify():
    m = json.loads((OUT / "comparison.json").read_text())
    for name, digest in m["arms"]["old"]["producerFilesSHA256"].items():
        assert sha(SRC / name) == digest, name
    for name, digest in m["rawSourcesSHA256"].items(): assert sha(name) == digest, name
    return m


def surface(day, m):
    module = load_module("build-phase1-read-surface-evidence")
    raw = Path(m["sessions"][str(day)])
    cache = (PREP / "old-sep7-surfaces" if day == 7 else
             ROOT / f"coupled-data/phase1-read-pipeline-factorial-20260907/old-sep{day}-surfaces")
    cache_m = json.loads((cache / "read-surface-evidence.json").read_text())
    assert cache_m["source"]["digestsSHA256"]["ocrSource"] == sha(SRC / "scripts/ocr-phase1-surface-regions.m")
    for name in ("raw.jsonl", "session.json"):
        assert cache_m["source"]["digestsSHA256"][name] == m["rawSourcesSHA256"][str(raw / name)]
    assert sha(cache / "read-surfaces.jsonl") == cache_m["artifacts"]["digestsSHA256"]["read-surfaces.jsonl"]
    # The later cache includes an extra projection field in its job ID. Map
    # only identical image/record/ROI jobs into the original job-ID formula.
    reuse = WORK / f"sep{day}-exact-ocr-cache.jsonl"
    if not reuse.exists():
        with reuse.open("x") as f:
            for r in rows(cache / "read-surfaces.jsonl"):
                if r["ruleVersion"] != "ax-pane-read-v2": continue
                key = {"recordID": r["sourceRecordID"], "screenshotSHA256": r["screenshotSHA256"],
                       "region": r["regionOfInterest"], "ruleVersion": r["ruleVersion"]}
                assert module.digest_text(r["content"]) == r["contentSHA256"]
                out = {**r, "jobID": "surface_" + module.digest_text(module.canonical(key))}
                f.write(module.canonical(out) + "\n")
        save(WORK / f"sep{day}-cache-binding.json", {"cacheManifest": str(cache / "read-surface-evidence.json"),
             "manifestSHA256": sha(cache / "read-surface-evidence.json"), "sourceSHA256": sha(cache / "read-surfaces.jsonl"),
             "translatedSHA256": sha(reuse), "rule": "identical_record_screenshot_roi_and_unchanged_ocr_source_only"})
    binding = json.loads((WORK / f"sep{day}-cache-binding.json").read_text())
    assert sha(reuse) == binding["translatedSHA256"]
    sys.argv = [module.__file__, "--input", str(raw), "--output", str(WORK / f"sep{day}-surfaces"),
                "--reuse-results", str(reuse), "--rule-version", "ax-pane-read-v2"]
    module.main()
    report = json.loads((WORK / f"sep{day}-surfaces/read-surface-evidence.json").read_text())
    assert report["counts"]["evidence"] > 0
    for r in rows(WORK / f"sep{day}-surfaces/unresolved.jsonl"):
        assert r["reason"] != "ocr_error", "OCR execution errors are not missing evidence"


def lazy_examples(module):
    # Identical decoded JSON objects; only repeated example strings are lazy.
    # The original functions remain in use for every other record type.
    sys.path.insert(0, str(ROOT / "scripts"))
    from phase1_historical_storage import HistoricalExamples
    original = module.load_jsonl
    module.load_jsonl = lambda p: HistoricalExamples(p) if Path(p).name == "examples.jsonl" and Path(p).exists() else original(p)


def worker(kind, day):
    m = verify()
    if kind == "surface": surface(day, m)
    elif kind == "assemble":
        module = load_module("phase1_corpus")
        lazy_examples(module)
        module.assemble([WORK / f"sep{d}-causal" for d in (2, 3, 4, 7)], WORK / "micro-corpus", 50)
    elif kind == "primitives":
        module = load_module("build-phase1-episode-review")
        lazy_examples(module)
        # __file__ is used only for project-relative artifact discovery. Code
        # itself is loaded and hash-verified from the historical export above.
        module.__file__ = str(ROOT / "scripts/build-phase1-episode-review.py")
        def sources(project, ids):
            result = {}
            for directory in m["sessions"].values():
                p = Path(directory); sid = json.loads((p / "session.json").read_text())["sessionID"]
                if sid in ids: result[sid] = (p / "raw.jsonl", str((p / "raw.jsonl").relative_to(ROOT)))
            assert set(result) == ids
            return result
        module.discover_raw_sessions = sources
        module.matching_hashed_artifacts = lambda project, name, digest: sorted(
            p for p in WORK.glob(f"**/{name}") if p.is_file() and sha(p) == digest)
        sys.argv = [module.__file__, "--corpus", str(WORK / "micro-corpus"),
                    "--output", str(WORK / "primitives"), "--all-write-singletons"]
        module.main()
    elif kind == "episodes":
        module = load_module("construct-phase1-raw-episode-corpus")
        projection = load_module("construct-phase1-closed-episode-corpus")
        lazy_examples(projection)
        result = module.assemble(WORK / "micro-corpus", WORK / "primitives", WORK / "episodes", ROOT, projection)
        print(json.dumps(result["counts"], sort_keys=True), flush=True)
    elif kind == "audit":
        module = load_module("phase1_corpus")
        lazy_examples(module)
        result = module.audit(WORK / "episodes")
        save(WORK / "historical-corpus-audit.json", result)
    else: raise ValueError(kind)


def stage(name, command, artifact):
    logs = WORK / "logs"; logs.mkdir(exist_ok=True)
    marker = logs / (name + ".complete.json")
    command = list(map(str, command))
    if marker.exists():
        prior = json.loads(marker.read_text())
        assert prior["command"] == command and prior["artifactSHA256"] == sha(artifact)
        # v1 completed the same historical stages; v2 only fixes repeated IO.
        prior_wrapper = OUT / "replay-wrapper-v1.py"
        assert prior["wrapperSHA256"] in {sha(__file__), sha(prior_wrapper)}, "Unknown historical replay wrapper"
        print("Verified existing", name, flush=True); return
    assert shutil.disk_usage(WORK).free > 20 * 1024**3
    assert not artifact.exists(), "Uncommitted stage output exists; review it before continuing"
    log = logs / (name + ".log")
    assert not log.exists(), "Prior attempt exists; preserve/review it instead of silently replaying"
    started, peak = time.monotonic(), 0
    env = {**os.environ, "CLANG_MODULE_CACHE_PATH": str(ROOT / ".build/module-cache"),
           "PYTHONDONTWRITEBYTECODE": "1", "TOKENIZERS_PARALLELISM": "false"}
    print("Starting", name, flush=True)
    with log.open("x") as handle:
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=handle, stderr=handle, start_new_session=True)
        try:
            while process.poll() is None:
                table = subprocess.check_output(["ps", "-axo", "pid=,ppid=,rss="], text=True)
                entries = [tuple(map(int, line.split())) for line in table.splitlines()]
                ids = {process.pid}
                for _ in range(16): ids.update(pid for pid, parent, rss in entries if parent in ids)
                rss = sum(rss for pid, parent, rss in entries if pid in ids)
                peak = max(peak, rss)
                if rss > 5 * 1024**2 or shutil.disk_usage(WORK).free < 20 * 1024**3:
                    raise RuntimeError("5-GiB process-tree / 20-GiB disk reserve guard reached")
                time.sleep(1)
        except BaseException:
            if process.poll() is None: os.killpg(process.pid, signal.SIGTERM); process.wait()
            raise
    assert process.returncode == 0, f"{name} failed; inspect {log}"
    save(marker, {"command": command, "artifactSHA256": sha(artifact), "wrapperSHA256": sha(__file__),
                  "durationSeconds": time.monotonic() - started, "peakRSSKiB": peak,
                  "processTreeLimitGiB": 5, "freeDiskReserveGiB": 20})
    print("Completed", name, "peak MiB", round(peak / 1024, 1), flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--worker", choices=["surface", "assemble", "primitives", "episodes", "audit"])
    p.add_argument("--day", type=int, choices=[2, 3, 4, 7])
    a = p.parse_args()
    if a.worker: worker(a.worker, a.day); return
    m = verify(); WORK.mkdir(exist_ok=True)
    assert BIN.is_file(), "Build the frozen producer first, never replace the live collector"
    for d in (2, 3, 4, 7):
        stage(f"sep{d}-surface", [sys.executable, "-B", __file__, "--worker", "surface", "--day", d],
              WORK / f"sep{d}-surfaces/read-surface-evidence.json")
        stage(f"sep{d}-reduce", [BIN, "reduce", "--input", m["sessions"][str(d)], "--output", WORK / f"sep{d}-reduced",
              "--reducer-version", "phase1-semantic-v13", "--read-surface-evidence", WORK / f"sep{d}-surfaces"], WORK / f"sep{d}-reduced/reduction.json")
        stage(f"sep{d}-compile", [BIN, "compile", "--input", WORK / f"sep{d}-reduced", "--source", m["sessions"][str(d)],
              "--output", WORK / f"sep{d}-causal", "--conversion-version", "phase1-causal-v15"], WORK / f"sep{d}-causal/dataset.json")
    for kind, artifact in [("assemble", "micro-corpus/corpus.json"), ("primitives", "primitives/episode-review.json"),
                           ("episodes", "episodes/corpus.json"), ("audit", "historical-corpus-audit.json")]:
        stage(kind, [sys.executable, "-B", __file__, "--worker", kind], WORK / artifact)
    ignored = {}
    for day, raw in m["sessions"].items():
        from collections import Counter
        counts = Counter(r.get("recordType", "unknown") for r in rows(Path(raw) / "raw.jsonl"))
        ignored[day] = dict(counts)
    save(WORK / "replay.json", {"version": VERSION, "status": "historical_replay_complete",
         "gitCommit": m["arms"]["old"]["gitCommit"], "binarySHA256": sha(BIN),
         "comparisonSHA256": sha(OUT / "comparison.json"), "wrapperSHA256": sha(__file__),
         "storageAdapterSHA256": sha(ROOT / "scripts/phase1_jsonl.py"),
         "deferredHistoryAdapterSHA256": sha(ROOT / "scripts/phase1_historical_storage.py"),
         "priorWrapperSHA256": sha(OUT / "replay-wrapper-v1.py"),
         "operationalAdapters": ["pinned source discovery", "exact OCR-cache ID remap", "exact metadata index with deferred history strings"],
         "semanticSourceModified": False, "rawTypeInventory": ignored,
         "visualChangeRecordsPromoted": False, "providerCalls": 0,
         "corpusManifestSHA256": sha(WORK / "episodes/corpus.json")})


if __name__ == "__main__": main()
