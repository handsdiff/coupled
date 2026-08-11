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
            minimalTextEdit(from: "one two three", to: "one three", preferredOffset: 4),
            TextEdit(operation: .delete, characterOffset: 4, removed: "two ", inserted: "")
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
}
