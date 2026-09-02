import AppKit
import CoupledCore
import Foundation
import ScreenCaptureKit

/// A bounded, raw-only shadow sensor for visual changes on the selected
/// eligible surface. It stores one completed frame in memory and never runs
/// OCR or persists continuous images. Promotion into authoritative READ
/// evidence is deliberately separate from validating this sensor.
final class ActiveSurfaceFrameMonitor {
    private let configuration: Configuration
    private let rawWriter: JSONLWriter

    private var timer: Timer?
    private var settlementTimer: Timer?
    private var captureInFlight = false
    private var generation: UInt64 = 0
    private var frameSequence: UInt64 = 0
    private var selectedSurface: ResolvedReadSurface?
    private var interactionPoint: CGPoint?
    private var previousFingerprint: VisualFrameFingerprint?
    private var baselineFingerprint: VisualFrameFingerprint?
    private var latestFrame: ShadowVisualFrame?
    private var pendingChange: PendingShadowVisualChange?
    private var activeWrite: ReadMutationBoundary?
    private var skippedCaptureCount = 0

    init(configuration: Configuration, rawWriter: JSONLWriter) {
        self.configuration = configuration
        self.rawWriter = rawWriter
    }

    func start() {
        guard timer == nil else { return }
        let timer = Timer(
            timeInterval: configuration.visualFrameInterval,
            repeats: true
        ) { [weak self] _ in
            self?.requestFrame()
        }
        self.timer = timer
        RunLoop.main.add(timer, forMode: .common)
    }

    func select(surface: ResolvedReadSurface, point: CGPoint) {
        if selectedSurface?.key != surface.key
            || selectedSurface?.matches(surface) != true {
            generation += 1
            settlementTimer?.invalidate()
            settlementTimer = nil
            selectedSurface = surface
            interactionPoint = point
            previousFingerprint = nil
            baselineFingerprint = nil
            latestFrame = nil
            pendingChange = nil
            activeWrite = nil
            requestFrame()
            return
        }
        interactionPoint = point
    }

    func linkWriteSurface(
        processIdentifier: Int32,
        independentlyResolvedWindowID: UInt32?
    ) -> VisualWriteSurfaceLinkage {
        linkVisualSurfaceToWrite(
            monitoredProcessIdentifier: selectedSurface?.processIdentifier,
            monitoredWindowID: selectedSurface?.windowID,
            independentlyResolvedWindowID: independentlyResolvedWindowID,
            writeProcessIdentifier: processIdentifier,
            frontmostProcessIdentifier: NSWorkspace.shared.frontmostApplication?
                .processIdentifier
        )
    }

    /// Called synchronously from the active event tap before the mutation is
    /// returned to the application. Only an already-completed frame can be
    /// considered causally safe here.
    func writeBegan(
        _ boundary: ReadMutationBoundary,
        linkage: VisualWriteSurfaceLinkage
    ) {
        if activeWrite?.attemptID == boundary.attemptID { return }
        guard let surface = selectedSurface,
              linkage.usesMonitoredSurface,
              surface.processIdentifier == boundary.processIdentifier,
              surface.windowID == linkage.authoritativeWindowID else {
            persistDiagnostic(
                event: "pre_write_surface_unlinked_shadow",
                frame: nil,
                difference: nil,
                boundary: boundary,
                linkage: linkage,
                firstChangedAt: pendingChange?.firstChangedAt,
                lastChangedAt: pendingChange?.lastChangedAt
            )
            return
        }

        let safeFrame = latestFrame.flatMap {
            $0.capturedAt < boundary.observedAt ? $0 : nil
        }
        let difference = safeFrame.flatMap { frame in
            baselineFingerprint.flatMap {
                visualDifference(
                    from: $0,
                    to: frame.fingerprint,
                    pixelThreshold: configuration.visualDifferencePixelThreshold,
                    fractionThreshold: configuration.visualDifferenceFractionThreshold
                )
            }
        }
        persistDiagnostic(
            event: "pre_write_checkpoint_shadow",
            frame: safeFrame,
            difference: difference,
            boundary: boundary,
            linkage: linkage,
            firstChangedAt: pendingChange?.firstChangedAt,
            lastChangedAt: pendingChange?.lastChangedAt
        )

        generation += 1
        settlementTimer?.invalidate()
        settlementTimer = nil
        pendingChange = nil
        activeWrite = boundary
    }

    func writeCompleted(_ completion: CompletedWriteCapture) {
        guard activeWrite?.attemptID == completion.attemptID else { return }
        persistDiagnostic(
            event: completion.endedWithUnmodifiedReturn
                ? "write_completed_after_return_shadow"
                : "write_completed_shadow",
            frame: latestFrame,
            difference: nil,
            boundary: activeWrite,
            linkage: nil,
            firstChangedAt: pendingChange?.firstChangedAt,
            lastChangedAt: pendingChange?.lastChangedAt
        )
        activeWrite = nil
    }

    private func requestFrame() {
        guard !configuration.isPaused(),
              !captureInFlight,
              let surface = selectedSurface,
              let point = interactionPoint,
              NSWorkspace.shared.frontmostApplication?.processIdentifier
                == surface.processIdentifier else {
            if captureInFlight { skippedCaptureCount += 1 }
            return
        }
        guard #available(macOS 15.2, *) else { return }

        captureInFlight = true
        let requestGeneration = generation
        let requestedAt = nowTimestamp()
        let expectedSurface = surface
        SCScreenshotManager.captureImage(in: surface.windowBounds) { [weak self] image, _ in
            guard let self else { return }
            let capturedAt = nowTimestamp()
            DispatchQueue.main.async {
                self.captureInFlight = false
                guard requestGeneration == self.generation,
                      let image,
                      self.selectedSurface?.matches(expectedSurface) == true,
                      NSWorkspace.shared.frontmostApplication?.processIdentifier
                        == expectedSurface.processIdentifier,
                      let fingerprint = makeVisualFrameFingerprint(
                        image,
                        width: self.configuration.visualFingerprintWidth,
                        height: self.configuration.visualFingerprintHeight
                      ) else { return }

                self.frameSequence += 1
                let frame = ShadowVisualFrame(
                    sequence: self.frameSequence,
                    requestedAt: requestedAt,
                    capturedAt: capturedAt,
                    surface: expectedSurface,
                    point: point,
                    fingerprint: fingerprint
                )
                self.accept(frame)
            }
        }
    }

    private func accept(_ frame: ShadowVisualFrame) {
        latestFrame = frame
        guard let previousFingerprint else {
            self.previousFingerprint = frame.fingerprint
            baselineFingerprint = frame.fingerprint
            persistDiagnostic(
                event: "surface_baseline_shadow",
                frame: frame,
                difference: nil,
                boundary: nil,
                linkage: nil,
                firstChangedAt: nil,
                lastChangedAt: nil
            )
            return
        }

        let difference = visualDifference(
            from: previousFingerprint,
            to: frame.fingerprint,
            pixelThreshold: configuration.visualDifferencePixelThreshold,
            fractionThreshold: configuration.visualDifferenceFractionThreshold
        )
        self.previousFingerprint = frame.fingerprint
        guard let difference, difference.isMaterial else { return }

        if var pendingChange {
            pendingChange.latestFrame = frame
            pendingChange.lastChangedAt = frame.capturedAt
            pendingChange.maximumChangedFraction = max(
                pendingChange.maximumChangedFraction,
                difference.changedFraction
            )
            pendingChange.materialFrameCount += 1
            if let attemptID = activeWrite?.attemptID {
                pendingChange.overlappedWriteAttemptIDs.insert(attemptID)
            }
            self.pendingChange = pendingChange
        } else {
            pendingChange = PendingShadowVisualChange(
                firstChangedAt: frame.capturedAt,
                lastChangedAt: frame.capturedAt,
                latestFrame: frame,
                maximumChangedFraction: difference.changedFraction,
                materialFrameCount: 1,
                overlappedWriteAttemptIDs: Set(
                    [activeWrite?.attemptID].compactMap { $0 }
                )
            )
        }
        scheduleSettlement()
    }

    private func scheduleSettlement() {
        settlementTimer?.invalidate()
        let timer = Timer(
            timeInterval: configuration.readDelay,
            repeats: false
        ) { [weak self] _ in
            self?.settleChange()
        }
        settlementTimer = timer
        RunLoop.main.add(timer, forMode: .common)
    }

    private func settleChange() {
        settlementTimer = nil
        guard let pendingChange else { return }
        self.pendingChange = nil
        let frame = latestFrame ?? pendingChange.latestFrame
        let difference = baselineFingerprint.flatMap {
            visualDifference(
                from: $0,
                to: frame.fingerprint,
                pixelThreshold: configuration.visualDifferencePixelThreshold,
                fractionThreshold: configuration.visualDifferenceFractionThreshold
            )
        }
        persistDiagnostic(
            event: "visual_change_settled_shadow",
            frame: frame,
            difference: difference,
            boundary: activeWrite,
            linkage: nil,
            firstChangedAt: pendingChange.firstChangedAt,
            lastChangedAt: pendingChange.lastChangedAt,
            materialFrameCount: pendingChange.materialFrameCount,
            maximumChangedFraction: pendingChange.maximumChangedFraction,
            overlappedWriteAttemptIDs: pendingChange.overlappedWriteAttemptIDs.sorted()
        )
        baselineFingerprint = frame.fingerprint
    }

    private func persistDiagnostic(
        event: String,
        frame: ShadowVisualFrame?,
        difference: VisualDifference?,
        boundary: ReadMutationBoundary?,
        linkage: VisualWriteSurfaceLinkage?,
        firstChangedAt: String?,
        lastChangedAt: String?,
        materialFrameCount: Int = 0,
        maximumChangedFraction: Double? = nil,
        overlappedWriteAttemptIDs: [String] = []
    ) {
        do {
            _ = try rawWriter.write(RawVisualMonitorDiagnostic(
                recordID: UUID().uuidString,
                observedAt: nowTimestamp(),
                event: event,
                frameSequence: frame?.sequence,
                captureRequestedAt: frame?.requestedAt,
                capturedAt: frame?.capturedAt,
                surface: frame?.surface.record ?? selectedSurface?.record,
                x: frame.map { Double($0.point.x) },
                y: frame.map { Double($0.point.y) },
                firstChangedAt: firstChangedAt,
                lastChangedAt: lastChangedAt,
                materialFrameCount: materialFrameCount,
                maximumChangedFraction: maximumChangedFraction,
                difference: difference.map(VisualDifferenceRecord.init),
                activeWriteAttemptID: boundary?.attemptID,
                activeWriteBeganAt: boundary?.observedAt,
                writeSurfaceLinkage: linkage.map(VisualWriteSurfaceLinkageRecord.init),
                overlappedWriteAttemptIDs: overlappedWriteAttemptIDs,
                skippedCaptureCount: skippedCaptureCount
            ))
        } catch {
            writeDiagnostic("could not persist visual monitor diagnostic: \(error)")
        }
    }
}

private struct ShadowVisualFrame {
    let sequence: UInt64
    let requestedAt: String
    let capturedAt: String
    let surface: ResolvedReadSurface
    let point: CGPoint
    let fingerprint: VisualFrameFingerprint
}

private struct PendingShadowVisualChange {
    let firstChangedAt: String
    var lastChangedAt: String
    var latestFrame: ShadowVisualFrame
    var maximumChangedFraction: Double
    var materialFrameCount: Int
    var overlappedWriteAttemptIDs: Set<String>
}

private struct VisualDifferenceRecord: Encodable {
    let comparedSampleCount: Int
    let changedSampleCount: Int
    let changedFraction: Double
    let meanAbsoluteDifference: Double
    let pixelThreshold: UInt8
    let fractionThreshold: Double
    let isMaterial: Bool

    init(_ difference: VisualDifference) {
        comparedSampleCount = difference.comparedSampleCount
        changedSampleCount = difference.changedSampleCount
        changedFraction = difference.changedFraction
        meanAbsoluteDifference = difference.meanAbsoluteDifference
        pixelThreshold = difference.pixelThreshold
        fractionThreshold = difference.fractionThreshold
        isMaterial = difference.isMaterial
    }
}

private struct VisualWriteSurfaceLinkageRecord: Encodable {
    let authoritativeWindowID: UInt32?
    let monitoredWindowID: UInt32?
    let independentlyResolvedWindowID: UInt32?
    let disposition: String
    let usesMonitoredSurface: Bool

    init(_ linkage: VisualWriteSurfaceLinkage) {
        authoritativeWindowID = linkage.authoritativeWindowID
        monitoredWindowID = linkage.monitoredWindowID
        independentlyResolvedWindowID = linkage.independentlyResolvedWindowID
        disposition = linkage.disposition
        usesMonitoredSurface = linkage.usesMonitoredSurface
    }
}

private struct RawVisualMonitorDiagnostic: Encodable {
    let schemaVersion = 2
    let recordType = "visual_monitor_diagnostic"
    let recordID: String
    let observedAt: String
    let event: String
    let frameSequence: UInt64?
    let captureRequestedAt: String?
    let capturedAt: String?
    let surface: ReadSurfaceRecord?
    let x: Double?
    let y: Double?
    let firstChangedAt: String?
    let lastChangedAt: String?
    let materialFrameCount: Int
    let maximumChangedFraction: Double?
    let difference: VisualDifferenceRecord?
    let activeWriteAttemptID: String?
    let activeWriteBeganAt: String?
    let writeSurfaceLinkage: VisualWriteSurfaceLinkageRecord?
    let overlappedWriteAttemptIDs: [String]
    let skippedCaptureCount: Int
}

private func makeVisualFrameFingerprint(
    _ image: CGImage,
    width: Int,
    height: Int
) -> VisualFrameFingerprint? {
    var samples = [UInt8](repeating: 0, count: width * height)
    guard let context = CGContext(
        data: &samples,
        width: width,
        height: height,
        bitsPerComponent: 8,
        bytesPerRow: width,
        space: CGColorSpaceCreateDeviceGray(),
        bitmapInfo: CGImageAlphaInfo.none.rawValue
    ) else { return nil }
    context.interpolationQuality = .low
    context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
    return VisualFrameFingerprint(width: width, height: height, samples: samples)
}
