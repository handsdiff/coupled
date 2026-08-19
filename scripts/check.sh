#!/bin/zsh
set -euo pipefail

project_dir="${0:A:h:h}"
module_cache="$project_dir/.build/module-cache"
check_binary="$project_dir/.build/coupled-core-check"
mkdir -p "$module_cache"

if [[ -n "${COUPLED_SDKROOT:-}" ]]; then
  sdk_root="$COUPLED_SDKROOT"
elif [[ -d /Library/Developer/CommandLineTools/SDKs/MacOSX15.5.sdk ]]; then
  sdk_root=/Library/Developer/CommandLineTools/SDKs/MacOSX15.5.sdk
else
  sdk_root="$(xcrun --sdk macosx --show-sdk-path)"
fi

SDKROOT="$sdk_root" \
CLANG_MODULE_CACHE_PATH="$module_cache" \
swiftc \
  "$project_dir/Sources/CoupledCore/TextUnderstanding.swift" \
  "$project_dir/Sources/CoupledCore/WriteBoundary.swift" \
  "$project_dir/Sources/CoupledCore/WriteAuthorship.swift" \
  "$project_dir/Sources/CoupledCore/Phase1TargetLoader.swift" \
  "$project_dir/Sources/CoupledCore/AdjacentViewportDeduplicator.swift" \
  "$project_dir/Sources/CoupledCore/ViewportCrop.swift" \
  "$project_dir/Sources/CoupledCore/LiveEventLogFormatter.swift" \
  "$project_dir/Sources/CoupledCore/Phase1SemanticReducer.swift" \
  "$project_dir/Sources/CoupledCore/CausalDatasetCompiler.swift" \
  "$project_dir/Checks/main.swift" \
  -o "$check_binary"

"$check_binary"

PYTHONPYCACHEPREFIX="$project_dir/.build/python-cache" \
python3 -m py_compile \
  "$project_dir/scripts/pack-phase1-dataset.py" \
  "$project_dir/scripts/audit-phase1-packed.py" \
  "$project_dir/scripts/phase1_corpus.py" \
  "$project_dir/scripts/assemble-phase1-corpus.py" \
  "$project_dir/scripts/audit-phase1-corpus.py" \
  "$project_dir/scripts/check-phase1-corpus.py" \
  "$project_dir/scripts/phase1_experiment.py" \
  "$project_dir/scripts/run-phase1-experiment.py" \
  "$project_dir/scripts/audit-phase1-experiment.py" \
  "$project_dir/scripts/prepare-phase1-experiment.py" \
  "$project_dir/scripts/phase1_subscription_responses.py" \
  "$project_dir/scripts/preflight-phase1-subscription.py" \
  "$project_dir/scripts/preflight-phase1-paste-example.py" \
  "$project_dir/scripts/run-phase1-frontier-arm.py" \
  "$project_dir/scripts/run-phase1-tinker-prequential.py" \
  "$project_dir/scripts/check-phase1-real-runners.py" \
  "$project_dir/scripts/audit-phase1-real-experiment.py" \
  "$project_dir/scripts/phase1_prediction_metrics.py" \
  "$project_dir/scripts/check-phase1-prediction-metrics.py" \
  "$project_dir/scripts/check-phase1-subscription-responses.py" \
  "$project_dir/scripts/plot-phase1-tinker-loss.py" \
  "$project_dir/scripts/phase1_training_contract.py" \
  "$project_dir/scripts/phase1_tinker_overfit_contract.py" \
  "$project_dir/scripts/prepare-phase1-tinker-smoke.py" \
  "$project_dir/scripts/prepare-phase1-tinker-overfit.py" \
  "$project_dir/scripts/run-phase1-tinker-overfit.py" \
  "$project_dir/scripts/preflight-phase1-tinker-tokenizer.py" \
  "$project_dir/scripts/check-phase1-training-contract.py" \
  "$project_dir/scripts/check-phase1-tinker-overfit-contract.py" \
  "$project_dir/scripts/phase1_cost_latency.py" \
  "$project_dir/scripts/check-phase1-cost-latency.py"

PYTHONPYCACHEPREFIX="$project_dir/.build/python-cache" \
python3 "$project_dir/scripts/check-phase1-training-contract.py"

PYTHONPYCACHEPREFIX="$project_dir/.build/python-cache" \
python3 "$project_dir/scripts/check-phase1-corpus.py"

PYTHONPYCACHEPREFIX="$project_dir/.build/python-cache" \
python3 "$project_dir/scripts/check-phase1-subscription-responses.py"
python3 "$project_dir/scripts/check-phase1-real-runners.py"
python3 "$project_dir/scripts/check-phase1-prediction-metrics.py"
python3 "$project_dir/scripts/check-phase1-cost-latency.py"

if [[ -x "$project_dir/.build/tinker-venv/bin/python" ]]; then
  PYTHONPYCACHEPREFIX="$project_dir/.build/python-cache" \
  "$project_dir/.build/tinker-venv/bin/python" \
    "$project_dir/scripts/check-phase1-tinker-overfit-contract.py"
fi
