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
    to after: String,
    preferredOffset: Int? = nil
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

    // Repeated characters at an edit boundary can make a pure prefix/suffix
    // diff choose a technically minimal but behaviorally misleading offset.
    // The pre-edit cursor or selection is stronger evidence when available.
    if let preferredOffset, preferredOffset >= 0 {
        prefix = min(prefix, preferredOffset)
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
