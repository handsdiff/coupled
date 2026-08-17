import CryptoKit
import Foundation

public struct Phase1SemanticReducerConfiguration: Sendable {
    public let reducerVersion: String

    public init(reducerVersion: String = "phase1-semantic-v1") {
        self.reducerVersion = reducerVersion
    }
}

public struct Phase1SemanticReducerResult: Sendable, Equatable {
    public let rawRecordCount: Int
    public let eventCount: Int
    public let unresolvedCount: Int
    public let readCount: Int
    public let writeCount: Int
}

public enum Phase1SemanticReducerError: Error, CustomStringConvertible {
    case missingFile(String)
    case outputAlreadyExists(String)
    case invalidManifest(String)
    case invalidJSON(String, Int)
    case duplicateRawRecordID(String)
    case couldNotCreate(String)

    public var description: String {
        switch self {
        case .missingFile(let path): return "required source file is missing: \(path)"
        case .outputAlreadyExists(let path): return "reducer output is not empty: \(path)"
        case .invalidManifest(let reason): return "invalid session manifest: \(reason)"
        case .invalidJSON(let path, let line): return "invalid JSON object at \(path):\(line)"
        case .duplicateRawRecordID(let id): return "duplicate raw record ID: \(id)"
        case .couldNotCreate(let path): return "could not create reducer output: \(path)"
        }
    }
}

/// Constructs the versioned Phase 1 READ/WRITE projection from sensor evidence.
/// It never reads the collector's provisional events.preview.jsonl artifact.
public struct Phase1SemanticReducer {
    public let configuration: Phase1SemanticReducerConfiguration

    public init(configuration: Phase1SemanticReducerConfiguration = .init()) {
        self.configuration = configuration
    }

    @discardableResult
    public func reduce(sourceDirectory: URL, outputDirectory: URL) throws
        -> Phase1SemanticReducerResult
    {
        let source = sourceDirectory.standardizedFileURL
        let output = outputDirectory.standardizedFileURL
        let sessionURL = source.appendingPathComponent("session.json")
        let rawURL = source.appendingPathComponent("raw.jsonl")
        for url in [sessionURL, rawURL] where !FileManager.default.fileExists(atPath: url.path) {
            throw Phase1SemanticReducerError.missingFile(url.path)
        }
        if FileManager.default.fileExists(atPath: output.path),
           !(try FileManager.default.contentsOfDirectory(atPath: output.path)).isEmpty {
            throw Phase1SemanticReducerError.outputAlreadyExists(output.path)
        }
        let manifestData = try Data(contentsOf: sessionURL)
        guard let manifest = try JSONSerialization.jsonObject(with: manifestData) as? [String: Any],
              let sessionID = manifest["sessionID"] as? String, !sessionID.isEmpty else {
            throw Phase1SemanticReducerError.invalidManifest("missing sessionID")
        }
        let raw = try reducerReadJSONL(rawURL)
        var seenRawIDs = Set<String>()
        for record in raw {
            guard let id = record.object["recordID"] as? String else { continue }
            guard seenRawIDs.insert(id).inserted else {
                throw Phase1SemanticReducerError.duplicateRawRecordID(id)
            }
        }

        var events = [[String: Any]]()
        var unresolved = [[String: Any]]()
        var viewportDeduplicator = AdjacentViewportDeduplicator()
        var sequence = 0
        for record in raw {
            let object = record.object
            guard object["sessionID"] as? String == sessionID else {
                unresolved.append(reducerUnresolved(
                    sessionID: sessionID, raw: object, line: record.line,
                    kind: "unknown", rule: "session_identity",
                    reason: "raw_session_id_mismatch"
                ))
                continue
            }
            switch object["recordType"] as? String {
            case "screen_ocr_observation":
                switch reduceRead(object, sessionID: sessionID) {
                case .failure(let failure):
                    unresolved.append(reducerUnresolved(
                        sessionID: sessionID, raw: object, line: record.line,
                        kind: "read", rule: failure.rule, reason: failure.reason,
                        details: failure.details
                    ))
                case .success(var event):
                    let context = "\(intValue(object["processIdentifier"]) ?? -1)|\(intValue(object["windowID"]) ?? -1)|\(intValue(object["displayID"]) ?? -1)"
                    let original = stringValue(object["content"]) ?? ""
                    guard let emitted = viewportDeduplicator.contentToEmit(
                        contextIdentifier: context, viewportContent: original
                    ) else {
                        unresolved.append(reducerUnresolved(
                            sessionID: sessionID, raw: object, line: record.line,
                            kind: "read", rule: "adjacent_viewport_overlap_v1",
                            reason: "adjacent_viewport_duplicate"
                        ))
                        continue
                    }
                    sequence += 1
                    event["sequence"] = sequence
                    event["content"] = emitted
                    let emittedLines = emitted.split(separator: "\n", omittingEmptySubsequences: true).count
                    event["emittedLineCount"] = emittedLines
                    event["overlapRemovedLineCount"] = max(
                        (intValue(object["recognizedLineCount"]) ?? emittedLines) - emittedLines, 0
                    )
                    events.append(event)
                }
            case "active_tap_write_attempt":
                switch reduceWrite(object, sessionID: sessionID) {
                case .failure(let failure):
                    unresolved.append(reducerUnresolved(
                        sessionID: sessionID, raw: object, line: record.line,
                        kind: "write", rule: failure.rule, reason: failure.reason,
                        details: failure.details
                    ))
                case .success(var event):
                    // Only a finalized semantic WRITE interrupts adjacent READ
                    // overlap. An unresolved sensor attempt is not a history event.
                    viewportDeduplicator.reset()
                    sequence += 1
                    event["sequence"] = sequence
                    events.append(event)
                }
            default:
                continue
            }
        }

        try FileManager.default.createDirectory(
            at: output, withIntermediateDirectories: true,
            attributes: [.posixPermissions: NSNumber(value: 0o700)]
        )
        let eventsURL = output.appendingPathComponent("events.jsonl")
        let unresolvedURL = output.appendingPathComponent("unresolved.jsonl")
        try reducerWriteJSONL(events, to: eventsURL)
        try reducerWriteJSONL(unresolved, to: unresolvedURL)
        let reduction: [String: Any] = [
            "schemaVersion": 1,
            "reducerVersion": configuration.reducerVersion,
            "sessionID": sessionID,
            "source": [
                "digestsSHA256": [
                    "session.json": try reducerSHA256(sessionURL),
                    "raw.jsonl": try reducerSHA256(rawURL),
                ],
                "rawRecordCount": raw.count,
            ],
            "artifacts": [
                "digestsSHA256": [
                    "events.jsonl": try reducerSHA256(eventsURL),
                    "unresolved.jsonl": try reducerSHA256(unresolvedURL),
                ],
            ],
            "counts": [
                "events": events.count,
                "reads": events.filter { $0["kind"] as? String == "read" }.count,
                "writes": events.filter { $0["kind"] as? String == "write" }.count,
                "unresolved": unresolved.count,
            ],
            "eventIdentity": "sha256(sessionID + ordered raw lineage + output ordinal); reducer version excluded",
            "previewAuthority": false,
        ]
        try reducerWriteJSON(reduction, to: output.appendingPathComponent("reduction.json"))
        return Phase1SemanticReducerResult(
            rawRecordCount: raw.count,
            eventCount: events.count,
            unresolvedCount: unresolved.count,
            readCount: events.filter { $0["kind"] as? String == "read" }.count,
            writeCount: events.filter { $0["kind"] as? String == "write" }.count
        )
    }
}

private struct ReducerLine { let line: Int; let object: [String: Any] }
private struct ReducerFailure: Error {
    let rule: String
    let reason: String
    let details: [String: Any]
}
private struct ReducerSelection {
    let observation: [String: Any]
    let source: String
    let checkpointID: String?
    let reason: String
}

private func reduceRead(_ raw: [String: Any], sessionID: String)
    -> Result<[String: Any], ReducerFailure>
{
    func fail(_ reason: String) -> Result<[String: Any], ReducerFailure> {
        .failure(ReducerFailure(rule: "screen_ocr_v1", reason: reason, details: [:]))
    }
    guard let recordID = stringValue(raw["recordID"]) else { return fail("missing_record_id") }
    if let post = raw["postCaptureSurface"] as? [String: Any],
       !sameCapturedReadSurface(raw, post) {
        return fail("surface_changed_during_capture")
    }
    if nonEmptyString(raw["supersedingWriteAttemptID"]) != nil {
        return fail("read_candidate_superseded_by_write")
    }
    if stringValue(raw["bundleIdentifier"]) == "com.google.Chrome",
       let bounds = raw["windowBounds"] as? [String: Any],
       ((doubleValue(bounds["height"]) ?? 0) < 300
        || (doubleValue(bounds["width"]) ?? 0) < 100) {
        return fail("chrome_auxiliary_surface")
    }
    guard let content = stringValue(raw["content"]), !content.isEmpty else {
        return fail("empty_ocr_content")
    }
    guard raw["contentWasTruncated"] as? Bool != true else {
        return fail("ocr_content_truncated")
    }
    let eventID = stableEventID(sessionID: sessionID, lineage: [recordID], ordinal: 0)
    var event = raw
    for key in [
        "recordType", "recordID", "schemaVersion", "derivedSuppressionReason",
        "supersedingWriteAttemptID", "screenshotRelativePath", "screenshotSHA256",
        "screenshotPixelWidth", "screenshotPixelHeight", "postCaptureSurface",
        "firstEventTimestampNanoseconds", "lastEventTimestampNanoseconds",
    ] { event.removeValue(forKey: key) }
    event["schemaVersion"] = 8
    event["kind"] = "read"
    event["provenance"] = "screen_ocr"
    event["eventID"] = eventID
    event["sourceRecordIDs"] = [recordID]
    event["reduction"] = [
        "schemaVersion": 1,
        "rule": "screen_ocr_v1",
        "reason": "eligible_capture_time_observation",
        "selectedObservationID": recordID,
        "rawLineage": [recordID],
        "outputOrdinal": 0,
    ]
    return .success(event)
}

private func reduceWrite(_ raw: [String: Any], sessionID: String)
    -> Result<[String: Any], ReducerFailure>
{
    func fail(_ reason: String, rule: String = "write_observation_selection_v1", details: [String: Any] = [:])
        -> Result<[String: Any], ReducerFailure> {
        .failure(ReducerFailure(rule: rule, reason: reason, details: details))
    }
    guard let recordID = stringValue(raw["recordID"]) else { return fail("missing_record_id") }
    guard (raw["tapTimeoutCountDuringBurst"] as? NSNumber)?.uint64Value ?? 0 == 0 else {
        return fail("tap_timeout")
    }
    guard stringArray(raw["beforeAXErrors"]).isEmpty,
          let before = raw["before"] as? [String: Any],
          before["valueWasTruncated"] as? Bool != true,
          let rawBefore = stringValue(before["value"]) else {
        return fail("before_missing_error_or_truncated")
    }
    let beforeValue = logicalEditableValue(
        rawBefore, placeholderValue: stringValue(before["placeholderValue"])
    )
    guard let selection = selectWriteObservation(raw, beforeValue: beforeValue) else {
        return fail("no_meaningful_terminal_observation")
    }
    guard selection.observation["valueWasTruncated"] as? Bool != true,
          let rawAfter = stringValue(selection.observation["value"]) else {
        return fail("selected_observation_missing_or_truncated")
    }
    let afterValue = logicalEditableValue(
        rawAfter, placeholderValue: stringValue(selection.observation["placeholderValue"])
    )
    let observedEdit = minimalTextEdit(from: beforeValue, to: afterValue)
    guard !observedEdit.isEmpty, applying(observedEdit, to: beforeValue) == afterValue else {
        return fail("empty_or_non_reconstructing_edit")
    }
    let hints = Set(stringArray(raw["inputHints"]))
    if hints.isSubset(of: ["delete", "navigation"]), !observedEdit.inserted.isEmpty {
        return fail(
            "delete_only_transition_inserted_content",
            rule: "input_capability_guard_v1",
            details: [
                "insertedCharacterCount": observedEdit.inserted.count,
                "removedCharacterCount": observedEdit.removed.count,
            ]
        )
    }

    let authorship = reduceAuthorship(
        raw: raw, beforeValue: beforeValue,
        usedObservation: selection.observation, observedEdit: observedEdit
    )
    guard authorship.resolution == "resolved" else {
        return fail(authorship.resolution, rule: "paste_authorship_v1")
    }
    guard !authorship.resolvedCompletion.contains("\u{200B}") else {
        return fail("application_generated_zero_width_scaffold", rule: "authorship_guard_v1")
    }

    let conditioning = raw["conditioningState"] as? [String: Any] ?? [:]
    let cursorFidelity = reducerCursorFidelity(raw: raw, terminalEditOffset: observedEdit.characterOffset)
    let target = raw["targetIdentity"] as? [String: Any] ?? [:]
    let eventID = stableEventID(sessionID: sessionID, lineage: [recordID], ordinal: 0)
    let outcome: [String: Any] = [
        "operation": observedEdit.operation.rawValue,
        "characterOffset": observedEdit.characterOffset,
        "removedContent": observedEdit.removed,
        "content": observedEdit.inserted,
    ]
    let segments: [[String: Any]] = authorship.segments.map { segment in
        var result: [String: Any] = ["type": segment.type, "content": segment.content]
        if let id = segment.clipboardSnapshotID { result["clipboardSnapshotID"] = id }
        if let id = segment.pasteCheckpointID { result["pasteCheckpointID"] = id }
        return result
    }
    let appName = ((conditioning["destination"] as? [String: Any])?["appName"] as? String)
        ?? stringValue(target["bundleIdentifier"]) ?? "Unknown"
    let bundle = ((conditioning["destination"] as? [String: Any])?["bundleIdentifier"] as? String)
        ?? stringValue(raw["bundleIdentifier"])
    let process = intValue((conditioning["destination"] as? [String: Any])?["processIdentifier"])
        ?? intValue(target["processIdentifier"]) ?? -1
    let window = ((conditioning["destination"] as? [String: Any])?["windowTitle"] as? String)
        ?? stringValue(target["windowTitle"])
    var event: [String: Any] = [
        "schemaVersion": 12,
        "kind": "write",
        "provenance": "raw_input_semantic_reducer",
        "eventID": eventID,
        "sessionID": sessionID,
        "observedAt": stringValue(raw["observedAt"]) ?? stringValue(raw["terminalDecisionAt"]) ?? "",
        "beganAt": stringValue(raw["beganAt"]) ?? "",
        "lastInputAt": stringValue(raw["lastInputAt"]) ?? "",
        "terminalDecisionAt": stringValue(raw["terminalDecisionAt"]) ?? "",
        "terminalSnapshotAt": raw["terminalSnapshotAt"] ?? NSNull(),
        "configuredWriteDelaySeconds": raw["configuredWriteDelaySeconds"] ?? 0,
        "boundaryReason": stringValue(raw["boundaryReason"]) ?? "unknown",
        "derivationObservationSource": selection.source,
        "fallbackReason": selection.reason == "terminal_observation" ? NSNull() : selection.reason,
        "usedCheckpointID": selection.checkpointID ?? NSNull(),
        "usedObservationCapturedAt": stringValue(selection.observation["observedAt"]) ?? "",
        "conditioningState": conditioning,
        "cursorFidelity": cursorFidelity,
        "authorshipResolution": authorship.resolution,
        "authorshipEvidence": authorship.evidence ?? NSNull(),
        "authorshipSegments": segments,
        "resolvedCompletion": authorship.resolvedCompletion,
        "stateContinuity": authorship.stateContinuity,
        "observedNetEdit": outcome,
        "outcome": outcome,
        "operation": observedEdit.operation.rawValue,
        "content": observedEdit.inserted,
        "removedContent": observedEdit.removed,
        "characterOffset": observedEdit.characterOffset,
        "inputEventCount": intValue(raw["inputEventCount"]) ?? 0,
        "appName": appName,
        "bundleIdentifier": bundle ?? NSNull(),
        "processIdentifier": process,
        "windowTitle": window ?? NSNull(),
        "sourceRecordIDs": [recordID],
        "reduction": [
            "schemaVersion": 1,
            "rule": "write_observation_selection_v1",
            "reason": selection.reason,
            "selectedObservationID": stringValue(selection.observation["observationID"]) ?? "",
            "selectedObservationSource": selection.source,
            "rawLineage": [recordID],
            "outputOrdinal": 0,
        ],
    ]
    // JSONSerialization cannot encode Swift optionals hidden in Any.
    event = removeNullOptionals(event)
    return .success(event)
}

private func selectWriteObservation(_ raw: [String: Any], beforeValue: String) -> ReducerSelection? {
    let lastTimestamp = uint64Value(raw["lastEventTimestampNanoseconds"])
    let returns = raw["returnCheckpoints"] as? [[String: Any]] ?? []
    let pastes = raw["pasteCheckpoints"] as? [[String: Any]] ?? []
    let mutations = raw["mutationCheckpoints"] as? [[String: Any]] ?? []
    func meaningful(_ checkpoint: [String: Any], source: String) -> ReducerSelection? {
        guard stringArray(checkpoint["axErrors"]).isEmpty,
              let observation = checkpoint["observation"] as? [String: Any],
              observation["valueWasTruncated"] as? Bool != true,
              let rawValue = stringValue(observation["value"]) else { return nil }
        let value = logicalEditableValue(
            rawValue, placeholderValue: stringValue(observation["placeholderValue"])
        )
        guard !minimalTextEdit(from: beforeValue, to: value).isEmpty else { return nil }
        return ReducerSelection(
            observation: observation, source: source,
            checkpointID: stringValue(checkpoint["checkpointID"]), reason: "checkpoint_recovery"
        )
    }
    let returnSelection = returns.last.flatMap { meaningful($0, source: "pre_return_checkpoint") }
    if stringValue(raw["boundaryReason"]) == "return_pressed" {
        return returnSelection.map { ReducerSelection(
            observation: $0.observation, source: $0.source,
            checkpointID: $0.checkpointID, reason: "immediate_terminal_return"
        ) }
    }

    var latest: (timestamp: UInt64, priority: Int, selection: ReducerSelection)?
    func consider(_ checkpoint: [String: Any], source: String, priority: Int) {
        guard let candidate = meaningful(checkpoint, source: source) else { return }
        let timestamp = uint64Value(checkpoint["eventTimestampNanoseconds"]) ?? 0
        guard lastTimestamp == nil || timestamp <= lastTimestamp! else { return }
        if latest == nil || timestamp > latest!.timestamp
            || (timestamp == latest!.timestamp && priority > latest!.priority) {
            latest = (timestamp, priority, candidate)
        }
    }
    returns.forEach { consider($0, source: "pre_return_checkpoint", priority: 3) }
    pastes.forEach { consider($0, source: "post_paste_checkpoint", priority: 2) }
    mutations.forEach { consider($0, source: "post_input_checkpoint", priority: 1) }

    let terminalErrors = stringArray(raw["afterAXErrors"])
    let terminalInvalid = !terminalErrors.isEmpty
        || (raw["after"] as? [String: Any]) == nil
    if terminalInvalid {
        return latest.map { ReducerSelection(
            observation: $0.selection.observation, source: $0.selection.source,
            checkpointID: $0.selection.checkpointID, reason: "terminal_invalid"
        ) }
    }
    guard let terminal = raw["after"] as? [String: Any],
          terminal["valueWasTruncated"] as? Bool != true,
          let terminalRaw = stringValue(terminal["value"]) else { return nil }
    let terminalValue = logicalEditableValue(
        terminalRaw, placeholderValue: stringValue(terminal["placeholderValue"])
    )
    if let lastReturn = returns.last,
       uint64Value(lastReturn["eventTimestampNanoseconds"]) == lastTimestamp,
       let returnSelection,
       let checkpointRaw = stringValue(returnSelection.observation["value"]) {
        let checkpointValue = logicalEditableValue(
            checkpointRaw,
            placeholderValue: stringValue(returnSelection.observation["placeholderValue"])
        )
        // Return may submit and repopulate/transform a transient field before
        // WRITE_DELAY expires. Preserve the synchronous pre-Return state unless
        // the terminal observation still contains that state (ordinary editor
        // newline/formatting behavior).
        if terminalValue != checkpointValue,
           !terminalValue.contains(checkpointValue) {
            return ReducerSelection(
                observation: returnSelection.observation,
                source: returnSelection.source,
                checkpointID: returnSelection.checkpointID,
                reason: "terminal_does_not_preserve_pre_return"
            )
        }
    }
    if terminalValue == beforeValue, let latest {
        guard latest.selection.source != "post_input_checkpoint" else { return nil }
        return ReducerSelection(
            observation: latest.selection.observation, source: latest.selection.source,
            checkpointID: latest.selection.checkpointID, reason: "terminal_matches_before"
        )
    }
    if terminalValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
       let latest {
        return ReducerSelection(
            observation: latest.selection.observation, source: latest.selection.source,
            checkpointID: latest.selection.checkpointID, reason: "terminal_unpopulated"
        )
    }
    return ReducerSelection(
        observation: terminal, source: "terminal_after",
        checkpointID: nil, reason: "terminal_observation"
    )
}

private struct ReducerAuthorship {
    let segments: [WriteAuthorshipSegment]
    let resolution: String
    let resolvedCompletion: String
    let stateContinuity: String
    let evidence: String?
}

private func reduceAuthorship(
    raw: [String: Any], beforeValue: String,
    usedObservation: [String: Any], observedEdit: TextEdit
) -> ReducerAuthorship {
    let checkpoints = raw["pasteCheckpoints"] as? [[String: Any]] ?? []
    if Set(stringArray(raw["inputHints"])).contains("paste"), checkpoints.isEmpty {
        return unresolvedAuthorship("paste_checkpoint_missing")
    }
    guard !checkpoints.isEmpty else {
        let segments = observedEdit.inserted.isEmpty ? [] : [WriteAuthorshipSegment.authored(observedEdit.inserted)]
        return ReducerAuthorship(
            segments: segments, resolution: "resolved",
            resolvedCompletion: observedEdit.inserted,
            stateContinuity: "single_ax_epoch", evidence: nil
        )
    }
    guard let conditioning = raw["conditioningState"] as? [String: Any],
          let clipboard = conditioning["clipboard"] as? [String: Any],
          let conditionedSnapshot = stringValue(clipboard["snapshotID"]),
          let conditionedCount = intValue(clipboard["changeCount"]),
          clipboard["textWasTruncated"] as? Bool != true else {
        return unresolvedAuthorship("conditioning_clipboard_missing")
    }

    // First use the globally observable path. It retains the net-edit invariant
    // and supports multiple paste spans without duplicating payload supervision.
    var mutations = [ProvenPasteMutation]()
    var allGloballyObservable = true
    for checkpoint in checkpoints {
        guard stringValue(checkpoint["clipboardSnapshotID"]) == conditionedSnapshot,
              intValue(checkpoint["clipboardChangeCount"]) == conditionedCount else {
            return unresolvedAuthorship("clipboard_changed_after_conditioning")
        }
        guard stringArray(checkpoint["prePasteAXErrors"]).isEmpty,
              stringArray(checkpoint["axErrors"]).isEmpty,
              checkpoint["clipboardTextWasTruncated"] as? Bool != true,
              let clipboardText = stringValue(checkpoint["clipboardText"]), !clipboardText.isEmpty,
              let pre = checkpoint["prePasteObservation"] as? [String: Any],
              let post = checkpoint["observation"] as? [String: Any],
              let preRaw = stringValue(pre["value"]), let postRaw = stringValue(post["value"]),
              pre["valueWasTruncated"] as? Bool != true,
              post["valueWasTruncated"] as? Bool != true else {
            return unresolvedAuthorship("paste_checkpoint_incomplete")
        }
        let preValue = logicalEditableValue(preRaw, placeholderValue: stringValue(pre["placeholderValue"]))
        let postValue = logicalEditableValue(postRaw, placeholderValue: stringValue(post["placeholderValue"]))
        let edit = minimalTextEdit(from: preValue, to: postValue)
        if edit.inserted == clipboardText {
            mutations.append(ProvenPasteMutation(
                checkpointID: stringValue(checkpoint["checkpointID"]) ?? "",
                clipboardSnapshotID: conditionedSnapshot,
                characterOffset: edit.characterOffset,
                inserted: clipboardText
            ))
        } else {
            allGloballyObservable = false
        }
    }
    if allGloballyObservable {
        let result = writeAuthorship(overallEdit: observedEdit, pasteMutations: mutations)
        return ReducerAuthorship(
            segments: result.segments, resolution: result.resolution,
            resolvedCompletion: result.resolution == "resolved"
                ? result.segments.map(\.content).joined() : observedEdit.inserted,
            stateContinuity: "single_ax_epoch", evidence: "grounded_clipboard_transition"
        )
    }

    // An AX provider may begin a new observation epoch after a bracketed paste.
    // Only this proven action may bridge epochs; arbitrary resets remain unresolved.
    guard checkpoints.count == 1, let checkpoint = checkpoints.first,
          stringArray(checkpoint["prePasteAXErrors"]).isEmpty,
          stringArray(checkpoint["axErrors"]).isEmpty,
          checkpoint["clipboardTextWasTruncated"] as? Bool != true,
          let clipboardText = stringValue(checkpoint["clipboardText"]), !clipboardText.isEmpty,
          let pre = checkpoint["prePasteObservation"] as? [String: Any],
          let post = checkpoint["observation"] as? [String: Any],
          let preRaw = stringValue(pre["value"]), let postRaw = stringValue(post["value"]),
          let terminalRaw = stringValue(usedObservation["value"]),
          pre["valueWasTruncated"] as? Bool != true,
          post["valueWasTruncated"] as? Bool != true,
          usedObservation["valueWasTruncated"] as? Bool != true else {
        return unresolvedAuthorship("paste_transition_does_not_match_clipboard")
    }
    let laterHints = inputEventsAfterPaste(raw: raw, checkpoint: checkpoint)
    guard laterHints.isDisjoint(with: ["delete", "cut", "undo"]) else {
        return unresolvedAuthorship("post_paste_edit_may_modify_payload")
    }
    let preValue = logicalEditableValue(preRaw, placeholderValue: stringValue(pre["placeholderValue"]))
    let postValue = logicalEditableValue(postRaw, placeholderValue: stringValue(post["placeholderValue"]))
    let terminalValue = logicalEditableValue(
        terminalRaw, placeholderValue: stringValue(usedObservation["placeholderValue"])
    )
    guard let completion = segmentedGroundedPasteCompletion(
        initialValue: beforeValue, prePasteValue: preValue,
        postPasteValue: postValue, terminalValue: terminalValue,
        clipboardText: clipboardText, clipboardSnapshotID: conditionedSnapshot,
        pasteCheckpointID: stringValue(checkpoint["checkpointID"]) ?? ""
    ) else { return unresolvedAuthorship("unproven_ax_epoch_transition") }
    return ReducerAuthorship(
        segments: completion.segments, resolution: "resolved",
        resolvedCompletion: completion.resolvedContent,
        stateContinuity: "segmented_at_grounded_paste",
        evidence: "grounded_paste_ax_epoch_transition"
    )
}

private func unresolvedAuthorship(_ reason: String) -> ReducerAuthorship {
    ReducerAuthorship(
        segments: [], resolution: reason, resolvedCompletion: "",
        stateContinuity: "unresolved", evidence: nil
    )
}

private func inputEventsAfterPaste(raw: [String: Any], checkpoint: [String: Any]) -> Set<String> {
    let timestamp = uint64Value(checkpoint["eventTimestampNanoseconds"]) ?? UInt64.max
    return Set((raw["inputEvents"] as? [[String: Any]] ?? []).compactMap { event in
        guard (uint64Value(event["eventTimestampNanoseconds"]) ?? 0) > timestamp else { return nil }
        return stringValue(event["hint"])
    })
}

private func reducerCursorFidelity(raw: [String: Any], terminalEditOffset: Int) -> [String: Any] {
    guard let before = raw["before"] as? [String: Any],
          let rawBefore = stringValue(before["value"]) else {
        return ["schemaVersion": 1, "status": CursorFidelityStatus.initialCursorUnavailable.rawValue,
                "terminalEditOffsetCharacters": terminalEditOffset]
    }
    let beforeValue = logicalEditableValue(rawBefore, placeholderValue: stringValue(before["placeholderValue"]))
    let cursor = semanticCursorContext(
        in: beforeValue,
        selectionStartUTF16: intValue(before["selectedRangeLocation"]),
        selectionLengthUTF16: intValue(before["selectedRangeLength"]),
        surroundingCharacterCount: 1
    )
    var candidates = [(at: String, id: String, offset: Int)]()
    for key in ["mutationCheckpoints", "pasteCheckpoints", "returnCheckpoints"] {
        for checkpoint in raw[key] as? [[String: Any]] ?? [] {
            guard stringArray(checkpoint["axErrors"]).isEmpty,
                  let observation = checkpoint["observation"] as? [String: Any],
                  observation["valueWasTruncated"] as? Bool != true,
                  let valueRaw = stringValue(observation["value"]),
                  let id = stringValue(observation["observationID"]),
                  let at = stringValue(observation["observedAt"]) else { continue }
            let value = logicalEditableValue(valueRaw, placeholderValue: stringValue(observation["placeholderValue"]))
            let edit = minimalTextEdit(from: beforeValue, to: value)
            if !edit.isEmpty { candidates.append((at, id, edit.characterOffset)) }
        }
    }
    let earliest = candidates.sorted { $0.at == $1.at ? $0.id < $1.id : $0.at < $1.at }.first
    let status = cursorFidelityStatus(
        initialCursorOffset: cursor?.selectionStartCharacters,
        earliestObservedMutationOffset: earliest?.offset,
        terminalEditOffset: terminalEditOffset
    )
    return removeNullOptionals([
        "schemaVersion": 1,
        "status": status.rawValue,
        "initialCursorOffsetCharacters": cursor?.selectionStartCharacters as Any,
        "initialSelectionLengthCharacters": cursor?.selectionLengthCharacters as Any,
        "earliestObservedMutationOffsetCharacters": earliest?.offset as Any,
        "earliestObservedMutationObservationID": earliest?.id as Any,
        "earliestObservedMutationCapturedAt": earliest?.at as Any,
        "terminalEditOffsetCharacters": terminalEditOffset,
    ])
}

private func reducerUnresolved(
    sessionID: String, raw: [String: Any], line: Int, kind: String,
    rule: String, reason: String, details: [String: Any] = [:]
) -> [String: Any] {
    let id = stringValue(raw["recordID"]) ?? "raw-line-\(line)"
    return removeNullOptionals([
        "schemaVersion": 1,
        "sessionID": sessionID,
        "kindCandidate": kind,
        "rawLine": line,
        "sourceRecordIDs": [id],
        "beganAt": raw["beganAt"] as Any,
        "capturedAt": raw["capturedAt"] as Any,
        "rule": rule,
        "reason": reason,
        "details": details,
    ])
}

private func stableEventID(sessionID: String, lineage: [String], ordinal: Int) -> String {
    let material = sessionID + "\u{1f}" + lineage.joined(separator: "\u{1e}") + "\u{1f}\(ordinal)"
    let digest = SHA256.hash(data: Data(material.utf8)).map { String(format: "%02x", $0) }.joined()
    return "evt_" + digest
}

private func sameCapturedReadSurface(_ raw: [String: Any], _ post: [String: Any]) -> Bool {
    intValue(raw["displayID"]) == intValue(post["displayID"])
        && intValue(raw["processIdentifier"]) == intValue(post["processIdentifier"])
        && intValue(raw["windowID"]) == intValue(post["windowID"])
        && stringValue(raw["windowTitle"]) == stringValue(post["windowTitle"])
        && rectanglesApproximatelyEqual(
            raw["windowBounds"] as? [String: Any],
            post["windowBounds"] as? [String: Any]
        )
}

private func rectanglesApproximatelyEqual(
    _ lhs: [String: Any]?, _ rhs: [String: Any]?
) -> Bool {
    guard let lhs, let rhs else { return lhs == nil && rhs == nil }
    return ["x", "y", "width", "height"].allSatisfy {
        abs((doubleValue(lhs[$0]) ?? .infinity) - (doubleValue(rhs[$0]) ?? -.infinity)) <= 1
    }
}

private func reducerReadJSONL(_ url: URL) throws -> [ReducerLine] {
    let text = try String(contentsOf: url, encoding: .utf8)
    var result = [ReducerLine]()
    for (offset, line) in text.split(separator: "\n", omittingEmptySubsequences: true).enumerated() {
        guard let data = String(line).data(using: .utf8),
              let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw Phase1SemanticReducerError.invalidJSON(url.path, offset + 1)
        }
        result.append(ReducerLine(line: offset + 1, object: object))
    }
    return result
}

private func reducerWriteJSONL(_ objects: [[String: Any]], to url: URL) throws {
    var data = Data()
    for object in objects {
        data.append(try JSONSerialization.data(
            withJSONObject: object, options: [.sortedKeys, .withoutEscapingSlashes]
        ))
        data.append(0x0a)
    }
    guard FileManager.default.createFile(
        atPath: url.path, contents: data,
        attributes: [.posixPermissions: NSNumber(value: 0o600)]
    ) else { throw Phase1SemanticReducerError.couldNotCreate(url.path) }
}

private func reducerWriteJSON(_ object: [String: Any], to url: URL) throws {
    var data = try JSONSerialization.data(
        withJSONObject: object, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
    )
    data.append(0x0a)
    guard FileManager.default.createFile(
        atPath: url.path, contents: data,
        attributes: [.posixPermissions: NSNumber(value: 0o600)]
    ) else { throw Phase1SemanticReducerError.couldNotCreate(url.path) }
}

private func reducerSHA256(_ url: URL) throws -> String {
    SHA256.hash(data: try Data(contentsOf: url)).map { String(format: "%02x", $0) }.joined()
}

private func stringValue(_ value: Any?) -> String? { value as? String }
private func nonEmptyString(_ value: Any?) -> String? {
    guard let value = value as? String, !value.isEmpty else { return nil }
    return value
}
private func intValue(_ value: Any?) -> Int? { (value as? NSNumber)?.intValue }
private func uint64Value(_ value: Any?) -> UInt64? { (value as? NSNumber)?.uint64Value }
private func doubleValue(_ value: Any?) -> Double? { (value as? NSNumber)?.doubleValue }
private func stringArray(_ value: Any?) -> [String] { value as? [String] ?? [] }

private func removeNullOptionals(_ object: [String: Any]) -> [String: Any] {
    object.compactMapValues { value in
        let mirror = Mirror(reflecting: value)
        if mirror.displayStyle == .optional {
            return mirror.children.first?.value
        }
        return value is NSNull ? nil : value
    }
}
