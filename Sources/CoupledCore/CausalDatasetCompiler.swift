import CryptoKit
import Foundation

public struct CausalDatasetCompilerConfiguration: Sendable {
    public let conversionVersion: String
    public let includeTimestampsInContext: Bool

    public init(
        conversionVersion: String = "phase1-causal-v9",
        includeTimestampsInContext: Bool = false
    ) {
        self.conversionVersion = conversionVersion
        self.includeTimestampsInContext = includeTimestampsInContext
    }
}

public struct CausalDatasetCompilerResult: Sendable, Equatable {
    public let sourceEventCount: Int
    public let convertedEventCount: Int
    public let exampleCount: Int
    public let targetExcludedEventCount: Int
    public let contextExcludedEventCount: Int
    public let rejectedEventCount: Int

    public init(
        sourceEventCount: Int,
        convertedEventCount: Int,
        exampleCount: Int,
        targetExcludedEventCount: Int,
        contextExcludedEventCount: Int,
        rejectedEventCount: Int
    ) {
        self.sourceEventCount = sourceEventCount
        self.convertedEventCount = convertedEventCount
        self.exampleCount = exampleCount
        self.targetExcludedEventCount = targetExcludedEventCount
        self.contextExcludedEventCount = contextExcludedEventCount
        self.rejectedEventCount = rejectedEventCount
    }
}

public enum CausalDatasetCompilerError: Error, CustomStringConvertible {
    case missingFile(String)
    case outputAlreadyExists(String)
    case invalidJSONObject(String, Int)
    case invalidManifest(String)
    case invalidEvent(String, Int, String)
    case couldNotCreate(String)

    public var description: String {
        switch self {
        case .missingFile(let path):
            return "required input file is missing: \(path)"
        case .outputAlreadyExists(let path):
            return "compiler output already exists: \(path); use a fresh output directory"
        case .invalidJSONObject(let path, let line):
            return "expected a JSON object at \(path):\(line)"
        case .invalidManifest(let reason):
            return "invalid session manifest: \(reason)"
        case .invalidEvent(let path, let line, let reason):
            return "invalid event at \(path):\(line): \(reason)"
        case .couldNotCreate(let path):
            return "could not create compiler output at \(path)"
        }
    }
}

/// Converts an operational Coupled session into deterministic Phase 1 causal
/// histories, pre-mutation queries, and human-written content targets. It deliberately
/// preserves the complete model input. Model-specific tokenization and left
/// truncation are a later packing step because a 32K suffix cannot be defined
/// without the selected tokenizer.
public struct CausalDatasetCompiler {
    public let configuration: CausalDatasetCompilerConfiguration

    public init(configuration: CausalDatasetCompilerConfiguration = .init()) {
        self.configuration = configuration
    }

    @discardableResult
    public func compile(inputDirectory: URL, outputDirectory: URL) throws
        -> CausalDatasetCompilerResult
    {
        let input = inputDirectory.standardizedFileURL
        let output = outputDirectory.standardizedFileURL
        let sessionURL = input.appendingPathComponent("session.json")
        let eventsURL = input.appendingPathComponent("events.jsonl")
        let rawURL = input.appendingPathComponent("raw.jsonl")
        for url in [sessionURL, eventsURL, rawURL] where !FileManager.default.fileExists(atPath: url.path) {
            throw CausalDatasetCompilerError.missingFile(url.path)
        }

        let outputFiles = [
            output.appendingPathComponent("dataset.json"),
            output.appendingPathComponent("events.jsonl"),
            output.appendingPathComponent("examples.jsonl"),
            output.appendingPathComponent("target-exclusions.jsonl"),
            output.appendingPathComponent("context-exclusions.jsonl"),
            output.appendingPathComponent("rejections.jsonl"),
        ]
        if FileManager.default.fileExists(atPath: output.path),
           !(try FileManager.default.contentsOfDirectory(atPath: output.path)).isEmpty {
            throw CausalDatasetCompilerError.outputAlreadyExists(
                "\(output.path) is not an empty directory"
            )
        }
        for url in outputFiles where FileManager.default.fileExists(atPath: url.path) {
            throw CausalDatasetCompilerError.outputAlreadyExists(url.path)
        }

        let manifestData = try Data(contentsOf: sessionURL)
        guard let manifest = try JSONSerialization.jsonObject(with: manifestData) as? [String: Any],
              let sessionID = manifest.string("sessionID"),
              !sessionID.isEmpty else {
            throw CausalDatasetCompilerError.invalidManifest("missing sessionID")
        }
        let timingVersion = ((manifest["schemas"] as? [String: Any])?["timingSemanticsVersion"] as? NSNumber)?.intValue
        guard timingVersion == 2 else {
            throw CausalDatasetCompilerError.invalidManifest(
                "timingSemanticsVersion must be 2, found \(timingVersion.map(String.init) ?? "missing")"
            )
        }

        let rawRecords = try readJSONL(rawURL)
        var attempts = [String: [String: Any]]()
        for record in rawRecords
            where record.object.string("recordType") == "active_tap_write_attempt" {
            guard let recordID = record.object.string("recordID"), !recordID.isEmpty else {
                throw CausalDatasetCompilerError.invalidEvent(
                    rawURL.path, record.line, "active tap attempt is missing recordID"
                )
            }
            guard attempts.updateValue(record.object, forKey: recordID) == nil else {
                throw CausalDatasetCompilerError.invalidEvent(
                    rawURL.path, record.line, "duplicate raw record ID \(recordID)"
                )
            }
        }
        let sourceEvents = try readJSONL(eventsURL)
        var converted = [ConvertedEvent]()
        var rejections = [[String: Any]]()
        var seenIDs = Set<String>()

        for source in sourceEvents {
            let event = source.object
            guard event.string("sessionID") == sessionID else {
                throw CausalDatasetCompilerError.invalidEvent(
                    eventsURL.path, source.line, "sessionID does not match session.json"
                )
            }
            guard let kind = event.string("kind"), kind == "read" || kind == "write" else {
                rejections.append(rejection(
                    source: source,
                    sourceEventID: sourceEventID(event: event, sessionID: sessionID, line: source.line),
                    reason: "phase1_ineligible_kind"
                ))
                continue
            }
            let eventID = sourceEventID(event: event, sessionID: sessionID, line: source.line)
            guard seenIDs.insert(eventID).inserted else {
                throw CausalDatasetCompilerError.invalidEvent(
                    eventsURL.path, source.line, "duplicate source event ID \(eventID)"
                )
            }
            if event.boolean("phase1Eligible") == false {
                rejections.append(rejection(
                    source: source,
                    sourceEventID: eventID,
                    reason: "explicitly_excluded_from_phase1"
                ))
                continue
            }

            if kind == "read" {
                guard let capturedAt = event.string("capturedAt"), parseTimestamp(capturedAt) != nil else {
                    rejections.append(rejection(
                        source: source, sourceEventID: eventID, reason: "missing_or_invalid_capturedAt"
                    ))
                    continue
                }
                converted.append(ConvertedEvent(
                    source: source,
                    object: event,
                    sourceEventID: eventID,
                    kind: kind,
                    availableAt: capturedAt,
                    beganAt: nil,
                    serialized: try serializeContextEvent(
                        event,
                        availableAt: capturedAt,
                        includeTimestamp: configuration.includeTimestampsInContext
                    )
                ))
                continue
            }

            guard let beganAt = event.string("beganAt"), let beganDate = parseTimestamp(beganAt),
                  let availableAt = event.string("terminalDecisionAt"),
                  let availableDate = parseTimestamp(availableAt) else {
                rejections.append(rejection(
                    source: source, sourceEventID: eventID,
                    reason: "missing_or_invalid_write_timestamps"
                ))
                continue
            }
            guard availableDate >= beganDate else {
                rejections.append(rejection(
                    source: source, sourceEventID: eventID,
                    reason: "write_available_before_began"
                ))
                continue
            }
            switch canonicalWrite(event: event, attempts: attempts) {
            case .success(let canonicalEvent):
                converted.append(ConvertedEvent(
                    source: source,
                    object: canonicalEvent,
                    sourceEventID: eventID,
                    kind: kind,
                    availableAt: availableAt,
                    beganAt: beganAt,
                    serialized: try serializeContextEvent(
                        canonicalEvent,
                        availableAt: availableAt,
                        includeTimestamp: configuration.includeTimestampsInContext
                    )
                ))
            case .failure(let reason):
                rejections.append(rejection(
                    source: source, sourceEventID: eventID, reason: reason
                ))
            }
        }

        var contextExclusions = [[String: Any]]()
        let verifiedWrites = converted.filter { $0.kind == "write" }
        converted.removeAll { candidate in
            guard candidate.kind == "read",
                  let supersedingWrite = verifiedWrites.first(where: {
                      staleReadCandidate(candidate, wasSupersededBy: $0)
                  }) else { return false }
            contextExclusions.append(contextExclusion(
                read: candidate,
                supersedingWrite: supersedingWrite
            ))
            return true
        }
        converted.sort(by: causalOrder)
        let targets = converted
            .filter { $0.kind == "write" }
            .sorted(by: targetOrder)
        var examples = [[String: Any]]()
        var targetExclusions = [[String: Any]]()
        for target in targets {
            let targetStart = parseTimestamp(target.beganAt!)!
            let contextEvents = converted.filter {
                parseTimestamp($0.availableAt)! < targetStart
            }
            let context = contextEvents.map(\.serialized).joined(separator: "\n")
            let contextIDs = contextEvents.map(\.sourceEventID)
            let contextSourceRecordIDs = contextEvents.flatMap {
                $0.object.stringArray("sourceRecordIDs")
            }
            let targetSourceRecordIDs = target.object.stringArray("sourceRecordIDs")
            let attempt = attempts[targetSourceRecordIDs[0]]!
            let conditioningState = try writeConditioningState(
                event: target.object,
                attempt: attempt
            )
            let query = try serializeQuery(
                conditioningState,
                includeTimestamp: configuration.includeTimestampsInContext
            )
            let modelInput = context.isEmpty ? query : context + "\n" + query
            let outcome = writeOutcome(target.object)
            let content = outcome.string("content") ?? ""
            let targetSegments = trainingTargetSegments(
                event: target.object,
                resolvedContent: content
            )
            let outcomeOffset = outcome.number("characterOffset")?.intValue
            let initialOffset = ((conditioningState["cursorContext"] as? [String: Any])?
                .number("selectionStartCharacters"))?.intValue
            let cursorFidelity = target.object["cursorFidelity"] as? [String: Any] ?? [:]
            let cursorFidelityStatus = cursorFidelity.string("status")
            let cursorContext = conditioningState["cursorContext"] as? [String: Any]
            let usesRangeSemanticContext = (
                conditioningState.number("schemaVersion")?.intValue ?? 0
            ) >= 2
            let hasCompleteSemanticContext = cursorContext?.string("leftContext") != nil
                && cursorContext?.string("selectedText") != nil
                && cursorContext?.string("rightContext") != nil
            let targetExclusionReason: String?
            if content.isEmpty {
                targetExclusionReason = "empty_content"
            } else if targetSegments == nil {
                targetExclusionReason = "unresolved_paste_authorship"
            } else if usesRangeSemanticContext, !hasCompleteSemanticContext {
                targetExclusionReason = "missing_semantic_cursor_context"
            } else if usesRangeSemanticContext {
                targetExclusionReason = nil
            } else if initialOffset == nil {
                targetExclusionReason = "missing_initial_cursor_context"
            } else if cursorFidelityStatus
                == CursorFidelityStatus.earliestObservedMutationUnavailable.rawValue {
                targetExclusionReason = "missing_earliest_observed_mutation"
            } else if cursorFidelityStatus
                == CursorFidelityStatus.initialCursorDiffersFromEarliestObservedMutation.rawValue {
                targetExclusionReason = "initial_cursor_differs_from_earliest_observed_mutation"
            } else if cursorFidelityStatus
                == CursorFidelityStatus.terminalEditMovedAfterAlignedStart.rawValue
                || outcomeOffset != initialOffset {
                targetExclusionReason = "net_edit_offset_differs_from_initial_cursor"
            } else if cursorFidelityStatus != CursorFidelityStatus.aligned.rawValue {
                targetExclusionReason = "missing_cursor_fidelity"
            } else {
                targetExclusionReason = nil
            }
            if let targetExclusionReason {
                targetExclusions.append(targetExclusion(
                    target: target,
                    reason: targetExclusionReason,
                    content: content,
                    initialOffset: initialOffset,
                    outcomeOffset: outcomeOffset,
                    conditioningState: conditioningState
                ))
                continue
            }
            examples.append([
                "schemaVersion": 8,
                "exampleID": "\(sessionID):\(target.sourceEventID)",
                "conversionVersion": configuration.conversionVersion,
                "sessionID": sessionID,
                "targetBeganAt": target.beganAt!,
                "context": context,
                "conditioningState": conditioningState,
                "query": query,
                "modelInput": modelInput,
                "cursorFidelity": cursorFidelity,
                "contextEventIDs": contextIDs,
                "contextSourceRecordIDs": contextSourceRecordIDs,
                "target": [
                    "schemaVersion": 1,
                    "segments": targetSegments!,
                    "resolvedContent": content,
                ],
                "targetMetadata": writeOutcomeMetadata(
                    event: target.object,
                    outcome: outcome
                ),
                "targetEventID": target.sourceEventID,
                "targetSourceRecordIDs": targetSourceRecordIDs,
                "sourceRecordIDs": contextSourceRecordIDs + targetSourceRecordIDs,
                "targetMask": [
                    "type": "authored_text_and_paste_actions_plus_eos",
                    "authoredTextReceivesLoss": true,
                    "pasteActionsReceiveLoss": true,
                    "pastedPayloadReceivesLoss": false,
                    "eosTokenCount": 1,
                    "eosReceivesLoss": true,
                ],
            ])
        }

        try FileManager.default.createDirectory(
            at: output,
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: NSNumber(value: 0o700)]
        )
        try writeJSONL(converted.map {
            convertedRecord(
                $0,
                sessionID: sessionID,
                conversionVersion: configuration.conversionVersion
            )
        }, to: outputFiles[1])
        rejections = rejections.map { rejection in
            var result = rejection
            result["sessionID"] = sessionID
            result["conversionVersion"] = configuration.conversionVersion
            return result
        }
        targetExclusions = targetExclusions.map { exclusion in
            var result = exclusion
            result["sessionID"] = sessionID
            result["conversionVersion"] = configuration.conversionVersion
            return result
        }
        contextExclusions = contextExclusions.map { exclusion in
            var result = exclusion
            result["sessionID"] = sessionID
            result["conversionVersion"] = configuration.conversionVersion
            return result
        }
        try writeJSONL(examples, to: outputFiles[2])
        try writeJSONL(targetExclusions, to: outputFiles[3])
        try writeJSONL(contextExclusions, to: outputFiles[4])
        try writeJSONL(rejections, to: outputFiles[5])

        let sourceDigests: [String: Any] = [
            "session.json": try sha256(of: sessionURL),
            "events.jsonl": try sha256(of: eventsURL),
            "raw.jsonl": try sha256(of: rawURL),
        ]
        let datasetManifest: [String: Any] = [
            "schemaVersion": 9,
            "conversionVersion": configuration.conversionVersion,
            "sessionID": sessionID,
            "source": [
                "sessionDirectory": input.path,
                "digestsSHA256": sourceDigests,
                "sourceEventCount": sourceEvents.count,
                "rawRecordCount": rawRecords.count,
            ],
            "counts": [
                "convertedEvents": converted.count,
                "examples": examples.count,
                "targetExclusions": targetExclusions.count,
                "contextExclusions": contextExclusions.count,
                "rejections": rejections.count,
            ],
            "timing": [
                "readAvailableAt": "capturedAt",
                "writeAvailableAt": "terminalDecisionAt",
                "targetBeganAt": "beganAt",
                "causalFilter": "event.availableAt < target.beganAt",
                "readSupersession": "exclude a same-process read whose last pointer activity precedes a later key and whose delayed screenshot lands inside that write interval",
                "tieBreak": "source JSONL line order",
            ],
            "eligibility": [
                "contextKinds": ["read", "verified_write"],
                "targetKinds": ["verified_write_with_nonempty_content_and_semantic_cursor_context", "legacy_verified_write_with_independently_aligned_numeric_cursor"],
                "targetExclusionRules": [
                    "content.isEmpty",
                    "new sessions require range-native left, selected, and right semantic strings",
                    "legacy sessions require an initial numeric cursor",
                    "legacy sessions require an earliest observed mutation",
                    "legacy earliest mutation and terminal edit must agree with the initial cursor",
                ],
                "explicitExclusion": "phase1Eligible == false",
                "writeVerification": "source outcome must reconstruct the used observation, except the identified legacy synthetic-deletion fallback",
                "writeProjection": "canonical minimum contiguous diff of raw logical BEFORE and used observation",
            ],
            "serialization": [
                "contextVersion": 1,
                "queryVersion": 3,
                "targetVersion": 8,
                "targetFormat": "structured_authorship_segments",
                "timestampsInContext": configuration.includeTimestampsInContext,
                "eventDelimiter": "newline",
                "jsonKeys": "sorted",
            ],
            "objective": [
                "modelInput": "causal_history_plus_pre_mutation_conditioning_state",
                "target": "authored_text_plus_grounded_paste_actions",
                "knownDestinationAndInitialSelectionReceiveLoss": false,
                "operationReceivesLoss": false,
                "removedContentReceivesLoss": false,
                "outcomeCharacterOffsetReceivesLoss": false,
            ],
            "contextPacking": [
                "state": "unpacked_complete_model_input",
                "requiredNextStep": "tokenize modelInput with the selected model tokenizer and retain its most recent L tokens",
                "initialPhase1TokenBudget": 32_768,
                "rightEdge": "write conditioning query",
                "reason": "token suffixes are model-tokenizer dependent; query remains while older history truncates first",
            ],
            "loader": [
                "targetSource": "example.target.segments",
                "targetTokenization": "tokenize authored_text normally; map every paste segment to one atomic paste_token_id; automatic special tokens disabled",
                "targetTermination": "append exactly one selected-tokenizer eos_token_id",
                "eosTokenCount": 1,
                "authoredTextTokensReceiveLoss": true,
                "pasteActionTokensReceiveLoss": true,
                "pastedPayloadTokensReceiveLoss": false,
                "eosTokenReceivesLoss": true,
            ],
            "targetMask": [
                "type": "authored_text_and_paste_actions_plus_eos",
                "authoredTextReceivesLoss": true,
                "pasteActionsReceiveLoss": true,
                "pastedPayloadReceivesLoss": false,
                "eosTokenCount": 1,
                "eosReceivesLoss": true,
            ],
        ]
        try writeJSONObject(datasetManifest, to: outputFiles[0], pretty: true)

        return CausalDatasetCompilerResult(
            sourceEventCount: sourceEvents.count,
            convertedEventCount: converted.count,
            exampleCount: examples.count,
            targetExcludedEventCount: targetExclusions.count,
            contextExcludedEventCount: contextExclusions.count,
            rejectedEventCount: rejections.count
        )
    }
}

private struct SourceRecord {
    let line: Int
    let object: [String: Any]
}

private struct ConvertedEvent {
    let source: SourceRecord
    let object: [String: Any]
    let sourceEventID: String
    let kind: String
    let availableAt: String
    let beganAt: String?
    let serialized: String
}

private func staleReadCandidate(
    _ read: ConvertedEvent,
    wasSupersededBy write: ConvertedEvent
) -> Bool {
    guard read.kind == "read", write.kind == "write",
          let readPID = read.object.number("processIdentifier")?.intValue,
          let writePID = write.object.number("processIdentifier")?.intValue,
          readPID == writePID,
          let lastActivityAt = read.object.string("lastActivityAt"),
          let capturedAt = read.object.string("capturedAt"),
          let writeBeganAt = write.object.string("beganAt"),
          let lastInputAt = write.object.string("lastInputAt"),
          let terminalDecisionAt = write.object.string("terminalDecisionAt"),
          let lastActivity = parseTimestamp(lastActivityAt),
          let captured = parseTimestamp(capturedAt),
          let writeBegan = parseTimestamp(writeBeganAt),
          let lastInput = parseTimestamp(lastInputAt),
          let terminalDecision = parseTimestamp(terminalDecisionAt) else { return false }
    return lastActivity < lastInput
        && captured >= writeBegan
        && captured <= terminalDecision
}

private func contextExclusion(
    read: ConvertedEvent,
    supersedingWrite: ConvertedEvent
) -> [String: Any] {
    compactJSONObject([
        "schemaVersion": 1,
        "reason": "read_candidate_superseded_by_write",
        "sourceEventID": read.sourceEventID,
        "sourceLine": read.source.line,
        "capturedAt": read.object.string("capturedAt"),
        "lastActivityAt": read.object.string("lastActivityAt"),
        "processIdentifier": read.object.number("processIdentifier")?.intValue,
        "supersedingWriteEventID": supersedingWrite.sourceEventID,
        "supersedingWriteBeganAt": supersedingWrite.object.string("beganAt"),
        "supersedingWriteLastInputAt": supersedingWrite.object.string("lastInputAt"),
        "supersedingWriteAvailableAt": supersedingWrite.object.string("terminalDecisionAt"),
        "sourceRecordIDs": read.object.stringArray("sourceRecordIDs"),
    ])
}

private func readJSONL(_ url: URL) throws -> [SourceRecord] {
    let contents = try String(contentsOf: url, encoding: .utf8)
    return try contents.split(separator: "\n", omittingEmptySubsequences: true)
        .enumerated()
        .map { index, line in
            let data = Data(line.utf8)
            guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                throw CausalDatasetCompilerError.invalidJSONObject(url.path, index + 1)
            }
            return SourceRecord(line: index + 1, object: object)
        }
}

private func sourceEventID(event: [String: Any], sessionID: String, line: Int) -> String {
    if let eventID = event.string("eventID"), !eventID.isEmpty { return eventID }
    if let sequence = event.number("sequence")?.intValue {
        return "\(sessionID):\(event.string("kind") ?? "event"):\(sequence)"
    }
    return "\(sessionID):line:\(line)"
}

private enum CanonicalWriteResult {
    case success([String: Any])
    case failure(String)
}

private func canonicalWrite(
    event: [String: Any],
    attempts: [String: [String: Any]]
) -> CanonicalWriteResult {
    let sourceIDs = event.stringArray("sourceRecordIDs")
    guard sourceIDs.count == 1, let attempt = attempts[sourceIDs[0]] else {
        return .failure("missing_unique_raw_write_attempt")
    }
    guard attempt.string("resolution") == "validated" else {
        return .failure("raw_write_attempt_not_validated")
    }
    guard attempt.string("proposedEventID") == event.string("eventID") else {
        return .failure("raw_derived_event_id_mismatch")
    }
    guard let before = attempt["before"] as? [String: Any],
          let rawBefore = before.string("value"),
          before.boolean("valueWasTruncated") != true else {
        return .failure("raw_before_missing_or_truncated")
    }
    let beforeValue = logicalEditableValue(
        rawBefore,
        placeholderValue: before.string("placeholderValue")
    )
    if attempt.string("fallbackReason") == "terminal_matches_before",
       attempt.string("derivationObservationSource") == "post_input_checkpoint",
       let terminal = attempt["after"] as? [String: Any],
       let terminalRawValue = terminal.string("value"),
       logicalEditableValue(
           terminalRawValue,
           placeholderValue: terminal.string("placeholderValue")
       ) == beforeValue {
        return .failure("checkpoint_edit_reverted_before_settlement")
    }
    guard let used = usedObservation(attempt) else {
        return .failure("used_observation_missing")
    }
    guard let rawAfter = used.string("value"),
          used.boolean("valueWasTruncated") != true else {
        return .failure("used_observation_missing_or_truncated")
    }
    let afterValue = logicalEditableValue(
        rawAfter,
        placeholderValue: used.string("placeholderValue")
    )
    guard let operation = event.string("operation"),
          let inserted = event.string("content"),
          let removed = event.string("removedContent"),
          let offset = event.number("characterOffset")?.intValue else {
        return .failure("derived_edit_fields_missing")
    }
    let reconstructed = applyEdit(
        operation: operation,
        offset: offset,
        removed: removed,
        inserted: inserted,
        to: beforeValue
    )
    let sourceReconstructsObservation = reconstructed == afterValue
    let knownSyntheticDeletion = event.string("fallbackReason")
        == "removal_only_terminal_unpopulated"
    guard sourceReconstructsObservation || knownSyntheticDeletion else {
        return .failure(
            reconstructed == nil
                ? "derived_edit_does_not_apply_to_before"
                : "derived_edit_does_not_reconstruct_used_observation"
        )
    }

    let canonicalEdit = minimalTextEdit(from: beforeValue, to: afterValue)
    guard !canonicalEdit.isEmpty else { return .failure("canonical_raw_edit_is_empty") }
    var canonicalEvent = event
    canonicalEvent["operation"] = canonicalEdit.operation.rawValue
    canonicalEvent["content"] = canonicalEdit.inserted
    canonicalEvent["removedContent"] = canonicalEdit.removed
    canonicalEvent["characterOffset"] = canonicalEdit.characterOffset
    canonicalEvent["outcome"] = [
        "operation": canonicalEdit.operation.rawValue,
        "content": canonicalEdit.inserted,
        "removedContent": canonicalEdit.removed,
        "characterOffset": canonicalEdit.characterOffset,
    ]
    canonicalEvent["sourceOutcomeMatchesCanonical"] = operation == canonicalEdit.operation.rawValue
        && inserted == canonicalEdit.inserted
        && removed == canonicalEdit.removed
        && offset == canonicalEdit.characterOffset
    canonicalEvent["sourceOutcomeReconstructedUsedObservation"] = sourceReconstructsObservation
    let cursorFidelity = cursorFidelityEvidence(
        attempt: attempt,
        terminalEditOffset: canonicalEdit.characterOffset
    )
    if let sourceCursorFidelity = event["cursorFidelity"] as? [String: Any],
       (try? canonicalJSONString(sourceCursorFidelity))
        != (try? canonicalJSONString(cursorFidelity)) {
        return .failure("derived_cursor_fidelity_does_not_match_raw_evidence")
    }
    canonicalEvent["cursorFidelity"] = cursorFidelity
    return .success(canonicalEvent)
}

private func cursorFidelityEvidence(
    attempt: [String: Any],
    terminalEditOffset: Int
) -> [String: Any] {
    guard let before = attempt["before"] as? [String: Any],
          let rawBefore = before.string("value") else {
        return cursorFidelityJSONObject(
            status: .initialCursorUnavailable,
            initialCursorOffset: nil,
            initialSelectionLength: nil,
            earliest: nil,
            terminalEditOffset: terminalEditOffset
        )
    }
    let beforeValue = logicalEditableValue(
        rawBefore,
        placeholderValue: before.string("placeholderValue")
    )
    let cursor = semanticCursorContext(
        in: beforeValue,
        selectionStartUTF16: before.number("selectedRangeLocation")?.intValue,
        selectionLengthUTF16: before.number("selectedRangeLength")?.intValue,
        surroundingCharacterCount: 1
    )
    var candidates = [CompilerObservedMutation]()
    for key in ["mutationCheckpoints", "pasteCheckpoints", "returnCheckpoints"] {
        for checkpoint in attempt[key] as? [[String: Any]] ?? [] {
            guard checkpoint.stringArray("axErrors").isEmpty,
                  let observation = checkpoint["observation"] as? [String: Any],
                  observation.boolean("valueWasTruncated") != true,
                  let rawValue = observation.string("value"),
                  let observationID = observation.string("observationID"),
                  let capturedAt = observation.string("observedAt") else { continue }
            let value = logicalEditableValue(
                rawValue,
                placeholderValue: observation.string("placeholderValue")
            )
            let edit = minimalTextEdit(from: beforeValue, to: value)
            guard !edit.isEmpty else { continue }
            candidates.append(CompilerObservedMutation(
                observationID: observationID,
                capturedAt: capturedAt,
                editOffset: edit.characterOffset
            ))
        }
    }
    let earliest = candidates.min {
        if $0.capturedAt != $1.capturedAt { return $0.capturedAt < $1.capturedAt }
        return $0.observationID < $1.observationID
    }
    let status = cursorFidelityStatus(
        initialCursorOffset: cursor?.selectionStartCharacters,
        earliestObservedMutationOffset: earliest?.editOffset,
        terminalEditOffset: terminalEditOffset
    )
    return cursorFidelityJSONObject(
        status: status,
        initialCursorOffset: cursor?.selectionStartCharacters,
        initialSelectionLength: cursor?.selectionLengthCharacters,
        earliest: earliest,
        terminalEditOffset: terminalEditOffset
    )
}

private struct CompilerObservedMutation {
    let observationID: String
    let capturedAt: String
    let editOffset: Int
}

private func cursorFidelityJSONObject(
    status: CursorFidelityStatus,
    initialCursorOffset: Int?,
    initialSelectionLength: Int?,
    earliest: CompilerObservedMutation?,
    terminalEditOffset: Int
) -> [String: Any] {
    compactJSONObject([
        "schemaVersion": 1,
        "status": status.rawValue,
        "initialCursorOffsetCharacters": initialCursorOffset,
        "initialSelectionLengthCharacters": initialSelectionLength,
        "earliestObservedMutationOffsetCharacters": earliest?.editOffset,
        "earliestObservedMutationObservationID": earliest?.observationID,
        "earliestObservedMutationCapturedAt": earliest?.capturedAt,
        "terminalEditOffsetCharacters": terminalEditOffset,
    ])
}

private func usedObservation(_ attempt: [String: Any]) -> [String: Any]? {
    switch attempt.string("derivationObservationSource") {
    case "terminal_after":
        return attempt["after"] as? [String: Any]
    case "pre_return_checkpoint":
        guard let checkpointID = attempt.string("usedCheckpointID"),
              let checkpoints = attempt["returnCheckpoints"] as? [[String: Any]] else { return nil }
        return checkpoints.first { $0.string("checkpointID") == checkpointID }?["observation"] as? [String: Any]
    case "post_paste_checkpoint":
        guard let checkpointID = attempt.string("usedCheckpointID"),
              let checkpoints = attempt["pasteCheckpoints"] as? [[String: Any]] else { return nil }
        return checkpoints.first { $0.string("checkpointID") == checkpointID }?["observation"] as? [String: Any]
    case "post_input_checkpoint":
        guard let checkpointID = attempt.string("usedCheckpointID"),
              let checkpoints = attempt["mutationCheckpoints"] as? [[String: Any]] else { return nil }
        return checkpoints.first { $0.string("checkpointID") == checkpointID }?["observation"] as? [String: Any]
    default:
        return nil
    }
}

private func applyEdit(
    operation: String,
    offset: Int,
    removed: String,
    inserted: String,
    to value: String
) -> String? {
    guard ["insert", "delete", "replace"].contains(operation), offset >= 0 else { return nil }
    let characters = Array(value)
    let removedCharacters = Array(removed)
    guard offset <= characters.count,
          offset + removedCharacters.count <= characters.count,
          Array(characters[offset..<(offset + removedCharacters.count)]) == removedCharacters else {
        return nil
    }
    return String(characters[..<offset]) + inserted
        + String(characters[(offset + removedCharacters.count)...])
}

private func serializeContextEvent(
    _ event: [String: Any],
    availableAt: String,
    includeTimestamp: Bool
) throws -> String {
    let kind = event.string("kind")!
    var serialized: [String: Any] = [
        "schemaVersion": 1,
        "kind": kind,
        "appName": event.string("appName") ?? "",
        "bundleIdentifier": event.string("bundleIdentifier") ?? "",
        "windowTitle": event.string("windowTitle") ?? "",
        "content": event.string("content") ?? "",
        "provenance": event.string("provenance") ?? "",
    ]
    if includeTimestamp { serialized["availableAt"] = availableAt }
    if kind == "write" {
        serialized["operation"] = event.string("operation") ?? ""
        serialized["removedContent"] = event.string("removedContent") ?? ""
        serialized["characterOffset"] = event.number("characterOffset")?.intValue ?? 0
        serialized["boundaryReason"] = event.string("boundaryReason") ?? ""
        if let segments = event["authorshipSegments"] as? [[String: Any]] {
            // Historical writes retain the resolved document text and the
            // provenance of pasted spans. Unlike the current target, paste
            // payloads remain visible here because they were causally available.
            serialized["authorshipSegments"] = segments
            serialized["authorshipResolution"] = event.string("authorshipResolution")
                ?? "unresolved"
        }
    }
    return try canonicalJSONString(serialized)
}

private func writeConditioningState(
    event: [String: Any],
    attempt: [String: Any]
) throws -> [String: Any] {
    let eventState = event["conditioningState"] as? [String: Any]
    let rawState = attempt["conditioningState"] as? [String: Any]
    if let rawState,
       rawState.number("schemaVersion")?.intValue ?? 0 >= 3 {
        guard let eventState,
              try canonicalJSONString(eventState) == canonicalJSONString(rawState) else {
            throw CausalDatasetCompilerError.invalidManifest(
                "derived write conditioning state does not match raw clipboard evidence"
            )
        }
        return rawState
    }
    if let before = attempt["before"] as? [String: Any],
       before["axRangeCursorProbe"] as? [String: Any] != nil {
        guard let rangeState = rangeSemanticConditioningState(
            event: event,
            attempt: attempt,
            before: before
        ) else {
            throw CausalDatasetCompilerError.invalidManifest(
                "verified write is missing its pre-mutation range observation"
            )
        }
        for sourceState in [rawState, eventState].compactMap({ $0 })
        where sourceState.number("schemaVersion")?.intValue ?? 0 >= 2 {
            if try canonicalJSONString(sourceState) != canonicalJSONString(rangeState) {
                throw CausalDatasetCompilerError.invalidManifest(
                    "range-native conditioning state does not match raw evidence"
                )
            }
        }
        return rangeState
    }
    if let rawState {
        if let eventState,
           try canonicalJSONString(eventState) != canonicalJSONString(rawState) {
            throw CausalDatasetCompilerError.invalidManifest(
                "derived write conditioning state does not match raw evidence"
            )
        }
        return rawState
    }

    guard let before = attempt["before"] as? [String: Any],
          let rawValue = before.string("value"),
          let capturedAt = before.string("observedAt") else {
        throw CausalDatasetCompilerError.invalidManifest(
            "verified write is missing its pre-mutation conditioning observation"
        )
    }
    let targetIdentity = attempt["targetIdentity"] as? [String: Any] ?? [:]
    let logicalValue = logicalEditableValue(
        rawValue,
        placeholderValue: before.string("placeholderValue")
    )
    var state: [String: Any] = [
        "schemaVersion": 1,
        "captureSemantics": "synchronous_before_application_mutation",
        "inputInterceptedAt": event.string("beganAt") ?? "",
        "capturedAt": capturedAt,
        "destination": compactJSONObject([
            "appName": event.string("appName") ?? "",
            "bundleIdentifier": event.string("bundleIdentifier"),
            "processIdentifier": event.number("processIdentifier")?.intValue,
            "windowTitle": event.string("windowTitle"),
            "resource": nil,
            "role": targetIdentity.string("role") ?? "",
            "subrole": targetIdentity.string("subrole"),
            "fieldIdentifier": targetIdentity.string("accessibilityIdentifier"),
            "fieldLabel": targetIdentity.string("fieldLabel"),
            "fieldDescription": targetIdentity.string("fieldDescription"),
            "placeholder": before.string("placeholderValue"),
        ]),
        "sourceObservationID": before.string("observationID") ?? "",
    ]
    if let cursor = semanticCursorContext(
        in: logicalValue,
        selectionStartUTF16: before.number("selectedRangeLocation")?.intValue,
        selectionLengthUTF16: before.number("selectedRangeLength")?.intValue
    ) {
        state["cursorContext"] = cursorJSONObject(cursor)
    }
    if let eventState,
       try canonicalJSONString(eventState) != canonicalJSONString(state) {
        throw CausalDatasetCompilerError.invalidManifest(
            "derived write conditioning state does not reconstruct from raw evidence"
        )
    }
    return state
}

private func trainingTargetSegments(
    event: [String: Any],
    resolvedContent: String
) -> [[String: Any]]? {
    guard let segments = event["authorshipSegments"] as? [[String: Any]] else {
        // Backward-compatible conversion of sessions collected before explicit
        // Cmd-V provenance. New records always carry authorshipResolution.
        return [["type": "authored_text", "content": resolvedContent]]
    }
    guard event.string("authorshipResolution") == "resolved" else { return nil }
    var target = [[String: Any]]()
    var reconstructed = ""
    for segment in segments {
        guard let type = segment.string("type"),
              let content = segment.string("content") else { return nil }
        reconstructed += content
        switch type {
        case "authored_text":
            target.append(["type": type, "content": content])
        case "paste":
            guard let snapshotID = segment.string("clipboardSnapshotID"),
                  let checkpointID = segment.string("pasteCheckpointID") else { return nil }
            // Deliberately omit the payload from the supervised target. The
            // loader maps this object to one atomic paste action token.
            target.append([
                "type": type,
                "clipboardSnapshotID": snapshotID,
                "pasteCheckpointID": checkpointID,
            ])
        default:
            return nil
        }
    }
    guard reconstructed == resolvedContent else { return nil }
    return target
}

private func rangeSemanticConditioningState(
    event: [String: Any],
    attempt: [String: Any],
    before: [String: Any]
) -> [String: Any]? {
    guard let capturedAt = before.string("observedAt"),
          let rawValue = before.string("value"),
          let probe = before["axRangeCursorProbe"] as? [String: Any] else { return nil }
    let targetIdentity = attempt["targetIdentity"] as? [String: Any] ?? [:]
    let bundleIdentifier = event.string("bundleIdentifier")
        ?? attempt.string("bundleIdentifier")
    let fieldDescription = targetIdentity.string("fieldDescription")
    let surfacePrompt = unpopulatedSurfacePrompt(
        bundleIdentifier: bundleIdentifier,
        fieldDescription: fieldDescription,
        value: rawValue,
        placeholderValue: before.string("placeholderValue"),
        valueRepresentedPlaceholder: before.boolean("valueRepresentedPlaceholder") == true
    )
    var state: [String: Any] = [
        "schemaVersion": 2,
        "captureSemantics": "synchronous_before_application_mutation",
        "inputInterceptedAt": event.string("beganAt") ?? "",
        "capturedAt": capturedAt,
        "destination": compactJSONObject([
            "appName": event.string("appName") ?? "",
            "bundleIdentifier": bundleIdentifier,
            "processIdentifier": event.number("processIdentifier")?.intValue,
            "windowTitle": event.string("windowTitle"),
            "resource": nil,
            "role": targetIdentity.string("role") ?? "",
            "subrole": targetIdentity.string("subrole"),
            "fieldIdentifier": targetIdentity.string("accessibilityIdentifier"),
            "fieldLabel": targetIdentity.string("fieldLabel"),
            "fieldDescription": fieldDescription,
            "placeholder": before.string("placeholderValue"),
        ]),
        "sourceObservationID": before.string("observationID") ?? "",
    ]
    if let cursor = rangeSemanticCursorJSONObject(
        probe: probe,
        surfacePrompt: surfacePrompt
    ) {
        state["cursorContext"] = cursor
    }
    return state
}

private func rangeSemanticCursorJSONObject(
    probe: [String: Any],
    surfacePrompt: String?
) -> [String: Any]? {
    guard let leftQuery = probe["left"] as? [String: Any],
          let selectedQuery = probe["selected"] as? [String: Any],
          let rightQuery = probe["right"] as? [String: Any],
          let left = leftQuery.string("text"),
          let selected = selectedQuery.string("text") else { return nil }
    let right: String
    let captureStatus: String
    if let capturedRight = rightQuery.string("text") {
        right = capturedRight
        captureStatus = "complete"
    } else if rightQuery.string("axError") == "no_value",
              rightQuery.number("rangeLength")?.intValue == 1 {
        right = ""
        captureStatus = "right_provider_no_value_after_minimum_probe"
    } else {
        return nil
    }
    return compactJSONObject([
        "schemaVersion": 2,
        "source": "accessibility_string_for_range",
        "captureStatus": captureStatus,
        "fieldState": surfacePrompt == nil ? "editable_text" : "unpopulated_prompt",
        "leftContext": surfacePrompt == nil ? left : "",
        "selectedText": surfacePrompt == nil ? selected : "",
        "rightContext": surfacePrompt == nil ? right : "",
        "surfacePrompt": surfacePrompt,
    ])
}

private func serializeQuery(
    _ conditioningState: [String: Any],
    includeTimestamp: Bool
) throws -> String {
    var destination = conditioningState["destination"] as? [String: Any] ?? [:]
    destination.removeValue(forKey: "processIdentifier")
    let cursor = conditioningState["cursorContext"] as? [String: Any]
    let modelCursor: Any
    if cursor?.string("source") == "accessibility_string_for_range" {
        modelCursor = compactJSONObject([
            "schemaVersion": 2,
            "fieldState": cursor?.string("fieldState"),
            "leftContext": cursor?.string("leftContext"),
            "selectedText": cursor?.string("selectedText"),
            "rightContext": cursor?.string("rightContext"),
            "surfacePrompt": cursor?.string("surfacePrompt"),
        ])
    } else {
        modelCursor = cursor.map { $0 as Any } ?? NSNull()
    }
    var query: [String: Any] = [
        "schemaVersion": conditioningState["clipboard"] == nil ? 2 : 3,
        "kind": "write_conditioning_state",
        "destination": destination,
        "cursorContext": modelCursor,
    ]
    if let clipboard = conditioningState["clipboard"] as? [String: Any] {
        query["clipboard"] = compactJSONObject([
            "changeCount": clipboard.number("changeCount")?.intValue,
            "content": clipboard.string("text"),
            "contentWasTruncated": clipboard.boolean("textWasTruncated"),
        ])
    }
    if includeTimestamp {
        query["capturedAt"] = conditioningState.string("capturedAt") ?? ""
    }
    return try canonicalJSONString(query)
}

private func writeOutcome(_ event: [String: Any]) -> [String: Any] {
    event["outcome"] as? [String: Any] ?? event
}

private func writeOutcomeMetadata(
    event: [String: Any],
    outcome: [String: Any]
) -> [String: Any] {
    compactJSONObject([
        "operation": outcome.string("operation") ?? "",
        "characterOffset": outcome.number("characterOffset")?.intValue,
        "removedContent": outcome.string("removedContent") ?? "",
        "provenance": event.string("provenance"),
        "boundaryReason": event.string("boundaryReason"),
        "derivationObservationSource": event.string("derivationObservationSource"),
        "fallbackReason": event.string("fallbackReason"),
        "sourceOutcomeMatchesCanonical": event.boolean("sourceOutcomeMatchesCanonical"),
    ])
}

private func targetExclusion(
    target: ConvertedEvent,
    reason: String,
    content: String,
    initialOffset: Int?,
    outcomeOffset: Int?,
    conditioningState: [String: Any]
) -> [String: Any] {
    compactJSONObject([
        "schemaVersion": 1,
        "sourceLine": target.source.line,
        "sourceEventID": target.sourceEventID,
        "kind": target.kind,
        "reason": reason,
        "contentCharacterCount": content.count,
        "initialCursorOffset": initialOffset,
        "outcomeCharacterOffset": outcomeOffset,
        "conditioningState": conditioningState,
        "sourceRecordIDs": target.object.stringArray("sourceRecordIDs"),
        "cursorFidelity": target.object["cursorFidelity"],
    ])
}

private func cursorJSONObject(_ cursor: SemanticCursorContext) -> [String: Any] {
    [
        "leftContext": cursor.leftContext,
        "selectedText": cursor.selectedText,
        "rightContext": cursor.rightContext,
        "selectionStartCharacters": cursor.selectionStartCharacters,
        "selectionLengthCharacters": cursor.selectionLengthCharacters,
        "selectionStartUTF16": cursor.selectionStartUTF16,
        "selectionLengthUTF16": cursor.selectionLengthUTF16,
        "fieldCharacterCount": cursor.fieldCharacterCount,
        "leftContextWasTruncated": cursor.leftContextWasTruncated,
        "selectedTextWasTruncated": cursor.selectedTextWasTruncated,
        "rightContextWasTruncated": cursor.rightContextWasTruncated,
    ]
}

private func compactJSONObject(_ values: [String: Any?]) -> [String: Any] {
    values.reduce(into: [String: Any]()) { result, entry in
        if let value = entry.value {
            result[entry.key] = value
        }
    }
}

private func causalOrder(_ lhs: ConvertedEvent, _ rhs: ConvertedEvent) -> Bool {
    let left = parseTimestamp(lhs.availableAt)!
    let right = parseTimestamp(rhs.availableAt)!
    if left != right { return left < right }
    return lhs.source.line < rhs.source.line
}

private func targetOrder(_ lhs: ConvertedEvent, _ rhs: ConvertedEvent) -> Bool {
    let left = parseTimestamp(lhs.beganAt!)!
    let right = parseTimestamp(rhs.beganAt!)!
    if left != right { return left < right }
    return lhs.source.line < rhs.source.line
}

private func parseTimestamp(_ value: String) -> Date? {
    ISO8601DateFormatter.causal.date(from: value)
}

private extension ISO8601DateFormatter {
    static let causal: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()
}

private func rejection(source: SourceRecord, sourceEventID: String, reason: String) -> [String: Any] {
    [
        "schemaVersion": 1,
        "sourceLine": source.line,
        "sourceEventID": sourceEventID,
        "kind": source.object.string("kind") ?? "unknown",
        "reason": reason,
        "sourceRecordIDs": source.object.stringArray("sourceRecordIDs"),
    ]
}

private func convertedRecord(
    _ event: ConvertedEvent,
    sessionID: String,
    conversionVersion: String
) -> [String: Any] {
    var record: [String: Any] = [
        "schemaVersion": 1,
        "conversionVersion": conversionVersion,
        "sessionID": sessionID,
        "sourceEventID": event.sourceEventID,
        "sourceLine": event.source.line,
        "kind": event.kind,
        "availableAt": event.availableAt,
        "serialized": event.serialized,
        "sourceRecordIDs": event.object.stringArray("sourceRecordIDs"),
    ]
    if let beganAt = event.beganAt { record["beganAt"] = beganAt }
    if event.kind == "write" {
        record["sourceOutcomeMatchesCanonical"] = event.object.boolean(
            "sourceOutcomeMatchesCanonical"
        ) ?? false
        record["sourceOutcomeReconstructedUsedObservation"] = event.object.boolean(
            "sourceOutcomeReconstructedUsedObservation"
        ) ?? false
        record["cursorFidelity"] = event.object["cursorFidelity"] ?? NSNull()
    }
    return record
}

private func canonicalJSONString(_ object: [String: Any]) throws -> String {
    let data = try JSONSerialization.data(
        withJSONObject: object,
        options: [.sortedKeys, .withoutEscapingSlashes]
    )
    return String(decoding: data, as: UTF8.self)
}

private func writeJSONL(_ objects: [[String: Any]], to url: URL) throws {
    var data = Data()
    for object in objects {
        data.append(try JSONSerialization.data(
            withJSONObject: object,
            options: [.sortedKeys, .withoutEscapingSlashes]
        ))
        data.append(0x0A)
    }
    guard FileManager.default.createFile(
        atPath: url.path,
        contents: data,
        attributes: [.posixPermissions: NSNumber(value: 0o600)]
    ) else {
        throw CausalDatasetCompilerError.couldNotCreate(url.path)
    }
}

private func writeJSONObject(_ object: [String: Any], to url: URL, pretty: Bool) throws {
    var options: JSONSerialization.WritingOptions = [.sortedKeys, .withoutEscapingSlashes]
    if pretty { options.insert(.prettyPrinted) }
    var data = try JSONSerialization.data(withJSONObject: object, options: options)
    data.append(0x0A)
    guard FileManager.default.createFile(
        atPath: url.path,
        contents: data,
        attributes: [.posixPermissions: NSNumber(value: 0o600)]
    ) else {
        throw CausalDatasetCompilerError.couldNotCreate(url.path)
    }
}

private func sha256(of url: URL) throws -> String {
    let data = try Data(contentsOf: url)
    return SHA256.hash(data: data)
        .map { String(format: "%02x", $0) }
        .joined()
}

private extension Dictionary where Key == String, Value == Any {
    func string(_ key: String) -> String? { self[key] as? String }
    func number(_ key: String) -> NSNumber? { self[key] as? NSNumber }
    func boolean(_ key: String) -> Bool? { self[key] as? Bool }
    func stringArray(_ key: String) -> [String] { self[key] as? [String] ?? [] }
}
