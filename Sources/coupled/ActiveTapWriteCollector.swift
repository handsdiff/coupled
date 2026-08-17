import AppKit
import ApplicationServices
import CoupledCore
import CryptoKit
import Foundation
import ScreenCaptureKit

/// Captures a focused editable immediately before its first mutation, retains
/// that exact AX element, then derives one settled before/after text diff.
final class ActiveTapWriteCollector {
    private let configuration: Configuration
    private let rawWriter: JSONLWriter
    private let eventWriter: JSONLWriter
    private let mutatingInputObserver: ((MutatingWriteInput) -> Void)?
    private let systemWideElement = AXUIElementCreateSystemWide()

    private var eventTap: CFMachPort?
    private var eventTapSource: CFRunLoopSource?
    private var writeTimer: Timer?
    private var mutationCheckpointTimer: Timer?
    private var pending: PendingActiveTapWrite?
    private var sequence: UInt64 = 0
    private var tapTimeoutCount: UInt64 = 0

    init(
        configuration: Configuration,
        rawWriter: JSONLWriter,
        eventWriter: JSONLWriter,
        mutatingInputObserver: ((MutatingWriteInput) -> Void)? = nil
    ) {
        self.configuration = configuration
        self.rawWriter = rawWriter
        self.eventWriter = eventWriter
        self.mutatingInputObserver = mutatingInputObserver
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
        let observedTypes: [CGEventType] = [
            .keyDown,
            .leftMouseDown,
            .rightMouseDown,
            .otherMouseDown,
        ]
        let mask = observedTypes.reduce(CGEventMask(0)) { partial, type in
            partial | (CGEventMask(1) << CGEventMask(type.rawValue))
        }
        let callback: CGEventTapCallBack = { _, type, event, userInfo in
            guard let userInfo else { return Unmanaged.passUnretained(event) }
            let collector = Unmanaged<ActiveTapWriteCollector>
                .fromOpaque(userInfo)
                .takeUnretainedValue()

            if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
                collector.handleDisabledTap(type)
                return Unmanaged.passUnretained(event)
            }

            if type == .keyDown {
                collector.handleKeyDown(event)
            } else {
                collector.handlePointerBoundary(type)
            }
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
        if let active = pending,
           active.conditioningClipboard.changeCount != NSPasteboard.general.changeCount {
            completeCapture(
                boundaryReason: "clipboard_changed",
                deferPersistence: true,
                callbackStartedNanoseconds: callbackStarted
            )
        }
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
                completeOutstandingPasteCheckpointsSynchronously()
                if classification.isSelectionBoundary {
                    completeCapture(
                        boundaryReason: "selection_navigation",
                        deferPersistence: true,
                        callbackStartedNanoseconds: callbackStarted
                    )
                    return
                }
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
                reportMutatingInput(
                    classification: classification,
                    event: event,
                    inputObservedAt: inputObservedAt
                )
                if classification.isPaste {
                    recordPasteSignal(
                        event: event,
                        inputObservedAt: inputObservedAt
                    )
                }
                if classification.canStartWrite, !classification.isPaste {
                    scheduleMutationCheckpoint(
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
            reportMutatingInput(
                classification: classification,
                event: event,
                inputObservedAt: inputObservedAt
            )
            if classification.isPaste {
                recordPasteSignal(event: event, inputObservedAt: inputObservedAt)
            } else {
                scheduleMutationCheckpoint(event: event, inputObservedAt: inputObservedAt)
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
        reportMutatingInput(
            classification: classification,
            event: event,
            inputObservedAt: inputObservedAt
        )
        if classification.isPaste {
            recordPasteSignal(event: event, inputObservedAt: inputObservedAt)
        } else {
            scheduleMutationCheckpoint(event: event, inputObservedAt: inputObservedAt)
        }
    }

    private func handlePointerBoundary(_ type: CGEventType) {
        let callbackStarted = DispatchTime.now().uptimeNanoseconds
        guard pending != nil else { return }
        completeOutstandingPasteCheckpointsSynchronously()
        completeCapture(
            boundaryReason: type == .leftMouseDown
                ? "pointer_selection_boundary"
                : "pointer_context_boundary",
            deferPersistence: true,
            callbackStartedNanoseconds: callbackStarted
        )
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
        let conditioningClipboard = clipboardSnapshot()
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
            inputEvents: [ActiveTapInputEvidence(
                observedAt: inputObservedAt,
                eventTimestampNanoseconds: event.timestamp,
                hint: classification.hint,
                mutationCapable: classification.canStartWrite
            )],
            returnCheckpoints: [],
            pasteCheckpoints: [],
            mutationCheckpoints: [],
            conditioningClipboard: conditioningClipboard,
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
        pending.inputEvents.append(ActiveTapInputEvidence(
            observedAt: inputObservedAt,
            eventTimestampNanoseconds: event.timestamp,
            hint: classification.hint,
            mutationCapable: classification.canStartWrite
        ))
        self.pending = pending
        scheduleWriteTimer()
    }

    private func reportMutatingInput(
        classification: KeyClassification,
        event: CGEvent,
        inputObservedAt: String
    ) {
        guard classification.canStartWrite,
              let pending,
              let processIdentifier = pending.target?.app.processIdentifier else { return }
        mutatingInputObserver?(MutatingWriteInput(
            attemptID: pending.attemptID,
            observedAt: inputObservedAt,
            eventTimestampNanoseconds: event.timestamp,
            processIdentifier: processIdentifier
        ))
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
        mutationCheckpointTimer?.invalidate()
        mutationCheckpointTimer = nil
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
        // raw.jsonl is the authoritative substrate. Persist the complete sensor
        // evidence before any provisional semantic interpretation runs.
        let conditioningState = writeConditioningState(for: pending)
        let rawEvidence = RawActiveTapWriteEvidence(
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
            inputEvents: pending.inputEvents,
            boundaryReason: boundaryReason,
            conditioningState: conditioningState,
            targetIdentity: pending.target?.identity,
            before: pending.before,
            after: after.observation,
            returnCheckpoints: pending.returnCheckpoints,
            pasteCheckpoints: pending.pasteCheckpoints,
            mutationCheckpoints: pending.mutationCheckpoints,
            beforeAXErrors: pending.beforeAXErrors,
            afterAXErrors: after.errors,
            beforeCaptureDurationMilliseconds: pending.beforeCaptureDurationMilliseconds,
            firstCallbackDurationMilliseconds: pending.firstCallbackDurationMilliseconds,
            maximumCallbackDurationMilliseconds: pending.maximumCallbackDurationMilliseconds,
            tapTimeoutCountDuringBurst: tapTimeoutCountAtCompletion
                - pending.tapTimeoutCountAtStart
        )
        do {
            _ = try rawWriter.write(rawEvidence)
        } catch {
            writeDiagnostic("could not persist active-tap write evidence: \(error)")
            return
        }

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
                mutationCheckpoints: pending.mutationCheckpoints,
                lastEventTimestampNanoseconds: pending.lastEventTimestampNanoseconds,
                boundaryReason: boundaryReason
            )
        }

        let authorship = decision.edit.flatMap {
            deriveAuthorship(
                overallEdit: $0,
                pending: pending,
                usedObservation: usedObservation(
                    for: decision,
                    pending: pending,
                    terminal: after.observation
                )
            )
        }
        let proposedEventID = decision.edit == nil ? nil : UUID().uuidString
        let cursorFidelity = cursorFidelityEvidence(
            for: pending,
            terminalEditOffset: decision.edit?.characterOffset
        )
        guard let edit = decision.edit,
              let proposedEventID,
              let target = pending.target,
              let conditioningState else {
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
            conditioningState: conditioningState,
            cursorFidelity: cursorFidelity,
            authorshipResolution: authorship?.result.resolution ?? "unresolved",
            authorshipEvidence: authorship?.evidence,
            authorshipSegments: authorship?.result.segments ?? [],
            resolvedCompletion: authorship?.resolvedCompletion ?? edit.inserted,
            stateContinuity: authorship?.stateContinuity ?? "single_ax_epoch",
            observedNetEdit: ActiveTapWriteOutcome(edit),
            outcome: ActiveTapWriteOutcome(edit),
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

    private func writeConditioningState(
        for pending: PendingActiveTapWrite
    ) -> ActiveTapWriteConditioningState? {
        guard let target = pending.target, let before = pending.before else { return nil }
        return ActiveTapWriteConditioningState(
            schemaVersion: 3,
            captureSemantics: "synchronous_before_application_mutation",
            inputInterceptedAt: pending.beganAt,
            capturedAt: before.observedAt,
            destination: ActiveTapWriteDestination(
                appName: target.app.name,
                bundleIdentifier: target.app.bundleIdentifier,
                processIdentifier: target.app.processIdentifier,
                windowTitle: target.window.title,
                resource: nil,
                role: target.identity.role,
                subrole: target.identity.subrole,
                fieldIdentifier: target.identity.accessibilityIdentifier,
                fieldLabel: target.identity.fieldLabel,
                fieldDescription: target.identity.fieldDescription,
                placeholder: before.placeholderValue
            ),
            cursorContext: rangeSemanticCursorContext(
                target: target,
                before: before
            ),
            clipboard: pending.conditioningClipboard,
            sourceObservationID: before.observationID
        )
    }

    private func deriveAuthorship(
        overallEdit: TextEdit,
        pending: PendingActiveTapWrite,
        usedObservation: ActiveTapEditableObservation?
    ) -> ActiveTapAuthorshipDerivation {
        var pasteMutations = [ProvenPasteMutation]()
        for checkpoint in pending.pasteCheckpoints {
            guard checkpoint.clipboardSnapshotID
                    == pending.conditioningClipboard.snapshotID,
                  checkpoint.clipboardChangeCount
                    == pending.conditioningClipboard.changeCount else {
                return ActiveTapAuthorshipDerivation(
                    result: WriteAuthorshipResult(
                        segments: [],
                        resolution: "clipboard_changed_after_conditioning"
                    ),
                    resolvedCompletion: overallEdit.inserted,
                    stateContinuity: "single_ax_epoch",
                    evidence: nil
                )
            }
            guard checkpoint.prePasteAXErrors.isEmpty,
                  checkpoint.axErrors.isEmpty,
                  let before = checkpoint.prePasteObservation,
                  let after = checkpoint.observation,
                  !before.valueWasTruncated,
                  !after.valueWasTruncated,
                  let clipboardText = checkpoint.clipboardText else {
                return ActiveTapAuthorshipDerivation(
                    result: WriteAuthorshipResult(
                        segments: [],
                        resolution: "paste_checkpoint_incomplete"
                    ),
                    resolvedCompletion: overallEdit.inserted,
                    stateContinuity: "single_ax_epoch",
                    evidence: nil
                )
            }
            let pasteEdit = minimalTextEdit(
                from: logicalEditableValue(
                    before.value,
                    placeholderValue: before.placeholderValue
                ),
                to: logicalEditableValue(
                    after.value,
                    placeholderValue: after.placeholderValue
                )
            )
            guard !pasteEdit.isEmpty, pasteEdit.inserted == clipboardText else {
                if let segmented = groundedPasteEpochCompletion(
                    overallEdit: overallEdit,
                    pending: pending,
                    checkpoint: checkpoint,
                    usedObservation: usedObservation
                ) {
                    return segmented
                }
                return ActiveTapAuthorshipDerivation(
                    result: WriteAuthorshipResult(
                        segments: [],
                        resolution: "paste_transition_does_not_match_clipboard"
                    ),
                    resolvedCompletion: overallEdit.inserted,
                    stateContinuity: "single_ax_epoch",
                    evidence: nil
                )
            }
            pasteMutations.append(ProvenPasteMutation(
                checkpointID: checkpoint.checkpointID,
                clipboardSnapshotID: checkpoint.clipboardSnapshotID,
                characterOffset: pasteEdit.characterOffset,
                inserted: pasteEdit.inserted
            ))
        }
        let result = writeAuthorship(
                overallEdit: overallEdit,
                pasteMutations: pasteMutations
            )
        return ActiveTapAuthorshipDerivation(
            result: result,
            resolvedCompletion: result.resolution == "resolved"
                ? result.segments.map(\.content).joined()
                : overallEdit.inserted,
            stateContinuity: "single_ax_epoch",
            evidence: nil
        )
    }

    private func groundedPasteEpochCompletion(
        overallEdit: TextEdit,
        pending: PendingActiveTapWrite,
        checkpoint: ActiveTapPasteCheckpoint,
        usedObservation: ActiveTapEditableObservation?
    ) -> ActiveTapAuthorshipDerivation? {
        guard pending.pasteCheckpoints.count == 1,
              let initial = pending.before,
              !initial.valueWasTruncated,
              checkpoint.prePasteAXErrors.isEmpty,
              checkpoint.axErrors.isEmpty,
              !checkpoint.clipboardTextWasTruncated,
              let clipboardText = checkpoint.clipboardText,
              !clipboardText.isEmpty,
              let prePaste = checkpoint.prePasteObservation,
              let postPaste = checkpoint.observation,
              let usedObservation,
              !prePaste.valueWasTruncated,
              !postPaste.valueWasTruncated,
              !usedObservation.valueWasTruncated,
              prePaste.selectedRangeLength == 0,
              postPaste.selectedRangeLocation == 0,
              postPaste.selectedRangeLength == 0 else { return nil }

        let initialValue = logicalEditableValue(
            initial.value,
            placeholderValue: initial.placeholderValue
        )
        let prePasteValue = logicalEditableValue(
            prePaste.value,
            placeholderValue: prePaste.placeholderValue
        )
        let postPasteValue = logicalEditableValue(
            postPaste.value,
            placeholderValue: postPaste.placeholderValue
        )
        let usedValue = logicalEditableValue(
            usedObservation.value,
            placeholderValue: usedObservation.placeholderValue
        )
        guard postPasteValue.isEmpty,
              let observedAfter = applying(overallEdit, to: initialValue),
              observedAfter == usedValue,
              let completion = segmentedGroundedPasteCompletion(
                initialValue: initialValue,
                prePasteValue: prePasteValue,
                postPasteValue: postPasteValue,
                terminalValue: usedValue,
                clipboardText: clipboardText,
                clipboardSnapshotID: checkpoint.clipboardSnapshotID,
                pasteCheckpointID: checkpoint.checkpointID
              ) else { return nil }
        return ActiveTapAuthorshipDerivation(
            result: WriteAuthorshipResult(
                segments: completion.segments,
                resolution: "resolved"
            ),
            resolvedCompletion: completion.resolvedContent,
            stateContinuity: "segmented_at_grounded_paste",
            evidence: "grounded_paste_ax_epoch_transition"
        )
    }

    private func rangeSemanticCursorContext(
        target: HeldEditableTarget,
        before: ActiveTapEditableObservation
    ) -> ActiveTapRangeSemanticCursorContext? {
        guard let probe = before.axRangeCursorProbe,
              let left = probe.left?.text,
              let selected = probe.selected?.text else { return nil }
        let right: String
        let captureStatus: String
        if let capturedRight = probe.right?.text {
            right = capturedRight
            captureStatus = "complete"
        } else if probe.right?.axError == axErrorName(.noValue),
                  probe.right?.rangeLength == 1 {
            right = ""
            captureStatus = "right_provider_no_value_after_minimum_probe"
        } else {
            return nil
        }
        let surfacePrompt = unpopulatedSurfacePrompt(
            bundleIdentifier: target.app.bundleIdentifier,
            fieldDescription: target.identity.fieldDescription,
            value: before.value,
            placeholderValue: before.placeholderValue,
            valueRepresentedPlaceholder: before.valueRepresentedPlaceholder
        )
        return ActiveTapRangeSemanticCursorContext(
            schemaVersion: 2,
            source: "accessibility_string_for_range",
            captureStatus: captureStatus,
            fieldState: surfacePrompt == nil ? "editable_text" : "unpopulated_prompt",
            leftContext: surfacePrompt == nil ? left : "",
            selectedText: surfacePrompt == nil ? selected : "",
            rightContext: surfacePrompt == nil ? right : "",
            surfacePrompt: surfacePrompt
        )
    }

    private func cursorFidelityEvidence(
        for pending: PendingActiveTapWrite,
        terminalEditOffset: Int?
    ) -> ActiveTapCursorFidelityEvidence {
        guard let before = pending.before else {
            return ActiveTapCursorFidelityEvidence(
                status: CursorFidelityStatus.initialCursorUnavailable.rawValue,
                initialCursorOffsetCharacters: nil,
                initialSelectionLengthCharacters: nil,
                earliestObservedMutationOffsetCharacters: nil,
                earliestObservedMutationObservationID: nil,
                earliestObservedMutationCapturedAt: nil,
                terminalEditOffsetCharacters: terminalEditOffset
            )
        }
        let beforeValue = logicalEditableValue(
            before.value,
            placeholderValue: before.placeholderValue
        )
        let cursor = semanticCursorContext(
            in: beforeValue,
            selectionStartUTF16: before.selectedRangeLocation,
            selectionLengthUTF16: before.selectedRangeLength,
            surroundingCharacterCount: 1
        )
        var candidates = [ObservedMutationCandidate]()
        func appendCandidate(
            observation: ActiveTapEditableObservation?,
            errors: [String]
        ) {
            guard errors.isEmpty,
                  let observation,
                  !observation.valueWasTruncated else { return }
            let value = logicalEditableValue(
                observation.value,
                placeholderValue: observation.placeholderValue
            )
            let edit = minimalTextEdit(from: beforeValue, to: value)
            guard !edit.isEmpty else { return }
            candidates.append(ObservedMutationCandidate(
                observationID: observation.observationID,
                capturedAt: observation.observedAt,
                editOffset: edit.characterOffset
            ))
        }
        for checkpoint in pending.mutationCheckpoints {
            appendCandidate(observation: checkpoint.observation, errors: checkpoint.axErrors)
        }
        for checkpoint in pending.pasteCheckpoints {
            appendCandidate(observation: checkpoint.observation, errors: checkpoint.axErrors)
        }
        for checkpoint in pending.returnCheckpoints {
            appendCandidate(observation: checkpoint.observation, errors: checkpoint.axErrors)
        }
        let earliest = candidates.min {
            if $0.capturedAt != $1.capturedAt { return $0.capturedAt < $1.capturedAt }
            return $0.observationID < $1.observationID
        }
        let status = cursorFidelityStatus(
            initialCursorOffset: cursor?.selectionStartCharacters,
            earliestObservedMutationOffset: earliest?.editOffset,
            terminalEditOffset: terminalEditOffset
        )
        return ActiveTapCursorFidelityEvidence(
            status: status.rawValue,
            initialCursorOffsetCharacters: cursor?.selectionStartCharacters,
            initialSelectionLengthCharacters: cursor?.selectionLengthCharacters,
            earliestObservedMutationOffsetCharacters: earliest?.editOffset,
            earliestObservedMutationObservationID: earliest?.observationID,
            earliestObservedMutationCapturedAt: earliest?.capturedAt,
            terminalEditOffsetCharacters: terminalEditOffset
        )
    }

    private func deriveWrite(
        before: ActiveTapEditableObservation,
        after: TargetCapture,
        checkpoints: [ActiveTapReturnCheckpoint],
        pasteCheckpoints: [ActiveTapPasteCheckpoint],
        mutationCheckpoints: [ActiveTapMutationCheckpoint],
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
            let latestMutation: ObservationCheckpoint? = mutationCheckpoints.last.map {
                ObservationCheckpoint(
                    checkpointID: $0.checkpointID,
                    eventTimestampNanoseconds: $0.eventTimestampNanoseconds,
                    observationSource: "post_input_checkpoint",
                    observation: $0.observation,
                    errors: $0.axErrors
                )
            }
            let latest = [latestReturn, latestPaste, latestMutation]
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
            // A transient submit/paste surface may reset to BEFORE, but an
            // ordinary typed checkpoint which later returns to BEFORE is an
            // undone write, not settled output.
            guard latestCheckpoint.observationSource != "post_input_checkpoint" else {
                return .unresolved(
                    "no_change",
                    observationSource: "terminal_after",
                    usedObservationCapturedAt: terminal.observedAt
                )
            }
            return .checkpoint(latestCheckpoint, fallbackReason: "terminal_matches_before")
        }
        if let latestCheckpoint,
           terminalValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return .checkpoint(latestCheckpoint, fallbackReason: "terminal_unpopulated")
        }

        let edit = minimalTextEdit(from: beforeValue, to: terminalValue)
        guard !edit.isEmpty else {
            return .unresolved(
                "no_change",
                observationSource: "terminal_after",
                usedObservationCapturedAt: terminal.observedAt
            )
        }
        guard applying(edit, to: beforeValue) == terminalValue else {
            return .unresolved(
                "derivation_mismatch",
                observationSource: "terminal_after",
                usedObservationCapturedAt: terminal.observedAt
            )
        }
        return WriteDerivationDecision(
            edit: edit,
            resolution: "validated",
            observationSource: "terminal_after",
            fallbackReason: nil,
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
        guard !edit.isEmpty, applying(edit, to: beforeValue) == value else { return nil }
        return MeaningfulCheckpoint(
            checkpointID: checkpointID,
            eventTimestampNanoseconds: eventTimestampNanoseconds,
            observationSource: observationSource,
            observation: observation,
            edit: edit
        )
    }

    private func usedObservation(
        for decision: WriteDerivationDecision,
        pending: PendingActiveTapWrite,
        terminal: ActiveTapEditableObservation?
    ) -> ActiveTapEditableObservation? {
        guard let source = decision.observationSource else { return nil }
        switch source {
        case "terminal_after":
            return terminal
        case "pre_return_checkpoint":
            return pending.returnCheckpoints.first {
                $0.checkpointID == decision.usedCheckpointID
            }?.observation
        case "post_paste_checkpoint":
            return pending.pasteCheckpoints.first {
                $0.checkpointID == decision.usedCheckpointID
            }?.observation
        case "post_input_checkpoint":
            return pending.mutationCheckpoints.first {
                $0.checkpointID == decision.usedCheckpointID
            }?.observation
        default:
            return nil
        }
    }

    private func discardPendingForPause() {
        writeTimer?.invalidate()
        writeTimer = nil
        mutationCheckpointTimer?.invalidate()
        mutationCheckpointTimer = nil
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
        let prePaste = pending.target.map {
            captureHeldTarget($0, reason: "pre_paste_checkpoint")
        } ?? TargetCapture(errors: ["target:unavailable"])
        let clipboardValue = pasteboard.string(forType: .string)
        let clipped = clipboardValue.map {
            String($0.prefix(configuration.maxCharacters))
        }
        pending.pasteCheckpoints.append(
            ActiveTapPasteCheckpoint(
                checkpointID: checkpointID,
                clipboardSnapshotID: pending.conditioningClipboard.snapshotID,
                inputObservedAt: inputObservedAt,
                eventTimestampNanoseconds: event.timestamp,
                clipboardObservedAt: nowTimestamp(),
                clipboardChangeCount: pasteboard.changeCount,
                clipboardTypes: pasteboard.types?.map(\.rawValue).sorted() ?? [],
                clipboardText: clipped,
                clipboardTextWasTruncated: clipboardValue.map {
                    clipped!.count < $0.count
                } ?? false,
                prePasteObservation: prePaste.observation,
                prePasteAXErrors: prePaste.errors,
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
        if configuration.retainScreenshots, let target {
            Timer.scheduledTimer(
                withTimeInterval: configuration.postPasteCheckpointDelay,
                repeats: false
            ) { [weak self] _ in
                self?.capturePasteAuditScreenshot(
                    attemptID: attemptID,
                    checkpointID: checkpointID,
                    target: target
                )
            }
        }
    }

    private func capturePasteAuditScreenshot(
        attemptID: String,
        checkpointID: String,
        target: HeldEditableTarget
    ) {
        let recordID = UUID().uuidString
        let requestedAt = nowTimestamp()
        guard #available(macOS 15.2, *),
              let windowElement = target.windowElement,
              let bounds = axFrame(of: windowElement),
              bounds.width > 0,
              bounds.height > 0 else {
            emitPasteAuditScreenshot(
                RawPasteAuditScreenshot(
                    recordID: recordID,
                    observedAt: nowTimestamp(),
                    attemptID: attemptID,
                    pasteCheckpointID: checkpointID,
                    captureRequestedAt: requestedAt,
                    capturedAt: nil,
                    appName: target.app.name,
                    bundleIdentifier: target.app.bundleIdentifier,
                    processIdentifier: target.app.processIdentifier,
                    windowTitle: target.window.title,
                    windowBounds: nil,
                    screenshotRelativePath: nil,
                    screenshotSHA256: nil,
                    screenshotPixelWidth: nil,
                    screenshotPixelHeight: nil,
                    error: "window_bounds_unavailable"
                )
            )
            return
        }
        SCScreenshotManager.captureImage(in: bounds) { [weak self] image, error in
            guard let self else { return }
            let capturedAt = nowTimestamp()
            let retained = image.flatMap {
                try? self.persistPasteAuditScreenshot($0, recordID: recordID)
            }
            let record = RawPasteAuditScreenshot(
                recordID: recordID,
                observedAt: nowTimestamp(),
                attemptID: attemptID,
                pasteCheckpointID: checkpointID,
                captureRequestedAt: requestedAt,
                capturedAt: image == nil ? nil : capturedAt,
                appName: target.app.name,
                bundleIdentifier: target.app.bundleIdentifier,
                processIdentifier: target.app.processIdentifier,
                windowTitle: target.window.title,
                windowBounds: rectValue(bounds),
                screenshotRelativePath: retained?.relativePath,
                screenshotSHA256: retained?.sha256,
                screenshotPixelWidth: retained?.pixelWidth,
                screenshotPixelHeight: retained?.pixelHeight,
                error: retained == nil
                    ? error?.localizedDescription ?? "screenshot_persistence_failed"
                    : nil
            )
            DispatchQueue.main.async {
                self.emitPasteAuditScreenshot(record)
            }
        }
    }

    private func persistPasteAuditScreenshot(
        _ image: CGImage,
        recordID: String
    ) throws -> ActiveTapRetainedScreenshot {
        let directory = URL(fileURLWithPath: configuration.screenshotsDirectory)
        try FileManager.default.createDirectory(
            at: directory,
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: NSNumber(value: 0o700)]
        )
        let relativePath = "screenshots/\(recordID)-paste.png"
        let url = URL(fileURLWithPath: configuration.outputDirectory)
            .appendingPathComponent(relativePath)
        let representation = NSBitmapImageRep(cgImage: image)
        guard let data = representation.representation(using: .png, properties: [:]),
              FileManager.default.createFile(
                atPath: url.path,
                contents: data,
                attributes: [.posixPermissions: NSNumber(value: 0o600)]
              ) else {
            throw ActiveTapWriteCollectorError.screenshotPersistenceFailed
        }
        return ActiveTapRetainedScreenshot(
            relativePath: relativePath,
            sha256: SHA256.hash(data: data)
                .map { String(format: "%02x", $0) }
                .joined(),
            pixelWidth: image.width,
            pixelHeight: image.height
        )
    }

    private func emitPasteAuditScreenshot(_ record: RawPasteAuditScreenshot) {
        do {
            _ = try rawWriter.write(record)
        } catch {
            writeDiagnostic("could not persist paste audit screenshot: \(error)")
        }
    }

    private func clipboardSnapshot() -> ActiveTapClipboardSnapshot {
        let pasteboard = NSPasteboard.general
        let value = pasteboard.string(forType: .string)
        let clipped = value.map { String($0.prefix(configuration.maxCharacters)) }
        let hash = value.map {
            SHA256.hash(data: Data($0.utf8)).map { String(format: "%02x", $0) }.joined()
        }
        return ActiveTapClipboardSnapshot(
            snapshotID: UUID().uuidString,
            capturedAt: nowTimestamp(),
            changeCount: pasteboard.changeCount,
            types: pasteboard.types?.map(\.rawValue).sorted() ?? [],
            text: clipped,
            textSHA256: hash,
            textWasTruncated: value.map { clipped!.count < $0.count } ?? false
        )
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
              }),
              pending.pasteCheckpoints[index].observation == nil else {
            return
        }
        pending.pasteCheckpoints[index].postPasteCaptureRequestedAt = requestedAt
        pending.pasteCheckpoints[index].observation = capture.observation
        pending.pasteCheckpoints[index].axErrors = capture.errors
        self.pending = pending
    }

    private func completeOutstandingPasteCheckpointsSynchronously() {
        guard var pending,
              let target = pending.target else { return }
        let unresolved = pending.pasteCheckpoints.indices.filter {
            pending.pasteCheckpoints[$0].observation == nil
        }
        guard !unresolved.isEmpty else { return }
        for index in unresolved {
            let requestedAt = nowTimestamp()
            let capture = captureHeldTarget(target, reason: "post_paste_before_next_input")
            pending.pasteCheckpoints[index].postPasteCaptureRequestedAt = requestedAt
            pending.pasteCheckpoints[index].observation = capture.observation
            pending.pasteCheckpoints[index].axErrors = capture.errors
        }
        self.pending = pending
    }

    private func scheduleMutationCheckpoint(event: CGEvent, inputObservedAt: String) {
        guard let pending else { return }
        mutationCheckpointTimer?.invalidate()
        let attemptID = pending.attemptID
        let target = pending.target
        let eventTimestamp = event.timestamp
        mutationCheckpointTimer = Timer.scheduledTimer(
            withTimeInterval: configuration.postInputCheckpointDelay,
            repeats: false
        ) { [weak self] _ in
            guard let self else { return }
            let requestedAt = nowTimestamp()
            let capture = target.map {
                self.captureHeldTarget($0, reason: "post_input_checkpoint")
            } ?? TargetCapture(errors: ["target:unavailable"])
            self.completeMutationCheckpoint(
                attemptID: attemptID,
                checkpoint: ActiveTapMutationCheckpoint(
                    checkpointID: UUID().uuidString,
                    inputObservedAt: inputObservedAt,
                    eventTimestampNanoseconds: eventTimestamp,
                    captureRequestedAt: requestedAt,
                    observation: capture.observation,
                    axErrors: capture.errors
                )
            )
        }
    }

    private func completeMutationCheckpoint(
        attemptID: String,
        checkpoint: ActiveTapMutationCheckpoint
    ) {
        guard var pending, pending.attemptID == attemptID else { return }
        pending.mutationCheckpoints.append(checkpoint)
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
            subrole: subrole,
            accessibilityIdentifier: stringAttribute(
                focusedElement,
                kAXIdentifierAttribute,
                errors: &errors
            ),
            fieldLabel: stringAttribute(
                focusedElement,
                kAXTitleAttribute,
                errors: &errors
            ),
            fieldDescription: stringAttribute(
                focusedElement,
                kAXDescriptionAttribute,
                errors: &errors
            )
        )
        let target = HeldEditableTarget(
            element: focusedElement,
            applicationElement: applicationElement,
            windowElement: windowElement,
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
        let axRangeProbe = reason == "write_before"
            ? captureAXRangeCursorProbe(
                target.element,
                selection: selection,
                surroundingCharacterCount: configuration.cursorContextCharacters,
                maximumRetainedCharacters: configuration.maxCharacters
            )
            : nil
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
            valueWasTruncated: clipped.count < value.count,
            axRangeCursorProbe: axRangeProbe
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
    var inputEvents: [ActiveTapInputEvidence]
    var returnCheckpoints: [ActiveTapReturnCheckpoint]
    var pasteCheckpoints: [ActiveTapPasteCheckpoint]
    var mutationCheckpoints: [ActiveTapMutationCheckpoint]
    let conditioningClipboard: ActiveTapClipboardSnapshot
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
    let windowElement: AXUIElement?
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
    let subrole: String?
    let accessibilityIdentifier: String?
    let fieldLabel: String?
    let fieldDescription: String?
}

private struct ActiveTapWriteDestination: Encodable {
    let appName: String
    let bundleIdentifier: String?
    let processIdentifier: Int32
    let windowTitle: String?
    let resource: String?
    let role: String
    let subrole: String?
    let fieldIdentifier: String?
    let fieldLabel: String?
    let fieldDescription: String?
    let placeholder: String?
}

private struct ActiveTapWriteConditioningState: Encodable {
    let schemaVersion: Int
    let captureSemantics: String
    let inputInterceptedAt: String
    let capturedAt: String
    let destination: ActiveTapWriteDestination
    let cursorContext: ActiveTapRangeSemanticCursorContext?
    let clipboard: ActiveTapClipboardSnapshot
    let sourceObservationID: String
}

private struct ActiveTapClipboardSnapshot: Encodable {
    let schemaVersion = 1
    let snapshotID: String
    let capturedAt: String
    let changeCount: Int
    let types: [String]
    let text: String?
    let textSHA256: String?
    let textWasTruncated: Bool
}

private struct ActiveTapRangeSemanticCursorContext: Encodable {
    let schemaVersion: Int
    let source: String
    let captureStatus: String
    let fieldState: String
    let leftContext: String
    let selectedText: String
    let rightContext: String
    let surfacePrompt: String?
}

private struct ActiveTapWriteOutcome: Encodable {
    let operation: String
    let content: String
    let removedContent: String
    let characterOffset: Int

    init(
        operation: String,
        content: String,
        removedContent: String,
        characterOffset: Int
    ) {
        self.operation = operation
        self.content = content
        self.removedContent = removedContent
        self.characterOffset = characterOffset
    }

    init(_ edit: TextEdit) {
        self.init(
            operation: edit.operation.rawValue,
            content: edit.inserted,
            removedContent: edit.removed,
            characterOffset: edit.characterOffset
        )
    }
}

private struct ActiveTapCursorFidelityEvidence: Encodable {
    let schemaVersion = 1
    let status: String
    let initialCursorOffsetCharacters: Int?
    let initialSelectionLengthCharacters: Int?
    let earliestObservedMutationOffsetCharacters: Int?
    let earliestObservedMutationObservationID: String?
    let earliestObservedMutationCapturedAt: String?
    let terminalEditOffsetCharacters: Int?
}

private struct ObservedMutationCandidate {
    let observationID: String
    let capturedAt: String
    let editOffset: Int
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
    let axRangeCursorProbe: ActiveTapAXRangeCursorProbe?
}

private struct ActiveTapAXRangeCursorProbe: Encodable {
    let schemaVersion = 1
    let capturedAt: String
    let durationMilliseconds: Double
    let requestedSurroundingCharacterCount: Int
    let selectedRangeLocation: Int?
    let selectedRangeLength: Int?
    let numberOfCharacters: Int?
    let left: ActiveTapAXRangeTextQuery?
    let selected: ActiveTapAXRangeTextQuery?
    let right: ActiveTapAXRangeTextQuery?
    let errors: [String]
}

private struct ActiveTapAXRangeTextQuery: Encodable {
    let rangeLocation: Int
    let rangeLength: Int
    let text: String?
    let textWasTruncated: Bool
    let axError: String?
}

private struct AXRangeProbePlan {
    let left: CFRange
    let selected: CFRange
    let right: CFRange
}

private struct ActiveTapInputEvidence: Encodable {
    let observedAt: String
    let eventTimestampNanoseconds: UInt64
    let hint: String
    let mutationCapable: Bool
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
    let clipboardSnapshotID: String
    let inputObservedAt: String
    let eventTimestampNanoseconds: UInt64
    let clipboardObservedAt: String
    let clipboardChangeCount: Int
    let clipboardTypes: [String]
    let clipboardText: String?
    let clipboardTextWasTruncated: Bool
    let prePasteObservation: ActiveTapEditableObservation?
    let prePasteAXErrors: [String]
    var postPasteCaptureRequestedAt: String?
    var observation: ActiveTapEditableObservation?
    var axErrors: [String]
}

private struct ActiveTapRetainedScreenshot {
    let relativePath: String
    let sha256: String
    let pixelWidth: Int
    let pixelHeight: Int
}

private struct RawPasteAuditScreenshot: Encodable {
    let schemaVersion = 1
    let recordType = "paste_audit_screenshot"
    let recordID: String
    let observedAt: String
    let attemptID: String
    let pasteCheckpointID: String
    let captureRequestedAt: String
    let capturedAt: String?
    let appName: String
    let bundleIdentifier: String?
    let processIdentifier: Int32
    let windowTitle: String?
    let windowBounds: RectValue?
    let screenshotRelativePath: String?
    let screenshotSHA256: String?
    let screenshotPixelWidth: Int?
    let screenshotPixelHeight: Int?
    let error: String?
}

private struct ActiveTapAuthorshipDerivation {
    let result: WriteAuthorshipResult
    let resolvedCompletion: String
    let stateContinuity: String
    let evidence: String?
}

private struct ActiveTapMutationCheckpoint: Encodable {
    let checkpointID: String
    let inputObservedAt: String
    let eventTimestampNanoseconds: UInt64
    let captureRequestedAt: String
    let observation: ActiveTapEditableObservation?
    let axErrors: [String]
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

private struct RawActiveTapWriteEvidence: Encodable {
    let schemaVersion = 15
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
    let inputEvents: [ActiveTapInputEvidence]
    let boundaryReason: String
    let conditioningState: ActiveTapWriteConditioningState?
    let targetIdentity: ActiveTapTargetIdentity?
    let before: ActiveTapEditableObservation?
    let after: ActiveTapEditableObservation?
    let returnCheckpoints: [ActiveTapReturnCheckpoint]
    let pasteCheckpoints: [ActiveTapPasteCheckpoint]
    let mutationCheckpoints: [ActiveTapMutationCheckpoint]
    let beforeAXErrors: [String]
    let afterAXErrors: [String]
    let beforeCaptureDurationMilliseconds: Double
    let firstCallbackDurationMilliseconds: Double?
    let maximumCallbackDurationMilliseconds: Double
    let tapTimeoutCountDuringBurst: UInt64
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
    let schemaVersion = 11
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
    let conditioningState: ActiveTapWriteConditioningState
    let cursorFidelity: ActiveTapCursorFidelityEvidence
    let authorshipResolution: String
    let authorshipEvidence: String?
    let authorshipSegments: [WriteAuthorshipSegment]
    let resolvedCompletion: String
    let stateContinuity: String
    let observedNetEdit: ActiveTapWriteOutcome
    let outcome: ActiveTapWriteOutcome
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
    let isSelectionBoundary: Bool

    init(
        canStartWrite: Bool,
        hint: String,
        isUnmodifiedReturn: Bool = false,
        isPaste: Bool = false,
        isSelectionBoundary: Bool = false
    ) {
        self.canStartWrite = canStartWrite
        self.hint = hint
        self.isUnmodifiedReturn = isUnmodifiedReturn
        self.isPaste = isPaste
        self.isSelectionBoundary = isSelectionBoundary
    }
}

private func classifyKey(_ event: CGEvent) -> KeyClassification {
    let code = event.getIntegerValueField(.keyboardEventKeycode)
    let commandPressed = event.flags.contains(.maskCommand)
    if isWriteSelectionBoundaryKey(
        keyCode: code,
        commandPressed: commandPressed
    ) {
        return KeyClassification(
            canStartWrite: false,
            hint: commandPressed && code == 0 ? "select_all" : "selection_navigation",
            isSelectionBoundary: true
        )
    }
    if commandPressed {
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

/// Queries cursor-adjacent text using the Accessibility provider's own range
/// coordinate space. All failures stay inside this raw diagnostic record and
/// never enter the write capture's AX errors or derivation path.
private func captureAXRangeCursorProbe(
    _ element: AXUIElement,
    selection: CFRange?,
    surroundingCharacterCount: Int,
    maximumRetainedCharacters: Int
) -> ActiveTapAXRangeCursorProbe {
    let started = DispatchTime.now().uptimeNanoseconds
    var errors = [String]()

    var numberValue: CFTypeRef?
    let numberError = AXUIElementCopyAttributeValue(
        element,
        kAXNumberOfCharactersAttribute as CFString,
        &numberValue
    )
    let numberOfCharacters = (numberValue as? NSNumber)?.intValue
    if numberError != .success,
       numberError != .noValue,
       numberError != .attributeUnsupported {
        errors.append("AXNumberOfCharacters:\(axErrorName(numberError))")
    }

    let plan = axRangeProbePlan(
        selection: selection,
        numberOfCharacters: numberOfCharacters,
        surroundingCharacterCount: surroundingCharacterCount
    )
    if selection != nil, plan == nil {
        errors.append("range_plan:selection_outside_provider_character_count")
    } else if selection == nil {
        errors.append("range_plan:selection_unavailable")
    }

    let left = plan.map {
        axRangeTextQuery(
            element,
            range: $0.left,
            maximumRetainedCharacters: maximumRetainedCharacters,
            retainSuffix: true
        )
    }
    let selected = plan.map {
        axRangeTextQuery(
            element,
            range: $0.selected,
            maximumRetainedCharacters: maximumRetainedCharacters,
            retainSuffix: false
        )
    }
    let right = plan.map {
        axRightRangeTextQuery(
            element,
            range: $0.right,
            maximumRetainedCharacters: maximumRetainedCharacters
        )
    }

    return ActiveTapAXRangeCursorProbe(
        capturedAt: nowTimestamp(),
        durationMilliseconds: milliseconds(sinceNanoseconds: started),
        requestedSurroundingCharacterCount: surroundingCharacterCount,
        selectedRangeLocation: selection?.location,
        selectedRangeLength: selection?.length,
        numberOfCharacters: numberOfCharacters,
        left: left,
        selected: selected,
        right: right,
        errors: errors
    )
}

/// Some Chromium/Electron editors report AXNumberOfCharacters in a different
/// coordinate space from AXSelectedTextRange. Keep the start anchored to the
/// provider-native selection and retry a bounded shorter suffix when the
/// advertised right extent is rejected.
private func axRightRangeTextQuery(
    _ element: AXUIElement,
    range: CFRange,
    maximumRetainedCharacters: Int
) -> ActiveTapAXRangeTextQuery {
    var length = range.length
    var last = axRangeTextQuery(
        element,
        range: range,
        maximumRetainedCharacters: maximumRetainedCharacters,
        retainSuffix: false
    )
    while last.text == nil,
          last.axError == axErrorName(.noValue),
          length > 1 {
        length = max(1, length / 2)
        last = axRangeTextQuery(
            element,
            range: CFRange(location: range.location, length: length),
            maximumRetainedCharacters: maximumRetainedCharacters,
            retainSuffix: false
        )
    }
    return last
}

private func axRangeProbePlan(
    selection: CFRange?,
    numberOfCharacters: Int?,
    surroundingCharacterCount: Int
) -> AXRangeProbePlan? {
    guard let selection,
          selection.location >= 0,
          selection.length >= 0,
          surroundingCharacterCount > 0 else { return nil }
    let (selectionEnd, overflowed) = selection.location.addingReportingOverflow(
        selection.length
    )
    guard !overflowed else { return nil }
    if let numberOfCharacters {
        guard numberOfCharacters >= selectionEnd else { return nil }
    }
    let leftLocation = max(0, selection.location - surroundingCharacterCount)
    let rightLength = numberOfCharacters.map {
        min(surroundingCharacterCount, $0 - selectionEnd)
    } ?? surroundingCharacterCount
    return AXRangeProbePlan(
        left: CFRange(
            location: leftLocation,
            length: selection.location - leftLocation
        ),
        selected: selection,
        right: CFRange(location: selectionEnd, length: rightLength)
    )
}

private func axRangeTextQuery(
    _ element: AXUIElement,
    range: CFRange,
    maximumRetainedCharacters: Int,
    retainSuffix: Bool
) -> ActiveTapAXRangeTextQuery {
    if range.length == 0 {
        return ActiveTapAXRangeTextQuery(
            rangeLocation: range.location,
            rangeLength: 0,
            text: "",
            textWasTruncated: false,
            axError: nil
        )
    }
    var cfRange = CFRange(location: range.location, length: range.length)
    guard let rangeValue = AXValueCreate(.cfRange, &cfRange) else {
        return ActiveTapAXRangeTextQuery(
            rangeLocation: range.location,
            rangeLength: range.length,
            text: nil,
            textWasTruncated: false,
            axError: "could_not_create_range"
        )
    }
    var value: CFTypeRef?
    let error = AXUIElementCopyParameterizedAttributeValue(
        element,
        kAXStringForRangeParameterizedAttribute as CFString,
        rangeValue,
        &value
    )
    guard error == .success, let text = value as? String else {
        return ActiveTapAXRangeTextQuery(
            rangeLocation: range.location,
            rangeLength: range.length,
            text: nil,
            textWasTruncated: false,
            axError: axErrorName(error)
        )
    }
    let clipped = retainSuffix
        ? String(text.suffix(maximumRetainedCharacters))
        : String(text.prefix(maximumRetainedCharacters))
    return ActiveTapAXRangeTextQuery(
        rangeLocation: range.location,
        rangeLength: range.length,
        text: clipped,
        textWasTruncated: clipped.count < text.count,
        axError: nil
    )
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

private func axFrame(of element: AXUIElement) -> CGRect? {
    var positionReference: CFTypeRef?
    var sizeReference: CFTypeRef?
    guard AXUIElementCopyAttributeValue(
        element,
        kAXPositionAttribute as CFString,
        &positionReference
    ) == .success,
    AXUIElementCopyAttributeValue(
        element,
        kAXSizeAttribute as CFString,
        &sizeReference
    ) == .success,
    let positionReference,
    let sizeReference,
    CFGetTypeID(positionReference) == AXValueGetTypeID(),
    CFGetTypeID(sizeReference) == AXValueGetTypeID() else { return nil }
    let positionValue = unsafeBitCast(positionReference, to: AXValue.self)
    let sizeValue = unsafeBitCast(sizeReference, to: AXValue.self)
    var point = CGPoint.zero
    var size = CGSize.zero
    guard AXValueGetValue(positionValue, .cgPoint, &point),
          AXValueGetValue(sizeValue, .cgSize, &size) else { return nil }
    return CGRect(origin: point, size: size)
}

enum ActiveTapWriteCollectorError: Error, CustomStringConvertible {
    case eventTapUnavailable
    case screenshotPersistenceFailed

    var description: String {
        switch self {
        case .eventTapUnavailable:
            return "could not create active write event tap; grant Accessibility and Input Monitoring, then try again"
        case .screenshotPersistenceFailed:
            return "could not retain paste audit screenshot"
        }
    }
}
