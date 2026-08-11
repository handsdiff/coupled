#!/bin/zsh
set -euo pipefail

project_dir="${0:A:h:h}"
module_cache="$project_dir/.build/module-cache"
swiftpm_cache="$project_dir/.build/swiftpm-cache"
swiftpm_config="$project_dir/.build/swiftpm-config"
swiftpm_security="$project_dir/.build/swiftpm-security"
mkdir -p "$module_cache" "$swiftpm_cache" "$swiftpm_config" "$swiftpm_security"

if [[ -n "${COUPLED_SDKROOT:-}" ]]; then
  sdk_root="$COUPLED_SDKROOT"
elif [[ -d /Library/Developer/CommandLineTools/SDKs/MacOSX15.5.sdk ]]; then
  # This machine's default 26.2 SDK is newer than its selected Swift compiler.
  sdk_root=/Library/Developer/CommandLineTools/SDKs/MacOSX15.5.sdk
else
  sdk_root="$(xcrun --sdk macosx --show-sdk-path)"
fi

SDKROOT="$sdk_root" \
CLANG_MODULE_CACHE_PATH="$module_cache" \
SWIFTPM_MODULECACHE_OVERRIDE="$module_cache" \
swift build --disable-sandbox \
  --cache-path "$swiftpm_cache" \
  --config-path "$swiftpm_config" \
  --security-path "$swiftpm_security" \
  --manifest-cache local \
  "$@"
