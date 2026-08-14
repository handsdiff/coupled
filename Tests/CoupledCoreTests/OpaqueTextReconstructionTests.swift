import XCTest
@testable import CoupledCore

final class OpaqueTextReconstructionTests: XCTestCase {
    func testLinearTypingAndCorrection() {
        var state = OpaqueTextState()
        XCTAssertEqual(state.apply(.insert("helo")), .applied)
        XCTAssertEqual(state.apply(.backspace), .applied)
        XCTAssertEqual(state.apply(.insert("lo")), .applied)
        XCTAssertEqual(state.text, "hello")
        XCTAssertEqual(state.selectionStart, 5)
        XCTAssertEqual(state.selectionLength, 0)
    }

    func testSelectionReplacement() {
        var state = OpaqueTextState(text: "hello world", anchor: 11, caret: 11)
        for _ in 0..<5 { XCTAssertEqual(state.apply(.moveLeft(extendingSelection: true)), .applied) }
        XCTAssertEqual(state.apply(.insert("there")), .applied)
        XCTAssertEqual(state.text, "hello there")
        XCTAssertEqual(state.caret, 11)
    }

    func testMoveBoundaryCreatesSemanticConditioning() {
        var state = OpaqueTextState(text: "alpha beta", anchor: 10, caret: 10)
        XCTAssertEqual(state.apply(.moveToStart(extendingSelection: false)), .applied)
        XCTAssertEqual(state.apply(.moveRight(extendingSelection: false)), .applied)
        XCTAssertEqual(
            state.semanticCursorContext(surroundingCharacterCount: 4),
            SemanticCursorContext(
                leftContext: "a",
                selectedText: "",
                rightContext: "lpha",
                selectionStartCharacters: 1,
                selectionLengthCharacters: 0,
                selectionStartUTF16: 1,
                selectionLengthUTF16: 0,
                fieldCharacterCount: 10,
                leftContextWasTruncated: false,
                selectedTextWasTruncated: false,
                rightContextWasTruncated: true
            )
        )
    }

    func testUnicodeOffsetsRemainCharacterBased() {
        var state = OpaqueTextState(text: "a🐈b", anchor: 3, caret: 3)
        XCTAssertEqual(state.apply(.moveLeft(extendingSelection: true)), .applied)
        let context = state.semanticCursorContext(surroundingCharacterCount: 2)
        XCTAssertEqual(context?.selectedText, "b")
        XCTAssertEqual(context?.selectionStartCharacters, 2)
        XCTAssertEqual(context?.selectionStartUTF16, 3)
    }

    func testUnsupportedActionDoesNotMutateState() {
        var state = OpaqueTextState(text: "draft", anchor: 5, caret: 5)
        XCTAssertEqual(state.apply(.unsupported("vertical_navigation")), .unsupported("vertical_navigation"))
        XCTAssertEqual(state, OpaqueTextState(text: "draft", anchor: 5, caret: 5))
    }
}
