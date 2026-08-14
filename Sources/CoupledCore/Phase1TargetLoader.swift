import Foundation

public enum Phase1TargetLoaderError: Error, Equatable {
    case unknownSegmentType(String)
    case unresolvedPaste
    case emptyPasteMarker
    case pasteMarkerContainsEOS
}

public struct Phase1LoadedTarget: Equatable, Sendable {
    public let tokenIDs: [Int]
    public let lossMask: [Bool]

    public init(tokenIDs: [Int], lossMask: [Bool]) {
        self.tokenIDs = tokenIDs
        self.lossMask = lossMask
    }
}

/// Tokenizer-independent packing contract for a Phase 1 target. The caller's
/// tokenizer handles both authored text and the reserved paste marker as
/// ordinary text. Exactly one loss-bearing EOS token terminates the target.
public func loadPhase1Target(
    segments: [WriteAuthorshipSegment],
    pasteMarker: String,
    eosTokenID: Int,
    tokenizeOrdinaryText: (String) throws -> [Int]
) throws -> Phase1LoadedTarget {
    let pasteMarkerTokenIDs = try tokenizeOrdinaryText(pasteMarker)
    guard !pasteMarkerTokenIDs.isEmpty else {
        throw Phase1TargetLoaderError.emptyPasteMarker
    }
    guard !pasteMarkerTokenIDs.contains(eosTokenID) else {
        throw Phase1TargetLoaderError.pasteMarkerContainsEOS
    }
    var tokenIDs = [Int]()
    for segment in segments {
        switch segment.type {
        case "authored_text":
            tokenIDs += try tokenizeOrdinaryText(segment.content)
        case "paste":
            guard segment.clipboardSnapshotID != nil,
                  segment.pasteCheckpointID != nil else {
                throw Phase1TargetLoaderError.unresolvedPaste
            }
            tokenIDs += pasteMarkerTokenIDs
        default:
            throw Phase1TargetLoaderError.unknownSegmentType(segment.type)
        }
    }
    tokenIDs.append(eosTokenID)
    return Phase1LoadedTarget(
        tokenIDs: tokenIDs,
        lossMask: Array(repeating: true, count: tokenIDs.count)
    )
}
