#!/bin/zsh
set -euo pipefail

if (( $# != 1 && $# != 3 )); then
  print -u2 "Usage: ./scripts/audit-causal-dataset.sh DATASET [REDUCTION RAW_SESSION]"
  exit 1
fi

dataset_dir="${1:A}"
manifest="$dataset_dir/dataset.json"
events="$dataset_dir/events.jsonl"
examples="$dataset_dir/examples.jsonl"
target_exclusions="$dataset_dir/target-exclusions.jsonl"
context_exclusions="$dataset_dir/context-exclusions.jsonl"
rejections="$dataset_dir/rejections.jsonl"

for dataset_file in "$manifest" "$events" "$examples" "$target_exclusions" "$context_exclusions" "$rejections"; do
  if [[ ! -f "$dataset_file" ]]; then
    print -u2 "Missing compiled dataset file: $dataset_file"
    exit 1
  fi
done

command -v jq >/dev/null || {
  print -u2 "jq is required for the causal dataset audit"
  exit 1
}

jq -e -s --slurpfile manifest "$manifest" --slurpfile examples "$examples" --slurpfile exclusions "$target_exclusions" --slurpfile context_exclusions "$context_exclusions" '
  def model_content:
    if has("content") then .content
    elif ((.authorshipSegments | type) == "array") then
      [.authorshipSegments[].content] | join("")
    else null end;

  . as $events |
  ($events | INDEX(.sourceEventID)) as $by_id |
  ($manifest[0]) as $m |
  ($examples) as $examples |
  ($exclusions) as $exclusions |
  ($context_exclusions) as $context_exclusions |
  ($examples | map(.targetEventID)) as $example_ids |
  ($exclusions | map(.sourceEventID)) as $excluded_ids |
  ($events | map(select(.kind == "write") | .sourceEventID)) as $write_ids |

  ($m.counts.convertedEvents == ($events | length)) and
  ($m.counts.examples == ($examples | length)) and
  ($m.counts.targetExclusions == ($exclusions | length)) and
  ($m.counts.contextExclusions == ($context_exclusions | length)) and
  (($by_id | length) == ($events | length)) and
  (all($events[];
    . as $event |
    (.serialized | fromjson) as $model |
    (.auditSerialized | fromjson) as $audit |
    .sessionID == $m.sessionID and
    .conversionVersion == $m.conversionVersion and
    ($model.kind == $event.kind) and
    (($model | model_content)
      == (if .kind == "write" then $audit.resolvedCompletion else $audit.content end)) and
    (($model | has("schemaVersion")) | not) and
    (($model | has("bundleIdentifier")) | not) and
    (($model | has("provenance")) | not) and
    (($model | has("characterOffset")) | not) and
    (($model | has("boundaryReason")) | not) and
    ($audit.schemaVersion == 1) and
    (if .kind == "read" then
       (($model.source // {} | keys - ["application", "window"] | length) == 0) and
       (($model | has("destination")) | not)
     else
       (($model.destination // {} | keys - ["application", "window"] | length) == 0) and
       (($model | has("source")) | not) and
       (($model.operation | type) == "string") and
       ((($audit.observedNetEdit // {content: $audit.content}).content | type) == "string") and
       (((($model | has("content")) and (($model | has("authorshipSegments")) | not))) or
        ((($model | has("content")) | not) and
         (($model.authorshipSegments | type) == "array") and
         (($model.authorshipSegments | length) > 0))) and
       (all($model.authorshipSegments[]?;
          ((keys - ["content", "type"]) | length) == 0))
     end)
  )) and
  (($write_ids | sort) == (($example_ids + $excluded_ids) | sort)) and
  ((($example_ids + $excluded_ids) | unique | length) == ($write_ids | length)) and

  all($examples[];
    . as $example |
    (.query | fromjson) as $query |
    (.sessionID == $m.sessionID) and
    (.conversionVersion == $m.conversionVersion) and
    ($by_id[$example.targetEventID].kind == "write") and
    ((.target | type) == "object") and
    (.target.resolvedContent != "") and
    (.target.resolvedContent == (($by_id[$example.targetEventID].serialized | fromjson) | model_content)) and
    ((.target.segments | type) == "array") and
    ($query.kind == "write_conditioning_state") and
    (.conditioningState.captureSemantics == "synchronous_before_application_mutation") and
    (.targetMask.type == "authored_text_and_paste_actions_plus_eos") and
    (.targetMask.authoredTextReceivesLoss == true) and
    (.targetMask.pasteActionsReceiveLoss == true) and
    (.targetMask.pastedPayloadReceivesLoss == false) and
    (.targetMask.eosTokenCount == 1) and
    (.targetMask.eosReceivesLoss == true) and
    ($m.loader.targetSource == "example.target.segments") and
    ($m.loader.targetTermination == "append exactly one selected-tokenizer eos_token_id") and
    ($m.loader.eosTokenCount == 1) and
    ($m.loader.authoredTextTokensReceiveLoss == true) and
    ($m.loader.pasteMarkerTokensReceiveLoss == true) and
    ($m.loader.pastedPayloadTokensReceiveLoss == false) and
    ($m.loader.eosTokenReceivesLoss == true) and
    ($m.serialization.contextVersion == 3) and
    ($m.serialization.auditContextVersion == 1) and
    (if .conditioningState.schemaVersion >= 2 then
       (.conditioningState.cursorContext.source == "accessibility_string_for_range") and
       ((.conditioningState.cursorContext.leftContext | type) == "string") and
       ((.conditioningState.cursorContext.selectedText | type) == "string") and
       ((.conditioningState.cursorContext.rightContext | type) == "string") and
       ($query.cursorContext.leftContext == .conditioningState.cursorContext.leftContext) and
       ($query.cursorContext.selectedText == .conditioningState.cursorContext.selectedText) and
       ($query.cursorContext.rightContext == .conditioningState.cursorContext.rightContext) and
       (($query.cursorContext | has("selectionStartCharacters")) | not)
     else
       (.cursorFidelity.status == "aligned") and
       (.cursorFidelity.initialCursorOffsetCharacters
         == .cursorFidelity.earliestObservedMutationOffsetCharacters) and
       (.cursorFidelity.initialCursorOffsetCharacters
         == .cursorFidelity.terminalEditOffsetCharacters) and
       (.conditioningState.cursorContext.selectionStartCharacters
         == .targetMetadata.characterOffset)
     end) and
    (if .conditioningState.schemaVersion >= 3 then
       ($query.clipboard.changeCount == .conditioningState.clipboard.changeCount) and
       ($query.clipboard.content == .conditioningState.clipboard.text) and
       ($query.clipboard.contentWasTruncated
         == .conditioningState.clipboard.textWasTruncated)
     else true end) and
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

  all($context_exclusions[];
    .sessionID == $m.sessionID and
    .conversionVersion == $m.conversionVersion and
    .reason == "read_candidate_superseded_by_write" and
    ($by_id[.sourceEventID] == null) and
    ($by_id[.supersedingWriteEventID].kind == "write") and
    (.lastActivityAt < .supersedingWriteBeganAt) and
    (.capturedAt >= .supersedingWriteBeganAt) and
    (.capturedAt <= .supersedingWriteAvailableAt)
  ) and

  all($exclusions[];
    .sessionID == $m.sessionID and
    .conversionVersion == $m.conversionVersion and
    ($by_id[.sourceEventID].kind == "write") and
    (if .reason == "empty_content" then
       ((($by_id[.sourceEventID].serialized | fromjson) | model_content) == "")
     elif .reason == "missing_initial_cursor_context" then
       (.initialCursorOffset == null)
     elif .reason == "missing_semantic_cursor_context" then
       ((.conditioningState.schemaVersion >= 2) and
        ((.conditioningState.cursorContext // null) == null))
     elif .reason == "missing_earliest_observed_mutation" then
       (.cursorFidelity.earliestObservedMutationOffsetCharacters == null)
     elif .reason == "initial_cursor_differs_from_earliest_observed_mutation" then
       (.cursorFidelity.initialCursorOffsetCharacters
         != .cursorFidelity.earliestObservedMutationOffsetCharacters)
     elif .reason == "net_edit_offset_differs_from_initial_cursor" then
       (.initialCursorOffset != .outcomeCharacterOffset)
     elif .reason == "missing_cursor_fidelity" then
       (.cursorFidelity.status != "aligned")
     elif .reason == "unresolved_paste_authorship" then
       ($by_id[.sourceEventID].serialized | fromjson).authorshipResolution != "resolved"
     else false end)
  )
' "$events" >/dev/null

if (( $# == 3 )); then
  reduction_dir="${2:A}"
  raw_session_dir="${3:A}"
  source_names=(reduction.json events.jsonl unresolved.jsonl session.json raw.jsonl)
  source_files=(
    "$reduction_dir/reduction.json"
    "$reduction_dir/events.jsonl"
    "$reduction_dir/unresolved.jsonl"
    "$raw_session_dir/session.json"
    "$raw_session_dir/raw.jsonl"
  )
else
  source_dir="$(jq -r '.source.sessionDirectory' "$manifest")"
  source_names=(session.json events.jsonl raw.jsonl)
  source_files=(
    "$source_dir/session.json"
    "$source_dir/events.jsonl"
    "$source_dir/raw.jsonl"
  )
fi
for (( index = 1; index <= ${#source_names[@]}; index += 1 )); do
  source_name="${source_names[index]}"
  source_file="${source_files[index]}"
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
actual_context_exclusions="$(wc -l < "$context_exclusions" | tr -d ' ')"
manifest_rejections="$(jq -r '.counts.rejections' "$manifest")"
manifest_target_exclusions="$(jq -r '.counts.targetExclusions' "$manifest")"
manifest_context_exclusions="$(jq -r '.counts.contextExclusions' "$manifest")"
[[ "$actual_rejections" == "$manifest_rejections" ]] || {
  print -u2 "Rejection count mismatch: manifest=$manifest_rejections file=$actual_rejections"
  exit 1
}
[[ "$actual_target_exclusions" == "$manifest_target_exclusions" ]] || {
  print -u2 "Target exclusion count mismatch: manifest=$manifest_target_exclusions file=$actual_target_exclusions"
  exit 1
}
[[ "$actual_context_exclusions" == "$manifest_context_exclusions" ]] || {
  print -u2 "Context exclusion count mismatch: manifest=$manifest_context_exclusions file=$actual_context_exclusions"
  exit 1
}

print "Causal dataset audit passed: $(jq -r '.counts.examples' "$manifest") examples, $(jq -r '.counts.convertedEvents' "$manifest") converted events, $actual_target_exclusions target exclusions, $actual_context_exclusions context exclusions, $actual_rejections rejections."
