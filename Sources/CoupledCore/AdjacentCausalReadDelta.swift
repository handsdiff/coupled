import Foundation

/// A conservative, order-preserving delta between two complete OCR states.
public struct AdjacentCausalReadDelta: Sendable, Equatable {
    public let emittedContent: String
    public let alignment: String
    public let overlapCharacterCount: Int
    public let currentCharacterCount: Int
    public let lineMatches: [AdjacentCausalReadLineMatch]
    public let tokenMatches: [AdjacentCausalReadTokenMatch]

    public init(
        emittedContent: String,
        alignment: String,
        overlapCharacterCount: Int,
        currentCharacterCount: Int,
        lineMatches: [AdjacentCausalReadLineMatch] = [],
        tokenMatches: [AdjacentCausalReadTokenMatch] = []
    ) {
        self.emittedContent = emittedContent
        self.alignment = alignment
        self.overlapCharacterCount = overlapCharacterCount
        self.currentCharacterCount = currentCharacterCount
        self.lineMatches = lineMatches
        self.tokenMatches = tokenMatches
    }
}

public struct AdjacentCausalReadTokenMatch: Sendable, Equatable {
    public let previousTokenIndex: Int
    public let currentTokenIndex: Int
    public let previousText: String
    public let currentText: String
    public let kind: String

    public init(
        previousTokenIndex: Int,
        currentTokenIndex: Int,
        previousText: String,
        currentText: String,
        kind: String
    ) {
        self.previousTokenIndex = previousTokenIndex
        self.currentTokenIndex = currentTokenIndex
        self.previousText = previousText
        self.currentText = currentText
        self.kind = kind
    }
}

public struct AdjacentCausalReadLineMatch: Sendable, Equatable {
    public let previousLineIndex: Int
    public let currentLineIndex: Int
    public let previousText: String
    public let currentText: String
    public let editDistance: Int
    public let similarity: Double

    public init(
        previousLineIndex: Int,
        currentLineIndex: Int,
        previousText: String,
        currentText: String,
        editDistance: Int,
        similarity: Double
    ) {
        self.previousLineIndex = previousLineIndex
        self.currentLineIndex = currentLineIndex
        self.previousText = previousText
        self.currentText = currentText
        self.editDistance = editDistance
        self.similarity = similarity
    }
}

private struct ReadDeltaCandidate {
    let ranges: [Range<Int>]
    let alignment: String
}

public func adjacentCausalReadDelta(
    previous: String,
    current: String
) -> AdjacentCausalReadDelta? {
    let prior = NormalizedReadText(previous)
    let next = NormalizedReadText(current)
    guard !prior.characters.isEmpty, !next.characters.isEmpty else { return nil }
    if prior.characters == next.characters {
        return AdjacentCausalReadDelta(
            emittedContent: "",
            alignment: "exact_state",
            overlapCharacterCount: next.characters.count,
            currentCharacterCount: next.characters.count
        )
    }

    let prefix = commonPrefixCount(prior.characters, next.characters)
    let maximumSuffix = min(
        prior.characters.count - prefix,
        next.characters.count - prefix
    )
    let suffix = commonSuffixCount(
        prior.characters,
        next.characters,
        maximum: maximumSuffix
    )
    var candidates = [ReadDeltaCandidate]()
    let boundaryRanges = [
        prefix > 0 ? 0..<prefix : nil,
        suffix > 0 ? (next.characters.count - suffix)..<next.characters.count : nil,
    ].compactMap { $0 }
    if !boundaryRanges.isEmpty {
        candidates.append(ReadDeltaCandidate(
            ranges: boundaryRanges,
            alignment: "same_position_boundaries"
        ))
    }
    appendEdgeCandidates(prior: prior, next: next, to: &candidates)

    let priorInCurrent = occurrenceOffsets(
        needle: prior.characters,
        haystack: next.characters,
        limit: 2
    )
    if priorInCurrent.count == 1 {
        let start = priorInCurrent[0]
        candidates.append(ReadDeltaCandidate(
            ranges: [start..<(start + prior.characters.count)],
            alignment: "prior_state_inside_current"
        ))
    }
    let currentInPrior = occurrenceOffsets(
        needle: next.characters,
        haystack: prior.characters,
        limit: 2
    )
    if currentInPrior.count == 1 {
        candidates.append(ReadDeltaCandidate(
            ranges: [0..<next.characters.count],
            alignment: "current_state_inside_prior"
        ))
    }
    return selectReadDelta(candidates: candidates, prior: prior, next: next)
}

/// v16's deliberately narrower primitive. Only exact normalized state or a
/// contiguous suffix/prefix overlap is removable. Internal or same-position
/// matches remain part of the complete current semantic READ.
public func adjacentCausalReadEdgeDelta(
    previous: String,
    current: String
) -> AdjacentCausalReadDelta? {
    let prior = NormalizedReadText(previous)
    let next = NormalizedReadText(current)
    guard !prior.characters.isEmpty, !next.characters.isEmpty else { return nil }
    if prior.characters == next.characters {
        return AdjacentCausalReadDelta(
            emittedContent: "",
            alignment: "exact_state",
            overlapCharacterCount: next.characters.count,
            currentCharacterCount: next.characters.count
        )
    }

    var candidates = [ReadDeltaCandidate]()

    appendEdgeCandidates(prior: prior, next: next, to: &candidates)
    return selectReadDelta(candidates: candidates, prior: prior, next: next)
}

/// A high-precision adjacent-state delta whose emitted content is always one
/// contiguous span from the authoritative current observation. Unlike the
/// older reflow matcher, it never subtracts several disconnected token runs.
public func adjacentCausalReadContiguousDelta(
    previous: String,
    current: String
) -> AdjacentCausalReadDelta? {
    let prior = NormalizedReadText(previous)
    let next = NormalizedReadText(current)
    guard !prior.characters.isEmpty, !next.characters.isEmpty else { return nil }
    if prior.characters == next.characters {
        return AdjacentCausalReadDelta(
            emittedContent: "",
            alignment: "exact_state",
            overlapCharacterCount: next.characters.count,
            currentCharacterCount: next.characters.count
        )
    }

    var candidates = [ReadDeltaCandidate]()
    let prefix = commonPrefixCount(prior.characters, next.characters)
    let maximumSuffix = min(
        prior.characters.count - prefix,
        next.characters.count - prefix
    )
    let suffix = commonSuffixCount(
        prior.characters, next.characters, maximum: maximumSuffix
    )
    let boundaries = [
        prefix > 0 ? 0..<prefix : nil,
        suffix > 0 ? (next.characters.count - suffix)..<next.characters.count : nil,
    ].compactMap { $0 }
    if !boundaries.isEmpty {
        candidates.append(ReadDeltaCandidate(
            ranges: boundaries,
            alignment: "same_position_contiguous_change"
        ))
    }
    appendEdgeCandidates(prior: prior, next: next, to: &candidates)

    // A current state wholly contained in its predecessor contains no new
    // model-facing text. The inverse is intentionally not used: removing an
    // internal prior state from a larger current state would create two
    // disconnected fragments.
    let currentInPrior = occurrenceOffsets(
        needle: next.characters, haystack: prior.characters, limit: 2
    )
    if currentInPrior.count == 1 {
        candidates.append(ReadDeltaCandidate(
            ranges: [0..<next.characters.count],
            alignment: "current_state_inside_prior"
        ))
    }
    if let exact = selectReadDelta(candidates: candidates, prior: prior, next: next) {
        let repeatedFraction = Double(exact.overlapCharacterCount)
            / Double(max(1, exact.currentCharacterCount))
        if exact.alignment != "same_position_contiguous_change"
            || repeatedFraction >= 0.60 {
            return exact
        }
    }

    return adjacentCausalReadContiguousLineDelta(
        previous: previous, current: current
    )
}

/// OCR-tolerant fallback for one contiguous changed line block. Matches must
/// remain ordered and each side of the changed block must have one consistent
/// line displacement. Disconnected residues are rejected.
public func adjacentCausalReadContiguousLineDelta(
    previous: String,
    current: String
) -> AdjacentCausalReadDelta? {
    let priorLines = normalizedReadLines(previous)
    let currentLines = normalizedReadLines(current)
    guard !priorLines.isEmpty, !currentLines.isEmpty else { return nil }
    let plans = orderedReadLineAlignment(previous: priorLines, current: currentLines)
    guard plans.count == 1, let plan = plans.first, !plan.matches.isEmpty else {
        return nil
    }
    let fuzzy = plan.matches.filter { $0.previousText != $0.currentText }
    let exact = plan.matches.filter { $0.previousText == $0.currentText }
    if !fuzzy.isEmpty {
        guard !exact.isEmpty,
              exact.reduce(0, { $0 + $1.currentText.count }) >= 24 else {
            return nil
        }
    }
    let matchedCurrent = Set(plan.matches.map(\.currentLineIndex))
    let unmatched = currentLines.indices.filter { !matchedCurrent.contains($0) }
    if unmatched.isEmpty {
        return AdjacentCausalReadDelta(
            emittedContent: "",
            alignment: "ocr_tolerant_equivalent_lines",
            overlapCharacterCount: plan.matchedCharacters,
            currentCharacterCount: currentLines.reduce(0) { $0 + $1.text.count },
            lineMatches: plan.matches
        )
    }
    guard unmatched.last! - unmatched.first! + 1 == unmatched.count else {
        return nil
    }
    let changed = unmatched.first!...unmatched.last!
    let before = plan.matches.filter { $0.currentLineIndex < changed.lowerBound }
    let after = plan.matches.filter { $0.currentLineIndex > changed.upperBound }
    func hasOneOffset(_ matches: [AdjacentCausalReadLineMatch]) -> Bool {
        Set(matches.map { $0.currentLineIndex - $0.previousLineIndex }).count <= 1
    }
    guard hasOneOffset(before), hasOneOffset(after) else { return nil }

    let overlap = plan.matches.reduce(0) { $0 + $1.currentText.count }
    let previousCount = priorLines.reduce(0) { $0 + $1.text.count }
    let currentCount = currentLines.reduce(0) { $0 + $1.text.count }
    let longest = plan.matches.map { $0.currentText.count }.max() ?? 0
    let smaller = min(previousCount, currentCount)
    let currentRepeatedFraction = Double(overlap) / Double(max(1, currentCount))
    let confident = overlap >= 64 && currentRepeatedFraction >= 0.35
        && (longest >= 20 || Double(overlap) / Double(max(1, smaller)) >= 0.35)
    guard confident else { return nil }
    return AdjacentCausalReadDelta(
        emittedContent: currentLines[changed].map(\.raw).joined(separator: "\n"),
        alignment: "ocr_tolerant_contiguous_lines",
        overlapCharacterCount: overlap,
        currentCharacterCount: currentCount,
        lineMatches: plan.matches
    )
}

/// A conservative second-stage matcher for adjacent OCR states. Exact
/// character overlap remains preferable because it can retain a completion
/// within one OCR line. When that fails, this aligns whole lines in order and
/// tolerates at most two OCR-like character changes in a line. Fuzzy matches
/// require substantial exact line context; a lone similar line is never enough
/// to suppress a current observation.
public func adjacentCausalReadTolerantLineDelta(
    previous: String,
    current: String
) -> AdjacentCausalReadDelta? {
    if let exact = adjacentCausalReadEdgeDelta(previous: previous, current: current) {
        return exact
    }
    let priorLines = normalizedReadLines(previous)
    let currentLines = normalizedReadLines(current)
    guard !priorLines.isEmpty, !currentLines.isEmpty else { return nil }

    let plans = orderedReadLineAlignment(previous: priorLines, current: currentLines)
    guard plans.count == 1, let plan = plans.first, !plan.matches.isEmpty else {
        return nil
    }
    let fuzzy = plan.matches.filter { $0.previousText != $0.currentText }
    let exact = plan.matches.filter { $0.previousText == $0.currentText }
    // OCR tolerance is intentionally unavailable without strong exact context.
    // This prevents one subtly changed sentence from being treated as an OCR
    // duplicate merely because its edit distance is small.
    if !fuzzy.isEmpty {
        guard !exact.isEmpty,
              exact.reduce(0, { $0 + $1.currentText.count }) >= 24 else {
            return nil
        }
    }
    let matchedCurrent = Set(plan.matches.map(\.currentLineIndex))
    let overlap = plan.matches.reduce(0) { $0 + $1.currentText.count }
    let previousCount = priorLines.reduce(0) { $0 + $1.text.count }
    let currentCount = currentLines.reduce(0) { $0 + $1.text.count }
    let longest = plan.matches.map { $0.currentText.count }.max() ?? 0
    let smaller = min(previousCount, currentCount)
    let confident = overlap == currentCount
        ? overlap >= 12
        : overlap >= 64
            && (longest >= 20 || Double(overlap) / Double(max(1, smaller)) >= 0.35)
    guard confident else { return nil }
    let emitted = currentLines.enumerated().compactMap { index, line in
        matchedCurrent.contains(index) ? nil : line.raw
    }.joined(separator: "\n")
    return AdjacentCausalReadDelta(
        emittedContent: emitted,
        alignment: "ocr_tolerant_ordered_lines",
        overlapCharacterCount: overlap,
        currentCharacterCount: currentCount,
        lineMatches: plan.matches
    )
}

/// A reflow-tolerant comparison of OCR text. Newlines are sensor layout, not
/// semantic boundaries, so matching operates over ordered tokens and removes
/// only the corresponding ranges in the current observation. OCR-like token
/// changes are accepted only inside substantial exact context; tokens carrying
/// digits must match exactly so a real version or quantity change is retained.
public func adjacentCausalReadReflowDelta(
    previous: String,
    current: String
) -> AdjacentCausalReadDelta? {
    if let exact = adjacentCausalReadEdgeDelta(previous: previous, current: current) {
        return exact
    }
    let prior = NormalizedReadText(previous)
    let next = NormalizedReadText(current)
    let priorTokens = readReflowTokens(prior.characters)
    let nextTokens = readReflowTokens(next.characters)
    guard !priorTokens.isEmpty, !nextTokens.isEmpty else { return nil }

    var runs = [ReadReflowRun]()
    for priorIndex in priorTokens.indices {
        for currentIndex in nextTokens.indices {
            guard readReflowTokenMatch(
                priorTokens[priorIndex], nextTokens[currentIndex]
            ) != nil else { continue }
            if priorIndex > 0, currentIndex > 0,
               readReflowTokenMatch(
                priorTokens[priorIndex - 1], nextTokens[currentIndex - 1]
               ) != nil {
                continue
            }
            var priorCursor = priorIndex
            var currentCursor = currentIndex
            var matches = [AdjacentCausalReadTokenMatch]()
            var exactCharacters = 0
            while priorCursor < priorTokens.count,
                  currentCursor < nextTokens.count,
                  let kind = readReflowTokenMatch(
                    priorTokens[priorCursor], nextTokens[currentCursor]
                  ) {
                let priorToken = priorTokens[priorCursor]
                let currentToken = nextTokens[currentCursor]
                matches.append(AdjacentCausalReadTokenMatch(
                    previousTokenIndex: priorCursor,
                    currentTokenIndex: currentCursor,
                    previousText: priorToken.raw,
                    currentText: currentToken.raw,
                    kind: kind
                ))
                if kind == "exact" { exactCharacters += currentToken.range.count }
                priorCursor += 1
                currentCursor += 1
            }
            let overlap = matches.reduce(0) {
                $0 + nextTokens[$1.currentTokenIndex].range.count
            }
            let fuzzyCount = matches.filter { $0.kind != "exact" }.count
            guard overlap >= 12,
                  exactCharacters >= (fuzzyCount > 0 ? 24 : 12) else { continue }
            runs.append(ReadReflowRun(
                previousRange: priorIndex..<priorCursor,
                currentRange: currentIndex..<currentCursor,
                overlapCharacters: overlap,
                exactCharacters: exactCharacters,
                matches: matches
            ))
        }
    }
    guard !runs.isEmpty else { return nil }
    runs.sort { left, right in
        if left.overlapCharacters != right.overlapCharacters {
            return left.overlapCharacters > right.overlapCharacters
        }
        if left.exactCharacters != right.exactCharacters {
            return left.exactCharacters > right.exactCharacters
        }
        if left.currentRange.lowerBound != right.currentRange.lowerBound {
            return left.currentRange.lowerBound < right.currentRange.lowerBound
        }
        return left.previousRange.lowerBound < right.previousRange.lowerBound
    }
    var selected = [ReadReflowRun]()
    for run in runs {
        guard selected.allSatisfy({ existing in
            !existing.previousRange.overlaps(run.previousRange)
                && !existing.currentRange.overlaps(run.currentRange)
        }) else { continue }
        selected.append(run)
    }
    selected.sort { $0.currentRange.lowerBound < $1.currentRange.lowerBound }
    guard selected.indices.dropFirst().allSatisfy({ index in
        selected[index - 1].previousRange.upperBound
            <= selected[index].previousRange.lowerBound
    }) else { return nil }
    let matches = selected.flatMap(\.matches)
    let currentRanges = matches.map {
        nextTokens[$0.currentTokenIndex].range
    }
    let overlap = selected.reduce(0) { $0 + $1.overlapCharacters }
    let exactCharacters = selected.reduce(0) { $0 + $1.exactCharacters }
    let fuzzyCount = matches.filter { $0.kind != "exact" }.count
    let priorCharacters = priorTokens.reduce(0) { $0 + $1.range.count }
    let currentCharacters = nextTokens.reduce(0) { $0 + $1.range.count }
    let smaller = min(priorCharacters, currentCharacters)
    guard exactCharacters >= (fuzzyCount > 0 ? 48 : 24),
          overlap >= 24,
          Double(overlap) / Double(max(1, smaller)) >= 0.35 else { return nil }
    return AdjacentCausalReadDelta(
        emittedContent: next.content(excluding: currentRanges),
        alignment: "ocr_tolerant_ordered_tokens",
        overlapCharacterCount: overlap,
        currentCharacterCount: next.characters.count,
        tokenMatches: matches
    )
}

/// Linear-time, order-preserving evidence that two OCR observations mostly
/// describe the same state. The unmatched text is audit evidence only: callers
/// must not serialize its disconnected fragments as a coherent READ.
public func adjacentCausalReadUniqueTokenOverlap(
    previous: String,
    current: String
) -> AdjacentCausalReadDelta? {
    let prior = NormalizedReadText(previous)
    let next = NormalizedReadText(current)
    let priorTokens = readReflowTokens(prior.characters)
    let currentTokens = readReflowTokens(next.characters)
    guard !priorTokens.isEmpty, !currentTokens.isEmpty else { return nil }
    let matches = orderedUniqueExactReadTokenMatches(
        previous: priorTokens, current: currentTokens
    )
    let overlap = matches.reduce(0) { $0 + $1.currentText.count }
    guard matches.count >= 3, overlap >= 24 else { return nil }
    let ranges = matches.map { currentTokens[$0.currentTokenIndex].range }
    return AdjacentCausalReadDelta(
        emittedContent: next.content(excluding: ranges),
        alignment: "ordered_unique_exact_token_overlap",
        overlapCharacterCount: overlap,
        currentCharacterCount: next.characters.count,
        tokenMatches: matches
    )
}

/// Exact-token longest-common-subsequence evidence for the rare case where
/// two near-simultaneous sensors wrap the same pixels very differently. This
/// is intentionally more expensive than the unique-token backbone and should
/// be used only after independent timing/surface evidence establishes that
/// the observations are competing views of one state.
public func adjacentCausalReadTokenLCSEvidence(
    previous: String,
    current: String
) -> AdjacentCausalReadDelta? {
    let prior = NormalizedReadText(previous)
    let next = NormalizedReadText(current)
    let priorTokens = readReflowTokens(prior.characters)
    let currentTokens = readReflowTokens(next.characters)
    guard !priorTokens.isEmpty, !currentTokens.isEmpty else { return nil }
    let matches = orderedExactReadTokenLCSMatches(
        previous: priorTokens, current: currentTokens
    )
    let overlap = matches.reduce(0) { $0 + $1.currentText.count }
    guard matches.count >= 3, overlap >= 24 else { return nil }
    return AdjacentCausalReadDelta(
        emittedContent: next.content(excluding: matches.map {
            currentTokens[$0.currentTokenIndex].range
        }),
        alignment: "exact_token_longest_common_subsequence",
        overlapCharacterCount: overlap,
        currentCharacterCount: next.characters.count,
        tokenMatches: matches
    )
}

private func orderedExactReadTokenLCSMatches(
    previous priorTokens: [ReadReflowToken],
    current currentTokens: [ReadReflowToken]
) -> [AdjacentCausalReadTokenMatch] {
    var lengths = Array(
        repeating: Array(repeating: 0, count: currentTokens.count + 1),
        count: priorTokens.count + 1
    )
    if !priorTokens.isEmpty, !currentTokens.isEmpty {
        for left in stride(from: priorTokens.count - 1, through: 0, by: -1) {
            for right in stride(
                from: currentTokens.count - 1, through: 0, by: -1
            ) {
                if priorTokens[left].normalized == currentTokens[right].normalized {
                    lengths[left][right] = lengths[left + 1][right + 1] + 1
                } else {
                    lengths[left][right] = max(
                        lengths[left + 1][right], lengths[left][right + 1]
                    )
                }
            }
        }
    }
    var left = 0
    var right = 0
    var matches = [AdjacentCausalReadTokenMatch]()
    while left < priorTokens.count, right < currentTokens.count {
        if priorTokens[left].normalized == currentTokens[right].normalized {
            matches.append(AdjacentCausalReadTokenMatch(
                previousTokenIndex: left,
                currentTokenIndex: right,
                previousText: priorTokens[left].raw,
                currentText: currentTokens[right].raw,
                kind: "exact"
            ))
            left += 1
            right += 1
        } else if lengths[left + 1][right] >= lengths[left][right + 1] {
            left += 1
        } else {
            right += 1
        }
    }
    return matches
}

/// Reconstructs the coherent edge exposed by a scroll without treating OCR
/// disagreements inside the overlapping viewport as new content. Direction
/// is established from the complete adjacent panes. The removal frontier is
/// then anchored to the text that was actually exposed to the model from the
/// preceding pane, so a clipped boundary line may become available when it
/// later moves into the stable interior.
public func adjacentCausalReadScrollFrontierDelta(
    previousFull: String,
    previousModelFacing: String,
    current: String
) -> AdjacentCausalReadDelta? {
    let prior = NormalizedReadText(previousFull)
    let next = NormalizedReadText(current)
    guard !prior.characters.isEmpty, !next.characters.isEmpty else { return nil }
    if prior.characters == next.characters {
        return AdjacentCausalReadDelta(
            emittedContent: "",
            alignment: "exact_state",
            overlapCharacterCount: next.characters.count,
            currentCharacterCount: next.characters.count
        )
    }

    enum Direction { case down, up }
    let direction: Direction?
    if let exact = adjacentCausalReadEdgeDelta(
        previous: previousFull, current: current
    ) {
        switch exact.alignment {
        case "prior_suffix_to_current_prefix": direction = .down
        case "prior_prefix_to_current_suffix": direction = .up
        default: direction = nil
        }
    } else {
        let priorTokens = readReflowTokens(prior.characters)
        let currentTokens = readReflowTokens(next.characters)
        let minimumDirectionalDisplacement = max(
            2, min(priorTokens.count, currentTokens.count) / 50
        )
        let phraseDisplacement = exactReadScrollDisplacement(
            previous: prior.characters, current: next.characters
        )
        if let phraseDisplacement,
           phraseDisplacement >= minimumDirectionalDisplacement {
            direction = .down
        } else if let phraseDisplacement,
                  phraseDisplacement <= -minimumDirectionalDisplacement {
            direction = .up
        } else {
            let matches = orderedUniqueExactReadTokenMatches(
                previous: priorTokens, current: currentTokens
            )
            guard matches.count >= 3,
                  matches.reduce(0, { $0 + $1.currentText.count }) >= 24,
                  let first = matches.first, let last = matches.last else {
                return nil
            }
            let priorLeading = first.previousTokenIndex
            let currentLeading = first.currentTokenIndex
            let priorTrailing = max(
                0, priorTokens.count - last.previousTokenIndex - 1
            )
            let currentTrailing = max(
                0, currentTokens.count - last.currentTokenIndex - 1
            )
            let downScore = (priorLeading - currentLeading)
                + (currentTrailing - priorTrailing)
            if downScore >= minimumDirectionalDisplacement {
                direction = .down
            } else if downScore <= -minimumDirectionalDisplacement {
                direction = .up
            } else {
                direction = nil
            }
        }
    }
    guard let direction else { return nil }

    let frontierText = previousModelFacing.isEmpty
        ? previousFull : previousModelFacing
    let frontier = NormalizedReadText(frontierText)
    guard !frontier.characters.isEmpty else { return nil }
    let frontierTokens = readReflowTokens(frontier.characters)
    let currentTokens = readReflowTokens(next.characters)
    guard !frontierTokens.isEmpty, !currentTokens.isEmpty else { return nil }

    let emitted: String
    let anchorMatches: [AdjacentCausalReadTokenMatch]
    switch direction {
    case .down:
        guard let anchor = exactReadFrontierAnchor(
            frontier: frontierTokens, current: currentTokens, fromEnd: true
        ) else { return nil }
        anchorMatches = anchor.matches
        let lower = currentTokens[anchor.currentRange.upperBound - 1].range.upperBound
        emitted = next.content(excluding: [0..<lower])
    case .up:
        guard let anchor = exactReadFrontierAnchor(
            frontier: frontierTokens, current: currentTokens, fromEnd: false
        ) else { return nil }
        anchorMatches = anchor.matches
        let upper = currentTokens[anchor.currentRange.lowerBound].range.lowerBound
        emitted = next.content(excluding: [upper..<next.characters.count])
    }
    return AdjacentCausalReadDelta(
        emittedContent: emitted,
        alignment: direction == .down
            ? "ocr_tolerant_downward_scroll_frontier"
            : "ocr_tolerant_upward_scroll_frontier",
        overlapCharacterCount: anchorMatches.reduce(0) {
            $0 + $1.currentText.count
        },
        currentCharacterCount: next.characters.count,
        tokenMatches: anchorMatches
    )
}

/// Infer viewport displacement from repeated exact phrases rather than from
/// isolated OCR tokens. A five-token phrase is long enough to resist common
/// words and short enough to survive line wrapping. The dominant displacement
/// must explain at least three phrases and sixty percent of all unique phrase
/// matches; otherwise direction remains unknown.
private func exactReadScrollDisplacement(
    previous: [Character],
    current: [Character]
) -> Int? {
    let priorTokens = readReflowTokens(previous).map(\.normalized)
    let currentTokens = readReflowTokens(current).map(\.normalized)
    let phraseLength = 5
    guard priorTokens.count >= phraseLength,
          currentTokens.count >= phraseLength else { return nil }

    func phrasePositions(_ tokens: [String]) -> [String: [Int]] {
        var positions = [String: [Int]]()
        for start in 0...(tokens.count - phraseLength) {
            let phrase = tokens[start..<(start + phraseLength)]
                .joined(separator: "\u{1F}")
            positions[phrase, default: []].append(start)
        }
        return positions
    }
    let priorPositions = phrasePositions(priorTokens)
    let currentPositions = phrasePositions(currentTokens)
    let offsets = priorPositions.compactMap { phrase, prior -> Int? in
        guard prior.count == 1,
              let current = currentPositions[phrase], current.count == 1
        else { return nil }
        return prior[0] - current[0]
    }.sorted()
    guard offsets.count >= 3 else { return nil }
    let median = offsets[offsets.count / 2]
    let consistent = offsets.filter { abs($0 - median) <= 2 }.count
    guard consistent >= 3, consistent * 5 >= offsets.count * 3 else {
        return nil
    }
    return median
}

private struct ExactReadFrontierAnchor {
    let currentRange: Range<Int>
    let matches: [AdjacentCausalReadTokenMatch]
}

/// Exact unique tokens provide a linear-time displacement backbone. OCR-
/// tolerant matching remains useful for audit, but its quadratic all-pairs
/// search is deliberately not used in the full-corpus sequence reducer.
private func orderedUniqueExactReadTokenMatches(
    previous: [ReadReflowToken],
    current: [ReadReflowToken]
) -> [AdjacentCausalReadTokenMatch] {
    var previousPositions = [String: [Int]]()
    var currentPositions = [String: [Int]]()
    for (index, token) in previous.enumerated()
        where token.normalized.count >= 3 {
        previousPositions[token.normalized, default: []].append(index)
    }
    for (index, token) in current.enumerated()
        where token.normalized.count >= 3 {
        currentPositions[token.normalized, default: []].append(index)
    }
    let pairs = current.enumerated().compactMap { currentIndex, token
        -> (Int, Int, String)? in
        guard previousPositions[token.normalized]?.count == 1,
              currentPositions[token.normalized]?.count == 1,
              let previousIndex = previousPositions[token.normalized]?.first
        else { return nil }
        return (previousIndex, currentIndex, token.raw)
    }
    var result = [AdjacentCausalReadTokenMatch]()
    var lastPrevious = -1
    for pair in pairs where pair.0 > lastPrevious {
        result.append(AdjacentCausalReadTokenMatch(
            previousTokenIndex: pair.0,
            currentTokenIndex: pair.1,
            previousText: previous[pair.0].raw,
            currentText: pair.2,
            kind: "exact"
        ))
        lastPrevious = pair.0
    }
    return result
}

/// Find a short exact phrase at the edge of the prior model-facing frontier.
/// The phrase must occur once in the current pane; this avoids anchoring a
/// scroll on a common repeated phrase elsewhere in the document.
private func exactReadFrontierAnchor(
    frontier: [ReadReflowToken],
    current: [ReadReflowToken],
    fromEnd: Bool
) -> ExactReadFrontierAnchor? {
    // The stable-interior crop can cut an entire wrapped line or short bullet
    // from the end of the preceding observation. Search modestly inward for a
    // unique exact phrase instead of requiring the literal last two tokens to
    // survive OCR. This deliberately prefers omitting an uncertain clipped
    // edge over replaying the already-read viewport.
    let maximumSkippedEdgeTokens = min(32, max(0, frontier.count - 1))
    let maximumPhraseTokens = min(8, frontier.count)
    for skipped in 0...maximumSkippedEdgeTokens {
        let edge = fromEnd ? frontier.count - skipped : skipped
        for length in stride(from: maximumPhraseTokens, through: 3, by: -1) {
            let frontierRange: Range<Int>
            if fromEnd {
                guard edge >= length else { continue }
                frontierRange = (edge - length)..<edge
            } else {
                guard edge + length <= frontier.count else { continue }
                frontierRange = edge..<(edge + length)
            }
            let phrase = frontier[frontierRange].map(\.normalized)
            var occurrences = [Range<Int>]()
            if current.count >= phrase.count {
                for start in 0...(current.count - phrase.count) where
                    Array(current[start..<(start + phrase.count)]).map(\.normalized)
                        == phrase {
                    occurrences.append(start..<(start + phrase.count))
                    if occurrences.count > 1 { break }
                }
            }
            guard occurrences.count == 1, let currentRange = occurrences.first
            else { continue }
            let characters = current[currentRange].reduce(0) {
                $0 + $1.raw.count
            }
            guard characters >= 12 else { continue }
            var resolvedFrontierRange = frontierRange
            var resolvedCurrentRange = currentRange
            if fromEnd, edge < frontier.count {
                let trailing = edge..<frontier.count
                let currentTrailing = currentRange.upperBound
                    ..< (currentRange.upperBound + trailing.count)
                if currentTrailing.upperBound <= current.count,
                   zip(trailing, currentTrailing).allSatisfy({
                       readReflowTokenMatch(frontier[$0], current[$1]) != nil
                   }) {
                    resolvedFrontierRange = frontierRange.lowerBound..<frontier.count
                    resolvedCurrentRange = currentRange.lowerBound
                        ..< currentTrailing.upperBound
                }
            } else if !fromEnd, edge > 0,
                      currentRange.lowerBound >= edge {
                let leading = 0..<edge
                let currentLeading = (currentRange.lowerBound - edge)
                    ..< currentRange.lowerBound
                if zip(leading, currentLeading).allSatisfy({
                    readReflowTokenMatch(frontier[$0], current[$1]) != nil
                }) {
                    resolvedFrontierRange = 0..<frontierRange.upperBound
                    resolvedCurrentRange = currentLeading.lowerBound
                        ..< currentRange.upperBound
                }
            }
            let matches = zip(resolvedFrontierRange, resolvedCurrentRange).map {
                previousIndex, currentIndex in
                AdjacentCausalReadTokenMatch(
                    previousTokenIndex: previousIndex,
                    currentTokenIndex: currentIndex,
                    previousText: frontier[previousIndex].raw,
                    currentText: current[currentIndex].raw,
                    kind: "exact"
                )
            }
            return ExactReadFrontierAnchor(
                currentRange: resolvedCurrentRange, matches: matches
            )
        }
    }


    // OCR often corrupts one word or changes wrapping at the exact viewport
    // edge. Fall back to several ordered, unique exact tokens close to the
    // prior frontier. This deliberately accepts a small omission instead of
    // replaying the whole overlapping viewport.
    let unique = orderedUniqueExactReadTokenMatches(
        previous: frontier, current: current
    )
    let edgeLimit = max(3, frontier.count / 4)
    func nearFrontierEdge(
        _ matches: [AdjacentCausalReadTokenMatch]
    ) -> [AdjacentCausalReadTokenMatch] {
        matches.filter { match in
            fromEnd
                ? match.previousTokenIndex >= frontier.count - edgeLimit
                : match.previousTokenIndex < edgeLimit
        }
    }
    let nearEdge = nearFrontierEdge(unique)
    guard nearEdge.count >= 3 else { return nil }
    let selected = fromEnd ? Array(nearEdge.suffix(6)) : Array(nearEdge.prefix(6))
    guard selected.reduce(0, { $0 + $1.currentText.count }) >= 24,
          let first = selected.first, let last = selected.last else { return nil }
    return ExactReadFrontierAnchor(
        currentRange: first.currentTokenIndex..<(last.currentTokenIndex + 1),
        matches: selected
    )
}

public func normalizedReadOCRSimilarity(
    _ first: String,
    _ second: String,
    minimum: Double = 0
) -> Double {
    let left = Array(normalizedReadOCRComparisonText(first))
    let right = Array(normalizedReadOCRComparisonText(second))
    let longest = max(left.count, right.count)
    guard longest > 0 else { return 1 }
    let maximumDistance = minimum > 0
        ? Int(ceil(Double(longest) * (1 - minimum)))
        : longest
    let distance = boundedLevenshteinDistance(
        left, right, maximum: maximumDistance
    )
    guard distance <= longest else { return 0 }
    return max(0, 1 - Double(distance) / Double(longest))
}

private func normalizedReadOCRComparisonText(_ value: String) -> String {
    fuzzyReadLine(
        value.split(whereSeparator: \.isWhitespace).joined(separator: " ")
    )
}

private struct ReadReflowToken {
    let raw: String
    let normalized: String
    let range: Range<Int>
}

private struct ReadReflowRun {
    let previousRange: Range<Int>
    let currentRange: Range<Int>
    let overlapCharacters: Int
    let exactCharacters: Int
    let matches: [AdjacentCausalReadTokenMatch]
}

private func readReflowTokens(_ characters: [Character]) -> [ReadReflowToken] {
    var result = [ReadReflowToken]()
    var start: Int?
    for index in 0...characters.count {
        let isBoundary = index == characters.count || characters[index] == " "
        if isBoundary, let lower = start {
            let raw = String(characters[lower..<index])
            let normalized = normalizeReadReflowToken(raw)
            if !normalized.isEmpty {
                result.append(ReadReflowToken(
                    raw: raw, normalized: normalized, range: lower..<index
                ))
            }
            start = nil
        } else if !isBoundary, start == nil {
            start = index
        }
    }
    return result
}

private func normalizeReadReflowToken(_ value: String) -> String {
    let normalized = fuzzyReadLine(value).trimmingCharacters(
        in: CharacterSet.alphanumerics.inverted
    )
    return normalized
}

private func readReflowTokenMatch(
    _ previous: ReadReflowToken,
    _ current: ReadReflowToken
) -> String? {
    if previous.normalized == current.normalized { return "exact" }
    if readOCRConfusableGlyphEquivalent(
        previous.normalized, current.normalized
    ) {
        return "ocr_glyph"
    }
    let semanticPrefixes = ["un", "non", "dis"]
    if semanticPrefixes.contains(where: {
        previous.normalized == $0 + current.normalized
            || current.normalized == $0 + previous.normalized
    }) {
        return nil
    }
    let prior = Array(previous.normalized)
    let next = Array(current.normalized)
    guard !prior.isEmpty, !next.isEmpty,
          prior.filter(\.isNumber) == next.filter(\.isNumber),
          prior.allSatisfy({ !$0.isNumber }),
          next.allSatisfy({ !$0.isNumber }) else { return nil }
    let shorter = min(prior.count, next.count)
    let longer = max(prior.count, next.count)
    if shorter >= 3,
       longer - shorter <= 6,
       Double(shorter) / Double(longer) >= 0.45 {
        let shorterToken = prior.count <= next.count ? prior : next
        let longerPrefix = Array((prior.count > next.count ? prior : next)
            .prefix(shorter))
        if shorterToken == longerPrefix
            || boundedLevenshteinDistance(
                shorterToken, longerPrefix, maximum: 1
            ) <= 1 {
            return "ocr_prefix"
        }
    }
    guard shorter >= 4, abs(prior.count - next.count) <= 2 else { return nil }
    let distance = boundedLevenshteinDistance(prior, next, maximum: 2)
    guard distance <= 2,
          1 - Double(distance) / Double(longer) >= 0.80 else { return nil }
    return "ocr_edit"
}

/// A digit is never allowed to drift to another digit. This recognizes only
/// the two common OCR glyph confusions inside otherwise identical tokens, so
/// `phase1` and `phasel` can reconcile while `v14` and `v15` remain distinct.
private func readOCRConfusableGlyphEquivalent(
    _ previous: String,
    _ current: String
) -> Bool {
    let left = Array(previous)
    let right = Array(current)
    guard left.count == right.count, left.count >= 4 else { return false }
    var differences = 0
    for index in left.indices where left[index] != right[index] {
        let pair = Set([left[index], right[index]])
        guard pair == Set<Character>(["1", "l"])
            || pair == Set<Character>(["0", "o"]) else { return false }
        differences += 1
    }
    return differences == 1
}

private struct NormalizedReadLine {
    let raw: String
    let text: String
    let fuzzyText: String
}

private struct ReadLineAlignmentPlan {
    let matchedCharacters: Int
    let exactCharacters: Int
    let editDistance: Int
    let matches: [AdjacentCausalReadLineMatch]

    var currentLineSignature: [Int] { matches.map(\.currentLineIndex) }
}

private func normalizedReadLines(_ value: String) -> [NormalizedReadLine] {
    value.split(whereSeparator: \.isNewline).compactMap { rawLine in
        let raw = String(rawLine).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !raw.isEmpty else { return nil }
        let text = String(NormalizedReadText(raw).characters)
        return NormalizedReadLine(raw: raw, text: text, fuzzyText: fuzzyReadLine(text))
    }
}

private func fuzzyReadLine(_ value: String) -> String {
    String(value.lowercased().map { character in
        switch character {
        case "\u{2010}", "\u{2011}", "\u{2012}", "\u{2013}", "\u{2014}",
             "\u{2212}":
            return "-"
        case "\u{2018}", "\u{2019}":
            return "'"
        case "\u{201C}", "\u{201D}":
            return "\""
        default:
            return character
        }
    })
}

private func readLineMatch(
    previous: NormalizedReadLine,
    current: NormalizedReadLine,
    previousIndex: Int,
    currentIndex: Int
) -> AdjacentCausalReadLineMatch? {
    if previous.text == current.text {
        return AdjacentCausalReadLineMatch(
            previousLineIndex: previousIndex,
            currentLineIndex: currentIndex,
            previousText: previous.text,
            currentText: current.text,
            editDistance: 0,
            similarity: 1
        )
    }
    let prior = Array(previous.fuzzyText)
    let next = Array(current.fuzzyText)
    let maximum = max(prior.count, next.count)
    guard min(prior.count, next.count) >= 12,
          abs(prior.count - next.count) <= 2,
          prior.filter(\.isNumber) == next.filter(\.isNumber) else { return nil }
    let distance = boundedLevenshteinDistance(prior, next, maximum: 2)
    guard distance <= 2 else { return nil }
    let similarity = 1 - Double(distance) / Double(maximum)
    guard similarity >= 0.92 else { return nil }
    return AdjacentCausalReadLineMatch(
        previousLineIndex: previousIndex,
        currentLineIndex: currentIndex,
        previousText: previous.text,
        currentText: current.text,
        editDistance: distance,
        similarity: similarity
    )
}

/// Return every equally optimal alignment when those alignments remove a
/// different set of current lines. The caller rejects such ambiguity.
private func orderedReadLineAlignment(
    previous: [NormalizedReadLine],
    current: [NormalizedReadLine]
) -> [ReadLineAlignmentPlan] {
    let empty = ReadLineAlignmentPlan(
        matchedCharacters: 0, exactCharacters: 0, editDistance: 0, matches: []
    )
    var table = Array(
        repeating: Array(repeating: [empty], count: current.count + 1),
        count: previous.count + 1
    )
    for previousIndex in stride(from: previous.count - 1, through: 0, by: -1) {
        for currentIndex in stride(from: current.count - 1, through: 0, by: -1) {
            var candidates = table[previousIndex + 1][currentIndex]
                + table[previousIndex][currentIndex + 1]
            if let match = readLineMatch(
                previous: previous[previousIndex],
                current: current[currentIndex],
                previousIndex: previousIndex,
                currentIndex: currentIndex
            ) {
                candidates += table[previousIndex + 1][currentIndex + 1].map { tail in
                    ReadLineAlignmentPlan(
                        matchedCharacters: tail.matchedCharacters
                            + match.currentText.count,
                        exactCharacters: tail.exactCharacters
                            + (match.previousText == match.currentText
                                ? match.currentText.count : 0),
                        editDistance: tail.editDistance + match.editDistance,
                        matches: [match] + tail.matches
                    )
                }
            }
            let bestQuality = candidates.map(readLineAlignmentQuality).max {
                readLineAlignmentQualityIsLess($0, $1)
            }!
            var selected = [ReadLineAlignmentPlan]()
            var signatures = Set<[Int]>()
            for candidate in candidates
            where readLineAlignmentQuality(candidate) == bestQuality {
                if signatures.insert(candidate.currentLineSignature).inserted {
                    selected.append(candidate)
                    if selected.count == 2 { break }
                }
            }
            table[previousIndex][currentIndex] = selected
        }
    }
    return table[0][0]
}

private func readLineAlignmentQuality(
    _ value: ReadLineAlignmentPlan
) -> (Int, Int, Int, Int) {
    (
        value.matchedCharacters,
        value.exactCharacters,
        -value.editDistance,
        value.matches.count
    )
}

private func readLineAlignmentQualityIsLess(
    _ left: (Int, Int, Int, Int),
    _ right: (Int, Int, Int, Int)
) -> Bool {
    if left.0 != right.0 { return left.0 < right.0 }
    if left.1 != right.1 { return left.1 < right.1 }
    if left.2 != right.2 { return left.2 < right.2 }
    return left.3 < right.3
}

private func boundedLevenshteinDistance(
    _ left: [Character],
    _ right: [Character],
    maximum: Int
) -> Int {
    guard abs(left.count - right.count) <= maximum else { return maximum + 1 }
    var prior = Array(0...right.count)
    for (leftOffset, character) in left.enumerated() {
        var current = Array(repeating: maximum + 1, count: right.count + 1)
        current[0] = leftOffset + 1
        var rowMinimum = current[0]
        for (rightOffset, other) in right.enumerated() {
            current[rightOffset + 1] = min(
                current[rightOffset] + 1,
                prior[rightOffset + 1] + 1,
                prior[rightOffset] + (character == other ? 0 : 1)
            )
            rowMinimum = min(rowMinimum, current[rightOffset + 1])
        }
        if rowMinimum > maximum { return maximum + 1 }
        prior = current
    }
    return prior[right.count]
}

private func appendEdgeCandidates(
    prior: NormalizedReadText,
    next: NormalizedReadText,
    to candidates: inout [ReadDeltaCandidate]
) {
    let forward = longestSuffixPrefix(
        source: prior.characters,
        prefixOf: next.characters
    )
    if forward > 0 {
        candidates.append(ReadDeltaCandidate(
            ranges: [0..<forward],
            alignment: "prior_suffix_to_current_prefix"
        ))
    }
    let backward = longestSuffixPrefix(
        source: next.characters,
        prefixOf: prior.characters
    )
    if backward > 0 {
        candidates.append(ReadDeltaCandidate(
            ranges: [(next.characters.count - backward)..<next.characters.count],
            alignment: "prior_prefix_to_current_suffix"
        ))
    }
}

private func selectReadDelta(
    candidates: [ReadDeltaCandidate],
    prior: NormalizedReadText,
    next: NormalizedReadText
) -> AdjacentCausalReadDelta? {
    let ranked = candidates.map { candidate in
        let ranges = mergedRanges(candidate.ranges)
        return (
            candidate: ReadDeltaCandidate(
                ranges: ranges,
                alignment: candidate.alignment
            ),
            overlap: ranges.reduce(0) { $0 + $1.count }
        )
    }.filter { candidate in
        confidentReadOverlap(
            overlap: candidate.overlap,
            previousCount: prior.characters.count,
            currentCount: next.characters.count,
            ranges: candidate.candidate.ranges
        )
    }.sorted { left, right in
        if left.overlap != right.overlap { return left.overlap > right.overlap }
        return left.candidate.alignment < right.candidate.alignment
    }
    guard let selected = ranked.first else { return nil }
    if let competing = ranked.dropFirst().first,
       competing.overlap == selected.overlap,
       competing.candidate.ranges != selected.candidate.ranges {
        return nil
    }
    return AdjacentCausalReadDelta(
        emittedContent: next.content(excluding: selected.candidate.ranges),
        alignment: selected.candidate.alignment,
        overlapCharacterCount: selected.overlap,
        currentCharacterCount: next.characters.count
    )
}

private struct NormalizedReadText {
    let rawCharacters: [Character]
    let characters: [Character]
    let rawSpans: [Range<Int>]

    init(_ value: String) {
        rawCharacters = Array(value)
        var normalized = [Character]()
        var spans = [Range<Int>]()
        for (offset, character) in rawCharacters.enumerated() {
            if character.isWhitespace {
                guard !normalized.isEmpty else { continue }
                if normalized.last == " " {
                    let prior = spans.removeLast()
                    spans.append(prior.lowerBound..<(offset + 1))
                } else {
                    normalized.append(" ")
                    spans.append(offset..<(offset + 1))
                }
            } else {
                normalized.append(character)
                spans.append(offset..<(offset + 1))
            }
        }
        if normalized.last == " " {
            normalized.removeLast()
            spans.removeLast()
        }
        characters = normalized
        rawSpans = spans
    }

    func content(excluding normalizedRanges: [Range<Int>]) -> String {
        let rawRanges = mergedRanges(normalizedRanges.compactMap {
            range -> Range<Int>? in
            guard !range.isEmpty,
                  range.lowerBound >= 0,
                  range.upperBound <= rawSpans.count else { return nil }
            return Range(
                uncheckedBounds: (
                    rawSpans[range.lowerBound].lowerBound,
                    rawSpans[range.upperBound - 1].upperBound
                )
            )
        })
        var fragments = [String]()
        var cursor = 0
        for range in rawRanges {
            if cursor < range.lowerBound {
                let fragment = String(rawCharacters[cursor..<range.lowerBound])
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                if fragment.contains(where: { $0.isLetter || $0.isNumber }) {
                    fragments.append(fragment)
                }
            }
            cursor = max(cursor, range.upperBound)
        }
        if cursor < rawCharacters.count {
            let fragment = String(rawCharacters[cursor..<rawCharacters.count])
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if fragment.contains(where: { $0.isLetter || $0.isNumber }) {
                fragments.append(fragment)
            }
        }
        return fragments.joined(separator: "\n")
    }
}

private func commonPrefixCount(_ left: [Character], _ right: [Character]) -> Int {
    var count = 0
    while count < left.count, count < right.count, left[count] == right[count] {
        count += 1
    }
    return count
}

private func commonSuffixCount(
    _ left: [Character],
    _ right: [Character],
    maximum: Int
) -> Int {
    var count = 0
    while count < maximum,
          left[left.count - count - 1] == right[right.count - count - 1] {
        count += 1
    }
    return count
}

private func longestSuffixPrefix(
    source: [Character],
    prefixOf pattern: [Character]
) -> Int {
    guard !source.isEmpty, !pattern.isEmpty else { return 0 }
    let prefix = prefixTable(pattern)
    var matched = 0
    for character in source {
        while matched > 0,
              (matched == pattern.count || pattern[matched] != character) {
            matched = prefix[matched - 1]
        }
        if matched < pattern.count, pattern[matched] == character {
            matched += 1
        }
    }
    return matched
}

private func occurrenceOffsets(
    needle: [Character],
    haystack: [Character],
    limit: Int
) -> [Int] {
    guard !needle.isEmpty, !haystack.isEmpty, needle.count <= haystack.count
    else { return [] }
    let prefix = prefixTable(needle)
    var result = [Int]()
    var matched = 0
    for (offset, character) in haystack.enumerated() {
        while matched > 0, needle[matched] != character {
            matched = prefix[matched - 1]
        }
        if needle[matched] == character { matched += 1 }
        if matched == needle.count {
            result.append(offset - needle.count + 1)
            if result.count >= limit { return result }
            matched = prefix[matched - 1]
        }
    }
    return result
}

private func prefixTable(_ pattern: [Character]) -> [Int] {
    guard !pattern.isEmpty else { return [] }
    var result = Array(repeating: 0, count: pattern.count)
    var matched = 0
    for offset in 1..<pattern.count {
        while matched > 0, pattern[matched] != pattern[offset] {
            matched = result[matched - 1]
        }
        if pattern[matched] == pattern[offset] { matched += 1 }
        result[offset] = matched
    }
    return result
}

private func mergedRanges(_ values: [Range<Int>]) -> [Range<Int>] {
    let ordered = values.filter { !$0.isEmpty }.sorted {
        if $0.lowerBound != $1.lowerBound { return $0.lowerBound < $1.lowerBound }
        return $0.upperBound < $1.upperBound
    }
    var result = [Range<Int>]()
    for range in ordered {
        guard let last = result.last, range.lowerBound <= last.upperBound else {
            result.append(range)
            continue
        }
        result[result.count - 1] = last.lowerBound..<max(last.upperBound, range.upperBound)
    }
    return result
}

private func confidentReadOverlap(
    overlap: Int,
    previousCount: Int,
    currentCount: Int,
    ranges: [Range<Int>]
) -> Bool {
    guard overlap > 0 else { return false }
    if overlap == currentCount { return true }
    let smaller = min(previousCount, currentCount)
    if overlap == smaller, smaller >= 4 { return true }
    let longest = ranges.map(\.count).max() ?? 0
    if smaller <= 8 {
        return overlap >= 2 && Double(overlap) / Double(smaller) >= 0.5
    }
    if longest >= 8, Double(overlap) / Double(smaller) >= 0.35 {
        return true
    }
    return longest >= 12
        && (overlap >= 64 || Double(overlap) / Double(smaller) >= 0.10)
}
