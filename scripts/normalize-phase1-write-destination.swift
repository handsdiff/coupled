import Foundation

/// Small offline adapter to the production normalizer, not another set of
/// application heuristics. Input/output are one JSON object per line.
@main
struct NormalizePhase1WriteDestination {
    static func main() throws {
        while let line = readLine() {
            guard let data = line.data(using: .utf8),
                  let evidence = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            else { throw NSError(domain: "invalid_destination", code: 1) }
            let normalized = Phase1WriteDestinationNormalizer.normalize(evidence)
            let result: [String: Any] = [
                "modelFacingDestination": normalized.modelFacingJSONObject,
                "logicalDestinationKey": normalized.logicalDestinationKey,
                "destinationDerivation": Phase1WriteDestinationNormalizer.provenance(
                    evidence: evidence, normalized: normalized
                ),
            ]
            let output = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
            print(String(decoding: output, as: UTF8.self))
        }
    }
}
