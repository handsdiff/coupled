import AppKit
import ApplicationServices
import Foundation

/// A deliberately small sensor: CGEventTap in, one JSONL record out.
/// It performs no Accessibility queries and makes no read/write inference.
final class TriggerCollector {
    private let configuration: Configuration
    private let writer: JSONLWriter
    private var eventTap: CFMachPort?
    private var eventTapSource: CFRunLoopSource?
    private var sequence: UInt64 = 0

    init(configuration: Configuration) throws {
        self.configuration = configuration
        writer = try JSONLWriter(
            path: configuration.triggersPath,
            sessionID: configuration.sessionID
        )
    }

    func run() throws {
        guard installEventTap() else { throw TriggerCollectorError.eventTapUnavailable }

        writeDiagnostic("raw triggers: \(configuration.triggersPath)")
        writeDiagnostic("one JSON object is written for every observed input event")
        writeDiagnostic("typed characters and raw key codes are not recorded")
        if let pauseFile = configuration.pauseFile {
            writeDiagnostic("collection pauses while this file exists: \(pauseFile)")
        }
        RunLoop.current.run()
    }

    private func installEventTap() -> Bool {
        let types: [CGEventType] = [
            .keyDown, .keyUp, .flagsChanged,
            .mouseMoved,
            .leftMouseDown, .leftMouseUp,
            .rightMouseDown, .rightMouseUp,
            .otherMouseDown, .otherMouseUp,
            .leftMouseDragged, .rightMouseDragged, .otherMouseDragged,
            .scrollWheel,
        ]
        let mask = types.reduce(CGEventMask(0)) {
            $0 | (CGEventMask(1) << CGEventMask($1.rawValue))
        }
        let callback: CGEventTapCallBack = { _, type, event, userInfo in
            guard let userInfo else { return Unmanaged.passUnretained(event) }
            let collector = Unmanaged<TriggerCollector>
                .fromOpaque(userInfo)
                .takeUnretainedValue()

            if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
                if let tap = collector.eventTap {
                    CGEvent.tapEnable(tap: tap, enable: true)
                }
                return Unmanaged.passUnretained(event)
            }

            collector.record(type: type, event: event)
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

    private func record(type: CGEventType, event: CGEvent) {
        guard !configuration.isPaused(), let kind = triggerKind(type) else { return }

        let location = event.location
        let display = displayContext(at: location)
        let app = NSWorkspace.shared.frontmostApplication
        let appName = app?.localizedName ?? app?.bundleIdentifier ?? "Unknown"
        guard configuration.captures(
            bundleIdentifier: app?.bundleIdentifier,
            appName: appName
        ) else { return }
        sequence += 1
        let isKeyboard = type == .keyDown || type == .keyUp || type == .flagsChanged
        let isMouse = kind.hasPrefix("mouse_")
        let isScroll = type == .scrollWheel

        let record = TriggerRecord(
            sequence: sequence,
            observedAt: nowTimestamp(),
            eventTimestampNanoseconds: event.timestamp,
            kind: kind,
            x: (isMouse || isScroll) ? location.x : nil,
            y: (isMouse || isScroll) ? location.y : nil,
            displayID: display?.id,
            displayBounds: display?.bounds,
            deltaX: isMouse ? event.getDoubleValueField(.mouseEventDeltaX) : nil,
            deltaY: isMouse ? event.getDoubleValueField(.mouseEventDeltaY) : nil,
            mouseButton: (kind.contains("button") || kind.contains("dragged"))
                ? event.getIntegerValueField(.mouseEventButtonNumber) : nil,
            clickCount: kind.contains("button")
                ? event.getIntegerValueField(.mouseEventClickState) : nil,
            scrollDeltaX: isScroll
                ? event.getIntegerValueField(.scrollWheelEventPointDeltaAxis2) : nil,
            scrollDeltaY: isScroll
                ? event.getIntegerValueField(.scrollWheelEventPointDeltaAxis1) : nil,
            scrollIsContinuous: isScroll
                ? event.getIntegerValueField(.scrollWheelEventIsContinuous) != 0 : nil,
            keyboardIsRepeat: isKeyboard && type != .flagsChanged
                ? event.getIntegerValueField(.keyboardEventAutorepeat) != 0 : nil,
            modifiers: modifierNames(event.flags),
            frontmostAppName: app?.localizedName,
            frontmostBundleIdentifier: app?.bundleIdentifier,
            frontmostProcessIdentifier: app?.processIdentifier
        )

        if let data = try? writer.write(record) {
            writeLineToStandardOutput(data)
        }
    }
}

private struct TriggerRecord: Encodable {
    let schemaVersion = 1
    let sequence: UInt64
    let observedAt: String
    let eventTimestampNanoseconds: UInt64
    let kind: String
    let x: Double?
    let y: Double?
    let displayID: UInt32?
    let displayBounds: RectValue?
    let deltaX: Double?
    let deltaY: Double?
    let mouseButton: Int64?
    let clickCount: Int64?
    let scrollDeltaX: Int64?
    let scrollDeltaY: Int64?
    let scrollIsContinuous: Bool?
    let keyboardIsRepeat: Bool?
    let modifiers: [String]
    let frontmostAppName: String?
    let frontmostBundleIdentifier: String?
    let frontmostProcessIdentifier: Int32?
}

private func triggerKind(_ type: CGEventType) -> String? {
    switch type {
    case .keyDown: return "keyboard_down"
    case .keyUp: return "keyboard_up"
    case .flagsChanged: return "keyboard_modifiers_changed"
    case .mouseMoved: return "mouse_moved"
    case .leftMouseDown: return "mouse_button_left_down"
    case .leftMouseUp: return "mouse_button_left_up"
    case .rightMouseDown: return "mouse_button_right_down"
    case .rightMouseUp: return "mouse_button_right_up"
    case .otherMouseDown: return "mouse_button_other_down"
    case .otherMouseUp: return "mouse_button_other_up"
    case .leftMouseDragged: return "mouse_left_dragged"
    case .rightMouseDragged: return "mouse_right_dragged"
    case .otherMouseDragged: return "mouse_other_dragged"
    case .scrollWheel: return "scroll"
    default: return nil
    }
}

private func modifierNames(_ flags: CGEventFlags) -> [String] {
    let known: [(CGEventFlags, String)] = [
        (.maskCommand, "command"),
        (.maskControl, "control"),
        (.maskAlternate, "option"),
        (.maskShift, "shift"),
        (.maskAlphaShift, "caps_lock"),
        (.maskSecondaryFn, "function"),
    ]
    return known.compactMap { flags.contains($0.0) ? $0.1 : nil }
}

enum TriggerCollectorError: Error, CustomStringConvertible {
    case eventTapUnavailable

    var description: String {
        "could not create a global input event tap; grant Input Monitoring permission and try again"
    }
}
