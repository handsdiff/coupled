public struct AdjacentViewportDeduplicator {
    private var previous: Signature?

    public init() {}

    public mutating func contentToEmit(
        contextIdentifier: String,
        viewportContent: String
    ) -> String? {
        let previousContent = previous?.contextIdentifier == contextIdentifier
            ? previous?.viewportContent
            : nil
        let emittedContent = newlyVisibleLines(
            previous: previousContent,
            current: viewportContent
        )
        let signature = Signature(
            contextIdentifier: contextIdentifier,
            viewportContent: viewportContent
        )
        previous = signature
        return emittedContent.isEmpty ? nil : emittedContent
    }

    public mutating func reset() {
        previous = nil
    }

    private struct Signature: Equatable {
        let contextIdentifier: String
        let viewportContent: String
    }
}
