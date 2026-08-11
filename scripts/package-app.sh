#!/bin/zsh
set -euo pipefail

project_dir="${0:A:h:h}"
app_dir="$project_dir/dist/Coupled.app"
contents_dir="$app_dir/Contents"
macos_dir="$contents_dir/MacOS"

"$project_dir/scripts/build.sh" -c release

mkdir -p "$macos_dir"
cp "$project_dir/.build/release/coupled" "$macos_dir/coupled"
cp "$project_dir/App/Info.plist" "$contents_dir/Info.plist"
chmod 755 "$macos_dir/coupled"

codesign \
  --force \
  --sign - \
  --identifier com.niyant.coupled \
  "$app_dir"

touch "$app_dir"

print "Created $app_dir"
print "Note: this app is ad-hoc signed; rebuilding may require macOS privacy permissions to be granted again."
print "Permission bootstrap:"
print "  $project_dir/scripts/coupled doctor --prompt-permissions"
