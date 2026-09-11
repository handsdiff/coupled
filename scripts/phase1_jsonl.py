"""Bounded-memory, read-only access to immutable JSONL artifacts.

Only row offsets are retained. Values are decoded on access, so iterating a
multi-gigabyte example file does not retain its repeated context strings.
This changes storage behavior, not JSON serialization or dataset semantics.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any


class JSONLSequence(Sequence[dict[str, Any]]):
    def __init__(self, path: Path):
        self.path = Path(path)
        self._shared_context = None
        self._identity = self._stat()
        self._offsets: list[tuple[int, int]] = []
        with self.path.open("rb") as handle:
            number = 0
            while True:
                offset = handle.tell()
                line = handle.readline()
                if not line:
                    break
                number += 1
                if line.strip():
                    self._offsets.append((offset, number))
        self._unchanged()

    def _stat(self) -> tuple[int, int, int, int]:
        value = self.path.stat()
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns

    def _unchanged(self) -> None:
        if self._stat() != self._identity:
            raise ValueError(f"immutable JSONL artifact changed: {self.path}")
        if self._shared_context is not None:
            self._shared_context.unchanged()

    def _decode(self, line: bytes, number: int) -> dict[str, Any]:
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError(f"{self.path}:{number}: invalid JSON") from error
        if not isinstance(value, dict):
            raise ValueError(f"{self.path}:{number}: expected a JSON object")
        if "_sharedContext" in value:
            from phase1_example_storage import SharedContextResolver
            marker = value["_sharedContext"]
            if not isinstance(marker, dict):
                raise ValueError("invalid shared-context marker")
            if self._shared_context is None:
                self._shared_context = SharedContextResolver(
                    self.path.with_name("context-blocks.jsonl"), marker.get("blocksSHA256")
                )
            value = self._shared_context.expand(value)
        return value

    def __len__(self) -> int:
        return len(self._offsets)

    def __getitem__(self, index: int | slice) -> dict[str, Any] | list[dict[str, Any]]:
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        self._unchanged()
        offset, number = self._offsets[index]
        with self.path.open("rb") as handle:
            handle.seek(offset)
            value = self._decode(handle.readline(), number)
        self._unchanged()
        return value

    def __iter__(self) -> Iterator[dict[str, Any]]:
        self._unchanged()
        with self.path.open("rb") as handle:
            for offset, number in self._offsets:
                handle.seek(offset)
                yield self._decode(handle.readline(), number)
        self._unchanged()
