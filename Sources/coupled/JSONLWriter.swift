import CoupledCore
import Foundation

enum WriterError: Error, CustomStringConvertible {
    case couldNotCreateFile(String)
    case recordWasNotJSONObject

    var description: String {
        switch self {
        case .couldNotCreateFile(let path):
            return "could not create JSONL file at \(path)"
        case .recordWasNotJSONObject:
            return "JSONL records must encode as JSON objects"
        }
    }
}

final class JSONLWriter {
    let path: String
    private let handle: FileHandle
    private let encoder: JSONEncoder
    private let sessionID: String
    private var viewportDeduplicator = AdjacentViewportDeduplicator()

    init(path: String, sessionID: String) throws {
        self.path = path
        self.sessionID = sessionID
        let url = URL(fileURLWithPath: path)
        let directory = url.deletingLastPathComponent()
        try FileManager.default.createDirectory(
            at: directory,
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: NSNumber(value: 0o700)]
        )

        if !FileManager.default.fileExists(atPath: path) {
            guard FileManager.default.createFile(
                atPath: path,
                contents: nil,
                attributes: [.posixPermissions: NSNumber(value: 0o600)]
            ) else {
                throw WriterError.couldNotCreateFile(path)
            }
        }

        handle = try FileHandle(forWritingTo: url)
        try handle.seekToEnd()
        encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
    }

    deinit {
        try? handle.close()
    }

    @discardableResult
    func write<T: Encodable>(_ value: T) throws -> Data {
        viewportDeduplicator.reset()
        return try append(value)
    }

    func writeViewport<T: Encodable>(
        contextIdentifier: String,
        viewportContent: String,
        makeValue: (String) -> T
    ) throws -> Data? {
        guard let emittedContent = viewportDeduplicator.contentToEmit(
            contextIdentifier: contextIdentifier,
            viewportContent: viewportContent
        ) else {
            return nil
        }
        return try append(makeValue(emittedContent))
    }

    private func append<T: Encodable>(_ value: T) throws -> Data {
        let encoded = try encoder.encode(value)
        guard var object = try JSONSerialization.jsonObject(with: encoded) as? [String: Any] else {
            throw WriterError.recordWasNotJSONObject
        }
        object["sessionID"] = sessionID
        var data = try JSONSerialization.data(
            withJSONObject: object,
            options: [.sortedKeys, .withoutEscapingSlashes]
        )
        data.append(0x0A)
        try handle.write(contentsOf: data)
        return data
    }
}

func writeLineToStandardOutput(_ data: Data) {
    try? FileHandle.standardOutput.write(contentsOf: data)
}

func writeDiagnostic(_ message: String) {
    let line = "[coupled] \(message)\n"
    if let data = line.data(using: .utf8) {
        try? FileHandle.standardError.write(contentsOf: data)
    }
}
