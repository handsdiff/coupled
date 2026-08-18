#!/bin/zsh
set -euo pipefail

project_dir="${0:A:h:h}"
app_dir="$project_dir/dist/Coupled.app"
contents_dir="$app_dir/Contents"
macos_dir="$contents_dir/MacOS"
logs_app_dir="$project_dir/dist/Coupled Logs.app"
logs_contents_dir="$logs_app_dir/Contents"
logs_macos_dir="$logs_contents_dir/MacOS"

"$project_dir/scripts/build.sh" -c release

mkdir -p "$macos_dir"
cp "$project_dir/.build/release/coupled" "$macos_dir/coupled"
cp "$project_dir/App/Info.plist" "$contents_dir/Info.plist"
chmod 755 "$macos_dir/coupled"

mkdir -p "$logs_macos_dir"
cp "$project_dir/.build/release/coupled-logs" "$logs_macos_dir/coupled-logs"
cp "$project_dir/App/Logs-Info.plist" "$logs_contents_dir/Info.plist"
chmod 755 "$logs_macos_dir/coupled-logs"

codesign \
  --force \
  --sign - \
  --identifier com.handsdiff.coupled \
  "$app_dir"

codesign \
  --force \
  --sign - \
  --identifier com.handsdiff.coupled.logs \
  "$logs_app_dir"

touch "$app_dir" "$logs_app_dir"

print "Created $app_dir"
print "Created $logs_app_dir"
print "Note: this app is ad-hoc signed; rebuilding may require macOS privacy permissions to be granted again."
print "Permission bootstrap:"
print "  $project_dir/scripts/coupled doctor --prompt-permissions"
