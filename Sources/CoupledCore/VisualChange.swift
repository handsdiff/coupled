import Foundation

public struct VisualFrameFingerprint: Equatable, Sendable {
    public let width: Int
    public let height: Int
    public let samples: [UInt8]

    public init(width: Int, height: Int, samples: [UInt8]) {
        precondition(width > 0 && height > 0)
        precondition(samples.count == width * height)
        self.width = width
        self.height = height
        self.samples = samples
    }
}

public struct VisualDifference: Equatable, Sendable {
    public let comparedSampleCount: Int
    public let changedSampleCount: Int
    public let changedFraction: Double
    public let meanAbsoluteDifference: Double
    public let pixelThreshold: UInt8
    public let fractionThreshold: Double
    public let isMaterial: Bool

    public init(
        comparedSampleCount: Int,
        changedSampleCount: Int,
        changedFraction: Double,
        meanAbsoluteDifference: Double,
        pixelThreshold: UInt8,
        fractionThreshold: Double,
        isMaterial: Bool
    ) {
        self.comparedSampleCount = comparedSampleCount
        self.changedSampleCount = changedSampleCount
        self.changedFraction = changedFraction
        self.meanAbsoluteDifference = meanAbsoluteDifference
        self.pixelThreshold = pixelThreshold
        self.fractionThreshold = fractionThreshold
        self.isMaterial = isMaterial
    }
}

/// Compares small grayscale frame fingerprints. The per-sample threshold
/// suppresses antialiasing and caret noise; the fractional gate prevents one
/// isolated pixel from becoming a READ boundary.
public func visualDifference(
    from previous: VisualFrameFingerprint,
    to current: VisualFrameFingerprint,
    pixelThreshold: UInt8,
    fractionThreshold: Double
) -> VisualDifference? {
    guard previous.width == current.width,
          previous.height == current.height,
          previous.samples.count == current.samples.count,
          !previous.samples.isEmpty,
          fractionThreshold >= 0,
          fractionThreshold <= 1 else { return nil }

    var changed = 0
    var absoluteDifference = 0
    for (left, right) in zip(previous.samples, current.samples) {
        let difference = abs(Int(left) - Int(right))
        absoluteDifference += difference
        if difference >= Int(pixelThreshold) {
            changed += 1
        }
    }
    let count = previous.samples.count
    let fraction = Double(changed) / Double(count)
    return VisualDifference(
        comparedSampleCount: count,
        changedSampleCount: changed,
        changedFraction: fraction,
        meanAbsoluteDifference: Double(absoluteDifference) / Double(count),
        pixelThreshold: pixelThreshold,
        fractionThreshold: fractionThreshold,
        isMaterial: fraction >= fractionThreshold
    )
}
