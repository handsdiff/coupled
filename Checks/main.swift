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
    minimalTextEdit(from: "one two three", to: "one three", preferredOffset: 4)
        == TextEdit(operation: .delete, characterOffset: 4, removed: "two ", inserted: ""),
    "middle deletion"
)
expect(
    minimalTextEdit(from: "A 🐈 sat", to: "A 🐕 ran")
        == TextEdit(operation: .replace, characterOffset: 2, removed: "🐈 sat", inserted: "🐕 ran"),
    "unicode replacement"
)
expect(minimalTextEdit(from: "same", to: "same").isEmpty, "no-op")
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

print("CoupledCore checks passed")
