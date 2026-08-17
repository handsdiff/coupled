import CoupledCore
import Foundation

struct ReduceCommand {
    let inputDirectory: URL
    let outputDirectory: URL
    let reducerVersion: String

    init(arguments: [String]) throws {
        var input: String?
        var output: String?
        var version = "phase1-semantic-v1"
        var index = 0
        while index < arguments.count {
            let argument = arguments[index]
            func value() throws -> String {
                guard index + 1 < arguments.count else {
                    throw ReduceCommandError.missingValue(argument)
                }
                index += 1
                return arguments[index]
            }
            switch argument {
            case "--input": input = try value()
            case "--output": output = try value()
            case "--reducer-version": version = try value()
            default: throw ReduceCommandError.unknownOption(argument)
            }
            index += 1
        }
        guard let input else { throw ReduceCommandError.missingRequired("--input") }
        guard let output else { throw ReduceCommandError.missingRequired("--output") }
        inputDirectory = URL(fileURLWithPath: input).standardizedFileURL
        outputDirectory = URL(fileURLWithPath: output).standardizedFileURL
        reducerVersion = version
    }

    func run() throws {
        let result = try Phase1SemanticReducer(configuration: .init(
            reducerVersion: reducerVersion
        )).reduce(sourceDirectory: inputDirectory, outputDirectory: outputDirectory)
        print("Phase 1 semantic reduction complete.")
        print("Raw records:  \(result.rawRecordCount)")
        print("READ events:  \(result.readCount)")
        print("WRITE events: \(result.writeCount)")
        print("Unresolved:   \(result.unresolvedCount)")
        print("Manifest:     \(outputDirectory.appendingPathComponent("reduction.json").path)")
        print("Events:       \(outputDirectory.appendingPathComponent("events.jsonl").path)")
    }
}

enum ReduceCommandError: Error, CustomStringConvertible {
    case missingRequired(String)
    case missingValue(String)
    case unknownOption(String)

    var description: String {
        switch self {
        case .missingRequired(let option): return "reduce requires \(option)"
        case .missingValue(let option): return "missing value for \(option)"
        case .unknownOption(let option): return "unknown reduce option: \(option)"
        }
    }
}
