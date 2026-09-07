#!/usr/bin/env python3
"""No-data checks for the packer-v12 source/rendering version agreement."""
import importlib.util
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "packed_audit", Path(__file__).with_name("audit-phase1-packed.py")
)
assert spec is not None and spec.loader is not None
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def manifest(source, required):
    return {
        "source": {"reducerVersion": source},
        "packing": {"readNoveltyRendering": {"requiredReducerVersion": required}},
    }


for version in ("phase1-semantic-v23", "phase1-semantic-v24", "phase1-semantic-v25"):
    assert audit.v12_reducer_contract_valid(manifest(version, version))
for source, required in (
    ("phase1-semantic-v24", "phase1-semantic-v23"),
    ("phase1-semantic-v23", "phase1-semantic-v24"),
    ("phase1-semantic-v25", "phase1-semantic-v24"),
    ("phase1-semantic-v24", "phase1-semantic-v25"),
    ("phase1-semantic-v26", "phase1-semantic-v26"),
    ("phase1-semantic-v22", "phase1-semantic-v22"),
    ("phase1-semantic-v24", None),
    (None, "phase1-semantic-v24"),
):
    assert not audit.v12_reducer_contract_valid(manifest(source, required))
assert not audit.v12_reducer_contract_valid({})
print("Phase 1 packed-version checks passed (12 cases)")
