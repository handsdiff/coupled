import ApplicationServices
import CoupledCore
import Foundation

final class Collector {
    private let configuration: Configuration
    private let snapshotter: AccessibilitySnapshotter
    private let rawWriter: JSONLWriter
    private let eventWriter: JSONLWriter

    private var eventTap: CFMachPort?
    private var eventTapSource: CFRunLoopSource?
    private var readTimer: Timer?
    private var writeTimer: Timer?
    private var pollTimer: Timer?

    private var pendingRead: PendingRead?
    private var lastContextIdentifier: String?
    private var lastReadTextByContext: [String: String] = [:]

    private var writeStart: EditableObservation?
    private var latestPreKey: EditableObservation?
    private var writeHints = Set<String>()
    private var lastKeyWasBoundary = false

    init(configuration: Configuration) throws {
        self.configuration = configuration
        snapshotter = AccessibilitySnapshotter(configuration: configuration)
        rawWriter = try JSONLWriter(
            path: configuration.rawPath,
            sessionID: configuration.sessionID
        )
        eventWriter = try JSONLWriter(
            path: configuration.eventsPath,
            sessionID: configuration.sessionID
        )
    }

    func run() throws {
        guard installEventTap() else {
            throw CollectorError.eventTapUnavailable
        }

        writeDiagnostic("raw observations: \(configuration.rawPath)")
        writeDiagnostic("understood events: \(configuration.eventsPath)")
        writeDiagnostic("event JSONL is also emitted on stdout; press Ctrl-C to stop")
        if let pauseFile = configuration.pauseFile {
            writeDiagnostic("collection pauses while this file exists: \(pauseFile)")
        }

        lastContextIdentifier = snapshotter.captureContextIdentifier()
        scheduleRead(reason: "collector_started")
        processPendingRead()

        pollTimer = Timer.scheduledTimer(withTimeInterval: configuration.pollInterval, repeats: true) {
            [weak self] _ in self?.pollFocus()
        }

        RunLoop.current.run()
    }

    func captureOneSnapshot() {
        guard !configuration.isPaused(),
              let snapshot = snapshotter.capture(reason: "manual_snapshot") else {
            writeDiagnostic("no eligible focused window was available")
            return
        }
        _ = try? rawWriter.write(RawSnapshotRecord(snapshot: snapshot))
        emitRead(from: snapshot, reasons: ["manual_snapshot"])
    }

    private func installEventTap() -> Bool {
        let interestedTypes: [CGEventType] = [
            .keyDown,
            .leftMouseDown,
            .rightMouseDown,
            .otherMouseDown,
            .mouseMoved,
            .leftMouseDragged,
            .rightMouseDragged,
            .otherMouseDragged,
            .scrollWheel,
        ]
        let mask = interestedTypes.reduce(CGEventMask(0)) {
            $0 | (CGEventMask(1) << CGEventMask($1.rawValue))
        }

        let callback: CGEventTapCallBack = { _, type, event, userInfo in
            guard let userInfo else { return Unmanaged.passUnretained(event) }
            let collector = Unmanaged<Collector>.fromOpaque(userInfo).takeUnretainedValue()

            if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
                if let tap = collector.eventTap {
                    CGEvent.tapEnable(tap: tap, enable: true)
                }
                return Unmanaged.passUnretained(event)
            }

            collector.handle(type: type, event: event)
            return Unmanaged.passUnretained(event)
        }

        let userInfo = Unmanaged.passUnretained(self).toOpaque()
        guard let tap = CGEvent.tapCreate(
            tap: .cgSessionEventTap,
            place: .headInsertEventTap,
            options: .listenOnly,
            eventsOfInterest: mask,
            callback: callback,
            userInfo: userInfo
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

    private func handle(type: CGEventType, event: CGEvent) {
        guard !configuration.isPaused() else {
            resetPendingActivity()
            return
        }

        switch type {
        case .keyDown:
            handleKeyDown(event)
        case .scrollWheel:
            scheduleRead(reason: "scroll")
        case .mouseMoved, .leftMouseDragged, .rightMouseDragged, .otherMouseDragged:
            scheduleRead(reason: "pointer_settled")
        case .leftMouseDown, .rightMouseDown, .otherMouseDown:
            finalizeWriteIfPossible(reason: "pointer_boundary")
            scheduleRead(reason: "click")
        default:
            break
        }
    }

    private func handleKeyDown(_ event: CGEvent) {
        let keyCode = event.getIntegerValueField(.keyboardEventKeycode)
        let flags = event.flags
        let classification = classifyKey(code: keyCode, flags: flags)

        guard classification.isMutation,
              let beforeKey = snapshotter.captureEditable(reason: "before_key_activity") else {
            scheduleRead(reason: "keyboard_navigation")
            return
        }

        if writeStart == nil || writeStart?.editable.identifier != beforeKey.editable.identifier {
            finalizeWriteIfPossible(reason: "editable_changed")
            writeStart = beforeKey
            _ = try? rawWriter.write(RawEditableRecord(observation: beforeKey))
        }

        latestPreKey = beforeKey
        writeHints.insert(classification.hint)
        lastKeyWasBoundary = classification.isBoundary
        writeTimer?.invalidate()
        writeTimer = Timer.scheduledTimer(withTimeInterval: configuration.writeDelay, repeats: false) {
            [weak self] _ in self?.finalizeWriteIfPossible(reason: "write_delay_elapsed")
        }
    }

    private func finalizeWriteIfPossible(reason: String) {
        writeTimer?.invalidate()
        writeTimer = nil
        guard let start = writeStart else { return }

        let after = snapshotter.captureEditable(reason: reason)
        if let after {
            _ = try? rawWriter.write(RawEditableRecord(observation: after))
        }

        let terminal: EditableObservation?
        let usedFallback: Bool
        if let after, after.editable.identifier == start.editable.identifier {
            let directEdit = minimalTextEdit(from: start.editable.value, to: after.editable.value)
            if lastKeyWasBoundary,
               after.editable.value.isEmpty,
               let latestPreKey,
               !minimalTextEdit(from: start.editable.value, to: latestPreKey.editable.value).isEmpty {
                terminal = latestPreKey
                usedFallback = true
            } else if !directEdit.isEmpty {
                terminal = after
                usedFallback = false
            } else {
                terminal = latestPreKey
                usedFallback = terminal?.editable.value != start.editable.value
            }
        } else {
            terminal = latestPreKey
            usedFallback = true
        }

        if let terminal {
            if terminal.observationID != after?.observationID,
               terminal.observationID != start.observationID {
                _ = try? rawWriter.write(RawEditableRecord(observation: terminal))
            }
            let edit = minimalTextEdit(
                from: start.editable.value,
                to: terminal.editable.value,
                preferredOffset: start.editable.selectedRangeLocation
            )
            if !edit.isEmpty {
                let provenance = writeProvenance()
                let event = UnderstoodEvent(
                    eventID: UUID().uuidString,
                    observedAt: terminal.observedAt,
                    kind: "write",
                    app: start.app,
                    window: start.window,
                    content: edit.inserted,
                    newlyVisibleContent: nil,
                    edit: edit,
                    provenance: provenance,
                    sourceRecordIDs: [start.observationID, terminal.observationID],
                    metadata: [
                        "boundary_reason": reason,
                        "editable_role": start.editable.role,
                        "terminal_fallback": String(usedFallback),
                        "value_truncated": String(start.editable.valueWasTruncated || terminal.editable.valueWasTruncated),
                    ]
                )
                emit(event)
            }
        }

        writeStart = nil
        latestPreKey = nil
        writeHints.removeAll()
        lastKeyWasBoundary = false

        if configuration.readOnWrite {
            scheduleRead(reason: "write_completed")
        }
    }

    private func scheduleRead(reason: String) {
        let triggeredAt = nowTimestamp()
        let triggerContext: ReadTriggerContext?
        if reason == "pointer_settled",
           let pendingRead,
           snapshotter.frontmostProcessIdentifier() == pendingRead.context.app.processIdentifier {
            // Avoid querying the Accessibility tree for every mouse-move event.
            // Clicks, scrolls, and the focus poll still refresh the full context.
            triggerContext = pendingRead.context
        } else {
            triggerContext = snapshotter.captureReadTriggerContext()
        }

        guard let triggerContext else {
            if let pendingRead {
                writeActivity(
                    pendingRead,
                    settledAt: triggeredAt,
                    settledContextIdentifier: nil,
                    resolution: "trigger_context_unavailable",
                    snapshotID: nil
                )
                self.pendingRead = nil
            }
            readTimer?.invalidate()
            readTimer = nil
            return
        }

        if let pendingRead,
           pendingRead.context.contextIdentifier != triggerContext.contextIdentifier {
            writeActivity(
                pendingRead,
                settledAt: triggeredAt,
                settledContextIdentifier: triggerContext.contextIdentifier,
                resolution: "context_changed_before_settle",
                snapshotID: nil
            )
            self.pendingRead = nil
        }

        if pendingRead == nil {
            pendingRead = PendingRead(
                context: triggerContext,
                firstTriggeredAt: triggeredAt,
                lastTriggeredAt: triggeredAt,
                reasons: [reason],
                activityCount: 1
            )
        } else {
            pendingRead?.lastTriggeredAt = triggeredAt
            pendingRead?.reasons.insert(reason)
            pendingRead?.activityCount += 1
        }

        readTimer?.invalidate()
        readTimer = Timer.scheduledTimer(withTimeInterval: configuration.readDelay, repeats: false) {
            [weak self] _ in self?.processPendingRead()
        }
    }

    private func processPendingRead() {
        readTimer?.invalidate()
        readTimer = nil
        guard !configuration.isPaused(), let pendingRead else { return }
        self.pendingRead = nil

        let settledAt = nowTimestamp()
        let settledContext = snapshotter.captureReadTriggerContext()
        guard settledContext?.contextIdentifier == pendingRead.context.contextIdentifier else {
            writeActivity(
                pendingRead,
                settledAt: settledAt,
                settledContextIdentifier: settledContext?.contextIdentifier,
                resolution: "context_changed_before_settle",
                snapshotID: nil
            )
            return
        }

        let reasons = pendingRead.reasons.sorted()
        guard let snapshot = snapshotter.capture(reason: reasons.joined(separator: ",")) else {
            writeActivity(
                pendingRead,
                settledAt: settledAt,
                settledContextIdentifier: settledContext?.contextIdentifier,
                resolution: "snapshot_unavailable",
                snapshotID: nil
            )
            return
        }
        guard snapshot.contextIdentifier == pendingRead.context.contextIdentifier else {
            writeActivity(
                pendingRead,
                settledAt: settledAt,
                settledContextIdentifier: snapshot.contextIdentifier,
                resolution: "context_changed_during_snapshot",
                snapshotID: nil
            )
            return
        }

        let activityRecordID = UUID().uuidString
        writeActivity(
            pendingRead,
            recordID: activityRecordID,
            settledAt: settledAt,
            settledContextIdentifier: snapshot.contextIdentifier,
            resolution: "snapshot_captured",
            snapshotID: snapshot.snapshotID
        )
        _ = try? rawWriter.write(RawSnapshotRecord(snapshot: snapshot))
        emitRead(from: snapshot, reasons: reasons, sourceActivityRecordID: activityRecordID)
    }

    private func writeActivity(
        _ pendingRead: PendingRead,
        recordID: String = UUID().uuidString,
        settledAt: String,
        settledContextIdentifier: String?,
        resolution: String,
        snapshotID: String?
    ) {
        let activity = RawActivityRecord(
            recordID: recordID,
            observedAt: pendingRead.firstTriggeredAt,
            lastObservedAt: pendingRead.lastTriggeredAt,
            settledAt: settledAt,
            activityTypes: pendingRead.reasons.sorted(),
            eventCount: pendingRead.activityCount,
            triggerContext: pendingRead.context,
            settledContextIdentifier: settledContextIdentifier,
            resolution: resolution,
            snapshotID: snapshotID
        )
        _ = try? rawWriter.write(activity)
    }

    private func emitRead(
        from snapshot: AccessibilitySnapshot,
        reasons: [String],
        sourceActivityRecordID: String? = nil
    ) {
        guard !snapshot.visibleText.isEmpty else { return }
        let previous = lastReadTextByContext[snapshot.contextIdentifier]
        guard previous != snapshot.visibleText else { return }
        let novel = newlyVisibleLines(previous: previous, current: snapshot.visibleText)
        lastReadTextByContext[snapshot.contextIdentifier] = snapshot.visibleText

        var sourceRecordIDs = [snapshot.snapshotID]
        if let sourceActivityRecordID {
            sourceRecordIDs.insert(sourceActivityRecordID, at: 0)
        }

        let event = UnderstoodEvent(
            eventID: UUID().uuidString,
            observedAt: snapshot.observedAt,
            kind: "read",
            app: snapshot.app,
            window: snapshot.window,
            content: snapshot.visibleText,
            newlyVisibleContent: novel,
            edit: nil,
            provenance: "accessibility_visible_text",
            sourceRecordIDs: sourceRecordIDs,
            metadata: [
                "trigger_reasons": reasons.joined(separator: ","),
                "focused_role": snapshot.focusedRole ?? "",
                "visited_nodes": String(snapshot.visitedNodeCount),
                "node_limit_reached": String(snapshot.hitNodeLimit),
                "character_limit_reached": String(snapshot.hitCharacterLimit),
            ]
        )
        emit(event)
    }

    private func emit(_ event: UnderstoodEvent) {
        if let data = try? eventWriter.write(event) {
            writeLineToStandardOutput(data)
        }
    }

    private func pollFocus() {
        guard !configuration.isPaused() else {
            resetPendingActivity()
            return
        }
        let current = snapshotter.captureContextIdentifier()
        if current != lastContextIdentifier {
            finalizeWriteIfPossible(reason: "focus_changed")
            lastContextIdentifier = current
            scheduleRead(reason: "focus_changed")
        }
    }

    private func resetPendingActivity() {
        readTimer?.invalidate()
        writeTimer?.invalidate()
        pendingRead = nil
        writeStart = nil
        latestPreKey = nil
        writeHints.removeAll()
    }

    private func writeProvenance() -> String {
        if writeHints == ["paste"] { return "pasted" }
        if writeHints.contains("paste") { return "mixed" }
        if writeHints == ["cut"] { return "cut" }
        if writeHints.contains("undo_redo") { return "transformed" }
        return "typed"
    }
}

private struct PendingRead {
    let context: ReadTriggerContext
    let firstTriggeredAt: String
    var lastTriggeredAt: String
    var reasons: Set<String>
    var activityCount: Int
}

private struct KeyClassification {
    let isMutation: Bool
    let hint: String
    let isBoundary: Bool
}

private func classifyKey(code: Int64, flags: CGEventFlags) -> KeyClassification {
    let command = flags.contains(.maskCommand)
    let control = flags.contains(.maskControl)
    let shift = flags.contains(.maskShift)

    if command {
        switch code {
        case 9: return KeyClassification(isMutation: true, hint: "paste", isBoundary: false) // V
        case 7: return KeyClassification(isMutation: true, hint: "cut", isBoundary: false) // X
        case 6: return KeyClassification(isMutation: true, hint: "undo_redo", isBoundary: false) // Z
        default: return KeyClassification(isMutation: false, hint: "shortcut", isBoundary: false)
        }
    }

    if control {
        return KeyClassification(isMutation: false, hint: "control_shortcut", isBoundary: false)
    }

    let navigationCodes: Set<Int64> = [48, 53, 115, 116, 119, 121, 123, 124, 125, 126]
    if navigationCodes.contains(code) {
        return KeyClassification(isMutation: false, hint: "navigation", isBoundary: code == 48)
    }

    let boundary = (code == 36 || code == 76) && !shift
    return KeyClassification(isMutation: true, hint: "typed", isBoundary: boundary)
}

enum CollectorError: Error, CustomStringConvertible {
    case eventTapUnavailable

    var description: String {
        switch self {
        case .eventTapUnavailable:
            return "could not create a global input event tap; grant Input Monitoring permission and try again"
        }
    }
}
