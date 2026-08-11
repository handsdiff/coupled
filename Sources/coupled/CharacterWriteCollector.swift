import AppKit
import ApplicationServices
import CoupledCore
import Foundation

/// A narrow second-stage sensor: Unicode key-down characters are grouped by app
/// and emitted as one write after an idle delay.
final class CharacterWriteCollector {
    private let configuration: Configuration
    private let writer: JSONLWriter
    private var eventTap: CFMachPort?
    private var eventTapSource: CFRunLoopSource?
    private var sequence: UInt64 = 0
    private var pendingByProcess: [Int32: PendingCharacterWrite] = [:]
    private var timersByProcess: [Int32: Timer] = [:]

    init(configuration: Configuration, writer: JSONLWriter? = nil) throws {
        self.configuration = configuration
        if let writer {
            self.writer = writer
        } else {
            self.writer = try JSONLWriter(
                path: configuration.writesPath,
                sessionID: configuration.sessionID
            )
        }
    }

    func run() throws {
        try start()
        RunLoop.current.run()
    }

    func start() throws {
        guard installEventTap() else { throw CharacterWriteCollectorError.eventTapUnavailable }

        writeDiagnostic("character writes: \(writer.path)")
        writeDiagnostic("one write is emitted after \(configuration.writeDelay) seconds without a new character in that app")
        writeDiagnostic("Command/Control shortcuts and non-writing control keys are ignored")
        writeDiagnostic("provenance is typed_character_burst; no field or document change is verified")
        writeDiagnostic("warning: this simple mode cannot recognize secure text fields")
        writeDiagnostic("allowed bundles: \(configuration.allowedBundles.sorted().joined(separator: ", "))")
        if let pauseFile = configuration.pauseFile {
            writeDiagnostic("collection pauses while this file exists: \(pauseFile)")
        }
    }

    private func installEventTap() -> Bool {
        let mask = CGEventMask(1) << CGEventMask(CGEventType.keyDown.rawValue)
        let callback: CGEventTapCallBack = { _, type, event, userInfo in
            guard let userInfo else { return Unmanaged.passUnretained(event) }
            let collector = Unmanaged<CharacterWriteCollector>
                .fromOpaque(userInfo)
                .takeUnretainedValue()

            if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
                if let tap = collector.eventTap {
                    CGEvent.tapEnable(tap: tap, enable: true)
                }
                return Unmanaged.passUnretained(event)
            }

            if type == .keyDown {
                collector.record(event)
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

    private func record(_ event: CGEvent) {
        guard !configuration.isPaused() else { return }
        let flags = event.flags
        guard !flags.contains(.maskCommand), !flags.contains(.maskControl) else { return }

        guard let app = NSWorkspace.shared.frontmostApplication else { return }
        let appName = app.localizedName ?? app.bundleIdentifier ?? "Unknown"
        guard configuration.captures(
            bundleIdentifier: app.bundleIdentifier,
            appName: appName
        ) else { return }

        let characters = writableCharacters(in: unicodeText(from: event))
        guard !characters.isEmpty else { return }

        let processIdentifier = app.processIdentifier
        let observedAt = nowTimestamp()
        let text = String(characters)
        let repeatCharacterCount = event.getIntegerValueField(.keyboardEventAutorepeat) != 0
            ? characters.count : 0

        if var pending = pendingByProcess[processIdentifier] {
            pending.content.append(text)
            pending.characterCount += characters.count
            pending.keyDownEventCount += 1
            pending.repeatCharacterCount += repeatCharacterCount
            pending.lastCharacterAt = observedAt
            pending.lastEventTimestampNanoseconds = event.timestamp
            pendingByProcess[processIdentifier] = pending
        } else {
            pendingByProcess[processIdentifier] = PendingCharacterWrite(
                content: text,
                characterCount: characters.count,
                keyDownEventCount: 1,
                repeatCharacterCount: repeatCharacterCount,
                firstCharacterAt: observedAt,
                lastCharacterAt: observedAt,
                firstEventTimestampNanoseconds: event.timestamp,
                lastEventTimestampNanoseconds: event.timestamp,
                appName: appName,
                bundleIdentifier: app.bundleIdentifier,
                processIdentifier: processIdentifier
            )
        }

        timersByProcess[processIdentifier]?.invalidate()
        timersByProcess[processIdentifier] = Timer.scheduledTimer(
            withTimeInterval: configuration.writeDelay,
            repeats: false
        ) { [weak self] _ in
            self?.emitPendingWrite(for: processIdentifier)
        }
    }

    private func emitPendingWrite(for processIdentifier: Int32) {
        timersByProcess[processIdentifier]?.invalidate()
        timersByProcess[processIdentifier] = nil
        guard let pending = pendingByProcess.removeValue(forKey: processIdentifier) else { return }
        guard !configuration.isPaused() else { return }

        sequence += 1
        let record = SettledWriteRecord(
            sequence: sequence,
            observedAt: nowTimestamp(),
            firstCharacterAt: pending.firstCharacterAt,
            lastCharacterAt: pending.lastCharacterAt,
            firstEventTimestampNanoseconds: pending.firstEventTimestampNanoseconds,
            lastEventTimestampNanoseconds: pending.lastEventTimestampNanoseconds,
            writeDelaySeconds: configuration.writeDelay,
            content: pending.content,
            characterCount: pending.characterCount,
            keyDownEventCount: pending.keyDownEventCount,
            repeatCharacterCount: pending.repeatCharacterCount,
            appName: pending.appName,
            bundleIdentifier: pending.bundleIdentifier,
            processIdentifier: pending.processIdentifier
        )
        if let data = try? writer.write(record) {
            writeLineToStandardOutput(data)
        }
    }
}

private struct PendingCharacterWrite {
    var content: String
    var characterCount: Int
    var keyDownEventCount: Int
    var repeatCharacterCount: Int
    let firstCharacterAt: String
    var lastCharacterAt: String
    let firstEventTimestampNanoseconds: UInt64
    var lastEventTimestampNanoseconds: UInt64
    let appName: String
    let bundleIdentifier: String?
    let processIdentifier: Int32
}

private struct SettledWriteRecord: Encodable {
    let schemaVersion = 1
    let kind = "write"
    let provenance = "typed_character_burst"
    let sequence: UInt64
    let observedAt: String
    let firstCharacterAt: String
    let lastCharacterAt: String
    let firstEventTimestampNanoseconds: UInt64
    let lastEventTimestampNanoseconds: UInt64
    let writeDelaySeconds: Double
    let content: String
    let characterCount: Int
    let keyDownEventCount: Int
    let repeatCharacterCount: Int
    let appName: String
    let bundleIdentifier: String?
    let processIdentifier: Int32
}

private func unicodeText(from event: CGEvent) -> String {
    let capacity = 64
    var buffer = [UniChar](repeating: 0, count: capacity)
    var actualLength = 0
    buffer.withUnsafeMutableBufferPointer { pointer in
        event.keyboardGetUnicodeString(
            maxStringLength: capacity,
            actualStringLength: &actualLength,
            unicodeString: pointer.baseAddress
        )
    }
    return String(utf16CodeUnits: buffer, count: min(actualLength, capacity))
}

enum CharacterWriteCollectorError: Error, CustomStringConvertible {
    case eventTapUnavailable

    var description: String {
        "could not create a global keyboard event tap; grant Input Monitoring permission and try again"
    }
}
