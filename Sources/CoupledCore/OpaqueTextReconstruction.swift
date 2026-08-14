import Foundation

/// A small, application-independent editor model used only when Accessibility
/// exposes an editable target but never exposes its text or selection changes.
public struct OpaqueTextState: Codable, Equatable, Sendable {
    public private(set) var text: String
    public private(set) var anchor: Int
    public private(set) var caret: Int

    public init(text: String = "", anchor: Int = 0, caret: Int = 0) {
        self.text = text
        self.anchor = anchor
        self.caret = caret
    }

    public var selectionStart: Int { min(anchor, caret) }
    public var selectionLength: Int { abs(caret - anchor) }

    public mutating func apply(_ action: OpaqueTextAction) -> OpaqueTextApplyResult {
        let characters = Array(text)
        guard anchor >= 0, caret >= 0,
              anchor <= characters.count, caret <= characters.count else {
            return .unsupported("state_out_of_bounds")
        }

        switch action {
        case .insert(let value):
            replaceSelection(with: Array(value))
            return .applied
        case .backspace:
            if selectionLength > 0 {
                replaceSelection(with: [])
            } else if caret > 0 {
                anchor = caret - 1
                replaceSelection(with: [])
            }
            return .applied
        case .deleteForward:
            if selectionLength > 0 {
                replaceSelection(with: [])
            } else if caret < characters.count {
                anchor = caret + 1
                replaceSelection(with: [])
            }
            return .applied
        case .moveLeft(let extending):
            if extending {
                caret = max(0, caret - 1)
            } else {
                let destination = selectionLength > 0 ? selectionStart : max(0, caret - 1)
                anchor = destination
                caret = destination
            }
            return .applied
        case .moveRight(let extending):
            if extending {
                caret = min(characters.count, caret + 1)
            } else {
                let destination = selectionLength > 0
                    ? selectionStart + selectionLength
                    : min(characters.count, caret + 1)
                anchor = destination
                caret = destination
            }
            return .applied
        case .moveToStart(let extending):
            if extending {
                caret = 0
            } else {
                anchor = 0
                caret = 0
            }
            return .applied
        case .moveToEnd(let extending):
            if extending {
                caret = characters.count
            } else {
                anchor = characters.count
                caret = characters.count
            }
            return .applied
        case .selectAll:
            anchor = 0
            caret = characters.count
            return .applied
        case .unsupported(let reason):
            return .unsupported(reason)
        }
    }

    public func semanticCursorContext(
        surroundingCharacterCount: Int
    ) -> SemanticCursorContext? {
        guard surroundingCharacterCount > 0 else { return nil }
        let characters = Array(text)
        let start = selectionStart
        let end = start + selectionLength
        guard start >= 0, end <= characters.count else { return nil }
        let leftStart = max(0, start - surroundingCharacterCount)
        let rightEnd = min(characters.count, end + surroundingCharacterCount)
        let selectedEnd = min(end, start + (2 * surroundingCharacterCount))
        let startIndex = text.index(text.startIndex, offsetBy: start)
        let endIndex = text.index(text.startIndex, offsetBy: end)
        let startUTF16 = text[..<startIndex].utf16.count
        let selectionUTF16 = text[startIndex..<endIndex].utf16.count
        return SemanticCursorContext(
            leftContext: String(characters[leftStart..<start]),
            selectedText: String(characters[start..<selectedEnd]),
            rightContext: String(characters[end..<rightEnd]),
            selectionStartCharacters: start,
            selectionLengthCharacters: selectionLength,
            selectionStartUTF16: startUTF16,
            selectionLengthUTF16: selectionUTF16,
            fieldCharacterCount: characters.count,
            leftContextWasTruncated: leftStart > 0,
            selectedTextWasTruncated: selectedEnd < end,
            rightContextWasTruncated: rightEnd < characters.count
        )
    }

    private mutating func replaceSelection(with replacement: [Character]) {
        var characters = Array(text)
        let start = selectionStart
        let end = start + selectionLength
        characters.replaceSubrange(start..<end, with: replacement)
        text = String(characters)
        caret = start + replacement.count
        anchor = caret
    }
}

public enum OpaqueTextAction: Equatable, Sendable {
    case insert(String)
    case backspace
    case deleteForward
    case moveLeft(extendingSelection: Bool)
    case moveRight(extendingSelection: Bool)
    case moveToStart(extendingSelection: Bool)
    case moveToEnd(extendingSelection: Bool)
    case selectAll
    case unsupported(String)
}

public enum OpaqueTextApplyResult: Equatable, Sendable {
    case applied
    case unsupported(String)
}
