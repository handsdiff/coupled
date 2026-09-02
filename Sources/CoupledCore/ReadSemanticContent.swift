import CryptoKit
import Foundation

public struct ReadOCRLineEvidence: Equatable, Sendable {
    public let index: Int
    public let text: String
    public let confidence: Double
    public let x: Double?
    public let y: Double?
    public let width: Double?
    public let height: Double?

    public init(
        index: Int,
        text: String,
        confidence: Double,
        x: Double? = nil,
        y: Double? = nil,
        width: Double? = nil,
        height: Double? = nil
    ) {
        self.index = index
        self.text = text
        self.confidence = confidence
        self.x = x
        self.y = y
        self.width = width
        self.height = height
    }

    fileprivate var normalizedText: String {
        text.precomposedStringWithCanonicalMapping
            .split(whereSeparator: \Character.isWhitespace)
            .joined(separator: " ")
    }

    fileprivate var centerX: Double? {
        guard let x, let width else { return nil }
        return x + width / 2
    }

    fileprivate var centerY: Double? {
        guard let y, let height else { return nil }
        return y + height / 2
    }
}

public struct RemovedReadScaffoldingLine: Equatable, Sendable {
    public let line: ReadOCRLineEvidence
    public let reason: String
    public let supportingDistinctContentStateCount: Int
    public let supportingDistinctWindowCount: Int
}

public struct ReadSemanticContentProjection: Equatable, Sendable {
    public let content: String
    public let removed: [RemovedReadScaffoldingLine]
}

/// A deliberately conservative, past-only interface-scaffolding detector.
/// Application-specific rules cover interface elements whose semantics are
/// directly known. The generic rule requires exact text at matching peripheral
/// geometry across several earlier, genuinely different central-content states.
/// Window-title diversity is supporting evidence, never the definition of a
/// changed content state.
public struct ReadInterfaceScaffoldingTracker: Sendable {
    private struct SeenPlacement: Sendable {
        let x: Double
        let y: Double
        let width: Double
        let height: Double
        var centralContentStates: Set<String>
        var windowTitles: Set<String>
    }

    private struct SeenLine: Sendable {
        var placements: [SeenPlacement]
    }

    private var seenBySurface = [String: [String: SeenLine]]()

    public init() {}

    public mutating func project(
        surfaceKey: String,
        bundleIdentifier: String,
        windowTitle: String,
        observedContent: String,
        lines: [ReadOCRLineEvidence]
    ) -> ReadSemanticContentProjection {
        guard !lines.isEmpty else {
            return ReadSemanticContentProjection(content: observedContent, removed: [])
        }
        let prior = seenBySurface[surfaceKey] ?? [:]
        let centralState = centralContentFingerprint(lines)
        var removed = [RemovedReadScaffoldingLine]()
        var retained = [String]()
        for line in lines {
            let normalized = line.normalizedText
            guard !normalized.isEmpty else { continue }
            if let reason = knownScaffoldingReason(
                bundleIdentifier: bundleIdentifier,
                line: line
            ) {
                removed.append(RemovedReadScaffoldingLine(
                    line: line,
                    reason: reason,
                    supportingDistinctContentStateCount: 0,
                    supportingDistinctWindowCount: 0
                ))
                continue
            }
            let supportingPlacement = prior[normalized]?.placements.first { placement in
                geometryMatches(
                    (placement.x, placement.y, placement.width, placement.height),
                    line
                )
            }
            let contentStateSupport = supportingPlacement?.centralContentStates.count ?? 0
            let windowSupport = supportingPlacement?.windowTitles.count ?? 0
            if contentStateSupport >= 4,
               windowSupport >= 3,
               line.confidence >= 0.80,
               isPeripheral(line), supportingPlacement != nil {
                removed.append(RemovedReadScaffoldingLine(
                    line: line,
                    reason: "stable_peripheral_interface_text",
                    supportingDistinctContentStateCount: contentStateSupport,
                    supportingDistinctWindowCount: windowSupport
                ))
            } else {
                retained.append(line.text)
            }
        }
        remember(
            surfaceKey: surfaceKey,
            windowTitle: windowTitle,
            centralContentState: centralState,
            lines: lines
        )
        return ReadSemanticContentProjection(
            content: retained.joined(separator: "\n"),
            removed: removed
        )
    }

    private mutating func remember(
        surfaceKey: String,
        windowTitle: String,
        centralContentState: String?,
        lines: [ReadOCRLineEvidence]
    ) {
        var surface = seenBySurface[surfaceKey] ?? [:]
        for line in lines where line.confidence >= 0.80 && isPeripheral(line) {
            let normalized = line.normalizedText
            guard !normalized.isEmpty,
                  let x = line.x, let y = line.y,
                  let width = line.width, let height = line.height else { continue }
            var seen = surface[normalized] ?? SeenLine(placements: [])
            if let placementIndex = seen.placements.firstIndex(where: { placement in
                geometryMatches(
                    (placement.x, placement.y, placement.width, placement.height),
                    line
                )
            }) {
                if !windowTitle.isEmpty,
                   seen.placements[placementIndex].windowTitles.count < 3 {
                    seen.placements[placementIndex].windowTitles.insert(windowTitle)
                }
                if let centralContentState,
                   seen.placements[placementIndex].centralContentStates.count < 8 {
                    seen.placements[placementIndex].centralContentStates.insert(
                        centralContentState
                    )
                }
            } else if seen.placements.count < 8 {
                seen.placements.append(SeenPlacement(
                    x: x, y: y, width: width, height: height,
                    centralContentStates: centralContentState.map { Set([$0]) }
                        ?? Set(),
                    windowTitles: windowTitle.isEmpty ? Set() : Set([windowTitle])
                ))
            }
            surface[normalized] = seen
        }
        seenBySurface[surfaceKey] = surface
    }
}

/// Identifies a tiny isolated OCR addition that is much more plausibly a
/// loading/spinner glyph than semantic content. This never removes the glyph
/// from the complete READ; it is only a conservative disposition for the
/// optional predecessor-dependent novelty projection.
public func isolatedReadNoveltyMicroglyph(
    _ novelty: String,
    lines: [ReadOCRLineEvidence],
    excludingLineIndices: Set<Int> = []
) -> ReadOCRLineEvidence? {
    let normalizedNovelty = novelty.precomposedStringWithCanonicalMapping
        .split(whereSeparator: \Character.isWhitespace)
        .joined(separator: " ")
    guard (1...2).contains(normalizedNovelty.count) else { return nil }
    let matches = lines.filter { line in
        guard !excludingLineIndices.contains(line.index),
              line.normalizedText == normalizedNovelty,
              let width = line.width, let height = line.height else { return false }
        return width <= 0.05 && height <= 0.06
    }
    return matches.count == 1 ? matches[0] : nil
}

private func knownScaffoldingReason(
    bundleIdentifier: String,
    line: ReadOCRLineEvidence
) -> String? {
    let text = line.normalizedText
    let lowered = text.lowercased()
    if bundleIdentifier == "md.obsidian",
       (line.centerY ?? 1) <= 0.04 {
        if lowered.range(
            of: #"^[0-9]+ backlinks?(?:\s+.*)?$"#,
            options: .regularExpression
        ) != nil {
            return "obsidian_backlink_status"
        }
        if lowered.range(
            of: #"^[0-9,]+ words\s+[0-9,]+ characters$"#,
            options: .regularExpression
        ) != nil {
            return "obsidian_document_statistics"
        }
    }
    guard bundleIdentifier == "com.openai.codex" else { return nil }
    if lowered.range(
        of: #"^(working|worked) for [0-9]+(?:\.[0-9]+)?[smh](?: [0-9]+[smh])?\s*>?$"#,
        options: .regularExpression
    ) != nil {
        return "codex_progress_status"
    }
    let centerY = line.centerY ?? 1
    if centerY <= 0.25, line.confidence < 0.50, text.count <= 2 {
        return "low_confidence_composer_microtext"
    }
    if centerY <= 0.15,
       lowered == "do anything" || lowered == "po anything" {
        return "codex_composer_placeholder"
    }
    guard centerY <= 0.08 else { return nil }
    if text == "+" { return "codex_composer_control" }
    if lowered == "approve for me" { return "codex_approval_control" }
    if lowered == "5.6 sol extra high v" { return "codex_model_selector" }
    return nil
}

private func centralContentFingerprint(_ lines: [ReadOCRLineEvidence]) -> String? {
    let content = lines.compactMap { line -> String? in
        guard line.confidence >= 0.80,
              let centerY = line.centerY,
              centerY >= 0.20, centerY <= 0.80 else { return nil }
        let normalized = line.normalizedText
        return normalized.isEmpty ? nil : normalized
    }.joined(separator: "\n")
    guard !content.isEmpty else { return nil }
    return SHA256.hash(data: Data(content.utf8))
        .map { String(format: "%02x", $0) }.joined()
}

private func isPeripheral(_ line: ReadOCRLineEvidence) -> Bool {
    guard let centerY = line.centerY else { return false }
    return centerY <= 0.15 || centerY >= 0.85
}

private func geometryMatches(
    _ prior: (x: Double, y: Double, width: Double, height: Double),
    _ current: ReadOCRLineEvidence
) -> Bool {
    guard let centerX = current.centerX, let centerY = current.centerY,
          let width = current.width, let height = current.height else { return false }
    let priorCenterX = prior.x + prior.width / 2
    let priorCenterY = prior.y + prior.height / 2
    return abs(priorCenterX - centerX) <= 0.04
        && abs(priorCenterY - centerY) <= 0.025
        && abs(prior.width - width) <= 0.08
        && abs(prior.height - height) <= 0.025
}
