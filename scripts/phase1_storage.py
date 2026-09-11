"""Storage-only disk safety helpers. No automatic deletion or archival."""

from __future__ import annotations

import hashlib
from pathlib import Path
import shutil

GIB = 1024 ** 3


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require_space(path: Path, expected_bytes: int = 0, reserve_bytes: int = 20 * GIB) -> None:
    """Account for the new output and reserve; never prune automatically."""
    if expected_bytes < 0 or reserve_bytes < 0:
        raise ValueError("negative disk-space requirement")
    parent = path.resolve()
    while not parent.exists():
        parent = parent.parent
    free = shutil.disk_usage(parent).free
    if free < expected_bytes + reserve_bytes:
        raise ValueError(
            f"insufficient disk space: {free / GIB:.2f} GiB free; "
            f"need {expected_bytes / GIB:.2f} GiB output plus "
            f"{reserve_bytes / GIB:.2f} GiB reserve. No data was pruned."
        )


def identity(path: Path) -> tuple[int, int, int, int]:
    st = path.stat()
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns
