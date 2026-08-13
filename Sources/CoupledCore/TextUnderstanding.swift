import Foundation

public enum EditOperation: String, Codable, Equatable {
    case insert
    case delete
    case replace
    case none
}

public struct TextEdit: Codable, Equatable {
    public let operation: EditOperation
    public let characterOffset: Int
    public let removed: String
    public let inserted: String

    public init(
        operation: EditOperation,
        characterOffset: Int,
        removed: String,
        inserted: String
    ) {
        self.operation = operation
        self.characterOffset = characterOffset
        self.removed = removed
        self.inserted = inserted
    }

    public var isEmpty: Bool { operation == .none }
}

/// Returns the smallest contiguous edit that transforms `before` into `after`.
/// This intentionally models one settled write burst, not individual keystrokes.
public func minimalTextEdit(
    from before: String,
    to after: String
) -> TextEdit {
    guard before != after else {
        return TextEdit(operation: .none, characterOffset: before.count, removed: "", inserted: "")
    }

    let old = Array(before)
    let new = Array(after)
    let sharedLimit = min(old.count, new.count)
    var prefix = 0

    while prefix < sharedLimit, old[prefix] == new[prefix] {
        prefix += 1
    }

    var oldSuffix = old.count
    var newSuffix = new.count
    while oldSuffix > prefix,
          newSuffix > prefix,
          old[oldSuffix - 1] == new[newSuffix - 1] {
        oldSuffix -= 1
        newSuffix -= 1
    }

    let removed = String(old[prefix..<oldSuffix])
    let inserted = String(new[prefix..<newSuffix])
    let operation: EditOperation
    if removed.isEmpty {
        operation = .insert
    } else if inserted.isEmpty {
        operation = .delete
    } else {
        operation = .replace
    }

    return TextEdit(
        operation: operation,
        characterOffset: prefix,
        removed: removed,
        inserted: inserted
    )
}

/// Converts an Accessibility UTF-16 selection offset into the Character offset
/// used by TextEdit. Returns nil when AX points inside a surrogate pair.
public func characterOffset(in value: String, utf16Offset: Int?) -> Int? {
    guard let utf16Offset, utf16Offset >= 0 else { return nil }
    let utf16 = value.utf16
    guard let utf16Index = utf16.index(
        utf16.startIndex,
        offsetBy: utf16Offset,
        limitedBy: utf16.endIndex
    ),
    let stringIndex = String.Index(utf16Index, within: value) else {
        return nil
    }
    return value.distance(from: value.startIndex, to: stringIndex)
}

/// A bounded semantic representation of a caret or selection inside an
/// editable value. Accessibility reports selection coordinates in UTF-16;
/// character coordinates count user-perceived Swift Characters.
public struct SemanticCursorContext: Codable, Equatable, Sendable {
    public let leftContext: String
    public let selectedText: String
    public let rightContext: String
    public let selectionStartCharacters: Int
    public let selectionLengthCharacters: Int
    public let selectionStartUTF16: Int
    public let selectionLengthUTF16: Int
    public let fieldCharacterCount: Int
    public let leftContextWasTruncated: Bool
    public let selectedTextWasTruncated: Bool
    public let rightContextWasTruncated: Bool

    public init(
        leftContext: String,
        selectedText: String,
        rightContext: String,
        selectionStartCharacters: Int,
        selectionLengthCharacters: Int,
        selectionStartUTF16: Int,
        selectionLengthUTF16: Int,
        fieldCharacterCount: Int,
        leftContextWasTruncated: Bool,
        selectedTextWasTruncated: Bool,
        rightContextWasTruncated: Bool
    ) {
        self.leftContext = leftContext
        self.selectedText = selectedText
        self.rightContext = rightContext
        self.selectionStartCharacters = selectionStartCharacters
        self.selectionLengthCharacters = selectionLengthCharacters
        self.selectionStartUTF16 = selectionStartUTF16
        self.selectionLengthUTF16 = selectionLengthUTF16
        self.fieldCharacterCount = fieldCharacterCount
        self.leftContextWasTruncated = leftContextWasTruncated
        self.selectedTextWasTruncated = selectedTextWasTruncated
        self.rightContextWasTruncated = rightContextWasTruncated
    }
}

/// Extracts semantic text anchors around an Accessibility selection. Selected
/// text is bounded to twice the surrounding radius; the complete value remains
/// raw evidence so later conversion versions can choose another representation.
public func semanticCursorContext(
    in value: String,
    selectionStartUTF16: Int?,
    selectionLengthUTF16: Int?,
    surroundingCharacterCount: Int = 512
) -> SemanticCursorContext? {
    guard let selectionStartUTF16,
          let selectionLengthUTF16,
          selectionStartUTF16 >= 0,
          selectionLengthUTF16 >= 0,
          surroundingCharacterCount > 0,
          surroundingCharacterCount <= Int.max / 2 else {
        return nil
    }
    let (selectionEndUTF16, overflowed) = selectionStartUTF16.addingReportingOverflow(
        selectionLengthUTF16
    )
    guard !overflowed,
          let selectionStart = characterOffset(in: value, utf16Offset: selectionStartUTF16),
          let selectionEnd = characterOffset(in: value, utf16Offset: selectionEndUTF16),
          selectionEnd >= selectionStart else {
        return nil
    }

    let characters = Array(value)
    guard selectionEnd <= characters.count else { return nil }
    let leftStart = max(0, selectionStart - surroundingCharacterCount)
    let rightEnd = min(characters.count, selectionEnd + surroundingCharacterCount)
    let selectedEnd = min(selectionEnd, selectionStart + (2 * surroundingCharacterCount))

    return SemanticCursorContext(
        leftContext: String(characters[leftStart..<selectionStart]),
        selectedText: String(characters[selectionStart..<selectedEnd]),
        rightContext: String(characters[selectionEnd..<rightEnd]),
        selectionStartCharacters: selectionStart,
        selectionLengthCharacters: selectionEnd - selectionStart,
        selectionStartUTF16: selectionStartUTF16,
        selectionLengthUTF16: selectionLengthUTF16,
        fieldCharacterCount: characters.count,
        leftContextWasTruncated: leftStart > 0,
        selectedTextWasTruncated: selectedEnd < selectionEnd,
        rightContextWasTruncated: rightEnd < characters.count
    )
}

/// Applies a derived edit only when its removed span exactly matches the source.
/// This is the invariant used before a write becomes a training candidate.
public func applying(_ edit: TextEdit, to value: String) -> String? {
    guard edit.characterOffset >= 0 else { return nil }
    let characters = Array(value)
    let removed = Array(edit.removed)
    let start = edit.characterOffset
    let end = start + removed.count
    guard start <= characters.count,
          end <= characters.count,
          Array(characters[start..<end]) == removed else {
        return nil
    }
    return String(characters[..<start]) + edit.inserted + String(characters[end...])
}

/// A deliberately simple overlap signal for read snapshots. Lines present in
/// the current viewport but absent from the immediately preceding viewport are
/// returned in display order. Returning to old material after another viewport
/// therefore remains observable as a reread.
public func newlyVisibleLines(previous: String?, current: String) -> String {
    guard let previous, !previous.isEmpty else { return current }

    let priorLines = Set(
        previous
            .split(separator: "\n", omittingEmptySubsequences: true)
            .map { normalizedLine(String($0)) }
            .filter { !$0.isEmpty }
    )

    var emitted = Set<String>()
    let novel = current
        .split(separator: "\n", omittingEmptySubsequences: true)
        .map(String.init)
        .filter { line in
            let normalized = normalizedLine(line)
            guard !normalized.isEmpty,
                  !priorLines.contains(normalized),
                  !emitted.contains(normalized) else {
                return false
            }
            emitted.insert(normalized)
            return true
        }

    return novel.joined(separator: "\n")
}

public func normalizedLine(_ value: String) -> String {
    value
        .split(whereSeparator: { $0.isWhitespace })
        .joined(separator: " ")
        .trimmingCharacters(in: .whitespacesAndNewlines)
}

/// Splits event-provided text into user-perceived characters while removing
/// control and function-key values that do not represent inserted text.
public func writableCharacters(in text: String) -> [Character] {
    text.filter { character in
        if character == "\r" || character == "\n" || character == "\t" {
            return true
        }

        let scalars = character.unicodeScalars
        guard !scalars.isEmpty else { return false }
        if scalars.allSatisfy({ CharacterSet.controlCharacters.contains($0) }) {
            return false
        }
        // macOS reports arrows and several function keys in this private-use block.
        if scalars.allSatisfy({ (0xF700...0xF8FF).contains(Int($0.value)) }) {
            return false
        }
        return true
    }
}

/// Roles whose complete AXValue can be tested with the retained-element diff.
/// A supported role is still rejected when macOS marks it as secure.
public func isSupportedEditableSurface(role: String, subrole: String?) -> Bool {
    guard !isSecureEditableSurface(role: role, subrole: subrole) else { return false }
    return ["AXTextArea", "AXTextField", "AXComboBox"].contains(role)
}

public func isSecureEditableSurface(role: String, subrole: String?) -> Bool {
    role == "AXSecureTextField" || subrole == "AXSecureTextField"
}

/// Chromium/Electron can expose an empty field's visual prompt as AXValue.
/// Treat it as UI only when AXPlaceholderValue independently confirms it.
public func valueRepresentsPlaceholder(_ value: String, placeholderValue: String?) -> Bool {
    guard let placeholderValue else { return false }
    let normalizedValue = value.trimmingCharacters(in: .whitespacesAndNewlines)
    let normalizedPlaceholder = placeholderValue.trimmingCharacters(in: .whitespacesAndNewlines)
    return !normalizedPlaceholder.isEmpty && normalizedValue == normalizedPlaceholder
}

public func logicalEditableValue(_ value: String, placeholderValue: String?) -> String {
    valueRepresentsPlaceholder(value, placeholderValue: placeholderValue) ? "" : value
}

/// True when deletion or cut is the only mutation-capable input observed in a
/// burst. Navigation, selection shortcuts, and Return may accompany removal
/// without providing evidence that the user inserted replacement text.
public func isRemovalOnlyWriteBurst(inputHints: Set<String>) -> Bool {
    guard inputHints.contains("delete") || inputHints.contains("cut") else { return false }
    let contentMutations: Set<String> = ["typed", "paste", "undo_redo"]
    return inputHints.isDisjoint(with: contentMutations)
}
