#!/usr/bin/env python3
"""Offline guards against accidentally restoring the shared-target experiment."""

from copy import deepcopy
import importlib.util
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "era", Path(__file__).with_name("prepare-phase1-pipeline-era-comparison.py"))
era = importlib.util.module_from_spec(spec)
spec.loader.exec_module(era)


def rejected(plan):
    try:
        era.validate_contract(plan)
    except ValueError:
        return
    raise AssertionError("Invalid historical comparison accepted")


plan = {
    "comparison": "whole_pipeline_era_not_read_only",
    "arms": {
        "old": {"gitCommit": era.BASELINE,
                "pipeline": {"pane": "ax-pane-read-v2", "semantic": "phase1-semantic-v13",
                             "causal": "phase1-causal-v15", "episode": "phase1-raw-episode-v8"},
                "applyNewWriteRepairs": False, "applyNewReadRepairs": False,
                "consumeDynamicVisualReadRecords": False},
        "new": {"manualCurationAllowed": True, "artifactDirectory": "/corpus/new"},
    },
    "pairingRequirements": {"sameTargetText": False, "sameExampleCount": False},
    "trainingAuthorization": "not_authorized", "providerCalls": 0,
}
era.validate_contract(plan)
for key in ("applyNewWriteRepairs", "applyNewReadRepairs", "consumeDynamicVisualReadRecords"):
    bad = deepcopy(plan)
    bad["arms"]["old"][key] = True
    rejected(bad)
bad = deepcopy(plan)
bad["arms"]["old"]["pipeline"]["semantic"] = "phase1-semantic-v14"
rejected(bad)
bad = deepcopy(plan)
bad["arms"]["old"]["gitCommit"] = "95609c3"
rejected(bad)
for key in plan["pairingRequirements"]:
    bad = deepcopy(plan)
    bad["pairingRequirements"][key] = True
    rejected(bad)
bad = deepcopy(plan)
bad["arms"]["new"]["artifactDirectory"] = "/corpus/old"
rejected(bad)
bad = deepcopy(plan)
bad["arms"]["new"]["manualCurationAllowed"] = False
rejected(bad)
bad = deepcopy(plan)
bad["trainingAuthorization"] = "authorized"
rejected(bad)
print("Pipeline-era contract checks passed: true historical anchor; independent targets; tracked manual curation; no execution authorization")
