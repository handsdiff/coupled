import CryptoKit
import Foundation

public struct Phase1SemanticReducerConfiguration: Sendable {
    public let reducerVersion: String

    public init(reducerVersion: String = "phase1-semantic-v3") {
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

        var candidates = [ReducerCandidate]()
        var dispositions = [ReducerDisposition]()
        for record in raw {
            let object = record.object
            guard object["sessionID"] as? String == sessionID else {
                dispositions.append(ReducerDisposition(line: record.line, object: reducerUnresolved(
                    sessionID: sessionID, raw: object, line: record.line,
                    kind: "unknown", rule: "session_identity",
                    reason: "raw_session_id_mismatch"
                )))
                continue
            }
            switch object["recordType"] as? String {
            case "screen_ocr_observation":
                switch reduceRead(object, sessionID: sessionID) {
                case .failure(let failure):
                    dispositions.append(ReducerDisposition(line: record.line, object: reducerUnresolved(
                        sessionID: sessionID, raw: object, line: record.line,
                        kind: "read", rule: failure.rule, reason: failure.reason,
                        details: failure.details
                    )))
                case .success(let event):
                    guard let capturedAt = stringValue(object["capturedAt"]) else {
                        dispositions.append(ReducerDisposition(line: record.line, object: reducerUnresolved(
                            sessionID: sessionID, raw: object, line: record.line,
                            kind: "read", rule: "semantic_time_overlap_v1",
                            reason: "read_missing_captured_at"
                        )))
                        continue
                    }
                    candidates.append(ReducerCandidate(
                        rawLine: record.line, kind: "read",
                        overlapBoundaryAt: capturedAt, raw: object, event: event
                    ))
                }
            case "active_tap_write_attempt":
                switch reduceWrite(object, sessionID: sessionID) {
                case .failure(let failure):
                    dispositions.append(ReducerDisposition(line: record.line, object: reducerUnresolved(
                        sessionID: sessionID, raw: object, line: record.line,
                        kind: "write", rule: failure.rule, reason: failure.reason,
                        details: failure.details
                    )))
                case .success(let event):
                    guard let beganAt = stringValue(object["beganAt"]) else {
                        dispositions.append(ReducerDisposition(line: record.line, object: reducerUnresolved(
                            sessionID: sessionID, raw: object, line: record.line,
                            kind: "write", rule: "semantic_time_overlap_v1",
                            reason: "write_missing_began_at"
                        )))
                        continue
                    }
                    candidates.append(ReducerCandidate(
                        rawLine: record.line, kind: "write",
                        overlapBoundaryAt: beganAt, raw: object, event: event
                    ))
                }
            default:
                continue
            }
        }

        let staleReadResult = removeStaleDelayedReads(
            candidates: candidates, sessionID: sessionID
        )
        dispositions.append(contentsOf: staleReadResult.dispositions)
        let overlapResult = applySemanticReadOverlap(
            candidates: staleReadResult.events, sessionID: sessionID
        )
        dispositions.append(contentsOf: overlapResult.dispositions)
        var events = overlapResult.events.sorted { $0.rawLine < $1.rawLine }.map(\.event)
        for index in events.indices { events[index]["sequence"] = index + 1 }
        let unresolved = dispositions.sorted {
            if $0.line != $1.line { return $0.line < $1.line }
            return (stringValue($0.object["reason"]) ?? "")
                < (stringValue($1.object["reason"]) ?? "")
        }.map(\.object)

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
            "staleDelayedReadRule": "exclude only when trigger lastActivityAt precedes WRITE beganAt and delayed capturedAt falls within that WRITE interval for the same process",
            "readOverlapOrdering": "READ capturedAt with finalized WRITE beganAt boundaries; raw append order ignored",
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
private struct ReducerCandidate {
    let rawLine: Int
    let kind: String
    let overlapBoundaryAt: String
    let raw: [String: Any]
    var event: [String: Any]
}
private struct ReducerDisposition { let line: Int; let object: [String: Any] }
private struct ReducerOverlapResult {
    let events: [ReducerCandidate]
    let dispositions: [ReducerDisposition]
}
private struct ReducerFailure: Error {
    let rule: String
    let reason: String
    let details: [String: Any]
}

private struct CheckpointGroundedEdit {
    let edit: TextEdit
    let observationCount: Int
}

private struct TaggedReducerCharacter {
    let character: Character
    let originalIndex: Int?
}

/// Uses ordered post-input field states only to choose among edits which are
/// already equivalent minimal reconstructions of the same BEFORE and AFTER.
/// It cannot introduce transient typo text or create a different document
/// transition.
private func checkpointGroundedEquivalentEdit(
    raw: [String: Any],
    beforeValue: String,
    afterValue: String,
    usedObservation: [String: Any],
    canonicalEdit: TextEdit
) -> CheckpointGroundedEdit? {
    guard !(raw["mutationCheckpoints"] as? [[String: Any]] ?? []).isEmpty,
          (raw["pasteCheckpoints"] as? [[String: Any]] ?? []).isEmpty else {
        return nil
    }
    let finalCandidates = equivalentMinimalEdits(
        from: beforeValue, to: afterValue, canonical: canonicalEdit
    )
    guard finalCandidates.count > 1 else { return nil }

    let selectedAt = reducerTimestamp(
        stringValue(usedObservation["observedAt"])
            ?? stringValue(raw["terminalSnapshotAt"])
            ?? stringValue(raw["terminalDecisionAt"])
            ?? ""
    )
    var observations = (raw["mutationCheckpoints"] as? [[String: Any]] ?? [])
        .compactMap { checkpoint -> (capturedAt: String, eventTimestamp: UInt64, value: String)? in
            guard stringArray(checkpoint["axErrors"]).isEmpty,
                  let observation = checkpoint["observation"] as? [String: Any],
                  observation["valueWasTruncated"] as? Bool != true,
                  let rawValue = stringValue(observation["value"]),
                  let capturedAt = stringValue(observation["observedAt"]) else {
                return nil
            }
            if let selectedAt {
                guard let captured = reducerTimestamp(capturedAt),
                      captured <= selectedAt else { return nil }
            }
            return (
                capturedAt,
                uint64Value(checkpoint["eventTimestampNanoseconds"]) ?? 0,
                logicalEditableValue(
                    rawValue,
                    placeholderValue: stringValue(observation["placeholderValue"])
                )
            )
        }
    observations.sort {
        if $0.capturedAt != $1.capturedAt { return $0.capturedAt < $1.capturedAt }
        return $0.eventTimestamp < $1.eventTimestamp
    }

    var tagged = Array(beforeValue).enumerated().map {
        TaggedReducerCharacter(character: $0.element, originalIndex: $0.offset)
    }
    var currentValue = beforeValue
    var expectedCaret: Int?
    var usedObservationCount = 0
    let states = observations.map(\.value) + [afterValue]
    for state in states where state != currentValue {
        let canonical = minimalTextEdit(from: currentValue, to: state)
        guard !canonical.isEmpty else { continue }
        let candidates = equivalentMinimalEdits(
            from: currentValue, to: state, canonical: canonical
        )
        let chosen: TextEdit
        if candidates.count == 1 {
            chosen = candidates[0]
        } else if let expectedCaret {
            let ranked = candidates.map { candidate in
                (
                    candidate,
                    min(
                        abs(candidate.characterOffset - expectedCaret),
                        abs(
                            candidate.characterOffset
                                + candidate.removed.count - expectedCaret
                        )
                    )
                )
            }
            guard let bestDistance = ranked.map(\.1).min(),
                  ranked.filter({ $0.1 == bestDistance }).count == 1,
                  let best = ranked.first(where: { $0.1 == bestDistance }) else {
                return nil
            }
            chosen = best.0
        } else {
            return nil
        }
        guard let updated = applyingTagged(chosen, to: tagged),
              String(updated.map(\.character)) == state else { return nil }
        tagged = updated
        currentValue = state
        expectedCaret = chosen.characterOffset + chosen.inserted.count
        usedObservationCount += 1
    }
    guard currentValue == afterValue,
          String(tagged.map(\.character)) == afterValue else { return nil }

    let survivingOriginals = Set(tagged.compactMap(\.originalIndex))
    let missingOriginals = Set(0..<beforeValue.count).subtracting(survivingOriginals)
    let authoredPositions = Set(tagged.indices.filter { tagged[$0].originalIndex == nil })
    let proven = finalCandidates.filter { candidate in
        let removedEnd = candidate.characterOffset + candidate.removed.count
        let insertedEnd = candidate.characterOffset + candidate.inserted.count
        let removed = Set<Int>(candidate.characterOffset..<removedEnd)
        let inserted = Set<Int>(candidate.characterOffset..<insertedEnd)
        return removed == missingOriginals && inserted == authoredPositions
    }
    guard proven.count == 1, let edit = proven.first,
          edit != canonicalEdit,
          !crossesStructuralBoundary(
            from: canonicalEdit.characterOffset,
            to: edit.characterOffset,
            beforeValue: beforeValue,
            afterValue: afterValue
          ),
          applying(edit, to: beforeValue) == afterValue else { return nil }
    return CheckpointGroundedEdit(
        edit: edit, observationCount: usedObservationCount
    )
}

private func crossesStructuralBoundary(
    from canonicalOffset: Int,
    to groundedOffset: Int,
    beforeValue: String,
    afterValue: String
) -> Bool {
    let lower = min(canonicalOffset, groundedOffset)
    let upper = max(canonicalOffset, groundedOffset)
    guard lower < upper else { return false }
    let structural: Set<Character> = ["\n", "\r", "\u{200B}"]
    let before = Array(beforeValue)
    let after = Array(afterValue)
    let beforeBoundary = before[lower..<min(upper, before.count)]
    let afterBoundary = after[lower..<min(upper, after.count)]
    return beforeBoundary.contains(where: structural.contains)
        || afterBoundary.contains(where: structural.contains)
}

private func equivalentMinimalEdits(
    from before: String,
    to after: String,
    canonical: TextEdit
) -> [TextEdit] {
    let old = Array(before)
    let new = Array(after)
    let removedCount = canonical.removed.count
    let insertedCount = canonical.inserted.count
    var sharedPrefix = 0
    while sharedPrefix < min(old.count, new.count),
          old[sharedPrefix] == new[sharedPrefix] {
        sharedPrefix += 1
    }
    var sharedSuffix = 0
    while sharedSuffix < min(old.count, new.count),
          old[old.count - sharedSuffix - 1]
            == new[new.count - sharedSuffix - 1] {
        sharedSuffix += 1
    }
    let lower = max(0, old.count - removedCount - sharedSuffix)
    let upper = min(
        sharedPrefix,
        min(old.count - removedCount, new.count - insertedCount)
    )
    guard lower <= upper, upper - lower <= 1_024 else { return [canonical] }
    var result = [TextEdit]()
    for offset in lower...upper {
        let removed = String(old[offset..<(offset + removedCount)])
        let inserted = String(new[offset..<(offset + insertedCount)])
        let operation: EditOperation = removed.isEmpty
            ? .insert : inserted.isEmpty ? .delete : .replace
        let candidate = TextEdit(
            operation: operation,
            characterOffset: offset,
            removed: removed,
            inserted: inserted
        )
        if applying(candidate, to: before) == after, !result.contains(candidate) {
            result.append(candidate)
        }
    }
    return result.isEmpty ? [canonical] : result
}

private func applyingTagged(
    _ edit: TextEdit,
    to source: [TaggedReducerCharacter]
) -> [TaggedReducerCharacter]? {
    let removedCount = edit.removed.count
    guard edit.characterOffset >= 0,
          edit.characterOffset + removedCount <= source.count,
          String(
            source[edit.characterOffset..<(edit.characterOffset + removedCount)]
                .map(\.character)
          ) == edit.removed else { return nil }
    var result = source
    result.replaceSubrange(
        edit.characterOffset..<(edit.characterOffset + removedCount),
        with: edit.inserted.map {
            TaggedReducerCharacter(character: $0, originalIndex: nil)
        }
    )
    return result
}

/// A pointer-triggered READ is stale only when its final trigger activity
/// predates a WRITE and its delayed capture lands inside that WRITE interval.
/// Activity which begins after the WRITE starts is a genuine new read
/// opportunity and is deliberately retained.
private func removeStaleDelayedReads(
    candidates: [ReducerCandidate],
    sessionID: String
) -> ReducerOverlapResult {
    let writes = candidates.filter { $0.kind == "write" }
    var accepted = [ReducerCandidate]()
    var dispositions = [ReducerDisposition]()
    for candidate in candidates {
        guard candidate.kind == "read" else {
            accepted.append(candidate)
            continue
        }
        let supersedingWrite = writes
            .filter { staleDelayedRead(candidate, wasSupersededBy: $0) }
            .min { $0.overlapBoundaryAt < $1.overlapBoundaryAt }
        guard let supersedingWrite else {
            accepted.append(candidate)
            continue
        }
        dispositions.append(ReducerDisposition(
            line: candidate.rawLine,
            object: reducerUnresolved(
                sessionID: sessionID, raw: candidate.raw,
                line: candidate.rawLine, kind: "read",
                rule: "semantic_time_stale_delayed_read_v1",
                reason: "read_candidate_superseded_by_write",
                details: [
                    "lastActivityAt": stringValue(candidate.raw["lastActivityAt"]) ?? "",
                    "capturedAt": stringValue(candidate.raw["capturedAt"]) ?? "",
                    "supersedingWriteEventID": stringValue(
                        supersedingWrite.event["eventID"]
                    ) ?? "",
                    "supersedingWriteBeganAt": stringValue(
                        supersedingWrite.raw["beganAt"]
                    ) ?? "",
                    "supersedingWriteLastInputAt": stringValue(
                        supersedingWrite.raw["lastInputAt"]
                    ) ?? "",
                    "supersedingWriteTerminalDecisionAt": stringValue(
                        supersedingWrite.raw["terminalDecisionAt"]
                    ) ?? "",
                ]
            )
        ))
    }
    return ReducerOverlapResult(events: accepted, dispositions: dispositions)
}

private func staleDelayedRead(
    _ read: ReducerCandidate,
    wasSupersededBy write: ReducerCandidate
) -> Bool {
    guard read.kind == "read", write.kind == "write",
          let readProcess = intValue(read.raw["processIdentifier"]),
          let writeProcess = intValue(write.event["processIdentifier"]),
          readProcess == writeProcess,
          let lastActivityAt = stringValue(read.raw["lastActivityAt"]),
          let capturedAt = stringValue(read.raw["capturedAt"]),
          let writeBeganAt = stringValue(write.raw["beganAt"]),
          let terminalDecisionAt = stringValue(write.raw["terminalDecisionAt"]),
          let lastActivity = reducerTimestamp(lastActivityAt),
          let captured = reducerTimestamp(capturedAt),
          let writeBegan = reducerTimestamp(writeBeganAt),
          let terminalDecision = reducerTimestamp(terminalDecisionAt) else {
        return false
    }
    return lastActivity < writeBegan
        && captured >= writeBegan
        && captured <= terminalDecision
}

private func reducerTimestamp(_ value: String) -> Date? {
    ReducerTimestampParser.formatter.date(from: value)
}

private enum ReducerTimestampParser {
    static let formatter: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()
}

/// Overlap is an interpretation of the semantic event timeline, not the order
/// in which asynchronous OCR and delayed WRITE persistence happened to append.
/// A finalized WRITE begins a new reading epoch at beganAt even though it only
/// becomes causally available later at terminalDecisionAt.
private func applySemanticReadOverlap(
    candidates: [ReducerCandidate],
    sessionID: String
) -> ReducerOverlapResult {
    let timeline = candidates.indices.sorted { lhs, rhs in
        let left = candidates[lhs]
        let right = candidates[rhs]
        if left.overlapBoundaryAt != right.overlapBoundaryAt {
            return left.overlapBoundaryAt < right.overlapBoundaryAt
        }
        if left.kind != right.kind {
            return left.kind == "write"
        }
        return left.rawLine < right.rawLine
    }
    var deduplicator = AdjacentViewportDeduplicator()
    var accepted = [ReducerCandidate]()
    var dispositions = [ReducerDisposition]()
    for index in timeline {
        var candidate = candidates[index]
        if candidate.kind == "write" {
            deduplicator.reset()
            accepted.append(candidate)
            continue
        }
        let context = "\(intValue(candidate.raw["processIdentifier"]) ?? -1)|\(intValue(candidate.raw["windowID"]) ?? -1)|\(intValue(candidate.raw["displayID"]) ?? -1)"
        let original = stringValue(candidate.raw["content"]) ?? ""
        guard let emitted = deduplicator.contentToEmit(
            contextIdentifier: context,
            viewportContent: original
        ) else {
            dispositions.append(ReducerDisposition(
                line: candidate.rawLine,
                object: reducerUnresolved(
                    sessionID: sessionID, raw: candidate.raw,
                    line: candidate.rawLine, kind: "read",
                    rule: "semantic_time_adjacent_viewport_overlap_v1",
                    reason: "adjacent_viewport_duplicate",
                    details: [
                        "orderingTimestamp": candidate.overlapBoundaryAt,
                        "orderingField": "capturedAt",
                    ]
                )
            ))
            continue
        }
        candidate.event["content"] = emitted
        let emittedLines = emitted.split(
            separator: "\n", omittingEmptySubsequences: true
        ).count
        candidate.event["emittedLineCount"] = emittedLines
        candidate.event["overlapRemovedLineCount"] = max(
            (intValue(candidate.raw["recognizedLineCount"]) ?? emittedLines)
                - emittedLines,
            0
        )
        if var reduction = candidate.event["reduction"] as? [String: Any] {
            reduction["overlapOrderingField"] = "capturedAt"
            reduction["overlapOrderingTimestamp"] = candidate.overlapBoundaryAt
            candidate.event["reduction"] = reduction
        }
        accepted.append(candidate)
    }
    return ReducerOverlapResult(events: accepted, dispositions: dispositions)
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

    let checkpointGrounding = checkpointGroundedEquivalentEdit(
        raw: raw,
        beforeValue: beforeValue,
        afterValue: afterValue,
        usedObservation: selection.observation,
        canonicalEdit: observedEdit
    )
    let resolvedEdit = checkpointGrounding?.edit ?? observedEdit
    guard applying(resolvedEdit, to: beforeValue) == afterValue,
          resolvedEdit.removed.count == observedEdit.removed.count,
          resolvedEdit.inserted.count == observedEdit.inserted.count else {
        return fail(
            "checkpoint_alignment_is_not_equivalent",
            rule: "checkpoint_grounded_equivalent_diff_v1"
        )
    }

    let authorship = reduceAuthorship(
        raw: raw, beforeValue: beforeValue,
        usedObservation: selection.observation, observedEdit: resolvedEdit
    )
    guard authorship.resolution == "resolved" else {
        return fail(authorship.resolution, rule: "paste_authorship_v1")
    }
    guard !authorship.resolvedCompletion.contains("\u{200B}") else {
        return fail("application_generated_zero_width_scaffold", rule: "authorship_guard_v1")
    }

    let conditioning = raw["conditioningState"] as? [String: Any] ?? [:]
    let cursorFidelity = reducerCursorFidelity(raw: raw, terminalEditOffset: resolvedEdit.characterOffset)
    let target = raw["targetIdentity"] as? [String: Any] ?? [:]
    let eventID = stableEventID(sessionID: sessionID, lineage: [recordID], ordinal: 0)
    let observedOutcome: [String: Any] = [
        "operation": observedEdit.operation.rawValue,
        "characterOffset": observedEdit.characterOffset,
        "removedContent": observedEdit.removed,
        "content": observedEdit.inserted,
    ]
    let outcome: [String: Any] = [
        "operation": resolvedEdit.operation.rawValue,
        "characterOffset": resolvedEdit.characterOffset,
        "removedContent": resolvedEdit.removed,
        "content": resolvedEdit.inserted,
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
        "observedNetEdit": observedOutcome,
        "outcome": outcome,
        "operation": resolvedEdit.operation.rawValue,
        "content": resolvedEdit.inserted,
        "removedContent": resolvedEdit.removed,
        "characterOffset": resolvedEdit.characterOffset,
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
            "alignmentRule": checkpointGrounding == nil
                ? "canonical_minimal_diff"
                : "checkpoint_grounded_equivalent_diff_v1",
            "alignmentObservationCount": checkpointGrounding?.observationCount ?? 0,
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
