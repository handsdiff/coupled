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
  "$project_dir/Sources/CoupledCore/WriteAuthorship.swift" \
  "$project_dir/Sources/CoupledCore/Phase1TargetLoader.swift" \
  "$project_dir/Sources/CoupledCore/AdjacentViewportDeduplicator.swift" \
  "$project_dir/Sources/CoupledCore/ViewportCrop.swift" \
  "$project_dir/Sources/CoupledCore/CausalDatasetCompiler.swift" \
  "$project_dir/Checks/main.swift" \
  -o "$check_binary"

"$check_binary"
