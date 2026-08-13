import XCTest
@testable import CoupledCore

final class TextUnderstandingTests: XCTestCase {
    func testInsertion() {
        XCTAssertEqual(
            minimalTextEdit(from: "hello world", to: "hello brave world"),
            TextEdit(operation: .insert, characterOffset: 6, removed: "", inserted: "brave ")
        )
    }

    func testDeletion() {
        XCTAssertEqual(
            minimalTextEdit(from: "one two three", to: "one three"),
            TextEdit(operation: .delete, characterOffset: 5, removed: "wo t", inserted: "")
        )
    }

    func testCursorCannotPullCanonicalEditIntoUnchangedPrefix() {
        XCTAssertEqual(
            minimalTextEdit(
                from: "older line\n",
                to: "older line\nnew line"
            ),
            TextEdit(
                operation: .insert,
                characterOffset: 11,
                removed: "",
                inserted: "new line"
            )
        )
    }

    func testReplacementWithUnicode() {
        XCTAssertEqual(
            minimalTextEdit(from: "A 🐈 sat", to: "A 🐕 ran"),
            TextEdit(operation: .replace, characterOffset: 2, removed: "🐈 sat", inserted: "🐕 ran")
        )
    }

    func testNoEdit() {
        XCTAssertTrue(minimalTextEdit(from: "same", to: "same").isEmpty)
    }

    func testSemanticCursorContextUsesTextRatherThanPixels() {
        XCTAssertEqual(
            semanticCursorContext(
                in: "α🐈beta",
                selectionStartUTF16: 1,
                selectionLengthUTF16: 2,
                surroundingCharacterCount: 2
            ),
            SemanticCursorContext(
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
            )
        )
    }

    func testCursorInsideSurrogatePairIsRejected() {
        XCTAssertNil(
            semanticCursorContext(
                in: "🐈",
                selectionStartUTF16: 1,
                selectionLengthUTF16: 0
            )
        )
    }

    func testCursorFidelityDoesNotRepairOffsets() {
        XCTAssertEqual(
            cursorFidelityStatus(
                initialCursorOffset: 4,
                earliestObservedMutationOffset: 8,
                terminalEditOffset: 8
            ),
            .initialCursorDiffersFromEarliestObservedMutation
        )
        XCTAssertEqual(
            cursorFidelityStatus(
                initialCursorOffset: 4,
                earliestObservedMutationOffset: 4,
                terminalEditOffset: 9
            ),
            .terminalEditMovedAfterAlignedStart
        )
    }

    func testSupportedEditableRolesAcrossApplications() {
        XCTAssertTrue(isSupportedEditableSurface(role: "AXTextArea", subrole: nil))
        XCTAssertTrue(isSupportedEditableSurface(role: "AXTextField", subrole: nil))
        XCTAssertTrue(isSupportedEditableSurface(role: "AXComboBox", subrole: nil))
        XCTAssertFalse(isSupportedEditableSurface(role: "AXWebArea", subrole: nil))
    }

    func testSecureEditableSurfaceIsRejected() {
        XCTAssertTrue(isSecureEditableSurface(role: "AXTextField", subrole: "AXSecureTextField"))
        XCTAssertFalse(
            isSupportedEditableSurface(role: "AXTextField", subrole: "AXSecureTextField")
        )
    }

    func testConfirmedPlaceholderBecomesEmptyLogicalValue() {
        XCTAssertTrue(
            valueRepresentsPlaceholder("\nDo anything", placeholderValue: "Do anything")
        )
        XCTAssertEqual(
            logicalEditableValue("Ask Gemini\n", placeholderValue: "Ask Gemini"),
            ""
        )
    }

    func testUnconfirmedPromptLikeTextRemainsUserContent() {
        XCTAssertFalse(valueRepresentsPlaceholder("Do anything", placeholderValue: nil))
        XCTAssertEqual(
            logicalEditableValue("Do anything", placeholderValue: "Ask Gemini"),
            "Do anything"
        )
    }

    func testKnownEmptyPromptChromeIsSeparatedFromEditableText() {
        XCTAssertEqual(
            unpopulatedSurfacePrompt(
                bundleIdentifier: "com.openai.codex",
                fieldDescription: "Do anything",
                value: "\nDo anything",
                placeholderValue: nil,
                valueRepresentedPlaceholder: false
            ),
            "Do anything"
        )
        XCTAssertEqual(
            unpopulatedSurfacePrompt(
                bundleIdentifier: "com.google.Chrome",
                fieldDescription: "Enter a prompt for Gemini",
                value: "Ask Gemini\n",
                placeholderValue: nil,
                valueRepresentedPlaceholder: false
            ),
            "Ask Gemini"
        )
    }

    func testOrdinaryPromptContentIsNotDiscarded() {
        XCTAssertNil(
            unpopulatedSurfacePrompt(
                bundleIdentifier: "com.google.Chrome",
                fieldDescription: "Enter a prompt for Gemini",
                value: "actual draft",
                placeholderValue: nil,
                valueRepresentedPlaceholder: false
            )
        )
    }

    func testNewlyVisibleLinesPreservesViewportOverlap() {
        XCTAssertEqual(
            newlyVisibleLines(previous: "alpha\nbeta", current: "beta\ngamma\ngamma"),
            "gamma"
        )
    }

    func testReturningToEarlierTextCanBecomeVisibleAgain() {
        XCTAssertEqual(
            newlyVisibleLines(previous: "later", current: "earlier"),
            "earlier"
        )
    }

    func testChromeAuxiliarySurfacesAreSuppressed() {
        XCTAssertTrue(
            isChromeAuxiliarySurface(
                bundleIdentifier: "com.google.Chrome",
                width: 1455,
                height: 158
            )
        )
        XCTAssertTrue(
            isChromeAuxiliarySurface(
                bundleIdentifier: "com.google.Chrome",
                width: 13,
                height: 1440
            )
        )
        XCTAssertFalse(
            isChromeAuxiliarySurface(
                bundleIdentifier: "com.google.Chrome",
                width: 1455,
                height: 1318
            )
        )
        XCTAssertFalse(
            isChromeAuxiliarySurface(
                bundleIdentifier: "md.obsidian",
                width: 800,
                height: 200
            )
        )
    }
}
