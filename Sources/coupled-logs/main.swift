import AppKit
import CoupledCore
import Foundation

private let collectorBundleIdentifier = "com.niyant.coupled"

@main
struct CoupledLogsApplication {
    static func main() {
        let application = NSApplication.shared
        let delegate = LogViewerApplicationDelegate()
        application.delegate = delegate
        application.setActivationPolicy(.regular)
        application.run()
    }
}

private final class LogViewerApplicationDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private let maximumCharacters = 250_000
    private let initialTailLineCount = 8
    private let initialTailByteCount: UInt64 = 512 * 1_024

    private var window: NSWindow?
    private var textView: NSTextView?
    private var timer: Timer?
    private var attachedLogURL: URL?
    private var byteOffset: UInt64 = 0
    private var partialLine = Data()
    private var formattedEvents: [String] = []
    private var lastHeader = ""

    private lazy var projectDirectory: URL = {
        if let override = ProcessInfo.processInfo.environment["COUPLED_PROJECT_DIR"],
           !override.isEmpty {
            return URL(fileURLWithPath: override).standardizedFileURL
        }
        return Bundle.main.bundleURL
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .standardizedFileURL
    }()

    func applicationDidFinishLaunching(_ notification: Notification) {
        makeWindow()
        refresh(force: true)
        timer = Timer.scheduledTimer(
            withTimeInterval: 0.4,
            repeats: true
        ) { [weak self] _ in
            self?.refresh(force: false)
        }
        RunLoop.main.add(timer!, forMode: .common)
        NSApplication.shared.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationShouldHandleReopen(
        _ sender: NSApplication,
        hasVisibleWindows flag: Bool
    ) -> Bool {
        window?.makeKeyAndOrderFront(nil)
        refresh(force: true)
        return true
    }

    private func makeWindow() {
        let size = NSSize(width: 940, height: 680)
        let visible = NSScreen.main?.visibleFrame ?? NSRect(origin: .zero, size: size)
        let frame = NSRect(
            x: visible.midX - size.width / 2,
            y: visible.midY - size.height / 2,
            width: size.width,
            height: size.height
        )
        let window = NSWindow(
            contentRect: frame,
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Coupled Logs"
        window.isReleasedWhenClosed = false
        window.delegate = self

        let scrollView = NSScrollView(frame: NSRect(origin: .zero, size: size))
        scrollView.autoresizingMask = [.width, .height]
        scrollView.hasVerticalScroller = true
        scrollView.hasHorizontalScroller = true
        scrollView.borderType = .noBorder

        let textView = NSTextView(frame: scrollView.bounds)
        textView.autoresizingMask = [.width]
        textView.isEditable = false
        textView.isSelectable = true
        textView.isRichText = false
        textView.importsGraphics = false
        textView.usesFindPanel = true
        textView.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        textView.textColor = .labelColor
        textView.backgroundColor = .textBackgroundColor
        textView.textContainer?.widthTracksTextView = false
        textView.textContainer?.containerSize = NSSize(
            width: CGFloat.greatestFiniteMagnitude,
            height: CGFloat.greatestFiniteMagnitude
        )
        scrollView.documentView = textView
        window.contentView = scrollView

        self.window = window
        self.textView = textView
        window.makeKeyAndOrderFront(nil)
    }

    private func refresh(force: Bool) {
        let discovery = discoverRun()
        if discovery.logURL != attachedLogURL {
            attach(to: discovery.logURL)
        } else {
            readAppendedData()
        }

        let header = makeHeader(discovery)
        if force || header != lastHeader {
            lastHeader = header
            render(scrollToEnd: force)
        }
    }

    private func discoverRun() -> RunDiscovery {
        let launchDirectory = projectDirectory.appendingPathComponent(".coupled-launch")
        let stateURL = launchDirectory.appendingPathComponent("latest-run")
        let state = parseState(at: stateURL)
        let stateLog = state["stdoutLog"].map(URL.init(fileURLWithPath:))
        let logURL = stateLog.flatMap { FileManager.default.fileExists(atPath: $0.path) ? $0 : nil }
            ?? latestLog(in: launchDirectory)
        let sessionURL = state["sessionPath"].map(URL.init(fileURLWithPath:))
        let collectorRunning = !NSRunningApplication.runningApplications(
            withBundleIdentifier: collectorBundleIdentifier
        ).isEmpty
        return RunDiscovery(
            isLive: collectorRunning && stateLog != nil && stateLog == logURL,
            logURL: logURL,
            sessionURL: sessionURL
        )
    }

    private func parseState(at url: URL) -> [String: String] {
        guard let text = try? String(contentsOf: url, encoding: .utf8) else { return [:] }
        return text.split(separator: "\n").reduce(into: [:]) { values, line in
            let components = line.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false)
            guard components.count == 2 else { return }
            values[String(components[0])] = String(components[1])
        }
    }

    private func latestLog(in directory: URL) -> URL? {
        guard let urls = try? FileManager.default.contentsOfDirectory(
            at: directory,
            includingPropertiesForKeys: [.contentModificationDateKey],
            options: [.skipsHiddenFiles]
        ) else { return nil }
        return urls
            .filter { $0.lastPathComponent.hasSuffix(".stdout.jsonl") }
            .max { left, right in
                let leftDate = try? left.resourceValues(forKeys: [.contentModificationDateKey])
                    .contentModificationDate
                let rightDate = try? right.resourceValues(forKeys: [.contentModificationDateKey])
                    .contentModificationDate
                return (leftDate ?? .distantPast) < (rightDate ?? .distantPast)
            }
    }

    private func attach(to logURL: URL?) {
        attachedLogURL = logURL
        byteOffset = 0
        partialLine.removeAll(keepingCapacity: false)
        formattedEvents.removeAll(keepingCapacity: false)
        guard let logURL,
              let attributes = try? FileManager.default.attributesOfItem(atPath: logURL.path),
              let size = (attributes[.size] as? NSNumber)?.uint64Value,
              let handle = try? FileHandle(forReadingFrom: logURL) else {
            render(scrollToEnd: false)
            return
        }
        defer { try? handle.close() }
        let start = size > initialTailByteCount ? size - initialTailByteCount : 0
        try? handle.seek(toOffset: start)
        let dataValue: Data
        do {
            dataValue = try handle.readToEnd() ?? Data()
        } catch {
            byteOffset = size
            return
        }
        var lines = splitCompleteLines(dataValue)
        if start > 0, !lines.isEmpty {
            lines.removeFirst()
        }
        for line in lines.suffix(initialTailLineCount) {
            appendFormatted(line)
        }
        byteOffset = size
        render(scrollToEnd: true)
    }

    private func readAppendedData() {
        guard let logURL = attachedLogURL,
              let attributes = try? FileManager.default.attributesOfItem(atPath: logURL.path),
              let size = (attributes[.size] as? NSNumber)?.uint64Value else { return }
        if size < byteOffset {
            attach(to: logURL)
            return
        }
        guard size > byteOffset,
              let handle = try? FileHandle(forReadingFrom: logURL) else { return }
        defer { try? handle.close() }
        try? handle.seek(toOffset: byteOffset)
        let data: Data
        do {
            data = try handle.readToEnd() ?? Data()
        } catch {
            return
        }
        byteOffset = size
        let lines = splitCompleteLines(data)
        guard !lines.isEmpty else { return }
        lines.forEach(appendFormatted)
        trimEvents()
        render(scrollToEnd: true)
    }

    private func splitCompleteLines(_ newData: Data) -> [Data] {
        partialLine.append(newData)
        var lines: [Data] = []
        while let newline = partialLine.firstIndex(of: 0x0A) {
            let line = partialLine[..<newline]
            if !line.isEmpty { lines.append(Data(line)) }
            partialLine.removeSubrange(...newline)
        }
        return lines
    }

    private func appendFormatted(_ line: Data) {
        if let formatted = LiveEventLogFormatter.format(line) {
            formattedEvents.append(formatted)
        }
    }

    private func trimEvents() {
        var count = formattedEvents.reduce(0) { $0 + $1.count + 2 }
        while count > maximumCharacters, formattedEvents.count > 1 {
            count -= formattedEvents.removeFirst().count + 2
        }
    }

    private func makeHeader(_ discovery: RunDiscovery) -> String {
        let status = discovery.isLive ? "LIVE COLLECTION" : "NO LIVE COLLECTION"
        var lines = [
            "COUPLED LOGS — \(status)",
            "Following: \(discovery.logURL?.path ?? "No launch log exists yet")",
        ]
        if let settings = sessionSettings(at: discovery.sessionURL) {
            lines.append("")
            lines.append("RUN SETTINGS")
            lines.append(settings)
        } else if discovery.isLive {
            lines.append("")
            lines.append("RUN SETTINGS")
            lines.append("Waiting for session.json…")
        }
        lines.append("")
        lines.append("LIVE EVENT STREAM")
        if formattedEvents.isEmpty {
            lines.append("Waiting for derived events…")
        }
        return lines.joined(separator: "\n")
    }

    private func sessionSettings(at url: URL?) -> String? {
        guard let url,
              let data = try? Data(contentsOf: url),
              let manifest = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }
        var summary: [String: Any] = [:]
        for key in ["sessionID", "startedAt", "command", "outputDirectory"] {
            summary[key] = manifest[key]
        }
        summary["configuration"] = manifest["configuration"]
        guard JSONSerialization.isValidJSONObject(summary),
              let encoded = try? JSONSerialization.data(
                withJSONObject: summary,
                options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
              ) else { return nil }
        return String(data: encoded, encoding: .utf8)
    }

    private func render(scrollToEnd: Bool) {
        guard let textView else { return }
        let body = formattedEvents.isEmpty ? "" : "\n" + formattedEvents.joined(separator: "\n\n")
        textView.string = lastHeader + body
        if scrollToEnd {
            textView.scrollToEndOfDocument(nil)
        }
    }
}

private struct RunDiscovery {
    let isLive: Bool
    let logURL: URL?
    let sessionURL: URL?
}
