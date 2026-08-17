import CryptoKit
import Foundation

public struct Phase1SemanticReducerConfiguration: Sendable {
    public let reducerVersion: String

    public init(reducerVersion: String = "phase1-semantic-v6") {
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
        let fastStartContinuations = reducerFastStartContinuationRecordIDs(raw)
        let navigationChains = reducerNavigationChains(raw)
        let writeOverlapBoundaries = raw.compactMap { record -> ReducerWriteBoundary? in
            guard stringValue(record.object["recordType"]) == "active_tap_write_attempt",
                  (intValue(record.object["inputEventCount"]) ?? 0) > 0,
                  let beganAt = stringValue(record.object["beganAt"]) else { return nil }
            return ReducerWriteBoundary(rawLine: record.line, beganAt: beganAt)
        }
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
                if navigationChains.consumedRecordIDs.contains(
                    stringValue(object["recordID"]) ?? ""
                ) {
                    continue
                }
                let chain = stringValue(object["recordID"])
                    .flatMap { navigationChains.byFirstRecordID[$0] }
                let writeObject = chain?.merged ?? object
                let writeLine = chain?.rawLine ?? record.line
                let writeLineage = chain?.lineage
                    ?? [stringValue(object["recordID"])].compactMap { $0 }
                if let recordID = stringValue(object["recordID"]),
                   fastStartContinuations.contains(recordID) {
                    dispositions.append(ReducerDisposition(
                        line: record.line,
                        object: reducerUnresolved(
                            sessionID: sessionID, raw: object, line: record.line,
                            kind: "write", rule: "fast_start_conditioning_guard_v1",
                            reason: "pre_first_mutation_conditioning_unavailable"
                        )
                    ))
                    continue
                }
                switch reduceWrite(
                    writeObject,
                    sessionID: sessionID,
                    lineage: writeLineage
                ) {
                case .failure(let failure):
                    dispositions.append(ReducerDisposition(line: writeLine, object: reducerUnresolved(
                        sessionID: sessionID, raw: writeObject, line: writeLine,
                        kind: "write", rule: failure.rule, reason: failure.reason,
                        details: failure.details,
                        sourceRecordIDs: writeLineage
                    )))
                case .success(let event):
                    guard let beganAt = stringValue(writeObject["beganAt"]) else {
                        dispositions.append(ReducerDisposition(line: writeLine, object: reducerUnresolved(
                            sessionID: sessionID, raw: writeObject, line: writeLine,
                            kind: "write", rule: "semantic_time_overlap_v1",
                            reason: "write_missing_began_at",
                            sourceRecordIDs: writeLineage
                        )))
                        continue
                    }
                    candidates.append(ReducerCandidate(
                        rawLine: writeLine, kind: "write",
                        overlapBoundaryAt: beganAt, raw: writeObject, event: event
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
        let authorshipReadResult = removeReadsContainingActiveWriteContent(
            candidates: staleReadResult.events, sessionID: sessionID
        )
        dispositions.append(contentsOf: authorshipReadResult.dispositions)
        let overlapResult = applySemanticReadOverlap(
            candidates: authorshipReadResult.events,
            writeBoundaries: writeOverlapBoundaries,
            sessionID: sessionID
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
            "activeWriteReadAuthorshipRule": "exclude a READ captured during a same-process WRITE only when normalized OCR contains at least 24 exact normalized characters from the beginning of the finalized WRITE completion",
            "cutAuthorshipRule": "a cut-only transition remains WRITE history but has no authored target segments",
            "pasteObservationRule": "a premature post-paste checkpoint may use the earliest later same-attempt observation whose local transition contains the exact conditioned clipboard payload once and only structural surrounding characters",
            "ambiguousPasteHistoryRule": "a complete reconstructible paste-containing transition from BEFORE to the selected observation remains WRITE history with unresolved authorship and receives no target loss",
            "navigationContinuationRule": "selection-navigation attempts are one WRITE only when the same retained editable either proves an end-of-field application completion or proves that value, caret, and selection were unchanged within WRITE_DELAY",
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
private struct ReducerWriteBoundary { let rawLine: Int; let beganAt: String }
private struct ReducerOverlapResult {
    let events: [ReducerCandidate]
    let dispositions: [ReducerDisposition]
}
private struct ReducerFailure: Error {
    let rule: String
    let reason: String
    let details: [String: Any]
}

private struct ReducerNavigationChain {
    let rawLine: Int
    let lineage: [String]
    let merged: [String: Any]
}

private struct ReducerNavigationChains {
    let byFirstRecordID: [String: ReducerNavigationChain]
    let consumedRecordIDs: Set<String>
}

/// The collector intentionally persists a boundary before a navigation key is
/// delivered. Continue the same semantic WRITE when the next BEFORE proves
/// either an application completion at the trailing caret or a true no-op:
/// identical value, caret, and selection on the same retained editable. Any
/// observable cursor or selection relocation remains a new opportunity.
private func reducerNavigationChains(
    _ records: [ReducerLine]
) -> ReducerNavigationChains {
    let attempts = records.filter {
        stringValue($0.object["recordType"]) == "active_tap_write_attempt"
    }.sorted {
        let left = stringValue($0.object["beganAt"]) ?? ""
        let right = stringValue($1.object["beganAt"]) ?? ""
        return left == right ? $0.line < $1.line : left < right
    }
    var byFirst = [String: ReducerNavigationChain]()
    var consumed = Set<String>()
    var index = 0
    while index < attempts.count {
        var end = index
        while end + 1 < attempts.count,
              provenNavigationContinuation(
                prior: attempts[end].object,
                next: attempts[end + 1].object
              ) {
            end += 1
        }
        guard end > index else {
            index += 1
            continue
        }
        let members = Array(attempts[index...end])
        let lineage = members.compactMap { stringValue($0.object["recordID"]) }
        if lineage.count == members.count, let firstID = lineage.first {
            let chain = ReducerNavigationChain(
                rawLine: members[0].line,
                lineage: lineage,
                merged: mergedNavigationAttempt(members.map(\.object))
            )
            byFirst[firstID] = chain
            consumed.formUnion(lineage.dropFirst())
        }
        index = end + 1
    }
    return ReducerNavigationChains(
        byFirstRecordID: byFirst,
        consumedRecordIDs: consumed
    )
}

private func provenNavigationContinuation(
    prior: [String: Any], next: [String: Any]
) -> Bool {
    let priorHints = Set(stringArray(prior["inputHints"]))
    let nextHints = Set(stringArray(next["inputHints"]))
    guard stringValue(prior["boundaryReason"]) == "selection_navigation",
          !priorHints.isEmpty,
          !nextHints.isEmpty,
          sameRetainedEditable(prior, next),
          sameConditionedClipboard(prior, next),
          let priorLast = stringValue(prior["lastInputAt"]).flatMap(reducerTimestamp),
          let nextBegan = stringValue(next["beganAt"]).flatMap(reducerTimestamp),
          nextBegan >= priorLast,
          nextBegan.timeIntervalSince(priorLast)
            <= (doubleValue(prior["configuredWriteDelaySeconds"]) ?? 3),
          stringArray(prior["afterAXErrors"]).isEmpty,
          stringArray(next["beforeAXErrors"]).isEmpty,
          let priorAfter = prior["after"] as? [String: Any],
          let nextBefore = next["before"] as? [String: Any],
          priorAfter["valueWasTruncated"] as? Bool != true,
          nextBefore["valueWasTruncated"] as? Bool != true,
          let priorRaw = stringValue(priorAfter["value"]),
          let nextRaw = stringValue(nextBefore["value"]) else { return false }
    let priorValue = logicalEditableValue(
        priorRaw, placeholderValue: stringValue(priorAfter["placeholderValue"])
    )
    let nextValue = logicalEditableValue(
        nextRaw, placeholderValue: stringValue(nextBefore["placeholderValue"])
    )
    let priorLocation = intValue(priorAfter["selectedRangeLocation"])
    let nextLocation = intValue(nextBefore["selectedRangeLocation"])
    let priorLength = intValue(priorAfter["selectedRangeLength"])
    let nextLength = intValue(nextBefore["selectedRangeLength"])
    if priorValue == nextValue,
       priorLocation == nextLocation,
       priorLength == nextLength {
        return true
    }
    guard priorHints.isSubset(of: ["typed"]),
          nextHints.contains("typed"),
          nextHints.isDisjoint(with: ["cut", "delete", "paste", "undo_redo"]),
          !priorValue.isEmpty,
          nextValue.count > priorValue.count,
          nextValue.hasPrefix(priorValue),
          priorLength == 0,
          nextLength == 0,
          priorLocation == priorValue.utf16.count,
          nextLocation == nextValue.utf16.count else { return false }
    let completion = minimalTextEdit(from: priorValue, to: nextValue)
    return completion.removed.isEmpty && !completion.inserted.isEmpty
}

private func sameRetainedEditable(
    _ lhs: [String: Any], _ rhs: [String: Any]
) -> Bool {
    guard let left = lhs["targetIdentity"] as? [String: Any],
          let right = rhs["targetIdentity"] as? [String: Any] else { return false }
    return intValue(left["elementHash"]) == intValue(right["elementHash"])
        && intValue(left["processIdentifier"]) == intValue(right["processIdentifier"])
        && stringValue(left["bundleIdentifier"]) == stringValue(right["bundleIdentifier"])
        && stringValue(left["windowTitle"]) == stringValue(right["windowTitle"])
        && stringValue(left["role"]) == stringValue(right["role"])
        && stringValue(left["fieldDescription"]) == stringValue(right["fieldDescription"])
        && stringValue(left["fieldLabel"]) == stringValue(right["fieldLabel"])
}

private func sameConditionedClipboard(
    _ lhs: [String: Any], _ rhs: [String: Any]
) -> Bool {
    let left = (lhs["conditioningState"] as? [String: Any])?["clipboard"]
        as? [String: Any]
    let right = (rhs["conditioningState"] as? [String: Any])?["clipboard"]
        as? [String: Any]
    if left == nil || right == nil { return left == nil && right == nil }
    return intValue(left?["changeCount"]) == intValue(right?["changeCount"])
        && stringValue(left?["textSHA256"]) == stringValue(right?["textSHA256"])
        && (left?["textWasTruncated"] as? Bool)
            == (right?["textWasTruncated"] as? Bool)
}

private func mergedNavigationAttempt(
    _ attempts: [[String: Any]]
) -> [String: Any] {
    guard let first = attempts.first, let last = attempts.last else { return [:] }
    var merged = first
    for key in [
        "after", "afterAXErrors", "boundaryReason", "lastEventTimestampNanoseconds",
        "lastInputAt", "observedAt", "terminalDecisionAt", "terminalSnapshotAt",
    ] {
        if let value = last[key] { merged[key] = value }
    }
    for key in ["inputEvents", "mutationCheckpoints", "pasteCheckpoints", "returnCheckpoints"] {
        merged[key] = attempts.flatMap { $0[key] as? [[String: Any]] ?? [] }
    }
    merged["inputEventCount"] = attempts.reduce(0) {
        $0 + (intValue($1["inputEventCount"]) ?? 0)
    }
    merged["inputHints"] = Array(Set(attempts.flatMap {
        stringArray($0["inputHints"])
    })).sorted()
    merged["tapTimeoutCountDuringBurst"] = attempts.reduce(UInt64(0)) {
        $0 + (uint64Value($1["tapTimeoutCountDuringBurst"]) ?? 0)
    }
    merged["semanticComposition"] = "same_editable_navigation_chain"
    return merged
}

/// Some renderer-backed editors expose a non-editable container for the first
/// key, then replace it with the real editable before the next key. The later
/// BEFORE already contains that first mutation, so it is not a valid query for
/// a focus-time next-content target. This narrow detector rejects only the
/// immediately adjacent, one-character continuation demonstrated by the raw
/// evidence; it does not attempt to invent the missing conditioning state.
private func reducerFastStartContinuationRecordIDs(
    _ records: [ReducerLine]
) -> Set<String> {
    let attempts = records.filter {
        stringValue($0.object["recordType"]) == "active_tap_write_attempt"
    }
    var result = Set<String>()
    for index in attempts.indices.dropFirst() {
        let prior = attempts[attempts.index(before: index)].object
        let current = attempts[index].object
        guard stringValue(prior["boundaryReason"]) == "target_changed",
              intValue(prior["inputEventCount"]) == 1,
              Set(stringArray(prior["inputHints"])) == ["typed"],
              (prior["before"] as? [String: Any]) == nil,
              !stringArray(prior["beforeAXErrors"]).isEmpty,
              stringValue(prior["bundleIdentifier"])
                == stringValue(current["bundleIdentifier"]),
              intValue(prior["processIdentifier"])
                == intValue(current["processIdentifier"]),
              let priorAt = stringValue(prior["beganAt"]).flatMap(reducerTimestamp),
              let currentAt = stringValue(current["beganAt"]).flatMap(reducerTimestamp),
              currentAt >= priorAt,
              currentAt.timeIntervalSince(priorAt) <= 0.5,
              let before = current["before"] as? [String: Any],
              stringArray(current["beforeAXErrors"]).isEmpty,
              before["valueWasTruncated"] as? Bool != true,
              let rawBefore = stringValue(before["value"]),
              logicalEditableValue(
                rawBefore,
                placeholderValue: stringValue(before["placeholderValue"])
              ).count == 1,
              let firstCheckpoint = (current["mutationCheckpoints"] as? [[String: Any]])?.first,
              stringArray(firstCheckpoint["axErrors"]).isEmpty,
              let observation = firstCheckpoint["observation"] as? [String: Any],
              observation["valueWasTruncated"] as? Bool != true,
              let rawCheckpoint = stringValue(observation["value"]),
              let recordID = stringValue(current["recordID"]) else { continue }
        let beforeValue = logicalEditableValue(
            rawBefore, placeholderValue: stringValue(before["placeholderValue"])
        )
        let checkpointValue = logicalEditableValue(
            rawCheckpoint,
            placeholderValue: stringValue(observation["placeholderValue"])
        )
        guard checkpointValue.hasPrefix(beforeValue),
              checkpointValue.count > beforeValue.count else { continue }
        result.insert(recordID)
    }
    return result
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

/// A new pointer trigger during a long WRITE can be a genuine read opportunity,
/// but the resulting screenshot may also contain the in-progress editable. We
/// initially reject the whole READ only when the finalized WRITE proves that a
/// substantial exact prefix of user output was present in OCR. This avoids
/// treating outbound text as later inbound context without attempting fragile
/// line-level OCR surgery.
private func removeReadsContainingActiveWriteContent(
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
        let contaminated = writes.compactMap { write -> (ReducerCandidate, Int)? in
            guard let matched = activeWritePrefixMatchLength(
                read: candidate, write: write
            ) else { return nil }
            return (write, matched)
        }.max { $0.1 < $1.1 }
        guard let (write, matchedLength) = contaminated else {
            accepted.append(candidate)
            continue
        }
        dispositions.append(ReducerDisposition(
            line: candidate.rawLine,
            object: reducerUnresolved(
                sessionID: sessionID, raw: candidate.raw,
                line: candidate.rawLine, kind: "read",
                rule: "active_write_read_authorship_guard_v1",
                reason: "read_contains_active_write_content",
                details: [
                    "capturedAt": stringValue(candidate.raw["capturedAt"]) ?? "",
                    "activeWriteEventID": stringValue(write.event["eventID"]) ?? "",
                    "activeWriteBeganAt": stringValue(write.raw["beganAt"]) ?? "",
                    "activeWriteTerminalDecisionAt": stringValue(
                        write.raw["terminalDecisionAt"]
                    ) ?? "",
                    "matchedNormalizedPrefixCharacterCount": matchedLength,
                ]
            )
        ))
    }
    return ReducerOverlapResult(events: accepted, dispositions: dispositions)
}

private func activeWritePrefixMatchLength(
    read: ReducerCandidate,
    write: ReducerCandidate
) -> Int? {
    guard read.kind == "read", write.kind == "write",
          intValue(read.raw["processIdentifier"])
            == intValue(write.event["processIdentifier"]),
          let captured = stringValue(read.raw["capturedAt"]).flatMap(reducerTimestamp),
          let began = stringValue(write.raw["beganAt"]).flatMap(reducerTimestamp),
          let terminal = stringValue(write.raw["terminalDecisionAt"]).flatMap(reducerTimestamp),
          captured >= began, captured <= terminal,
          let readContent = stringValue(read.raw["content"]),
          let completion = stringValue(write.event["resolvedCompletion"]) else {
        return nil
    }
    let normalizedRead = normalizedReducerText(readContent)
    let normalizedWrite = normalizedReducerText(completion)
    guard normalizedWrite.count >= 24 else { return nil }
    let maximum = min(normalizedWrite.count, 512)
    for length in stride(from: maximum, through: 24, by: -1) {
        if normalizedRead.contains(String(normalizedWrite.prefix(length))) {
            return length
        }
    }
    return nil
}

private func normalizedReducerText(_ value: String) -> String {
    value.split(whereSeparator: { $0.isWhitespace })
        .joined(separator: " ")
        .lowercased()
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
    writeBoundaries: [ReducerWriteBoundary],
    sessionID: String
) -> ReducerOverlapResult {
    enum TimelineItem {
        case candidate(Int)
        case writeBoundary(ReducerWriteBoundary)
    }
    func ordering(_ item: TimelineItem) -> (String, Int, Int) {
        switch item {
        case .candidate(let index):
            let candidate = candidates[index]
            return (
                candidate.overlapBoundaryAt,
                candidate.kind == "write" ? 0 : 1,
                candidate.rawLine
            )
        case .writeBoundary(let boundary):
            return (boundary.beganAt, 0, boundary.rawLine)
        }
    }
    let timeline = (
        candidates.indices.map(TimelineItem.candidate)
            + writeBoundaries.map(TimelineItem.writeBoundary)
    ).sorted { lhs, rhs in
        let left = ordering(lhs)
        let right = ordering(rhs)
        if left.0 != right.0 { return left.0 < right.0 }
        if left.1 != right.1 { return left.1 < right.1 }
        return left.2 < right.2
    }
    var deduplicator = AdjacentViewportDeduplicator()
    var accepted = [ReducerCandidate]()
    var dispositions = [ReducerDisposition]()
    for item in timeline {
        guard case .candidate(let index) = item else {
            deduplicator.reset()
            continue
        }
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

private func reduceWrite(
    _ raw: [String: Any],
    sessionID: String,
    lineage: [String]? = nil
)
    -> Result<[String: Any], ReducerFailure>
{
    func fail(_ reason: String, rule: String = "write_observation_selection_v1", details: [String: Any] = [:])
        -> Result<[String: Any], ReducerFailure> {
        .failure(ReducerFailure(rule: rule, reason: reason, details: details))
    }
    guard let recordID = stringValue(raw["recordID"]) else { return fail("missing_record_id") }
    let sourceRecordIDs = lineage ?? [recordID]
    let composedNavigation = sourceRecordIDs.count > 1
        && stringValue(raw["semanticComposition"])
            == "same_editable_navigation_chain"
    guard (raw["tapTimeoutCountDuringBurst"] as? NSNumber)?.uint64Value ?? 0 == 0 else {
        return fail("tap_timeout")
    }
    if let category = sensitiveWriteFieldCategory(raw) {
        return fail(
            "sensitive_input_field",
            rule: "sensitive_input_guard_v1",
            details: ["category": category]
        )
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
    let hints = Set(stringArray(raw["inputHints"]))
    let cutOnly = !hints.isEmpty
        && hints.contains("cut")
        && hints.isSubset(of: ["cut", "navigation"])
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
    if cutOnly, afterValue.count >= beforeValue.count {
        return fail(
            "cut_only_state_did_not_contract",
            rule: "cut_authorship_guard_v1",
            details: [
                "beforeCharacterCount": beforeValue.count,
                "selectedObservationCharacterCount": afterValue.count,
            ]
        )
    }
    let observedEdit = minimalTextEdit(from: beforeValue, to: afterValue)
    guard !observedEdit.isEmpty, applying(observedEdit, to: beforeValue) == afterValue else {
        return fail("empty_or_non_reconstructing_edit")
    }
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
    if Set(stringArray(raw["inputHints"])).isDisjoint(with: ["paste"]),
       resolvedEdit.removed.count >= 16,
       resolvedEdit.inserted.contains(resolvedEdit.removed) {
        return fail(
            "noncontiguous_authorship_unresolved",
            rule: "application_formatting_authorship_guard_v1",
            details: [
                "observedInsertedCharacterCount": resolvedEdit.inserted.count,
                "preservedInteriorCharacterCount": resolvedEdit.removed.count,
            ]
        )
    }

    let interpretedAuthorship = cutOnly
        ? ReducerAuthorship(
            segments: [],
            resolution: "resolved",
            resolvedCompletion: "",
            stateContinuity: "single_ax_epoch",
            evidence: "cut_only_no_authored_content",
            evidenceObservationID: nil,
            semanticEdit: nil
        )
        : reduceAuthorship(
            raw: raw, beforeValue: beforeValue,
            usedObservation: selection.observation, observedEdit: resolvedEdit
        )
    let unresolvedPasteReason: String?
    let authorship: ReducerAuthorship
    if interpretedAuthorship.resolution != "resolved", hints.contains("paste") {
        unresolvedPasteReason = interpretedAuthorship.resolution
        let contextSegments = resolvedEdit.inserted.isEmpty
            ? []
            : [WriteAuthorshipSegment(
                type: "unresolved_paste_transition",
                content: resolvedEdit.inserted
            )]
        authorship = ReducerAuthorship(
            segments: contextSegments,
            resolution: "unresolved",
            resolvedCompletion: resolvedEdit.inserted,
            stateContinuity: "observed_document_transition_unresolved_authorship",
            evidence: "complete_before_selected_observation_minimal_diff",
            evidenceObservationID: stringValue(selection.observation["observationID"]),
            semanticEdit: nil
        )
    } else {
        unresolvedPasteReason = nil
        guard interpretedAuthorship.resolution == "resolved" else {
            return fail(interpretedAuthorship.resolution, rule: "paste_authorship_v1")
        }
        authorship = interpretedAuthorship
    }
    guard !authorship.segments.contains(where: {
        $0.type == "authored_text" && $0.content.contains("\u{200B}")
    }) else {
        return fail("application_generated_zero_width_scaffold", rule: "authorship_guard_v1")
    }

    let conditioning = raw["conditioningState"] as? [String: Any] ?? [:]
    let semanticEdit = authorship.semanticEdit ?? resolvedEdit
    let cursorFidelity = reducerCursorFidelity(
        raw: raw,
        terminalEditOffset: semanticEdit.characterOffset
    )
    let target = raw["targetIdentity"] as? [String: Any] ?? [:]
    let eventID = stableEventID(
        sessionID: sessionID,
        lineage: sourceRecordIDs,
        ordinal: 0
    )
    let observedOutcome: [String: Any] = [
        "operation": observedEdit.operation.rawValue,
        "characterOffset": observedEdit.characterOffset,
        "removedContent": observedEdit.removed,
        "content": observedEdit.inserted,
    ]
    let outcome: [String: Any] = [
        "operation": semanticEdit.operation.rawValue,
        "characterOffset": semanticEdit.characterOffset,
        "removedContent": semanticEdit.removed,
        "content": semanticEdit.inserted,
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
    let authorshipEvidence: Any
    if unresolvedPasteReason != nil {
        authorshipEvidence = "complete_before_selected_observation_minimal_diff"
    } else if composedNavigation {
        authorshipEvidence = "same_editable_navigation_completion"
    } else {
        authorshipEvidence = authorship.evidence ?? NSNull()
    }
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
        "authorshipEvidence": authorshipEvidence,
        "authorshipUnresolvedReason": unresolvedPasteReason ?? NSNull(),
        "authorshipObservationID": authorship.evidenceObservationID ?? NSNull(),
        "authorshipSegments": segments,
        "resolvedCompletion": authorship.resolvedCompletion,
        "stateContinuity": unresolvedPasteReason != nil
            ? authorship.stateContinuity
            : composedNavigation
            ? "same_editable_navigation_chain"
            : authorship.stateContinuity,
        "observedNetEdit": observedOutcome,
        "outcome": outcome,
        "operation": semanticEdit.operation.rawValue,
        "content": semanticEdit.inserted,
        "removedContent": semanticEdit.removed,
        "characterOffset": semanticEdit.characterOffset,
        "inputEventCount": intValue(raw["inputEventCount"]) ?? 0,
        "appName": appName,
        "bundleIdentifier": bundle ?? NSNull(),
        "processIdentifier": process,
        "windowTitle": window ?? NSNull(),
        "sourceRecordIDs": sourceRecordIDs,
        "reduction": [
            "schemaVersion": 1,
            "rule": unresolvedPasteReason != nil
                ? "observable_ambiguous_paste_transition_v1"
                : composedNavigation
                ? "same_editable_navigation_chain_v2"
                : "write_observation_selection_v1",
            "reason": unresolvedPasteReason
                ?? (composedNavigation
                ? "proven_application_or_noop_navigation_chain"
                : selection.reason),
            "selectedObservationID": stringValue(selection.observation["observationID"]) ?? "",
            "selectedObservationSource": selection.source,
            "alignmentRule": checkpointGrounding == nil
                ? "canonical_minimal_diff"
                : "checkpoint_grounded_equivalent_diff_v1",
            "alignmentObservationCount": checkpointGrounding?.observationCount ?? 0,
            "rawLineage": sourceRecordIDs,
            "outputOrdinal": 0,
        ],
    ]
    // JSONSerialization cannot encode Swift optionals hidden in Any.
    event = removeNullOptionals(event)
    return .success(event)
}

private func sensitiveWriteFieldCategory(_ raw: [String: Any]) -> String? {
    let conditioning = raw["conditioningState"] as? [String: Any]
    let destination = conditioning?["destination"] as? [String: Any] ?? [:]
    let target = raw["targetIdentity"] as? [String: Any] ?? [:]
    let role = (
        stringValue(destination["role"])
            ?? stringValue(target["role"])
            ?? ""
    ).lowercased()
    if role.contains("securetextfield") { return "secure_text_field" }

    let descriptors = [
        stringValue(destination["fieldDescription"]),
        stringValue(destination["fieldLabel"]),
        stringValue(target["fieldDescription"]),
        stringValue(target["fieldLabel"]),
    ].compactMap { $0?.lowercased() }.joined(separator: " ")
    guard !descriptors.isEmpty else { return nil }
    if descriptors.range(
        of: #"\bdigit\s+\d+\s+of\s+\d+\b"#,
        options: .regularExpression
    ) != nil {
        return "segmented_verification_code"
    }
    for marker in [
        "one-time code", "one time code", "verification code",
        "security code", "authentication code", "passcode", "password",
    ] where descriptors.contains(marker) {
        return "credential_or_verification_field"
    }
    return nil
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
    if let checkpoint = reliableFinalMutationCheckpoint(
        raw: raw,
        beforeValue: beforeValue,
        terminalValue: terminalValue,
        lastEventTimestamp: lastTimestamp
    ) {
        return checkpoint
    }
    if let checkpoint = reliableCutMutationCheckpoint(
        raw: raw,
        beforeValue: beforeValue,
        lastEventTimestamp: lastTimestamp
    ) {
        return checkpoint
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

private func reliableCutMutationCheckpoint(
    raw: [String: Any],
    beforeValue: String,
    lastEventTimestamp: UInt64?
) -> ReducerSelection? {
    let hints = Set(stringArray(raw["inputHints"]))
    guard !hints.isEmpty,
          hints.contains("cut"),
          hints.isSubset(of: ["cut", "navigation"]),
          let lastEventTimestamp else { return nil }
    let candidates = (raw["mutationCheckpoints"] as? [[String: Any]] ?? [])
        .filter {
            uint64Value($0["eventTimestampNanoseconds"]) == lastEventTimestamp
                && stringArray($0["axErrors"]).isEmpty
        }
        .compactMap { checkpoint -> ([String: Any], String)? in
            guard let observation = checkpoint["observation"] as? [String: Any],
                  observation["valueWasTruncated"] as? Bool != true,
                  let rawValue = stringValue(observation["value"]) else {
                return nil
            }
            let value = logicalEditableValue(
                rawValue,
                placeholderValue: stringValue(observation["placeholderValue"])
            )
            guard value.count < beforeValue.count,
                  !minimalTextEdit(from: beforeValue, to: value).isEmpty else {
                return nil
            }
            return (observation, stringValue(checkpoint["checkpointID"]) ?? "")
        }
    guard let selected = candidates.last else { return nil }
    return ReducerSelection(
        observation: selected.0,
        source: "post_input_checkpoint",
        checkpointID: selected.1,
        reason: "cut_post_input_checkpoint"
    )
}

/// Recover only the demonstrated catastrophic AX epoch jump: a complete
/// checkpoint captured after the final input continues a locally coherent
/// mutation trajectory, while the later terminal state replaces hundreds of
/// characters on both sides without another input. Smaller post-input changes
/// remain ordinary terminal application behavior.
private func reliableFinalMutationCheckpoint(
    raw: [String: Any],
    beforeValue: String,
    terminalValue: String,
    lastEventTimestamp: UInt64?
) -> ReducerSelection? {
    guard (raw["pasteCheckpoints"] as? [[String: Any]] ?? []).isEmpty,
          let lastEventTimestamp,
          let lastInputAt = stringValue(raw["lastInputAt"]).flatMap(reducerTimestamp) else {
        return nil
    }
    let terminalTransition = minimalTextEdit(from: beforeValue, to: terminalValue)
    guard terminalTransition.removed.count >= 256,
          terminalTransition.inserted.count >= 256 else { return nil }

    let complete = (raw["mutationCheckpoints"] as? [[String: Any]] ?? [])
        .compactMap { checkpoint -> (UInt64, Date, [String: Any], String, String)? in
            guard stringArray(checkpoint["axErrors"]).isEmpty,
                  let timestamp = uint64Value(checkpoint["eventTimestampNanoseconds"]),
                  let observation = checkpoint["observation"] as? [String: Any],
                  observation["valueWasTruncated"] as? Bool != true,
                  let rawValue = stringValue(observation["value"]),
                  let capturedText = stringValue(observation["observedAt"]),
                  let capturedAt = reducerTimestamp(capturedText) else { return nil }
            return (
                timestamp,
                capturedAt,
                observation,
                logicalEditableValue(
                    rawValue,
                    placeholderValue: stringValue(observation["placeholderValue"])
                ),
                stringValue(checkpoint["checkpointID"]) ?? ""
            )
        }
        .sorted {
            if $0.0 != $1.0 { return $0.0 < $1.0 }
            return $0.1 < $1.1
        }
    guard complete.count >= 2,
          let final = complete.last,
          final.0 == lastEventTimestamp,
          final.1 >= lastInputAt,
          !minimalTextEdit(from: beforeValue, to: final.3).isEmpty else {
        return nil
    }
    let finalToTerminal = minimalTextEdit(from: final.3, to: terminalValue)
    guard finalToTerminal.removed.count >= 256,
          finalToTerminal.inserted.count >= 256 else { return nil }

    let recent = Array(complete.suffix(16))
    for pair in zip(recent, recent.dropFirst()) {
        let transition = minimalTextEdit(from: pair.0.3, to: pair.1.3)
        guard transition.removed.count + transition.inserted.count <= 16 else {
            return nil
        }
    }
    return ReducerSelection(
        observation: final.2,
        source: "post_input_checkpoint",
        checkpointID: final.4,
        reason: "terminal_ax_epoch_discontinuity"
    )
}

private struct ReducerAuthorship {
    let segments: [WriteAuthorshipSegment]
    let resolution: String
    let resolvedCompletion: String
    let stateContinuity: String
    let evidence: String?
    let evidenceObservationID: String?
    let semanticEdit: TextEdit?
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
            stateContinuity: "single_ax_epoch", evidence: nil,
            evidenceObservationID: nil, semanticEdit: nil
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
            stateContinuity: "single_ax_epoch", evidence: "grounded_clipboard_transition",
            evidenceObservationID: checkpoints.last
                .flatMap { $0["observation"] as? [String: Any] }
                .flatMap { stringValue($0["observationID"]) },
            semanticEdit: nil
        )
    }

    if checkpoints.count == 1,
       let delayed = delayedGroundedPasteAuthorship(
        raw: raw,
        checkpoint: checkpoints[0],
        conditionedSnapshotID: conditionedSnapshot,
        beforeValue: beforeValue,
        usedObservation: usedObservation
       ) {
        return delayed
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
    guard laterHints.isDisjoint(with: ["delete", "cut", "undo_redo"]) else {
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
        evidence: "grounded_paste_ax_epoch_transition",
        evidenceObservationID: stringValue(usedObservation["observationID"]),
        semanticEdit: nil
    )
}

/// Electron may expose the pre-paste value again at the fixed 50 ms checkpoint
/// even though the paste settles shortly afterward. Search only ordered
/// observations from the same retained attempt. The transition must contain
/// the conditioned clipboard exactly once, may replace only the observed
/// selection, and may add only structural whitespace around that payload.
private func delayedGroundedPasteAuthorship(
    raw: [String: Any],
    checkpoint: [String: Any],
    conditionedSnapshotID: String,
    beforeValue: String,
    usedObservation: [String: Any]
) -> ReducerAuthorship? {
    guard stringValue(checkpoint["clipboardSnapshotID"]) == conditionedSnapshotID,
          stringArray(checkpoint["prePasteAXErrors"]).isEmpty,
          checkpoint["clipboardTextWasTruncated"] as? Bool != true,
          let clipboardText = stringValue(checkpoint["clipboardText"]),
          !clipboardText.isEmpty,
          let pre = checkpoint["prePasteObservation"] as? [String: Any],
          pre["valueWasTruncated"] as? Bool != true,
          let preRaw = stringValue(pre["value"]),
          let pasteTimestamp = uint64Value(checkpoint["eventTimestampNanoseconds"]),
          inputEventsAfterPaste(raw: raw, checkpoint: checkpoint)
            .isSubset(of: ["return", "navigation"]) else { return nil }
    let preValue = logicalEditableValue(
        preRaw, placeholderValue: stringValue(pre["placeholderValue"])
    )
    guard preValue == beforeValue else { return nil }
    guard let terminalRaw = stringValue(usedObservation["value"]),
          usedObservation["valueWasTruncated"] as? Bool != true,
          logicalEditableValue(
            terminalRaw,
            placeholderValue: stringValue(usedObservation["placeholderValue"])
          ).contains(clipboardText) else { return nil }

    var observations = [[String: Any]]()
    if let immediate = checkpoint["observation"] as? [String: Any] {
        observations.append(immediate)
    }
    for key in ["mutationCheckpoints", "returnCheckpoints"] {
        for item in raw[key] as? [[String: Any]] ?? []
            where (uint64Value(item["eventTimestampNanoseconds"]) ?? 0) >= pasteTimestamp {
            if stringArray(item["axErrors"]).isEmpty,
               let observation = item["observation"] as? [String: Any] {
                observations.append(observation)
            }
        }
    }
    observations.append(usedObservation)
    var seen = Set<String>()
    observations = observations.filter {
        guard let id = stringValue($0["observationID"]) else { return false }
        return seen.insert(id).inserted
    }.sorted {
        let left = stringValue($0["observedAt"]) ?? ""
        let right = stringValue($1["observedAt"]) ?? ""
        if left != right { return left < right }
        return (stringValue($0["observationID"]) ?? "")
            < (stringValue($1["observationID"]) ?? "")
    }

    for observation in observations {
        guard observation["valueWasTruncated"] as? Bool != true,
              let rawValue = stringValue(observation["value"]),
              let observationID = stringValue(observation["observationID"]) else {
            continue
        }
        let value = logicalEditableValue(
            rawValue,
            placeholderValue: stringValue(observation["placeholderValue"])
        )
        let edit = minimalTextEdit(from: preValue, to: value)
        guard applying(edit, to: preValue) == value,
              pasteRemovalMatchesSelection(
                edit: edit, preValue: preValue, preObservation: pre
              ),
              let payloadRange = singleOccurrence(
                of: clipboardText, in: edit.inserted
              ) else { continue }
        let prefix = String(edit.inserted[..<payloadRange.lowerBound])
        let suffix = String(edit.inserted[payloadRange.upperBound...])
        guard pasteStructuralText(prefix), pasteStructuralText(suffix) else {
            continue
        }
        return ReducerAuthorship(
            segments: [.paste(
                edit.inserted,
                clipboardSnapshotID: conditionedSnapshotID,
                pasteCheckpointID: stringValue(checkpoint["checkpointID"]) ?? ""
            )],
            resolution: "resolved",
            resolvedCompletion: edit.inserted,
            stateContinuity: "same_ax_field_delayed_paste_observation",
            evidence: "grounded_delayed_paste_observation",
            evidenceObservationID: observationID,
            semanticEdit: edit
        )
    }
    return nil
}

private func pasteRemovalMatchesSelection(
    edit: TextEdit,
    preValue: String,
    preObservation: [String: Any]
) -> Bool {
    if edit.removed.isEmpty { return true }
    guard let context = semanticCursorContext(
        in: preValue,
        selectionStartUTF16: intValue(preObservation["selectedRangeLocation"]),
        selectionLengthUTF16: intValue(preObservation["selectedRangeLength"]),
        surroundingCharacterCount: 0
    ) else { return false }
    return context.selectionLengthCharacters > 0
        && edit.characterOffset == context.selectionStartCharacters
        && edit.removed == context.selectedText
}

private func singleOccurrence(
    of needle: String,
    in haystack: String
) -> Range<String.Index>? {
    guard let first = haystack.range(of: needle) else { return nil }
    guard haystack.range(
        of: needle,
        range: first.upperBound..<haystack.endIndex
    ) == nil else { return nil }
    return first
}

private func pasteStructuralText(_ value: String) -> Bool {
    value.allSatisfy { $0.isWhitespace || $0 == "\u{200B}" }
}

private func unresolvedAuthorship(_ reason: String) -> ReducerAuthorship {
    ReducerAuthorship(
        segments: [], resolution: reason, resolvedCompletion: "",
        stateContinuity: "unresolved", evidence: nil,
        evidenceObservationID: nil, semanticEdit: nil
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
    rule: String, reason: String, details: [String: Any] = [:],
    sourceRecordIDs: [String]? = nil
) -> [String: Any] {
    let id = stringValue(raw["recordID"]) ?? "raw-line-\(line)"
    return removeNullOptionals([
        "schemaVersion": 1,
        "sessionID": sessionID,
        "kindCandidate": kind,
        "rawLine": line,
        "sourceRecordIDs": sourceRecordIDs ?? [id],
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
