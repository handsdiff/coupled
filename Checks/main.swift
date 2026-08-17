import Foundation
import CoreGraphics

func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
    guard condition() else {
        FileHandle.standardError.write(Data("FAIL: \(message)\n".utf8))
        exit(1)
    }
}

expect(
    isWriteSelectionBoundaryKey(keyCode: 123, commandPressed: false)
        && isWriteSelectionBoundaryKey(keyCode: 48, commandPressed: false)
        && isWriteSelectionBoundaryKey(keyCode: 0, commandPressed: true),
    "caret, focus-traversal, and select-all keys close an active write"
)
expect(
    !isWriteSelectionBoundaryKey(keyCode: 0, commandPressed: false)
        && !isWriteSelectionBoundaryKey(keyCode: 9, commandPressed: true),
    "ordinary typing and paste remain inside the current write burst"
)

expect(
    minimalTextEdit(from: "hello world", to: "hello brave world")
        == TextEdit(operation: .insert, characterOffset: 6, removed: "", inserted: "brave "),
    "middle insertion"
)
expect(
    minimalTextEdit(from: "one two three", to: "one three")
        == TextEdit(operation: .delete, characterOffset: 5, removed: "wo t", inserted: ""),
    "cursor-independent deletion uses the deterministic longest prefix"
)
expect(
    minimalTextEdit(from: "older line\n", to: "older line\nnew line")
        == TextEdit(operation: .insert, characterOffset: 11, removed: "", inserted: "new line"),
    "canonical diff never absorbs an unchanged prefix to match cursor metadata"
)
expect(
    minimalTextEdit(from: "A 🐈 sat", to: "A 🐕 ran")
        == TextEdit(operation: .replace, characterOffset: 2, removed: "🐈 sat", inserted: "🐕 ran"),
    "unicode replacement"
)
expect(minimalTextEdit(from: "same", to: "same").isEmpty, "no-op")
let unicodeEdit = TextEdit(
    operation: .replace,
    characterOffset: 2,
    removed: "🐈",
    inserted: "🐕"
)
expect(applying(unicodeEdit, to: "A 🐈 sat") == "A 🐕 sat", "verified edit application")
expect(
    characterOffset(in: "A 🐈 sat", utf16Offset: 4) == 3,
    "AX UTF-16 selection offset conversion"
)
expect(
    cursorFidelityStatus(
        initialCursorOffset: 10,
        earliestObservedMutationOffset: 10,
        terminalEditOffset: 10
    ) == .aligned,
    "independent cursor and mutation observations align"
)
expect(
    cursorFidelityStatus(
        initialCursorOffset: 10,
        earliestObservedMutationOffset: 12,
        terminalEditOffset: 12
    ) == .initialCursorDiffersFromEarliestObservedMutation,
    "cursor disagreement is measured rather than changing the diff"
)
expect(
    semanticCursorContext(
        in: "α🐈beta",
        selectionStartUTF16: 1,
        selectionLengthUTF16: 2,
        surroundingCharacterCount: 2
    ) == SemanticCursorContext(
        leftContext: "α",
        selectedText: "🐈",
        rightContext: "be",
        selectionStartCharacters: 1,
        selectionLengthCharacters: 1,
        selectionStartUTF16: 1,
        selectionLengthUTF16: 2,
        fieldCharacterCount: 6,
        leftContextWasTruncated: false,
        selectedTextWasTruncated: false,
        rightContextWasTruncated: true
    ),
    "semantic cursor context converts UTF-16 and preserves surrounding text"
)
expect(
    semanticCursorContext(
        in: "0123456789",
        selectionStartUTF16: 2,
        selectionLengthUTF16: 6,
        surroundingCharacterCount: 2
    )?.selectedText == "2345",
    "large selected text is bounded independently from raw evidence"
)
expect(
    semanticCursorContext(
        in: "🐈",
        selectionStartUTF16: 1,
        selectionLengthUTF16: 0
    ) == nil,
    "cursor offsets inside a surrogate pair are rejected"
)
expect(
    unpopulatedSurfacePrompt(
        bundleIdentifier: "com.openai.codex",
        fieldDescription: "Do anything",
        value: "\nDo anything",
        placeholderValue: nil,
        valueRepresentedPlaceholder: false
    ) == "Do anything",
    "Codex prompt chrome is separated from editable cursor context"
)
expect(
    unpopulatedSurfacePrompt(
        bundleIdentifier: "com.google.Chrome",
        fieldDescription: "Enter a prompt for Gemini",
        value: "Ask Gemini\n",
        placeholderValue: nil,
        valueRepresentedPlaceholder: false
    ) == "Ask Gemini",
    "Gemini prompt chrome is separated from editable cursor context"
)
expect(
    unpopulatedSurfacePrompt(
        bundleIdentifier: "com.google.Chrome",
        fieldDescription: "Enter a prompt for Gemini",
        value: "actual draft",
        placeholderValue: nil,
        valueRepresentedPlaceholder: false
    ) == nil,
    "ordinary prompt content is never guessed to be UI chrome"
)
expect(
    newlyVisibleLines(previous: "alpha\nbeta", current: "beta\ngamma\ngamma") == "gamma",
    "viewport overlap"
)
expect(
    newlyVisibleLines(previous: "later", current: "earlier") == "earlier",
    "reread after leaving a viewport"
)
expect(
    writableCharacters(in: "aé👨‍👩‍👧‍👦") == ["a", "é", "👨‍👩‍👧‍👦"],
    "unicode grapheme splitting"
)
expect(
    writableCharacters(in: "\t\r\u{7f}\u{f700}") == ["\t", "\r"],
    "control and function key filtering"
)
let segmentedPaste = segmentedGroundedPasteCompletion(
    initialValue: "",
    prePasteValue: "before ",
    postPasteValue: "",
    terminalValue: " after",
    clipboardText: "distinctive phrase",
    clipboardSnapshotID: "clipboard",
    pasteCheckpointID: "checkpoint"
)
expect(
    segmentedPaste?.resolvedContent == "before distinctive phrase after"
        && segmentedPaste?.segments.map(\.type)
            == ["authored_text", "paste", "authored_text"],
    "grounded paste composes locally observed AX epochs"
)
expect(
    segmentedGroundedPasteCompletion(
        initialValue: "", prePasteValue: "before ", postPasteValue: "unexpected",
        terminalValue: " after", clipboardText: "distinctive phrase",
        clipboardSnapshotID: "clipboard", pasteCheckpointID: "checkpoint"
    ) == nil
        && segmentedGroundedPasteCompletion(
            initialValue: "", prePasteValue: "before ", postPasteValue: "",
            terminalValue: "distinctive phrase after", clipboardText: "distinctive phrase",
            clipboardSnapshotID: "clipboard", pasteCheckpointID: "checkpoint"
        ) == nil,
    "unexplained or delayed observable paste transitions are never stitched"
)

let formattedLiveWrite = LiveEventLogFormatter.format([
    "kind": "write",
    "observedAt": "2026-01-01T00:00:04.000Z",
    "appName": "Obsidian",
    "windowTitle": "Entry - Notes",
    "operation": "insert",
    "provenance": "active_tap_accessibility_diff",
    "content": "hello",
    "removedContent": "",
    "characterOffset": 4,
    "configuredWriteDelaySeconds": 3,
    "boundaryReason": "write_delay_elapsed",
    "derivationObservationSource": "terminal_after",
])
expect(
    formattedLiveWrite.contains("WRITE  app=Obsidian")
        && formattedLiveWrite.contains("completion=\"hello\"")
        && formattedLiveWrite.contains("authorship=unreported")
        && formattedLiveWrite.contains("configured-delay=3s"),
    "native live window formats the compact write log"
)
let formattedSegmentedWrite = LiveEventLogFormatter.format([
    "kind": "write",
    "observedAt": "2026-01-01T00:00:04.000Z",
    "appName": "Code",
    "operation": "insert",
    "provenance": "active_tap_accessibility_diff",
    "content": " after",
    "resolvedCompletion": "before distinctive phrase after",
    "removedContent": "",
    "characterOffset": 0,
    "authorshipResolution": "resolved",
    "stateContinuity": "segmented_at_grounded_paste",
])
expect(
    formattedSegmentedWrite.contains("completion=\"before distinctive phrase after\"")
        && formattedSegmentedWrite.contains("observed-inserted=\" after\"")
        && formattedSegmentedWrite.contains("authorship=resolved")
        && formattedSegmentedWrite.contains("continuity=segmented_at_grounded_paste"),
    "live view distinguishes resolved completion from observed AX net edit"
)
let formattedLiveRead = LiveEventLogFormatter.format([
    "kind": "read",
    "provenance": "screen_ocr",
    "observedAt": "2026-01-01T00:00:04.000Z",
    "appName": "Code",
    "windowTitle": "checkpoint.md",
    "content": (1...10).map { "line \($0)" }.joined(separator: "\n"),
    "emittedLineCount": 10,
    "recognizedLineCount": 12,
    "overlapRemovedLineCount": 2,
    "viewportSideCropFraction": 0.1,
    "viewportTopCropFraction": 0.1,
    "viewportBottomCropFraction": 0.35,
    "displayID": 7,
])
expect(
    formattedLiveRead.contains("READ  app=Code")
        && formattedLiveRead.contains("    8 | line 8")
        && formattedLiveRead.contains("… 2 more recognized lines")
        && !formattedLiveRead.contains("line 9"),
    "native live window matches the eight-line read preview"
)
expect(
    croppedViewport(
        in: CGRect(x: 100, y: -100, width: 1_000, height: 800),
        sideCropFraction: 0.1,
        topCropFraction: 0.1,
        bottomCropFraction: 0.5
    ) == CGRect(x: 200, y: -20, width: 800, height: 320),
    "asymmetric viewport crop"
)

var viewportDeduplicator = AdjacentViewportDeduplicator()
expect(
    viewportDeduplicator.contentToEmit(
        contextIdentifier: "window-a",
        viewportContent: "alpha\nbeta"
    ) == "alpha\nbeta",
    "first viewport emits all lines"
)
expect(
    viewportDeduplicator.contentToEmit(
        contextIdentifier: "window-a",
        viewportContent: "beta\ngamma\ngamma"
    ) == "gamma",
    "adjacent line overlap is removed"
)
expect(
    viewportDeduplicator.contentToEmit(
        contextIdentifier: "window-a",
        viewportContent: "beta\ngamma\ngamma"
    ) == nil,
    "exact adjacent viewport is suppressed"
)
expect(
    viewportDeduplicator.contentToEmit(
        contextIdentifier: "window-b",
        viewportContent: "beta\ngamma"
    ) == "beta\ngamma",
    "same content in another context is emitted"
)
expect(
    viewportDeduplicator.contentToEmit(
        contextIdentifier: "window-a",
        viewportContent: "alpha\nbeta"
    ) == "alpha\nbeta",
    "returning after another context emits the viewport"
)
viewportDeduplicator.reset()
expect(
    viewportDeduplicator.contentToEmit(
        contextIdentifier: "window-a",
        viewportContent: "alpha\nbeta"
    ) == "alpha\nbeta",
    "an intervening non-viewport event preserves the next read"
)

func jsonData(_ object: Any, pretty: Bool = false) -> Data {
    var options: JSONSerialization.WritingOptions = [.sortedKeys, .withoutEscapingSlashes]
    if pretty { options.insert(.prettyPrinted) }
    var data = try! JSONSerialization.data(withJSONObject: object, options: options)
    data.append(0x0A)
    return data
}

func writeFixtureJSONL(_ objects: [[String: Any]], to url: URL) {
    var data = Data()
    for object in objects { data.append(jsonData(object)) }
    try! data.write(to: url)
}

func readFixtureJSONL(_ url: URL) -> [[String: Any]] {
    let text = try! String(contentsOf: url, encoding: .utf8)
    return text.split(separator: "\n").map {
        try! JSONSerialization.jsonObject(with: Data($0.utf8)) as! [String: Any]
    }
}

func observation(
    _ value: String,
    at timestamp: String,
    selectionLocation: Int? = nil,
    selectionLength: Int = 0
) -> [String: Any] {
    [
        "observationID": UUID().uuidString,
        "observedAt": timestamp,
        "value": value,
        "valueWasTruncated": false,
        "selectedRangeLocation": selectionLocation ?? value.utf16.count,
        "selectedRangeLength": selectionLength,
    ]
}

func rawAttempt(
    id: String,
    eventID: String,
    before: String,
    after: String,
    timestamp: String,
    selectionLocation: Int? = nil,
    selectionLength: Int = 0,
    rangeCursor: [String: String]? = nil
) -> [String: Any] {
    let terminal = observation(after, at: timestamp)
    var beforeObservation = observation(
        before,
        at: timestamp,
        selectionLocation: selectionLocation,
        selectionLength: selectionLength
    )
    if let rangeCursor {
        beforeObservation["axRangeCursorProbe"] = [
            "capturedAt": timestamp,
            "durationMilliseconds": 1,
            "requestedSurroundingCharacterCount": 512,
            "numberOfCharacters": before.count,
            "left": [
                "rangeLocation": 0, "rangeLength": 0,
                "text": rangeCursor["left"] ?? "", "textWasTruncated": false,
            ],
            "selected": [
                "rangeLocation": 0, "rangeLength": 0,
                "text": rangeCursor["selected"] ?? "", "textWasTruncated": false,
            ],
            "right": [
                "rangeLocation": 0, "rangeLength": before.count,
                "text": rangeCursor["right"] ?? "", "textWasTruncated": false,
            ],
            "errors": [],
        ]
    }
    return [
        "recordType": "active_tap_write_attempt",
        "recordID": id,
        "resolution": "validated",
        "proposedEventID": eventID,
        "derivationObservationSource": "terminal_after",
        "targetIdentity": [
            "role": "AXTextArea",
            "subrole": "AXStandardTextArea",
            "accessibilityIdentifier": "fixture-editor",
            "fieldLabel": "Fixture body",
        ],
        "before": beforeObservation,
        "after": terminal,
        "returnCheckpoints": [],
        "pasteCheckpoints": [],
        "mutationCheckpoints": [[
            "checkpointID": UUID().uuidString,
            "inputObservedAt": timestamp,
            "eventTimestampNanoseconds": 1,
            "captureRequestedAt": timestamp,
            "observation": terminal,
            "axErrors": [],
        ]],
    ]
}

func writeEvent(
    id: String,
    sourceID: String,
    sequence: Int,
    began: String,
    available: String,
    before: String,
    inserted: String,
    removed: String = "",
    offset: Int
) -> [String: Any] {
    [
        "sessionID": "fixture-session",
        "kind": "write",
        "sequence": sequence,
        "eventID": id,
        "beganAt": began,
        "lastInputAt": began,
        "terminalDecisionAt": available,
        "operation": removed.isEmpty ? "insert" : (inserted.isEmpty ? "delete" : "replace"),
        "content": inserted,
        "removedContent": removed,
        "characterOffset": offset,
        "provenance": "active_tap_accessibility_diff",
        "boundaryReason": "write_delay_elapsed",
        "appName": "Fixture",
        "bundleIdentifier": "fixture.app",
        "windowTitle": "Fixture",
        "processIdentifier": 42,
        "sourceRecordIDs": [sourceID],
    ]
}

let fixtureRoot = FileManager.default.temporaryDirectory
    .appendingPathComponent("coupled-causal-check-\(UUID().uuidString)")
let fixtureInput = fixtureRoot.appendingPathComponent("input")
let fixtureOutput = fixtureRoot.appendingPathComponent("output")
try! FileManager.default.createDirectory(at: fixtureInput, withIntermediateDirectories: true)
try! jsonData([
    "sessionID": "fixture-session",
    "schemas": ["timingSemanticsVersion": 2],
], pretty: true).write(to: fixtureInput.appendingPathComponent("session.json"))

let readLate: [String: Any] = [
    "sessionID": "fixture-session", "kind": "read", "sequence": 1,
    "firstActivityAt": "2026-01-01T00:00:01.000Z",
    "lastActivityAt": "2026-01-01T00:00:01.900Z",
    "capturedAt": "2026-01-01T00:00:03.000Z", "content": "late",
    "processIdentifier": 42,
    "provenance": "screen_ocr", "sourceRecordIDs": ["raw-read-late"],
]
let firstWrite = writeEvent(
    id: "write-1", sourceID: "attempt-1", sequence: 1,
    began: "2026-01-01T00:00:02.000Z", available: "2026-01-01T00:00:05.000Z",
    before: "hello world", inserted: "HELLO", removed: "hello", offset: 0
)
let readEarly: [String: Any] = [
    "sessionID": "fixture-session", "kind": "read", "sequence": 2,
    "capturedAt": "2026-01-01T00:00:01.000Z", "content": "early",
    "provenance": "screen_ocr", "sourceRecordIDs": ["raw-read-early"],
]
let excludedModelRead: [String: Any] = [
    "sessionID": "fixture-session", "kind": "read", "sequence": 3,
    "capturedAt": "2026-01-01T00:00:01.500Z", "content": "model suggestion",
    "provenance": "displayed_model_prediction", "phase1Eligible": false,
    "sourceRecordIDs": ["raw-model-read"],
]
let secondWrite = writeEvent(
    id: "write-2", sourceID: "attempt-2", sequence: 2,
    began: "2026-01-01T00:00:06.000Z", available: "2026-01-01T00:00:07.000Z",
    before: "HELLO world", inserted: "world!", removed: "world", offset: 6
)
let cursorMovedWrite = writeEvent(
    id: "write-cursor-moved", sourceID: "attempt-cursor-moved", sequence: 3,
    began: "2026-01-01T00:00:08.000Z", available: "2026-01-01T00:00:09.000Z",
    before: "abc", inserted: "X", offset: 0
)
let deletionWrite = writeEvent(
    id: "write-deletion", sourceID: "attempt-deletion", sequence: 4,
    began: "2026-01-01T00:00:10.000Z", available: "2026-01-01T00:00:11.000Z",
    before: "AB", inserted: "", removed: "AB", offset: 0
)
let invalidWrite = writeEvent(
    id: "write-invalid", sourceID: "attempt-invalid", sequence: 5,
    began: "2026-01-01T00:00:12.000Z", available: "2026-01-01T00:00:13.000Z",
    before: "AB", inserted: "", removed: "AB", offset: 0
)
var revertedWrite = writeEvent(
    id: "write-reverted", sourceID: "attempt-reverted", sequence: 6,
    began: "2026-01-01T00:00:14.000Z", available: "2026-01-01T00:00:17.000Z",
    before: "A", inserted: "X", offset: 1
)
revertedWrite["fallbackReason"] = "terminal_matches_before"
revertedWrite["derivationObservationSource"] = "post_input_checkpoint"
revertedWrite["usedCheckpointID"] = "reverted-checkpoint"
writeFixtureJSONL(
    [
        readLate, firstWrite, readEarly, excludedModelRead, secondWrite,
        cursorMovedWrite, deletionWrite, invalidWrite, revertedWrite,
    ],
    to: fixtureInput.appendingPathComponent("events.jsonl")
)
writeFixtureJSONL([
    rawAttempt(
        id: "attempt-1", eventID: "write-1",
        before: "hello world", after: "HELLO world",
        timestamp: "2026-01-01T00:00:05.000Z",
        selectionLocation: 0,
        selectionLength: 5
    ),
    rawAttempt(
        id: "attempt-2", eventID: "write-2",
        before: "HELLO world", after: "HELLO world!",
        timestamp: "2026-01-01T00:00:07.000Z"
    ),
    rawAttempt(
        id: "attempt-cursor-moved", eventID: "write-cursor-moved",
        before: "abc", after: "Xabc",
        timestamp: "2026-01-01T00:00:09.000Z",
        selectionLocation: 3,
        rangeCursor: ["left": "", "selected": "", "right": "abc"]
    ),
    rawAttempt(
        id: "attempt-deletion", eventID: "write-deletion",
        before: "AB", after: "",
        timestamp: "2026-01-01T00:00:11.000Z",
        selectionLocation: 0,
        selectionLength: 2
    ),
    rawAttempt(
        id: "attempt-invalid", eventID: "write-invalid", before: "AB", after: "AC",
        timestamp: "2026-01-01T00:00:09.000Z"
    ),
    [
        "recordType": "active_tap_write_attempt",
        "recordID": "attempt-reverted",
        "resolution": "validated",
        "proposedEventID": "write-reverted",
        "fallbackReason": "terminal_matches_before",
        "derivationObservationSource": "post_input_checkpoint",
        "usedCheckpointID": "reverted-checkpoint",
        "targetIdentity": ["role": "AXTextArea"],
        "before": observation("A", at: "2026-01-01T00:00:14.000Z"),
        "after": observation("A", at: "2026-01-01T00:00:17.000Z"),
        "returnCheckpoints": [],
        "pasteCheckpoints": [],
        "mutationCheckpoints": [[
            "checkpointID": "reverted-checkpoint",
            "inputObservedAt": "2026-01-01T00:00:15.000Z",
            "eventTimestampNanoseconds": 1,
            "captureRequestedAt": "2026-01-01T00:00:15.000Z",
            "observation": observation("AX", at: "2026-01-01T00:00:15.000Z"),
            "axErrors": [],
        ]],
    ],
], to: fixtureInput.appendingPathComponent("raw.jsonl"))

let compilerResult = try! CausalDatasetCompiler().compile(
    inputDirectory: fixtureInput,
    outputDirectory: fixtureOutput
)
expect(compilerResult.sourceEventCount == 9, "compiler source count")
expect(compilerResult.convertedEventCount == 5, "compiler excludes invalid and stale events")
expect(compilerResult.exampleCount == 3, "compiler creates one example per verified write")
expect(compilerResult.targetExcludedEventCount == 1, "compiler separates valid history from targets")
expect(compilerResult.contextExcludedEventCount == 1, "compiler removes a read superseded by later keyboard input")
expect(compilerResult.rejectedEventCount == 3, "compiler records exclusions and rejections")

let examples = readFixtureJSONL(fixtureOutput.appendingPathComponent("examples.jsonl"))
let compiledEvents = readFixtureJSONL(fixtureOutput.appendingPathComponent("events.jsonl"))
let compactRead = try! JSONSerialization.jsonObject(
    with: Data((compiledEvents[0]["serialized"] as! String).utf8)
) as! [String: Any]
let auditRead = try! JSONSerialization.jsonObject(
    with: Data((compiledEvents[0]["auditSerialized"] as! String).utf8)
) as! [String: Any]
expect(
    compactRead["kind"] as! String == "read"
        && compactRead["source"] == nil
        && compactRead["schemaVersion"] == nil
        && compactRead["bundleIdentifier"] == nil
        && compactRead["provenance"] == nil
        && auditRead["bundleIdentifier"] != nil
        && auditRead["provenance"] != nil,
    "model history is compact while the converted audit projection retains sensor metadata"
)
expect(
    examples[0]["contextEventIDs"] as! [String] == ["fixture-session:read:2"],
    "future read is excluded despite earlier emission"
)
expect(
    examples[1]["contextEventIDs"] as! [String]
        == ["fixture-session:read:2", "write-1"],
    "context is stably ordered by causal availability"
)
expect(
    (examples[0]["targetMask"] as! [String: Any])["type"] as! String
        == "authored_text_and_paste_actions_plus_eos",
    "authored text, paste actions, and one EOS receive loss"
)
expect(
    (examples[0]["targetMask"] as! [String: Any])["eosTokenCount"] as! Int == 1
        && (examples[0]["targetMask"] as! [String: Any])["eosReceivesLoss"] as! Bool,
    "loader appends exactly one loss-bearing EOS token"
)
let firstQuery = try! JSONSerialization.jsonObject(
    with: Data((examples[0]["query"] as! String).utf8)
) as! [String: Any]
let firstCursor = firstQuery["cursorContext"] as! [String: Any]
expect(
    firstCursor["selectionStartCharacters"] as! Int == 0
        && firstCursor["selectedText"] as! String == "hello"
        && firstCursor["rightContext"] as! String == " world",
    "compiler conditions on semantic pre-mutation cursor context"
)
expect(
    ((examples[0]["target"] as! [String: Any])["resolvedContent"] as! String) == "HELLO"
        && (examples[0]["targetMetadata"] as! [String: Any])["characterOffset"] as! Int == 0,
    "Phase 1 target is exact written content while edit metadata remains available"
)
expect(
    examples[0]["modelInput"] as! String
        == (examples[0]["context"] as! String) + "\n" + (examples[0]["query"] as! String),
    "model input is the causal history followed by known conditioning state"
)
let targetExclusions = readFixtureJSONL(
    fixtureOutput.appendingPathComponent("target-exclusions.jsonl")
)
let rangeQuery = try! JSONSerialization.jsonObject(
    with: Data((examples[2]["query"] as! String).utf8)
) as! [String: Any]
let rangeCursor = rangeQuery["cursorContext"] as! [String: Any]
expect(
    rangeCursor["leftContext"] as! String == ""
        && rangeCursor["rightContext"] as! String == "abc"
        && ((examples[2]["target"] as! [String: Any])["resolvedContent"] as! String) == "X",
    "range-native semantic context admits a target independently of numeric cursor mismatch"
)

let mixedAuthorship = writeAuthorship(
    overallEdit: TextEdit(
        operation: .insert,
        characterOffset: 4,
        removed: "",
        inserted: "please review COPIED tomorrow"
    ),
    pasteMutations: [ProvenPasteMutation(
        checkpointID: "paste-1",
        clipboardSnapshotID: "clipboard-1",
        characterOffset: 18,
        inserted: "COPIED"
    )]
)
expect(
    mixedAuthorship.resolution == "resolved"
        && mixedAuthorship.segments.map(\.type)
            == ["authored_text", "paste", "authored_text"]
        && mixedAuthorship.segments.map(\.content)
            == ["please review ", "COPIED", " tomorrow"],
    "mixed writes preserve exact resolved text with paste provenance"
)
let loadedTarget = try! loadPhase1Target(
    segments: mixedAuthorship.segments,
    pasteMarker: "<|paste|>",
    eosTokenID: 901,
    tokenizeOrdinaryText: { Array($0.utf8).map(Int.init) }
)
let expectedLoadedTarget = Array("please review <|paste|> tomorrow".utf8).map(Int.init) + [901]
expect(
    loadedTarget.tokenIDs == expectedLoadedTarget
        && loadedTarget.lossMask.allSatisfy { $0 },
    "loader encodes the paste marker with the existing tokenizer and appends one loss-bearing EOS"
)
expect(
    targetExclusions.contains { $0["reason"] as? String == "empty_content" },
    "pure deletion remains history but is excluded as a content target"
)
let rejections = readFixtureJSONL(fixtureOutput.appendingPathComponent("rejections.jsonl"))
let contextExclusions = readFixtureJSONL(
    fixtureOutput.appendingPathComponent("context-exclusions.jsonl")
)
expect(
    contextExclusions.count == 1
        && contextExclusions[0]["reason"] as? String == "read_candidate_superseded_by_write"
        && contextExclusions[0]["supersedingWriteEventID"] as? String == "write-1",
    "stale delayed read remains explicit audit evidence"
)
expect(
    rejections.contains {
        $0["reason"] as? String == "derived_edit_does_not_reconstruct_used_observation"
    },
    "raw reconstruction catches corrupt targets"
)
expect(
    rejections.contains {
        $0["reason"] as? String == "explicitly_excluded_from_phase1"
    },
    "explicit Phase 1 exclusions never enter context"
)
expect(
    rejections.contains {
        $0["reason"] as? String == "checkpoint_edit_reverted_before_settlement"
    },
    "a typed checkpoint which returns to BEFORE is not resurrected as a write"
)

let pasteFixtureInput = fixtureRoot.appendingPathComponent("paste-input")
let pasteFixtureOutput = fixtureRoot.appendingPathComponent("paste-output")
try! FileManager.default.createDirectory(
    at: pasteFixtureInput,
    withIntermediateDirectories: true
)
try! jsonData([
    "sessionID": "paste-session",
    "schemas": ["timingSemanticsVersion": 2],
], pretty: true).write(to: pasteFixtureInput.appendingPathComponent("session.json"))
var pasteRaw = rawAttempt(
    id: "paste-attempt", eventID: "paste-write",
    before: "", after: "please COPIED tomorrow",
    timestamp: "2026-01-01T00:00:02.000Z",
    rangeCursor: ["left": "", "selected": "", "right": ""]
)
let pasteBefore = pasteRaw["before"] as! [String: Any]
let clipboard: [String: Any] = [
    "schemaVersion": 1, "snapshotID": "clipboard-1",
    "capturedAt": "2026-01-01T00:00:01.000Z", "changeCount": 7,
    "types": ["public.utf8-plain-text"], "text": "COPIED",
    "textSHA256": "fixture", "textWasTruncated": false,
]
let conditioning: [String: Any] = [
    "schemaVersion": 3,
    "captureSemantics": "synchronous_before_application_mutation",
    "inputInterceptedAt": "2026-01-01T00:00:01.000Z",
    "capturedAt": "2026-01-01T00:00:01.000Z",
    "destination": ["appName": "Fixture", "role": "AXTextArea"],
    "cursorContext": [
        "schemaVersion": 2, "source": "accessibility_string_for_range",
        "captureStatus": "complete", "fieldState": "editable_text",
        "leftContext": "", "selectedText": "", "rightContext": "",
    ],
    "clipboard": clipboard,
    "sourceObservationID": pasteBefore["observationID"] as! String,
]
pasteRaw["conditioningState"] = conditioning
pasteRaw["authorshipResolution"] = "resolved"
pasteRaw["authorshipSegments"] = [
    ["type": "authored_text", "content": "please "],
    [
        "type": "paste", "content": "COPIED",
        "clipboardSnapshotID": "clipboard-1", "pasteCheckpointID": "paste-1",
    ],
    ["type": "authored_text", "content": " tomorrow"],
]
var pasteEvent = writeEvent(
    id: "paste-write", sourceID: "paste-attempt", sequence: 1,
    began: "2026-01-01T00:00:01.000Z", available: "2026-01-01T00:00:02.000Z",
    before: "", inserted: "please COPIED tomorrow", offset: 0
)
pasteEvent["sessionID"] = "paste-session"
pasteEvent["conditioningState"] = conditioning
pasteEvent["authorshipResolution"] = "resolved"
pasteEvent["authorshipSegments"] = pasteRaw["authorshipSegments"]
var laterEvent = writeEvent(
    id: "later-write", sourceID: "later-attempt", sequence: 2,
    began: "2026-01-01T00:00:03.000Z", available: "2026-01-01T00:00:04.000Z",
    before: "please COPIED tomorrow", inserted: "!", offset: 22
)
laterEvent["sessionID"] = "paste-session"
writeFixtureJSONL([pasteEvent, laterEvent], to: pasteFixtureInput.appendingPathComponent("events.jsonl"))
writeFixtureJSONL([
    pasteRaw,
    rawAttempt(
        id: "later-attempt", eventID: "later-write",
        before: "please COPIED tomorrow", after: "please COPIED tomorrow!",
        timestamp: "2026-01-01T00:00:04.000Z"
    ),
], to: pasteFixtureInput.appendingPathComponent("raw.jsonl"))
_ = try! CausalDatasetCompiler().compile(
    inputDirectory: pasteFixtureInput,
    outputDirectory: pasteFixtureOutput
)
let pasteExamples = readFixtureJSONL(
    pasteFixtureOutput.appendingPathComponent("examples.jsonl")
)
let pasteTargetSegments = ((pasteExamples[0]["target"] as! [String: Any])["segments"]
    as! [[String: Any]])
let pasteCompiledEvents = readFixtureJSONL(
    pasteFixtureOutput.appendingPathComponent("events.jsonl")
)
let compactPasteWrite = try! JSONSerialization.jsonObject(
    with: Data((pasteCompiledEvents[0]["serialized"] as! String).utf8)
) as! [String: Any]
let auditPasteWrite = try! JSONSerialization.jsonObject(
    with: Data((pasteCompiledEvents[0]["auditSerialized"] as! String).utf8)
) as! [String: Any]
let compactDestination = compactPasteWrite["destination"] as! [String: Any]
let compactPasteSegments = compactPasteWrite["authorshipSegments"] as! [[String: Any]]
let auditPasteSegments = auditPasteWrite["authorshipSegments"] as! [[String: Any]]
expect(
    pasteTargetSegments[1]["type"] as! String == "paste"
        && pasteTargetSegments[1]["content"] == nil,
    "current target omits pasted payload and preserves a grounded paste action"
)
expect(
    compactDestination["application"] as! String == "Fixture"
        && compactPasteWrite["operation"] as! String == "insert"
        && compactPasteWrite["content"] == nil
        && compactPasteSegments[1]["content"] as! String == "COPIED"
        && compactPasteSegments[1]["clipboardSnapshotID"] == nil
        && compactPasteWrite["characterOffset"] == nil
        && auditPasteSegments[1]["clipboardSnapshotID"] as! String == "clipboard-1"
        && auditPasteWrite["content"] as! String == "please COPIED tomorrow"
        && auditPasteWrite["characterOffset"] != nil,
    "compact WRITE history preserves semantics while audit history retains reconstruction evidence"
)
expect(
    (pasteExamples[1]["context"] as! String).contains("COPIED")
        && (pasteExamples[1]["context"] as! String).contains("authorshipSegments"),
    "later history retains resolved pasted content and paste provenance"
)

let opaquePasteInput = fixtureRoot.appendingPathComponent("opaque-paste-input")
let opaquePasteOutput = fixtureRoot.appendingPathComponent("opaque-paste-output")
try! FileManager.default.createDirectory(at: opaquePasteInput, withIntermediateDirectories: true)
try! jsonData([
    "sessionID": "opaque-paste-session",
    "schemas": ["timingSemanticsVersion": 2],
], pretty: true).write(to: opaquePasteInput.appendingPathComponent("session.json"))
var opaqueRaw = rawAttempt(
    id: "opaque-paste-attempt", eventID: "opaque-paste-write",
    before: "", after: " after",
    timestamp: "2026-01-01T00:00:02.000Z",
    rangeCursor: ["left": "", "selected": "", "right": ""]
)
let opaqueBefore = opaqueRaw["before"] as! [String: Any]
let opaqueClipboard: [String: Any] = [
    "schemaVersion": 1, "snapshotID": "opaque-clipboard",
    "capturedAt": "2026-01-01T00:00:01.000Z", "changeCount": 9,
    "types": ["public.utf8-plain-text"], "text": "COPIED",
    "textSHA256": "fixture", "textWasTruncated": false,
]
let opaqueConditioning: [String: Any] = [
    "schemaVersion": 3,
    "captureSemantics": "synchronous_before_application_mutation",
    "inputInterceptedAt": "2026-01-01T00:00:01.000Z",
    "capturedAt": "2026-01-01T00:00:01.000Z",
    "destination": [
        "appName": "Fixture", "bundleIdentifier": "fixture.app",
        "processIdentifier": 42, "windowTitle": "Fixture",
        "role": "AXTextArea", "subrole": "AXStandardTextArea",
        "fieldIdentifier": "fixture-editor", "fieldLabel": "Fixture body",
    ],
    "cursorContext": [
        "schemaVersion": 2, "source": "accessibility_string_for_range",
        "captureStatus": "complete", "fieldState": "editable_text",
        "leftContext": "", "selectedText": "", "rightContext": "",
    ],
    "clipboard": opaqueClipboard,
    "sourceObservationID": opaqueBefore["observationID"] as! String,
]
let opaqueSegments: [[String: Any]] = [
    ["type": "authored_text", "content": "before "],
    [
        "type": "paste", "content": "COPIED",
        "clipboardSnapshotID": "opaque-clipboard",
        "pasteCheckpointID": "opaque-paste-checkpoint",
    ],
    ["type": "authored_text", "content": " after"],
]
opaqueRaw["conditioningState"] = opaqueConditioning
opaqueRaw["authorshipResolution"] = "resolved"
opaqueRaw["authorshipEvidence"] = "grounded_paste_ax_epoch_transition"
opaqueRaw["authorshipSegments"] = opaqueSegments
opaqueRaw["resolvedCompletion"] = "before COPIED after"
opaqueRaw["stateContinuity"] = "segmented_at_grounded_paste"
opaqueRaw["observedNetEdit"] = [
    "operation": "insert", "content": " after",
    "removedContent": "", "characterOffset": 0,
]
opaqueRaw["pasteCheckpoints"] = [[
    "checkpointID": "opaque-paste-checkpoint",
    "clipboardSnapshotID": "opaque-clipboard",
    "clipboardChangeCount": 9,
    "clipboardText": "COPIED",
    "clipboardTextWasTruncated": false,
    "prePasteAXErrors": [],
    "axErrors": [],
    "prePasteObservation": observation(
        "before ", at: "2026-01-01T00:00:01.200Z", selectionLocation: 7
    ),
    "observation": observation(
        "", at: "2026-01-01T00:00:01.300Z", selectionLocation: 0
    ),
]]
var opaqueEvent = writeEvent(
    id: "opaque-paste-write", sourceID: "opaque-paste-attempt", sequence: 1,
    began: "2026-01-01T00:00:01.000Z", available: "2026-01-01T00:00:02.000Z",
    before: "", inserted: " after", offset: 0
)
opaqueEvent["sessionID"] = "opaque-paste-session"
opaqueEvent["conditioningState"] = opaqueConditioning
opaqueEvent["authorshipResolution"] = "resolved"
opaqueEvent["authorshipEvidence"] = "grounded_paste_ax_epoch_transition"
opaqueEvent["authorshipSegments"] = opaqueSegments
opaqueEvent["resolvedCompletion"] = "before COPIED after"
opaqueEvent["stateContinuity"] = "segmented_at_grounded_paste"
opaqueEvent["observedNetEdit"] = opaqueRaw["observedNetEdit"]
writeFixtureJSONL([opaqueEvent], to: opaquePasteInput.appendingPathComponent("events.jsonl"))
writeFixtureJSONL([opaqueRaw], to: opaquePasteInput.appendingPathComponent("raw.jsonl"))
_ = try! CausalDatasetCompiler().compile(
    inputDirectory: opaquePasteInput,
    outputDirectory: opaquePasteOutput
)
let opaquePasteExamples = readFixtureJSONL(
    opaquePasteOutput.appendingPathComponent("examples.jsonl")
)
let opaqueTarget = opaquePasteExamples[0]["target"] as! [String: Any]
let opaqueTargetSegments = opaqueTarget["segments"] as! [[String: Any]]
expect(
    opaqueTarget["resolvedContent"] as! String == "before COPIED after"
        && opaqueTargetSegments.map { $0["type"] as! String }
            == ["authored_text", "paste", "authored_text"]
        && opaqueTargetSegments[1]["content"] == nil,
    "grounded AX epoch transition preserves authored prefix, paste action, and suffix"
)

// Raw-first architecture: finalized semantics are deterministic and independent
// of the collector's provisional preview artifact.
let rawFirstInput = fixtureRoot.appendingPathComponent("raw-first-input")
let rawFirstReductionA = fixtureRoot.appendingPathComponent("raw-first-reduction-a")
let rawFirstReductionB = fixtureRoot.appendingPathComponent("raw-first-reduction-b")
let rawFirstDataset = fixtureRoot.appendingPathComponent("raw-first-dataset")
try! FileManager.default.createDirectory(at: rawFirstInput, withIntermediateDirectories: true)
try! jsonData([
    "sessionID": "raw-first-session",
    "schemas": ["timingSemanticsVersion": 2],
], pretty: true).write(to: rawFirstInput.appendingPathComponent("session.json"))
let rawFirstBefore = observation("", at: "2026-01-01T00:00:01.000Z", selectionLocation: 0)
let rawFirstAfter = observation("hello", at: "2026-01-01T00:00:04.000Z", selectionLocation: 5)
let rawFirstConditioning: [String: Any] = [
    "schemaVersion": 3,
    "captureSemantics": "synchronous_before_application_mutation",
    "inputInterceptedAt": "2026-01-01T00:00:01.000Z",
    "capturedAt": "2026-01-01T00:00:01.000Z",
    "destination": [
        "appName": "Fixture", "bundleIdentifier": "fixture.app",
        "processIdentifier": 42, "windowTitle": "Fixture", "role": "AXTextArea",
    ],
    "cursorContext": [
        "schemaVersion": 2, "source": "accessibility_string_for_range",
        "captureStatus": "complete", "fieldState": "editable_text",
        "leftContext": "", "selectedText": "", "rightContext": "",
    ],
    "sourceObservationID": rawFirstBefore["observationID"] as! String,
]
let rawFirstAttempt: [String: Any] = [
    "schemaVersion": 15, "recordType": "active_tap_write_attempt",
    "recordID": "raw-first-attempt", "sessionID": "raw-first-session",
    "bundleIdentifier": "fixture.app", "observedAt": "2026-01-01T00:00:04.000Z",
    "beganAt": "2026-01-01T00:00:01.000Z",
    "lastInputAt": "2026-01-01T00:00:01.000Z",
    "terminalDecisionAt": "2026-01-01T00:00:04.000Z",
    "terminalSnapshotAt": "2026-01-01T00:00:04.000Z",
    "configuredWriteDelaySeconds": 3, "firstEventTimestampNanoseconds": 1,
    "lastEventTimestampNanoseconds": 1, "inputEventCount": 1,
    "inputHints": ["typed"],
    "inputEvents": [[
        "observedAt": "2026-01-01T00:00:01.000Z",
        "eventTimestampNanoseconds": 1, "hint": "typed", "mutationCapable": true,
    ]],
    "boundaryReason": "write_delay_elapsed", "conditioningState": rawFirstConditioning,
    "targetIdentity": [
        "bundleIdentifier": "fixture.app", "processIdentifier": 42,
        "role": "AXTextArea", "windowTitle": "Fixture",
    ],
    "before": rawFirstBefore, "after": rawFirstAfter,
    "returnCheckpoints": [], "pasteCheckpoints": [],
    "mutationCheckpoints": [[
        "checkpointID": "raw-first-mutation", "inputObservedAt": "2026-01-01T00:00:01.000Z",
        "eventTimestampNanoseconds": 1, "captureRequestedAt": "2026-01-01T00:00:01.050Z",
        "observation": rawFirstAfter, "axErrors": [],
    ]],
    "beforeAXErrors": [], "afterAXErrors": [], "tapTimeoutCountDuringBurst": 0,
]
writeFixtureJSONL([rawFirstAttempt], to: rawFirstInput.appendingPathComponent("raw.jsonl"))
try! Data("deliberately corrupt preview\n".utf8).write(
    to: rawFirstInput.appendingPathComponent("events.preview.jsonl")
)
let reducer = Phase1SemanticReducer()
_ = try! reducer.reduce(sourceDirectory: rawFirstInput, outputDirectory: rawFirstReductionA)
try! Data("different corrupt preview\n".utf8).write(
    to: rawFirstInput.appendingPathComponent("events.preview.jsonl")
)
_ = try! reducer.reduce(sourceDirectory: rawFirstInput, outputDirectory: rawFirstReductionB)
expect(
    try! Data(contentsOf: rawFirstReductionA.appendingPathComponent("events.jsonl"))
        == Data(contentsOf: rawFirstReductionB.appendingPathComponent("events.jsonl")),
    "semantic reduction is deterministic and preview-independent"
)
let rawFirstEvents = readFixtureJSONL(rawFirstReductionA.appendingPathComponent("events.jsonl"))
expect(
    rawFirstEvents.count == 1
        && rawFirstEvents[0]["content"] as! String == "hello"
        && (rawFirstEvents[0]["reduction"] as! [String: Any])["selectedObservationID"] as! String
            == rawFirstAfter["observationID"] as! String,
    "finalized write embeds its raw observation decision and lineage"
)
_ = try! CausalDatasetCompiler().compile(
    inputDirectory: rawFirstReductionA,
    sourceDirectory: rawFirstInput,
    outputDirectory: rawFirstDataset
)
expect(
    readFixtureJSONL(rawFirstDataset.appendingPathComponent("examples.jsonl")).count == 1,
    "causal compiler consumes a hash-verified finalized reduction"
)

let motivatingInput = fixtureRoot.appendingPathComponent("motivating-input")
let motivatingReduction = fixtureRoot.appendingPathComponent("motivating-reduction")
try! FileManager.default.createDirectory(at: motivatingInput, withIntermediateDirectories: true)
try! jsonData([
    "sessionID": "motivating-session",
    "schemas": ["timingSemanticsVersion": 2, "rawActiveTapWrite": 15],
], pretty: true).write(to: motivatingInput.appendingPathComponent("session.json"))

func rawScreenFixture(
    id: String, capturedAt: String, content: String,
    triggerAt: String? = nil
) -> [String: Any] {
    let activityAt = triggerAt ?? capturedAt
    return [
        "schemaVersion": 6, "recordType": "screen_ocr_observation",
        "recordID": id, "sessionID": "motivating-session",
        "observedAt": capturedAt, "settledAt": capturedAt, "capturedAt": capturedAt,
        "surfaceResolvedAt": capturedAt, "firstActivityAt": activityAt,
        "lastActivityAt": activityAt, "readDelaySeconds": 3,
        "triggerTypes": ["scroll"], "eventCount": 1,
        "content": content,
        "recognizedLineCount": content.split(separator: "\n").count,
        "contentWasTruncated": false,
        "viewportSideCropFraction": 0.1, "viewportTopCropFraction": 0.1,
        "viewportBottomCropFraction": 0.35,
        "windowBounds": ["x": 0, "y": 0, "width": 1000, "height": 800],
        "captureBounds": ["x": 100, "y": 80, "width": 800, "height": 440],
        "x": 500, "y": 400, "displayID": 1,
        "displayBounds": ["x": 0, "y": 0, "width": 1000, "height": 800],
        "windowID": 7, "windowTitle": "Fixture", "appName": "Fixture",
        "bundleIdentifier": "fixture.app", "processIdentifier": 42,
        "triggerSurface": [
            "resolvedAt": activityAt, "displayID": 1,
            "displayBounds": ["x": 0, "y": 0, "width": 1000, "height": 800],
            "windowID": 7, "windowTitle": "Fixture",
            "windowBounds": ["x": 0, "y": 0, "width": 1000, "height": 800],
            "appName": "Fixture", "bundleIdentifier": "fixture.app",
            "processIdentifier": 42,
        ],
    ]
}

func schema15WriteFixture(
    id: String,
    beforeValue: String,
    afterValue: String,
    beganAt: String,
    terminalAt: String,
    inputHints: [String],
    returnCheckpoints: [[String: Any]] = [],
    pasteCheckpoints: [[String: Any]] = [],
    mutationCheckpoints: [[String: Any]]? = nil
) -> [String: Any] {
    let before = observation(beforeValue, at: beganAt)
    let after = observation(afterValue, at: terminalAt)
    let conditioning: [String: Any] = [
        "schemaVersion": 3,
        "captureSemantics": "synchronous_before_application_mutation",
        "inputInterceptedAt": beganAt, "capturedAt": beganAt,
        "destination": [
            "appName": "Fixture", "bundleIdentifier": "fixture.app",
            "processIdentifier": 42, "windowTitle": "Fixture", "role": "AXTextArea",
        ],
        "cursorContext": [
            "schemaVersion": 2, "source": "accessibility_string_for_range",
            "captureStatus": "complete", "fieldState": "editable_text",
            "leftContext": beforeValue, "selectedText": "", "rightContext": "",
        ],
        "sourceObservationID": before["observationID"] as! String,
    ]
    let inputEvents = inputHints.enumerated().map { index, hint in
        [
            "observedAt": beganAt,
            "eventTimestampNanoseconds": index + 1,
            "hint": hint,
            "mutationCapable": true,
        ] as [String: Any]
    }
    return [
        "schemaVersion": 15, "recordType": "active_tap_write_attempt",
        "recordID": id, "sessionID": "motivating-session",
        "bundleIdentifier": "fixture.app", "observedAt": terminalAt,
        "beganAt": beganAt, "lastInputAt": beganAt,
        "terminalDecisionAt": terminalAt, "terminalSnapshotAt": terminalAt,
        "configuredWriteDelaySeconds": 3,
        "firstEventTimestampNanoseconds": 1,
        "lastEventTimestampNanoseconds": inputHints.count,
        "inputEventCount": inputHints.count, "inputHints": inputHints,
        "inputEvents": inputEvents, "boundaryReason": "write_delay_elapsed",
        "conditioningState": conditioning,
        "targetIdentity": [
            "bundleIdentifier": "fixture.app", "processIdentifier": 42,
            "role": "AXTextArea", "windowTitle": "Fixture",
        ],
        "before": before, "after": after,
        "returnCheckpoints": returnCheckpoints,
        "pasteCheckpoints": pasteCheckpoints,
        "mutationCheckpoints": mutationCheckpoints ?? [[
            "checkpointID": "\(id)-mutation", "inputObservedAt": beganAt,
            "eventTimestampNanoseconds": 1, "captureRequestedAt": beganAt,
            "observation": after, "axErrors": [],
        ]],
        "beforeAXErrors": [], "afterAXErrors": [], "tapTimeoutCountDuringBurst": 0,
    ]
}

let semanticTimeWrite = schema15WriteFixture(
    id: "semantic-time-write", beforeValue: "", afterValue: "typed",
    beganAt: "2026-01-01T00:00:02.000Z",
    terminalAt: "2026-01-01T00:00:04.000Z", inputHints: ["typed"]
)
let geminiCheckpoint = observation(
    "What should we build?", at: "2026-01-01T00:00:05.900Z", selectionLocation: 21
)
let geminiWrite = schema15WriteFixture(
    id: "gemini-return-reset", beforeValue: "", afterValue: "Ask Gemini\n",
    beganAt: "2026-01-01T00:00:05.000Z",
    terminalAt: "2026-01-01T00:00:08.000Z", inputHints: ["typed", "return"],
    returnCheckpoints: [[
        "checkpointID": "gemini-return", "inputObservedAt": "2026-01-01T00:00:05.900Z",
        "eventTimestampNanoseconds": 2, "observation": geminiCheckpoint, "axErrors": [],
    ]]
)
let deleteOnlyWrite = schema15WriteFixture(
    id: "impossible-delete", beforeValue: "small", afterValue: "generated document expansion",
    beganAt: "2026-01-01T00:00:09.000Z",
    terminalAt: "2026-01-01T00:00:12.000Z", inputHints: ["delete"]
)
let unresolvedPaste = schema15WriteFixture(
    id: "missing-paste", beforeValue: "", afterValue: "COPIED",
    beganAt: "2026-01-01T00:00:13.000Z",
    terminalAt: "2026-01-01T00:00:16.000Z", inputHints: ["paste"]
)
let repeatedBoundaryBefore = "typing here then typing here again"
let repeatedBoundaryAfter = "typing here then then typing in between here typing here again"
let repeatedBoundaryStates = [
    "typing here thent typing here again",
    repeatedBoundaryBefore,
    "typing here then  typing here again",
    repeatedBoundaryAfter,
]
let repeatedBoundaryWrite = schema15WriteFixture(
    id: "repeated-boundary-write",
    beforeValue: repeatedBoundaryBefore,
    afterValue: repeatedBoundaryAfter,
    beganAt: "2026-01-01T00:00:17.000Z",
    terminalAt: "2026-01-01T00:00:20.000Z",
    inputHints: ["typed", "delete", "typed", "typed"],
    mutationCheckpoints: repeatedBoundaryStates.enumerated().map { index, value in
        let timestamp = "2026-01-01T00:00:17.\(index + 1)00Z"
        return [
            "checkpointID": "repeated-boundary-\(index)",
            "inputObservedAt": timestamp,
            "eventTimestampNanoseconds": index + 1,
            "captureRequestedAt": timestamp,
            "observation": observation(value, at: timestamp),
            "axErrors": [],
        ] as [String: Any]
    }
)

// Deliberately append the READ captured at t=3 before the WRITE which began at
// t=2 but settled at t=4. Raw-order overlap would incorrectly emit only gamma.
writeFixtureJSONL([
    rawScreenFixture(
        id: "read-before-write", capturedAt: "2026-01-01T00:00:01.000Z",
        content: "alpha\nbeta"
    ),
    rawScreenFixture(
        id: "read-after-write-began", capturedAt: "2026-01-01T00:00:03.000Z",
        content: "beta\ngamma"
    ),
    rawScreenFixture(
        id: "stale-delayed-read", capturedAt: "2026-01-01T00:00:03.500Z",
        content: "stale content", triggerAt: "2026-01-01T00:00:01.500Z"
    ),
    semanticTimeWrite, geminiWrite, deleteOnlyWrite, unresolvedPaste,
    repeatedBoundaryWrite,
], to: motivatingInput.appendingPathComponent("raw.jsonl"))

_ = try! reducer.reduce(
    sourceDirectory: motivatingInput,
    outputDirectory: motivatingReduction
)
let motivatingEvents = readFixtureJSONL(
    motivatingReduction.appendingPathComponent("events.jsonl")
)
let motivatingDispositions = readFixtureJSONL(
    motivatingReduction.appendingPathComponent("unresolved.jsonl")
)
expect(
    motivatingEvents.first { $0["eventID"] as? String != nil
        && ($0["sourceRecordIDs"] as? [String]) == ["read-after-write-began"] }?["content"]
        as? String == "beta\ngamma",
    "genuine READ activity during a long WRITE survives semantic overlap reduction"
)
expect(
    motivatingDispositions.contains {
        ($0["sourceRecordIDs"] as? [String]) == ["stale-delayed-read"]
            && $0["reason"] as? String == "read_candidate_superseded_by_write"
            && $0["rule"] as? String == "semantic_time_stale_delayed_read_v1"
    },
    "pointer activity before a WRITE cannot emit a delayed READ inside that WRITE"
)
let recoveredGemini = motivatingEvents.first {
    ($0["sourceRecordIDs"] as? [String]) == ["gemini-return-reset"]
}
expect(
    recoveredGemini?["content"] as? String == "What should we build?"
        && recoveredGemini?["derivationObservationSource"] as? String
            == "pre_return_checkpoint",
    "Return reset recovers exact pre-Return human content"
)
expect(
    motivatingDispositions.contains {
        ($0["sourceRecordIDs"] as? [String]) == ["impossible-delete"]
            && $0["reason"] as? String == "delete_only_transition_inserted_content"
    },
    "delete-only impossible insertion is a non-event disposition"
)
expect(
    motivatingDispositions.contains {
        ($0["sourceRecordIDs"] as? [String]) == ["missing-paste"]
            && $0["reason"] as? String == "paste_checkpoint_missing"
    },
    "paste without grounding remains an explicit non-event disposition"
)
let repeatedBoundaryEvent = motivatingEvents.first {
    ($0["sourceRecordIDs"] as? [String]) == ["repeated-boundary-write"]
}
expect(
    repeatedBoundaryEvent?["content"] as? String == " then typing in between here"
        && repeatedBoundaryEvent?["resolvedCompletion"] as? String
            == " then typing in between here"
        && (repeatedBoundaryEvent?["observedNetEdit"] as? [String: Any])?["content"]
            as? String == "hen typing in between here t"
        && (repeatedBoundaryEvent?["reduction"] as? [String: Any])?["alignmentRule"]
            as? String == "checkpoint_grounded_equivalent_diff_v1",
    "ordered checkpoints resolve repeated-boundary authorship without changing the observed transition"
)

let motivatingDataset = fixtureRoot.appendingPathComponent("motivating-dataset")
_ = try! CausalDatasetCompiler().compile(
    inputDirectory: motivatingReduction,
    sourceDirectory: motivatingInput,
    outputDirectory: motivatingDataset
)
expect(
    readFixtureJSONL(
        motivatingDataset.appendingPathComponent("context-exclusions.jsonl")
    ).isEmpty,
    "finalized reductions resolve stale delayed READs before causal compilation"
)

let tamperedReduction = fixtureRoot.appendingPathComponent("tampered-reduction")
let tamperedDataset = fixtureRoot.appendingPathComponent("tampered-dataset")
try! FileManager.default.copyItem(at: rawFirstReductionA, to: tamperedReduction)
try! Data("tampered\n".utf8).write(
    to: tamperedReduction.appendingPathComponent("events.jsonl")
)
var tamperWasRejected = false
do {
    _ = try CausalDatasetCompiler().compile(
        inputDirectory: tamperedReduction,
        sourceDirectory: rawFirstInput,
        outputDirectory: tamperedDataset
    )
} catch {
    tamperWasRejected = true
}
expect(tamperWasRejected, "compiler rejects a reducer artifact digest mismatch")

let schema15LegacyInput = fixtureRoot.appendingPathComponent("schema15-legacy-input")
let schema15LegacyOutput = fixtureRoot.appendingPathComponent("schema15-legacy-output")
try! FileManager.default.createDirectory(at: schema15LegacyInput, withIntermediateDirectories: true)
try! jsonData([
    "sessionID": "schema15-legacy",
    "schemas": ["timingSemanticsVersion": 2, "rawActiveTapWrite": 15],
], pretty: true).write(to: schema15LegacyInput.appendingPathComponent("session.json"))
writeFixtureJSONL([], to: schema15LegacyInput.appendingPathComponent("events.jsonl"))
writeFixtureJSONL([], to: schema15LegacyInput.appendingPathComponent("raw.jsonl"))
var schema15LegacyWasRejected = false
do {
    _ = try CausalDatasetCompiler().compile(
        inputDirectory: schema15LegacyInput,
        outputDirectory: schema15LegacyOutput
    )
} catch {
    schema15LegacyWasRejected = String(describing: error).contains("requires reduction.json")
}
expect(schema15LegacyWasRejected, "schema 15 cannot use the legacy compiler importer")

try! FileManager.default.removeItem(at: fixtureRoot)

print("CoupledCore checks passed")
