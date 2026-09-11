#!/usr/bin/env python3
"""Make a new lossless shared-history corpus; never mutate the source corpus."""

import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile

from phase1_example_storage import write_examples
from phase1_jsonl import JSONLSequence
from phase1_storage import require_space, sha256


def compact(source: Path, output: Path) -> dict:
    source, output = source.resolve(strict=True), output.absolute()
    if output.exists() or output.is_relative_to(source):
        raise ValueError("use a fresh output directory outside the source")
    manifest = json.loads((source / "corpus.json").read_text())
    if not (source / "context-blocks.jsonl").is_file():
        raise ValueError("source has no authoritative shared context blocks")
    names = set(manifest["artifactDigestsSHA256"])
    if "examples.jsonl" not in names or "context-blocks.jsonl" not in names:
        raise ValueError("source manifest does not bind examples and shared blocks")
    for name in names:
        if Path(name).name != name or name in {"raw.jsonl", "session.json"}:
            raise ValueError("not a derived corpus artifact")
        if sha256(source / name) != manifest["artifactDigestsSHA256"][name]:
            raise ValueError(f"source digest mismatch: {name}")
    retained = [p for p in source.iterdir() if p.name not in {"examples.jsonl", "corpus.json", "dataset.json"}]
    if any(not p.is_file() or p.is_symlink() or p.name in {"raw.jsonl", "session.json"} for p in retained):
        raise ValueError("expected a flat derived corpus, without raw capture or symlinks")
    require_space(output.parent, sum(p.stat().st_size for p in retained))
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".compact-corpus-", dir=output.parent))
    try:
        for path in retained:
            shutil.copy2(path, temporary / path.name)
        storage = write_examples(temporary / "examples.jsonl", JSONLSequence(source / "examples.jsonl"), compact=True)
        storage.update({"sourceDirectory": str(source), "sourceManifestSHA256": sha256(source / "corpus.json"),
                        "sourceExamplesSHA256": manifest["artifactDigestsSHA256"]["examples.jsonl"]})
        manifest = {**manifest, "exampleStorage": storage,
                    "artifactDigestsSHA256": {**manifest["artifactDigestsSHA256"], "examples.jsonl": sha256(temporary / "examples.jsonl")}}
        for name in ("corpus.json", "dataset.json"):
            (temporary / name).write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        # Compare full decoded records, not just targets or a sample.
        old, new = JSONLSequence(source / "examples.jsonl"), JSONLSequence(temporary / "examples.jsonl")
        if len(old) != len(new):
            raise ValueError("compaction changed example count")
        for a, b in zip(old, new):
            if a != b:
                raise ValueError("compaction changed example semantics")
        os.rename(temporary, output)
        return storage
    except BaseException:
        shutil.rmtree(temporary)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(compact(args.source, args.output), indent=2))
