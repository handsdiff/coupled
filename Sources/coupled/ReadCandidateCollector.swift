import AppKit
import ApplicationServices
import CoupledCore
import CryptoKit
import Foundation
import ScreenCaptureKit
import Vision

/// Pointer activity becomes a read candidate after an idle delay. Screen-text
/// mode additionally requires one capture surface to remain stable for the
/// complete interval before its pixels can become a derived read.
final class ReadCandidateCollector {
    private let configuration: Configuration
    private let captureScreenText: Bool
    private let writer: JSONLWriter
    private let rawWriter: JSONLWriter?
    private var eventTap: CFMachPort?
    private var eventTapSource: CFRunLoopSource?
    private var activationObserver: NSObjectProtocol?
    private var sequence: UInt64 = 0
    private var pendingByContext: [ReadCandidateKey: PendingReadCandidate] = [:]
    private var timersByContext: [ReadCandidateKey: Timer] = [:]
    private var cachedPointerWindow: PointerWindowContext?
    private var lastWindowLookupTimestamp: UInt64 = 0
    private var latestMutatingInputBySurface: [ReadMutationSurfaceKey: ReadMutationBoundary] = [:]
    private var reportedCaptureError = false

    init(
        configuration: Configuration,
        captureScreenText: Bool = false,
        writer: JSONLWriter? = nil,
        rawWriter: JSONLWriter? = nil
    ) throws {
        self.configuration = configuration
        self.captureScreenText = captureScreenText
        self.rawWriter = rawWriter
        if let writer {
            self.writer = writer
        } else {
            self.writer = try JSONLWriter(
                path: configuration.readsPath,
                sessionID: configuration.sessionID
            )
        }
    }

    func run() throws {
        try start()
        RunLoop.current.run()
    }

    func start() throws {
        guard installEventTap() else { throw ReadCandidateCollectorError.eventTapUnavailable }
        if captureScreenText {
            installApplicationActivationObserver()
        }

        if captureScreenText {
            writeDiagnostic("screen-text reads: \(writer.path)")
            writeDiagnostic("visible pixels are captured and recognized locally after \(configuration.readDelay) seconds without pointer activity")
            writeDiagnostic("capture-time app/window identity and pixels must share one stable settling interval; trigger-time identity is provenance only")
            writeDiagnostic("same-window mutating input supersedes an unsettled read candidate")
            writeDiagnostic("viewport crop removes \(Int((configuration.viewportSideCropFraction * 100).rounded()))% from each side, \(Int((configuration.viewportTopCropFraction * 100).rounded()))% from the top, and \(Int((configuration.viewportBottomCropFraction * 100).rounded()))% from the bottom")
            writeDiagnostic("normalized line overlap is removed between adjacent OCR viewports in the same app/window/display")
            writeDiagnostic("Chrome auxiliary surfaces are retained raw and suppressed from derived reads")
            if configuration.retainScreenshots {
                writeDiagnostic("full-window PNG evidence: \(configuration.screenshotsDirectory)")
            }
        } else {
            writeDiagnostic("read candidates: \(writer.path)")
            writeDiagnostic("one candidate is emitted after \(configuration.readDelay) seconds without pointer activity in that app/display")
            writeDiagnostic("no Accessibility data or screen text is captured")
        }
        writeDiagnostic("allowed bundles: \(configuration.allowedBundles.sorted().joined(separator: ", "))")
        if let pauseFile = configuration.pauseFile {
            writeDiagnostic("collection pauses while this file exists: \(pauseFile)")
        }
    }

    deinit {
        if let activationObserver {
            NSWorkspace.shared.notificationCenter.removeObserver(activationObserver)
        }
    }

    func supersedePendingReads(with input: MutatingWriteInput) {
        let focusedWindowID = topmostWindow(
            ownedBy: input.processIdentifier
        )?.windowID
        let boundary = ReadMutationBoundary(
            attemptID: input.attemptID,
            observedAt: input.observedAt,
            eventTimestampNanoseconds: input.eventTimestampNanoseconds,
            processIdentifier: input.processIdentifier,
            windowID: focusedWindowID
        )
        latestMutatingInputBySurface[ReadMutationSurfaceKey(
            processIdentifier: input.processIdentifier,
            windowID: focusedWindowID
        )] = boundary

        let supersededKeys = pendingByContext.keys.filter {
            sameReadSurface(
                processIdentifier: $0.processIdentifier,
                windowID: $0.windowID,
                as: boundary
            )
        }
        for key in supersededKeys {
            timersByContext[key]?.invalidate()
            timersByContext[key] = nil
            guard let pending = pendingByContext.removeValue(forKey: key) else { continue }
            persistSupersededCandidate(pending, boundary: boundary)
        }
    }

    func observePostSubmission(_ trigger: PostSubmissionReadTrigger) {
        guard captureScreenText, !configuration.isPaused() else { return }
        // Let the application process Return before resolving the surface. The
        // normal READ delay begins only after this observation, and capture
        // still revalidates the visible surface at settlement and completion.
        Timer.scheduledTimer(withTimeInterval: 0.1, repeats: false) { [weak self] _ in
            guard let self,
                  !self.configuration.isPaused(),
                  let application = NSWorkspace.shared.frontmostApplication else { return }
            let pointerPoint = CGEvent(source: nil)?.location
            guard let window = primaryContentWindow(
                ownedBy: application.processIdentifier,
                near: pointerPoint
            ) else { return }
            let point = pointerPoint.flatMap { window.bounds.contains($0) ? $0 : nil }
                ?? CGPoint(x: window.bounds.midX, y: window.bounds.midY)
            guard let surface = self.eligibleSurface(window: window, at: point) else { return }
            self.beginSurfaceTransitionInterval(
                surface,
                point: point,
                trigger: "post_submission_\(trigger.evidence)",
                replacementReason: "read_candidate_replaced_by_post_submission_surface"
            )
        }
    }

    private func installEventTap() -> Bool {
        let types: [CGEventType] = [
            .mouseMoved,
            .leftMouseDown, .rightMouseDown, .otherMouseDown,
            .leftMouseDragged, .rightMouseDragged, .otherMouseDragged,
            .scrollWheel,
        ]
        let mask = types.reduce(CGEventMask(0)) {
            $0 | (CGEventMask(1) << CGEventMask($1.rawValue))
        }
        let callback: CGEventTapCallBack = { _, type, event, userInfo in
            guard let userInfo else { return Unmanaged.passUnretained(event) }
            let collector = Unmanaged<ReadCandidateCollector>
                .fromOpaque(userInfo)
                .takeUnretainedValue()

            if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
                if let tap = collector.eventTap {
                    CGEvent.tapEnable(tap: tap, enable: true)
                }
                return Unmanaged.passUnretained(event)
            }

            if let trigger = readTrigger(type) {
                collector.record(trigger: trigger, event: event)
            }
            return Unmanaged.passUnretained(event)
        }

        guard let tap = CGEvent.tapCreate(
            tap: .cgSessionEventTap,
            place: .headInsertEventTap,
            options: .listenOnly,
            eventsOfInterest: mask,
            callback: callback,
            userInfo: Unmanaged.passUnretained(self).toOpaque()
        ) else {
            return false
        }

        eventTap = tap
        let source = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
        eventTapSource = source
        CFRunLoopAddSource(CFRunLoopGetCurrent(), source, .commonModes)
        CGEvent.tapEnable(tap: tap, enable: true)
        return true
    }

    private func record(trigger: String, event: CGEvent) {
        guard !configuration.isPaused() else { return }

        let point = event.location
        let pointerWindow = resolvedWindow(
            at: point,
            eventTimestamp: event.timestamp,
            forceRefresh: trigger == "click" || trigger == "scroll"
        )
        guard let pointerWindow,
              let surface = eligibleSurface(window: pointerWindow, at: point) else { return }
        let key = surface.key
        let observedAt = nowTimestamp()

        if captureScreenText {
            collapsePendingCandidates(
                except: key,
                reason: "read_candidate_replaced_by_new_surface_activity",
                observedSurface: surface
            )
        }

        if var pending = pendingByContext[key], pending.matches(surface) {
            pending.lastActivityAt = observedAt
            pending.lastEventTimestampNanoseconds = event.timestamp
            pending.eventCount += 1
            pending.triggerTypes.insert(trigger)
            pending.lastX = point.x
            pending.lastY = point.y
            pendingByContext[key] = pending
        } else {
            if let replaced = pendingByContext.removeValue(forKey: key) {
                timersByContext[key]?.invalidate()
                timersByContext[key] = nil
                persistCandidateDisposition(
                    replaced,
                    reason: "read_candidate_surface_changed_during_activity",
                    observedSurface: surface
                )
            }
            pendingByContext[key] = PendingReadCandidate(
                surface: surface,
                observedAt: observedAt,
                eventTimestampNanoseconds: event.timestamp,
                trigger: trigger,
                eventCount: 1,
                point: point
            )
        }

        scheduleSettlement(for: key)
        if captureScreenText, trigger == "click" {
            schedulePostClickSurfaceObservation(
                point: point,
                sourceKey: key,
                sourceEventTimestampNanoseconds: event.timestamp
            )
        }
    }

    private func emitPendingCandidate(for key: ReadCandidateKey) {
        timersByContext[key]?.invalidate()
        timersByContext[key] = nil
        guard let pending = pendingByContext.removeValue(forKey: key) else { return }
        guard !configuration.isPaused() else { return }

        if captureScreenText {
            settleScreenRead(pending)
            return
        }

        sequence += 1
        let record = ReadCandidateRecord(
            sequence: sequence,
            observedAt: nowTimestamp(),
            firstActivityAt: pending.firstActivityAt,
            lastActivityAt: pending.lastActivityAt,
            firstEventTimestampNanoseconds: pending.firstEventTimestampNanoseconds,
            lastEventTimestampNanoseconds: pending.lastEventTimestampNanoseconds,
            readDelaySeconds: configuration.readDelay,
            triggerTypes: pending.triggerTypes.sorted(),
            eventCount: pending.eventCount,
            x: pending.lastX,
            y: pending.lastY,
            displayID: pending.displayID,
            displayBounds: pending.displayBounds,
            windowID: pending.windowID,
            windowTitle: pending.windowTitle,
            windowBounds: pending.windowBounds,
            appName: pending.appName,
            bundleIdentifier: pending.bundleIdentifier,
            processIdentifier: pending.processIdentifier
        )
        if let data = try? writer.write(record) {
            writeLineToStandardOutput(data)
        }
    }

    private func settleScreenRead(_ pending: PendingReadCandidate) {
        let point = CGPoint(x: pending.lastX, y: pending.lastY)
        guard let surface = eligibleSurface(window: topmostWindow(at: point), at: point) else {
            persistCandidateDisposition(
                pending,
                reason: "no_eligible_surface_at_settlement"
            )
            return
        }
        guard pending.matches(surface) else {
            persistCandidateDisposition(
                pending,
                reason: "surface_transition_detected_at_settlement",
                observedSurface: surface
            )
            beginSurfaceTransitionInterval(surface, point: point)
            return
        }
        captureAndEmitRead(pending, surface: surface)
    }

    private func captureAndEmitRead(
        _ pending: PendingReadCandidate,
        surface: ResolvedReadSurface
    ) {
        guard #available(macOS 15.2, *) else {
            reportCaptureErrorOnce("screen-text reads require macOS 15.2 or newer")
            return
        }

        let windowBounds = surface.windowBounds
        let captureBounds = croppedViewport(
            in: windowBounds,
            sideCropFraction: configuration.viewportSideCropFraction,
            topCropFraction: configuration.viewportTopCropFraction,
            bottomCropFraction: configuration.viewportBottomCropFraction
        )
        let rawObservationID = UUID().uuidString
        let surfaceResolvedAt = surface.resolvedAt
        let settledAt = nowTimestamp()
        SCScreenshotManager.captureImage(in: windowBounds) { [weak self] image, error in
            guard let self else { return }
            let capturedAt = nowTimestamp()
            guard let image else {
                DispatchQueue.main.async {
                    self.reportCaptureErrorOnce(
                        error?.localizedDescription ?? "screen capture returned no image"
                    )
                }
                return
            }

            DispatchQueue.main.async {
                let point = CGPoint(x: pending.lastX, y: pending.lastY)
                let postCaptureSurface = self.eligibleSurface(
                    window: topmostWindow(at: point),
                    at: point
                )
                let surfaceChangedDuringCapture = postCaptureSurface.map {
                    !surface.matches($0)
                } ?? true
                if surfaceChangedDuringCapture, let postCaptureSurface {
                    self.beginSurfaceTransitionInterval(
                        postCaptureSurface,
                        point: point,
                        replacementReason: "read_candidate_replaced_after_capture_race"
                    )
                }

                DispatchQueue.global(qos: .userInitiated).async {
                    do {
                        let retainedScreenshot = try self.persistScreenshot(
                            image,
                            recordID: rawObservationID
                        )
                        let recognized = try recognizeText(
                            in: image,
                            regionOfInterest: CGRect(
                                x: self.configuration.viewportSideCropFraction,
                                y: self.configuration.viewportBottomCropFraction,
                                width: 1 - (2 * self.configuration.viewportSideCropFraction),
                                height: 1
                                    - self.configuration.viewportTopCropFraction
                                    - self.configuration.viewportBottomCropFraction
                            ),
                            maxCharacters: self.configuration.maxCharacters
                        )
                        DispatchQueue.main.async {
                            self.emitScreenRead(
                                pending,
                                rawObservationID: rawObservationID,
                                settledAt: settledAt,
                                capturedAt: capturedAt,
                                surface: surface,
                                surfaceResolvedAt: surfaceResolvedAt,
                                postCaptureSurface: postCaptureSurface,
                                surfaceChangedDuringCapture: surfaceChangedDuringCapture,
                                captureBounds: captureBounds,
                                recognized: recognized,
                                retainedScreenshot: retainedScreenshot
                            )
                        }
                    } catch {
                        DispatchQueue.main.async {
                            self.reportCaptureErrorOnce("text recognition failed: \(error)")
                        }
                    }
                }
            }
        }
    }

    private func emitScreenRead(
        _ pending: PendingReadCandidate,
        rawObservationID: String,
        settledAt: String,
        capturedAt: String,
        surface: ResolvedReadSurface,
        surfaceResolvedAt: String,
        postCaptureSurface: ResolvedReadSurface?,
        surfaceChangedDuringCapture: Bool,
        captureBounds: CGRect,
        recognized: RecognizedScreenText,
        retainedScreenshot: RetainedScreenshot?
    ) {
        guard !configuration.isPaused() else { return }
        let supersedingWrite = supersedingWriteInput(
            for: surface,
            after: pending.lastActivityAt,
            capturedAt: capturedAt
        )
        let suppressesChromeAuxiliarySurface = isChromeAuxiliarySurface(
            bundleIdentifier: surface.bundleIdentifier,
            width: surface.windowBounds.width,
            height: surface.windowBounds.height
        )
        let derivedSuppressionReason = surfaceChangedDuringCapture
            ? "surface_changed_during_capture"
            : supersedingWrite != nil ? "read_candidate_superseded_by_write"
            : suppressesChromeAuxiliarySurface ? "chrome_auxiliary_surface" : nil
        var sourceRecordIDs: [String] = []
        if let rawWriter {
            do {
                _ = try rawWriter.write(
                    RawScreenReadRecord(
                        recordID: rawObservationID,
                        observedAt: nowTimestamp(),
                        settledAt: settledAt,
                        capturedAt: capturedAt,
                        surfaceResolvedAt: surfaceResolvedAt,
                        firstActivityAt: pending.firstActivityAt,
                        lastActivityAt: pending.lastActivityAt,
                        firstEventTimestampNanoseconds: pending.firstEventTimestampNanoseconds,
                        lastEventTimestampNanoseconds: pending.lastEventTimestampNanoseconds,
                        readDelaySeconds: configuration.readDelay,
                        triggerTypes: pending.triggerTypes.sorted(),
                        eventCount: pending.eventCount,
                        content: recognized.content,
                        recognizedLineCount: recognized.lineCount,
                        contentWasTruncated: recognized.wasTruncated,
                        screenshotRelativePath: retainedScreenshot?.relativePath,
                        screenshotSHA256: retainedScreenshot?.sha256,
                        screenshotPixelWidth: retainedScreenshot?.pixelWidth,
                        screenshotPixelHeight: retainedScreenshot?.pixelHeight,
                        derivedSuppressionReason: derivedSuppressionReason,
                        supersedingWriteAttemptID: supersedingWrite?.attemptID,
                        postCaptureSurface: postCaptureSurface?.record,
                        viewportSideCropFraction: configuration.viewportSideCropFraction,
                        viewportTopCropFraction: configuration.viewportTopCropFraction,
                        viewportBottomCropFraction: configuration.viewportBottomCropFraction,
                        windowBounds: rectValue(surface.windowBounds),
                        captureBounds: rectValue(captureBounds),
                        x: pending.lastX,
                        y: pending.lastY,
                        displayID: surface.displayID,
                        displayBounds: surface.displayBounds,
                        windowID: surface.windowID,
                        windowTitle: surface.windowTitle,
                        appName: surface.appName,
                        bundleIdentifier: surface.bundleIdentifier,
                        processIdentifier: surface.processIdentifier,
                        triggerSurface: pending.surfaceRecord
                    )
                )
                sourceRecordIDs = [rawObservationID]
            } catch {
                writeDiagnostic("could not preserve raw screen observation: \(error)")
                return
            }
        }
        guard derivedSuppressionReason == nil else { return }
        let contextIdentifier = "\(surface.processIdentifier)|\(surface.windowID)|\(surface.displayID)"
        do {
            if let data = try writer.writeViewport(
                contextIdentifier: contextIdentifier,
                viewportContent: recognized.content,
                makeValue: { emittedContent in
                self.sequence += 1
                let emittedLineCount = emittedContent
                    .split(separator: "\n", omittingEmptySubsequences: true)
                    .count
                return ScreenReadRecord(
                    sequence: self.sequence,
                    observedAt: nowTimestamp(),
                    settledAt: settledAt,
                    capturedAt: capturedAt,
                    surfaceResolvedAt: surfaceResolvedAt,
                    firstActivityAt: pending.firstActivityAt,
                    lastActivityAt: pending.lastActivityAt,
                    readDelaySeconds: self.configuration.readDelay,
                    triggerTypes: pending.triggerTypes.sorted(),
                    eventCount: pending.eventCount,
                    content: emittedContent,
                    emittedLineCount: emittedLineCount,
                    recognizedLineCount: recognized.lineCount,
                    overlapRemovedLineCount: max(recognized.lineCount - emittedLineCount, 0),
                    contentWasTruncated: recognized.wasTruncated,
                    viewportSideCropFraction: self.configuration.viewportSideCropFraction,
                    viewportTopCropFraction: self.configuration.viewportTopCropFraction,
                    viewportBottomCropFraction: self.configuration.viewportBottomCropFraction,
                    windowBounds: rectValue(surface.windowBounds),
                    captureBounds: rectValue(captureBounds),
                    x: pending.lastX,
                    y: pending.lastY,
                    displayID: surface.displayID,
                    displayBounds: surface.displayBounds,
                    windowID: surface.windowID,
                    windowTitle: surface.windowTitle,
                    appName: surface.appName,
                    bundleIdentifier: surface.bundleIdentifier,
                    processIdentifier: surface.processIdentifier,
                    triggerSurface: pending.surfaceRecord,
                    sourceRecordIDs: sourceRecordIDs
                )
            }) {
                writeLineToStandardOutput(data)
            }
        } catch {
            writeDiagnostic("could not write screen read: \(error)")
        }
    }

    private func reportCaptureErrorOnce(_ message: String) {
        guard !reportedCaptureError else { return }
        reportedCaptureError = true
        writeDiagnostic("screen-text capture unavailable: \(message)")
    }

    private func persistCandidateDisposition(
        _ pending: PendingReadCandidate,
        reason: String,
        boundary: ReadMutationBoundary? = nil,
        observedSurface: ResolvedReadSurface? = nil
    ) {
        guard let rawWriter else { return }
        _ = try? rawWriter.write(RawReadCandidateSuppression(
            recordID: UUID().uuidString,
            observedAt: nowTimestamp(),
            reason: reason,
            supersedingWriteAttemptID: boundary?.attemptID,
            supersedingInputAt: boundary?.observedAt,
            supersedingEventTimestampNanoseconds: boundary?.eventTimestampNanoseconds,
            observedSurface: observedSurface?.record,
            firstActivityAt: pending.firstActivityAt,
            lastActivityAt: pending.lastActivityAt,
            firstEventTimestampNanoseconds: pending.firstEventTimestampNanoseconds,
            lastEventTimestampNanoseconds: pending.lastEventTimestampNanoseconds,
            readDelaySeconds: configuration.readDelay,
            triggerTypes: pending.triggerTypes.sorted(),
            eventCount: pending.eventCount,
            x: pending.lastX,
            y: pending.lastY,
            displayID: pending.displayID,
            windowID: pending.windowID,
            windowTitle: pending.windowTitle,
            appName: pending.appName,
            bundleIdentifier: pending.bundleIdentifier,
            processIdentifier: pending.processIdentifier
        ))
    }

    private func persistSupersededCandidate(
        _ pending: PendingReadCandidate,
        boundary: ReadMutationBoundary
    ) {
        persistCandidateDisposition(
            pending,
            reason: "read_candidate_superseded_by_write",
            boundary: boundary
        )
    }

    private func supersedingWriteInput(
        for surface: ResolvedReadSurface,
        after lastActivityAt: String,
        capturedAt: String
    ) -> ReadMutationBoundary? {
        latestMutatingInputBySurface.values
            .filter {
                sameReadSurface(
                    processIdentifier: surface.processIdentifier,
                    windowID: surface.windowID,
                    as: $0
                )
                    && $0.observedAt > lastActivityAt
                    && $0.observedAt <= capturedAt
            }
            .max { $0.observedAt < $1.observedAt }
    }

    private func persistScreenshot(_ image: CGImage, recordID: String) throws
        -> RetainedScreenshot?
    {
        guard configuration.retainScreenshots else { return nil }
        let directory = URL(fileURLWithPath: configuration.screenshotsDirectory)
        try FileManager.default.createDirectory(
            at: directory,
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: NSNumber(value: 0o700)]
        )
        let relativePath = "screenshots/\(recordID).png"
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
        let digest = SHA256.hash(data: data)
            .map { String(format: "%02x", $0) }
            .joined()
        return RetainedScreenshot(
            relativePath: relativePath,
            sha256: digest,
            pixelWidth: image.width,
            pixelHeight: image.height
        )
    }

    private func installApplicationActivationObserver() {
        activationObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification,
            object: nil,
            queue: .main
        ) { [weak self] notification in
            guard let self,
                  let application = notification.userInfo?[NSWorkspace.applicationUserInfoKey]
                    as? NSRunningApplication else { return }
            Timer.scheduledTimer(withTimeInterval: 0.05, repeats: false) { [weak self] _ in
                self?.observeActivatedApplication(application)
            }
        }
    }

    private func observeActivatedApplication(_ application: NSRunningApplication) {
        guard !configuration.isPaused() else { return }
        let pointerPoint = CGEvent(source: nil)?.location
        guard let window = primaryContentWindow(
            ownedBy: application.processIdentifier,
            near: pointerPoint
        ) else { return }
        let point = pointerPoint.flatMap { window.bounds.contains($0) ? $0 : nil }
            ?? CGPoint(x: window.bounds.midX, y: window.bounds.midY)
        guard let surface = eligibleSurface(window: window, at: point) else { return }
        beginSurfaceTransitionInterval(
            surface,
            point: point,
            trigger: "application_activated",
            replacementReason: "read_candidate_replaced_by_application_activation"
        )
    }

    private func schedulePostClickSurfaceObservation(
        point: CGPoint,
        sourceKey: ReadCandidateKey,
        sourceEventTimestampNanoseconds: UInt64
    ) {
        Timer.scheduledTimer(withTimeInterval: 0.1, repeats: false) { [weak self] _ in
            guard let self,
                  let pending = self.pendingByContext[sourceKey],
                  pending.lastEventTimestampNanoseconds == sourceEventTimestampNanoseconds,
                  let surface = self.eligibleSurface(
                    window: topmostWindow(at: point),
                    at: point
                  ) else { return }
            self.beginSurfaceTransitionInterval(
                surface,
                point: point,
                trigger: "post_click_surface_observation",
                replacementReason: "read_candidate_replaced_by_post_click_surface"
            )
        }
    }

    private func beginSurfaceTransitionInterval(
        _ surface: ResolvedReadSurface,
        point: CGPoint,
        trigger: String = "surface_transition_detected",
        replacementReason: String = "read_candidate_replaced_by_surface_transition"
    ) {
        let key = surface.key
        collapsePendingCandidates(
            except: key,
            reason: replacementReason,
            observedSurface: surface
        )
        let observedAt = nowTimestamp()
        let eventTimestamp = DispatchTime.now().uptimeNanoseconds
        if var pending = pendingByContext[key], pending.matches(surface) {
            pending.lastActivityAt = observedAt
            pending.lastEventTimestampNanoseconds = eventTimestamp
            pending.triggerTypes.insert(trigger)
            pending.lastX = point.x
            pending.lastY = point.y
            pendingByContext[key] = pending
        } else {
            if let replaced = pendingByContext.removeValue(forKey: key) {
                timersByContext[key]?.invalidate()
                timersByContext[key] = nil
                persistCandidateDisposition(
                    replaced,
                    reason: replacementReason,
                    observedSurface: surface
                )
            }
            pendingByContext[key] = PendingReadCandidate(
                surface: surface,
                observedAt: observedAt,
                eventTimestampNanoseconds: eventTimestamp,
                trigger: trigger,
                eventCount: 0,
                point: point
            )
        }
        scheduleSettlement(for: key)
    }

    private func collapsePendingCandidates(
        except retainedKey: ReadCandidateKey?,
        reason: String,
        observedSurface: ResolvedReadSurface
    ) {
        let staleKeys = pendingByContext.keys.filter { $0 != retainedKey }
        for key in staleKeys {
            timersByContext[key]?.invalidate()
            timersByContext[key] = nil
            guard let stale = pendingByContext.removeValue(forKey: key) else { continue }
            persistCandidateDisposition(
                stale,
                reason: reason,
                observedSurface: observedSurface
            )
        }
    }

    private func scheduleSettlement(for key: ReadCandidateKey) {
        timersByContext[key]?.invalidate()
        timersByContext[key] = Timer.scheduledTimer(
            withTimeInterval: configuration.readDelay,
            repeats: false
        ) { [weak self] _ in
            self?.emitPendingCandidate(for: key)
        }
    }

    private func eligibleSurface(
        window: PointerWindowContext?,
        at point: CGPoint
    ) -> ResolvedReadSurface? {
        guard let window, let display = displayContext(at: point) else { return nil }
        let application = NSRunningApplication(
            processIdentifier: window.ownerProcessIdentifier
        )
        let bundleIdentifier = application?.bundleIdentifier
        let appName = application?.localizedName
            ?? window.ownerName
            ?? bundleIdentifier
            ?? "Unknown"
        guard configuration.captures(
            bundleIdentifier: bundleIdentifier,
            appName: appName
        ) else { return nil }
        return ResolvedReadSurface(
            resolvedAt: nowTimestamp(),
            displayID: display.id,
            displayBounds: display.bounds,
            windowID: window.windowID,
            windowTitle: window.title,
            windowBounds: window.bounds,
            appName: appName,
            bundleIdentifier: bundleIdentifier,
            processIdentifier: window.ownerProcessIdentifier
        )
    }

    private func resolvedWindow(
        at point: CGPoint,
        eventTimestamp: UInt64,
        forceRefresh: Bool
    ) -> PointerWindowContext? {
        let refreshInterval: UInt64 = 50_000_000
        let elapsed = eventTimestamp >= lastWindowLookupTimestamp
            ? eventTimestamp - lastWindowLookupTimestamp
            : refreshInterval

        if !forceRefresh, elapsed < refreshInterval {
            if let cachedPointerWindow, cachedPointerWindow.bounds.contains(point) {
                return cachedPointerWindow
            }
            if cachedPointerWindow == nil {
                return nil
            }
        }

        cachedPointerWindow = topmostWindow(at: point)
        lastWindowLookupTimestamp = eventTimestamp
        return cachedPointerWindow
    }
}

private struct ReadCandidateKey: Hashable {
    let processIdentifier: Int32
    let windowID: UInt32?
    let displayID: UInt32
}

private struct ReadMutationSurfaceKey: Hashable {
    let processIdentifier: Int32
    let windowID: UInt32?
}

private struct ReadMutationBoundary {
    let attemptID: String
    let observedAt: String
    let eventTimestampNanoseconds: UInt64
    let processIdentifier: Int32
    let windowID: UInt32?
}

private func sameReadSurface(
    processIdentifier: Int32,
    windowID: UInt32?,
    as boundary: ReadMutationBoundary
) -> Bool {
    guard processIdentifier == boundary.processIdentifier else { return false }
    guard let boundaryWindowID = boundary.windowID else { return true }
    return windowID == nil || windowID == boundaryWindowID
}

private struct PendingReadCandidate {
    let surfaceResolvedAt: String
    let firstActivityAt: String
    var lastActivityAt: String
    let firstEventTimestampNanoseconds: UInt64
    var lastEventTimestampNanoseconds: UInt64
    var eventCount: Int
    var triggerTypes: Set<String>
    var lastX: Double
    var lastY: Double
    let displayID: UInt32
    let displayBounds: RectValue
    let windowID: UInt32?
    let windowTitle: String?
    let windowBounds: RectValue?
    let appName: String
    let bundleIdentifier: String?
    let processIdentifier: Int32

    init(
        surface: ResolvedReadSurface,
        observedAt: String,
        eventTimestampNanoseconds: UInt64,
        trigger: String,
        eventCount: Int,
        point: CGPoint
    ) {
        surfaceResolvedAt = surface.resolvedAt
        firstActivityAt = observedAt
        lastActivityAt = observedAt
        firstEventTimestampNanoseconds = eventTimestampNanoseconds
        lastEventTimestampNanoseconds = eventTimestampNanoseconds
        self.eventCount = eventCount
        triggerTypes = [trigger]
        lastX = point.x
        lastY = point.y
        displayID = surface.displayID
        displayBounds = surface.displayBounds
        windowID = surface.windowID
        windowTitle = surface.windowTitle
        windowBounds = rectValue(surface.windowBounds)
        appName = surface.appName
        bundleIdentifier = surface.bundleIdentifier
        processIdentifier = surface.processIdentifier
    }

    func matches(_ surface: ResolvedReadSurface) -> Bool {
        displayID == surface.displayID
            && processIdentifier == surface.processIdentifier
            && windowID == surface.windowID
            && windowTitle == surface.windowTitle
            && approximatelyEqual(windowBounds, surface.windowBounds)
    }

    var surfaceRecord: ReadSurfaceRecord {
        ReadSurfaceRecord(
            resolvedAt: surfaceResolvedAt,
            displayID: displayID,
            displayBounds: displayBounds,
            windowID: windowID,
            windowTitle: windowTitle,
            windowBounds: windowBounds,
            appName: appName,
            bundleIdentifier: bundleIdentifier,
            processIdentifier: processIdentifier
        )
    }
}

private struct ResolvedReadSurface {
    let resolvedAt: String
    let displayID: UInt32
    let displayBounds: RectValue
    let windowID: UInt32
    let windowTitle: String?
    let windowBounds: CGRect
    let appName: String
    let bundleIdentifier: String?
    let processIdentifier: Int32

    var key: ReadCandidateKey {
        ReadCandidateKey(
            processIdentifier: processIdentifier,
            windowID: windowID,
            displayID: displayID
        )
    }

    func matches(_ other: ResolvedReadSurface) -> Bool {
        displayID == other.displayID
            && processIdentifier == other.processIdentifier
            && windowID == other.windowID
            && windowTitle == other.windowTitle
            && approximatelyEqual(rectValue(windowBounds), other.windowBounds)
    }

    var record: ReadSurfaceRecord {
        ReadSurfaceRecord(
            resolvedAt: resolvedAt,
            displayID: displayID,
            displayBounds: displayBounds,
            windowID: windowID,
            windowTitle: windowTitle,
            windowBounds: rectValue(windowBounds),
            appName: appName,
            bundleIdentifier: bundleIdentifier,
            processIdentifier: processIdentifier
        )
    }
}

private struct ReadSurfaceRecord: Encodable {
    let resolvedAt: String
    let displayID: UInt32
    let displayBounds: RectValue
    let windowID: UInt32?
    let windowTitle: String?
    let windowBounds: RectValue?
    let appName: String
    let bundleIdentifier: String?
    let processIdentifier: Int32
}

private func approximatelyEqual(_ stored: RectValue?, _ current: CGRect) -> Bool {
    guard let stored else { return false }
    let tolerance = 1.0
    return abs(stored.x - current.origin.x) <= tolerance
        && abs(stored.y - current.origin.y) <= tolerance
        && abs(stored.width - current.width) <= tolerance
        && abs(stored.height - current.height) <= tolerance
}

private struct ReadCandidateRecord: Encodable {
    let schemaVersion = 1
    let kind = "read_candidate"
    let provenance = "settled_pointer_activity"
    let sequence: UInt64
    let observedAt: String
    let firstActivityAt: String
    let lastActivityAt: String
    let firstEventTimestampNanoseconds: UInt64
    let lastEventTimestampNanoseconds: UInt64
    let readDelaySeconds: Double
    let triggerTypes: [String]
    let eventCount: Int
    let x: Double
    let y: Double
    let displayID: UInt32
    let displayBounds: RectValue
    let windowID: UInt32?
    let windowTitle: String?
    let windowBounds: RectValue?
    let appName: String
    let bundleIdentifier: String?
    let processIdentifier: Int32
}

private struct RawReadCandidateSuppression: Encodable {
    let schemaVersion = 2
    let recordType = "read_candidate_suppression"
    let recordID: String
    let observedAt: String
    let reason: String
    let supersedingWriteAttemptID: String?
    let supersedingInputAt: String?
    let supersedingEventTimestampNanoseconds: UInt64?
    let observedSurface: ReadSurfaceRecord?
    let firstActivityAt: String
    let lastActivityAt: String
    let firstEventTimestampNanoseconds: UInt64
    let lastEventTimestampNanoseconds: UInt64
    let readDelaySeconds: Double
    let triggerTypes: [String]
    let eventCount: Int
    let x: Double
    let y: Double
    let displayID: UInt32
    let windowID: UInt32?
    let windowTitle: String?
    let appName: String
    let bundleIdentifier: String?
    let processIdentifier: Int32
}

private struct ScreenReadRecord: Encodable {
    let schemaVersion = 7
    let kind = "read"
    let provenance = "screen_ocr"
    let sequence: UInt64
    let observedAt: String
    let settledAt: String
    let capturedAt: String
    let surfaceResolvedAt: String
    let firstActivityAt: String
    let lastActivityAt: String
    let readDelaySeconds: Double
    let triggerTypes: [String]
    let eventCount: Int
    let content: String
    let emittedLineCount: Int
    let recognizedLineCount: Int
    let overlapRemovedLineCount: Int
    let contentWasTruncated: Bool
    let viewportSideCropFraction: Double
    let viewportTopCropFraction: Double
    let viewportBottomCropFraction: Double
    let windowBounds: RectValue
    let captureBounds: RectValue
    let x: Double
    let y: Double
    let displayID: UInt32
    let displayBounds: RectValue
    let windowID: UInt32?
    let windowTitle: String?
    let appName: String
    let bundleIdentifier: String?
    let processIdentifier: Int32
    let triggerSurface: ReadSurfaceRecord
    let sourceRecordIDs: [String]
}

private struct RawScreenReadRecord: Encodable {
    let schemaVersion = 6
    let recordType = "screen_ocr_observation"
    let recordID: String
    let observedAt: String
    let settledAt: String
    let capturedAt: String
    let surfaceResolvedAt: String
    let firstActivityAt: String
    let lastActivityAt: String
    let firstEventTimestampNanoseconds: UInt64
    let lastEventTimestampNanoseconds: UInt64
    let readDelaySeconds: Double
    let triggerTypes: [String]
    let eventCount: Int
    let content: String
    let recognizedLineCount: Int
    let contentWasTruncated: Bool
    let screenshotRelativePath: String?
    let screenshotSHA256: String?
    let screenshotPixelWidth: Int?
    let screenshotPixelHeight: Int?
    let derivedSuppressionReason: String?
    let supersedingWriteAttemptID: String?
    let postCaptureSurface: ReadSurfaceRecord?
    let viewportSideCropFraction: Double
    let viewportTopCropFraction: Double
    let viewportBottomCropFraction: Double
    let windowBounds: RectValue
    let captureBounds: RectValue
    let x: Double
    let y: Double
    let displayID: UInt32
    let displayBounds: RectValue
    let windowID: UInt32?
    let windowTitle: String?
    let appName: String
    let bundleIdentifier: String?
    let processIdentifier: Int32
    let triggerSurface: ReadSurfaceRecord
}

private struct RecognizedScreenText {
    let content: String
    let lineCount: Int
    let wasTruncated: Bool
}

private struct RetainedScreenshot {
    let relativePath: String
    let sha256: String
    let pixelWidth: Int
    let pixelHeight: Int
}

private func recognizeText(
    in image: CGImage,
    regionOfInterest: CGRect,
    maxCharacters: Int
) throws -> RecognizedScreenText {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    request.automaticallyDetectsLanguage = true
    request.regionOfInterest = regionOfInterest

    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    try handler.perform([request])

    let observations = (request.results ?? []).sorted { left, right in
        let verticalDifference = abs(left.boundingBox.midY - right.boundingBox.midY)
        if verticalDifference > 0.02 {
            return left.boundingBox.midY > right.boundingBox.midY
        }
        return left.boundingBox.minX < right.boundingBox.minX
    }
    let lines = observations.compactMap { $0.topCandidates(1).first?.string }
    let fullContent = lines.joined(separator: "\n")
    let clipped = fullContent.count > maxCharacters
        ? String(fullContent.prefix(maxCharacters))
        : fullContent
    return RecognizedScreenText(
        content: clipped,
        lineCount: lines.count,
        wasTruncated: clipped.count < fullContent.count
    )
}

private struct PointerWindowContext {
    let windowID: UInt32
    let ownerProcessIdentifier: Int32
    let ownerName: String?
    let title: String?
    let bounds: CGRect
}

private func topmostWindow(at point: CGPoint) -> PointerWindowContext? {
    let options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
    guard let rawList = CGWindowListCopyWindowInfo(options, kCGNullWindowID),
          let windows = rawList as? [[String: Any]] else {
        return nil
    }

    for window in windows {
        let layer = (window[kCGWindowLayer as String] as? NSNumber)?.intValue
        guard layer == 0 else { continue }
        let alpha = (window[kCGWindowAlpha as String] as? NSNumber)?.doubleValue ?? 1
        guard alpha > 0 else { continue }
        guard let boundsDictionary = window[kCGWindowBounds as String] as? NSDictionary,
              let bounds = CGRect(dictionaryRepresentation: boundsDictionary) else {
            continue
        }
        guard bounds.width > 1, bounds.height > 1, bounds.contains(point) else { continue }
        guard let windowID = (window[kCGWindowNumber as String] as? NSNumber)?.uint32Value,
              let ownerPID = (window[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value else {
            continue
        }

        let rawTitle = window[kCGWindowName as String] as? String
        return PointerWindowContext(
            windowID: windowID,
            ownerProcessIdentifier: ownerPID,
            ownerName: window[kCGWindowOwnerName as String] as? String,
            title: rawTitle?.isEmpty == false ? rawTitle : nil,
            bounds: bounds
        )
    }
    return nil
}

private func topmostWindow(ownedBy processIdentifier: Int32) -> PointerWindowContext? {
    onScreenWindows(ownedBy: processIdentifier).first
}

/// App activation can publish narrow scrollbars, tab strips, and renderer
/// helpers ahead of the document window. Prefer a plausible window beneath the
/// pointer, then the largest plausible window owned by the activated app.
private func primaryContentWindow(
    ownedBy processIdentifier: Int32,
    near point: CGPoint?
) -> PointerWindowContext? {
    let candidates = onScreenWindows(ownedBy: processIdentifier).filter {
        $0.bounds.width >= 100 && $0.bounds.height >= 300
    }
    if let point, let pointed = candidates.first(where: { $0.bounds.contains(point) }) {
        return pointed
    }
    return candidates.max {
        ($0.bounds.width * $0.bounds.height) < ($1.bounds.width * $1.bounds.height)
    }
}

private func onScreenWindows(ownedBy processIdentifier: Int32)
    -> [PointerWindowContext]
{
    let options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
    guard let rawList = CGWindowListCopyWindowInfo(options, kCGNullWindowID),
          let windows = rawList as? [[String: Any]] else { return [] }
    var result = [PointerWindowContext]()
    for window in windows {
        let layer = (window[kCGWindowLayer as String] as? NSNumber)?.intValue
        let ownerPID = (window[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value
        guard layer == 0, ownerPID == processIdentifier else { continue }
        let alpha = (window[kCGWindowAlpha as String] as? NSNumber)?.doubleValue ?? 1
        guard alpha > 0,
              let boundsDictionary = window[kCGWindowBounds as String] as? NSDictionary,
              let bounds = CGRect(dictionaryRepresentation: boundsDictionary),
              bounds.width > 1,
              bounds.height > 1,
              let windowID = (window[kCGWindowNumber as String] as? NSNumber)?.uint32Value else {
            continue
        }
        let rawTitle = window[kCGWindowName as String] as? String
        result.append(PointerWindowContext(
            windowID: windowID,
            ownerProcessIdentifier: processIdentifier,
            ownerName: window[kCGWindowOwnerName as String] as? String,
            title: rawTitle?.isEmpty == false ? rawTitle : nil,
            bounds: bounds
        ))
    }
    return result
}

private func readTrigger(_ type: CGEventType) -> String? {
    switch type {
    case .mouseMoved:
        return "pointer_moved"
    case .leftMouseDragged, .rightMouseDragged, .otherMouseDragged:
        return "pointer_dragged"
    case .leftMouseDown, .rightMouseDown, .otherMouseDown:
        return "click"
    case .scrollWheel:
        return "scroll"
    default:
        return nil
    }
}

enum ReadCandidateCollectorError: Error, CustomStringConvertible {
    case eventTapUnavailable
    case screenshotPersistenceFailed(String)

    var description: String {
        switch self {
        case .eventTapUnavailable:
            return "could not create a global pointer event tap; grant Input Monitoring permission and try again"
        case .screenshotPersistenceFailed(let path):
            return "could not retain raw screenshot at \(path)"
        }
    }
}
