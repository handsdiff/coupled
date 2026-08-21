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
    before: "HELLO world", inserted: "done", offset: 11
)
let cursorMovedWrite = writeEvent(
    id: "write-cursor-moved", sourceID: "attempt-cursor-moved", sequence: 3,
    began: "2026-01-01T00:00:08.000Z", available: "2026-01-01T00:00:09.000Z",
    before: "abc", inserted: "XXXX", offset: 0
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
let shortWrite = writeEvent(
    id: "write-short", sourceID: "attempt-short", sequence: 7,
    began: "2026-01-01T00:00:18.000Z", available: "2026-01-01T00:00:19.000Z",
    before: "", inserted: "for", offset: 0
)
writeFixtureJSONL(
    [
        readLate, firstWrite, readEarly, excludedModelRead, secondWrite,
        cursorMovedWrite, deletionWrite, invalidWrite, revertedWrite, shortWrite,
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
        before: "HELLO world", after: "HELLO worlddone",
        timestamp: "2026-01-01T00:00:07.000Z"
    ),
    rawAttempt(
        id: "attempt-cursor-moved", eventID: "write-cursor-moved",
        before: "abc", after: "XXXXabc",
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
    rawAttempt(
        id: "attempt-short", eventID: "write-short",
        before: "", after: "for",
        timestamp: "2026-01-01T00:00:19.000Z",
        rangeCursor: ["left": "", "selected": "", "right": ""]
    ),
], to: fixtureInput.appendingPathComponent("raw.jsonl"))

let compilerResult = try! CausalDatasetCompiler().compile(
    inputDirectory: fixtureInput,
    outputDirectory: fixtureOutput
)
expect(compilerResult.sourceEventCount == 10, "compiler source count")
expect(compilerResult.convertedEventCount == 6, "compiler excludes invalid and stale events")
expect(compilerResult.exampleCount == 3, "compiler creates one example per verified write")
expect(compilerResult.targetExcludedEventCount == 2, "compiler separates valid history from targets")
expect(compilerResult.contextExcludedEventCount == 1, "compiler removes a read superseded by later keyboard input")
expect(compilerResult.rejectedEventCount == 3, "compiler records exclusions and rejections")

let examples = readFixtureJSONL(fixtureOutput.appendingPathComponent("examples.jsonl"))
let compiledEvents = readFixtureJSONL(fixtureOutput.appendingPathComponent("events.jsonl"))
let compiledManifest = try! JSONSerialization.jsonObject(
    with: Data(contentsOf: fixtureOutput.appendingPathComponent("dataset.json"))
) as! [String: Any]
let compiledEligibility = compiledManifest["eligibility"] as! [String: Any]
expect(
    compiledManifest["conversionVersion"] as! String == "phase1-causal-v14"
        && compiledEligibility["minimumTrimmedAuthoredCharactersForTextOnlyTarget"]
            as! Int == 4
        && compiledEligibility["groundedPasteActionBypassesMinimumAuthoredLength"]
            as! Bool,
    "v14 manifest freezes the text-only minimum and grounded-paste exception"
)
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
        && ((examples[2]["target"] as! [String: Any])["resolvedContent"] as! String) == "XXXX",
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
expect(
    targetExclusions.contains {
        $0["sourceEventID"] as? String == "write-short"
            && $0["reason"] as? String == "authored_content_below_minimum_length"
            && $0["trimmedAuthoredCharacterCount"] as? Int == 3
            && $0["minimumTrimmedAuthoredCharacters"] as? Int == 4
            && $0["hasGroundedPasteAction"] as? Bool == false
    }
        && compiledEvents.contains { $0["sourceEventID"] as? String == "write-short" },
    "short authored text remains causal history but receives no target loss"
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
    before: "", after: "COPIED",
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
    [
        "type": "paste", "content": "COPIED",
        "clipboardSnapshotID": "clipboard-1", "pasteCheckpointID": "paste-1",
    ],
]
var pasteEvent = writeEvent(
    id: "paste-write", sourceID: "paste-attempt", sequence: 1,
    began: "2026-01-01T00:00:01.000Z", available: "2026-01-01T00:00:02.000Z",
    before: "", inserted: "COPIED", offset: 0
)
pasteEvent["sessionID"] = "paste-session"
pasteEvent["conditioningState"] = conditioning
pasteEvent["authorshipResolution"] = "resolved"
pasteEvent["authorshipSegments"] = pasteRaw["authorshipSegments"]
var laterEvent = writeEvent(
    id: "later-write", sourceID: "later-attempt", sequence: 2,
    began: "2026-01-01T00:00:03.000Z", available: "2026-01-01T00:00:04.000Z",
    before: "COPIED", inserted: "done", offset: 6
)
laterEvent["sessionID"] = "paste-session"
writeFixtureJSONL([pasteEvent, laterEvent], to: pasteFixtureInput.appendingPathComponent("events.jsonl"))
writeFixtureJSONL([
    pasteRaw,
    rawAttempt(
        id: "later-attempt", eventID: "later-write",
        before: "COPIED", after: "COPIEDdone",
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
    pasteTargetSegments.count == 1
        && pasteTargetSegments[0]["type"] as! String == "paste"
        && pasteTargetSegments[0]["content"] == nil,
    "paste-only target bypasses authored-length minimum and omits its payload"
)
expect(
    compactDestination["application"] as! String == "Fixture"
        && compactPasteWrite["operation"] as! String == "insert"
        && compactPasteWrite["content"] == nil
        && compactPasteSegments[0]["content"] as! String == "COPIED"
        && compactPasteSegments[0]["clipboardSnapshotID"] == nil
        && compactPasteWrite["characterOffset"] == nil
        && auditPasteSegments[0]["clipboardSnapshotID"] as! String == "clipboard-1"
        && auditPasteWrite["content"] as! String == "COPIED"
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
    before: "", after: " b",
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
    ["type": "authored_text", "content": "a"],
    [
        "type": "paste", "content": "COPIED",
        "clipboardSnapshotID": "opaque-clipboard",
        "pasteCheckpointID": "opaque-paste-checkpoint",
    ],
    ["type": "authored_text", "content": " b"],
]
opaqueRaw["conditioningState"] = opaqueConditioning
opaqueRaw["authorshipResolution"] = "resolved"
opaqueRaw["authorshipEvidence"] = "grounded_paste_ax_epoch_transition"
opaqueRaw["authorshipSegments"] = opaqueSegments
opaqueRaw["resolvedCompletion"] = "aCOPIED b"
opaqueRaw["stateContinuity"] = "segmented_at_grounded_paste"
opaqueRaw["observedNetEdit"] = [
    "operation": "insert", "content": " b",
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
        "a", at: "2026-01-01T00:00:01.200Z", selectionLocation: 1
    ),
    "observation": observation(
        "", at: "2026-01-01T00:00:01.300Z", selectionLocation: 0
    ),
]]
var opaqueEvent = writeEvent(
    id: "opaque-paste-write", sourceID: "opaque-paste-attempt", sequence: 1,
    began: "2026-01-01T00:00:01.000Z", available: "2026-01-01T00:00:02.000Z",
    before: "", inserted: " b", offset: 0
)
opaqueEvent["sessionID"] = "opaque-paste-session"
opaqueEvent["conditioningState"] = opaqueConditioning
opaqueEvent["authorshipResolution"] = "resolved"
opaqueEvent["authorshipEvidence"] = "grounded_paste_ax_epoch_transition"
opaqueEvent["authorshipSegments"] = opaqueSegments
opaqueEvent["resolvedCompletion"] = "aCOPIED b"
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
    opaqueTarget["resolvedContent"] as! String == "aCOPIED b"
        && opaqueTargetSegments.map { $0["type"] as! String }
            == ["authored_text", "paste", "authored_text"]
        && opaqueTargetSegments[1]["content"] == nil,
    "grounded AX epoch transition preserves authored prefix, paste action, and suffix"
)
expect(
    opaquePasteExamples.count == 1,
    "mixed authored and paste target remains eligible regardless of authored fragment length"
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
            "fieldDescription": "Fixture editor", "fieldLabel": "",
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
        "bundleIdentifier": "fixture.app", "processIdentifier": 42,
        "observedAt": terminalAt,
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
            "fieldDescription": "Fixture editor", "fieldLabel": "",
            "elementHash": 12345,
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
    id: "semantic-time-write", beforeValue: "",
    afterValue: "there was a paper about encrypted reasoning blocks",
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

let rangeAlignedBoundaryBefore = "update after reviewing the latest trace"
let rangeAlignedBoundaryStates = [
    "update after reviewing the llatest trace",
    "update after reviewing the lilatest trace",
    "update after reviewing the livlatest trace",
    "update after reviewing the livelatest trace",
    "update after reviewing the live latest trace",
]
var rangeAlignedBoundaryWrite = schema15WriteFixture(
    id: "range-aligned-repeated-boundary",
    beforeValue: rangeAlignedBoundaryBefore,
    afterValue: rangeAlignedBoundaryStates.last!,
    beganAt: "2026-01-01T00:00:20.100Z",
    terminalAt: "2026-01-01T00:00:20.900Z",
    inputHints: Array(repeating: "typed", count: 5),
    mutationCheckpoints: rangeAlignedBoundaryStates.enumerated().map { index, value in
        let timestamp = "2026-01-01T00:00:20.\(index + 2)00Z"
        return [
            "checkpointID": "range-aligned-boundary-\(index)",
            "inputObservedAt": timestamp,
            "eventTimestampNanoseconds": index + 1,
            "captureRequestedAt": timestamp,
            "observation": observation(
                value, at: timestamp, selectionLocation: 28 + index
            ),
            "axErrors": [],
        ] as [String: Any]
    }
)
var rangeAlignedBoundaryBeforeObservation =
    rangeAlignedBoundaryWrite["before"] as! [String: Any]
rangeAlignedBoundaryBeforeObservation["selectedRangeLocation"] = 27
rangeAlignedBoundaryBeforeObservation["selectedRangeLength"] = 0
rangeAlignedBoundaryBeforeObservation["axRangeCursorProbe"] = [
    "capturedAt": "2026-01-01T00:00:20.100Z",
    "durationMilliseconds": 1,
    "requestedSurroundingCharacterCount": 512,
    "numberOfCharacters": rangeAlignedBoundaryBefore.utf16.count,
    "selectedRangeLocation": 27,
    "selectedRangeLength": 0,
    "left": [
        "rangeLocation": 0, "rangeLength": 27,
        "text": "update after reviewing the ", "textWasTruncated": false,
    ],
    "selected": [
        "rangeLocation": 27, "rangeLength": 0,
        "text": "", "textWasTruncated": false,
    ],
    "right": [
        "rangeLocation": 27,
        "rangeLength": rangeAlignedBoundaryBefore.utf16.count - 27,
        "text": "latest trace", "textWasTruncated": false,
    ],
    "errors": [],
]
rangeAlignedBoundaryWrite["before"] = rangeAlignedBoundaryBeforeObservation
var rangeAlignedConditioning =
    rangeAlignedBoundaryWrite["conditioningState"] as! [String: Any]
rangeAlignedConditioning["cursorContext"] = [
    "schemaVersion": 2, "source": "accessibility_string_for_range",
    "captureStatus": "complete", "fieldState": "editable_text",
    "leftContext": "update after reviewing the ",
    "selectedText": "", "rightContext": "latest trace",
]
rangeAlignedBoundaryWrite["conditioningState"] = rangeAlignedConditioning

let epochBefore = String(repeating: "old document ", count: 30)
let epochStableOne = epochBefore + "human though"
let epochStableFinal = epochBefore + "human thought"
let epochTerminal = String(repeating: "different accessibility epoch ", count: 30)
var epochJumpWrite = schema15WriteFixture(
    id: "terminal-epoch-jump",
    beforeValue: epochBefore,
    afterValue: epochTerminal,
    beganAt: "2026-01-01T00:00:21.000Z",
    terminalAt: "2026-01-01T00:00:24.000Z",
    inputHints: ["typed", "typed"],
    mutationCheckpoints: [
        [
            "checkpointID": "epoch-stable-one",
            "inputObservedAt": "2026-01-01T00:00:21.100Z",
            "eventTimestampNanoseconds": 1,
            "captureRequestedAt": "2026-01-01T00:00:21.150Z",
            "observation": observation(
                epochStableOne, at: "2026-01-01T00:00:21.160Z"
            ),
            "axErrors": [],
        ],
        [
            "checkpointID": "epoch-stable-final",
            "inputObservedAt": "2026-01-01T00:00:21.200Z",
            "eventTimestampNanoseconds": 2,
            "captureRequestedAt": "2026-01-01T00:00:21.250Z",
            "observation": observation(
                epochStableFinal, at: "2026-01-01T00:00:21.260Z"
            ),
            "axErrors": [],
        ],
    ]
)
epochJumpWrite["lastInputAt"] = "2026-01-01T00:00:21.200Z"

let formattingBefore = "prefix\nhttps://example.com\nsuffix"
let formattingAfter = "prefix\n\nhttps://example.com\n authored\nsuffix"
let formattingWrite = schema15WriteFixture(
    id: "noncontiguous-formatting",
    beforeValue: formattingBefore,
    afterValue: formattingAfter,
    beganAt: "2026-01-01T00:00:25.000Z",
    terminalAt: "2026-01-01T00:00:28.000Z",
    inputHints: ["typed"]
)

let fastStartWarmup: [String: Any] = [
    "schemaVersion": 15, "recordType": "active_tap_write_attempt",
    "recordID": "fast-start-warmup", "sessionID": "motivating-session",
    "bundleIdentifier": "fixture.app", "processIdentifier": 42,
    "observedAt": "2026-01-01T00:00:29.100Z",
    "beganAt": "2026-01-01T00:00:29.000Z",
    "lastInputAt": "2026-01-01T00:00:29.000Z",
    "terminalDecisionAt": "2026-01-01T00:00:29.100Z",
    "configuredWriteDelaySeconds": 3,
    "firstEventTimestampNanoseconds": 1,
    "lastEventTimestampNanoseconds": 2,
    "inputEventCount": 2, "inputHints": ["typed"],
    "inputEvents": [[
        "observedAt": "2026-01-01T00:00:29.000Z",
        "eventTimestampNanoseconds": 1,
        "hint": "typed", "mutationCapable": true,
    ], [
        "observedAt": "2026-01-01T00:00:29.050Z",
        "eventTimestampNanoseconds": 2,
        "hint": "typed", "mutationCapable": true,
    ]],
    "boundaryReason": "target_changed",
    "targetIdentity": [
        "bundleIdentifier": "fixture.app", "processIdentifier": 42,
        "role": "AXGroup", "windowTitle": "Fixture",
        "fieldDescription": "Fixture editor", "fieldLabel": "",
        "elementHash": 12344,
    ],
    "beforeAXErrors": ["focused_element:unsupported_role:AXGroup"],
    "afterAXErrors": ["target:unavailable"],
    "returnCheckpoints": [], "pasteCheckpoints": [],
    "mutationCheckpoints": [[
        "checkpointID": "fast-start-unavailable",
        "inputObservedAt": "2026-01-01T00:00:29.000Z",
        "eventTimestampNanoseconds": 1,
        "captureRequestedAt": "2026-01-01T00:00:29.050Z",
        "axErrors": ["target:unavailable"],
    ]],
    "tapTimeoutCountDuringBurst": 0,
]
let fastStartContinuation = schema15WriteFixture(
    id: "fast-start-continuation",
    beforeValue: "is", afterValue: "is it possible",
    beganAt: "2026-01-01T00:00:29.100Z",
    terminalAt: "2026-01-01T00:00:32.100Z",
    inputHints: ["typed"],
    mutationCheckpoints: [[
        "checkpointID": "fast-start-first-visible",
        "inputObservedAt": "2026-01-01T00:00:29.100Z",
        "eventTimestampNanoseconds": 1,
        "captureRequestedAt": "2026-01-01T00:00:29.150Z",
        "observation": observation("is ", at: "2026-01-01T00:00:29.160Z"),
        "axErrors": [],
    ]]
)

var sensitiveWrite = schema15WriteFixture(
    id: "verification-digit",
    beforeValue: "", afterValue: "X",
    beganAt: "2026-01-01T00:00:33.000Z",
    terminalAt: "2026-01-01T00:00:36.000Z",
    inputHints: ["typed"]
)
var sensitiveConditioning = sensitiveWrite["conditioningState"] as! [String: Any]
var sensitiveDestination = sensitiveConditioning["destination"] as! [String: Any]
sensitiveDestination["fieldDescription"] = "digit 1 of 6"
sensitiveConditioning["destination"] = sensitiveDestination
sensitiveWrite["conditioningState"] = sensitiveConditioning
var sensitiveTarget = sensitiveWrite["targetIdentity"] as! [String: Any]
sensitiveTarget["fieldDescription"] = "digit 1 of 6"
sensitiveWrite["targetIdentity"] = sensitiveTarget

let cutOnlyWrite = schema15WriteFixture(
    id: "cut-only-write",
    beforeValue: "alpha\nselected block\nomega",
    afterValue: "alpha\n\nomega",
    beganAt: "2026-01-01T00:00:37.000Z",
    terminalAt: "2026-01-01T00:00:40.000Z",
    inputHints: ["cut"]
)
let delayedPasteBefore = "prefix"
let delayedPasteAfter = "prefixCOPIED\n"
let delayedPastePre = observation(
    delayedPasteBefore,
    at: "2026-01-01T00:00:41.010Z"
)
let delayedPasteStale = observation(
    delayedPasteBefore,
    at: "2026-01-01T00:00:41.050Z"
)
var delayedPasteWrite = schema15WriteFixture(
    id: "delayed-paste-write",
    beforeValue: delayedPasteBefore,
    afterValue: delayedPasteAfter,
    beganAt: "2026-01-01T00:00:41.000Z",
    terminalAt: "2026-01-01T00:00:44.000Z",
    inputHints: ["paste"],
    pasteCheckpoints: [[
        "checkpointID": "delayed-paste-checkpoint",
        "clipboardSnapshotID": "delayed-paste-snapshot",
        "clipboardChangeCount": 9,
        "clipboardText": "COPIED",
        "clipboardTextWasTruncated": false,
        "inputObservedAt": "2026-01-01T00:00:41.000Z",
        "eventTimestampNanoseconds": 1,
        "prePasteObservation": delayedPastePre,
        "prePasteAXErrors": [],
        "observation": delayedPasteStale,
        "axErrors": [],
    ]],
    mutationCheckpoints: []
)
var delayedConditioning = delayedPasteWrite["conditioningState"] as! [String: Any]
delayedConditioning["clipboard"] = [
    "schemaVersion": 1,
    "snapshotID": "delayed-paste-snapshot",
    "changeCount": 9,
    "capturedAt": "2026-01-01T00:00:41.000Z",
    "text": "COPIED",
    "textWasTruncated": false,
]
delayedPasteWrite["conditioningState"] = delayedConditioning

let ambiguousPasteBefore = "prefix\n"
let ambiguousPasteAfter = "prefix\n-\u{200B} COPIED\n"
let ambiguousPastePre = observation(
    ambiguousPasteBefore,
    at: "2026-01-01T00:00:44.010Z"
)
let ambiguousPastePost = observation(
    ambiguousPasteAfter,
    at: "2026-01-01T00:00:44.050Z"
)
var ambiguousPasteWrite = schema15WriteFixture(
    id: "ambiguous-paste-history",
    beforeValue: ambiguousPasteBefore,
    afterValue: ambiguousPasteAfter,
    beganAt: "2026-01-01T00:00:44.000Z",
    terminalAt: "2026-01-01T00:00:44.500Z",
    inputHints: ["paste"],
    pasteCheckpoints: [[
        "checkpointID": "ambiguous-paste-checkpoint",
        "clipboardSnapshotID": "ambiguous-paste-snapshot",
        "clipboardChangeCount": 10,
        "clipboardText": "COPIED",
        "clipboardTextWasTruncated": false,
        "inputObservedAt": "2026-01-01T00:00:44.000Z",
        "eventTimestampNanoseconds": 1,
        "prePasteObservation": ambiguousPastePre,
        "prePasteAXErrors": [],
        "observation": ambiguousPastePost,
        "axErrors": [],
    ]]
)
var ambiguousConditioning = ambiguousPasteWrite["conditioningState"] as! [String: Any]
ambiguousConditioning["clipboard"] = [
    "schemaVersion": 1,
    "snapshotID": "ambiguous-paste-snapshot",
    "changeCount": 10,
    "capturedAt": "2026-01-01T00:00:44.000Z",
    "text": "COPIED",
    "textWasTruncated": false,
]
ambiguousPasteWrite["conditioningState"] = ambiguousConditioning

var untrustworthyPasteWrite = schema15WriteFixture(
    id: "untrustworthy-paste",
    beforeValue: "stable",
    afterValue: "stableCOPIED",
    beganAt: "2026-01-01T00:00:44.600Z",
    terminalAt: "2026-01-01T00:00:44.900Z",
    inputHints: ["paste"]
)
untrustworthyPasteWrite["afterAXErrors"] = ["invalid_ui_element"]
untrustworthyPasteWrite["mutationCheckpoints"] = [[
    "checkpointID": "untrustworthy-paste-mutation",
    "inputObservedAt": "2026-01-01T00:00:44.600Z",
    "eventTimestampNanoseconds": 1,
    "axErrors": ["invalid_ui_element"],
]]

var autocompleteOne = schema15WriteFixture(
    id: "autocomplete-one",
    beforeValue: "", afterValue: "./sc",
    beganAt: "2026-01-01T00:00:45.000Z",
    terminalAt: "2026-01-01T00:00:45.200Z",
    inputHints: ["typed"]
)
autocompleteOne["boundaryReason"] = "selection_navigation"
var autocompleteTwo = schema15WriteFixture(
    id: "autocomplete-two",
    beforeValue: "./scripts/", afterValue: "./scripts/cou",
    beganAt: "2026-01-01T00:00:45.400Z",
    terminalAt: "2026-01-01T00:00:45.600Z",
    inputHints: ["typed"]
)
autocompleteTwo["boundaryReason"] = "selection_navigation"
var autocompleteThree = schema15WriteFixture(
    id: "autocomplete-three",
    beforeValue: "./scripts/coupled ", afterValue: "./scripts/coupled do",
    beganAt: "2026-01-01T00:00:45.800Z",
    terminalAt: "2026-01-01T00:00:46.000Z",
    inputHints: ["typed"]
)
autocompleteThree["boundaryReason"] = "selection_navigation"
var autocompleteFour = schema15WriteFixture(
    id: "autocomplete-four",
    beforeValue: "./scripts/coupled do", afterValue: "./scripts/coupled doc",
    beganAt: "2026-01-01T00:00:46.100Z",
    terminalAt: "2026-01-01T00:00:46.200Z",
    inputHints: ["typed"]
)
autocompleteFour["boundaryReason"] = "selection_navigation"
var autocompleteFive = schema15WriteFixture(
    id: "autocomplete-five",
    beforeValue: "./scripts/coupled doc", afterValue: "./scripts/coupled stop",
    beganAt: "2026-01-01T00:00:46.300Z",
    terminalAt: "2026-01-01T00:00:46.500Z",
    inputHints: ["delete", "typed", "return"],
    returnCheckpoints: [[
        "checkpointID": "autocomplete-return",
        "inputObservedAt": "2026-01-01T00:00:46.450Z",
        "eventTimestampNanoseconds": 3,
        "observation": observation(
            "./scripts/coupled stop",
            at: "2026-01-01T00:00:46.450Z"
        ),
        "axErrors": [],
    ]]
)
autocompleteFive["boundaryReason"] = "return_pressed"
var cursorMoveOne = schema15WriteFixture(
    id: "cursor-move-one",
    beforeValue: "abc", afterValue: "abcd",
    beganAt: "2026-01-01T00:00:47.000Z",
    terminalAt: "2026-01-01T00:00:47.200Z",
    inputHints: ["typed"]
)
cursorMoveOne["boundaryReason"] = "selection_navigation"
var cursorMoveTwo = schema15WriteFixture(
    id: "cursor-move-two",
    beforeValue: "abcd", afterValue: "abXcd",
    beganAt: "2026-01-01T00:00:47.400Z",
    terminalAt: "2026-01-01T00:00:47.600Z",
    inputHints: ["typed"]
)
var cursorMoveTwoBefore = cursorMoveTwo["before"] as! [String: Any]
cursorMoveTwoBefore["selectedRangeLocation"] = 2
cursorMoveTwo["before"] = cursorMoveTwoBefore

var selectedReplacementWrite = schema15WriteFixture(
    id: "selected-replacement",
    beforeValue: "compare with this",
    afterValue: "consider with this",
    beganAt: "2026-01-01T00:00:48.000Z",
    terminalAt: "2026-01-01T00:00:51.000Z",
    inputHints: Array(repeating: "typed", count: 8)
)
var selectedReplacementBefore = selectedReplacementWrite["before"] as! [String: Any]
selectedReplacementBefore["selectedRangeLocation"] = 0
selectedReplacementBefore["selectedRangeLength"] = 7
selectedReplacementWrite["before"] = selectedReplacementBefore
var selectedReplacementConditioning =
    selectedReplacementWrite["conditioningState"] as! [String: Any]
selectedReplacementConditioning["cursorContext"] = [
    "schemaVersion": 2, "source": "accessibility_string_for_range",
    "captureStatus": "complete", "fieldState": "editable_text",
    "leftContext": "", "selectedText": "compare", "rightContext": " with this",
]
selectedReplacementWrite["conditioningState"] = selectedReplacementConditioning

var unpopulatedPromptWrite = schema15WriteFixture(
    id: "unpopulated-prompt-replacement",
    beforeValue: "\nDo anything",
    afterValue: "does checkpoint need updating",
    beganAt: "2026-01-01T00:00:52.000Z",
    terminalAt: "2026-01-01T00:00:55.000Z",
    inputHints: Array(repeating: "typed", count: 29)
)
var unpopulatedConditioning =
    unpopulatedPromptWrite["conditioningState"] as! [String: Any]
unpopulatedConditioning["cursorContext"] = [
    "schemaVersion": 2, "source": "accessibility_string_for_range",
    "captureStatus": "complete", "fieldState": "unpopulated_prompt",
    "leftContext": "", "selectedText": "", "rightContext": "",
    "surfacePrompt": "Do anything",
]
unpopulatedPromptWrite["conditioningState"] = unpopulatedConditioning

var ambiguousShortcutWrite = schema15WriteFixture(
    id: "mid-burst-shortcut",
    beforeValue: "old", afterValue: "oldnewreplacement",
    beganAt: "2026-01-01T00:00:56.000Z",
    terminalAt: "2026-01-01T00:00:59.000Z",
    inputHints: ["typed", "shortcut", "typed"]
)
ambiguousShortcutWrite["inputEvents"] = [
    ["observedAt": "2026-01-01T00:00:56.000Z", "eventTimestampNanoseconds": 1,
     "hint": "typed", "mutationCapable": true],
    ["observedAt": "2026-01-01T00:00:56.100Z", "eventTimestampNanoseconds": 2,
     "hint": "shortcut", "mutationCapable": false],
    ["observedAt": "2026-01-01T00:00:56.200Z", "eventTimestampNanoseconds": 3,
     "hint": "typed", "mutationCapable": true],
]

let obsidianScaffoldBefore = "existing note"
let obsidianScaffoldAfter = "existing note\nfirst thought\n\u{200B}\t\n\u{200B}\n-\n\u{200B} second thought\n\u{200B}\t\n\u{200B}\n-\n\u{200B} "
var obsidianScaffoldWrite = schema15WriteFixture(
    id: "obsidian-list-scaffold",
    beforeValue: obsidianScaffoldBefore,
    afterValue: obsidianScaffoldAfter,
    beganAt: "2026-01-01T00:01:00.000Z",
    terminalAt: "2026-01-01T00:01:03.000Z",
    inputHints: ["typed", "return"]
)
obsidianScaffoldWrite["bundleIdentifier"] = "md.obsidian"
var obsidianTarget = obsidianScaffoldWrite["targetIdentity"] as! [String: Any]
obsidianTarget["bundleIdentifier"] = "md.obsidian"
obsidianScaffoldWrite["targetIdentity"] = obsidianTarget
var obsidianConditioning = obsidianScaffoldWrite["conditioningState"] as! [String: Any]
var obsidianDestination = obsidianConditioning["destination"] as! [String: Any]
obsidianDestination["bundleIdentifier"] = "md.obsidian"
obsidianConditioning["destination"] = obsidianDestination
obsidianScaffoldWrite["conditioningState"] = obsidianConditioning

let compactObsidianScaffoldAfter =
    "existing note\nfirst thought\n\u{200B}\n-\n\u{200B} \nsecond thought\n\u{200B}\n-\n\u{200B} \nthird thought\n"
var compactObsidianScaffoldWrite = schema15WriteFixture(
    id: "obsidian-compact-list-scaffold",
    beforeValue: obsidianScaffoldBefore,
    afterValue: compactObsidianScaffoldAfter,
    beganAt: "2026-01-01T00:01:04.000Z",
    terminalAt: "2026-01-01T00:01:07.000Z",
    inputHints: ["typed", "return"]
)
compactObsidianScaffoldWrite["bundleIdentifier"] = "md.obsidian"
var compactObsidianTarget =
    compactObsidianScaffoldWrite["targetIdentity"] as! [String: Any]
compactObsidianTarget["bundleIdentifier"] = "md.obsidian"
compactObsidianScaffoldWrite["targetIdentity"] = compactObsidianTarget
var compactObsidianConditioning =
    compactObsidianScaffoldWrite["conditioningState"] as! [String: Any]
var compactObsidianDestination =
    compactObsidianConditioning["destination"] as! [String: Any]
compactObsidianDestination["bundleIdentifier"] = "md.obsidian"
compactObsidianConditioning["destination"] = compactObsidianDestination
compactObsidianScaffoldWrite["conditioningState"] = compactObsidianConditioning

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
        id: "read-containing-active-write",
        capturedAt: "2026-01-01T00:00:03.250Z",
        content: "page text\nthere was a paper about encr",
        triggerAt: "2026-01-01T00:00:02.500Z"
    ),
    rawScreenFixture(
        id: "stale-delayed-read", capturedAt: "2026-01-01T00:00:03.500Z",
        content: "stale content", triggerAt: "2026-01-01T00:00:01.500Z"
    ),
    semanticTimeWrite, geminiWrite, deleteOnlyWrite, unresolvedPaste,
    repeatedBoundaryWrite, rangeAlignedBoundaryWrite,
    epochJumpWrite, formattingWrite,
    fastStartWarmup, fastStartContinuation, sensitiveWrite,
    cutOnlyWrite, delayedPasteWrite, ambiguousPasteWrite, untrustworthyPasteWrite,
    autocompleteOne, autocompleteTwo, autocompleteThree,
    autocompleteFour, autocompleteFive,
    cursorMoveOne, cursorMoveTwo,
    selectedReplacementWrite, unpopulatedPromptWrite, ambiguousShortcutWrite,
    obsidianScaffoldWrite, compactObsidianScaffoldWrite,
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
        ($0["sourceRecordIDs"] as? [String]) == ["read-containing-active-write"]
            && $0["reason"] as? String == "read_contains_active_write_content"
            && $0["rule"] as? String == "active_write_read_authorship_guard_v1"
    },
    "READ containing a proven in-progress WRITE prefix is not inbound history"
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
let missingPasteHistoryEvent = motivatingEvents.first {
    ($0["sourceRecordIDs"] as? [String]) == ["missing-paste"]
}
expect(
    missingPasteHistoryEvent?["authorshipResolution"] as? String == "unresolved"
        && missingPasteHistoryEvent?["authorshipUnresolvedReason"] as? String
            == "paste_checkpoint_missing"
        && missingPasteHistoryEvent?["resolvedCompletion"] as? String == "COPIED",
    "observable paste without grounding remains context-only WRITE history"
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
let rangeAlignedBoundaryEvent = motivatingEvents.first {
    ($0["sourceRecordIDs"] as? [String]) == ["range-aligned-repeated-boundary"]
}
expect(
    rangeAlignedBoundaryEvent?["content"] as? String == "live "
        && rangeAlignedBoundaryEvent?["characterOffset"] as? Int == 27
        && (rangeAlignedBoundaryEvent?["observedNetEdit"] as? [String: Any])?["content"]
            as? String == "ive l"
        && (rangeAlignedBoundaryEvent?["reduction"] as? [String: Any])?["alignmentRule"]
            as? String == "checkpoint_grounded_equivalent_diff_v1",
    "range-native initial cursor resolves an ambiguous first-character boundary without changing the observed transition"
)
let recoveredEpochWrite = motivatingEvents.first {
    ($0["sourceRecordIDs"] as? [String]) == ["terminal-epoch-jump"]
}
expect(
    recoveredEpochWrite?["content"] as? String == "human thought"
        && recoveredEpochWrite?["derivationObservationSource"] as? String
            == "post_input_checkpoint"
        && (recoveredEpochWrite?["reduction"] as? [String: Any])?["reason"]
            as? String == "terminal_ax_epoch_discontinuity",
    "catastrophic terminal AX epoch jump uses the reliable post-final-input checkpoint"
)
expect(
    motivatingDispositions.contains {
        ($0["sourceRecordIDs"] as? [String]) == ["noncontiguous-formatting"]
            && $0["reason"] as? String == "noncontiguous_authorship_unresolved"
    },
    "noncontiguous editor formatting cannot become authored supervision"
)
let fastStartHistory = motivatingEvents.first {
    ($0["sourceRecordIDs"] as? [String])
        == ["fast-start-warmup", "fast-start-continuation"]
}
expect(
    fastStartHistory?["resolvedCompletion"] as? String == "is it possible"
        && ((fastStartHistory?["phase1TargetEligibility"] as? [String: Any])?["eligible"]
            as? Bool) == false
        && ((fastStartHistory?["phase1TargetEligibility"] as? [String: Any])?["reason"]
            as? String) == "pre_first_mutation_conditioning_unavailable",
    "fast-start continuation remains exact history but cannot become a target"
)
let selectedReplacementEvent = motivatingEvents.first {
    ($0["sourceRecordIDs"] as? [String]) == ["selected-replacement"]
}
expect(
    selectedReplacementEvent?["content"] as? String == "consider"
        && selectedReplacementEvent?["removedContent"] as? String == "compare"
        && (selectedReplacementEvent?["observedNetEdit"] as? [String: Any])?["content"]
            as? String == "nsider"
        && (selectedReplacementEvent?["reduction"] as? [String: Any])?["alignmentRule"]
            as? String == "checkpoint_grounded_selected_replacement_v1",
    "complete initial selection restores shared authored replacement characters"
)
let unpopulatedPromptEvent = motivatingEvents.first {
    ($0["sourceRecordIDs"] as? [String]) == ["unpopulated-prompt-replacement"]
}
expect(
    unpopulatedPromptEvent?["content"] as? String == "does checkpoint need updating"
        && unpopulatedPromptEvent?["removedContent"] as? String == "\nDo anything"
        && (unpopulatedPromptEvent?["observedNetEdit"] as? [String: Any])?["content"]
            as? String == "does checkpoint need updat"
        && (unpopulatedPromptEvent?["reduction"] as? [String: Any])?["alignmentRule"]
            as? String == "checkpoint_grounded_selected_replacement_v1",
    "explicit unpopulated prompt state prevents scaffolding from truncating authorship"
)
expect(
    motivatingDispositions.contains {
        ($0["sourceRecordIDs"] as? [String]) == ["mid-burst-shortcut"]
            && $0["reason"] as? String
                == "shortcut_changed_semantic_position_without_observation"
    },
    "unobserved shortcut between mutations remains unresolved"
)
let obsidianScaffoldEvent = motivatingEvents.first {
    ($0["sourceRecordIDs"] as? [String]) == ["obsidian-list-scaffold"]
}
expect(
    obsidianScaffoldEvent?["content"] as? String == "first thought\nsecond thought"
        && obsidianScaffoldEvent?["resolvedCompletion"] as? String
            == "first thought\nsecond thought"
        && obsidianScaffoldEvent?["authorshipEvidence"] as? String
            == "obsidian_list_scaffold_normalized_v1"
        && (obsidianScaffoldEvent?["observedNetEdit"] as? [String: Any])?["content"]
            as? String == "\nfirst thought\n\u{200B}\t\n\u{200B}\n-\n\u{200B} second thought\n\u{200B}\t\n\u{200B}\n-\n\u{200B} ",
    "Obsidian list scaffolding remains observed evidence but not human-authored content"
)
let compactObsidianScaffoldEvent = motivatingEvents.first {
    ($0["sourceRecordIDs"] as? [String]) == ["obsidian-compact-list-scaffold"]
}
expect(
    compactObsidianScaffoldEvent?["content"] as? String
        == "first thought\nsecond thought\nthird thought\n"
        && compactObsidianScaffoldEvent?["authorshipEvidence"] as? String
            == "obsidian_list_scaffold_normalized_v1"
        && (compactObsidianScaffoldEvent?["observedNetEdit"] as? [String: Any])?["content"]
            as? String
            == "\nfirst thought\n\u{200B}\n-\n\u{200B} \nsecond thought\n\u{200B}\n-\n\u{200B} \nthird thought\n",
    "compact Obsidian list scaffolding is normalized from the observed transition"
)
expect(
    motivatingDispositions.contains {
        ($0["sourceRecordIDs"] as? [String]) == ["verification-digit"]
            && $0["reason"] as? String == "sensitive_input_field"
    },
    "verification and credential fields never become semantic WRITE targets"
)
let cutOnlyEvent = motivatingEvents.first {
    ($0["sourceRecordIDs"] as? [String]) == ["cut-only-write"]
}
expect(
    cutOnlyEvent?["resolvedCompletion"] as? String == ""
        && (cutOnlyEvent?["authorshipSegments"] as? [[String: Any]])?.isEmpty == true
        && cutOnlyEvent?["authorshipEvidence"] as? String
            == "cut_only_no_authored_content"
        && (cutOnlyEvent?["observedNetEdit"] as? [String: Any])?["content"]
            as? String == ""
        && (cutOnlyEvent?["observedNetEdit"] as? [String: Any])?["removedContent"]
            as? String == "selected block",
    "cut-only transition remains history without an authored target"
)
let delayedPasteEvent = motivatingEvents.first {
    ($0["sourceRecordIDs"] as? [String]) == ["delayed-paste-write"]
}
let delayedPasteSegments = delayedPasteEvent?["authorshipSegments"]
    as? [[String: Any]]
expect(
    delayedPasteEvent?["resolvedCompletion"] as? String == "COPIED\n"
        && delayedPasteEvent?["stateContinuity"] as? String
            == "same_ax_field_delayed_paste_observation"
        && delayedPasteEvent?["authorshipEvidence"] as? String
            == "grounded_delayed_paste_observation"
        && delayedPasteSegments?.count == 1
        && delayedPasteSegments?.first?["type"] as? String == "paste"
        && delayedPasteSegments?.first?["content"] as? String == "COPIED\n",
    "premature paste checkpoint uses a later exact clipboard-grounded observation"
)
let ambiguousPasteEvent = motivatingEvents.first {
    ($0["sourceRecordIDs"] as? [String]) == ["ambiguous-paste-history"]
}
let ambiguousPasteSegments = ambiguousPasteEvent?["authorshipSegments"]
    as? [[String: Any]]
expect(
    ambiguousPasteEvent?["authorshipResolution"] as? String == "unresolved"
        && ambiguousPasteEvent?["authorshipUnresolvedReason"] as? String
            == "unproven_ax_epoch_transition"
        && ambiguousPasteEvent?["resolvedCompletion"] as? String
            == "-\u{200B} COPIED\n"
        && ambiguousPasteSegments?.count == 1
        && ambiguousPasteSegments?.first?["type"] as? String
            == "unresolved_paste_transition"
        && ambiguousPasteSegments?.first?["content"] as? String
            == "-\u{200B} COPIED\n",
    "reconstructible paste formatting remains context-only with unresolved authorship"
)
expect(
    motivatingDispositions.contains {
        ($0["sourceRecordIDs"] as? [String]) == ["untrustworthy-paste"]
            && $0["reason"] as? String == "no_meaningful_terminal_observation"
    } && !motivatingEvents.contains {
        ($0["sourceRecordIDs"] as? [String]) == ["untrustworthy-paste"]
    },
    "paste without a trustworthy document observation remains excluded"
)
let autocompleteEvent = motivatingEvents.first {
    ($0["sourceRecordIDs"] as? [String])
        == [
            "autocomplete-one", "autocomplete-two", "autocomplete-three",
            "autocomplete-four", "autocomplete-five",
        ]
}
expect(
    autocompleteEvent?["resolvedCompletion"] as? String
        == "./scripts/coupled stop"
        && autocompleteEvent?["stateContinuity"] as? String
            == "same_editable_navigation_chain"
        && motivatingEvents.filter {
            let ids = $0["sourceRecordIDs"] as? [String] ?? []
            return ids.contains { $0.hasPrefix("autocomplete-") }
        }.count == 1,
    "same-editable completion and no-op navigation reduce to one final WRITE"
)
expect(
    motivatingEvents.filter {
        let ids = $0["sourceRecordIDs"] as? [String] ?? []
        return ids == ["cursor-move-one"] || ids == ["cursor-move-two"]
    }.count == 2,
    "selection-only cursor relocation remains two independently conditioned WRITEs"
)

let motivatingDataset = fixtureRoot.appendingPathComponent("motivating-dataset")
_ = try! CausalDatasetCompiler().compile(
    inputDirectory: motivatingReduction,
    sourceDirectory: motivatingInput,
    outputDirectory: motivatingDataset
)
let motivatingExamples = readFixtureJSONL(
    motivatingDataset.appendingPathComponent("examples.jsonl")
)
let motivatingCompiledEvents = readFixtureJSONL(
    motivatingDataset.appendingPathComponent("events.jsonl")
)
expect(
    !motivatingExamples.contains {
        $0["targetEventID"] as? String == cutOnlyEvent?["eventID"] as? String
    },
    "cut-only WRITE is retained in history but excluded as a target"
)
expect(
    !motivatingExamples.contains {
        $0["targetEventID"] as? String == fastStartHistory?["eventID"] as? String
    },
    "reducer target-ineligibility is enforced by the causal compiler"
)
expect(
    motivatingExamples.contains {
        $0["targetEventID"] as? String == delayedPasteEvent?["eventID"] as? String
            && (($0["target"] as? [String: Any])?["segments"]
                as? [[String: Any]])?.first?["type"] as? String == "paste"
    },
    "delayed grounded paste compiles to a paste-action target"
)
let contextOnlyPasteIDs = Set([
    missingPasteHistoryEvent?["eventID"] as? String,
    ambiguousPasteEvent?["eventID"] as? String,
].compactMap { $0 })
expect(
    motivatingExamples.allSatisfy {
        guard let id = $0["targetEventID"] as? String else { return false }
        return !contextOnlyPasteIDs.contains(id)
    },
    "unresolved paste transitions never receive target loss"
)
let compiledContextOnlyPastes = motivatingCompiledEvents.filter {
    guard let id = $0["sourceEventID"] as? String else { return false }
    return contextOnlyPasteIDs.contains(id)
}
expect(
    compiledContextOnlyPastes.count == 2
        && compiledContextOnlyPastes.allSatisfy {
            guard let serializedText = $0["serialized"] as? String,
                  let data = serializedText.data(using: .utf8),
                  let object = try? JSONSerialization.jsonObject(with: data),
                  let serialized = object as? [String: Any],
                  serialized["authorshipResolution"] as? String == "unresolved",
                  let segments = serialized["authorshipSegments"]
                    as? [[String: Any]] else {
                return false
            }
            return segments.allSatisfy {
                $0["type"] as? String == "unresolved_paste_transition"
                    && $0["content"] as? String != nil
            }
        },
    "context-only paste text and uncertainty remain visible in later model history"
)
expect(
    motivatingExamples.contains {
        $0["targetEventID"] as? String == autocompleteEvent?["eventID"] as? String
            && ($0["target"] as? [String: Any])?["resolvedContent"] as? String
                == "./scripts/coupled stop"
    },
    "multi-record autocomplete lineage compiles to one full-content target"
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

// A settled prompt WRITE may be closed later by independently persisted raw
// evidence. The reducer—not the collector preview—binds that observation into
// semantic lineage and moves causal availability to the proven submission.
let promptClosureInput = fixtureRoot.appendingPathComponent("prompt-closure-input")
let promptClosureReduction = fixtureRoot.appendingPathComponent("prompt-closure-reduction")
let promptClosureDataset = fixtureRoot.appendingPathComponent("prompt-closure-dataset")
try! FileManager.default.createDirectory(
    at: promptClosureInput, withIntermediateDirectories: true
)
try! jsonData([
    "sessionID": "prompt-closure-session",
    "schemas": ["timingSemanticsVersion": 2, "rawActiveTapWrite": 15],
], pretty: true).write(to: promptClosureInput.appendingPathComponent("session.json"))
var promptClosureAttempt = rawFirstAttempt
promptClosureAttempt["recordID"] = "prompt-closure-attempt"
promptClosureAttempt["sessionID"] = "prompt-closure-session"
let promptClosureObservation: [String: Any] = [
    "schemaVersion": 1,
    "recordType": "prompt_submission_observation",
    "recordID": "prompt-closure-observation",
    "sessionID": "prompt-closure-session",
    "sourceWriteRecordID": "prompt-closure-attempt",
    "referenceRetainedAt": "2026-01-01T00:00:04.000Z",
    "terminalObservationID": rawFirstAfter["observationID"] as! String,
    "terminalValueSHA256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
    "terminalCharacterCount": 5,
    "preActionObservation": rawFirstAfter,
    "preActionAXErrors": [],
    "action": [
        "kind": "unmodified_return",
        "observedAt": "2026-01-01T00:00:05.000Z",
        "eventTimestampNanoseconds": 5,
    ],
    "observedAt": "2026-01-01T00:00:05.150Z",
    "postActionAXErrors": ["AXValue:invalid_ui_element"],
    "surfaceValidationErrors": [],
    "disposition": "confirmed_field_disappeared",
]
var pointerPromptClosureAttempt = promptClosureAttempt
pointerPromptClosureAttempt["recordID"] = "pointer-prompt-closure-attempt"
pointerPromptClosureAttempt["beganAt"] = "2026-01-01T00:00:06.000Z"
pointerPromptClosureAttempt["lastInputAt"] = "2026-01-01T00:00:06.000Z"
pointerPromptClosureAttempt["observedAt"] = "2026-01-01T00:00:09.000Z"
pointerPromptClosureAttempt["terminalDecisionAt"] = "2026-01-01T00:00:09.000Z"
pointerPromptClosureAttempt["terminalSnapshotAt"] = "2026-01-01T00:00:09.000Z"
let pointerPromptClosureObservation: [String: Any] = [
    "schemaVersion": 1,
    "recordType": "prompt_submission_observation",
    "recordID": "pointer-prompt-closure-observation",
    "sessionID": "prompt-closure-session",
    "sourceWriteRecordID": "pointer-prompt-closure-attempt",
    "referenceRetainedAt": "2026-01-01T00:00:09.000Z",
    "terminalObservationID": rawFirstAfter["observationID"] as! String,
    "terminalValueSHA256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
    "terminalCharacterCount": 5,
    "preActionObservation": rawFirstAfter,
    "preActionAXErrors": [],
    "action": [
        "kind": "pointer_click",
        "controlRole": "AXGroup",
        "matchedSubmissionTerm": "send",
        "observedAt": "2026-01-01T00:00:10.000Z",
        "eventTimestampNanoseconds": 10,
    ],
    "observedAt": "2026-01-01T00:00:10.150Z",
    "postActionObservation": observation(
        "", at: "2026-01-01T00:00:10.150Z", selectionLocation: 0
    ),
    "postActionAXErrors": [],
    "surfaceValidationErrors": [],
    "disposition": "confirmed_field_cleared",
]
writeFixtureJSONL(
    [
        promptClosureAttempt, promptClosureObservation,
        pointerPromptClosureAttempt, pointerPromptClosureObservation,
    ],
    to: promptClosureInput.appendingPathComponent("raw.jsonl")
)
_ = try! Phase1SemanticReducer().reduce(
    sourceDirectory: promptClosureInput,
    outputDirectory: promptClosureReduction
)
let promptClosureEvents = readFixtureJSONL(
    promptClosureReduction.appendingPathComponent("events.jsonl")
)
expect(
    promptClosureEvents.count == 2
        && promptClosureEvents.allSatisfy {
            $0["boundaryReason"] as? String == "submission_boundary"
        }
        && promptClosureEvents[0]["captureBoundaryReason"] as? String
            == "write_delay_elapsed"
        && promptClosureEvents[0]["submissionObservedAt"] as? String
            == "2026-01-01T00:00:05.150Z"
        && promptClosureEvents[0]["sourceRecordIDs"] as? [String]
            == ["prompt-closure-attempt", "prompt-closure-observation"],
    "reducer binds a proven post-settlement submission to its source WRITE"
)
_ = try! CausalDatasetCompiler().compile(
    inputDirectory: promptClosureReduction,
    sourceDirectory: promptClosureInput,
    outputDirectory: promptClosureDataset
)
let compiledPromptClosure = readFixtureJSONL(
    promptClosureDataset.appendingPathComponent("events.jsonl")
)
expect(
    compiledPromptClosure.count == 2
        && compiledPromptClosure.contains {
            $0["sourceRecordIDs"] as? [String]
                == ["prompt-closure-attempt", "prompt-closure-observation"]
        }
        && compiledPromptClosure.contains {
            $0["sourceRecordIDs"] as? [String]
                == [
                    "pointer-prompt-closure-attempt",
                    "pointer-prompt-closure-observation",
                ]
        },
    "causal compiler verifies mixed WRITE and closure raw lineage"
)

// A renderer may replace its editable after the first key, producing a
// fast-start history-only WRITE whose lineage contains a failed attempt, the
// surviving conditioned attempt, and later prompt-closure evidence. The
// closure record is not itself a candidate conditioning attempt.
let fastStartClosureInput = fixtureRoot.appendingPathComponent(
    "fast-start-prompt-closure-input"
)
let fastStartClosureReduction = fixtureRoot.appendingPathComponent(
    "fast-start-prompt-closure-reduction"
)
let fastStartClosureDataset = fixtureRoot.appendingPathComponent(
    "fast-start-prompt-closure-dataset"
)
try! FileManager.default.createDirectory(
    at: fastStartClosureInput, withIntermediateDirectories: true
)
try! jsonData([
    "sessionID": "fast-start-prompt-closure-session",
    "schemas": ["timingSemanticsVersion": 2, "rawActiveTapWrite": 15],
], pretty: true).write(
    to: fastStartClosureInput.appendingPathComponent("session.json")
)
var missingFirstMutationAttempt = rawFirstAttempt
missingFirstMutationAttempt["recordID"] = "fast-start-missing-attempt"
missingFirstMutationAttempt["sessionID"] = "fast-start-prompt-closure-session"
missingFirstMutationAttempt["beganAt"] = "2026-01-01T00:00:12.000Z"
missingFirstMutationAttempt["lastInputAt"] = "2026-01-01T00:00:12.000Z"
missingFirstMutationAttempt["observedAt"] = "2026-01-01T00:00:12.050Z"
missingFirstMutationAttempt["terminalDecisionAt"] = "2026-01-01T00:00:12.050Z"
missingFirstMutationAttempt["boundaryReason"] = "target_changed"
missingFirstMutationAttempt["beforeAXErrors"] = [
    "focused_element:unsupported_role:AXGroup",
]
missingFirstMutationAttempt["afterAXErrors"] = ["target:unavailable"]
missingFirstMutationAttempt.removeValue(forKey: "before")
missingFirstMutationAttempt.removeValue(forKey: "after")
missingFirstMutationAttempt.removeValue(forKey: "conditioningState")
missingFirstMutationAttempt["mutationCheckpoints"] = []

let survivingBefore = observation(
    "h", at: "2026-01-01T00:00:12.100Z", selectionLocation: 1
)
let survivingCheckpoint = observation(
    "he", at: "2026-01-01T00:00:12.250Z", selectionLocation: 2
)
let survivingAfter = observation(
    "hello", at: "2026-01-01T00:00:15.100Z", selectionLocation: 5
)
var survivingConditioning = rawFirstConditioning
survivingConditioning["inputInterceptedAt"] = "2026-01-01T00:00:12.100Z"
survivingConditioning["capturedAt"] = "2026-01-01T00:00:12.100Z"
survivingConditioning["sourceObservationID"] = survivingBefore["observationID"]
survivingConditioning["cursorContext"] = [
    "schemaVersion": 2,
    "source": "accessibility_string_for_range",
    "captureStatus": "complete",
    "fieldState": "editable_text",
    "leftContext": "h",
    "selectedText": "",
    "rightContext": "",
]
var survivingAttempt = rawFirstAttempt
survivingAttempt["recordID"] = "fast-start-surviving-attempt"
survivingAttempt["sessionID"] = "fast-start-prompt-closure-session"
survivingAttempt["beganAt"] = "2026-01-01T00:00:12.100Z"
survivingAttempt["lastInputAt"] = "2026-01-01T00:00:12.100Z"
survivingAttempt["observedAt"] = "2026-01-01T00:00:15.100Z"
survivingAttempt["terminalDecisionAt"] = "2026-01-01T00:00:15.100Z"
survivingAttempt["terminalSnapshotAt"] = "2026-01-01T00:00:15.100Z"
survivingAttempt["conditioningState"] = survivingConditioning
survivingAttempt["before"] = survivingBefore
survivingAttempt["after"] = survivingAfter
survivingAttempt["mutationCheckpoints"] = [[
    "checkpointID": "fast-start-surviving-checkpoint",
    "inputObservedAt": "2026-01-01T00:00:12.100Z",
    "eventTimestampNanoseconds": 2,
    "captureRequestedAt": "2026-01-01T00:00:12.200Z",
    "observation": survivingCheckpoint,
    "axErrors": [],
]]
let fastStartClosureObservation: [String: Any] = [
    "schemaVersion": 1,
    "recordType": "prompt_submission_observation",
    "recordID": "fast-start-closure-observation",
    "sessionID": "fast-start-prompt-closure-session",
    "sourceWriteRecordID": "fast-start-surviving-attempt",
    "referenceRetainedAt": "2026-01-01T00:00:15.100Z",
    "terminalObservationID": survivingAfter["observationID"] as! String,
    "terminalValueSHA256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
    "terminalCharacterCount": 5,
    "preActionObservation": survivingAfter,
    "preActionAXErrors": [],
    "action": [
        "kind": "unmodified_return",
        "observedAt": "2026-01-01T00:00:16.000Z",
        "eventTimestampNanoseconds": 3,
    ],
    "observedAt": "2026-01-01T00:00:16.150Z",
    "postActionAXErrors": ["AXValue:invalid_ui_element"],
    "surfaceValidationErrors": [],
    "disposition": "confirmed_field_disappeared",
]
writeFixtureJSONL(
    [
        missingFirstMutationAttempt,
        survivingAttempt,
        fastStartClosureObservation,
    ],
    to: fastStartClosureInput.appendingPathComponent("raw.jsonl")
)
_ = try! Phase1SemanticReducer().reduce(
    sourceDirectory: fastStartClosureInput,
    outputDirectory: fastStartClosureReduction
)
let fastStartClosureEvents = readFixtureJSONL(
    fastStartClosureReduction.appendingPathComponent("events.jsonl")
)
let fastStartClosureEligibility = (
    fastStartClosureEvents.first?["phase1TargetEligibility"]
) as? [String: Any]
expect(
    fastStartClosureEvents.count == 1
        && fastStartClosureEvents[0]["stateContinuity"] as? String
            == "incomplete_pre_mutation_conditioning"
        && fastStartClosureEligibility?["reason"] as? String
            == "pre_first_mutation_conditioning_unavailable"
        && fastStartClosureEvents[0]["sourceRecordIDs"] as? [String]
            == [
                "fast-start-missing-attempt",
                "fast-start-surviving-attempt",
                "fast-start-closure-observation",
            ],
    "fast-start prompt closure remains an explicit history-only WRITE"
)
_ = try! CausalDatasetCompiler().compile(
    inputDirectory: fastStartClosureReduction,
    sourceDirectory: fastStartClosureInput,
    outputDirectory: fastStartClosureDataset
)
let fastStartClosureCompiledEvents = readFixtureJSONL(
    fastStartClosureDataset.appendingPathComponent("events.jsonl")
)
let fastStartClosureExclusions = readFixtureJSONL(
    fastStartClosureDataset.appendingPathComponent("target-exclusions.jsonl")
)
expect(
    fastStartClosureCompiledEvents.count == 1
        && fastStartClosureExclusions.count == 1
        && fastStartClosureExclusions[0]["reason"] as? String
            == "pre_first_mutation_conditioning_unavailable",
    "compiler conditions on the surviving write attempt rather than closure evidence"
)

expect(
    promptClosureDisposition(
        observedLogicalValue: "",
        observedSurfacePrompt: nil,
        observedValueMatchesTerminal: false,
        observationErrors: [],
        sameSurface: true
    ) == .confirmedFieldCleared,
    "a submission-shaped action followed by an empty field proves prompt closure"
)
expect(
    promptClosureDisposition(
        observedLogicalValue: "",
        observedSurfacePrompt: "Ask anything",
        observedValueMatchesTerminal: false,
        observationErrors: [],
        sameSurface: true
    ) == .confirmedPlaceholderRestored,
    "restored prompt chrome proves prompt closure"
)
expect(
    promptClosureDisposition(
        observedLogicalValue: nil,
        observedSurfacePrompt: nil,
        observedValueMatchesTerminal: nil,
        observationErrors: ["AXValue:invalid_ui_element"],
        sameSurface: true
    ) == .confirmedFieldDisappeared,
    "an invalidated prompt on the same surface is retained as disappearance evidence"
)
expect(
    promptClosureDisposition(
        observedLogicalValue: "draft",
        observedSurfacePrompt: nil,
        observedValueMatchesTerminal: true,
        observationErrors: [],
        sameSurface: true
    ) == .promptRemainedPopulated,
    "clicking elsewhere while a draft remains does not fabricate closure"
)
expect(
    promptClosureDisposition(
        observedLogicalValue: "",
        observedSurfacePrompt: nil,
        observedValueMatchesTerminal: false,
        observationErrors: [],
        sameSurface: false
    ) == .surfaceChanged,
    "a different capture-time surface cannot close the old prompt"
)

try! FileManager.default.removeItem(at: fixtureRoot)

print("CoupledCore checks passed")
