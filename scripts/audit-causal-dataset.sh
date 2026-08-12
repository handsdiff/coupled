#!/bin/zsh
set -euo pipefail

if (( $# != 1 )); then
  print -u2 "Usage: ./scripts/audit-causal-dataset.sh COMPILED_DATASET_DIRECTORY"
  exit 1
fi

dataset_dir="${1:A}"
manifest="$dataset_dir/dataset.json"
events="$dataset_dir/events.jsonl"
examples="$dataset_dir/examples.jsonl"
target_exclusions="$dataset_dir/target-exclusions.jsonl"
rejections="$dataset_dir/rejections.jsonl"

for dataset_file in "$manifest" "$events" "$examples" "$target_exclusions" "$rejections"; do
  if [[ ! -f "$dataset_file" ]]; then
    print -u2 "Missing compiled dataset file: $dataset_file"
    exit 1
  fi
done

command -v jq >/dev/null || {
  print -u2 "jq is required for the causal dataset audit"
  exit 1
}

jq -e -s --slurpfile manifest "$manifest" --slurpfile examples "$examples" --slurpfile exclusions "$target_exclusions" '
  . as $events |
  ($events | INDEX(.sourceEventID)) as $by_id |
  ($manifest[0]) as $m |
  ($examples) as $examples |
  ($exclusions) as $exclusions |
  ($examples | map(.targetEventID)) as $example_ids |
  ($exclusions | map(.sourceEventID)) as $excluded_ids |
  ($events | map(select(.kind == "write") | .sourceEventID)) as $write_ids |

  ($m.counts.convertedEvents == ($events | length)) and
  ($m.counts.examples == ($examples | length)) and
  ($m.counts.targetExclusions == ($exclusions | length)) and
  (($by_id | length) == ($events | length)) and
  (all($events[];
    .sessionID == $m.sessionID and
    .conversionVersion == $m.conversionVersion
  )) and
  (($write_ids | sort) == (($example_ids + $excluded_ids) | sort)) and
  ((($example_ids + $excluded_ids) | unique | length) == ($write_ids | length)) and

  all($examples[];
    . as $example |
    (.query | fromjson) as $query |
    (.sessionID == $m.sessionID) and
    (.conversionVersion == $m.conversionVersion) and
    ($by_id[$example.targetEventID].kind == "write") and
    ((.target | type) == "string") and
    (.target != "") and
    (.target == (($by_id[$example.targetEventID].serialized | fromjson).content)) and
    ($query.kind == "write_conditioning_state") and
    (.conditioningState.captureSemantics == "synchronous_before_application_mutation") and
    (.targetMask.type == "all_target_tokens") and
    (.conditioningState.cursorContext.selectionStartCharacters == .targetMetadata.characterOffset) and
    (.modelInput == (if .context == "" then .query else .context + "\n" + .query end)) and
    (all(.contextEventIDs[];
      ($by_id[.].availableAt < $example.targetBeganAt)
    )) and
    (
      [.contextEventIDs[] | $by_id[.]]
      == ([.contextEventIDs[] | $by_id[.]] | sort_by(.availableAt, .sourceLine))
    ) and
    (
      .context
      == ([.contextEventIDs[] | $by_id[.].serialized] | join("\n"))
    )
  ) and

  all($exclusions[];
    .sessionID == $m.sessionID and
    .conversionVersion == $m.conversionVersion and
    ($by_id[.sourceEventID].kind == "write") and
    (if .reason == "empty_content" then
       (($by_id[.sourceEventID].serialized | fromjson).content == "")
     elif .reason == "missing_initial_cursor_context" then
       (.initialCursorOffset == null)
     elif .reason == "net_edit_offset_differs_from_initial_cursor" then
       (.initialCursorOffset != .outcomeCharacterOffset)
     else false end)
  )
' "$events" >/dev/null

source_dir="$(jq -r '.source.sessionDirectory' "$manifest")"
for source_name in session.json events.jsonl raw.jsonl; do
  source_file="$source_dir/$source_name"
  [[ -f "$source_file" ]] || {
    print -u2 "Source file recorded by manifest is unavailable: $source_file"
    exit 1
  }
  expected_digest="$(jq -r --arg name "$source_name" '.source.digestsSHA256[$name]' "$manifest")"
  actual_digest="$(shasum -a 256 "$source_file" | awk '{print $1}')"
  [[ "$actual_digest" == "$expected_digest" ]] || {
    print -u2 "Source digest mismatch: $source_file"
    exit 1
  }
done

actual_rejections="$(wc -l < "$rejections" | tr -d ' ')"
actual_target_exclusions="$(wc -l < "$target_exclusions" | tr -d ' ')"
manifest_rejections="$(jq -r '.counts.rejections' "$manifest")"
manifest_target_exclusions="$(jq -r '.counts.targetExclusions' "$manifest")"
[[ "$actual_rejections" == "$manifest_rejections" ]] || {
  print -u2 "Rejection count mismatch: manifest=$manifest_rejections file=$actual_rejections"
  exit 1
}
[[ "$actual_target_exclusions" == "$manifest_target_exclusions" ]] || {
  print -u2 "Target exclusion count mismatch: manifest=$manifest_target_exclusions file=$actual_target_exclusions"
  exit 1
}

print "Causal dataset audit passed: $(jq -r '.counts.examples' "$manifest") examples, $(jq -r '.counts.convertedEvents' "$manifest") converted events, $actual_target_exclusions target exclusions, $actual_rejections rejections."
