import AppKit
import CoreImage
import CoreMedia
import CoreVideo
import CoupledCore
import CryptoKit
import Foundation
import ImageIO
import ScreenCaptureKit

/// A bounded, raw-only shadow sensor for visual changes on the selected
/// eligible surface. Continuous frames remain bounded in memory. Only settled
/// frames and causally safe pre-WRITE checkpoints are promoted into raw frame
/// evidence; OCR is then derived from that exact retained frame.
final class ActiveSurfaceFrameMonitor: NSObject, SCStreamOutput, SCStreamDelegate {
    private let configuration: Configuration
    private let rawWriter: JSONLWriter
    private let streamOutputQueue = DispatchQueue(
        label: "com.handsdiff.coupled.visual-frame-stream",
        qos: .utility
    )
    private let frameEvidenceQueue = DispatchQueue(
        label: "com.handsdiff.coupled.visual-frame-persistence",
        qos: .userInitiated
    )
    private let ocrQueue = DispatchQueue(
        label: "com.handsdiff.coupled.visual-frame-ocr",
        qos: .userInitiated
    )
    private let imageContext = CIContext(options: [.cacheIntermediates: false])
    private let accessibilitySurfaceProbe = ReadAccessibilitySurfaceProbe()

    private var started = false
    private var settlementTimer: Timer?
    private var stream: SCStream?
    private var streamGeneration: UInt64?
    private var streamReady = false
    private var generation: UInt64 = 0
    private var frameSequence: UInt64 = 0
    private var selectedSurface: ResolvedReadSurface?
    private var rawInteractionSurface: ResolvedReadSurface?
    private var surfaceSelectionReason: String?
    private var interactionPoint: CGPoint?
    private var semanticContentPoint: CGPoint?
    private var semanticContentPointReason: String?
    private var previousFingerprint: VisualFrameFingerprint?
    private var baselineFingerprint: VisualFrameFingerprint?
    private var latestFrame: ShadowVisualFrame?
    private var pendingChange: PendingShadowVisualChange?
    private var activeWrite: ReadMutationBoundary?
    private var lastCompletedWrite: CompletedWriteCapture?
    private var discardedFrameCount = 0
    private var highestPromotedFrameSequence: UInt64 = 0

    init(configuration: Configuration, rawWriter: JSONLWriter) {
        self.configuration = configuration
        self.rawWriter = rawWriter
        super.init()
    }

    func start() {
        guard !started else { return }
        started = true
        if let selectedSurface {
            replaceStream(for: selectedSurface, generation: generation)
        }
    }

    func select(
        surface: ResolvedReadSurface,
        point: CGPoint,
        rawInteractionSurface: ResolvedReadSurface,
        selectionReason: String
    ) {
        let surfaceChanged = selectedSurface.map {
            !sameVisualCaptureSurface($0, surface)
        } ?? true
        if surfaceChanged {
            generation += 1
            settlementTimer?.invalidate()
            settlementTimer = nil
            selectedSurface = surface
            self.rawInteractionSurface = rawInteractionSurface
            surfaceSelectionReason = selectionReason
            interactionPoint = point
            if isSemanticContentPoint(
                point,
                rawInteractionSurface: rawInteractionSurface,
                selectedSurface: surface
            ) {
                semanticContentPoint = point
                semanticContentPointReason = "direct_content_interaction"
            } else {
                semanticContentPoint = CGPoint(
                    x: surface.windowBounds.midX,
                    y: surface.windowBounds.midY
                )
                semanticContentPointReason = "canonical_surface_center_initialization"
            }
            previousFingerprint = nil
            baselineFingerprint = nil
            latestFrame = nil
            pendingChange = nil
            activeWrite = nil
            lastCompletedWrite = nil
            if started {
                replaceStream(for: surface, generation: generation)
            }
            return
        }
        interactionPoint = point
        self.rawInteractionSurface = rawInteractionSurface
        surfaceSelectionReason = selectionReason
        if isSemanticContentPoint(
            point,
            rawInteractionSurface: rawInteractionSurface,
            selectedSurface: surface
        ) {
            semanticContentPoint = point
            semanticContentPointReason = "direct_content_interaction"
        }
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
        if difference?.isMaterial == true, let safeFrame {
            promoteFrameEvidence(
                safeFrame,
                evidenceReason: "pre_write_visual_checkpoint",
                settledAt: boundary.observedAt,
                firstChangedAt: pendingChange?.firstChangedAt ?? safeFrame.capturedAt,
                lastChangedAt: pendingChange?.lastChangedAt ?? safeFrame.capturedAt,
                materialFrameCount: pendingChange?.materialFrameCount ?? 1,
                maximumChangedFraction: pendingChange?.maximumChangedFraction
                    ?? difference?.changedFraction,
                difference: difference,
                overlappedWriteAttemptIDs: pendingChange?
                    .overlappedWriteAttemptIDs.sorted() ?? []
            )
        }

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
        // Split a visual change that began during this WRITE at the completion
        // boundary. Its final in-WRITE frame remains raw suppressed evidence;
        // later application output starts a fresh change interval and can
        // settle into a READ instead of inheriting the WRITE overlap forever.
        if let pendingChange {
            settlementTimer?.invalidate()
            settlementTimer = nil
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
            var overlaps = pendingChange.overlappedWriteAttemptIDs
            overlaps.insert(completion.attemptID)
            promoteFrameEvidence(
                frame,
                evidenceReason: "active_write_visual_change",
                settledAt: nowTimestamp(),
                firstChangedAt: pendingChange.firstChangedAt,
                lastChangedAt: pendingChange.lastChangedAt,
                materialFrameCount: pendingChange.materialFrameCount,
                maximumChangedFraction: pendingChange.maximumChangedFraction,
                difference: difference,
                overlappedWriteAttemptIDs: overlaps.sorted()
            )
            baselineFingerprint = frame.fingerprint
        }
        lastCompletedWrite = completion
        activeWrite = nil
    }

    private func replaceStream(
        for surface: ResolvedReadSurface,
        generation requestedGeneration: UInt64
    ) {
        guard #available(macOS 13.0, *) else { return }
        let previousStream = stream
        stream = nil
        streamGeneration = nil
        streamReady = false

        let prepare = { [weak self] in
            guard let self,
                  self.generation == requestedGeneration,
                  self.selectedSurface.map({ sameVisualCaptureSurface($0, surface) }) == true else { return }
            self.prepareStream(for: surface, generation: requestedGeneration)
        }
        if let previousStream {
            previousStream.stopCapture { _ in
                DispatchQueue.main.async(execute: prepare)
            }
        } else {
            prepare()
        }
    }

    @available(macOS 13.0, *)
    private func prepareStream(
        for surface: ResolvedReadSurface,
        generation requestedGeneration: UInt64
    ) {
        SCShareableContent.getExcludingDesktopWindows(
            false,
            onScreenWindowsOnly: true
        ) { [weak self] content, error in
            DispatchQueue.main.async {
                guard let self,
                      self.generation == requestedGeneration,
                      self.selectedSurface.map({ sameVisualCaptureSurface($0, surface) }) == true else { return }
                guard let content,
                      let display = content.displays.first(where: {
                          $0.displayID == surface.displayID
                      }),
                      let streamConfiguration = self.streamConfiguration(
                          for: surface,
                          display: display
                      ) else {
                    self.persistDiagnostic(
                        event: "visual_stream_preparation_failed_shadow",
                        frame: nil,
                        difference: nil,
                        boundary: self.activeWrite,
                        linkage: nil,
                        firstChangedAt: self.pendingChange?.firstChangedAt,
                        lastChangedAt: self.pendingChange?.lastChangedAt,
                        captureError: error?.localizedDescription
                            ?? "display or capture geometry unavailable"
                    )
                    return
                }

                let filter = SCContentFilter(
                    display: display,
                    excludingApplications: [],
                    exceptingWindows: []
                )
                let candidate = SCStream(
                    filter: filter,
                    configuration: streamConfiguration.configuration,
                    delegate: self
                )
                do {
                    try candidate.addStreamOutput(
                        self,
                        type: .screen,
                        sampleHandlerQueue: self.streamOutputQueue
                    )
                } catch {
                    self.persistDiagnostic(
                        event: "visual_stream_output_failed_shadow",
                        frame: nil,
                        difference: nil,
                        boundary: self.activeWrite,
                        linkage: nil,
                        firstChangedAt: self.pendingChange?.firstChangedAt,
                        lastChangedAt: self.pendingChange?.lastChangedAt,
                        captureError: error.localizedDescription
                    )
                    return
                }

                self.stream = candidate
                self.streamGeneration = requestedGeneration
                self.streamReady = true
                candidate.startCapture { [weak self, weak candidate] error in
                    DispatchQueue.main.async {
                        guard let self, let candidate,
                              self.stream === candidate,
                              self.streamGeneration == requestedGeneration else {
                            candidate?.stopCapture()
                            return
                        }
                        guard error == nil else {
                            self.stream = nil
                            self.streamGeneration = nil
                            self.persistDiagnostic(
                                event: "visual_stream_start_failed_shadow",
                                frame: nil,
                                difference: nil,
                                boundary: self.activeWrite,
                                linkage: nil,
                                firstChangedAt: self.pendingChange?.firstChangedAt,
                                lastChangedAt: self.pendingChange?.lastChangedAt,
                                captureError: error?.localizedDescription
                            )
                            return
                        }
                        self.persistDiagnostic(
                            event: "visual_stream_started_shadow",
                            frame: nil,
                            difference: nil,
                            boundary: self.activeWrite,
                            linkage: nil,
                            firstChangedAt: self.pendingChange?.firstChangedAt,
                            lastChangedAt: self.pendingChange?.lastChangedAt,
                            streamConfiguration: streamConfiguration.record
                        )
                    }
                }
            }
        }
    }

    @available(macOS 13.0, *)
    private func streamConfiguration(
        for surface: ResolvedReadSurface,
        display: SCDisplay
    ) -> ShadowStreamConfiguration? {
        let displayBounds = CGRect(
            x: surface.displayBounds.x,
            y: surface.displayBounds.y,
            width: surface.displayBounds.width,
            height: surface.displayBounds.height
        )
        let clippedBounds = surface.windowBounds.intersection(displayBounds)
        guard !clippedBounds.isNull,
              clippedBounds.width >= 2,
              clippedBounds.height >= 2 else { return nil }
        let sourceRect = clippedBounds.offsetBy(
            dx: -displayBounds.minX,
            dy: -displayBounds.minY
        )
        let horizontalScale = max(
            1,
            Double(CGDisplayPixelsWide(surface.displayID)) / displayBounds.width
        )
        let verticalScale = max(
            1,
            Double(CGDisplayPixelsHigh(surface.displayID)) / displayBounds.height
        )
        let outputWidth = max(2, Int((sourceRect.width * horizontalScale).rounded(.up)))
        let outputHeight = max(2, Int((sourceRect.height * verticalScale).rounded(.up)))

        let result = SCStreamConfiguration()
        result.sourceRect = sourceRect
        result.width = outputWidth
        result.height = outputHeight
        result.scalesToFit = true
        result.minimumFrameInterval = CMTime(
            seconds: configuration.visualFrameInterval,
            preferredTimescale: 600
        )
        result.queueDepth = configuration.visualStreamQueueDepth
        result.pixelFormat = kCVPixelFormatType_32BGRA
        result.showsCursor = false
        result.capturesAudio = false
        return ShadowStreamConfiguration(
            configuration: result,
            record: VisualStreamConfigurationRecord(
                displayID: surface.displayID,
                sourceRect: rectValue(sourceRect),
                outputPixelWidth: outputWidth,
                outputPixelHeight: outputHeight,
                frameIntervalSeconds: configuration.visualFrameInterval,
                queueDepth: configuration.visualStreamQueueDepth,
                pixelFormat: "32BGRA",
                showsCursor: false
            )
        )
    }

    func stream(
        _ stream: SCStream,
        didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
        of outputType: SCStreamOutputType
    ) {
        guard outputType == .screen,
              sampleBuffer.isValid,
              let attachmentsArray = CMSampleBufferGetSampleAttachmentsArray(
                  sampleBuffer,
                  createIfNecessary: false
              ) as? [[SCStreamFrameInfo: Any]],
              let attachments = attachmentsArray.first,
              let statusRawValue = attachments[.status] as? Int,
              SCFrameStatus(rawValue: statusRawValue) == .complete,
              let pixelBuffer = sampleBuffer.imageBuffer,
              let fingerprint = makeVisualFrameFingerprint(
                  pixelBuffer,
                  width: configuration.visualFingerprintWidth,
                  height: configuration.visualFingerprintHeight
              ) else {
            DispatchQueue.main.async { [weak self] in
                self?.discardedFrameCount += 1
            }
            return
        }

        let receivedAt = nowTimestamp()
        let displayTimeNanoseconds = attachments[.displayTime] as? UInt64
        let pixelWidth = CVPixelBufferGetWidth(pixelBuffer)
        let pixelHeight = CVPixelBufferGetHeight(pixelBuffer)
        DispatchQueue.main.async { [weak self, weak stream] in
            guard let self, let stream,
                  self.stream === stream,
                  self.streamReady,
                  self.streamGeneration == self.generation,
                  !self.configuration.isPaused(),
                  let surface = self.selectedSurface,
                  let point = self.interactionPoint,
                  NSWorkspace.shared.frontmostApplication?.processIdentifier
                    == surface.processIdentifier else {
                self?.discardedFrameCount += 1
                return
            }

            self.frameSequence += 1
            self.accept(ShadowVisualFrame(
                sequence: self.frameSequence,
                requestedAt: nil,
                capturedAt: receivedAt,
                displayTimeNanoseconds: displayTimeNanoseconds,
                pixelWidth: pixelWidth,
                pixelHeight: pixelHeight,
                surface: surface,
                point: point,
                fingerprint: fingerprint,
                pixelBuffer: pixelBuffer
            ))
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        DispatchQueue.main.async { [weak self, weak stream] in
            guard let self, let stream, self.stream === stream else { return }
            self.stream = nil
            self.streamGeneration = nil
            self.streamReady = false
            self.persistDiagnostic(
                event: "visual_stream_stopped_with_error_shadow",
                frame: nil,
                difference: nil,
                boundary: self.activeWrite,
                linkage: nil,
                firstChangedAt: self.pendingChange?.firstChangedAt,
                lastChangedAt: self.pendingChange?.lastChangedAt,
                captureError: error.localizedDescription
            )
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
        let overlappingWriteAttemptID = writeAttemptOverlapping(frame)

        if var pendingChange {
            pendingChange.latestFrame = frame
            pendingChange.lastChangedAt = frame.capturedAt
            pendingChange.maximumChangedFraction = max(
                pendingChange.maximumChangedFraction,
                difference.changedFraction
            )
            pendingChange.materialFrameCount += 1
            if let attemptID = overlappingWriteAttemptID {
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
                    [overlappingWriteAttemptID].compactMap { $0 }
                )
            )
        }
        scheduleSettlement()
    }

    private func writeAttemptOverlapping(_ frame: ShadowVisualFrame) -> String? {
        if let activeWrite { return activeWrite.attemptID }
        guard let completion = lastCompletedWrite,
              completion.processIdentifier == frame.surface.processIdentifier,
              frame.capturedAt <= completion.observedAt else { return nil }
        return completion.attemptID
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
        promoteFrameEvidence(
            frame,
            evidenceReason: "visual_change_settled",
            settledAt: nowTimestamp(),
            firstChangedAt: pendingChange.firstChangedAt,
            lastChangedAt: pendingChange.lastChangedAt,
            materialFrameCount: pendingChange.materialFrameCount,
            maximumChangedFraction: pendingChange.maximumChangedFraction,
            difference: difference,
            overlappedWriteAttemptIDs: pendingChange.overlappedWriteAttemptIDs.sorted()
        )
        baselineFingerprint = frame.fingerprint
    }

    /// The frame record is appended before its OCR record. The retained
    /// CVPixelBuffer is the exact completed SCStream frame used for both the
    /// screenshot digest and Vision request; a later screen recapture is never
    /// substituted.
    private func promoteFrameEvidence(
        _ frame: ShadowVisualFrame,
        evidenceReason: String,
        settledAt: String,
        firstChangedAt: String,
        lastChangedAt: String,
        materialFrameCount: Int,
        maximumChangedFraction: Double?,
        difference: VisualDifference?,
        overlappedWriteAttemptIDs: [String]
    ) {
        guard frame.sequence > highestPromotedFrameSequence else { return }
        highestPromotedFrameSequence = frame.sequence

        let frameRecordID = UUID().uuidString
        let suppressionReason: String? = !overlappedWriteAttemptIDs.isEmpty
            ? "visual_change_overlapped_active_write"
            : configuration.retainScreenshots ? nil : "screenshot_retention_disabled"
        let configuration = configuration
        let rawWriter = rawWriter
        let imageContext = imageContext
        let rawInteractionSurface = rawInteractionSurface?.record
        let surfaceSelectionReason = surfaceSelectionReason
        let metadata = VisualFrameEvidenceMetadata(frame)
        let semanticPoint = semanticContentPoint ?? CGPoint(
            x: frame.surface.windowBounds.midX,
            y: frame.surface.windowBounds.midY
        )
        let semanticPointReason = semanticContentPointReason
            ?? "canonical_surface_center_fallback"
        let accessibilitySurface = accessibilitySurfaceProbe.capture(
            at: semanticPoint,
            expectedProcessIdentifier: frame.surface.processIdentifier
        )
        frameEvidenceQueue.async { [weak self] in
            autoreleasepool {
                let image = imageContext.createCGImage(
                    CIImage(cvPixelBuffer: frame.pixelBuffer),
                    from: CGRect(
                        x: 0, y: 0,
                        width: frame.pixelWidth,
                        height: frame.pixelHeight
                    )
                )
                guard let image else {
                    DispatchQueue.main.async {
                        self?.persistDiagnostic(
                            event: "visual_frame_image_conversion_failed",
                            frame: frame,
                            difference: difference,
                            boundary: self?.activeWrite,
                            linkage: nil,
                            firstChangedAt: firstChangedAt,
                            lastChangedAt: lastChangedAt,
                            captureError: "could not convert retained CVPixelBuffer"
                        )
                    }
                    return
                }
                do {
                    let screenshot = try persistVisualFramePNG(
                        image,
                        configuration: configuration,
                        recordID: frameRecordID
                    )
                    DispatchQueue.main.async { [weak self] in
                        guard let self else { return }
                        do {
                            _ = try rawWriter.write(RawVisualFrameObservation(
                                recordID: frameRecordID,
                                observedAt: nowTimestamp(),
                                evidenceReason: evidenceReason,
                                settledAt: settledAt,
                                frameSequence: metadata.sequence,
                                capturedAt: metadata.capturedAt,
                                displayTimeNanoseconds: metadata.displayTimeNanoseconds,
                                framePixelWidth: metadata.pixelWidth,
                                framePixelHeight: metadata.pixelHeight,
                                surface: metadata.surface.record,
                                rawInteractionSurface: rawInteractionSurface,
                                surfaceSelectionReason: surfaceSelectionReason,
                                rawInteractionX: Double(metadata.point.x),
                                rawInteractionY: Double(metadata.point.y),
                                x: Double(semanticPoint.x),
                                y: Double(semanticPoint.y),
                                semanticContentPointReason: semanticPointReason,
                                firstChangedAt: firstChangedAt,
                                lastChangedAt: lastChangedAt,
                                materialFrameCount: materialFrameCount,
                                maximumChangedFraction: maximumChangedFraction,
                                difference: difference.map(VisualDifferenceRecord.init),
                                overlappedWriteAttemptIDs: overlappedWriteAttemptIDs,
                                derivedSuppressionReason: suppressionReason,
                                screenshotRelativePath: screenshot?.relativePath,
                                screenshotSHA256: screenshot?.sha256,
                                screenshotPixelWidth: screenshot?.pixelWidth,
                                screenshotPixelHeight: screenshot?.pixelHeight,
                                accessibilitySurface: accessibilitySurface
                            ))
                        } catch {
                            writeDiagnostic("could not persist raw visual frame: \(error)")
                            return
                        }
                        guard suppressionReason == nil, let screenshot else { return }
                        self.recognizePromotedFrame(
                            frame: metadata,
                            frameRecordID: frameRecordID,
                            evidenceReason: evidenceReason,
                            settledAt: settledAt,
                            firstChangedAt: firstChangedAt,
                            lastChangedAt: lastChangedAt,
                            materialFrameCount: materialFrameCount,
                            screenshot: screenshot,
                            triggerSurface: rawInteractionSurface ?? metadata.surface.record,
                            semanticPoint: semanticPoint,
                            semanticContentPointReason: semanticPointReason,
                            accessibilitySurface: accessibilitySurface
                        )
                    }
                } catch {
                    DispatchQueue.main.async {
                        writeDiagnostic("could not persist raw visual frame PNG: \(error)")
                    }
                }
            }
        }
    }

    private func recognizePromotedFrame(
        frame: VisualFrameEvidenceMetadata,
        frameRecordID: String,
        evidenceReason: String,
        settledAt: String,
        firstChangedAt: String,
        lastChangedAt: String,
        materialFrameCount: Int,
        screenshot: RetainedVisualFrame,
        triggerSurface: ReadSurfaceRecord,
        semanticPoint: CGPoint,
        semanticContentPointReason: String,
        accessibilitySurface: RawReadAccessibilitySurfaceProbe
    ) {
        let configuration = configuration
        let screenshotURL = URL(fileURLWithPath: configuration.outputDirectory)
            .appendingPathComponent(screenshot.relativePath)
        ocrQueue.async { [weak self] in
            do {
                guard let source = CGImageSourceCreateWithURL(
                    screenshotURL as CFURL, nil
                ), let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
                    throw VisualFrameEvidenceError.couldNotReadPNG(screenshotURL.path)
                }
                let recognized = try recognizeText(
                    in: image,
                    regionOfInterest: CGRect(x: 0, y: 0, width: 1, height: 1),
                    maxCharacters: configuration.maxCharacters
                )
                DispatchQueue.main.async {
                    guard let self else { return }
                    do {
                        _ = try self.rawWriter.write(RawVisualOCRObservation(
                            recordID: UUID().uuidString,
                            observedAt: nowTimestamp(),
                            sourceFrameRecordID: frameRecordID,
                            sourceFrameSequence: frame.sequence,
                            sourceFrameCapturedAt: frame.capturedAt,
                            sourceFrameScreenshotSHA256: screenshot.sha256,
                            evidenceReason: evidenceReason,
                            settledAt: settledAt,
                            capturedAt: frame.capturedAt,
                            surfaceResolvedAt: frame.surface.resolvedAt,
                            firstActivityAt: firstChangedAt,
                            lastActivityAt: lastChangedAt,
                            readDelaySeconds: configuration.readDelay,
                            triggerTypes: [evidenceReason],
                            eventCount: materialFrameCount,
                            content: recognized.content,
                            recognizedLineCount: recognized.lineCount,
                            contentWasTruncated: recognized.wasTruncated,
                            screenshotRelativePath: screenshot.relativePath,
                            screenshotSHA256: screenshot.sha256,
                            screenshotPixelWidth: screenshot.pixelWidth,
                            screenshotPixelHeight: screenshot.pixelHeight,
                            captureScope: "canonical_full_window",
                            windowBounds: rectValue(frame.surface.windowBounds),
                            captureBounds: rectValue(frame.surface.windowBounds),
                            x: Double(semanticPoint.x),
                            y: Double(semanticPoint.y),
                            semanticContentPointReason: semanticContentPointReason,
                            displayID: frame.surface.displayID,
                            displayBounds: frame.surface.displayBounds,
                            windowID: frame.surface.windowID,
                            windowTitle: frame.surface.windowTitle,
                            appName: frame.surface.appName,
                            bundleIdentifier: frame.surface.bundleIdentifier,
                            processIdentifier: frame.surface.processIdentifier,
                            triggerSurface: triggerSurface,
                            accessibilitySurface: accessibilitySurface
                        ))
                    } catch {
                        writeDiagnostic("could not persist visual OCR observation: \(error)")
                    }
                }
            } catch {
                DispatchQueue.main.async {
                    writeDiagnostic("visual frame text recognition failed: \(error)")
                }
            }
        }
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
        overlappedWriteAttemptIDs: [String] = [],
        captureError: String? = nil,
        streamConfiguration: VisualStreamConfigurationRecord? = nil
    ) {
        do {
            _ = try rawWriter.write(RawVisualMonitorDiagnostic(
                recordID: UUID().uuidString,
                observedAt: nowTimestamp(),
                event: event,
                frameSequence: frame?.sequence,
                captureRequestedAt: frame?.requestedAt,
                capturedAt: frame?.capturedAt,
                displayTimeNanoseconds: frame?.displayTimeNanoseconds,
                framePixelWidth: frame?.pixelWidth,
                framePixelHeight: frame?.pixelHeight,
                surface: frame?.surface.record ?? selectedSurface?.record,
                rawInteractionSurface: rawInteractionSurface?.record,
                surfaceSelectionReason: surfaceSelectionReason,
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
                streamConfiguration: streamConfiguration,
                overlappedWriteAttemptIDs: overlappedWriteAttemptIDs,
                discardedFrameCount: discardedFrameCount,
                captureError: captureError
            ))
        } catch {
            writeDiagnostic("could not persist visual monitor diagnostic: \(error)")
        }
    }
}

private struct ShadowVisualFrame {
    let sequence: UInt64
    let requestedAt: String?
    let capturedAt: String
    let displayTimeNanoseconds: UInt64?
    let pixelWidth: Int
    let pixelHeight: Int
    let surface: ResolvedReadSurface
    let point: CGPoint
    let fingerprint: VisualFrameFingerprint
    let pixelBuffer: CVPixelBuffer
}

private struct VisualFrameEvidenceMetadata {
    let sequence: UInt64
    let capturedAt: String
    let displayTimeNanoseconds: UInt64?
    let pixelWidth: Int
    let pixelHeight: Int
    let surface: ResolvedReadSurface
    let point: CGPoint

    init(_ frame: ShadowVisualFrame) {
        sequence = frame.sequence
        capturedAt = frame.capturedAt
        displayTimeNanoseconds = frame.displayTimeNanoseconds
        pixelWidth = frame.pixelWidth
        pixelHeight = frame.pixelHeight
        surface = frame.surface
        point = frame.point
    }
}

@available(macOS 13.0, *)
private struct ShadowStreamConfiguration {
    let configuration: SCStreamConfiguration
    let record: VisualStreamConfigurationRecord
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

private struct VisualStreamConfigurationRecord: Encodable {
    let displayID: UInt32
    let sourceRect: RectValue
    let outputPixelWidth: Int
    let outputPixelHeight: Int
    let frameIntervalSeconds: Double
    let queueDepth: Int
    let pixelFormat: String
    let showsCursor: Bool
}

private struct RawVisualMonitorDiagnostic: Encodable {
    let schemaVersion = 3
    let recordType = "visual_monitor_diagnostic"
    let captureTransport = "scstream_display_source_rect"
    let recordID: String
    let observedAt: String
    let event: String
    let frameSequence: UInt64?
    let captureRequestedAt: String?
    let capturedAt: String?
    let displayTimeNanoseconds: UInt64?
    let framePixelWidth: Int?
    let framePixelHeight: Int?
    let surface: ReadSurfaceRecord?
    let rawInteractionSurface: ReadSurfaceRecord?
    let surfaceSelectionReason: String?
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
    let streamConfiguration: VisualStreamConfigurationRecord?
    let overlappedWriteAttemptIDs: [String]
    let discardedFrameCount: Int
    let captureError: String?
}

private struct RawVisualFrameObservation: Encodable {
    let schemaVersion = 1
    let recordType = "visual_frame_observation"
    let captureTransport = "scstream_display_source_rect"
    let recordID: String
    let observedAt: String
    let evidenceReason: String
    let settledAt: String
    let frameSequence: UInt64
    let capturedAt: String
    let displayTimeNanoseconds: UInt64?
    let framePixelWidth: Int
    let framePixelHeight: Int
    let surface: ReadSurfaceRecord
    let rawInteractionSurface: ReadSurfaceRecord?
    let surfaceSelectionReason: String?
    let rawInteractionX: Double
    let rawInteractionY: Double
    let x: Double
    let y: Double
    let semanticContentPointReason: String
    let firstChangedAt: String
    let lastChangedAt: String
    let materialFrameCount: Int
    let maximumChangedFraction: Double?
    let difference: VisualDifferenceRecord?
    let overlappedWriteAttemptIDs: [String]
    let derivedSuppressionReason: String?
    let screenshotRelativePath: String?
    let screenshotSHA256: String?
    let screenshotPixelWidth: Int?
    let screenshotPixelHeight: Int?
    let accessibilitySurface: RawReadAccessibilitySurfaceProbe
}

private struct RawVisualOCRObservation: Encodable {
    let schemaVersion = 1
    let recordType = "visual_ocr_observation"
    let ocrEngine = "apple_vision"
    let recordID: String
    let observedAt: String
    let sourceFrameRecordID: String
    let sourceFrameSequence: UInt64
    let sourceFrameCapturedAt: String
    let sourceFrameScreenshotSHA256: String
    let evidenceReason: String
    let settledAt: String
    let capturedAt: String
    let surfaceResolvedAt: String
    let firstActivityAt: String
    let lastActivityAt: String
    let readDelaySeconds: Double
    let triggerTypes: [String]
    let eventCount: Int
    let content: String
    let recognizedLineCount: Int
    let contentWasTruncated: Bool
    let screenshotRelativePath: String
    let screenshotSHA256: String
    let screenshotPixelWidth: Int
    let screenshotPixelHeight: Int
    let captureScope: String
    let windowBounds: RectValue
    let captureBounds: RectValue
    let x: Double
    let y: Double
    let semanticContentPointReason: String
    let displayID: UInt32
    let displayBounds: RectValue
    let windowID: UInt32
    let windowTitle: String?
    let appName: String
    let bundleIdentifier: String?
    let processIdentifier: Int32
    let triggerSurface: ReadSurfaceRecord
    let accessibilitySurface: RawReadAccessibilitySurfaceProbe
}

private struct RetainedVisualFrame {
    let relativePath: String
    let sha256: String
    let pixelWidth: Int
    let pixelHeight: Int
}

private enum VisualFrameEvidenceError: Error {
    case couldNotReadPNG(String)
}

private func persistVisualFramePNG(
    _ image: CGImage,
    configuration: Configuration,
    recordID: String
) throws -> RetainedVisualFrame? {
    guard configuration.retainScreenshots else { return nil }
    let directory = URL(fileURLWithPath: configuration.screenshotsDirectory)
    try FileManager.default.createDirectory(
        at: directory,
        withIntermediateDirectories: true,
        attributes: [.posixPermissions: NSNumber(value: 0o700)]
    )
    let relativePath = "screenshots/visual-\(recordID).png"
    let url = URL(fileURLWithPath: configuration.outputDirectory)
        .appendingPathComponent(relativePath)
    let representation = NSBitmapImageRep(cgImage: image)
    guard let data = representation.representation(using: .png, properties: [:]),
          FileManager.default.createFile(
            atPath: url.path,
            contents: data,
            attributes: [.posixPermissions: NSNumber(value: 0o600)]
          ) else {
        throw ReadCandidateCollectorError.screenshotPersistenceFailed(url.path)
    }
    return RetainedVisualFrame(
        relativePath: relativePath,
        sha256: SHA256.hash(data: data)
            .map { String(format: "%02x", $0) }
            .joined(),
        pixelWidth: image.width,
        pixelHeight: image.height
    )
}

/// A toolbar/helper interaction may select the correct canonical window while
/// being a poor semantic pane anchor. Preserve the most recent point in the
/// selected top-level window's content area; initialize from its center when
/// no such point is available yet.
private func isSemanticContentPoint(
    _ point: CGPoint,
    rawInteractionSurface: ResolvedReadSurface,
    selectedSurface: ResolvedReadSurface
) -> Bool {
    guard rawInteractionSurface.processIdentifier
            == selectedSurface.processIdentifier,
          rawInteractionSurface.windowID == selectedSurface.windowID,
          selectedSurface.windowBounds.contains(point) else { return false }
    let topInset = min(
        120,
        max(44, selectedSurface.windowBounds.height * 0.08)
    )
    return point.y >= selectedSurface.windowBounds.minY + topInset
}

private func sameVisualCaptureSurface(
    _ left: ResolvedReadSurface,
    _ right: ResolvedReadSurface
) -> Bool {
    guard left.displayID == right.displayID,
          left.processIdentifier == right.processIdentifier,
          left.windowID == right.windowID else { return false }
    let tolerance = 24.0
    return abs(left.windowBounds.minX - right.windowBounds.minX) <= tolerance
        && abs(left.windowBounds.minY - right.windowBounds.minY) <= tolerance
        && abs(left.windowBounds.width - right.windowBounds.width) <= tolerance
        && abs(left.windowBounds.height - right.windowBounds.height) <= tolerance
}

private func makeVisualFrameFingerprint(
    _ pixelBuffer: CVPixelBuffer,
    width: Int,
    height: Int
) -> VisualFrameFingerprint? {
    guard CVPixelBufferGetPixelFormatType(pixelBuffer) == kCVPixelFormatType_32BGRA,
          CVPixelBufferLockBaseAddress(pixelBuffer, .readOnly) == kCVReturnSuccess,
          let baseAddress = CVPixelBufferGetBaseAddress(pixelBuffer) else { return nil }
    defer { CVPixelBufferUnlockBaseAddress(pixelBuffer, .readOnly) }

    let sourceWidth = CVPixelBufferGetWidth(pixelBuffer)
    let sourceHeight = CVPixelBufferGetHeight(pixelBuffer)
    let bytesPerRow = CVPixelBufferGetBytesPerRow(pixelBuffer)
    guard sourceWidth > 0, sourceHeight > 0, bytesPerRow >= sourceWidth * 4 else {
        return nil
    }
    let bytes = baseAddress.assumingMemoryBound(to: UInt8.self)
    var samples = [UInt8](repeating: 0, count: width * height)
    for destinationY in 0..<height {
        let sourceY = min(
            sourceHeight - 1,
            Int((Double(destinationY) + 0.5) * Double(sourceHeight) / Double(height))
        )
        let row = bytes.advanced(by: sourceY * bytesPerRow)
        for destinationX in 0..<width {
            let sourceX = min(
                sourceWidth - 1,
                Int((Double(destinationX) + 0.5) * Double(sourceWidth) / Double(width))
            )
            let pixel = row.advanced(by: sourceX * 4)
            let blue = Int(pixel[0])
            let green = Int(pixel[1])
            let red = Int(pixel[2])
            samples[destinationY * width + destinationX] = UInt8(
                (29 * blue + 150 * green + 77 * red) >> 8
            )
        }
    }
    return VisualFrameFingerprint(width: width, height: height, samples: samples)
}
