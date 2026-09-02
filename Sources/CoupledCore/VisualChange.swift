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

public struct VisualWriteSurfaceLinkage: Equatable, Sendable {
    public let authoritativeWindowID: UInt32?
    public let monitoredWindowID: UInt32?
    public let independentlyResolvedWindowID: UInt32?
    public let disposition: String
    public let usesMonitoredSurface: Bool

    public init(
        authoritativeWindowID: UInt32?,
        monitoredWindowID: UInt32?,
        independentlyResolvedWindowID: UInt32?,
        disposition: String,
        usesMonitoredSurface: Bool
    ) {
        self.authoritativeWindowID = authoritativeWindowID
        self.monitoredWindowID = monitoredWindowID
        self.independentlyResolvedWindowID = independentlyResolvedWindowID
        self.disposition = disposition
        self.usesMonitoredSurface = usesMonitoredSurface
    }
}

/// Chooses the already-observed visual surface for a WRITE whenever it still
/// belongs to the focused, frontmost process. A second Core Graphics lookup is
/// retained only as audit evidence and as a fallback when no monitored surface
/// can be trusted.
public func linkVisualSurfaceToWrite(
    monitoredProcessIdentifier: Int32?,
    monitoredWindowID: UInt32?,
    independentlyResolvedWindowID: UInt32?,
    writeProcessIdentifier: Int32,
    frontmostProcessIdentifier: Int32?
) -> VisualWriteSurfaceLinkage {
    let monitoredProcessMatches = monitoredProcessIdentifier == writeProcessIdentifier
    let frontmostProcessMatches = frontmostProcessIdentifier == writeProcessIdentifier
    if monitoredProcessMatches, frontmostProcessMatches, let monitoredWindowID {
        let disposition: String
        if independentlyResolvedWindowID == monitoredWindowID {
            disposition = "monitored_surface_matches_independent_lookup"
        } else if independentlyResolvedWindowID == nil {
            disposition = "monitored_surface_used_independent_lookup_missing"
        } else {
            disposition = "monitored_surface_used_independent_lookup_disagreed"
        }
        return VisualWriteSurfaceLinkage(
            authoritativeWindowID: monitoredWindowID,
            monitoredWindowID: monitoredWindowID,
            independentlyResolvedWindowID: independentlyResolvedWindowID,
            disposition: disposition,
            usesMonitoredSurface: true
        )
    }

    let disposition: String
    if monitoredProcessIdentifier == nil || monitoredWindowID == nil {
        disposition = "independent_fallback_monitor_unavailable"
    } else if !monitoredProcessMatches {
        disposition = "independent_fallback_monitor_process_mismatch"
    } else {
        disposition = "independent_fallback_frontmost_process_mismatch"
    }
    return VisualWriteSurfaceLinkage(
        authoritativeWindowID: independentlyResolvedWindowID,
        monitoredWindowID: monitoredWindowID,
        independentlyResolvedWindowID: independentlyResolvedWindowID,
        disposition: disposition,
        usesMonitoredSurface: false
    )
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
