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

/// Comparison-only OCR may corroborate semantic novelty, but it must not add
/// words absent from the cleaned authoritative pane observation. Whitespace
/// reflow and case are non-semantic; token order and spelling remain strict.
public func readNoveltyIsStrictlyGrounded(
    _ novelty: String,
    in authoritativeContent: String
) -> Bool {
    func normalized(_ value: String) -> String {
        value.precomposedStringWithCanonicalMapping
            .split(whereSeparator: \Character.isWhitespace)
            .joined(separator: " ")
            .lowercased()
    }
    let needle = normalized(novelty)
    let haystack = normalized(authoritativeContent)
    guard !needle.isEmpty, !haystack.isEmpty else { return false }
    return haystack == needle
        || " \(haystack) ".contains(" \(needle) ")
}

/// Cross-sensor disagreement is not sufficient evidence that a visible line
/// is clipped. A removable seed also needs low OCR confidence or the geometry
/// of a materially wide/punctuation-only line compressed at the boundary.
public func readCrossSensorClippingSeedIsStrong(
    text: String,
    confidence: Double,
    width: Double,
    height: Double,
    medianReferenceLineHeight: Double
) -> Bool {
    if confidence < 0.80 { return true }
    guard medianReferenceLineHeight > 0,
          height <= medianReferenceLineHeight * 0.80 else { return false }
    let alphanumericCount = text.reduce(0) {
        $0 + ($1.isLetter || $1.isNumber ? 1 : 0)
    }
    return width >= 0.10 || alphanumericCount == 0
}

/// A deliberately conservative interface-scaffolding detector.
/// Application-specific rules cover interface elements whose semantics are
/// directly known. The generic rule requires exact text at matching peripheral
/// geometry across several genuinely different central-content states.
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

    private struct SessionLineRecurrence: Sendable {
        var centralContentStates: Set<String>
        var surfaceKeys: Set<String>
    }

    private var seenBySurface = [String: [String: SeenLine]]()
    private var bottomContentStatesByBundle = [String: [String: Set<String>]]()
    private var recurrenceByLine = [String: SessionLineRecurrence]()
    // Compatibility only. Recurrence across unrelated panes/apps does not
    // establish that text is UI: source code and research notes recur too.
    private let legacySessionBlacklist: Bool

    public init(legacySessionBlacklist: Bool = false) {
        self.legacySessionBlacklist = legacySessionBlacklist
    }

    public mutating func project(
        surfaceKey: String,
        bundleIdentifier: String,
        windowTitle: String,
        observedContent: String,
        lines: [ReadOCRLineEvidence],
        allowJoinedLineScaffolding: Bool = false,
        removeClippedOuterBoundaryLines: Bool = false,
        additionalRemovalReasons: [Int: String] = [:]
    ) -> ReadSemanticContentProjection {
        guard !lines.isEmpty else {
            return ReadSemanticContentProjection(content: observedContent, removed: [])
        }
        let prior = seenBySurface[surfaceKey] ?? [:]
        let clippedOuterBoundaryIndices = removeClippedOuterBoundaryLines
            ? clippedOuterBoundaryLineIndices(lines)
            : []
        var removed = [RemovedReadScaffoldingLine]()
        var retained = [String]()
        for line in lines {
            let normalized = line.normalizedText
            guard !normalized.isEmpty else { continue }
            if allowJoinedLineScaffolding,
               let suffix = knownScaffoldingSuffix(
                bundleIdentifier: bundleIdentifier,
                line: line
            ) {
                if !suffix.retained.isEmpty { retained.append(suffix.retained) }
                removed.append(RemovedReadScaffoldingLine(
                    line: ReadOCRLineEvidence(
                        index: line.index,
                        text: suffix.removed,
                        confidence: line.confidence,
                        x: line.x, y: line.y,
                        width: line.width, height: line.height
                    ),
                    reason: suffix.reason,
                    supportingDistinctContentStateCount: 0,
                    supportingDistinctWindowCount: 0
                ))
                continue
            }
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
            if let reason = additionalRemovalReasons[line.index]
                ?? (clippedOuterBoundaryIndices.contains(line.index)
                    ? "geometrically_clipped_outer_boundary_line"
                    : nil) {
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
            let applicationContentStateSupport =
                bottomContentStatesByBundle[bundleIdentifier]?[normalized]?.count ?? 0
            let recurrence = recurrenceByLine[recurrenceText(normalized)]
            let sessionContentStateSupport =
                recurrence?.centralContentStates.count ?? 0
            let sessionSurfaceSupport = recurrence?.surfaceKeys.count ?? 0
            let stableAcrossWindows = contentStateSupport >= 4
                && windowSupport >= 3
                && isPeripheral(line)
            // A durable application footer normally remains under one window
            // title. Requiring title diversity made those strongest repeated
            // controls impossible to classify. Eight distinct central states
            // at the same bottom geometry is stronger evidence than the title
            // heuristic, while deliberately not applying to top-of-document
            // text such as authors or headings.
            let stableSameWindowBottomChrome = contentStateSupport >= 8
                && windowSupport >= 1
                && isBottomPeripheral(line)
            let stableApplicationBottomChrome = applicationContentStateSupport >= 16
                && isBottomPeripheral(line)
            // Kept solely to reproduce reducers <=25, never new reductions.
            let stableSessionInterfaceText = legacySessionBlacklist
                && sessionContentStateSupport >= 32
                && sessionSurfaceSupport >= 4
            if (stableAcrossWindows || stableSameWindowBottomChrome
                    || stableApplicationBottomChrome
                    || stableSessionInterfaceText),
               line.confidence >= 0.80,
               (supportingPlacement != nil || stableApplicationBottomChrome
                    || stableSessionInterfaceText) {
                removed.append(RemovedReadScaffoldingLine(
                    line: line,
                    reason: stableAcrossWindows ? "stable_peripheral_interface_text"
                        : stableSameWindowBottomChrome
                            ? "stable_same_window_bottom_interface_text"
                            : stableApplicationBottomChrome
                                ? "stable_application_bottom_interface_text"
                                : "stable_session_interface_text",
                    supportingDistinctContentStateCount: max(
                        max(contentStateSupport, applicationContentStateSupport),
                        sessionContentStateSupport
                    ),
                    supportingDistinctWindowCount: max(
                        windowSupport, sessionSurfaceSupport
                    )
                ))
            } else {
                retained.append(line.text)
            }
        }
        observe(
            surfaceKey: surfaceKey,
            bundleIdentifier: bundleIdentifier,
            windowTitle: windowTitle,
            lines: lines
        )
        return ReadSemanticContentProjection(
            content: retained.joined(separator: "\n"),
            removed: removed
        )
    }

    /// Adds one observation to the recurrence evidence without deriving a
    /// projection. Offline reduction uses this to prove stable interface chrome
    /// over the immutable session before projecting any individual READ.
    public mutating func observe(
        surfaceKey: String,
        bundleIdentifier: String,
        windowTitle: String,
        lines: [ReadOCRLineEvidence]
    ) {
        let centralState = centralContentFingerprint(lines)
        remember(
            surfaceKey: surfaceKey,
            windowTitle: windowTitle,
            centralContentState: centralState,
            lines: lines
        )
        guard let centralState, !bundleIdentifier.isEmpty else { return }
        for line in lines where legacySessionBlacklist && line.confidence >= 0.80 {
            let normalized = recurrenceText(line.normalizedText)
            guard !normalized.isEmpty else { continue }
            var recurrence = recurrenceByLine[normalized]
                ?? SessionLineRecurrence(
                    centralContentStates: [], surfaceKeys: []
                )
            if recurrence.centralContentStates.count < 64 {
                recurrence.centralContentStates.insert(centralState)
            }
            if recurrence.surfaceKeys.count < 16 {
                recurrence.surfaceKeys.insert(surfaceKey)
            }
            recurrenceByLine[normalized] = recurrence
        }
        var bundle = bottomContentStatesByBundle[bundleIdentifier] ?? [:]
        for line in lines where line.confidence >= 0.80 && isBottomPeripheral(line) {
            let normalized = line.normalizedText
            guard !normalized.isEmpty else { continue }
            var states = bundle[normalized] ?? []
            if states.count < 32 { states.insert(centralState) }
            bundle[normalized] = states
        }
        bottomContentStatesByBundle[bundleIdentifier] = bundle
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

/// Vision can return text for the few visible pixels of a line cut by the
/// exact top or bottom of an OCR region. Reject only a line that both touches
/// that boundary and is materially shorter than the ordinary lines in the
/// same observation. A fully visible first or last line remains authoritative.
private func clippedOuterBoundaryLineIndices(
    _ lines: [ReadOCRLineEvidence]
) -> Set<Int> {
    let referenceHeights = lines.compactMap { line -> Double? in
        guard line.confidence >= 0.80,
              line.normalizedText.count >= 8,
              let height = line.height,
              height > 0 else { return nil }
        return height
    }.sorted()
    guard !referenceHeights.isEmpty else { return [] }
    let middle = referenceHeights.count / 2
    let median = referenceHeights.count.isMultiple(of: 2)
        ? (referenceHeights[middle - 1] + referenceHeights[middle]) / 2
        : referenceHeights[middle]
    let boundaryTolerance = 0.002
    return Set(lines.compactMap { line -> Int? in
        guard let y = line.y, let height = line.height else { return nil }
        let touchesBoundary = y <= boundaryTolerance
            || y + height >= 1 - boundaryTolerance
        // Exact-edge geometry alone is not enough: compact but fully visible
        // headings and status lines can also touch the crop boundary. A lone
        // observation is conclusive only when Vision itself reports low
        // confidence. High-confidence clipping requires the paired-sensor
        // disagreement rule in semantic v19.
        guard touchesBoundary,
              line.confidence < 0.80,
              height <= median * 0.80 else { return nil }
        return line.index
    })
}

/// Removes only application controls whose meaning is known independently of
/// session recurrence. This is used when deciding whether two sensor pathways
/// observed the same state before the full semantic projection is constructed.
public func readContentRemovingKnownInterfaceLines(
    bundleIdentifier: String,
    content: String
) -> String {
    content.split(whereSeparator: \.isNewline).enumerated().compactMap {
        index, value in
        let text = String(value).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        let line = ReadOCRLineEvidence(index: index, text: text, confidence: 1)
        return knownScaffoldingReason(
            bundleIdentifier: bundleIdentifier,
            line: line
        ) == nil ? text : nil
    }.joined(separator: "\n")
}

private func recurrenceText(_ normalizedText: String) -> String {
    guard let first = normalizedText.first,
          first == "•" || first == "›" || first == "»" || first == ">" else {
        return normalizedText
    }
    return normalizedText.dropFirst()
        .trimmingCharacters(in: .whitespacesAndNewlines)
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

/// The same conservative test for a novelty projection composed entirely of
/// several disconnected spinner/caret glyphs. A mixed projection containing
/// even one normal-sized or longer line remains semantic content.
public func isolatedReadNoveltyMicroglyphs(
    _ novelty: String,
    lines: [ReadOCRLineEvidence],
    excludingLineIndices: Set<Int> = []
) -> [ReadOCRLineEvidence]? {
    let fragments: [String] = novelty.split(whereSeparator: \.isNewline).compactMap {
        let value = String($0).precomposedStringWithCanonicalMapping
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty,
              value.contains(where: { $0.isLetter || $0.isNumber }) else {
            return nil
        }
        return value
    }
    guard !fragments.isEmpty else { return nil }
    var claimed = excludingLineIndices
    var result = [ReadOCRLineEvidence]()
    for fragment in fragments {
        guard (1...2).contains(fragment.count) else { return nil }
        let matches = lines.filter { line in
            guard !claimed.contains(line.index),
                  line.normalizedText == fragment,
                  let width = line.width, let height = line.height else { return false }
            return width <= 0.05 && height <= 0.06
        }
        guard matches.count == 1, let match = matches.first else { return nil }
        claimed.insert(match.index)
        result.append(match)
    }
    return result
}

private func knownScaffoldingReason(
    bundleIdentifier: String,
    line: ReadOCRLineEvidence
) -> String? {
    let text = line.normalizedText
    let lowered = text.lowercased()
    if text.count <= 3,
       text.contains(where: { $0.isLetter || $0.isNumber }),
       let x = line.x, let width = line.width, let height = line.height,
       width <= 0.025, height <= 0.04,
       x <= 0.002 || x + width >= 0.998 {
        return "clipped_pane_edge_microtext"
    }
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
    let isCodexSurface = bundleIdentifier == "com.openai.codex"
        || bundleIdentifier == "com.microsoft.VSCode"
    guard isCodexSurface else { return nil }
    if lowered.contains("esc to interrupt") {
        return "codex_progress_status"
    }
    if lowered.contains("ctrl + t to view transcript") {
        return "codex_transcript_control"
    }
    if lowered.range(
        of: #"^[•›»>]?\s*[ae]sk codex to do anything$"#,
        options: .regularExpression
    ) != nil {
        return "codex_composer_placeholder"
    }
    if lowered.range(
        of: #"^(working|worked) (?:for|tor) [0-9]+(?:\.[0-9]+)?[smh](?: [0-9]+[smh])?\s*[›»>]?\s*$"#,
        options: .regularExpression
    ) != nil {
        return "codex_progress_status"
    }
    if lowered.range(
        of: #"^[•›»>]?\s*(?:working|worked)(?: for)?\s*\(?[0-9]+(?:\.[0-9]+)?[smh](?:\s+[0-9]+[smh])?(?:\s*[•·]\s*esc to interrupt)?\)?\s*[›»>]?\s*$"#,
        options: .regularExpression
    ) != nil {
        return "codex_progress_status"
    }
    let centerY = line.centerY ?? 1
    if centerY <= 0.25, line.confidence < 0.50, text.count <= 2 {
        return "low_confidence_composer_microtext"
    }
    if centerY <= 0.15,
       lowered == "do anything" || lowered == "po anything"
    {
        return "codex_composer_placeholder"
    }
    guard centerY <= 0.08 else { return nil }
    if lowered.range(
        of: #"^\+\s*[o0©]?$"#,
        options: .regularExpression
    ) != nil {
        return "codex_composer_control"
    }
    if lowered == "approve for me" { return "codex_approval_control" }
    if lowered == "5.6 sol extra high v" { return "codex_model_selector" }
    return nil
}

/// Obsidian sometimes joins its bottom status bar to the final visible note
/// line in one Vision OCR observation. Removing the whole OCR line would lose
/// authored note content; retain the prefix and strip only the proven status
/// suffix. Geometry is required so identical prose elsewhere is untouched.
private func knownScaffoldingSuffix(
    bundleIdentifier: String,
    line: ReadOCRLineEvidence
) -> (retained: String, removed: String, reason: String)? {
    guard bundleIdentifier == "md.obsidian",
          (line.centerY ?? 1) <= 0.04 else { return nil }
    let text = line.normalizedText
    let pattern = #"\s*\(?\s*[0-9]+\s+backlinks?(?:\s+[^\s,]+)?\s+[0-9,]+\s+words\s+[0-9,]+\s+characters\s*$"#
    guard let range = text.range(of: pattern, options: [
        .regularExpression, .caseInsensitive,
    ]) else { return nil }
    let retained = String(text[..<range.lowerBound])
        .trimmingCharacters(in: .whitespacesAndNewlines)
    let removed = String(text[range])
        .trimmingCharacters(in: .whitespacesAndNewlines)
    guard !retained.isEmpty, !removed.isEmpty else { return nil }
    return (retained, removed, "obsidian_joined_footer_status")
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

private func isBottomPeripheral(_ line: ReadOCRLineEvidence) -> Bool {
    guard let centerY = line.centerY else { return false }
    // Vision bounding boxes use a bottom-left origin.
    return centerY <= 0.15
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
