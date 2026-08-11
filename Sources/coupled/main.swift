import ApplicationServices
import Foundation

let help = """
coupled — local macOS input-trigger experiment

USAGE
  coupled triggers [options]   Emit raw input triggers as JSONL; no AX interpretation.
  coupled writes [options]     Emit typed-character bursts after an idle delay.
  coupled reads [options]      Emit timing-only read candidates after an idle delay.
  coupled events [options]     Emit screen-text reads and settled typed writes.
  coupled collect [options]    Continuously emit interpreted events as JSONL.
  coupled snapshot [options]   Capture one visible-text snapshot and exit.
  coupled doctor [options]     Report required macOS permissions.

OPTIONS
  --output PATH                Output directory (default: ./coupled-data/<timestamp>)
  --pause-file PATH            Pause all capture while PATH exists
  --write-delay SECONDS        Idle time before emitting a typed write (default: 3)
  --read-delay SECONDS         Idle time before emitting a read candidate (default: 1)
  --viewport-side-crop NUMBER  Fraction removed from each side for OCR (default: 0.1)
  --viewport-top-crop NUMBER   Fraction removed from the top for OCR (default: 0.1)
  --viewport-bottom-crop NUM   Fraction removed from the bottom for OCR (default: 0.5)
  --allow-bundle ID            Add a bundle to the default Obsidian/Chrome/Codex allowlist
  --exclude-bundle ID          Remove a bundle from capture; may be repeated
  --exclude-app-name NAME      Ignore an application name; may be repeated
  --prompt-permissions         Ask macOS to show relevant permission prompts
  -h, --help                   Show this help

LATER INTERPRETATION OPTIONS
  --poll-interval SECONDS      Focus-change polling interval (default: 0.35)
  --max-characters COUNT       Maximum text retained per snapshot/field (default: 30000)
  --max-nodes COUNT            Maximum Accessibility nodes visited per snapshot (default: 1200)
  --read-on-write              Also schedule a read snapshot after each settled write
  --no-activate-renderer-accessibility
                               Do not activate Chromium/Electron AX trees

FILES
  triggers.jsonl               One record per keyboard, pointer, click, or scroll trigger
  writes.jsonl                 Settled per-app typed-character write bursts
  reads.jsonl                  Settled per-app/per-display read candidates
  raw.jsonl                    Coalesced input activity and Accessibility observations
  events.jsonl                 Combined OCR reads and typed writes

The trigger collector never records typed characters or raw key codes. The
writes/events record settled typed-character bursts and cannot identify secure
fields; events also recognizes a central crop of visible screen text. Treat all
output as sensitive.
"""

do {
    let configuration = try Configuration(arguments: CommandLine.arguments)

    if configuration.command == "help" {
        print(help)
        exit(EXIT_SUCCESS)
    }

    if configuration.promptForPermissions {
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(options)
        _ = CGRequestListenEventAccess()
        _ = CGRequestScreenCaptureAccess()
    }

    if configuration.command == "doctor" {
        print("Input Monitoring (trigger capture): \(CGPreflightListenEventAccess() ? "granted" : "missing")")
        print("Screen Recording (screen-text reads): \(CGPreflightScreenCaptureAccess() ? "granted" : "missing")")
        print("Accessibility (later interpretation): \(AXIsProcessTrusted() ? "granted" : "missing")")
        if !AXIsProcessTrusted() || !CGPreflightListenEventAccess() || !CGPreflightScreenCaptureAccess() {
            print(permissionInstructions())
        }
        exit(EXIT_SUCCESS)
    }

    switch configuration.command {
    case "triggers":
        let collector = try TriggerCollector(configuration: configuration)
        try collector.run()
    case "writes":
        let collector = try CharacterWriteCollector(configuration: configuration)
        try collector.run()
    case "reads":
        let collector = try ReadCandidateCollector(configuration: configuration)
        try collector.run()
    case "events":
        guard CGPreflightScreenCaptureAccess() else { throw MainError.missingScreenRecording }
        let eventWriter = try JSONLWriter(path: configuration.eventsPath)
        let writes = try CharacterWriteCollector(
            configuration: configuration,
            writer: eventWriter
        )
        let reads = try ReadCandidateCollector(
            configuration: configuration,
            captureScreenText: true,
            writer: eventWriter
        )
        try writes.start()
        try reads.start()
        RunLoop.current.run()
    case "collect":
        guard AXIsProcessTrusted() else { throw MainError.missingAccessibility }
        let collector = try Collector(configuration: configuration)
        try collector.run()
    case "snapshot":
        guard AXIsProcessTrusted() else { throw MainError.missingAccessibility }
        let collector = try Collector(configuration: configuration)
        collector.captureOneSnapshot()
    default:
        throw MainError.unknownCommand(configuration.command)
    }
} catch {
    writeDiagnostic(String(describing: error))
    writeDiagnostic("run `coupled --help` for usage")
    exit(EXIT_FAILURE)
}

enum MainError: Error, CustomStringConvertible {
    case missingAccessibility
    case missingScreenRecording
    case unknownCommand(String)

    var description: String {
        switch self {
        case .missingAccessibility:
            return "Accessibility permission is required; run `coupled doctor --prompt-permissions`"
        case .missingScreenRecording:
            return "Screen Recording permission is required for screen-text reads; run `coupled doctor --prompt-permissions`"
        case .unknownCommand(let command):
            return "unknown command: \(command)"
        }
    }
}

func permissionInstructions() -> String {
    let executablePath = URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL.path
    if let appRange = executablePath.range(of: ".app/Contents/MacOS/") {
        let appPath = String(executablePath[..<appRange.lowerBound]) + ".app"
        return "Add this bundle in System Settings > Privacy & Security. Input Monitoring supports triggers/writes; Screen Recording supports screen-text reads; Accessibility is needed only by the older interpreted collector:\n\(appPath)"
    }
    return "Package the executable with `./scripts/package-app.sh`, then grant permissions to dist/Coupled.app instead of this build artifact."
}
