import CoupledCore
import Foundation

enum WriterError: Error, CustomStringConvertible {
    case couldNotCreateFile(String)

    var description: String {
        switch self {
        case .couldNotCreateFile(let path):
            return "could not create JSONL file at \(path)"
        }
    }
}

final class JSONLWriter {
    let path: String
    private let handle: FileHandle
    private let encoder: JSONEncoder
    private var viewportDeduplicator = AdjacentViewportDeduplicator()

    init(path: String) throws {
        self.path = path
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
        var data = try encoder.encode(value)
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
