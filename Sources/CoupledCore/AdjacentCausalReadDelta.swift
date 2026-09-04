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
