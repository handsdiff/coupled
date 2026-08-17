import CoupledCore
import Foundation

struct CompileCommand {
    let inputDirectory: URL
    let sourceDirectory: URL?
    let outputDirectory: URL
    let conversionVersion: String
    let includeTimestampsInContext: Bool

    init(arguments: [String]) throws {
        var input: String?
        var source: String?
        var output: String?
        var version = "phase1-causal-v13"
        var includeTimestamps = false
        var index = 0
        while index < arguments.count {
            let argument = arguments[index]
            func value() throws -> String {
                guard index + 1 < arguments.count else {
                    throw CompileCommandError.missingValue(argument)
                }
                index += 1
                return arguments[index]
            }
            switch argument {
            case "--input": input = try value()
            case "--source": source = try value()
            case "--output": output = try value()
            case "--conversion-version": version = try value()
            case "--include-timestamps-in-context": includeTimestamps = true
            default: throw CompileCommandError.unknownOption(argument)
            }
            index += 1
        }
        guard let input else { throw CompileCommandError.missingRequired("--input") }
        guard let output else { throw CompileCommandError.missingRequired("--output") }
        inputDirectory = URL(fileURLWithPath: input).standardizedFileURL
        sourceDirectory = source.map { URL(fileURLWithPath: $0).standardizedFileURL }
        outputDirectory = URL(fileURLWithPath: output).standardizedFileURL
        conversionVersion = version
        includeTimestampsInContext = includeTimestamps
    }

    func run() throws {
        let compiler = CausalDatasetCompiler(configuration: .init(
            conversionVersion: conversionVersion,
            includeTimestampsInContext: includeTimestampsInContext
        ))
        let result = try compiler.compile(
            inputDirectory: inputDirectory,
            sourceDirectory: sourceDirectory,
            outputDirectory: outputDirectory
        )
        print("Phase 1 causal dataset compiled.")
        print("Source events:     \(result.sourceEventCount)")
        print("Converted events:  \(result.convertedEventCount)")
        print("Training examples: \(result.exampleCount)")
        print("Target exclusions: \(result.targetExcludedEventCount)")
        print("Context exclusions: \(result.contextExcludedEventCount)")
        print("Rejected events:   \(result.rejectedEventCount)")
        print("Manifest:          \(outputDirectory.appendingPathComponent("dataset.json").path)")
        print("Examples:          \(outputDirectory.appendingPathComponent("examples.jsonl").path)")
        print("Target exclusions: \(outputDirectory.appendingPathComponent("target-exclusions.jsonl").path)")
        print("Context exclusions: \(outputDirectory.appendingPathComponent("context-exclusions.jsonl").path)")
    }
}

enum CompileCommandError: Error, CustomStringConvertible {
    case missingRequired(String)
    case missingValue(String)
    case unknownOption(String)

    var description: String {
        switch self {
        case .missingRequired(let option): return "compile requires \(option)"
        case .missingValue(let option): return "missing value for \(option)"
        case .unknownOption(let option): return "unknown compile option: \(option)"
        }
    }
}
