import AppKit
import ApplicationServices
import CoupledCore
import Foundation

/// Captures a focused editable immediately before its first mutation, retains
/// that exact AX element, then derives one settled before/after text diff.
final class ActiveTapWriteCollector {
    private let configuration: Configuration
    private let rawWriter: JSONLWriter
    private let eventWriter: JSONLWriter
    private let systemWideElement = AXUIElementCreateSystemWide()

    private var eventTap: CFMachPort?
    private var eventTapSource: CFRunLoopSource?
    private var writeTimer: Timer?
    private var pending: PendingActiveTapWrite?
    private var sequence: UInt64 = 0
    private var tapTimeoutCount: UInt64 = 0

    init(configuration: Configuration, rawWriter: JSONLWriter, eventWriter: JSONLWriter) {
        self.configuration = configuration
        self.rawWriter = rawWriter
        self.eventWriter = eventWriter
    }

    func start() throws {
        let timeoutError = AXUIElementSetMessagingTimeout(
            systemWideElement,
            Float(Self.axTimeoutSeconds)
        )
        writeDiagnostic(
            "AX messaging timeout: \(Self.axTimeoutSeconds) seconds (\(axErrorName(timeoutError)))"
        )
        primeRendererAccessibility()
        guard installActiveEventTap() else {
            throw ActiveTapWriteCollectorError.eventTapUnavailable
        }
        writeDiagnostic("active-tap write capture: \(eventWriter.path)")
        let enabledBundles = configuration.allowedBundles
            .subtracting(configuration.excludedBundles)
            .sorted()
        writeDiagnostic("write apps: \(enabledBundles.joined(separator: ", "))")
        writeDiagnostic("no polling; one before snapshot is held only during an active write burst")
    }

    private func primeRendererAccessibility() {
        guard configuration.activateRendererAccessibility else { return }
        for bundleIdentifier in configuration.allowedBundles.sorted() {
            guard !configuration.excludedBundles.contains(bundleIdentifier),
                  let running = NSRunningApplication.runningApplications(
                    withBundleIdentifier: bundleIdentifier
                  ).first else {
                continue
            }
            let applicationElement = AXUIElementCreateApplication(running.processIdentifier)
            let manual = AXUIElementSetAttributeValue(
                applicationElement,
                "AXManualAccessibility" as CFString,
                kCFBooleanTrue
            )
            let enhanced = AXUIElementSetAttributeValue(
                applicationElement,
                "AXEnhancedUserInterface" as CFString,
                kCFBooleanTrue
            )
            writeDiagnostic(
                "accessibility prime \(bundleIdentifier): manual=\(axErrorName(manual)) enhanced=\(axErrorName(enhanced))"
            )
        }
    }

    private func installActiveEventTap() -> Bool {
        let mask = CGEventMask(1) << CGEventMask(CGEventType.keyDown.rawValue)
        let callback: CGEventTapCallBack = { _, type, event, userInfo in
            guard let userInfo else { return Unmanaged.passUnretained(event) }
            let collector = Unmanaged<ActiveTapWriteCollector>
                .fromOpaque(userInfo)
                .takeUnretainedValue()

            if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
                collector.handleDisabledTap(type)
                return Unmanaged.passUnretained(event)
            }

            collector.handleKeyDown(event)
            return Unmanaged.passUnretained(event)
        }

        guard let tap = CGEvent.tapCreate(
            tap: .cgSessionEventTap,
            place: .headInsertEventTap,
            options: .defaultTap,
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

    private func handleDisabledTap(_ type: CGEventType) {
        let reason = type == .tapDisabledByTimeout ? "timeout" : "user_input"
        if type == .tapDisabledByTimeout {
            tapTimeoutCount += 1
        }
        let record = RawWriteSensorHealth(
            recordID: UUID().uuidString,
            observedAt: nowTimestamp(),
            event: "tap_disabled_\(reason)",
            totalTapTimeoutCount: tapTimeoutCount,
            activeAttemptID: pending?.attemptID
        )
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            _ = try? self.rawWriter.write(record)
        }
        if let eventTap {
            CGEvent.tapEnable(tap: eventTap, enable: true)
        }
    }

    private func handleKeyDown(_ event: CGEvent) {
        let callbackStarted = DispatchTime.now().uptimeNanoseconds
        let inputObservedAt = nowTimestamp()
        defer {
            let duration = milliseconds(sinceNanoseconds: callbackStarted)
            if var pending {
                if pending.firstCallbackDurationMilliseconds == nil {
                    pending.firstCallbackDurationMilliseconds = duration
                }
                pending.maximumCallbackDurationMilliseconds = max(
                    pending.maximumCallbackDurationMilliseconds,
                    duration
                )
                self.pending = pending
            }
        }

        guard !configuration.isPaused() else {
            discardPendingForPause()
            return
        }

        let classification = classifyKey(event)
        if let pending {
            let focused = captureFocusedTarget(includeValue: false, reason: "key_target")
            let remainsOnTarget: Bool
            if pending.target == nil {
                remainsOnTarget = focused.bundleIdentifier == pending.bundleIdentifier
                    && focused.target == nil
            } else {
                remainsOnTarget = sameTarget(pending.target, focused.target)
            }

            if remainsOnTarget {
                let returnCheckpoint = classification.isUnmodifiedReturn
                    ? captureReturnCheckpoint(
                        for: pending,
                        inputObservedAt: inputObservedAt,
                        event: event
                    )
                    : nil
                if let returnCheckpoint {
                    appendReturnCheckpoint(returnCheckpoint.record)
                }
                extendPending(
                    with: classification,
                    event: event,
                    inputObservedAt: inputObservedAt
                )
                if classification.isPaste {
                    recordPasteSignal(
                        event: event,
                        inputObservedAt: inputObservedAt
                    )
                }
                if shouldFinalizeBeforeReturn(classification: classification, pending: pending) {
                    completeCapture(
                        boundaryReason: "return_pressed",
                        deferPersistence: true,
                        callbackStartedNanoseconds: callbackStarted,
                        terminalOverride: returnCheckpoint?.capture
                    )
                }
                return
            }

            let boundary = focused.bundleIdentifier == pending.bundleIdentifier
                && focused.target == nil
                ? "focus_unavailable"
                : "target_changed"
            completeCapture(boundaryReason: boundary, deferPersistence: true)
            guard classification.canStartWrite else { return }

            let beforeStarted = DispatchTime.now().uptimeNanoseconds
            let before = focused.target.map {
                captureHeldTarget($0, reason: "write_before")
            } ?? focused
            startPending(
                with: classification,
                event: event,
                inputObservedAt: inputObservedAt,
                before: merging(focused, with: before),
                beforeDurationMilliseconds: milliseconds(
                    sinceNanoseconds: beforeStarted
                )
            )
            if classification.isPaste {
                recordPasteSignal(event: event, inputObservedAt: inputObservedAt)
            }
            return
        }

        guard classification.canStartWrite else { return }
        let beforeStarted = DispatchTime.now().uptimeNanoseconds
        let before = captureFocusedTarget(includeValue: true, reason: "write_before")
        startPending(
            with: classification,
            event: event,
            inputObservedAt: inputObservedAt,
            before: before,
            beforeDurationMilliseconds: milliseconds(sinceNanoseconds: beforeStarted)
        )
        if classification.isPaste {
            recordPasteSignal(event: event, inputObservedAt: inputObservedAt)
        }
    }

    private func startPending(
        with classification: KeyClassification,
        event: CGEvent,
        inputObservedAt: String,
        before: TargetCapture,
        beforeDurationMilliseconds: Double
    ) {
        guard before.isEligibleApplication, let bundleIdentifier = before.bundleIdentifier else {
            return
        }

        let attemptID = UUID().uuidString
        pending = PendingActiveTapWrite(
            attemptID: attemptID,
            bundleIdentifier: bundleIdentifier,
            target: before.target,
            before: before.observation,
            beforeAXErrors: before.errors,
            beganAt: inputObservedAt,
            lastInputAt: inputObservedAt,
            firstEventTimestampNanoseconds: event.timestamp,
            lastEventTimestampNanoseconds: event.timestamp,
            inputEventCount: 1,
            inputHints: [classification.hint],
            returnCheckpoints: [],
            pasteCheckpoints: [],
            beforeCaptureDurationMilliseconds: beforeDurationMilliseconds,
            firstCallbackDurationMilliseconds: nil,
            maximumCallbackDurationMilliseconds: 0,
            tapTimeoutCountAtStart: tapTimeoutCount
        )
        scheduleWriteTimer()
    }

    private func extendPending(
        with classification: KeyClassification,
        event: CGEvent,
        inputObservedAt: String
    ) {
        guard var pending else { return }
        pending.lastInputAt = inputObservedAt
        pending.lastEventTimestampNanoseconds = event.timestamp
        pending.inputEventCount += 1
        pending.inputHints.insert(classification.hint)
        self.pending = pending
        scheduleWriteTimer()
    }

    private func scheduleWriteTimer() {
        writeTimer?.invalidate()
        writeTimer = Timer.scheduledTimer(
            withTimeInterval: configuration.writeDelay,
            repeats: false
        ) { [weak self] _ in
            self?.completeCapture(boundaryReason: "write_delay_elapsed")
        }
    }

    private func completeCapture(
        boundaryReason: String,
        deferPersistence: Bool = false,
        callbackStartedNanoseconds: UInt64? = nil,
        terminalOverride: TargetCapture? = nil
    ) {
        guard !configuration.isPaused() else {
            discardPendingForPause()
            return
        }
        writeTimer?.invalidate()
        writeTimer = nil
        guard var pending else { return }
        self.pending = nil

        let after = terminalOverride ?? pending.target.map {
            captureHeldTarget($0, reason: "write_after_\(boundaryReason)")
        } ?? TargetCapture(
            bundleIdentifier: pending.bundleIdentifier,
            target: nil,
            observation: nil,
            errors: ["target:unavailable"]
        )
        if let callbackStartedNanoseconds {
            let duration = milliseconds(sinceNanoseconds: callbackStartedNanoseconds)
            if pending.firstCallbackDurationMilliseconds == nil {
                pending.firstCallbackDurationMilliseconds = duration
            }
            pending.maximumCallbackDurationMilliseconds = max(
                pending.maximumCallbackDurationMilliseconds,
                duration
            )
        }
        let tapTimeoutCountAtCompletion = tapTimeoutCount
        let terminalDecisionAt = nowTimestamp()

        let work: () -> Void = { [weak self] in
            guard let self else { return }
            self.persistAndDerive(
                pending: pending,
                after: after,
                boundaryReason: boundaryReason,
                terminalDecisionAt: terminalDecisionAt,
                tapTimeoutCountAtCompletion: tapTimeoutCountAtCompletion
            )
        }
        if deferPersistence {
            DispatchQueue.main.async(execute: work)
        } else {
            work()
        }
    }

    private func persistAndDerive(
        pending: PendingActiveTapWrite,
        after: TargetCapture,
        boundaryReason: String,
        terminalDecisionAt: String,
        tapTimeoutCountAtCompletion: UInt64
    ) {
        let decision: WriteDerivationDecision
        if tapTimeoutCountAtCompletion > pending.tapTimeoutCountAtStart {
            decision = .unresolved("tap_timeout")
        } else if pending.target == nil || pending.before == nil {
            decision = .unresolved("before_capture_failed")
        } else if !pending.beforeAXErrors.isEmpty {
            decision = .unresolved("ax_error")
        } else if pending.before!.valueWasTruncated {
            decision = .unresolved("value_truncated")
        } else {
            decision = deriveWrite(
                before: pending.before!,
                after: after,
                checkpoints: pending.returnCheckpoints,
                pasteCheckpoints: pending.pasteCheckpoints,
                inputHints: pending.inputHints,
                lastEventTimestampNanoseconds: pending.lastEventTimestampNanoseconds,
                boundaryReason: boundaryReason
            )
        }

        let proposedEventID = decision.edit == nil ? nil : UUID().uuidString
        let rawRecord = RawActiveTapWriteAttempt(
            recordID: pending.attemptID,
            bundleIdentifier: pending.bundleIdentifier,
            observedAt: nowTimestamp(),
            beganAt: pending.beganAt,
            lastInputAt: pending.lastInputAt,
            terminalDecisionAt: terminalDecisionAt,
            terminalSnapshotAt: after.observation?.observedAt,
            configuredWriteDelaySeconds: configuration.writeDelay,
            firstEventTimestampNanoseconds: pending.firstEventTimestampNanoseconds,
            lastEventTimestampNanoseconds: pending.lastEventTimestampNanoseconds,
            inputEventCount: pending.inputEventCount,
            inputHints: pending.inputHints.sorted(),
            boundaryReason: boundaryReason,
            resolution: decision.resolution,
            derivationObservationSource: decision.observationSource,
            fallbackReason: decision.fallbackReason,
            usedCheckpointID: decision.usedCheckpointID,
            usedObservationCapturedAt: decision.usedObservationCapturedAt,
            targetIdentity: pending.target?.identity,
            before: pending.before,
            after: after.observation,
            returnCheckpoints: pending.returnCheckpoints,
            pasteCheckpoints: pending.pasteCheckpoints,
            beforeAXErrors: pending.beforeAXErrors,
            afterAXErrors: after.errors,
            beforeCaptureDurationMilliseconds: pending.beforeCaptureDurationMilliseconds,
            firstCallbackDurationMilliseconds: pending.firstCallbackDurationMilliseconds,
            maximumCallbackDurationMilliseconds: pending.maximumCallbackDurationMilliseconds,
            tapTimeoutCountDuringBurst: tapTimeoutCountAtCompletion
                - pending.tapTimeoutCountAtStart,
            proposedEventID: proposedEventID
        )

        do {
            _ = try rawWriter.write(rawRecord)
        } catch {
            writeDiagnostic("could not persist active-tap write evidence: \(error)")
            return
        }

        guard let edit = decision.edit,
              let proposedEventID,
              let target = pending.target else {
            return
        }

        sequence += 1
        let record = ActiveTapWriteRecord(
            sequence: sequence,
            eventID: proposedEventID,
            observedAt: nowTimestamp(),
            beganAt: pending.beganAt,
            lastInputAt: pending.lastInputAt,
            terminalDecisionAt: terminalDecisionAt,
            terminalSnapshotAt: after.observation?.observedAt,
            derivationObservationSource: decision.observationSource!,
            fallbackReason: decision.fallbackReason,
            usedCheckpointID: decision.usedCheckpointID,
            usedObservationCapturedAt: decision.usedObservationCapturedAt!,
            configuredWriteDelaySeconds: configuration.writeDelay,
            operation: edit.operation.rawValue,
            content: edit.inserted,
            removedContent: edit.removed,
            characterOffset: edit.characterOffset,
            inputEventCount: pending.inputEventCount,
            boundaryReason: boundaryReason,
            appName: target.app.name,
            bundleIdentifier: target.app.bundleIdentifier,
            processIdentifier: target.app.processIdentifier,
            windowTitle: target.window.title,
            sourceRecordIDs: [pending.attemptID]
        )
        do {
            let data = try eventWriter.write(record)
            writeLineToStandardOutput(data)
        } catch {
            writeDiagnostic("could not write active-tap event: \(error)")
        }
    }

    private func deriveWrite(
        before: ActiveTapEditableObservation,
        after: TargetCapture,
        checkpoints: [ActiveTapReturnCheckpoint],
        pasteCheckpoints: [ActiveTapPasteCheckpoint],
        inputHints: Set<String>,
        lastEventTimestampNanoseconds: UInt64,
        boundaryReason: String
    ) -> WriteDerivationDecision {
        let beforeValue = logicalEditableValue(
            before.value,
            placeholderValue: before.placeholderValue
        )
        let returnCheckpoint = checkpoints.last.flatMap {
            meaningfulCheckpoint(
                checkpointID: $0.checkpointID,
                eventTimestampNanoseconds: $0.eventTimestampNanoseconds,
                observationSource: "pre_return_checkpoint",
                observation: $0.observation,
                errors: $0.axErrors,
                beforeValue: beforeValue
            )
        }
        let latestCheckpoint: MeaningfulCheckpoint? = {
            let latestReturn: ObservationCheckpoint? = checkpoints.last.flatMap {
                guard $0.eventTimestampNanoseconds == lastEventTimestampNanoseconds else {
                    return nil
                }
                return ObservationCheckpoint(
                    checkpointID: $0.checkpointID,
                    eventTimestampNanoseconds: $0.eventTimestampNanoseconds,
                    observationSource: "pre_return_checkpoint",
                    observation: $0.observation,
                    errors: $0.axErrors
                )
            }
            let latestPaste: ObservationCheckpoint? = pasteCheckpoints.last.flatMap {
                guard $0.eventTimestampNanoseconds == lastEventTimestampNanoseconds else {
                    return nil
                }
                return ObservationCheckpoint(
                    checkpointID: $0.checkpointID,
                    eventTimestampNanoseconds: $0.eventTimestampNanoseconds,
                    observationSource: "post_paste_checkpoint",
                    observation: $0.observation,
                    errors: $0.axErrors
                )
            }
            let latest = [latestReturn, latestPaste]
                .compactMap { $0 }
                .max { $0.eventTimestampNanoseconds < $1.eventTimestampNanoseconds }
            guard let latest else { return nil }
            return meaningfulCheckpoint(
                checkpointID: latest.checkpointID,
                eventTimestampNanoseconds: latest.eventTimestampNanoseconds,
                observationSource: latest.observationSource,
                observation: latest.observation,
                errors: latest.errors,
                beforeValue: beforeValue
            )
        }()

        if boundaryReason == "return_pressed" {
            guard let returnCheckpoint else { return .unresolved("no_change") }
            return .checkpoint(
                returnCheckpoint,
                fallbackReason: "immediate_terminal_return"
            )
        }

        let terminalIsInvalid = after.errors.contains { error in
            error.contains("invalid_ui_element")
                || error.contains("no_value")
                || error == "target:unavailable"
        }
        if terminalIsInvalid, let latestCheckpoint {
            return .checkpoint(latestCheckpoint, fallbackReason: "terminal_invalid")
        }
        guard let terminal = after.observation else {
            return .unresolved("after_capture_failed")
        }
        guard after.errors.isEmpty else { return .unresolved("ax_error") }
        guard !terminal.valueWasTruncated else { return .unresolved("value_truncated") }

        let terminalValue = logicalEditableValue(
            terminal.value,
            placeholderValue: terminal.placeholderValue
        )
        if let latestCheckpoint, terminal.value == before.value {
            return .checkpoint(latestCheckpoint, fallbackReason: "terminal_matches_before")
        }
        if let latestCheckpoint,
           terminalValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return .checkpoint(latestCheckpoint, fallbackReason: "terminal_unpopulated")
        }

        var edit = minimalTextEdit(from: beforeValue, to: terminalValue)
        var fallbackReason: String?
        if isRemovalOnlyWriteBurst(inputHints: inputHints), !edit.inserted.isEmpty {
            edit = minimalTextEdit(from: beforeValue, to: "")
            fallbackReason = "removal_only_terminal_unpopulated"
        }
        guard !edit.isEmpty else {
            return .unresolved(
                "no_change",
                observationSource: "terminal_after",
                usedObservationCapturedAt: terminal.observedAt
            )
        }
        return WriteDerivationDecision(
            edit: edit,
            resolution: "validated",
            observationSource: "terminal_after",
            fallbackReason: fallbackReason,
            usedCheckpointID: nil,
            usedObservationCapturedAt: terminal.observedAt
        )
    }

    private func meaningfulCheckpoint(
        checkpointID: String,
        eventTimestampNanoseconds: UInt64,
        observationSource: String,
        observation: ActiveTapEditableObservation?,
        errors: [String],
        beforeValue: String
    ) -> MeaningfulCheckpoint? {
        guard errors.isEmpty,
              let observation,
              !observation.valueWasTruncated else {
            return nil
        }
        let value = logicalEditableValue(
            observation.value,
            placeholderValue: observation.placeholderValue
        )
        let edit = minimalTextEdit(from: beforeValue, to: value)
        guard !edit.isEmpty else { return nil }
        return MeaningfulCheckpoint(
            checkpointID: checkpointID,
            eventTimestampNanoseconds: eventTimestampNanoseconds,
            observationSource: observationSource,
            observation: observation,
            edit: edit
        )
    }

    private func discardPendingForPause() {
        writeTimer?.invalidate()
        writeTimer = nil
        pending = nil
    }

    private func captureReturnCheckpoint(
        for pending: PendingActiveTapWrite,
        inputObservedAt: String,
        event: CGEvent
    ) -> CapturedReturnCheckpoint {
        let capture = pending.target.map {
            captureHeldTarget($0, reason: "pre_return_checkpoint")
        } ?? TargetCapture(
            bundleIdentifier: pending.bundleIdentifier,
            errors: ["target:unavailable"]
        )
        return CapturedReturnCheckpoint(
            capture: capture,
            record: ActiveTapReturnCheckpoint(
                checkpointID: UUID().uuidString,
                inputObservedAt: inputObservedAt,
                eventTimestampNanoseconds: event.timestamp,
                observation: capture.observation,
                axErrors: capture.errors
            )
        )
    }

    private func appendReturnCheckpoint(_ checkpoint: ActiveTapReturnCheckpoint) {
        guard var pending else { return }
        pending.returnCheckpoints.append(checkpoint)
        self.pending = pending
    }

    private func recordPasteSignal(event: CGEvent, inputObservedAt: String) {
        guard var pending else { return }
        let checkpointID = UUID().uuidString
        let pasteboard = NSPasteboard.general
        let clipboardValue = pasteboard.string(forType: .string)
        let clipped = clipboardValue.map {
            String($0.prefix(configuration.maxCharacters))
        }
        pending.pasteCheckpoints.append(
            ActiveTapPasteCheckpoint(
                checkpointID: checkpointID,
                inputObservedAt: inputObservedAt,
                eventTimestampNanoseconds: event.timestamp,
                clipboardObservedAt: nowTimestamp(),
                clipboardChangeCount: pasteboard.changeCount,
                clipboardTypes: pasteboard.types?.map(\.rawValue).sorted() ?? [],
                clipboardText: clipped,
                clipboardTextWasTruncated: clipboardValue.map {
                    clipped!.count < $0.count
                } ?? false,
                postPasteCaptureRequestedAt: nil,
                observation: nil,
                axErrors: []
            )
        )
        let attemptID = pending.attemptID
        let target = pending.target
        self.pending = pending

        Timer.scheduledTimer(
            withTimeInterval: configuration.postPasteCheckpointDelay,
            repeats: false
        ) { [weak self] _ in
            guard let self else { return }
            let requestedAt = nowTimestamp()
            let capture = target.map {
                self.captureHeldTarget($0, reason: "post_paste_checkpoint")
            } ?? TargetCapture(errors: ["target:unavailable"])
            self.completePasteCheckpoint(
                attemptID: attemptID,
                checkpointID: checkpointID,
                requestedAt: requestedAt,
                capture: capture
            )
        }
    }

    private func completePasteCheckpoint(
        attemptID: String,
        checkpointID: String,
        requestedAt: String,
        capture: TargetCapture
    ) {
        guard var pending,
              pending.attemptID == attemptID,
              let index = pending.pasteCheckpoints.firstIndex(where: {
                  $0.checkpointID == checkpointID
              }) else {
            return
        }
        pending.pasteCheckpoints[index].postPasteCaptureRequestedAt = requestedAt
        pending.pasteCheckpoints[index].observation = capture.observation
        pending.pasteCheckpoints[index].axErrors = capture.errors
        self.pending = pending
    }

    private func shouldFinalizeBeforeReturn(
        classification: KeyClassification,
        pending: PendingActiveTapWrite
    ) -> Bool {
        guard classification.isUnmodifiedReturn else { return false }
        let role = pending.target?.identity.role
        return role == (kAXTextFieldRole as String)
            || role == (kAXComboBoxRole as String)
    }

    private func captureFocusedTarget(includeValue: Bool, reason: String) -> TargetCapture {
        guard let running = NSWorkspace.shared.frontmostApplication else {
            return TargetCapture(errors: ["frontmost_application:no_value"])
        }
        let appName = running.localizedName ?? running.bundleIdentifier ?? "Unknown"
        guard configuration.captures(
            bundleIdentifier: running.bundleIdentifier,
            appName: appName
        ) else {
            return TargetCapture(bundleIdentifier: running.bundleIdentifier)
        }

        let app = AppContext(
            name: appName,
            bundleIdentifier: running.bundleIdentifier,
            processIdentifier: running.processIdentifier
        )
        let applicationElement = AXUIElementCreateApplication(running.processIdentifier)
        var errors: [String] = []
        let timeoutError = AXUIElementSetMessagingTimeout(
            applicationElement,
            Float(Self.axTimeoutSeconds)
        )
        if timeoutError != .success {
            errors.append("messaging_timeout:\(axErrorName(timeoutError))")
        }

        guard let focusedElement = elementAttribute(
            applicationElement,
            kAXFocusedUIElementAttribute,
            errors: &errors
        ) else {
            return TargetCapture(
                bundleIdentifier: running.bundleIdentifier,
                isEligibleApplication: true,
                errors: errors
            )
        }
        let targetTimeoutError = AXUIElementSetMessagingTimeout(
            focusedElement,
            Float(Self.axTimeoutSeconds)
        )
        if targetTimeoutError != .success {
            errors.append("messaging_timeout_target:\(axErrorName(targetTimeoutError))")
        }
        guard let role = stringAttribute(
            focusedElement,
            kAXRoleAttribute,
            errors: &errors
        ) else {
            errors.append("focused_element:missing_role")
            return TargetCapture(
                bundleIdentifier: running.bundleIdentifier,
                isEligibleApplication: true,
                errors: errors
            )
        }
        let subrole = stringAttribute(focusedElement, kAXSubroleAttribute, errors: &errors)
        if isSecureEditableSurface(role: role, subrole: subrole) {
            return TargetCapture(errors: ["focused_element:secure"])
        }
        guard isSupportedEditableSurface(role: role, subrole: subrole) else {
            errors.append("focused_element:unsupported_role:\(role)")
            return TargetCapture(
                bundleIdentifier: running.bundleIdentifier,
                isEligibleApplication: true,
                errors: errors
            )
        }

        let windowElement = elementAttribute(
            applicationElement,
            kAXFocusedWindowAttribute,
            errors: &errors
        )
        let window = WindowContext(
            title: windowElement.flatMap {
                stringAttribute($0, kAXTitleAttribute, errors: &errors)
            },
            identifier: windowElement.flatMap {
                stringAttribute($0, kAXIdentifierAttribute, errors: &errors)
            } ?? "unidentified-window"
        )
        let identity = ActiveTapTargetIdentity(
            elementHash: CFHash(focusedElement),
            processIdentifier: running.processIdentifier,
            bundleIdentifier: running.bundleIdentifier,
            windowTitle: window.title,
            role: role,
            accessibilityIdentifier: stringAttribute(
                focusedElement,
                kAXIdentifierAttribute,
                errors: &errors
            )
        )
        let target = HeldEditableTarget(
            element: focusedElement,
            applicationElement: applicationElement,
            identity: identity,
            app: app,
            window: window
        )
        guard includeValue else {
            return TargetCapture(
                bundleIdentifier: running.bundleIdentifier,
                isEligibleApplication: true,
                target: target,
                errors: errors
            )
        }
        let observation = captureObservation(
            from: target,
            reason: reason,
            errors: &errors
        )
        return TargetCapture(
            bundleIdentifier: running.bundleIdentifier,
            isEligibleApplication: true,
            target: target,
            observation: observation,
            errors: errors
        )
    }

    private func captureHeldTarget(_ target: HeldEditableTarget, reason: String) -> TargetCapture {
        var errors: [String] = []
        for (name, element) in [
            ("application", target.applicationElement),
            ("target", target.element),
        ] {
            let timeoutError = AXUIElementSetMessagingTimeout(
                element,
                Float(Self.axTimeoutSeconds)
            )
            if timeoutError != .success {
                errors.append("messaging_timeout_\(name):\(axErrorName(timeoutError))")
            }
        }
        let observation = captureObservation(from: target, reason: reason, errors: &errors)
        return TargetCapture(
            bundleIdentifier: target.app.bundleIdentifier,
            isEligibleApplication: true,
            target: target,
            observation: observation,
            errors: errors
        )
    }

    private func captureObservation(
        from target: HeldEditableTarget,
        reason: String,
        errors: inout [String]
    ) -> ActiveTapEditableObservation? {
        guard let value = requiredStringAttribute(
            target.element,
            kAXValueAttribute,
            errors: &errors
        ) else {
            return nil
        }
        let clipped = String(value.prefix(configuration.maxCharacters))
        let placeholderValue = stringAttribute(
            target.element,
            kAXPlaceholderValueAttribute,
            errors: &errors
        )
        let selection = rangeAttribute(
            target.element,
            kAXSelectedTextRangeAttribute,
            errors: &errors
        )
        return ActiveTapEditableObservation(
            observationID: UUID().uuidString,
            observedAt: nowTimestamp(),
            reason: reason,
            value: clipped,
            placeholderValue: placeholderValue,
            valueRepresentedPlaceholder: valueRepresentsPlaceholder(
                clipped,
                placeholderValue: placeholderValue
            ),
            selectedRangeLocation: selection?.location,
            selectedRangeLength: selection?.length,
            valueWasTruncated: clipped.count < value.count
        )
    }

    private static let axTimeoutSeconds = 0.2
}

private struct PendingActiveTapWrite {
    let attemptID: String
    let bundleIdentifier: String
    let target: HeldEditableTarget?
    let before: ActiveTapEditableObservation?
    let beforeAXErrors: [String]
    let beganAt: String
    var lastInputAt: String
    let firstEventTimestampNanoseconds: UInt64
    var lastEventTimestampNanoseconds: UInt64
    var inputEventCount: Int
    var inputHints: Set<String>
    var returnCheckpoints: [ActiveTapReturnCheckpoint]
    var pasteCheckpoints: [ActiveTapPasteCheckpoint]
    let beforeCaptureDurationMilliseconds: Double
    var firstCallbackDurationMilliseconds: Double?
    var maximumCallbackDurationMilliseconds: Double
    let tapTimeoutCountAtStart: UInt64
}

private struct CapturedReturnCheckpoint {
    let capture: TargetCapture
    let record: ActiveTapReturnCheckpoint
}

private struct HeldEditableTarget {
    let element: AXUIElement
    let applicationElement: AXUIElement
    let identity: ActiveTapTargetIdentity
    let app: AppContext
    let window: WindowContext
}

private struct TargetCapture {
    let bundleIdentifier: String?
    let isEligibleApplication: Bool
    let target: HeldEditableTarget?
    let observation: ActiveTapEditableObservation?
    let errors: [String]

    init(
        bundleIdentifier: String? = nil,
        isEligibleApplication: Bool = false,
        target: HeldEditableTarget? = nil,
        observation: ActiveTapEditableObservation? = nil,
        errors: [String] = []
    ) {
        self.bundleIdentifier = bundleIdentifier
        self.isEligibleApplication = isEligibleApplication
        self.target = target
        self.observation = observation
        self.errors = errors
    }
}

private struct ActiveTapTargetIdentity: Encodable {
    let elementHash: UInt
    let processIdentifier: Int32
    let bundleIdentifier: String?
    let windowTitle: String?
    let role: String
    let accessibilityIdentifier: String?
}

private struct ActiveTapEditableObservation: Encodable {
    let observationID: String
    let observedAt: String
    let reason: String
    let value: String
    let placeholderValue: String?
    let valueRepresentedPlaceholder: Bool
    let selectedRangeLocation: Int?
    let selectedRangeLength: Int?
    let valueWasTruncated: Bool
}

private struct ActiveTapReturnCheckpoint: Encodable {
    let checkpointID: String
    let inputObservedAt: String
    let eventTimestampNanoseconds: UInt64
    let observation: ActiveTapEditableObservation?
    let axErrors: [String]
}

private struct ActiveTapPasteCheckpoint: Encodable {
    let checkpointID: String
    let inputObservedAt: String
    let eventTimestampNanoseconds: UInt64
    let clipboardObservedAt: String
    let clipboardChangeCount: Int
    let clipboardTypes: [String]
    let clipboardText: String?
    let clipboardTextWasTruncated: Bool
    var postPasteCaptureRequestedAt: String?
    var observation: ActiveTapEditableObservation?
    var axErrors: [String]
}

private struct ObservationCheckpoint {
    let checkpointID: String
    let eventTimestampNanoseconds: UInt64
    let observationSource: String
    let observation: ActiveTapEditableObservation?
    let errors: [String]
}

private struct MeaningfulCheckpoint {
    let checkpointID: String
    let eventTimestampNanoseconds: UInt64
    let observationSource: String
    let observation: ActiveTapEditableObservation
    let edit: TextEdit
}

private struct WriteDerivationDecision {
    let edit: TextEdit?
    let resolution: String
    let observationSource: String?
    let fallbackReason: String?
    let usedCheckpointID: String?
    let usedObservationCapturedAt: String?

    static func unresolved(
        _ resolution: String,
        observationSource: String? = nil,
        usedObservationCapturedAt: String? = nil
    ) -> WriteDerivationDecision {
        WriteDerivationDecision(
            edit: nil,
            resolution: resolution,
            observationSource: observationSource,
            fallbackReason: nil,
            usedCheckpointID: nil,
            usedObservationCapturedAt: usedObservationCapturedAt
        )
    }

    static func checkpoint(
        _ checkpoint: MeaningfulCheckpoint,
        fallbackReason: String
    ) -> WriteDerivationDecision {
        WriteDerivationDecision(
            edit: checkpoint.edit,
            resolution: "validated",
            observationSource: checkpoint.observationSource,
            fallbackReason: fallbackReason,
            usedCheckpointID: checkpoint.checkpointID,
            usedObservationCapturedAt: checkpoint.observation.observedAt
        )
    }
}

private struct RawActiveTapWriteAttempt: Encodable {
    let schemaVersion = 4
    let recordType = "active_tap_write_attempt"
    let recordID: String
    let bundleIdentifier: String
    let observedAt: String
    let beganAt: String
    let lastInputAt: String
    let terminalDecisionAt: String
    let terminalSnapshotAt: String?
    let configuredWriteDelaySeconds: Double
    let firstEventTimestampNanoseconds: UInt64
    let lastEventTimestampNanoseconds: UInt64
    let inputEventCount: Int
    let inputHints: [String]
    let boundaryReason: String
    let resolution: String
    let derivationObservationSource: String?
    let fallbackReason: String?
    let usedCheckpointID: String?
    let usedObservationCapturedAt: String?
    let targetIdentity: ActiveTapTargetIdentity?
    let before: ActiveTapEditableObservation?
    let after: ActiveTapEditableObservation?
    let returnCheckpoints: [ActiveTapReturnCheckpoint]
    let pasteCheckpoints: [ActiveTapPasteCheckpoint]
    let beforeAXErrors: [String]
    let afterAXErrors: [String]
    let beforeCaptureDurationMilliseconds: Double
    let firstCallbackDurationMilliseconds: Double?
    let maximumCallbackDurationMilliseconds: Double
    let tapTimeoutCountDuringBurst: UInt64
    let proposedEventID: String?
}

private struct RawWriteSensorHealth: Encodable {
    let schemaVersion = 1
    let recordType = "write_sensor_health"
    let recordID: String
    let observedAt: String
    let event: String
    let totalTapTimeoutCount: UInt64
    let activeAttemptID: String?
}

private struct ActiveTapWriteRecord: Encodable {
    let schemaVersion = 4
    let kind = "write"
    let provenance = "active_tap_accessibility_diff"
    let sequence: UInt64
    let eventID: String
    let observedAt: String
    let beganAt: String
    let lastInputAt: String
    let terminalDecisionAt: String
    let terminalSnapshotAt: String?
    let derivationObservationSource: String
    let fallbackReason: String?
    let usedCheckpointID: String?
    let usedObservationCapturedAt: String
    let configuredWriteDelaySeconds: Double
    let operation: String
    let content: String
    let removedContent: String
    let characterOffset: Int
    let inputEventCount: Int
    let boundaryReason: String
    let appName: String
    let bundleIdentifier: String?
    let processIdentifier: Int32
    let windowTitle: String?
    let sourceRecordIDs: [String]
}

private struct KeyClassification {
    let canStartWrite: Bool
    let hint: String
    let isUnmodifiedReturn: Bool
    let isPaste: Bool

    init(
        canStartWrite: Bool,
        hint: String,
        isUnmodifiedReturn: Bool = false,
        isPaste: Bool = false
    ) {
        self.canStartWrite = canStartWrite
        self.hint = hint
        self.isUnmodifiedReturn = isUnmodifiedReturn
        self.isPaste = isPaste
    }
}

private func classifyKey(_ event: CGEvent) -> KeyClassification {
    let code = event.getIntegerValueField(.keyboardEventKeycode)
    if event.flags.contains(.maskCommand) {
        switch code {
        case 9: return KeyClassification(canStartWrite: true, hint: "paste", isPaste: true)
        case 7: return KeyClassification(canStartWrite: true, hint: "cut")
        case 6: return KeyClassification(canStartWrite: true, hint: "undo_redo")
        default: return KeyClassification(canStartWrite: false, hint: "shortcut")
        }
    }
    if event.flags.contains(.maskControl) {
        return KeyClassification(canStartWrite: false, hint: "control_shortcut")
    }
    let returnCodes: Set<Int64> = [36, 76]
    let mutationModifiers: CGEventFlags = [.maskShift, .maskCommand, .maskControl, .maskAlternate]
    if returnCodes.contains(code), event.flags.intersection(mutationModifiers).isEmpty {
        return KeyClassification(
            canStartWrite: false,
            hint: "return",
            isUnmodifiedReturn: true
        )
    }
    if code == 51 || code == 117 {
        return KeyClassification(canStartWrite: true, hint: "delete")
    }
    let navigationCodes: Set<Int64> = [53, 115, 116, 119, 121, 123, 124, 125, 126]
    if navigationCodes.contains(code) {
        return KeyClassification(canStartWrite: false, hint: "navigation")
    }
    return KeyClassification(canStartWrite: true, hint: "typed")
}

private func sameTarget(_ left: HeldEditableTarget?, _ right: HeldEditableTarget?) -> Bool {
    guard let left, let right else { return false }
    var leftPID: pid_t = 0
    var rightPID: pid_t = 0
    guard AXUIElementGetPid(left.element, &leftPID) == .success,
          AXUIElementGetPid(right.element, &rightPID) == .success,
          leftPID == rightPID else {
        return false
    }
    return CFEqual(left.element, right.element)
}

private func merging(_ identity: TargetCapture, with value: TargetCapture) -> TargetCapture {
    TargetCapture(
        bundleIdentifier: value.bundleIdentifier ?? identity.bundleIdentifier,
        isEligibleApplication: value.isEligibleApplication || identity.isEligibleApplication,
        target: value.target ?? identity.target,
        observation: value.observation,
        errors: identity.errors + value.errors
    )
}

private func elementAttribute(
    _ element: AXUIElement,
    _ attribute: String,
    errors: inout [String]
) -> AXUIElement? {
    var value: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(element, attribute as CFString, &value)
    guard error == .success, let value else {
        errors.append("\(attribute):\(axErrorName(error))")
        return nil
    }
    guard CFGetTypeID(value) == AXUIElementGetTypeID() else {
        errors.append("\(attribute):wrong_type")
        return nil
    }
    return unsafeBitCast(value, to: AXUIElement.self)
}

private func stringAttribute(
    _ element: AXUIElement,
    _ attribute: String,
    errors: inout [String]
) -> String? {
    var value: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(element, attribute as CFString, &value)
    guard error == .success else {
        if error != .noValue && error != .attributeUnsupported {
            errors.append("\(attribute):\(axErrorName(error))")
        }
        return nil
    }
    return value as? String
}

private func requiredStringAttribute(
    _ element: AXUIElement,
    _ attribute: String,
    errors: inout [String]
) -> String? {
    var value: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(element, attribute as CFString, &value)
    guard error == .success, let string = value as? String else {
        errors.append("\(attribute):\(axErrorName(error))")
        return nil
    }
    return string
}

private func rangeAttribute(
    _ element: AXUIElement,
    _ attribute: String,
    errors: inout [String]
) -> CFRange? {
    var value: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(element, attribute as CFString, &value)
    guard error == .success, let value else {
        if error != .noValue && error != .attributeUnsupported {
            errors.append("\(attribute):\(axErrorName(error))")
        }
        return nil
    }
    guard CFGetTypeID(value) == AXValueGetTypeID() else { return nil }
    let axValue = unsafeBitCast(value, to: AXValue.self)
    guard AXValueGetType(axValue) == .cfRange else { return nil }
    var range = CFRange()
    return AXValueGetValue(axValue, .cfRange, &range) ? range : nil
}

private func axErrorName(_ error: AXError) -> String {
    switch error {
    case .success: return "success"
    case .failure: return "failure"
    case .illegalArgument: return "illegal_argument"
    case .invalidUIElement: return "invalid_ui_element"
    case .invalidUIElementObserver: return "invalid_ui_element_observer"
    case .cannotComplete: return "cannot_complete"
    case .attributeUnsupported: return "attribute_unsupported"
    case .actionUnsupported: return "action_unsupported"
    case .notificationUnsupported: return "notification_unsupported"
    case .notImplemented: return "not_implemented"
    case .notificationAlreadyRegistered: return "notification_already_registered"
    case .notificationNotRegistered: return "notification_not_registered"
    case .apiDisabled: return "api_disabled"
    case .noValue: return "no_value"
    case .parameterizedAttributeUnsupported: return "parameterized_attribute_unsupported"
    case .notEnoughPrecision: return "not_enough_precision"
    @unknown default: return "unknown_\(error.rawValue)"
    }
}

private func milliseconds(sinceNanoseconds started: UInt64) -> Double {
    let finished = DispatchTime.now().uptimeNanoseconds
    return Double(finished - started) / 1_000_000
}

enum ActiveTapWriteCollectorError: Error, CustomStringConvertible {
    case eventTapUnavailable

    var description: String {
        "could not create active write event tap; grant Accessibility and Input Monitoring, then try again"
    }
}
