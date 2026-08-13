import CryptoKit
import Foundation

enum SessionManifestError: Error, CustomStringConvertible {
    case alreadyExists(String)
    case outputContainsData(String)
    case couldNotCreate(String)

    var description: String {
        switch self {
        case .alreadyExists(let path):
            return "session manifest already exists at \(path); use a fresh output directory"
        case .outputContainsData(let path):
            return "output already contains collection data at \(path); use a fresh output directory"
        case .couldNotCreate(let path):
            return "could not create immutable session manifest at \(path)"
        }
    }
}

func writeSessionManifest(_ configuration: Configuration) throws {
    let fileManager = FileManager.default
    let manifestURL = URL(fileURLWithPath: configuration.sessionPath)
    let outputURL = manifestURL.deletingLastPathComponent()
    try fileManager.createDirectory(
        at: outputURL,
        withIntermediateDirectories: true,
        attributes: [.posixPermissions: NSNumber(value: 0o700)]
    )

    guard !fileManager.fileExists(atPath: manifestURL.path) else {
        throw SessionManifestError.alreadyExists(manifestURL.path)
    }
    for path in [
        configuration.rawPath,
        configuration.eventsPath,
        configuration.triggersPath,
        configuration.writesPath,
        configuration.readsPath,
    ] where fileManager.fileExists(atPath: path) {
        let size = try fileManager.attributesOfItem(atPath: path)[.size] as? NSNumber
        if size?.intValue ?? 0 > 0 {
            throw SessionManifestError.outputContainsData(path)
        }
    }

    let manifest = SessionManifest(
        sessionID: configuration.sessionID,
        startedAt: configuration.sessionStartedAt,
        command: configuration.command,
        outputDirectory: outputURL.standardizedFileURL.path,
        configuration: ResolvedCollectorConfiguration(configuration),
        ocr: OCRManifest(configuration),
        schemas: SchemaManifest(),
        build: CollectorBuildManifest()
    )
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
    var data = try encoder.encode(manifest)
    data.append(0x0A)
    guard fileManager.createFile(
        atPath: manifestURL.path,
        contents: data,
        attributes: [.posixPermissions: NSNumber(value: 0o600)]
    ) else {
        throw SessionManifestError.couldNotCreate(manifestURL.path)
    }
}

private struct SessionManifest: Encodable {
    let schemaVersion = 1
    let sessionID: String
    let startedAt: String
    let command: String
    let outputDirectory: String
    let configuration: ResolvedCollectorConfiguration
    let ocr: OCRManifest
    let schemas: SchemaManifest
    let build: CollectorBuildManifest
}

private struct ResolvedCollectorConfiguration: Encodable {
    let readDelaySeconds: Double
    let writeDelaySeconds: Double
    let postPasteCheckpointDelaySeconds: Double
    let postInputCheckpointDelaySeconds: Double
    let cursorContextCharacters: Int
    let viewportSideCropFraction: Double
    let viewportTopCropFraction: Double
    let viewportBottomCropFraction: Double
    let pollIntervalSeconds: Double
    let maxCharacters: Int
    let maxNodes: Int
    let activateRendererAccessibility: Bool
    let readOnWrite: Bool
    let retainScreenshots: Bool
    let promptForPermissions: Bool
    let allowedBundles: [String]
    let excludedBundles: [String]
    let excludedAppNames: [String]
    let pauseFile: String?

    init(_ configuration: Configuration) {
        readDelaySeconds = configuration.readDelay
        writeDelaySeconds = configuration.writeDelay
        postPasteCheckpointDelaySeconds = configuration.postPasteCheckpointDelay
        postInputCheckpointDelaySeconds = configuration.postInputCheckpointDelay
        cursorContextCharacters = configuration.cursorContextCharacters
        viewportSideCropFraction = configuration.viewportSideCropFraction
        viewportTopCropFraction = configuration.viewportTopCropFraction
        viewportBottomCropFraction = configuration.viewportBottomCropFraction
        pollIntervalSeconds = configuration.pollInterval
        maxCharacters = configuration.maxCharacters
        maxNodes = configuration.maxNodes
        activateRendererAccessibility = configuration.activateRendererAccessibility
        readOnWrite = configuration.readOnWrite
        retainScreenshots = configuration.retainScreenshots
        promptForPermissions = configuration.promptForPermissions
        allowedBundles = configuration.allowedBundles.sorted()
        excludedBundles = configuration.excludedBundles.sorted()
        excludedAppNames = configuration.excludedAppNames.sorted()
        pauseFile = configuration.pauseFile.map { resolvedPath($0) }
    }
}

private struct OCRManifest: Encodable {
    let engine = "apple_vision"
    let recognitionLevel = "accurate"
    let usesLanguageCorrection = true
    let automaticallyDetectsLanguage = true
    let screenshotAPI = "SCScreenshotManager.captureImage"
    let retainedScreenshot: Bool
    let retainedScreenshotScope: String?
    let retainedScreenshotFormat: String?

    init(_ configuration: Configuration) {
        retainedScreenshot = configuration.retainScreenshots
        retainedScreenshotScope = configuration.retainScreenshots ? "full_window" : nil
        retainedScreenshotFormat = configuration.retainScreenshots ? "png" : nil
    }
}

private struct SchemaManifest: Encodable {
    let timingSemanticsVersion = 2
    let triggerRecord = 1
    let characterWrite = 1
    let settledCharacterWrite = 1
    let readCandidate = 1
    let rawScreenOCR = 6
    let derivedScreenRead = 7
    let rawReadCandidateSuppression = 2
    let rawActiveTapWrite = 11
    let derivedActiveTapWrite = 8
    let writeSensorHealth = 1
    let rawActivity = 2
    let rawAccessibilitySnapshot = 1
    let rawEditableObservation = 1
    let understoodEvent = 1
}

private struct CollectorBuildManifest: Encodable {
    let bundleIdentifier: String?
    let shortVersion: String
    let bundleVersion: String
    let executablePath: String
    let executableSHA256: String?

    init() {
        let bundle = Bundle.main
        bundleIdentifier = bundle.bundleIdentifier
        shortVersion = bundle.object(
            forInfoDictionaryKey: "CFBundleShortVersionString"
        ) as? String ?? "development"
        bundleVersion = bundle.object(
            forInfoDictionaryKey: "CFBundleVersion"
        ) as? String ?? "development"
        let executableURL = bundle.executableURL
            ?? URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL
        executablePath = executableURL.path
        executableSHA256 = try? Data(contentsOf: executableURL).sha256
    }
}

private func resolvedPath(_ path: String) -> String {
    URL(fileURLWithPath: path).standardizedFileURL.path
}

private extension Data {
    var sha256: String {
        SHA256.hash(data: self)
            .map { String(format: "%02x", $0) }
            .joined()
    }
}
