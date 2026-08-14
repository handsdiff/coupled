import Foundation
import CoreGraphics

func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
    guard condition() else {
        FileHandle.standardError.write(Data("FAIL: \(message)\n".utf8))
        exit(1)
    }
}

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
    pasteTokenID: 900,
    eosTokenID: 901,
    tokenizeAuthoredText: { Array($0.utf8).map(Int.init) }
)
expect(
    loadedTarget.tokenIDs.filter { $0 == 900 }.count == 1
        && loadedTarget.tokenIDs.last == 901
        && loadedTarget.lossMask.allSatisfy { $0 },
    "loader emits one atomic paste token and exactly one loss-bearing EOS"
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
expect(
    pasteTargetSegments[1]["type"] as! String == "paste"
        && pasteTargetSegments[1]["content"] == nil,
    "current target omits pasted payload and preserves a grounded paste action"
)
expect(
    (pasteExamples[1]["context"] as! String).contains("COPIED")
        && (pasteExamples[1]["context"] as! String).contains("authorshipSegments"),
    "later history retains resolved pasted content and paste provenance"
)
try! FileManager.default.removeItem(at: fixtureRoot)

print("CoupledCore checks passed")
