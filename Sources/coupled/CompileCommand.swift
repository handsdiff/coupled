import CoupledCore
import Foundation

struct CompileCommand {
    let inputDirectory: URL
    let sourceDirectory: URL?
    let outputDirectory: URL
    let conversionVersion: String
    let includeTimestampsInContext: Bool
    let terminalAgentProgramMappings: [String: String]

    init(arguments: [String]) throws {
        var input: String?
        var source: String?
        var output: String?
        var version = "phase1-causal-v15"
        var includeTimestamps = false
        var terminalAgentMappings = [String: String]()
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
            case "--terminal-agent-title":
                let mapping = try value()
                guard let separator = mapping.firstIndex(of: "=") else {
                    throw CompileCommandError.invalidValue(
                        argument, "expected TITLE=AGENT"
                    )
                }
                let title = String(mapping[..<separator])
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                    .lowercased()
                let agent = String(mapping[mapping.index(after: separator)...])
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                guard !title.isEmpty, !agent.isEmpty else {
                    throw CompileCommandError.invalidValue(
                        argument, "expected nonempty TITLE=AGENT"
                    )
                }
                guard terminalAgentMappings[title] == nil else {
                    throw CompileCommandError.invalidValue(
                        argument, "duplicate title: \(title)"
                    )
                }
                terminalAgentMappings[title] = agent
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
        terminalAgentProgramMappings = terminalAgentMappings
    }

    func run() throws {
        let compiler = CausalDatasetCompiler(configuration: .init(
            conversionVersion: conversionVersion,
            includeTimestampsInContext: includeTimestampsInContext,
            terminalAgentProgramMappings: terminalAgentProgramMappings
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
    case invalidValue(String, String)
    case unknownOption(String)

    var description: String {
        switch self {
        case .missingRequired(let option): return "compile requires \(option)"
        case .missingValue(let option): return "missing value for \(option)"
        case .invalidValue(let option, let reason):
            return "invalid value for \(option): \(reason)"
        case .unknownOption(let option): return "unknown compile option: \(option)"
        }
    }
}
