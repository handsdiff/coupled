"""Lossless shared-history representation, independent of semantic versions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable

from phase1_storage import identity, require_space, sha256

VERSION = "phase1-shared-context-v1"
MARKER = "_sharedContext"


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class SharedContextResolver:
    def __init__(self, path: Path, expected_hash: str):
        self.path = path
        self.identity = identity(path)
        if sha256(path) != expected_hash:
            raise ValueError("shared context table digest mismatch")
        self.digest = expected_hash
        self.blocks = {}
        with path.open() as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                key, text = row.get("contextBlockID"), row.get("serialized")
                if not isinstance(key, str) or not isinstance(text, str) or key in self.blocks:
                    raise ValueError("invalid/duplicate shared context block")
                self.blocks[key] = text
        self.unchanged()

    def unchanged(self):
        if identity(self.path) != self.identity:
            raise ValueError("immutable shared context table changed")

    def expand(self, row: dict) -> dict:
        self.unchanged()
        marker = row[MARKER]
        if marker.get("version") != VERSION or marker.get("blocksSHA256") != self.digest:
            raise ValueError("unsupported/mismatched shared context storage")
        if "context" in row or "modelInput" in row:
            raise ValueError("shared examples must not contain duplicate expanded history")
        ids = row.get("contextBlockIDs")
        query = row.get("query")
        if not isinstance(ids, list) or not all(isinstance(key, str) and key in self.blocks for key in ids) or not isinstance(query, str):
            raise ValueError("invalid context references/query")
        context = "\n".join(self.blocks[key] for key in ids)
        model_input = query if not context else context + "\n" + query
        if text_hash(context) != marker.get("contextSHA256") or text_hash(model_input) != marker.get("modelInputSHA256"):
            raise ValueError("reconstructed model input digest mismatch")
        return {**{k: v for k, v in row.items() if k != MARKER}, "context": context, "modelInput": model_input}


def write_examples(path: Path, rows: Iterable[dict], *, compact: bool) -> dict:
    """The blocks file must already be finalized. One expanded row in memory."""
    require_space(path.parent)
    blocks_path = path.with_name("context-blocks.jsonl")
    digest = sha256(blocks_path) if compact else None
    resolver = SharedContextResolver(blocks_path, digest) if compact else None
    written = 0
    next_check = 16 * 1024 * 1024
    count = 0
    with path.open("xb") as handle:
        for row in rows:
            if MARKER in row:
                raise ValueError("writer requires expanded, verified examples")
            if compact:
                marker = {"version": VERSION, "blocksSHA256": digest,
                          "contextSHA256": text_hash(row["context"]),
                          "modelInputSHA256": text_hash(row["modelInput"])}
                stored = {k: v for k, v in row.items() if k not in {"context", "modelInput"}}
                stored[MARKER] = marker
                if resolver.expand(stored) != row:
                    raise ValueError("shared-context round trip changed the example")
            else:
                stored = row
            payload = (json.dumps(stored, ensure_ascii=False, sort_keys=True) + "\n").encode()
            if written + len(payload) >= next_check:
                require_space(path.parent, len(payload))
                next_check = written + len(payload) + 16 * 1024 * 1024
            handle.write(payload)
            written += len(payload)
            count += 1
    return {"version": VERSION if compact else "expanded-jsonl-v1", "examples": count,
            "storedBytes": written, "modelSemanticsChanged": False,
            "contextBlocksSHA256": digest}
