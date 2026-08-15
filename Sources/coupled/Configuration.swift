import Foundation

struct Configuration {
    let sessionID: String
    let sessionStartedAt: String
    var command = "collect"
    var outputDirectory: String
    var readDelay: TimeInterval = 3.0
    var writeDelay: TimeInterval = 3.0
    var viewportSideCropFraction = 0.1
    var viewportTopCropFraction = 0.1
    var viewportBottomCropFraction = 0.35
    let postPasteCheckpointDelay: TimeInterval = 0.05
    let postInputCheckpointDelay: TimeInterval = 0.05
    var cursorContextCharacters = 512
    var pollInterval: TimeInterval = 0.35
    var maxCharacters = 30_000
    var maxNodes = 1_200
    var promptForPermissions = false
    var activateRendererAccessibility = true
    var readOnWrite = false
    var retainScreenshots = true
    var allowedBundles: Set<String> = [
        "com.google.Chrome",
        "com.microsoft.VSCode",
        "com.openai.codex",
        "md.obsidian",
    ]
    var excludedBundles: Set<String> = [
        "com.niyant.coupled",
        "com.niyant.coupled.logs",
    ]
    var excludedAppNames = Set<String>()
    var pauseFile: String?

    init(arguments: [String]) throws {
        sessionID = UUID().uuidString
        sessionStartedAt = nowTimestamp()
        let stamp = ISO8601DateFormatter.string(
            from: Date(),
            timeZone: TimeZone.current,
            formatOptions: [.withFullDate, .withTime, .withDashSeparatorInDate]
        )
        outputDirectory = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
            .appendingPathComponent("coupled-data")
            .appendingPathComponent(stamp.replacingOccurrences(of: ":", with: "-"))
            .path

        var args = Array(arguments.dropFirst())
        if let first = args.first, !first.hasPrefix("-") {
            command = first
            args.removeFirst()
        }

        var index = 0
        while index < args.count {
            let argument = args[index]
            func value() throws -> String {
                guard index + 1 < args.count else {
                    throw ConfigurationError.missingValue(argument)
                }
                index += 1
                return args[index]
            }

            switch argument {
            case "--output":
                outputDirectory = try value()
            case "--read-delay":
                readDelay = try positiveDouble(value(), option: argument)
            case "--write-delay":
                writeDelay = try positiveDouble(value(), option: argument)
            case "--viewport-side-crop":
                viewportSideCropFraction = try fraction(
                    value(), option: argument, upperBound: 0.5
                )
            case "--viewport-top-crop":
                viewportTopCropFraction = try fraction(
                    value(), option: argument, upperBound: 1
                )
            case "--viewport-bottom-crop":
                viewportBottomCropFraction = try fraction(
                    value(), option: argument, upperBound: 1
                )
            case "--poll-interval":
                pollInterval = try positiveDouble(value(), option: argument)
            case "--max-characters":
                maxCharacters = try positiveInt(value(), option: argument)
            case "--cursor-context-characters":
                cursorContextCharacters = try positiveInt(value(), option: argument)
            case "--max-nodes":
                maxNodes = try positiveInt(value(), option: argument)
            case "--exclude-bundle":
                excludedBundles.insert(try value())
            case "--allow-bundle":
                allowedBundles.insert(try value())
            case "--exclude-app-name":
                excludedAppNames.insert(try value())
            case "--pause-file":
                pauseFile = try value()
            case "--prompt-permissions":
                promptForPermissions = true
            case "--no-activate-renderer-accessibility":
                activateRendererAccessibility = false
            case "--read-on-write":
                readOnWrite = true
            case "--no-retain-screenshots":
                retainScreenshots = false
            case "--help", "-h":
                command = "help"
            default:
                throw ConfigurationError.unknownOption(argument)
            }
            index += 1
        }

        guard viewportTopCropFraction + viewportBottomCropFraction < 1 else {
            throw ConfigurationError.invalidValue(
                "--viewport-top-crop/--viewport-bottom-crop",
                "\(viewportTopCropFraction)/\(viewportBottomCropFraction)"
            )
        }
    }

    var rawPath: String { URL(fileURLWithPath: outputDirectory).appendingPathComponent("raw.jsonl").path }
    var eventsPath: String { URL(fileURLWithPath: outputDirectory).appendingPathComponent("events.jsonl").path }
    var triggersPath: String { URL(fileURLWithPath: outputDirectory).appendingPathComponent("triggers.jsonl").path }
    var writesPath: String { URL(fileURLWithPath: outputDirectory).appendingPathComponent("writes.jsonl").path }
    var readsPath: String { URL(fileURLWithPath: outputDirectory).appendingPathComponent("reads.jsonl").path }
    var sessionPath: String { URL(fileURLWithPath: outputDirectory).appendingPathComponent("session.json").path }
    var screenshotsDirectory: String {
        URL(fileURLWithPath: outputDirectory).appendingPathComponent("screenshots").path
    }

    func isPaused() -> Bool {
        guard let pauseFile else { return false }
        return FileManager.default.fileExists(atPath: pauseFile)
    }

    func captures(bundleIdentifier: String?, appName: String) -> Bool {
        guard let bundleIdentifier,
              allowedBundles.contains(bundleIdentifier),
              !excludedBundles.contains(bundleIdentifier),
              !excludedAppNames.contains(appName) else {
            return false
        }
        return true
    }
}

enum ConfigurationError: Error, CustomStringConvertible {
    case missingValue(String)
    case invalidValue(String, String)
    case unknownOption(String)

    var description: String {
        switch self {
        case .missingValue(let option):
            return "missing value for \(option)"
        case .invalidValue(let option, let value):
            return "invalid value for \(option): \(value)"
        case .unknownOption(let option):
            return "unknown option: \(option)"
        }
    }
}

private func positiveDouble(_ value: String, option: String) throws -> Double {
    guard let parsed = Double(value), parsed > 0 else {
        throw ConfigurationError.invalidValue(option, value)
    }
    return parsed
}

private func positiveInt(_ value: String, option: String) throws -> Int {
    guard let parsed = Int(value), parsed > 0 else {
        throw ConfigurationError.invalidValue(option, value)
    }
    return parsed
}

private func fraction(_ value: String, option: String, upperBound: Double) throws -> Double {
    guard let parsed = Double(value), parsed >= 0, parsed < upperBound else {
        throw ConfigurationError.invalidValue(option, value)
    }
    return parsed
}
