import Foundation

public struct WriteAuthorshipSegment: Codable, Equatable, Sendable {
    public let type: String
    public let content: String
    public let clipboardSnapshotID: String?
    public let pasteCheckpointID: String?

    public init(
        type: String,
        content: String,
        clipboardSnapshotID: String? = nil,
        pasteCheckpointID: String? = nil
    ) {
        self.type = type
        self.content = content
        self.clipboardSnapshotID = clipboardSnapshotID
        self.pasteCheckpointID = pasteCheckpointID
    }

    public static func authored(_ content: String) -> WriteAuthorshipSegment {
        WriteAuthorshipSegment(type: "authored_text", content: content)
    }

    public static func paste(
        _ content: String,
        clipboardSnapshotID: String,
        pasteCheckpointID: String
    ) -> WriteAuthorshipSegment {
        WriteAuthorshipSegment(
            type: "paste",
            content: content,
            clipboardSnapshotID: clipboardSnapshotID,
            pasteCheckpointID: pasteCheckpointID
        )
    }
}

public struct ProvenPasteMutation: Equatable, Sendable {
    public let checkpointID: String
    public let clipboardSnapshotID: String
    public let characterOffset: Int
    public let inserted: String

    public init(
        checkpointID: String,
        clipboardSnapshotID: String,
        characterOffset: Int,
        inserted: String
    ) {
        self.checkpointID = checkpointID
        self.clipboardSnapshotID = clipboardSnapshotID
        self.characterOffset = characterOffset
        self.inserted = inserted
    }
}

public struct WriteAuthorshipResult: Equatable, Sendable {
    public let segments: [WriteAuthorshipSegment]
    public let resolution: String

    public init(segments: [WriteAuthorshipSegment], resolution: String) {
        self.segments = segments
        self.resolution = resolution
    }
}

public struct SegmentedWriteCompletion: Equatable, Sendable {
    public let segments: [WriteAuthorshipSegment]
    public let resolvedContent: String

    public init(segments: [WriteAuthorshipSegment], resolvedContent: String) {
        self.segments = segments
        self.resolvedContent = resolvedContent
    }
}

/// Composes one explicitly grounded paste boundary whose Accessibility value
/// starts a new empty observation epoch. Each authored side is reduced to its
/// local final diff, so temporary corrections never become target content.
public func segmentedGroundedPasteCompletion(
    initialValue: String,
    prePasteValue: String,
    postPasteValue: String,
    terminalValue: String,
    clipboardText: String,
    clipboardSnapshotID: String,
    pasteCheckpointID: String
) -> SegmentedWriteCompletion? {
    guard postPasteValue.isEmpty,
          !clipboardText.isEmpty,
          !terminalValue.contains(clipboardText) else { return nil }
    let prefixEdit = minimalTextEdit(from: initialValue, to: prePasteValue)
    let suffixEdit = minimalTextEdit(from: postPasteValue, to: terminalValue)
    guard applying(prefixEdit, to: initialValue) == prePasteValue,
          applying(suffixEdit, to: postPasteValue) == terminalValue else { return nil }

    var segments = [WriteAuthorshipSegment]()
    if !prefixEdit.inserted.isEmpty { segments.append(.authored(prefixEdit.inserted)) }
    segments.append(.paste(
        clipboardText,
        clipboardSnapshotID: clipboardSnapshotID,
        pasteCheckpointID: pasteCheckpointID
    ))
    if !suffixEdit.inserted.isEmpty { segments.append(.authored(suffixEdit.inserted)) }
    return SegmentedWriteCompletion(
        segments: segments,
        resolvedContent: segments.map(\.content).joined()
    )
}

/// Projects proven paste transitions into the final net insertion. A paste is
/// accepted only when its exact inserted span still occurs at its observed
/// document-relative offset. Later edits which obscure that provenance remain
/// unresolved instead of receiving false authorship supervision.
public func writeAuthorship(
    overallEdit: TextEdit,
    pasteMutations: [ProvenPasteMutation]
) -> WriteAuthorshipResult {
    guard !pasteMutations.isEmpty else {
        return WriteAuthorshipResult(
            segments: overallEdit.inserted.isEmpty ? [] : [.authored(overallEdit.inserted)],
            resolution: "resolved"
        )
    }

    let inserted = Array(overallEdit.inserted)
    var spans = [(start: Int, end: Int, mutation: ProvenPasteMutation)]()
    for mutation in pasteMutations {
        let paste = Array(mutation.inserted)
        let start = mutation.characterOffset - overallEdit.characterOffset
        let end = start + paste.count
        guard !paste.isEmpty,
              start >= 0,
              end <= inserted.count,
              Array(inserted[start..<end]) == paste else {
            return WriteAuthorshipResult(segments: [], resolution: "paste_span_not_preserved")
        }
        spans.append((start, end, mutation))
    }
    spans.sort {
        if $0.start != $1.start { return $0.start < $1.start }
        return $0.end < $1.end
    }
    var cursor = 0
    var segments = [WriteAuthorshipSegment]()
    for span in spans {
        guard span.start >= cursor else {
            return WriteAuthorshipResult(segments: [], resolution: "paste_spans_overlap")
        }
        if span.start > cursor {
            segments.append(.authored(String(inserted[cursor..<span.start])))
        }
        segments.append(.paste(
            span.mutation.inserted,
            clipboardSnapshotID: span.mutation.clipboardSnapshotID,
            pasteCheckpointID: span.mutation.checkpointID
        ))
        cursor = span.end
    }
    if cursor < inserted.count {
        segments.append(.authored(String(inserted[cursor...])))
    }
    return WriteAuthorshipResult(segments: segments, resolution: "resolved")
}
