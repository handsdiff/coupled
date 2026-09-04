import CryptoKit
import Foundation

public struct Phase1SemanticReducerConfiguration: Sendable {
    public let reducerVersion: String
    public let readSurfaceEvidenceDirectory: URL?

    public init(
        reducerVersion: String,
        readSurfaceEvidenceDirectory: URL? = nil
    ) {
        self.reducerVersion = reducerVersion
        self.readSurfaceEvidenceDirectory = readSurfaceEvidenceDirectory
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
        case .invalidManifest(let reason): return "invalid reducer input manifest: \(reason)"
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

    public init(configuration: Phase1SemanticReducerConfiguration) {
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
        let rawScreenOCRSchema = (manifest["schemas"] as? [String: Any])
            .flatMap { intValue($0["rawScreenOCR"]) } ?? 0
        let raw = try reducerReadJSONL(rawURL)
        let readSurfaceEvidence = try loadReadSurfaceEvidence(
            configuration: configuration,
            sessionURL: sessionURL,
            rawURL: rawURL,
            sessionID: sessionID,
            rawScreenOCRSchema: rawScreenOCRSchema,
            raw: raw
        )
        let fastStartChains = reducerFastStartChains(raw)
        let navigationChains = reducerNavigationChains(raw)
        let writeOverlapBoundaries = raw.compactMap { record -> ReducerWriteBoundary? in
            guard stringValue(record.object["recordType"]) == "active_tap_write_attempt",
                  (intValue(record.object["inputEventCount"]) ?? 0) > 0,
                  let beganAt = stringValue(record.object["beganAt"]) else { return nil }
            return ReducerWriteBoundary(rawLine: record.line, beganAt: beganAt)
        }
        let usesReconciledSemanticReads = [
            "phase1-semantic-v18", "phase1-semantic-v19",
            "phase1-semantic-v20",
        ].contains(configuration.reducerVersion)
        let dynamicReadBoundaries = usesReconciledSemanticReads
            ? reducerDynamicReadBoundaries(raw)
            : []
        var seenRawIDs = Set<String>()
        for record in raw {
            guard let id = record.object["recordID"] as? String else { continue }
            guard seenRawIDs.insert(id).inserted else {
                throw Phase1SemanticReducerError.duplicateRawRecordID(id)
            }
        }
        let rawByID = Dictionary(uniqueKeysWithValues: raw.compactMap { record in
            stringValue(record.object["recordID"]).map { ($0, record) }
        })
        let referencedVisualFrameIDs = Set(raw.compactMap {
            stringValue($0.object["recordType"]) == "visual_ocr_observation"
                ? nonEmptyString($0.object["sourceFrameRecordID"])
                : nil
        })
        let promptClosures = validatedPromptClosures(
            raw,
            rawByID: rawByID,
            sessionID: sessionID
        )

        var candidates = [ReducerCandidate]()
        var dispositions = promptClosures.dispositions
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
            case "visual_frame_observation":
                guard reducerUsesVisualReadEvidence(configuration.reducerVersion) else {
                    continue
                }
                let recordID = stringValue(object["recordID"]) ?? ""
                if let suppression = nonEmptyString(object["derivedSuppressionReason"]) {
                    dispositions.append(ReducerDisposition(
                        line: record.line,
                        object: reducerUnresolved(
                            sessionID: sessionID, raw: object, line: record.line,
                            kind: "read", rule: "visual_frame_selection_v1",
                            reason: suppression
                        )
                    ))
                } else if !referencedVisualFrameIDs.contains(recordID) {
                    dispositions.append(ReducerDisposition(
                        line: record.line,
                        object: reducerUnresolved(
                            sessionID: sessionID, raw: object, line: record.line,
                            kind: "read", rule: "visual_frame_selection_v1",
                            reason: "visual_frame_ocr_missing"
                        )
                    ))
                }
            case "screen_ocr_observation":
                let screenRecordID = stringValue(object["recordID"]) ?? ""
                if ["ax-pane-read-v6", "ax-pane-read-v7"].contains(
                    readSurfaceEvidence?.ruleVersion ?? ""
                ),
                   readSurfaceEvidence?.bySourceRecordID[screenRecordID] == nil {
                    dispositions.append(ReducerDisposition(
                        line: record.line,
                        object: reducerUnresolved(
                            sessionID: sessionID, raw: object,
                            line: record.line, kind: "read",
                            rule: "read_surface_resolution_v6_plus",
                            reason: readSurfaceEvidence?
                                .unresolvedReasonBySourceRecordID[screenRecordID]
                                ?? "read_surface_evidence_missing"
                        )
                    ))
                    continue
                }
                let effectiveObject = effectiveReadObject(
                    object,
                    evidence: readSurfaceEvidence?.bySourceRecordID[
                        screenRecordID
                    ],
                    unresolvedReason: readSurfaceEvidence?.unresolvedReasonBySourceRecordID[
                        screenRecordID
                    ],
                    ruleVersion: readSurfaceEvidence?.ruleVersion
                )
                switch reduceRead(effectiveObject, sessionID: sessionID) {
                case .failure(let failure):
                    dispositions.append(ReducerDisposition(line: record.line, object: reducerUnresolved(
                        sessionID: sessionID, raw: effectiveObject, line: record.line,
                        kind: "read", rule: failure.rule, reason: failure.reason,
                        details: failure.details
                    )))
                case .success(let event):
                    guard let capturedAt = stringValue(effectiveObject["capturedAt"]) else {
                        dispositions.append(ReducerDisposition(line: record.line, object: reducerUnresolved(
                            sessionID: sessionID, raw: effectiveObject, line: record.line,
                            kind: "read", rule: "semantic_time_overlap_v1",
                            reason: "read_missing_captured_at"
                        )))
                        continue
                    }
                    candidates.append(ReducerCandidate(
                        rawLine: record.line, kind: "read",
                        overlapBoundaryAt: capturedAt, raw: effectiveObject, event: event
                    ))
                }
            case "visual_ocr_observation":
                guard reducerUsesVisualReadEvidence(configuration.reducerVersion) else {
                    continue
                }
                switch validateVisualReadEvidence(
                    record,
                    rawByID: rawByID,
                    sessionID: sessionID,
                    sourceDirectory: source
                ) {
                case .failure(let failure):
                    dispositions.append(ReducerDisposition(
                        line: record.line,
                        object: reducerUnresolved(
                            sessionID: sessionID, raw: object, line: record.line,
                            kind: "read", rule: failure.rule,
                            reason: failure.reason, details: failure.details
                        )
                    ))
                case .success(let visual):
                    let visualRecordID = stringValue(visual.object["recordID"]) ?? ""
                    guard let surfaceEvidence = readSurfaceEvidence?
                        .bySourceRecordID[visualRecordID] else {
                        dispositions.append(ReducerDisposition(
                            line: record.line,
                            object: reducerUnresolved(
                                sessionID: sessionID, raw: visual.object,
                                line: record.line, kind: "read",
                                rule: "visual_active_pane_v1",
                                reason: readSurfaceEvidence?
                                    .unresolvedReasonBySourceRecordID[visualRecordID]
                                    ?? "visual_active_pane_evidence_missing",
                                sourceRecordIDs: visual.lineage
                            )
                        ))
                        continue
                    }
                    let effectiveVisual = effectiveReadObject(
                        visual.object,
                        evidence: surfaceEvidence,
                        unresolvedReason: nil,
                        ruleVersion: readSurfaceEvidence?.ruleVersion
                    )
                    switch reduceRead(
                        effectiveVisual,
                        sessionID: sessionID,
                        lineage: visual.lineage,
                        provenance: "visual_change_screen_ocr",
                        rule: "visual_frame_ocr_v1"
                    ) {
                    case .failure(let failure):
                        dispositions.append(ReducerDisposition(
                            line: record.line,
                            object: reducerUnresolved(
                                sessionID: sessionID, raw: visual.object,
                                line: record.line, kind: "read",
                                rule: failure.rule, reason: failure.reason,
                                details: failure.details,
                                sourceRecordIDs: visual.lineage
                            )
                        ))
                    case .success(let event):
                        guard let capturedAt = stringValue(effectiveVisual["capturedAt"]) else {
                            dispositions.append(ReducerDisposition(
                                line: record.line,
                                object: reducerUnresolved(
                                    sessionID: sessionID, raw: visual.object,
                                    line: record.line, kind: "read",
                                    rule: "visual_frame_ocr_v1",
                                    reason: "read_missing_captured_at",
                                    sourceRecordIDs: visual.lineage
                                )
                            ))
                            continue
                        }
                        candidates.append(ReducerCandidate(
                            rawLine: record.line, kind: "read",
                            overlapBoundaryAt: capturedAt,
                            raw: effectiveVisual, event: event
                        ))
                    }
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
                var effectiveWriteObject = writeObject
                if let recordID = stringValue(object["recordID"]),
                   fastStartChains[recordID] != nil {
                    effectiveWriteObject["semanticTargetIneligibilityReason"] =
                        "pre_first_mutation_conditioning_unavailable"
                    effectiveWriteObject["semanticFullFieldCompletion"] = true
                    effectiveWriteObject["semanticComposition"] =
                        "fast_start_history_only_completion"
                }
                let effectiveLineage: [String]
                if let recordID = stringValue(object["recordID"]),
                   let fastStart = fastStartChains[recordID] {
                    effectiveLineage = fastStart + [recordID]
                } else {
                    effectiveLineage = writeLineage
                }
                let closure = effectiveLineage.compactMap {
                    promptClosures.bySourceWriteRecordID[$0]
                }.sorted {
                    if $0.observedAt != $1.observedAt {
                        return $0.observedAt < $1.observedAt
                    }
                    return $0.rawLine < $1.rawLine
                }.first
                switch reduceWrite(
                    effectiveWriteObject,
                    sessionID: sessionID,
                    lineage: effectiveLineage,
                    promptClosure: closure
                ) {
                case .failure(let failure):
                    dispositions.append(ReducerDisposition(line: writeLine, object: reducerUnresolved(
                        sessionID: sessionID, raw: effectiveWriteObject, line: writeLine,
                        kind: "write", rule: failure.rule, reason: failure.reason,
                        details: failure.details,
                        sourceRecordIDs: effectiveLineage
                    )))
                case .success(let event):
                    guard let beganAt = stringValue(effectiveWriteObject["beganAt"]) else {
                        dispositions.append(ReducerDisposition(line: writeLine, object: reducerUnresolved(
                            sessionID: sessionID, raw: writeObject, line: writeLine,
                            kind: "write", rule: "semantic_time_overlap_v1",
                            reason: "write_missing_began_at",
                            sourceRecordIDs: effectiveLineage
                        )))
                        continue
                    }
                    candidates.append(ReducerCandidate(
                        rawLine: writeLine, kind: "write",
                        overlapBoundaryAt: beganAt, raw: effectiveWriteObject, event: event
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
        // v15 folds exact pointer/visual duplicates into its one globally
        // adjacent causal-state rule. The older per-surface cache would allow
        // X -> Y -> X suppression before global adjacency is established.
        let coincidentReadResult = configuration.reducerVersion
            == "phase1-semantic-v14"
            ? removeCoincidentDuplicateReads(
                candidates: authorshipReadResult.events,
                writeBoundaries: writeOverlapBoundaries,
                sessionID: sessionID
            )
            : ReducerOverlapResult(
                events: authorshipReadResult.events,
                dispositions: []
            )
        dispositions.append(contentsOf: coincidentReadResult.dispositions)
        let reconciledReadResult = usesReconciledSemanticReads
            ? reconcileEquivalentSensorReads(
                candidates: coincidentReadResult.events,
                writeBoundaries: writeOverlapBoundaries,
                sessionID: sessionID,
                sourceDirectory: source
            )
            : ReducerOverlapResult(
                events: coincidentReadResult.events,
                dispositions: []
            )
        dispositions.append(contentsOf: reconciledReadResult.dispositions)
        let dynamicVisualResult = reducerUsesVisualReadEvidence(
            configuration.reducerVersion
        )
            ? consolidateDynamicVisualReads(
                candidates: reconciledReadResult.events,
                writeBoundaries: writeOverlapBoundaries,
                attentionBoundaries: dynamicReadBoundaries,
                sessionID: sessionID,
                includeInterleavedObservations: usesReconciledSemanticReads
            )
            : ReducerOverlapResult(
                events: coincidentReadResult.events,
                dispositions: []
            )
        dispositions.append(contentsOf: dynamicVisualResult.dispositions)
        let overlapResult = applySemanticReadOverlap(
            candidates: dynamicVisualResult.events,
            writeBoundaries: writeOverlapBoundaries,
            sessionID: sessionID,
            paneAwareSurfaceIdentity: configuration.reducerVersion
                == "phase1-semantic-v13"
                || reducerUsesVisualReadEvidence(configuration.reducerVersion),
            mode: configuration.reducerVersion == "phase1-semantic-v20"
                ? .semanticV20
                : configuration.reducerVersion == "phase1-semantic-v19"
                ? .semanticV19
                : configuration.reducerVersion == "phase1-semantic-v18"
                ? .semanticV18
                : configuration.reducerVersion == "phase1-semantic-v17"
                ? .semanticV17
                : configuration.reducerVersion == "phase1-semantic-v16"
                    ? .semanticV16
                    : configuration.reducerVersion == "phase1-semantic-v15"
                        ? .destructiveV15
                        : .legacy
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
        var sourceDescription: [String: Any] = [
            "digestsSHA256": [
                "session.json": try reducerSHA256(sessionURL),
                "raw.jsonl": try reducerSHA256(rawURL),
            ],
            "rawRecordCount": raw.count,
        ]
        if let readSurfaceEvidence {
            sourceDescription["readSurfaceEvidence"] = [
                "schemaVersion": readSurfaceEvidence.schemaVersion,
                "ruleVersion": readSurfaceEvidence.ruleVersion,
                "manifestSHA256": readSurfaceEvidence.manifestSHA256,
                "readSurfacesSHA256": readSurfaceEvidence.readSurfacesSHA256,
                "unresolvedSHA256": readSurfaceEvidence.unresolvedSHA256,
                "evidenceCount": readSurfaceEvidence.bySourceRecordID.count,
                "fallbackCount": readSurfaceEvidence.unresolvedReasonBySourceRecordID.count,
            ]
        }
        var reduction: [String: Any] = [
            "schemaVersion": 1,
            "reducerVersion": configuration.reducerVersion,
            "sessionID": sessionID,
            "source": sourceDescription,
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
            "selectedReplacementRule": "an initial complete AX selection or explicit unpopulated-prompt state may expand a minimal diff to the exact replacement completion only when the replacement reconstructs the selected observation and the final ordered mutation checkpoint reaches that observation",
            "fastStartRule": "adjacent target-changing attempts whose typed-input count exactly explains the later prefilled prefix remain history-only with explicit target ineligibility",
            "promptClosureRule": "a settled WRITE gains a submission boundary only from a linked raw post-action observation whose terminal hash and pre-action state match, whose action is unmodified Return or has a semantic submission term anywhere on the bounded clicked AX ancestor chain, and whose same-surface field clears, restores its placeholder, or disappears",
            "readOverlapOrdering": "READ capturedAt with finalized WRITE beganAt boundaries; raw append order ignored",
            "readOverlapSurfaceIdentity": configuration.reducerVersion
                == "phase1-semantic-v15"
                    || configuration.reducerVersion == "phase1-semantic-v16"
                    || configuration.reducerVersion == "phase1-semantic-v17"
                    || configuration.reducerVersion == "phase1-semantic-v18"
                    || configuration.reducerVersion == "phase1-semantic-v19"
                    || configuration.reducerVersion == "phase1-semantic-v20"
                ? "globally adjacent causal READs with the same app/window, strongly overlapping capture geometry, and compatible AX pane role; AX depth and object identity are supporting evidence only"
                : configuration.reducerVersion == "phase1-semantic-v13"
                    || configuration.reducerVersion == "phase1-semantic-v14"
                ? "process + window + display + stable selected AX pane; a pane change resets adjacent overlap"
                : "process + window + display",
            "visualReadRule": reducerUsesVisualReadEvidence(configuration.reducerVersion)
                ? "hash-verified visual frame -> OCR lineage; active-WRITE-overlapped frames never become READs; exact coincident pointer/visual observations are emitted once; uninterrupted progressive states on one dynamic surface emit only their final causally available viewport"
                : "not enabled",
            "previewAuthority": false,
        ]
        if let readSurfaceEvidence {
            reduction["readSurfaceRule"] = [
                "ax-pane-read-v6", "ax-pane-read-v7",
            ].contains(readSurfaceEvidence.ruleVersion)
                ? "hash-verified \(readSurfaceEvidence.ruleVersion) evidence before causal overlap; unresolved pane observations remain raw evidence and are excluded from semantic READs"
                : "hash-verified \(readSurfaceEvidence.ruleVersion) evidence before causal overlap; unresolved evidence falls back to collector OCR"
        }
        if configuration.reducerVersion == "phase1-semantic-v15" {
            reduction["adjacentReadDeltaRule"] = "only globally adjacent causal READ states on a proven compatible surface may align; any WRITE, other surface, browser-title change, geometry change, or uncertain order-preserving alignment resets to the complete current READ"
        }
        if configuration.reducerVersion == "phase1-semantic-v16"
            || configuration.reducerVersion == "phase1-semantic-v17"
            || configuration.reducerVersion == "phase1-semantic-v18"
            || configuration.reducerVersion == "phase1-semantic-v19"
            || configuration.reducerVersion == "phase1-semantic-v20" {
            reduction["semanticReadRule"] = configuration.reducerVersion
                == "phase1-semantic-v20"
                ? "full selected-pane OCR is projected to complete semantic READ content by conservative application rules plus session-wide exact-recurrence and strictly evidenced clipped-line removal; comparison-only OCR can suppress unsupported novelty but can emit positive novelty only when strictly grounded in the cleaned full-pane state"
                : configuration.reducerVersion == "phase1-semantic-v19"
                ? "full selected-pane OCR is projected to complete semantic READ content by conservative application rules plus session-wide exact-recurrence and clipped-line evidence; reflow-tolerant novelty is recorded separately and never overwrites complete content"
                : configuration.reducerVersion == "phase1-semantic-v18"
                ? "full selected-pane OCR is projected to complete semantic READ content by conservative application rules plus session-wide exact-recurrence evidence that can only remove proven interface scaffolding; reflow-tolerant novelty is recorded separately and never overwrites complete content"
                : "immutable observed OCR is projected to complete semantic READ content by conservative application rules plus session-wide exact-recurrence evidence that can only remove proven interface scaffolding; exact contiguous edge novelty is recorded separately and never overwrites complete content"
            reduction["readNoveltyDependencyRule"] = "novel content may be rendered only when its complete predecessor is usable in context; otherwise render the complete semantic READ"
        }
        if configuration.reducerVersion == "phase1-semantic-v17" {
            reduction["adjacentReadNoveltyRule"] = "after exact normalized edge overlap, align OCR lines in order; a fuzzy line may differ by at most two nonnumeric characters, requires at least 24 characters of exact aligned line context, and only matched current lines are removable; ambiguous alignment retains the complete current READ"
        }
        if usesReconciledSemanticReads {
            reduction["readObservationRule"] = "same-surface sensor observations are reconciled before semantic novelty; passive visual-response progress is represented by its final causally available state only within an uninterrupted attention interval and never crosses click, post-click, scroll, activation/focus, surface-transition, pre-WRITE, WRITE-onset, or surface boundaries"
            reduction["dynamicReadBoundaryRule"] = "raw material-input intervals are ordered by firstActivityAt/lastActivityAt rather than append order; observations inside or derived from those intervals remain independent READ evidence and cannot participate in passive-state consolidation"
            reduction["dynamicReadBoundaryCount"] = dynamicReadBoundaries.count
            reduction["readContentAuthority"] = [
                "phase1-semantic-v19", "phase1-semantic-v20",
            ].contains(configuration.reducerVersion)
                ? "the complete selected AX pane is authoritative after removing only geometrically proven clipped OCR lines; a 20 percent top/bottom interior projection supplies independent OCR comparison evidence"
                : "the complete selected AX pane is authoritative; a 20 percent top/bottom interior projection supports comparison but cannot remove edge content"
            reduction["adjacentReadNoveltyRule"] = "after exact normalized edge overlap, compare complete pane text with reflow-tolerant ordered token alignment; line boundaries are not semantic, numeric changes remain distinct except one/ell and zero/oh OCR glyph equivalence, and uncertain alignment retains the complete current READ"
            if configuration.reducerVersion == "phase1-semantic-v19" {
                reduction["clippedOCRRule"] = "remove a line only when it is low-confidence, abnormally short, and touches the exact OCR-region boundary, or when same-state cross-sensor observations materially disagree on an abnormally short lower-boundary line or a wide upper-boundary line; suppress novelty when an independent interior OCR projection proves the claimed interior change absent, plus tiny peripheral projection disagreements"
            }
            if configuration.reducerVersion == "phase1-semantic-v20" {
                reduction["clippedOCRRule"] = "remove exact-edge lines only with low-confidence short geometry; cross-sensor peripheral removals additionally require low confidence, punctuation-only evidence, or materially wide short-line evidence, so disagreement alone cannot remove a high-confidence compact label; positive comparison novelty must occur verbatim after whitespace reflow in the cleaned authoritative full-pane state"
            }
        }
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
private struct ReducerReadSurfaceEvidence {
    let schemaVersion: Int
    let ruleVersion: String
    let manifestSHA256: String
    let readSurfacesSHA256: String
    let unresolvedSHA256: String
    let bySourceRecordID: [String: [String: Any]]
    let unresolvedReasonBySourceRecordID: [String: String]
}
private struct ReducerCandidate {
    let rawLine: Int
    let kind: String
    let overlapBoundaryAt: String
    let raw: [String: Any]
    var event: [String: Any]
    var comparisonContent: String? = nil
}
private struct ReducerDisposition { let line: Int; let object: [String: Any] }
private struct ReducerWriteBoundary { let rawLine: Int; let beganAt: String }
private struct ReducerDynamicReadBoundary {
    let rawLine: Int
    let beganAt: String
    let endedAt: String
    let triggerTypes: [String]
    let sourceRecordID: String?
}
private struct ReducerPromptClosure {
    let rawLine: Int
    let recordID: String
    let sourceWriteRecordID: String
    let observedAt: String
    let evidence: [String: Any]
}
private struct ReducerPromptClosureResult {
    let bySourceWriteRecordID: [String: ReducerPromptClosure]
    let dispositions: [ReducerDisposition]
}
private struct ReducerOverlapResult {
    let events: [ReducerCandidate]
    let dispositions: [ReducerDisposition]
}
private struct ReducerVisualReadEvidence {
    let object: [String: Any]
    let lineage: [String]
}

private enum ReducerReadOverlapMode: Equatable {
    case legacy
    case destructiveV15
    case semanticV16
    case semanticV17
    case semanticV18
    case semanticV19
    case semanticV20
}
private struct ReducerFailure: Error {
    let rule: String
    let reason: String
    let details: [String: Any]
}

private func reducerUsesVisualReadEvidence(_ version: String) -> Bool {
    version == "phase1-semantic-v14"
        || version == "phase1-semantic-v15"
        || version == "phase1-semantic-v16"
        || version == "phase1-semantic-v17"
        || version == "phase1-semantic-v18"
        || version == "phase1-semantic-v19"
        || version == "phase1-semantic-v20"
}

/// Returns raw interaction intervals that divide autonomous visual progress
/// into separate attention intervals. Pointer motion alone is intentionally
/// absent: it can start a settled READ without proving that the user changed
/// the viewed material. Clicks, scrolling, activation/surface transitions,
/// and the checkpoint immediately preceding a WRITE are material boundaries.
private func reducerDynamicReadBoundaries(
    _ raw: [ReducerLine]
) -> [ReducerDynamicReadBoundary] {
    let materialTriggerTypes: Set<String> = [
        "application_activated",
        "click",
        "post_click_surface_observation",
        "pre_write_visual_checkpoint",
        "scroll",
        "surface_transition_detected",
    ]
    return raw.compactMap { record in
        let object = record.object
        let triggerTypes = stringArray(object["triggerTypes"])
            .filter { materialTriggerTypes.contains($0) }
        guard !triggerTypes.isEmpty else { return nil }
        guard let beganAt = nonEmptyString(object["firstActivityAt"])
                ?? nonEmptyString(object["capturedAt"])
                ?? nonEmptyString(object["observedAt"]) else { return nil }
        let endedAt = nonEmptyString(object["lastActivityAt"]) ?? beganAt
        return ReducerDynamicReadBoundary(
            rawLine: record.line,
            beganAt: beganAt,
            endedAt: endedAt,
            triggerTypes: triggerTypes,
            sourceRecordID: nonEmptyString(object["recordID"])
        )
    }
}

private func loadReadSurfaceEvidence(
    configuration: Phase1SemanticReducerConfiguration,
    sessionURL: URL,
    rawURL: URL,
    sessionID: String,
    rawScreenOCRSchema: Int,
    raw: [ReducerLine]
) throws -> ReducerReadSurfaceEvidence? {
    let expectedRuleVersions: Set<String>
    let expectedReadRecordTypes: Set<String>
    switch configuration.reducerVersion {
    case "phase1-semantic-v11":
        expectedRuleVersions = ["pointer-local-read-v1"]
        expectedReadRecordTypes = ["screen_ocr_observation"]
    case "phase1-semantic-v12", "phase1-semantic-v13":
        expectedRuleVersions = rawScreenOCRSchema >= 7
            ? ["ax-pane-read-v2", "ax-pane-read-v3", "ax-pane-read-v4"]
            : ["pointer-local-read-v1"]
        expectedReadRecordTypes = ["screen_ocr_observation"]
    case "phase1-semantic-v14", "phase1-semantic-v15", "phase1-semantic-v16",
         "phase1-semantic-v17":
        expectedRuleVersions = rawScreenOCRSchema >= 7
            ? ["ax-pane-read-v2", "ax-pane-read-v3", "ax-pane-read-v4"]
            : ["pointer-local-read-v1"]
        expectedReadRecordTypes = [
            "screen_ocr_observation", "visual_ocr_observation",
        ]
    case "phase1-semantic-v18", "phase1-semantic-v19",
         "phase1-semantic-v20":
        expectedRuleVersions = rawScreenOCRSchema >= 7
            ? ["ax-pane-read-v5", "ax-pane-read-v6", "ax-pane-read-v7"]
            : ["pointer-local-read-v1"]
        expectedReadRecordTypes = [
            "screen_ocr_observation", "visual_ocr_observation",
        ]
    default:
        guard configuration.readSurfaceEvidenceDirectory == nil else {
            throw Phase1SemanticReducerError.invalidManifest(
                "--read-surface-evidence requires phase1-semantic-v11 through phase1-semantic-v20"
            )
        }
        return nil
    }
    guard let directory = configuration.readSurfaceEvidenceDirectory?
        .standardizedFileURL else {
        throw Phase1SemanticReducerError.invalidManifest(
            "\(configuration.reducerVersion) requires --read-surface-evidence"
        )
    }
    let manifestURL = directory.appendingPathComponent("read-surface-evidence.json")
    let evidenceURL = directory.appendingPathComponent("read-surfaces.jsonl")
    let unresolvedURL = directory.appendingPathComponent("unresolved.jsonl")
    let jobsURL = directory.appendingPathComponent("jobs.jsonl")
    for url in [manifestURL, evidenceURL, unresolvedURL, jobsURL]
        where !FileManager.default.fileExists(atPath: url.path)
    {
        throw Phase1SemanticReducerError.missingFile(url.path)
    }
    let manifestData = try Data(contentsOf: manifestURL)
    let sessionHash = try reducerSHA256(sessionURL)
    let rawHash = try reducerSHA256(rawURL)
    let jobsHash = try reducerSHA256(jobsURL)
    let readSurfacesHash = try reducerSHA256(evidenceURL)
    let unresolvedHash = try reducerSHA256(unresolvedURL)
    let evidenceManifestHash = try reducerSHA256(manifestURL)
    guard let manifest = try JSONSerialization.jsonObject(with: manifestData)
        as? [String: Any],
        intValue(manifest["schemaVersion"]) == 1,
        let evidenceRuleVersion = stringValue(manifest["ruleVersion"]),
        expectedRuleVersions.contains(evidenceRuleVersion),
        stringValue(manifest["sessionID"]) == sessionID,
        let source = manifest["source"] as? [String: Any],
        let sourceDigests = source["digestsSHA256"] as? [String: Any],
        stringValue(sourceDigests["session.json"]) == sessionHash,
        stringValue(sourceDigests["raw.jsonl"]) == rawHash,
        let artifacts = manifest["artifacts"] as? [String: Any],
        let artifactDigests = artifacts["digestsSHA256"] as? [String: Any],
        stringValue(artifactDigests["jobs.jsonl"]) == jobsHash,
        stringValue(artifactDigests["read-surfaces.jsonl"])
            == readSurfacesHash,
        stringValue(artifactDigests["unresolved.jsonl"])
            == unresolvedHash else {
        throw Phase1SemanticReducerError.invalidManifest(
            "read-surface evidence identity or digest differs from the raw session"
        )
    }
    let manifestedRecordTypes = (manifest["ruleSelection"] as? [String: Any])
        .map { stringArray($0["includedRecordTypes"]) } ?? []
    guard Set(manifestedRecordTypes.isEmpty
        ? ["screen_ocr_observation"]
        : manifestedRecordTypes) == expectedReadRecordTypes else {
        throw Phase1SemanticReducerError.invalidManifest(
            "read-surface evidence does not cover the reducer's expected READ record types"
        )
    }

    let rawReadRecords = raw.filter {
        stringValue($0.object["recordType"]).map {
            expectedReadRecordTypes.contains($0)
        } == true
    }
    let rawReadByID = Dictionary(uniqueKeysWithValues: rawReadRecords.compactMap {
        line in stringValue(line.object["recordID"]).map { ($0, line) }
    })
    var evidenceBySourceID = [String: [String: Any]]()
    for row in try reducerReadJSONL(evidenceURL) {
        let object = row.object
        guard intValue(object["schemaVersion"]) == 1,
              stringValue(object["ruleVersion"]) == evidenceRuleVersion,
              stringValue(object["sessionID"]) == sessionID,
              let sourceID = nonEmptyString(object["sourceRecordID"]),
              let sourceRecord = rawReadByID[sourceID],
              intValue(object["sourceRawLine"]) == sourceRecord.line,
              stringValue(object["capturedAt"])
                == stringValue(sourceRecord.object["capturedAt"]),
              stringValue(object["screenshotSHA256"])
                == stringValue(sourceRecord.object["screenshotSHA256"]),
              let content = object["content"] as? String, !content.isEmpty,
              nonEmptyString(object["evidenceID"]) != nil,
              stringValue(object["contentSHA256"])
                == reducerSHA256String(content),
              intValue(object["recognizedLineCount"]) != nil,
              object["surfaceSelection"] is [String: Any],
              validNormalizedRegion(object["regionOfInterest"] as? [String: Any]),
              evidenceBySourceID[sourceID] == nil else {
            throw Phase1SemanticReducerError.invalidManifest(
                "invalid or duplicate read-surface evidence at line \(row.line)"
            )
        }
        if ["ax-pane-read-v5", "ax-pane-read-v6", "ax-pane-read-v7"]
            .contains(evidenceRuleVersion) {
            guard let comparison = object["comparisonContent"] as? String,
                  !comparison.isEmpty,
                  stringValue(object["comparisonContentSHA256"])
                    == reducerSHA256String(comparison),
                  intValue(object["comparisonRecognizedLineCount"]) != nil,
                  object["comparisonLines"] is [[String: Any]],
                  validNormalizedRegion(
                    object["comparisonRegionOfInterest"] as? [String: Any]
                  ) else {
                throw Phase1SemanticReducerError.invalidManifest(
                    "invalid v5 comparison projection at line \(row.line)"
                )
            }
        }
        evidenceBySourceID[sourceID] = object
    }

    var unresolvedBySourceID = [String: String]()
    for row in try reducerReadJSONL(unresolvedURL) {
        guard intValue(row.object["schemaVersion"]) == 1,
              stringValue(row.object["ruleVersion"]) == evidenceRuleVersion,
              stringValue(row.object["sessionID"]) == sessionID,
              let sourceID = nonEmptyString(row.object["sourceRecordID"]),
              let sourceRecord = rawReadByID[sourceID],
              intValue(row.object["sourceRawLine"]) == sourceRecord.line,
              let reason = nonEmptyString(row.object["reason"]),
              evidenceBySourceID[sourceID] == nil,
              unresolvedBySourceID[sourceID] == nil else {
            throw Phase1SemanticReducerError.invalidManifest(
                "invalid, overlapping, or duplicate read-surface disposition at line \(row.line)"
            )
        }
        unresolvedBySourceID[sourceID] = reason
    }
    let disposed = Set(evidenceBySourceID.keys).union(unresolvedBySourceID.keys)
    guard disposed == Set(rawReadByID.keys),
          let counts = manifest["counts"] as? [String: Any],
          intValue(counts["rawRecords"]) == raw.count,
          (intValue(counts["readObservations"])
            ?? intValue(counts["screenObservations"])) == rawReadRecords.count,
          intValue(counts["evidence"]) == evidenceBySourceID.count,
          intValue(counts["unresolved"]) == unresolvedBySourceID.count else {
        throw Phase1SemanticReducerError.invalidManifest(
            "read-surface evidence does not account for every screen observation"
        )
    }
    return ReducerReadSurfaceEvidence(
        schemaVersion: 1,
        ruleVersion: evidenceRuleVersion,
        manifestSHA256: evidenceManifestHash,
        readSurfacesSHA256: readSurfacesHash,
        unresolvedSHA256: unresolvedHash,
        bySourceRecordID: evidenceBySourceID,
        unresolvedReasonBySourceRecordID: unresolvedBySourceID
    )
}

private func validNormalizedRegion(_ region: [String: Any]?) -> Bool {
    guard let region,
          let x = doubleValue(region["x"]),
          let y = doubleValue(region["y"]),
          let width = doubleValue(region["width"]),
          let height = doubleValue(region["height"]) else { return false }
    let tolerance = 0.000_001
    return x >= -tolerance && y >= -tolerance
        && width > 0 && height > 0
        && x + width <= 1 + tolerance
        && y + height <= 1 + tolerance
}

private func effectiveReadObject(
    _ raw: [String: Any],
    evidence: [String: Any]?,
    unresolvedReason: String?,
    ruleVersion: String?
) -> [String: Any] {
    var effective = raw
    guard let evidence else {
        if let unresolvedReason, let ruleVersion {
            effective["readSurface"] = [
                "schemaVersion": 1,
                "ruleVersion": ruleVersion,
                "status": "fallback_original_ocr",
                "reason": unresolvedReason,
            ]
        }
        return effective
    }
    let originalContent = stringValue(raw["content"]) ?? ""
    effective["content"] = evidence["content"]
    effective["recognizedLineCount"] = evidence["recognizedLineCount"]
    // Private reducer input. It is stripped before semantic events are written.
    // Keeping the line geometry beside the observed OCR lets v16 explain every
    // scaffolding decision without changing the immutable evidence artifact.
    effective["_readSurfaceLines"] = evidence["lines"]
    if let comparisonContent = evidence["comparisonContent"] as? String,
       let comparisonLines = evidence["comparisonLines"] as? [[String: Any]] {
        effective["_readComparisonContent"] = comparisonContent
        effective["_readComparisonLines"] = comparisonLines
    }
    effective["contentWasTruncated"] = false
    for key in [
        "viewportSideCropFraction", "viewportTopCropFraction",
        "viewportBottomCropFraction",
    ] { effective.removeValue(forKey: key) }
    if let bounds = readSurfaceCaptureBounds(raw: raw, evidence: evidence) {
        effective["captureBounds"] = bounds
    }
    let evidenceRuleVersion = stringValue(evidence["ruleVersion"])
        ?? ruleVersion ?? "unknown"
    effective["captureScope"] = [
        "ax-pane-read-v2", "ax-pane-read-v3", "ax-pane-read-v4",
        "ax-pane-read-v5", "ax-pane-read-v6", "ax-pane-read-v7",
    ]
        .contains(evidenceRuleVersion)
        ? "active_ax_pane"
        : "active_surface_proxy"
    var readSurface: [String: Any] = [
        "schemaVersion": 1,
        "ruleVersion": evidenceRuleVersion,
        "status": "surface_ocr_replacement",
        "evidenceID": evidence["evidenceID"]!,
        "surfaceSelection": evidence["surfaceSelection"]!,
        "regionOfInterest": evidence["regionOfInterest"]!,
        "originalContentSHA256": reducerSHA256String(originalContent),
        "replacementContentSHA256": evidence["contentSHA256"]!,
    ]
    if ["ax-pane-read-v5", "ax-pane-read-v6", "ax-pane-read-v7"]
        .contains(evidenceRuleVersion),
       let comparisonRegion = evidence["comparisonRegionOfInterest"],
       let comparisonHash = evidence["comparisonContentSHA256"] {
        readSurface["comparisonRegionOfInterest"] = comparisonRegion
        readSurface["comparisonContentSHA256"] = comparisonHash
        readSurface["contentAuthority"] = "full_selected_ax_pane"
        readSurface["comparisonProjection"] = "interior_20_percent_edge_inset"
    }
    effective["readSurface"] = readSurface
    return effective
}

private func readSurfaceCaptureBounds(
    raw: [String: Any], evidence: [String: Any]
) -> [String: Any]? {
    guard let window = raw["windowBounds"] as? [String: Any],
          let region = evidence["regionOfInterest"] as? [String: Any],
          validNormalizedRegion(region),
          let windowX = doubleValue(window["x"]),
          let windowY = doubleValue(window["y"]),
          let windowWidth = doubleValue(window["width"]),
          let windowHeight = doubleValue(window["height"]),
          let x = doubleValue(region["x"]),
          let y = doubleValue(region["y"]),
          let width = doubleValue(region["width"]),
          let height = doubleValue(region["height"]) else { return nil }
    return [
        "x": windowX + (x * windowWidth),
        "y": windowY + ((1 - y - height) * windowHeight),
        "width": width * windowWidth,
        "height": height * windowHeight,
    ]
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

private func validatedPromptClosures(
    _ records: [ReducerLine],
    rawByID: [String: ReducerLine],
    sessionID: String
) -> ReducerPromptClosureResult {
    let positiveDispositions: Set<String> = [
        "confirmed_field_cleared",
        "confirmed_placeholder_restored",
        "confirmed_field_disappeared",
    ]
    let submissionTerms: Set<String> = [
        "send", "submit", "post", "publish", "search", "ask", "go", "continue",
    ]
    var accepted = [String: ReducerPromptClosure]()
    var dispositions = [ReducerDisposition]()

    func reject(_ record: ReducerLine, reason: String, details: [String: Any] = [:]) {
        dispositions.append(ReducerDisposition(
            line: record.line,
            object: reducerUnresolved(
                sessionID: sessionID,
                raw: record.object,
                line: record.line,
                kind: "write_closure",
                rule: "prompt_closure_evidence_v1",
                reason: reason,
                details: details
            )
        ))
    }

    for record in records where
        stringValue(record.object["recordType"]) == "prompt_submission_observation"
    {
        let raw = record.object
        guard stringValue(raw["sessionID"]) == sessionID else {
            reject(record, reason: "closure_session_identity_mismatch")
            continue
        }
        guard let disposition = stringValue(raw["disposition"]),
              positiveDispositions.contains(disposition) else {
            continue
        }
        guard let recordID = stringValue(raw["recordID"]),
              let sourceID = stringValue(raw["sourceWriteRecordID"]),
              let source = rawByID[sourceID]?.object,
              stringValue(source["recordType"]) == "active_tap_write_attempt" else {
            reject(record, reason: "closure_source_write_missing")
            continue
        }
        guard stringValue(source["sessionID"]) == sessionID,
              stringValue(source["boundaryReason"]) == "write_delay_elapsed",
              let terminal = source["after"] as? [String: Any],
              terminal["valueWasTruncated"] as? Bool != true,
              let terminalRaw = stringValue(terminal["value"]),
              let terminalObservationID = stringValue(terminal["observationID"]),
              terminalObservationID == stringValue(raw["terminalObservationID"]) else {
            reject(record, reason: "closure_terminal_observation_mismatch")
            continue
        }
        let terminalValue = logicalEditableValue(
            terminalRaw,
            placeholderValue: stringValue(terminal["placeholderValue"])
        )
        let terminalHash = reducerSHA256String(terminalValue)
        guard !terminalValue.isEmpty,
              terminalHash == stringValue(raw["terminalValueSHA256"]),
              terminalValue.count == intValue(raw["terminalCharacterCount"]),
              let preAction = raw["preActionObservation"] as? [String: Any],
              stringArray(raw["preActionAXErrors"]).isEmpty,
              preAction["valueWasTruncated"] as? Bool != true,
              let preActionRaw = stringValue(preAction["value"]),
              reducerSHA256String(logicalEditableValue(
                preActionRaw,
                placeholderValue: stringValue(preAction["placeholderValue"])
              )) == terminalHash else {
            reject(record, reason: "closure_pre_action_state_mismatch")
            continue
        }
        guard stringArray(raw["surfaceValidationErrors"]).isEmpty,
              let action = raw["action"] as? [String: Any],
              let actionKind = stringValue(action["kind"]),
              let actionAt = stringValue(action["observedAt"]),
              let observedAt = stringValue(raw["observedAt"]),
              let retainedAt = stringValue(raw["referenceRetainedAt"]),
              retainedAt <= actionAt,
              actionAt <= observedAt else {
            reject(record, reason: "closure_action_or_timing_invalid")
            continue
        }
        let actionProvesSubmission = actionKind == "unmodified_return"
            || (actionKind == "pointer_click"
                && stringValue(action["matchedSubmissionTerm"]).map {
                    submissionTerms.contains($0)
                } == true)
        guard actionProvesSubmission else {
            reject(record, reason: "closure_action_not_semantically_submissive")
            continue
        }

        let postAction = raw["postActionObservation"] as? [String: Any]
        let postErrors = stringArray(raw["postActionAXErrors"])
        let transitionIsValid: Bool
        switch disposition {
        case "confirmed_field_disappeared":
            transitionIsValid = postAction == nil && postErrors.contains(where: {
                $0.localizedCaseInsensitiveContains("invalid_ui_element")
            })
        case "confirmed_field_cleared":
            transitionIsValid = postErrors.isEmpty
                && postAction.flatMap { stringValue($0["value"]) }.map {
                    logicalEditableValue(
                        $0,
                        placeholderValue: stringValue(postAction?["placeholderValue"])
                    ).isEmpty
                } == true
        case "confirmed_placeholder_restored":
            transitionIsValid = postErrors.isEmpty
                && postAction.flatMap { stringValue($0["value"]) }.map {
                    logicalEditableValue(
                        $0,
                        placeholderValue: stringValue(postAction?["placeholderValue"])
                    ).isEmpty
                } == true
        default:
            transitionIsValid = false
        }
        guard transitionIsValid else {
            reject(record, reason: "closure_post_action_transition_invalid")
            continue
        }
        guard accepted[sourceID] == nil else {
            reject(record, reason: "duplicate_confirmed_closure_for_write")
            continue
        }
        let evidence: [String: Any] = [
            "schemaVersion": 1,
            "status": "submitted",
            "sourceRecordID": recordID,
            "observedAt": observedAt,
            "disposition": disposition,
            "action": action,
            "terminalObservationID": terminalObservationID,
            "preActionObservationID": stringValue(preAction["observationID"]) ?? "",
            "postActionObservationID": stringValue(postAction?["observationID"]) as Any,
            "rule": "prompt_closure_evidence_v1",
        ]
        accepted[sourceID] = ReducerPromptClosure(
            rawLine: record.line,
            recordID: recordID,
            sourceWriteRecordID: sourceID,
            observedAt: observedAt,
            evidence: removeNullOptionals(evidence)
        )
    }
    return ReducerPromptClosureResult(
        bySourceWriteRecordID: accepted,
        dispositions: dispositions
    )
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

/// Renderer-backed fields can replace their AX element during the first few
/// keystrokes. The surviving attempt then begins with those characters already
/// in BEFORE. Preserve the eventual completion as later history, but never use
/// that later query as supervision for the full action. A chain is recognized
/// only when adjacent target-changing attempts contain typed input whose exact
/// count explains the surviving prefix; no key payload is guessed.
private func reducerFastStartChains(
    _ records: [ReducerLine]
) -> [String: [String]] {
    let attempts = records.filter {
        stringValue($0.object["recordType"]) == "active_tap_write_attempt"
    }
    var result = [String: [String]]()
    for index in attempts.indices.dropFirst() {
        let current = attempts[index].object
        guard let before = current["before"] as? [String: Any],
              stringArray(current["beforeAXErrors"]).isEmpty,
              before["valueWasTruncated"] as? Bool != true,
              let rawBefore = stringValue(before["value"]),
              let recordID = stringValue(current["recordID"]),
              let currentAt = stringValue(current["beganAt"]).flatMap(reducerTimestamp) else {
            continue
        }
        let beforeValue = logicalEditableValue(
                rawBefore,
                placeholderValue: stringValue(before["placeholderValue"])
              )
        guard !beforeValue.isEmpty,
              intValue(before["selectedRangeLength"]) == 0,
              intValue(before["selectedRangeLocation"]) == beforeValue.utf16.count,
              let firstCheckpoint =
                (current["mutationCheckpoints"] as? [[String: Any]])?.first,
              stringArray(firstCheckpoint["axErrors"]).isEmpty,
              let observation = firstCheckpoint["observation"] as? [String: Any],
              observation["valueWasTruncated"] as? Bool != true,
              let rawCheckpoint = stringValue(observation["value"]) else { continue }
        let checkpointValue = logicalEditableValue(
            rawCheckpoint,
            placeholderValue: stringValue(observation["placeholderValue"])
        )
        guard checkpointValue.hasPrefix(beforeValue),
              checkpointValue.count > beforeValue.count else { continue }

        var lineage = [String]()
        var explainedInputCount = 0
        var cursor = attempts.index(before: index)
        while true {
            let prior = attempts[cursor].object
            guard stringValue(prior["boundaryReason"]) == "target_changed",
                  Set(stringArray(prior["inputHints"])).isSubset(of: ["typed"]),
                  (intValue(prior["inputEventCount"]) ?? 0) > 0,
                  stringValue(prior["bundleIdentifier"])
                    == stringValue(current["bundleIdentifier"]),
                  intValue(prior["processIdentifier"])
                    == intValue(current["processIdentifier"]),
                  let priorAt = stringValue(prior["beganAt"]).flatMap(reducerTimestamp),
                  currentAt >= priorAt,
                  currentAt.timeIntervalSince(priorAt) <= 1.0,
                  let priorID = stringValue(prior["recordID"]) else { break }
            let priorWindow = stringValue(
                (prior["targetIdentity"] as? [String: Any])?["windowTitle"]
            )
            let currentWindow = stringValue(
                (current["targetIdentity"] as? [String: Any])?["windowTitle"]
            )
            if let priorWindow, let currentWindow, priorWindow != currentWindow {
                break
            }
            lineage.insert(priorID, at: 0)
            explainedInputCount += intValue(prior["inputEventCount"]) ?? 0
            if explainedInputCount >= beforeValue.count || cursor == attempts.startIndex {
                break
            }
            cursor = attempts.index(before: cursor)
        }
        guard explainedInputCount == beforeValue.count, !lineage.isEmpty else { continue }
        result[recordID] = lineage
    }
    return result
}

/// A generic raw shortcut hint does not reveal whether the user selected,
/// moved, or transformed text. If typing continues afterward and AX did not
/// produce a boundary observation for that shortcut, the final content cannot
/// be assigned to the initial cursor query without guessing. Shortcuts at the
/// end of a burst remain harmless boundaries.
private func hasUnobservedMidBurstShortcut(_ raw: [String: Any]) -> Bool {
    let events = raw["inputEvents"] as? [[String: Any]] ?? []
    for index in events.indices where stringValue(events[index]["hint"]) == "shortcut" {
        let hasMutationBefore = events[..<index].contains {
            ($0["mutationCapable"] as? Bool) == true
        }
        let hasMutationAfter = events[events.index(after: index)...].contains {
            ($0["mutationCapable"] as? Bool) == true
        }
        if hasMutationBefore && hasMutationAfter { return true }
    }
    return false
}

private struct CheckpointGroundedEdit {
    let edit: TextEdit
    let observationCount: Int
    let rule: String
}

private struct TaggedReducerCharacter {
    let character: Character
    let originalIndex: Int?
}

/// Uses a complete range-native initial selection and ordered post-input field
/// states to choose among edits which are already equivalent minimal
/// reconstructions of the same BEFORE and AFTER. The cursor can resolve only
/// the alignment of an otherwise ambiguous edit; it cannot change edit size,
/// introduce transient typo text, or create a different document transition.
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
    var expectedCaret = rangeNativeInitialCaret(raw: raw, beforeValue: beforeValue)
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
        edit: edit, observationCount: usedObservationCount,
        rule: "checkpoint_grounded_equivalent_diff_v1"
    )
}

/// AX numeric selections were historically unreliable in rich editors, so a
/// bare selectedRangeLocation must never steer reconstruction. A successful
/// accessibility_string_for_range capture proves that the numeric selection
/// and semantic left/selected/right strings came from the same synchronous
/// pre-mutation observation. That evidence is safe to use only as a tie-break
/// among equal-size edits which all reconstruct the identical AFTER state.
private func rangeNativeInitialCaret(
    raw: [String: Any],
    beforeValue: String
) -> Int? {
    guard let conditioning = raw["conditioningState"] as? [String: Any],
          let cursor = conditioning["cursorContext"] as? [String: Any],
          stringValue(cursor["source"]) == "accessibility_string_for_range",
          stringValue(cursor["captureStatus"]) == "complete",
          let before = raw["before"] as? [String: Any],
          let probe = before["axRangeCursorProbe"] as? [String: Any],
          stringArray(probe["errors"]).isEmpty,
          let selectionStartUTF16 = intValue(before["selectedRangeLocation"]) else {
        return nil
    }
    return characterOffset(in: beforeValue, utf16Offset: selectionStartUTF16)
}

/// A minimal document diff is not always the human completion. If selected
/// text and its replacement share a prefix or suffix, a minimal diff omits the
/// shared characters even though the person typed (or accepted autocomplete
/// for) the complete replacement. The initial AX selection supplies the exact
/// replacement boundary. An explicit unpopulated-prompt query supplies the
/// equivalent empty logical field boundary when AX exposes prompt scaffolding
/// as value text.
///
/// This rule never concatenates keystrokes or temporary checkpoints. It uses
/// only the final selected observation, and accepts the expanded edit only when
/// it reconstructs that observation exactly and the final ordered mutation
/// checkpoint independently reaches the same value.
private func checkpointGroundedReplacementEdit(
    raw: [String: Any],
    beforeValue: String,
    afterValue: String,
    usedObservation: [String: Any],
    canonicalEdit: TextEdit
) -> CheckpointGroundedEdit? {
    let hints = Set(stringArray(raw["inputHints"]))
    guard hints.contains("typed"),
          hints.isDisjoint(with: ["paste", "cut", "undo_redo"]),
          !(raw["mutationCheckpoints"] as? [[String: Any]] ?? []).isEmpty else {
        return nil
    }
    let selectedAt = reducerTimestamp(
        stringValue(usedObservation["observedAt"])
            ?? stringValue(raw["terminalSnapshotAt"])
            ?? stringValue(raw["terminalDecisionAt"])
            ?? ""
    )
    let observations = (raw["mutationCheckpoints"] as? [[String: Any]] ?? [])
        .compactMap { checkpoint -> (Date, UInt64, String)? in
            guard stringArray(checkpoint["axErrors"]).isEmpty,
                  let observation = checkpoint["observation"] as? [String: Any],
                  observation["valueWasTruncated"] as? Bool != true,
                  let rawValue = stringValue(observation["value"]),
                  let capturedText = stringValue(observation["observedAt"]),
                  let capturedAt = reducerTimestamp(capturedText),
                  selectedAt == nil || capturedAt <= selectedAt! else { return nil }
            return (
                capturedAt,
                uint64Value(checkpoint["eventTimestampNanoseconds"]) ?? 0,
                logicalEditableValue(
                    rawValue,
                    placeholderValue: stringValue(observation["placeholderValue"])
                )
            )
        }
        .sorted {
            if $0.0 != $1.0 { return $0.0 < $1.0 }
            return $0.1 < $1.1
        }
    guard let final = observations.last, final.2 == afterValue else { return nil }

    let candidate: TextEdit?
    if let before = raw["before"] as? [String: Any],
       let startUTF16 = intValue(before["selectedRangeLocation"]),
       let lengthUTF16 = intValue(before["selectedRangeLength"]),
       lengthUTF16 > 0,
       let start = characterOffset(in: beforeValue, utf16Offset: startUTF16),
       let end = characterOffset(
        in: beforeValue, utf16Offset: startUTF16 + lengthUTF16
       ), end >= start {
        let old = Array(beforeValue)
        let new = Array(afterValue)
        let prefix = Array(old[..<start])
        let suffix = Array(old[end...])
        guard new.count >= prefix.count + suffix.count,
              Array(new.prefix(prefix.count)) == prefix,
              Array(new.suffix(suffix.count)) == suffix else { return nil }
        let insertedEnd = new.count - suffix.count
        let inserted = String(new[prefix.count..<insertedEnd])
        let removed = String(old[start..<end])
        candidate = TextEdit(
            operation: inserted.isEmpty ? .delete : .replace,
            characterOffset: start,
            removed: removed,
            inserted: inserted
        )
    } else if let conditioning = raw["conditioningState"] as? [String: Any],
              let cursor = conditioning["cursorContext"] as? [String: Any],
              stringValue(cursor["fieldState"]) == "unpopulated_prompt",
              stringValue(cursor["leftContext"]) == "",
              stringValue(cursor["selectedText"]) == "",
              stringValue(cursor["rightContext"]) == "" {
        candidate = TextEdit(
            operation: beforeValue.isEmpty ? .insert : .replace,
            characterOffset: 0,
            removed: beforeValue,
            inserted: afterValue
        )
    } else {
        candidate = nil
    }
    guard let candidate,
          candidate != canonicalEdit,
          !candidate.inserted.isEmpty,
          applying(candidate, to: beforeValue) == afterValue else { return nil }
    return CheckpointGroundedEdit(
        edit: candidate,
        observationCount: observations.count,
        rule: "checkpoint_grounded_selected_replacement_v1"
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

/// Pointer-triggered and visual-change sensors may preserve the same pixels as
/// separate raw observations. Reconcile only exact, near-simultaneous content
/// on the same captured window, and never cross an intervening WRITE boundary.
/// Broader fuzzy overlap remains the responsibility of the existing viewport
/// reducer below.
private func removeCoincidentDuplicateReads(
    candidates: [ReducerCandidate],
    writeBoundaries: [ReducerWriteBoundary],
    sessionID: String
) -> ReducerOverlapResult {
    struct PriorRead {
        let capturedAt: String
        let content: String
    }
    let orderedReadIndices = candidates.indices.filter {
        candidates[$0].kind == "read"
    }.sorted {
        let left = candidates[$0]
        let right = candidates[$1]
        if left.overlapBoundaryAt != right.overlapBoundaryAt {
            return left.overlapBoundaryAt < right.overlapBoundaryAt
        }
        return left.rawLine < right.rawLine
    }
    var latestBySurface = [String: PriorRead]()
    var excluded = Set<Int>()
    var dispositions = [ReducerDisposition]()
    for index in orderedReadIndices {
        let candidate = candidates[index]
        let surface = [
            String(intValue(candidate.raw["processIdentifier"]) ?? -1),
            String(intValue(candidate.raw["windowID"]) ?? -1),
            String(intValue(candidate.raw["displayID"]) ?? -1),
        ].joined(separator: "|")
        let content = normalizedReducerText(
            stringValue(candidate.raw["content"]) ?? ""
        )
        defer {
            latestBySurface[surface] = PriorRead(
                capturedAt: candidate.overlapBoundaryAt,
                content: content
            )
        }
        guard !content.isEmpty,
              let prior = latestBySurface[surface],
              prior.content == content,
              let priorDate = reducerTimestamp(prior.capturedAt),
              let currentDate = reducerTimestamp(candidate.overlapBoundaryAt),
              currentDate >= priorDate,
              currentDate.timeIntervalSince(priorDate) <= 1.5 else {
            continue
        }
        let crossedWrite = writeBoundaries.contains {
            $0.beganAt > prior.capturedAt
                && $0.beganAt <= candidate.overlapBoundaryAt
        }
        guard !crossedWrite else { continue }
        excluded.insert(index)
        dispositions.append(ReducerDisposition(
            line: candidate.rawLine,
            object: reducerUnresolved(
                sessionID: sessionID,
                raw: candidate.raw,
                line: candidate.rawLine,
                kind: "read",
                rule: "coincident_read_reconciliation_v1",
                reason: "exact_coincident_pointer_visual_duplicate",
                details: [
                    "priorCapturedAt": prior.capturedAt,
                    "capturedAt": candidate.overlapBoundaryAt,
                    "maximumIntervalSeconds": 1.5,
                ],
                sourceRecordIDs: candidate.event["sourceRecordIDs"] as? [String]
            )
        ))
    }
    return ReducerOverlapResult(
        events: candidates.indices.compactMap {
            excluded.contains($0) ? nil : candidates[$0]
        },
        dispositions: dispositions
    )
}

/// Reconciles two capture pathways only when they provide strong independent
/// evidence that they observed the same screen state. This runs before READ
/// novelty so OCR disagreements cannot masquerade as newly available content.
private func reconcileEquivalentSensorReads(
    candidates: [ReducerCandidate],
    writeBoundaries: [ReducerWriteBoundary],
    sessionID: String,
    sourceDirectory: URL
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
    ).sorted { left, right in
        let lhs = ordering(left)
        let rhs = ordering(right)
        if lhs.0 != rhs.0 { return lhs.0 < rhs.0 }
        if lhs.1 != rhs.1 { return lhs.1 < rhs.1 }
        return lhs.2 < rhs.2
    }
    var updated = candidates
    var excluded = Set<Int>()
    var dispositions = [ReducerDisposition]()
    var priorReadIndex: Int?
    var similarityCache = [String: Double]()

    func screenshotURL(_ candidate: ReducerCandidate) -> URL? {
        guard let relative = nonEmptyString(candidate.raw["screenshotRelativePath"])
        else { return nil }
        let url = sourceDirectory.appendingPathComponent(relative).standardizedFileURL
        guard url.path.hasPrefix(sourceDirectory.standardizedFileURL.path + "/"),
              FileManager.default.fileExists(atPath: url.path) else { return nil }
        return url
    }
    func visualSimilarity(
        _ left: ReducerCandidate,
        _ right: ReducerCandidate
    ) -> Double? {
        guard let leftHash = nonEmptyString(left.raw["screenshotSHA256"]),
              let rightHash = nonEmptyString(right.raw["screenshotSHA256"]),
              let leftURL = screenshotURL(left),
              let rightURL = screenshotURL(right) else { return nil }
        let key = [leftHash, rightHash].sorted().joined(separator: "|")
        if let cached = similarityCache[key] { return cached }
        func comparisonRegion(_ candidate: ReducerCandidate) -> CGRect? {
            guard let surface = candidate.raw["readSurface"] as? [String: Any],
                  let value = surface["comparisonRegionOfInterest"]
                    as? [String: Any],
                  let x = doubleValue(value["x"]),
                  let y = doubleValue(value["y"]),
                  let width = doubleValue(value["width"]),
                  let height = doubleValue(value["height"]),
                  width > 0, height > 0 else { return nil }
            return CGRect(x: x, y: y, width: width, height: height)
        }
        guard let similarity = try? normalizedReadImageSSIM(
            leftURL,
            rightURL,
            firstRegionOfInterest: comparisonRegion(left),
            secondRegionOfInterest: comparisonRegion(right)
        )
        else { return nil }
        similarityCache[key] = similarity
        return similarity
    }
    func observationID(_ candidate: ReducerCandidate) -> String {
        if let reduction = candidate.event["reduction"] as? [String: Any],
           let selected = nonEmptyString(reduction["selectedObservationID"]) {
            return selected
        }
        return stringValue(candidate.raw["recordID"]) ?? "unknown"
    }
    func provenance(_ candidate: ReducerCandidate) -> String {
        stringValue(candidate.event["provenance"]) ?? "unknown"
    }
    func semanticDifferenceCounts(
        _ left: String,
        _ right: String
    ) -> (forward: Int, backward: Int)? {
        guard let forward = adjacentCausalReadReflowDelta(
            previous: left, current: right
        ), let backward = adjacentCausalReadReflowDelta(
            previous: right, current: left
        ) else { return nil }
        func count(_ value: String) -> Int {
            value.reduce(0) { $0 + ($1.isLetter || $1.isNumber ? 1 : 0) }
        }
        return (count(forward.emittedContent), count(backward.emittedContent))
    }
    func pixelArea(_ candidate: ReducerCandidate) -> Int {
        (intValue(candidate.raw["screenshotPixelWidth"]) ?? 0)
            * (intValue(candidate.raw["screenshotPixelHeight"]) ?? 0)
    }

    for item in timeline {
        switch item {
        case .writeBoundary:
            priorReadIndex = nil
        case .candidate(let currentIndex):
            let current = updated[currentIndex]
            guard current.kind == "read" else {
                priorReadIndex = nil
                continue
            }
            guard let priorIndex = priorReadIndex else {
                priorReadIndex = currentIndex
                continue
            }
            let prior = updated[priorIndex]
            priorReadIndex = currentIndex
            guard provenance(prior) != provenance(current),
                  adjacentReadSurfaceCompatibility(prior.raw, current.raw) != nil,
                  let priorAt = reducerTimestamp(prior.overlapBoundaryAt),
                  let currentAt = reducerTimestamp(current.overlapBoundaryAt),
                  currentAt.timeIntervalSince(priorAt) >= 0,
                  currentAt.timeIntervalSince(priorAt) <= 1.5 else { continue }
            let bundle = stringValue(current.raw["bundleIdentifier"]) ?? ""
            let comparisonSource = prior.raw["_readComparisonContent"] is String
                && current.raw["_readComparisonContent"] is String
            let priorObserved = comparisonSource
                ? stringValue(prior.raw["_readComparisonContent"]) ?? ""
                : stringValue(prior.raw["content"]) ?? ""
            let currentObserved = comparisonSource
                ? stringValue(current.raw["_readComparisonContent"]) ?? ""
                : stringValue(current.raw["content"]) ?? ""
            let priorText = readContentRemovingKnownInterfaceLines(
                bundleIdentifier: bundle,
                content: priorObserved
            )
            let currentText = readContentRemovingKnownInterfaceLines(
                bundleIdentifier: bundle,
                content: currentObserved
            )
            let textSimilarity = normalizedReadOCRSimilarity(
                priorText, currentText, minimum: 0.85
            )
            let lengthDifference = Double(abs(priorText.count - currentText.count))
                / Double(max(1, max(priorText.count, currentText.count)))
            let differences = semanticDifferenceCounts(priorText, currentText)
            guard textSimilarity >= 0.85,
                  lengthDifference <= 0.10,
                  let imageSimilarity = visualSimilarity(prior, current),
                  imageSimilarity >= 0.80 else { continue }

            // If the OCR disagreement could reflect bounded passive progress,
            // retain the later state. For an ordinary duplicate, prefer the
            // denser capture while preserving both raw observations.
            let containsPossibleProgress = differences.map {
                $0.forward > 24 || $0.backward > 24
            } ?? true
            let keepCurrent = containsPossibleProgress
                || pixelArea(current) >= pixelArea(prior)
            let keptIndex = keepCurrent ? currentIndex : priorIndex
            let removedIndex = keepCurrent ? priorIndex : currentIndex
            let kept = updated[keptIndex]
            let removed = updated[removedIndex]
            let unstableClippedLines = containsPossibleProgress
                ? []
                : pairedUnstableClippedOCRLineEvidence(
                    selected: kept.raw, peer: removed.raw
                )
            var details: [String: Any] = [
                "policy": containsPossibleProgress
                    ? "cross_sensor_bounded_progress_final_state_v1"
                    : "cross_sensor_same_state_v1",
                "memberObservationIDs": [observationID(prior), observationID(current)],
                "memberProvenances": [provenance(prior), provenance(current)],
                "keptObservationID": observationID(kept),
                "imageSSIM": imageSimilarity,
                "normalizedOCRSimilarity": textSimilarity,
                "ocrComparisonProjection": comparisonSource
                    ? "interior_20_percent_edge_inset"
                    : "authoritative_surface",
                "contentAuthority": "selected_observation_full_ax_pane",
                "maximumIntervalSeconds": 1.5,
                "firstCapturedAt": prior.overlapBoundaryAt,
                "lastCapturedAt": current.overlapBoundaryAt,
            ]
            if !unstableClippedLines.isEmpty {
                details["unstableClippedLineEvidence"] = unstableClippedLines
            }
            if let differences {
                details["semanticDifferenceAlphanumeric"] = [
                    "forward": differences.forward,
                    "backward": differences.backward,
                ]
            }
            excluded.insert(removedIndex)
            dispositions.append(ReducerDisposition(
                line: removed.rawLine,
                object: reducerUnresolved(
                    sessionID: sessionID,
                    raw: removed.raw,
                    line: removed.rawLine,
                    kind: "read",
                    rule: "read_observation_reconciliation_v1",
                    reason: "superseded_equivalent_sensor_observation",
                    details: details,
                    sourceRecordIDs: removed.event["sourceRecordIDs"] as? [String]
                )
            ))
            var merged = keepCurrent ? current : prior
            let lineage = ((prior.event["sourceRecordIDs"] as? [String]) ?? [])
                + ((current.event["sourceRecordIDs"] as? [String]) ?? [])
            let uniqueLineage = lineage.reduce(into: [String]()) { values, value in
                if !values.contains(value) { values.append(value) }
            }
            merged.event["sourceRecordIDs"] = uniqueLineage
            merged.event["eventID"] = stableEventID(
                sessionID: sessionID, lineage: uniqueLineage, ordinal: 0
            )
            var reduction = merged.event["reduction"] as? [String: Any] ?? [:]
            reduction["rawLineage"] = uniqueLineage
            reduction["observationReconciliation"] = details
            if !unstableClippedLines.isEmpty {
                reduction["unstableClippedOCRLineEvidence"] = unstableClippedLines
            }
            merged.event["reduction"] = reduction
            updated[keptIndex] = merged
            priorReadIndex = keptIndex
        }
    }
    return ReducerOverlapResult(
        events: updated.indices.compactMap {
            excluded.contains($0) ? nil : updated[$0]
        },
        dispositions: dispositions
    )
}

/// A dynamic surface can expose several progressively newer OCR snapshots for
/// one autonomous update, such as a streaming assistant response. Those raw
/// observations are valuable evidence, but treating every partial state as an
/// independent READ overweights one response and evicts older causal history.
///
/// This rule is deliberately narrower than viewport overlap. It applies only
/// for consecutive visual-frame observations on the same captured surface and
/// compatible pane, and crosses no user-triggered READ or WRITE onset. The
/// final snapshot is therefore the state available at the end of the chain;
/// every intermediate frame remains available in the raw evidence.
private func consolidateDynamicVisualReads(
    candidates: [ReducerCandidate],
    writeBoundaries: [ReducerWriteBoundary],
    attentionBoundaries: [ReducerDynamicReadBoundary] = [],
    sessionID: String,
    includeInterleavedObservations: Bool = false
) -> ReducerOverlapResult {
    enum TimelineItem {
        case candidate(Int)
        case writeBoundary(ReducerWriteBoundary)
        case attentionBoundary(ReducerDynamicReadBoundary, Bool)
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
        case .attentionBoundary(let boundary, let isEnd):
            return (
                isEnd ? boundary.endedAt : boundary.beganAt,
                0,
                boundary.rawLine
            )
        }
    }
    let timeline = (
        candidates.indices.map(TimelineItem.candidate)
            + writeBoundaries.map(TimelineItem.writeBoundary)
            + attentionBoundaries.flatMap { boundary in
                [
                    TimelineItem.attentionBoundary(boundary, false),
                    TimelineItem.attentionBoundary(boundary, true),
                ]
            }
    ).sorted { lhs, rhs in
        let left = ordering(lhs)
        let right = ordering(rhs)
        if left.0 != right.0 { return left.0 < right.0 }
        if left.1 != right.1 { return left.1 < right.1 }
        return left.2 < right.2
    }

    var updated = candidates
    var excluded = Set<Int>()
    var dispositions = [ReducerDisposition]()
    var chain = [Int]()
    let materialBoundaryRecordIDs = Set(
        attentionBoundaries.compactMap(\.sourceRecordID)
    )

    func occursInsideMaterialActivity(_ candidate: ReducerCandidate) -> Bool {
        guard let candidateAt = reducerTimestamp(candidate.overlapBoundaryAt)
        else { return true }
        return attentionBoundaries.contains { boundary in
            guard let beganAt = reducerTimestamp(boundary.beganAt),
                  let endedAt = reducerTimestamp(boundary.endedAt) else {
                return true
            }
            return candidateAt >= beganAt && candidateAt <= endedAt
        }
    }

    func hasMaterialBoundaryLineage(_ candidate: ReducerCandidate) -> Bool {
        let lineage = candidate.event["sourceRecordIDs"] as? [String] ?? []
        return lineage.contains { materialBoundaryRecordIDs.contains($0) }
    }

    func observationID(_ candidate: ReducerCandidate) -> String {
        if let reduction = candidate.event["reduction"] as? [String: Any],
           let selected = nonEmptyString(reduction["selectedObservationID"]) {
            return selected
        }
        return stringValue(candidate.raw["recordID"]) ?? "unknown"
    }
    func flushChain() {
        defer {
            chain.removeAll(keepingCapacity: true)
        }
        guard chain.count > 1,
              let keptIndex = chain.last else { return }
        let kept = updated[keptIndex]
        let memberIDs = chain.map { observationID(updated[$0]) }
        let details: [String: Any] = [
            "policy": includeInterleavedObservations
                ? "bounded_dynamic_surface_final_state_v2"
                : "uninterrupted_progressive_dynamic_surface_last_viewport_v1",
            "memberCount": chain.count,
            "memberObservationIDs": memberIDs,
            "firstCapturedAt": updated[chain[0]].overlapBoundaryAt,
            "lastCapturedAt": kept.overlapBoundaryAt,
            "keptObservationID": observationID(kept),
            "boundaryPolicy": "raw_material_interaction_intervals_v1",
        ]
        for index in chain.dropLast() {
            excluded.insert(index)
            let candidate = updated[index]
            dispositions.append(ReducerDisposition(
                line: candidate.rawLine,
                object: reducerUnresolved(
                    sessionID: sessionID,
                    raw: candidate.raw,
                    line: candidate.rawLine,
                    kind: "read",
                    rule: "dynamic_visual_state_consolidation_v1",
                    reason: "superseded_dynamic_surface_state",
                    details: details,
                    sourceRecordIDs: candidate.event["sourceRecordIDs"] as? [String]
                )
            ))
        }
        var keptCandidate = kept
        var reduction = keptCandidate.event["reduction"] as? [String: Any] ?? [:]
        reduction["dynamicVisualConsolidation"] = details
        if includeInterleavedObservations {
            let lineage = chain.flatMap {
                updated[$0].event["sourceRecordIDs"] as? [String] ?? []
            }.reduce(into: [String]()) { values, value in
                if !values.contains(value) { values.append(value) }
            }
            keptCandidate.event["sourceRecordIDs"] = lineage
            keptCandidate.event["eventID"] = stableEventID(
                sessionID: sessionID, lineage: lineage, ordinal: 0
            )
            reduction["rawLineage"] = lineage
        }
        keptCandidate.event["reduction"] = reduction
        updated[keptIndex] = keptCandidate
    }

    for item in timeline {
        switch item {
        case .writeBoundary, .attentionBoundary:
            flushChain()
        case .candidate(let index):
            let candidate = updated[index]
            guard candidate.kind == "read" else {
                flushChain()
                continue
            }
            let isChainCandidate = isDynamicVisualChainCandidate(
                candidate,
                includeReconciledObservations: includeInterleavedObservations
            )
                && !hasMaterialBoundaryLineage(candidate)
                && !occursInsideMaterialActivity(candidate)
            guard let priorIndex = chain.last else {
                if isChainCandidate { chain = [index] }
                continue
            }
            let compatible = dynamicVisualStatesAreCompatible(
                updated[priorIndex], candidate
            )
            if compatible && isChainCandidate {
                chain.append(index)
            } else {
                flushChain()
                if isChainCandidate { chain = [index] }
            }
        }
    }
    flushChain()
    return ReducerOverlapResult(
        events: updated.indices.compactMap {
            excluded.contains($0) ? nil : updated[$0]
        },
        dispositions: dispositions
    )
}

private func isVisualReadCandidate(_ candidate: ReducerCandidate) -> Bool {
    stringValue(candidate.event["provenance"]) == "visual_change_screen_ocr"
}

private func isDynamicVisualChainCandidate(
    _ candidate: ReducerCandidate,
    includeReconciledObservations: Bool
) -> Bool {
    if isVisualReadCandidate(candidate) { return true }
    guard includeReconciledObservations,
          let reduction = candidate.event["reduction"] as? [String: Any],
          let reconciliation = reduction["observationReconciliation"]
            as? [String: Any] else { return false }
    return stringArray(reconciliation["memberProvenances"])
        .contains("visual_change_screen_ocr")
}

private func dynamicVisualStatesAreCompatible(
    _ left: ReducerCandidate,
    _ right: ReducerCandidate
) -> Bool {
    guard left.kind == "read", right.kind == "read" else { return false }
    for key in [
        "processIdentifier", "windowID", "displayID", "bundleIdentifier",
        "windowTitle",
    ] where reducerComparableString(left.raw[key])
        != reducerComparableString(right.raw[key]) {
        return false
    }

    let samePane = readOverlapPaneIdentity(left.raw)
        == readOverlapPaneIdentity(right.raw)
    let paneOverlap = reducerRectangleOverlapOverSmallerArea(
        left.raw["captureBounds"] as? [String: Any],
        right.raw["captureBounds"] as? [String: Any]
    )
    return samePane || paneOverlap >= 0.70
}

private func reducerComparableString(_ value: Any?) -> String {
    if let value = value as? String { return value }
    if let value = intValue(value) { return String(value) }
    return ""
}

private func reducerRectangleOverlapOverSmallerArea(
    _ left: [String: Any]?,
    _ right: [String: Any]?
) -> Double {
    guard let left, let right,
          let leftX = doubleValue(left["x"]),
          let leftY = doubleValue(left["y"]),
          let leftWidth = doubleValue(left["width"]),
          let leftHeight = doubleValue(left["height"]),
          let rightX = doubleValue(right["x"]),
          let rightY = doubleValue(right["y"]),
          let rightWidth = doubleValue(right["width"]),
          let rightHeight = doubleValue(right["height"]),
          leftWidth > 0, leftHeight > 0, rightWidth > 0, rightHeight > 0
    else { return 0 }
    let intersectionWidth = max(
        0, min(leftX + leftWidth, rightX + rightWidth) - max(leftX, rightX)
    )
    let intersectionHeight = max(
        0, min(leftY + leftHeight, rightY + rightHeight) - max(leftY, rightY)
    )
    let smallerArea = min(leftWidth * leftHeight, rightWidth * rightHeight)
    return smallerArea > 0
        ? (intersectionWidth * intersectionHeight) / smallerArea
        : 0
}

private func semanticReadNoveltyRuleVersion(
    _ mode: ReducerReadOverlapMode
) -> String {
    switch mode {
    case .semanticV20:
        return "adjacent-causal-read-reflow-overlap-v5"
    case .semanticV19:
        return "adjacent-causal-read-reflow-overlap-v4"
    case .semanticV18:
        return "adjacent-causal-read-reflow-overlap-v3"
    case .semanticV17:
        return "adjacent-causal-read-line-overlap-v2"
    default:
        return "adjacent-causal-read-edge-overlap-v1"
    }
}

private func semanticReadUnprovenReason(
    _ mode: ReducerReadOverlapMode,
    fullDeltaAvailable: Bool
) -> String {
    switch mode {
    case .semanticV18, .semanticV19, .semanticV20:
        return fullDeltaAvailable
            ? "comparison_interior_alignment_unproven"
            : "conservative_reflow_alignment_unproven"
    case .semanticV17:
        return "conservative_line_alignment_unproven"
    default:
        return "exact_edge_alignment_unproven"
    }
}

private func semanticReadOverlapReason(_ alignment: String) -> String {
    switch alignment {
    case "ocr_tolerant_ordered_tokens":
        return "ocr_tolerant_reflow_overlap"
    case "ocr_tolerant_ordered_lines":
        return "ocr_tolerant_ordered_line_overlap"
    default:
        return "exact_contiguous_edge_overlap"
    }
}

private func semanticReadComparisonDelta(
    previous: ReducerCandidate,
    current: ReducerCandidate
) -> AdjacentCausalReadDelta? {
    guard let prior = previous.comparisonContent, !prior.isEmpty,
          let next = current.comparisonContent, !next.isEmpty else { return nil }
    return adjacentCausalReadReflowDelta(previous: prior, current: next)
}

private func normalizedReadFragment(_ value: String) -> String {
    value.precomposedStringWithCanonicalMapping
        .split(whereSeparator: \Character.isWhitespace)
        .joined(separator: " ")
        .lowercased()
}

private func readNoveltySupportingLines(
    novelty: String,
    lines: [ReadOCRLineEvidence]
) -> [ReadOCRLineEvidence] {
    let fragment = normalizedReadFragment(novelty)
    guard !fragment.isEmpty else { return [] }
    return lines.filter { line in
        let content = normalizedReadFragment(line.text)
        return content == fragment
            || " \(content) ".contains(" \(fragment) ")
    }
}

private func readComparisonVerticalBand(
    _ raw: [String: Any]
) -> ClosedRange<Double>? {
    guard let surface = raw["readSurface"] as? [String: Any],
          let full = surface["regionOfInterest"] as? [String: Any],
          let comparison = surface["comparisonRegionOfInterest"]
            as? [String: Any],
          let fullY = doubleValue(full["y"]),
          let fullHeight = doubleValue(full["height"]), fullHeight > 0,
          let comparisonY = doubleValue(comparison["y"]),
          let comparisonHeight = doubleValue(comparison["height"])
    else { return nil }
    // Region y-coordinates use a top origin while Vision line coordinates use
    // a bottom origin. Convert the crop's two insets into Vision coordinates.
    let topInset = (comparisonY - fullY) / fullHeight
    let bottomInset = (
        fullY + fullHeight - comparisonY - comparisonHeight
    ) / fullHeight
    let lower = max(0, bottomInset)
    let upper = min(1, 1 - topInset)
    guard lower <= upper else { return nil }
    return lower...upper
}

/// The full-pane and interior OCR passes independently observe the same
/// pixels. When the complete projection claims novelty inside the interior but
/// the interior pass proves no change, the claimed novelty is OCR disagreement,
/// not new information. A second narrow case handles tiny peripheral UI glyphs
/// when both projections report only low-information disagreement.
private func readProjectionDisagreementEvidence(
    current: ReducerCandidate,
    fullDelta: AdjacentCausalReadDelta,
    comparisonDelta: AdjacentCausalReadDelta,
    lines: [ReadOCRLineEvidence]
) -> [String: Any]? {
    guard !fullDelta.emittedContent.isEmpty else { return nil }
    let supportingLines = readNoveltySupportingLines(
        novelty: fullDelta.emittedContent, lines: lines
    )
    guard !supportingLines.isEmpty else { return nil }
    let comparisonAlphanumericCount = comparisonDelta.emittedContent.reduce(0) {
        $0 + ($1.isLetter || $1.isNumber ? 1 : 0)
    }
    let fullAlphanumericCount = fullDelta.emittedContent.reduce(0) {
        $0 + ($1.isLetter || $1.isNumber ? 1 : 0)
    }
    let band = readComparisonVerticalBand(current.raw)
    let allInsideComparison = band.map { band in
        supportingLines.allSatisfy { line in
            guard let y = line.y, let height = line.height else { return false }
            return y >= band.lowerBound - 0.005
                && y + height <= band.upperBound + 0.005
        }
    } ?? false
    let allPeripheral = supportingLines.allSatisfy { line in
        guard let y = line.y, let height = line.height else { return false }
        let center = y + height / 2
        return center <= 0.20 || center >= 0.80
    }
    let interiorNoChange = comparisonDelta.emittedContent.isEmpty
        && allInsideComparison
    let peripheralLowInformation = fullAlphanumericCount <= 2
        && comparisonAlphanumericCount <= 1
        && allPeripheral
    guard interiorNoChange || peripheralLowInformation else { return nil }
    return [
        "reason": interiorNoChange
            ? "interior_ocr_projection_reports_no_change"
            : "peripheral_low_information_ocr_disagreement",
        "fullNovelContent": fullDelta.emittedContent,
        "fullNovelContentSHA256": reducerSHA256String(fullDelta.emittedContent),
        "comparisonNovelContent": comparisonDelta.emittedContent,
        "comparisonNovelContentSHA256": reducerSHA256String(
            comparisonDelta.emittedContent
        ),
        "supportingLineIndices": supportingLines.map(\.index),
        "supportingLineCount": supportingLines.count,
        "fullAlphanumericCount": fullAlphanumericCount,
        "comparisonAlphanumericCount": comparisonAlphanumericCount,
    ]
}

private func semanticReadComparisonAudit(
    previous: ReducerCandidate,
    current: ReducerCandidate,
    delta: AdjacentCausalReadDelta?
) -> [String: Any] {
    guard let prior = previous.comparisonContent, !prior.isEmpty,
          let next = current.comparisonContent, !next.isEmpty else {
        return [
            "role": "comparison_only",
            "decision": "unavailable",
            "affectsAuthoritativeContent": false,
        ]
    }
    var result: [String: Any] = [
        "role": "comparison_only",
        "decision": delta == nil ? "alignment_unproven" : "alignment_proven",
        "affectsAuthoritativeContent": false,
        "previousContentSHA256": reducerSHA256String(prior),
        "currentContentSHA256": reducerSHA256String(next),
    ]
    if let delta {
        result["alignment"] = delta.alignment
        result["overlapCharacterCount"] = delta.overlapCharacterCount
        result["currentCharacterCount"] = delta.currentCharacterCount
        result["novelCharacterCount"] = delta.emittedContent.count
    }
    return result
}

private func addSemanticReadComparisonAudit(
    to novelty: inout [String: Any],
    mode: ReducerReadOverlapMode,
    previous: ReducerCandidate?,
    current: ReducerCandidate,
    delta: AdjacentCausalReadDelta?
) {
    guard mode == .semanticV18 || mode == .semanticV19
        || mode == .semanticV20 else { return }
    guard let previous else {
        novelty["comparisonProjection"] = [
            "role": "comparison_only",
            "decision": "no_causal_predecessor",
            "affectsAuthoritativeContent": false,
        ]
        return
    }
    novelty["comparisonProjection"] = semanticReadComparisonAudit(
        previous: previous, current: current, delta: delta
    )
}

/// Overlap is an interpretation of the semantic event timeline, not the order
/// in which asynchronous OCR and delayed WRITE persistence happened to append.
/// A finalized WRITE begins a new reading epoch at beganAt even though it only
/// becomes causally available later at terminalDecisionAt.
private func applySemanticReadOverlap(
    candidates: [ReducerCandidate],
    writeBoundaries: [ReducerWriteBoundary],
    sessionID: String,
    paneAwareSurfaceIdentity: Bool,
    mode: ReducerReadOverlapMode
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
    var scaffoldingTracker = ReadInterfaceScaffoldingTracker()
    var comparisonScaffoldingTracker = ReadInterfaceScaffoldingTracker()
    let usesSemanticReadProjection = mode == .semanticV16
        || mode == .semanticV17 || mode == .semanticV18
        || mode == .semanticV19 || mode == .semanticV20
    if usesSemanticReadProjection {
        // Classify recurring interface chrome from the immutable session as a
        // whole, then apply that classification to every READ. This can only
        // remove proven UI text; it never introduces future semantic content.
        for item in timeline {
            guard case .candidate(let index) = item,
                  candidates[index].kind == "read" else { continue }
            let raw = candidates[index].raw
            scaffoldingTracker.observe(
                surfaceKey: reducerReadScaffoldingSurfaceKey(raw),
                bundleIdentifier: stringValue(raw["bundleIdentifier"]) ?? "",
                windowTitle: stringValue(raw["windowTitle"]) ?? "",
                lines: reducerReadOCRLines(raw["_readSurfaceLines"])
            )
            if mode == .semanticV18 || mode == .semanticV19
                || mode == .semanticV20 {
                comparisonScaffoldingTracker.observe(
                    surfaceKey: reducerReadScaffoldingSurfaceKey(raw)
                        + "|comparison-interior",
                    bundleIdentifier: stringValue(raw["bundleIdentifier"]) ?? "",
                    windowTitle: stringValue(raw["windowTitle"]) ?? "",
                    lines: reducerReadOCRLines(raw["_readComparisonLines"])
                )
            }
        }
    }
    var previousCausalRead: ReducerCandidate?
    var accepted = [ReducerCandidate]()
    var dispositions = [ReducerDisposition]()
    for item in timeline {
        guard case .candidate(let index) = item else {
            deduplicator.reset()
            previousCausalRead = nil
            continue
        }
        var candidate = candidates[index]
        if candidate.kind == "write" {
            deduplicator.reset()
            previousCausalRead = nil
            accepted.append(candidate)
            continue
        }
        if usesSemanticReadProjection {
            let observedContent = stringValue(candidate.raw["content"]) ?? ""
            let lines = reducerReadOCRLines(candidate.raw["_readSurfaceLines"])
            let bundleIdentifier = stringValue(candidate.raw["bundleIdentifier"]) ?? ""
            let windowTitle = stringValue(candidate.raw["windowTitle"]) ?? ""
            let projection = scaffoldingTracker.project(
                surfaceKey: reducerReadScaffoldingSurfaceKey(candidate.raw),
                bundleIdentifier: bundleIdentifier,
                windowTitle: windowTitle,
                observedContent: observedContent,
                lines: lines,
                allowJoinedLineScaffolding: mode == .semanticV18
                    || mode == .semanticV19 || mode == .semanticV20,
                removeClippedOuterBoundaryLines: mode == .semanticV19
                    || mode == .semanticV20,
                additionalRemovalReasons: mode == .semanticV20
                    ? reducerAdditionalReadRemovalReasons(
                        candidate.event, requireStrongClippingEvidence: true
                    )
                    : mode == .semanticV19
                    ? reducerAdditionalReadRemovalReasons(candidate.event)
                    : [:]
            )
            candidate.event["content"] = projection.content
            if (mode == .semanticV18 || mode == .semanticV19
                    || mode == .semanticV20),
               let comparisonObserved = candidate.raw["_readComparisonContent"]
                    as? String {
                let comparisonLines = reducerReadOCRLines(
                    candidate.raw["_readComparisonLines"]
                )
                let comparisonProjection = comparisonScaffoldingTracker.project(
                    surfaceKey: reducerReadScaffoldingSurfaceKey(candidate.raw)
                        + "|comparison-interior",
                    bundleIdentifier: bundleIdentifier,
                    windowTitle: windowTitle,
                    observedContent: comparisonObserved,
                    lines: comparisonLines,
                    allowJoinedLineScaffolding: true,
                    removeClippedOuterBoundaryLines: mode == .semanticV19
                        || mode == .semanticV20
                )
                candidate.comparisonContent = comparisonProjection.content
                if var reduction = candidate.event["reduction"] as? [String: Any] {
                    reduction["semanticReadComparison"] = reducerSemanticReadDetails(
                        observedContent: comparisonObserved,
                        projection: comparisonProjection,
                        hadLineEvidence: !comparisonLines.isEmpty,
                        ruleVersion: mode == .semanticV20
                            ? "read-interface-scaffolding-v5"
                            : mode == .semanticV19
                            ? "read-interface-scaffolding-v4"
                            : "read-interface-scaffolding-v3"
                    )
                    candidate.event["reduction"] = reduction
                }
            }
            let semanticDetails = reducerSemanticReadDetails(
                observedContent: observedContent,
                projection: projection,
                hadLineEvidence: !lines.isEmpty,
                ruleVersion: mode == .semanticV20
                    ? "read-interface-scaffolding-v5"
                    : mode == .semanticV19
                    ? "read-interface-scaffolding-v4"
                    : "read-interface-scaffolding-v3"
            )
            if var reduction = candidate.event["reduction"] as? [String: Any] {
                reduction["semanticReadContent"] = semanticDetails
                candidate.event["reduction"] = reduction
            }

            let completeCurrent = candidate
            let currentEventID = stringValue(candidate.event["eventID"]) ?? ""
            guard let previous = previousCausalRead,
                  let surface = adjacentReadSurfaceCompatibility(
                    previous.raw,
                    candidate.raw
                  ) else {
                var novelty: [String: Any] = [
                    "schemaVersion": 1,
                    "ruleVersion": semanticReadNoveltyRuleVersion(mode),
                    "decision": "full_state",
                    "reason": previousCausalRead == nil
                        ? "no_causal_predecessor"
                        : "surface_changed",
                    "content": projection.content,
                    "currentEventID": currentEventID,
                    "orderingField": "capturedAt",
                    "orderingTimestamp": candidate.overlapBoundaryAt,
                ]
                addSemanticReadComparisonAudit(
                    to: &novelty, mode: mode,
                    previous: previousCausalRead, current: candidate,
                    delta: nil
                )
                candidate.event["readNovelty"] = novelty
                accepted.append(candidate)
                previousCausalRead = completeCurrent
                continue
            }
            let previousContent = stringValue(previous.event["content"]) ?? ""
            let priorEventID = stringValue(previous.event["eventID"]) ?? ""
            let delta = mode == .semanticV18 || mode == .semanticV19
                    || mode == .semanticV20
                ? adjacentCausalReadReflowDelta(
                    previous: previousContent,
                    current: projection.content
                )
                : mode == .semanticV17
                ? adjacentCausalReadTolerantLineDelta(
                    previous: previousContent,
                    current: projection.content
                )
                : adjacentCausalReadEdgeDelta(
                    previous: previousContent,
                    current: projection.content
                )
            let comparisonDelta = mode == .semanticV18 || mode == .semanticV19
                    || mode == .semanticV20
                ? semanticReadComparisonDelta(previous: previous, current: candidate)
                : nil
            guard let delta,
                  mode != .semanticV18 && mode != .semanticV19
                    && mode != .semanticV20
                    || delta.alignment != "ocr_tolerant_ordered_tokens"
                    || comparisonDelta != nil else {
                var novelty: [String: Any] = [
                    "schemaVersion": 1,
                    "ruleVersion": semanticReadNoveltyRuleVersion(mode),
                    "decision": "retain_full_uncertain",
                    "reason": semanticReadUnprovenReason(
                        mode, fullDeltaAvailable: delta != nil
                    ),
                    "content": projection.content,
                    "comparedEventID": priorEventID,
                    "currentEventID": currentEventID,
                    "orderingField": "capturedAt",
                    "orderingTimestamp": candidate.overlapBoundaryAt,
                    "surface": surface,
                ]
                addSemanticReadComparisonAudit(
                    to: &novelty, mode: mode,
                    previous: previous, current: candidate,
                    delta: comparisonDelta
                )
                candidate.event["readNovelty"] = novelty
                accepted.append(candidate)
                previousCausalRead = completeCurrent
                continue
            }
            let removedLineIndices = Set(projection.removed.map(\.line.index))
            let removedClippedLineCount = projection.removed.filter {
                $0.reason == "geometrically_clipped_outer_boundary_line"
                    || $0.reason == "cross_sensor_unstable_clipped_boundary_line"
                    || $0.reason
                        == "cross_sensor_clipped_boundary_neighbor_disagreement"
            }.count
            let removedKnownBoundaryInterfaceLineCount = projection.removed.filter {
                $0.reason == "codex_interface_timestamp"
            }.count
            if (mode == .semanticV19 || mode == .semanticV20),
               removedClippedLineCount + removedKnownBoundaryInterfaceLineCount > 0,
               let comparisonDelta {
                let comparisonNovelty = comparisonDelta.emittedContent
                let comparisonNoveltyGrounded = comparisonNovelty.isEmpty
                    || readNoveltyIsStrictlyGrounded(
                        comparisonNovelty, in: projection.content
                    )
                if mode == .semanticV20,
                   !comparisonNovelty.isEmpty,
                   !comparisonNoveltyGrounded,
                   removedClippedLineCount > 0 {
                    var novelty: [String: Any] = [
                        "schemaVersion": 1,
                        "ruleVersion": semanticReadNoveltyRuleVersion(mode),
                        "decision": "retain_full_uncertain",
                        "reason": "comparison_novelty_not_grounded_in_authoritative_full_state",
                        "content": projection.content,
                        "comparedEventID": priorEventID,
                        "currentEventID": currentEventID,
                        "observedFullProjectionNovelContent": delta.emittedContent,
                        "ungroundedComparisonNovelContent": comparisonNovelty,
                        "ungroundedComparisonNovelContentSHA256": reducerSHA256String(
                            comparisonNovelty
                        ),
                        "clippedLineCount": removedClippedLineCount,
                        "orderingField": "capturedAt",
                        "orderingTimestamp": candidate.overlapBoundaryAt,
                        "surface": surface,
                        "noveltyAuthority": "complete_cleaned_authoritative_full_state",
                    ]
                    addSemanticReadComparisonAudit(
                        to: &novelty, mode: mode,
                        previous: previous, current: candidate,
                        delta: comparisonDelta
                    )
                    candidate.event["readNovelty"] = novelty
                    accepted.append(candidate)
                    previousCausalRead = completeCurrent
                    continue
                }
                if mode != .semanticV20
                    || comparisonNovelty.isEmpty
                    || comparisonNoveltyGrounded {
                    var novelty: [String: Any] = [
                        "schemaVersion": 1,
                        "ruleVersion": semanticReadNoveltyRuleVersion(mode),
                        "decision": comparisonDelta.emittedContent.isEmpty
                            ? "suppress_clipped_boundary_no_stable_novelty"
                            : "emit_stable_interior_after_clipped_boundary",
                        "reason": removedClippedLineCount > 0
                            ? "clipped_full_projection_uses_independent_interior_ocr"
                            : "known_boundary_interface_removal_uses_independent_interior_ocr",
                        "content": comparisonDelta.emittedContent,
                        "dependsOnEventID": priorEventID,
                        "currentEventID": currentEventID,
                        "observedFullProjectionNovelContent": delta.emittedContent,
                        "clippedLineCount": removedClippedLineCount,
                        "knownBoundaryInterfaceLineCount":
                            removedKnownBoundaryInterfaceLineCount,
                        "alignment": comparisonDelta.alignment,
                        "overlapCharacterCount": comparisonDelta.overlapCharacterCount,
                        "currentCharacterCount": comparisonDelta.currentCharacterCount,
                        "novelCharacterCount": comparisonDelta.emittedContent.count,
                        "orderingField": "capturedAt",
                        "orderingTimestamp": candidate.overlapBoundaryAt,
                        "surface": surface,
                        "noveltyAuthority": "independent_interior_ocr_projection",
                        "comparisonNoveltyGroundedInAuthoritativeFullState":
                            comparisonNoveltyGrounded,
                    ]
                    addSemanticReadComparisonAudit(
                        to: &novelty, mode: mode,
                        previous: previous, current: candidate,
                        delta: comparisonDelta
                    )
                    candidate.event["readNovelty"] = novelty
                    accepted.append(candidate)
                    previousCausalRead = completeCurrent
                    continue
                }
            }
            if (mode == .semanticV19 || mode == .semanticV20),
               let comparisonDelta,
               let disagreement = readProjectionDisagreementEvidence(
                    current: candidate,
                    fullDelta: delta,
                    comparisonDelta: comparisonDelta,
                    lines: lines
               ) {
                var novelty: [String: Any] = [
                    "schemaVersion": 1,
                    "ruleVersion": semanticReadNoveltyRuleVersion(mode),
                    "decision": "suppress_cross_projection_ocr_disagreement",
                    "reason": disagreement["reason"]!,
                    "content": "",
                    "dependsOnEventID": priorEventID,
                    "currentEventID": currentEventID,
                    "observedNovelContent": delta.emittedContent,
                    "projectionDisagreementEvidence": disagreement,
                    "alignment": delta.alignment,
                    "overlapCharacterCount": delta.overlapCharacterCount,
                    "currentCharacterCount": delta.currentCharacterCount,
                    "novelCharacterCount": delta.emittedContent.count,
                    "orderingField": "capturedAt",
                    "orderingTimestamp": candidate.overlapBoundaryAt,
                    "surface": surface,
                ]
                addSemanticReadComparisonAudit(
                    to: &novelty, mode: mode,
                    previous: previous, current: candidate,
                    delta: comparisonDelta
                )
                candidate.event["readNovelty"] = novelty
                accepted.append(candidate)
                previousCausalRead = completeCurrent
                continue
            }
            let microglyphs: [ReadOCRLineEvidence]?
            if mode == .semanticV18 || mode == .semanticV19
                || mode == .semanticV20 {
                microglyphs = isolatedReadNoveltyMicroglyphs(
                    delta.emittedContent,
                    lines: lines,
                    excludingLineIndices: removedLineIndices
                )
            } else if let one = isolatedReadNoveltyMicroglyph(
                delta.emittedContent,
                lines: lines,
                excludingLineIndices: removedLineIndices
            ) {
                microglyphs = [one]
            } else {
                microglyphs = nil
            }
            if !delta.emittedContent.isEmpty, let microglyphs {
                let microglyphEvidence = microglyphs.map { microglyph -> [String: Any] in
                    var evidence: [String: Any] = [
                        "lineIndex": microglyph.index,
                        "text": microglyph.text,
                        "textSHA256": reducerSHA256String(microglyph.text),
                        "confidence": microglyph.confidence,
                    ]
                    if let x = microglyph.x, let y = microglyph.y,
                       let width = microglyph.width, let height = microglyph.height {
                        evidence["boundingBox"] = [
                            "x": x, "y": y, "width": width, "height": height,
                        ]
                    }
                    return evidence
                }
                var novelty: [String: Any] = [
                    "schemaVersion": 1,
                    "ruleVersion": semanticReadNoveltyRuleVersion(mode),
                    "decision": "suppress_nonsemantic_microglyph",
                    "reason": "isolated_tiny_ocr_glyph",
                    "content": "",
                    "dependsOnEventID": priorEventID,
                    "currentEventID": currentEventID,
                    "observedNovelContent": delta.emittedContent,
                    "microglyphEvidence": microglyphEvidence,
                    "alignment": delta.alignment,
                    "overlapCharacterCount": delta.overlapCharacterCount,
                    "currentCharacterCount": delta.currentCharacterCount,
                    "novelCharacterCount": delta.emittedContent.count,
                    "orderingField": "capturedAt",
                    "orderingTimestamp": candidate.overlapBoundaryAt,
                    "surface": surface,
                ]
                addSemanticReadComparisonAudit(
                    to: &novelty, mode: mode,
                    previous: previous, current: candidate,
                    delta: comparisonDelta
                )
                candidate.event["readNovelty"] = novelty
                accepted.append(candidate)
                previousCausalRead = completeCurrent
                continue
            }
            var novelty: [String: Any] = [
                "schemaVersion": 1,
                "ruleVersion": semanticReadNoveltyRuleVersion(mode),
                "decision": delta.emittedContent.isEmpty
                    ? "suppress_no_new_content"
                    : "emit_new_content",
                "reason": delta.emittedContent.isEmpty
                    ? "complete_semantic_state_repeated"
                    : semanticReadOverlapReason(delta.alignment),
                "content": delta.emittedContent,
                "dependsOnEventID": priorEventID,
                "currentEventID": currentEventID,
                "alignment": delta.alignment,
                "overlapCharacterCount": delta.overlapCharacterCount,
                "currentCharacterCount": delta.currentCharacterCount,
                "novelCharacterCount": delta.emittedContent.count,
                "orderingField": "capturedAt",
                "orderingTimestamp": candidate.overlapBoundaryAt,
                "surface": surface,
            ]
            addSemanticReadComparisonAudit(
                to: &novelty, mode: mode,
                previous: previous, current: candidate,
                delta: comparisonDelta
            )
            if !delta.lineMatches.isEmpty {
                novelty["lineAlignment"] = [
                    "matchedLineCount": delta.lineMatches.count,
                    "exactLineCount": delta.lineMatches.filter {
                        $0.previousText == $0.currentText
                    }.count,
                    "fuzzyLineCount": delta.lineMatches.filter {
                        $0.previousText != $0.currentText
                    }.count,
                    "matches": delta.lineMatches.map { match in
                        [
                            "previousLineIndex": match.previousLineIndex,
                            "currentLineIndex": match.currentLineIndex,
                            "previousText": match.previousText,
                            "currentText": match.currentText,
                            "editDistance": match.editDistance,
                            "similarity": match.similarity,
                        ] as [String: Any]
                    },
                ]
            }
            if !delta.tokenMatches.isEmpty {
                novelty["tokenAlignment"] = [
                    "matchedTokenCount": delta.tokenMatches.count,
                    "exactTokenCount": delta.tokenMatches.filter {
                        $0.kind == "exact"
                    }.count,
                    "fuzzyTokenCount": delta.tokenMatches.filter {
                        $0.kind != "exact"
                    }.count,
                    "matches": delta.tokenMatches.map { match in
                        [
                            "previousTokenIndex": match.previousTokenIndex,
                            "currentTokenIndex": match.currentTokenIndex,
                            "previousText": match.previousText,
                            "currentText": match.currentText,
                            "kind": match.kind,
                        ] as [String: Any]
                    },
                ]
            }
            candidate.event["readNovelty"] = novelty
            accepted.append(candidate)
            previousCausalRead = completeCurrent
            continue
        }
        if mode == .destructiveV15 {
            let completeCurrent = candidate
            defer { previousCausalRead = completeCurrent }
            guard let previous = previousCausalRead,
                  let surface = adjacentReadSurfaceCompatibility(
                    previous.raw,
                    candidate.raw
                  ) else {
                accepted.append(candidate)
                continue
            }
            let previousContent = stringValue(previous.raw["content"]) ?? ""
            let currentContent = stringValue(candidate.raw["content"]) ?? ""
            guard let delta = adjacentCausalReadDelta(
                previous: previousContent,
                current: currentContent
            ) else {
                if var reduction = candidate.event["reduction"] as? [String: Any] {
                    reduction["adjacentReadDelta"] = [
                        "ruleVersion": "adjacent-causal-read-delta-v1",
                        "decision": "retain_complete_current",
                        "reason": "order_preserving_alignment_unproven",
                        "priorEventID": stringValue(previous.event["eventID"]) ?? "",
                        "orderingField": "capturedAt",
                        "orderingTimestamp": candidate.overlapBoundaryAt,
                        "surface": surface,
                    ]
                    candidate.event["reduction"] = reduction
                }
                accepted.append(candidate)
                continue
            }
            let details: [String: Any] = [
                "ruleVersion": "adjacent-causal-read-delta-v1",
                "decision": delta.emittedContent.isEmpty
                    ? "suppress_no_new_content"
                    : "emit_new_content",
                "alignment": delta.alignment,
                "overlapCharacterCount": delta.overlapCharacterCount,
                "currentCharacterCount": delta.currentCharacterCount,
                "emittedCharacterCount": delta.emittedContent.count,
                "priorEventID": stringValue(previous.event["eventID"]) ?? "",
                "orderingField": "capturedAt",
                "orderingTimestamp": candidate.overlapBoundaryAt,
                "surface": surface,
            ]
            guard !delta.emittedContent.isEmpty else {
                dispositions.append(ReducerDisposition(
                    line: candidate.rawLine,
                    object: reducerUnresolved(
                        sessionID: sessionID, raw: candidate.raw,
                        line: candidate.rawLine, kind: "read",
                        rule: "semantic_time_adjacent_causal_read_delta_v1",
                        reason: "adjacent_causal_read_no_new_content",
                        details: details,
                        sourceRecordIDs: candidate.event["sourceRecordIDs"]
                            as? [String]
                    )
                ))
                continue
            }
            candidate.event["content"] = delta.emittedContent
            let emittedLines = delta.emittedContent.split(
                separator: "\n", omittingEmptySubsequences: true
            ).count
            candidate.event["emittedLineCount"] = emittedLines
            candidate.event["overlapRemovedLineCount"] = max(
                (intValue(candidate.raw["recognizedLineCount"]) ?? emittedLines)
                    - emittedLines,
                0
            )
            if var reduction = candidate.event["reduction"] as? [String: Any] {
                reduction["adjacentReadDelta"] = details
                candidate.event["reduction"] = reduction
            }
            accepted.append(candidate)
            continue
        }
        var context = "\(intValue(candidate.raw["processIdentifier"]) ?? -1)|\(intValue(candidate.raw["windowID"]) ?? -1)|\(intValue(candidate.raw["displayID"]) ?? -1)"
        if paneAwareSurfaceIdentity {
            context += "|pane:\(readOverlapPaneIdentity(candidate.raw))"
        }
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

private func reducerReadOCRLines(_ value: Any?) -> [ReadOCRLineEvidence] {
    guard let rows = value as? [[String: Any]] else { return [] }
    return rows.enumerated().compactMap { index, row in
        guard let text = stringValue(row["text"]),
              let confidence = doubleValue(row["confidence"]) else { return nil }
        let bounds = row["boundingBox"] as? [String: Any]
        return ReadOCRLineEvidence(
            index: index,
            text: text,
            confidence: confidence,
            x: doubleValue(bounds?["x"]),
            y: doubleValue(bounds?["y"]),
            width: doubleValue(bounds?["width"]),
            height: doubleValue(bounds?["height"])
        )
    }
}

/// Two capture pathways can prove that they observed the same settled state
/// while disagreeing about the few visible pixels of a line at a content
/// boundary. This identifies only short, peripheral lines whose peer OCR at
/// the same vertical location is materially incompatible. The evidence is
/// attached to the reconciled event and is interpreted only by semantic v19.
private func pairedUnstableClippedOCRLineEvidence(
    selected: [String: Any],
    peer: [String: Any]
) -> [[String: Any]] {
    let selectedLines = reducerReadOCRLines(selected["_readSurfaceLines"])
    let peerLines = reducerReadOCRLines(peer["_readSurfaceLines"])
    let referenceHeights = selectedLines.compactMap { line -> Double? in
        guard line.confidence >= 0.80,
              line.text.split(whereSeparator: \Character.isWhitespace)
                .joined(separator: " ").count >= 8,
              let height = line.height, height > 0 else { return nil }
        return height
    }.sorted()
    guard !referenceHeights.isEmpty, !peerLines.isEmpty else { return [] }
    let middle = referenceHeights.count / 2
    let median = referenceHeights.count.isMultiple(of: 2)
        ? (referenceHeights[middle - 1] + referenceHeights[middle]) / 2
        : referenceHeights[middle]

    func peerComparison(
        _ line: ReadOCRLineEvidence
    ) -> (peers: [ReadOCRLineEvidence], bestSimilarity: Double)? {
        guard let y = line.y, let height = line.height,
              height > 0 else { return nil }
        let center = y + height / 2
        let peers = peerLines.filter { other in
            guard let otherY = other.y, let otherHeight = other.height else {
                return false
            }
            let overlap = max(
                0, min(y + height, otherY + otherHeight) - max(y, otherY)
            )
            let centerDistance = abs(
                center - (otherY + otherHeight / 2)
            )
            return overlap > 0 || centerDistance <= max(height, otherHeight)
        }
        guard !peers.isEmpty else { return nil }
        let bestSimilarity = peers.map {
            normalizedReadOCRSimilarity(line.text, $0.text)
        }.max() ?? 0
        return (peers, bestSimilarity)
    }

    func evidence(
        _ line: ReadOCRLineEvidence,
        peers: [ReadOCRLineEvidence],
        bestSimilarity: Double,
        reason: String
    ) -> [String: Any]? {
        guard let y = line.y, let height = line.height else { return nil }
        var result: [String: Any] = [
            "lineIndex": line.index,
            "text": line.text,
            "textSHA256": reducerSHA256String(line.text),
            "confidence": line.confidence,
            "reason": reason,
            "medianReferenceLineHeight": median,
            "bestPeerOCRSimilarity": bestSimilarity,
            "peerTextSHA256": peers.map { reducerSHA256String($0.text) },
        ]
        if let x = line.x, let width = line.width {
            result["boundingBox"] = [
                "x": x, "y": y, "width": width, "height": height,
            ]
        }
        return result
    }

    // The seed is an abnormally short line at the demonstrated lower
    // content/composer boundary. Exact outer-boundary clipping is handled by
    // the projection itself; this cross-sensor rule exists for an internal
    // boundary inside a larger selected AX pane.
    var selectedEvidence = [Int: [String: Any]]()
    var contaminatedUpperEdge: Double?
    for line in selectedLines {
        guard let y = line.y, let height = line.height,
              height <= median * 0.80,
              let comparison = peerComparison(line),
              comparison.bestSimilarity < 0.90,
              let item = evidence(
                line, peers: comparison.peers,
                bestSimilarity: comparison.bestSimilarity,
                reason: "cross_sensor_unstable_clipped_boundary_line"
              ) else { continue }
        let center = y + height / 2
        let isLowerBoundary = center <= 0.20
        let isWideUpperBoundary = center >= 0.80
            && ((line.width ?? 0) >= 0.50 || line.text.count >= 64)
        guard isLowerBoundary || isWideUpperBoundary else { continue }
        selectedEvidence[line.index] = item
        if isLowerBoundary {
            contaminatedUpperEdge = max(contaminatedUpperEdge ?? 0, y + height)
        }
    }
    guard var upperEdge = contaminatedUpperEdge else {
        return selectedEvidence.keys.sorted().compactMap { selectedEvidence[$0] }
    }

    // A clipped line can corrupt the immediately adjacent OCR row even when
    // that row retains normal height. Extend only through locally contiguous,
    // cross-sensor-disagreeing rows and stop once stable text resumes.
    for line in selectedLines.sorted(by: { ($0.y ?? 2) < ($1.y ?? 2) }) {
        guard selectedEvidence[line.index] == nil,
              let y = line.y, let height = line.height,
              y + height / 2 <= 0.25,
              y <= upperEdge + median * 1.25,
              let comparison = peerComparison(line),
              comparison.bestSimilarity < 0.98,
              let item = evidence(
                line, peers: comparison.peers,
                bestSimilarity: comparison.bestSimilarity,
                reason: "cross_sensor_clipped_boundary_neighbor_disagreement"
              ) else { continue }
        selectedEvidence[line.index] = item
        upperEdge = max(upperEdge, y + height)
    }
    return selectedEvidence.keys.sorted().compactMap { selectedEvidence[$0] }
}

private func reducerAdditionalReadRemovalReasons(
    _ event: [String: Any],
    requireStrongClippingEvidence: Bool = false
) -> [Int: String] {
    guard let reduction = event["reduction"] as? [String: Any],
          let rows = reduction["unstableClippedOCRLineEvidence"]
            as? [[String: Any]] else { return [:] }
    let eligibleRows: [[String: Any]]
    let bundleIdentifier = stringValue(event["bundleIdentifier"]) ?? ""
    func isKnownCodexTimestamp(_ row: [String: Any]) -> Bool {
        guard bundleIdentifier == "com.openai.codex",
              let text = stringValue(row["text"]),
              text.range(
                of: #"^[0-9]{1,2}:[0-9]{2}\s*(?:AM|PM)$"#,
                options: [.regularExpression, .caseInsensitive]
              ) != nil,
              let box = row["boundingBox"] as? [String: Any],
              let y = doubleValue(box["y"]),
              let height = doubleValue(box["height"]),
              y + height / 2 <= 0.08 else { return false }
        return true
    }
    if !requireStrongClippingEvidence {
        eligibleRows = rows
    } else {
        func verticalBounds(_ row: [String: Any]) -> (Double, Double)? {
            guard let box = row["boundingBox"] as? [String: Any],
                  let y = doubleValue(box["y"]),
                  let height = doubleValue(box["height"]) else { return nil }
            return (y, y + height)
        }
        func isStrong(_ row: [String: Any]) -> Bool {
            if isKnownCodexTimestamp(row) { return true }
            guard let confidence = doubleValue(row["confidence"]),
                  let text = stringValue(row["text"]) else { return false }
            guard let box = row["boundingBox"] as? [String: Any],
                  let height = doubleValue(box["height"]),
                  let width = doubleValue(box["width"]),
                  let median = doubleValue(row["medianReferenceLineHeight"])
            else { return false }
            return readCrossSensorClippingSeedIsStrong(
                text: text, confidence: confidence, width: width,
                height: height, medianReferenceLineHeight: median
            )
        }
        let strongIndices = Set(rows.indices.filter { isStrong(rows[$0]) })
        var connected = strongIndices
        var changed = true
        while changed {
            changed = false
            for index in rows.indices where !connected.contains(index) {
                guard let bounds = verticalBounds(rows[index]),
                      let median = doubleValue(
                        rows[index]["medianReferenceLineHeight"]
                      ) else { continue }
                let touchesStrongComponent = connected.contains { otherIndex in
                    guard let other = verticalBounds(rows[otherIndex]) else {
                        return false
                    }
                    let gap = max(
                        0, max(bounds.0, other.0) - min(bounds.1, other.1)
                    )
                    return gap <= median * 1.25
                }
                if touchesStrongComponent {
                    connected.insert(index)
                    changed = true
                }
            }
        }
        eligibleRows = rows.indices.compactMap {
            connected.contains($0) ? rows[$0] : nil
        }
    }
    return eligibleRows.reduce(into: [Int: String]()) { result, row in
        guard let index = intValue(row["lineIndex"]),
              let reason = nonEmptyString(row["reason"]) else { return }
        result[index] = requireStrongClippingEvidence
            && isKnownCodexTimestamp(row)
            ? "codex_interface_timestamp"
            : reason
    }
}

private func reducerSemanticReadDetails(
    observedContent: String,
    projection: ReadSemanticContentProjection,
    hadLineEvidence: Bool,
    ruleVersion: String = "read-interface-scaffolding-v3"
) -> [String: Any] {
    let removed: [[String: Any]] = projection.removed.map { item in
        var result: [String: Any] = [
            "lineIndex": item.line.index,
            "text": item.line.text,
            "textSHA256": reducerSHA256String(item.line.text),
            "confidence": item.line.confidence,
            "reason": item.reason,
            "supportingDistinctContentStateCount": item.supportingDistinctContentStateCount,
            "supportingDistinctWindowCount": item.supportingDistinctWindowCount,
        ]
        if let x = item.line.x, let y = item.line.y,
           let width = item.line.width, let height = item.line.height {
            result["boundingBox"] = [
                "x": x, "y": y, "width": width, "height": height,
            ]
        }
        return result
    }
    return [
        "schemaVersion": 1,
        "ruleVersion": ruleVersion,
        "decision": removed.isEmpty
            ? "retain_observed_content"
            : "remove_proven_interface_scaffolding",
        "lineEvidenceAvailable": hadLineEvidence,
        "observedContentSHA256": reducerSHA256String(observedContent),
        "semanticContentSHA256": reducerSHA256String(projection.content),
        "observedCharacterCount": observedContent.count,
        "semanticCharacterCount": projection.content.count,
        "removedLineCount": removed.count,
        "removedLines": removed,
    ]
}

/// Past-only scaffolding evidence is scoped to a durable visual surface. The
/// key intentionally excludes window title and AX object identity: titles vary
/// with content and Electron recreates equivalent AX objects. Process/window,
/// selected pane role/method, and coarse normalized geometry prevent evidence
/// from leaking across unrelated panes while tolerating minute AX jitter.
private func reducerReadScaffoldingSurfaceKey(_ raw: [String: Any]) -> String {
    let readSurface = raw["readSurface"] as? [String: Any]
    let selection = readSurface?["surfaceSelection"] as? [String: Any]
    let region = selection?["regionOfInterest"] as? [String: Any]
        ?? readSurface?["regionOfInterest"] as? [String: Any]
    func coarse(_ value: Any?) -> String {
        guard let number = doubleValue(value) else { return "-" }
        return String(
            format: "%.2f", locale: Locale(identifier: "en_US_POSIX"), number
        )
    }
    let components = [
        stringValue(raw["bundleIdentifier"]) ?? "-",
        reducerComparableString(raw["processIdentifier"]),
        reducerComparableString(raw["windowID"]),
        stringValue(raw["captureScope"]) ?? "-",
        stringValue(readSurface?["ruleVersion"]) ?? "-",
        stringValue(selection?["method"]) ?? "-",
        stringValue(selection?["selectedRole"]) ?? "-",
        stringValue(selection?["selectedSubrole"]) ?? "-",
        coarse(region?["x"]), coarse(region?["y"]),
        coarse(region?["width"]), coarse(region?["height"]),
    ].joined(separator: "|")
    return reducerSHA256String(components)
}

/// Establishes whether two globally adjacent READ observations can safely be
/// treated as successive states of one visual surface. AX objects and their
/// depths are deliberately excluded from the hard identity: Electron often
/// recreates those while the same pane remains visible.
private func adjacentReadSurfaceCompatibility(
    _ previous: [String: Any],
    _ current: [String: Any]
) -> [String: Any]? {
    for key in [
        "processIdentifier", "windowID", "displayID", "bundleIdentifier",
        "windowTitle",
    ] where reducerComparableString(previous[key])
        != reducerComparableString(current[key]) {
        return nil
    }
    guard !reducerComparableString(current["processIdentifier"]).isEmpty,
          !reducerComparableString(current["windowID"]).isEmpty,
          !reducerComparableString(current["bundleIdentifier"]).isEmpty
    else { return nil }

    let geometryOverlap = reducerRectangleOverlapOverSmallerArea(
        previous["captureBounds"] as? [String: Any],
        current["captureBounds"] as? [String: Any]
    )
    guard geometryOverlap >= 0.90 else { return nil }
    let priorPane = adjacentReadPaneEvidence(previous)
    let currentPane = adjacentReadPaneEvidence(current)
    guard let priorRole = priorPane.role,
          let currentRole = currentPane.role,
          adjacentReadRolesAreCompatible(priorRole, currentRole) else {
        return nil
    }
    return [
        "geometryOverlapOverSmallerArea": geometryOverlap,
        "previousPaneRole": priorPane.role ?? NSNull(),
        "currentPaneRole": currentPane.role ?? NSNull(),
        "previousPaneLabel": priorPane.label ?? NSNull(),
        "currentPaneLabel": currentPane.label ?? NSNull(),
        "previousAXDepth": priorPane.depth ?? NSNull(),
        "currentAXDepth": currentPane.depth ?? NSNull(),
        "axDepthUsedAsIdentity": false,
    ]
}

private func adjacentReadPaneEvidence(
    _ raw: [String: Any]
) -> (role: String?, label: String?, depth: Any?) {
    guard let readSurface = raw["readSurface"] as? [String: Any],
          let selection = readSurface["surfaceSelection"] as? [String: Any]
    else { return (nil, nil, nil) }
    let depth = intValue(selection["selectedDepth"])
    let selectedNode = depth.flatMap { selectedDepth in
        (raw["accessibilitySurface"] as? [String: Any])
            .flatMap { $0["ancestors"] as? [[String: Any]] }
            .flatMap { ancestors in
                ancestors.first { intValue($0["depth"]) == selectedDepth }
            }
    }
    let role = nonEmptyString(selection["selectedRole"])
        ?? nonEmptyString(selectedNode?["role"])
    let label = nonEmptyString(selection["selectedLabel"])
        ?? nonEmptyString(selection["selectedTitle"])
        ?? nonEmptyString(selection["selectedDescription"])
        ?? nonEmptyString(selectedNode?["title"])
        ?? nonEmptyString(selectedNode?["elementDescription"])
        ?? nonEmptyString(selectedNode?["identifier"])
    return (role, label, depth)
}

private func adjacentReadRolesAreCompatible(_ left: String, _ right: String) -> Bool {
    if left == right { return true }
    let containerRoles: Set<String> = [
        "AXGroup", "AXLayoutArea", "AXScrollArea", "AXWebArea",
    ]
    return containerRoles.contains(left) && containerRoles.contains(right)
}

/// Returns a deterministic logical identity for the AX pane selected by the
/// immutable READ-surface artifact. Geometry is normalized to the captured
/// window, so moving the window does not change the identity. We deliberately
/// do not use AX elementHash: Electron can recreate an equivalent AX object,
/// while its semantic role, label, and pane geometry remain stable.
private func readOverlapPaneIdentity(_ raw: [String: Any]) -> String {
    guard let readSurface = raw["readSurface"] as? [String: Any],
          [
            "ax-pane-read-v2", "ax-pane-read-v3", "ax-pane-read-v4",
            "ax-pane-read-v5", "ax-pane-read-v6", "ax-pane-read-v7",
          ].contains(
            stringValue(readSurface["ruleVersion"]) ?? ""
          ) else {
        return "legacy-window-surface"
    }
    guard let selection = readSurface["surfaceSelection"] as? [String: Any]
    else {
        // Missing v2 selection means the pane is not proven. A unique identity
        // conservatively retains the READ instead of deduplicating across an
        // unknown pane boundary.
        return "unresolved-\(stringValue(raw["recordID"]) ?? "unknown")"
    }

    let selectedDepth = intValue(selection["selectedDepth"])
    var selectedNode: [String: Any]?
    if let selectedDepth,
       let surface = raw["accessibilitySurface"] as? [String: Any],
       let ancestors = surface["ancestors"] as? [[String: Any]] {
        selectedNode = ancestors.first {
            intValue($0["depth"]) == selectedDepth
        }
    }
    let region = selection["regionOfInterest"] as? [String: Any]
        ?? readSurface["regionOfInterest"] as? [String: Any]
    func normalizedNumber(_ value: Any?) -> String {
        guard let number = doubleValue(value) else { return "-" }
        return String(
            format: "%.6f", locale: Locale(identifier: "en_US_POSIX"), number
        )
    }
    let method = stringValue(selection["method"]) ?? "unknown-method"
    let depth = selectedDepth.map(String.init) ?? "-"
    let role = stringValue(selection["selectedRole"])
        ?? stringValue(selectedNode?["role"]) ?? "-"
    let subrole = stringValue(selection["selectedSubrole"])
        ?? stringValue(selectedNode?["subrole"]) ?? "-"
    let identifier = stringValue(selectedNode?["identifier"]) ?? "-"
    let title = stringValue(selectedNode?["title"]) ?? "-"
    let elementDescription = stringValue(selectedNode?["elementDescription"])
        ?? "-"
    let components = [
        method, depth, role, subrole, identifier, title, elementDescription,
        normalizedNumber(region?["x"]), normalizedNumber(region?["y"]),
        normalizedNumber(region?["width"]), normalizedNumber(region?["height"]),
    ].joined(separator: "|")
    return reducerSHA256String(components)
}
private struct ReducerSelection {
    let observation: [String: Any]
    let source: String
    let checkpointID: String?
    let reason: String
}

/// Verifies the immutable visual frame -> OCR chain before that OCR can become
/// a semantic READ. The screenshot digest is checked against bytes in the raw
/// session, and the frame must precede its OCR record in raw append order.
private func validateVisualReadEvidence(
    _ ocr: ReducerLine,
    rawByID: [String: ReducerLine],
    sessionID: String,
    sourceDirectory: URL
) -> Result<ReducerVisualReadEvidence, ReducerFailure> {
    func fail(
        _ reason: String,
        details: [String: Any] = [:]
    ) -> Result<ReducerVisualReadEvidence, ReducerFailure> {
        .failure(ReducerFailure(
            rule: "visual_frame_ocr_lineage_v1",
            reason: reason,
            details: details
        ))
    }
    let raw = ocr.object
    guard intValue(raw["schemaVersion"]) == 1,
          let ocrID = nonEmptyString(raw["recordID"]),
          let frameID = nonEmptyString(raw["sourceFrameRecordID"]),
          let frame = rawByID[frameID],
          frame.line < ocr.line,
          stringValue(frame.object["recordType"]) == "visual_frame_observation",
          intValue(frame.object["schemaVersion"]) == 1 else {
        return fail("visual_source_frame_missing_or_out_of_order")
    }
    guard stringValue(frame.object["sessionID"]) == sessionID,
          stringValue(raw["sessionID"]) == sessionID else {
        return fail("visual_frame_session_identity_mismatch")
    }
    guard nonEmptyString(frame.object["derivedSuppressionReason"]) == nil,
          stringArray(frame.object["overlappedWriteAttemptIDs"]).isEmpty else {
        return fail("visual_frame_suppressed_or_write_overlapped")
    }
    guard let frameCapturedAt = nonEmptyString(frame.object["capturedAt"]),
          frameCapturedAt == stringValue(raw["capturedAt"]),
          frameCapturedAt == stringValue(raw["sourceFrameCapturedAt"]),
          intValue(frame.object["frameSequence"])
            == intValue(raw["sourceFrameSequence"]),
          let frameSHA = nonEmptyString(frame.object["screenshotSHA256"]),
          frameSHA == stringValue(raw["screenshotSHA256"]),
          frameSHA == stringValue(raw["sourceFrameScreenshotSHA256"]),
          let relativePath = nonEmptyString(frame.object["screenshotRelativePath"]),
          relativePath == stringValue(raw["screenshotRelativePath"]),
          intValue(frame.object["screenshotPixelWidth"])
            == intValue(raw["screenshotPixelWidth"]),
          intValue(frame.object["screenshotPixelHeight"])
            == intValue(raw["screenshotPixelHeight"]) else {
        return fail("visual_frame_ocr_identity_mismatch")
    }
    guard let surface = frame.object["surface"] as? [String: Any],
          visualFrameSurfaceMatchesOCR(surface, raw) else {
        return fail("visual_frame_ocr_surface_mismatch")
    }
    let screenshotURL = sourceDirectory.appendingPathComponent(relativePath)
        .standardizedFileURL
    let sourcePrefix = sourceDirectory.standardizedFileURL.path + "/"
    guard screenshotURL.path.hasPrefix(sourcePrefix),
          FileManager.default.fileExists(atPath: screenshotURL.path) else {
        return fail("visual_frame_screenshot_missing")
    }
    do {
        guard try reducerSHA256(screenshotURL) == frameSHA else {
            return fail("visual_frame_screenshot_digest_mismatch")
        }
    } catch {
        return fail("visual_frame_screenshot_unreadable")
    }
    var effective = raw
    effective["sourceRecordIDs"] = [frameID, ocrID]
    effective["visualFrameEvidence"] = [
        "schemaVersion": 1,
        "sourceFrameRecordID": frameID,
        "sourceFrameSequence": raw["sourceFrameSequence"]!,
        "evidenceReason": stringValue(frame.object["evidenceReason"]) ?? "unknown",
        "screenshotSHA256": frameSHA,
    ]
    return .success(ReducerVisualReadEvidence(
        object: effective,
        lineage: [frameID, ocrID]
    ))
}

private func visualFrameSurfaceMatchesOCR(
    _ surface: [String: Any],
    _ ocr: [String: Any]
) -> Bool {
    intValue(surface["displayID"]) == intValue(ocr["displayID"])
        && intValue(surface["windowID"]) == intValue(ocr["windowID"])
        && intValue(surface["processIdentifier"])
            == intValue(ocr["processIdentifier"])
        && stringValue(surface["windowTitle"]) == stringValue(ocr["windowTitle"])
        && rectanglesApproximatelyEqual(
            surface["windowBounds"] as? [String: Any],
            ocr["windowBounds"] as? [String: Any]
        )
}

private func reduceRead(
    _ raw: [String: Any],
    sessionID: String,
    lineage: [String]? = nil,
    provenance: String = "screen_ocr",
    rule: String = "screen_ocr_v1"
)
    -> Result<[String: Any], ReducerFailure>
{
    func fail(_ reason: String) -> Result<[String: Any], ReducerFailure> {
        .failure(ReducerFailure(rule: rule, reason: reason, details: [:]))
    }
    guard let recordID = stringValue(raw["recordID"]) else { return fail("missing_record_id") }
    let sourceRecordIDs = lineage ?? [recordID]
    if let post = raw["postCaptureSurface"] as? [String: Any],
       !sameCapturedReadSurface(raw, post) {
        return fail("surface_changed_during_capture")
    }
    if nonEmptyString(raw["supersedingWriteAttemptID"]) != nil {
        return fail("read_candidate_superseded_by_write")
    }
    if isChromiumBrowserBundleIdentifier(stringValue(raw["bundleIdentifier"])),
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
    let eventID = stableEventID(sessionID: sessionID, lineage: sourceRecordIDs, ordinal: 0)
    var event = raw
    for key in [
        "recordType", "recordID", "schemaVersion", "derivedSuppressionReason",
        "supersedingWriteAttemptID", "screenshotRelativePath", "screenshotSHA256",
        "screenshotPixelWidth", "screenshotPixelHeight", "postCaptureSurface",
        "firstEventTimestampNanoseconds", "lastEventTimestampNanoseconds",
        "sourceFrameRecordID", "sourceFrameSequence", "sourceFrameCapturedAt",
        "sourceFrameScreenshotSHA256", "ocrEngine",
        "_readSurfaceLines", "_readComparisonContent", "_readComparisonLines",
    ] { event.removeValue(forKey: key) }
    event["schemaVersion"] = 8
    event["kind"] = "read"
    event["provenance"] = provenance
    event["eventID"] = eventID
    event["sourceRecordIDs"] = sourceRecordIDs
    event["reduction"] = [
        "schemaVersion": 1,
        "rule": rule,
        "reason": "eligible_capture_time_observation",
        "selectedObservationID": recordID,
        "rawLineage": sourceRecordIDs,
        "outputOrdinal": 0,
    ]
    return .success(event)
}

private func reduceWrite(
    _ raw: [String: Any],
    sessionID: String,
    lineage: [String]? = nil,
    promptClosure: ReducerPromptClosure? = nil
)
    -> Result<[String: Any], ReducerFailure>
{
    func fail(_ reason: String, rule: String = "write_observation_selection_v1", details: [String: Any] = [:])
        -> Result<[String: Any], ReducerFailure> {
        .failure(ReducerFailure(rule: rule, reason: reason, details: details))
    }
    guard let recordID = stringValue(raw["recordID"]) else { return fail("missing_record_id") }
    let writeSourceRecordIDs = lineage ?? [recordID]
    let sourceRecordIDs = writeSourceRecordIDs
        + [promptClosure?.recordID].compactMap { $0 }
    let composedNavigation = writeSourceRecordIDs.count > 1
        && stringValue(raw["semanticComposition"])
            == "same_editable_navigation_chain"
    guard (raw["tapTimeoutCountDuringBurst"] as? NSNumber)?.uint64Value ?? 0 == 0 else {
        return fail("tap_timeout")
    }
    if hasUnobservedMidBurstShortcut(raw) {
        return fail(
            "shortcut_changed_semantic_position_without_observation",
            rule: "unobserved_mid_burst_shortcut_guard_v1"
        )
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

    let checkpointGrounding = checkpointGroundedReplacementEdit(
        raw: raw, beforeValue: beforeValue, afterValue: afterValue,
        usedObservation: selection.observation, canonicalEdit: observedEdit
    ) ?? checkpointGroundedEquivalentEdit(
        raw: raw, beforeValue: beforeValue, afterValue: afterValue,
        usedObservation: selection.observation, canonicalEdit: observedEdit
    )
    let resolvedEdit = checkpointGrounding?.edit ?? observedEdit
    guard applying(resolvedEdit, to: beforeValue) == afterValue else {
        return fail(
            "checkpoint_alignment_does_not_reconstruct_observation",
            rule: checkpointGrounding?.rule
                ?? "checkpoint_grounded_equivalent_diff_v1"
        )
    }
    if checkpointGrounding?.rule == "checkpoint_grounded_equivalent_diff_v1",
       (resolvedEdit.removed.count != observedEdit.removed.count
        || resolvedEdit.inserted.count != observedEdit.inserted.count) {
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

    let historyOnlyFullField = raw["semanticFullFieldCompletion"] as? Bool == true
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
        : historyOnlyFullField
        ? ReducerAuthorship(
            segments: afterValue.isEmpty ? [] : [.authored(afterValue)],
            resolution: "resolved",
            resolvedCompletion: afterValue,
            stateContinuity: "incomplete_pre_mutation_conditioning",
            evidence: "fast_start_full_field_history_only",
            evidenceObservationID: stringValue(selection.observation["observationID"]),
            semanticEdit: nil
        )
        : reduceAuthorship(
            raw: raw, beforeValue: beforeValue,
            usedObservation: selection.observation, observedEdit: resolvedEdit
        )
    let unresolvedPasteReason: String?
    let unnormalizedAuthorship: ReducerAuthorship
    if interpretedAuthorship.resolution != "resolved", hints.contains("paste") {
        unresolvedPasteReason = interpretedAuthorship.resolution
        let contextSegments = resolvedEdit.inserted.isEmpty
            ? []
            : [WriteAuthorshipSegment(
                type: "unresolved_paste_transition",
                content: resolvedEdit.inserted
            )]
        unnormalizedAuthorship = ReducerAuthorship(
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
        unnormalizedAuthorship = interpretedAuthorship
    }
    let authorship = normalizeObsidianListScaffolding(
        unnormalizedAuthorship,
        bundleIdentifier: stringValue(raw["bundleIdentifier"])
            ?? stringValue((raw["targetIdentity"] as? [String: Any])?["bundleIdentifier"]),
        inputHints: hints,
        fallbackEdit: resolvedEdit
    )
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
        "schemaVersion": 13,
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
        "boundaryReason": promptClosure == nil
            ? stringValue(raw["boundaryReason"]) ?? "unknown"
            : "submission_boundary",
        "captureBoundaryReason": stringValue(raw["boundaryReason"]) ?? "unknown",
        "submissionObservedAt": promptClosure?.observedAt as Any,
        "closureEvidence": promptClosure?.evidence as Any,
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
            "alignmentRule": checkpointGrounding?.rule
                ?? "canonical_minimal_diff",
            "alignmentObservationCount": checkpointGrounding?.observationCount ?? 0,
            "rawLineage": sourceRecordIDs,
            "outputOrdinal": 0,
        ],
    ]
    if let reason = stringValue(raw["semanticTargetIneligibilityReason"]) {
        event["phase1TargetEligibility"] = [
            "eligible": false,
            "reason": reason,
        ]
    }
    // JSONSerialization cannot encode Swift optionals hidden in Any.
    event = removeNullOptionals(event)
    return .success(event)
}

/// Obsidian exposes its internal list continuation markers as literal AX text:
/// zero-width-space-only lines, an optional tab line, and the next bullet. The
/// markers are a rendered editor transition, not characters authored by the
/// person. Keep the exact BEFORE/AFTER transition in `observedNetEdit`, while
/// removing only this proven scaffold from the semantic completion.
///
/// This deliberately applies only to resolved, typed Return bursts in
/// Obsidian. A zero-width character anywhere else remains unresolved.
private func normalizeObsidianListScaffolding(
    _ authorship: ReducerAuthorship,
    bundleIdentifier: String?,
    inputHints: Set<String>,
    fallbackEdit: TextEdit
) -> ReducerAuthorship {
    guard bundleIdentifier == "md.obsidian",
          authorship.resolution == "resolved",
          inputHints.contains("typed"), inputHints.contains("return"),
          authorship.segments.contains(where: {
              $0.type == "authored_text" && $0.content.contains("\u{200B}")
          }) else { return authorship }

    // Obsidian currently exposes two equivalent list boundaries. Some editor
    // states include an extra zero-width-only line before the literal dash;
    // others proceed directly from the first zero-width line to the dash.
    // Both are exact structural AX scaffolds surrounding a typed Return.
    let scaffold = "\n\u{200B}(?:\\t)?\n(?:\u{200B}\n)?-\n\u{200B} (?:\n)?"
    guard let expression = try? NSRegularExpression(pattern: scaffold) else {
        return authorship
    }
    var changed = false
    let segments = authorship.segments.map { segment -> WriteAuthorshipSegment in
        guard segment.type == "authored_text" else { return segment }
        let range = NSRange(segment.content.startIndex..., in: segment.content)
        var content = expression.stringByReplacingMatches(
            in: segment.content, range: range, withTemplate: "\n"
        )
        if content != segment.content { changed = true }
        // A trailing generated bullet is represented by the replacement's
        // final newline. It is not part of the completed thought.
        if content.hasSuffix("\n"), segment.content.hasSuffix("\u{200B} ") {
            content.removeLast()
        }
        // Starting a fresh Obsidian bullet also exposes one structural newline.
        if content.hasPrefix("\n") { content.removeFirst() }
        return WriteAuthorshipSegment(type: segment.type, content: content,
                                      clipboardSnapshotID: segment.clipboardSnapshotID,
                                      pasteCheckpointID: segment.pasteCheckpointID)
    }
    guard changed,
          !segments.contains(where: {
              $0.type == "authored_text" && $0.content.contains("\u{200B}")
          }) else { return authorship }
    let resolved = segments.map(\.content).joined()
    let sourceEdit = authorship.semanticEdit ?? fallbackEdit
    let semanticEdit = TextEdit(
        operation: sourceEdit.operation,
        characterOffset: sourceEdit.characterOffset,
        removed: sourceEdit.removed,
        inserted: resolved
    )
    return ReducerAuthorship(
        segments: segments,
        resolution: authorship.resolution,
        resolvedCompletion: resolved,
        stateContinuity: authorship.stateContinuity,
        evidence: "obsidian_list_scaffold_normalized_v1",
        evidenceObservationID: authorship.evidenceObservationID,
        semanticEdit: semanticEdit
    )
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

private func reducerSHA256String(_ value: String) -> String {
    SHA256.hash(data: Data(value.utf8))
        .map { String(format: "%02x", $0) }
        .joined()
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
