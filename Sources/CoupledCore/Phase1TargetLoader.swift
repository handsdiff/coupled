import Foundation

public enum Phase1TargetLoaderError: Error, Equatable {
    case unknownSegmentType(String)
    case unresolvedPaste
    case pasteTokenEqualsEOS
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
/// tokenizer handles authored text only. Every proven paste becomes one atomic
/// action token, and exactly one loss-bearing EOS token terminates the target.
public func loadPhase1Target(
    segments: [WriteAuthorshipSegment],
    pasteTokenID: Int,
    eosTokenID: Int,
    tokenizeAuthoredText: (String) throws -> [Int]
) throws -> Phase1LoadedTarget {
    guard pasteTokenID != eosTokenID else {
        throw Phase1TargetLoaderError.pasteTokenEqualsEOS
    }
    var tokenIDs = [Int]()
    for segment in segments {
        switch segment.type {
        case "authored_text":
            tokenIDs += try tokenizeAuthoredText(segment.content)
        case "paste":
            guard segment.clipboardSnapshotID != nil,
                  segment.pasteCheckpointID != nil else {
                throw Phase1TargetLoaderError.unresolvedPaste
            }
            tokenIDs.append(pasteTokenID)
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
