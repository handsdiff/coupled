import Foundation

/// A conservative, order-preserving delta between two complete OCR states.
/// The returned text contains only current-state spans that were not already
/// present in the immediately preceding state. `nil` means that a trustworthy
/// overlap could not be established and the caller must retain the current
/// state in full.
public struct AdjacentCausalReadDelta: Sendable, Equatable {
    public let emittedContent: String
    public let alignment: String
    public let overlapCharacterCount: Int
    public let currentCharacterCount: Int

    public init(
        emittedContent: String,
        alignment: String,
        overlapCharacterCount: Int,
        currentCharacterCount: Int
    ) {
        self.emittedContent = emittedContent
        self.alignment = alignment
        self.overlapCharacterCount = overlapCharacterCount
        self.currentCharacterCount = currentCharacterCount
    }
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
    struct Candidate {
        let ranges: [Range<Int>]
        let alignment: String
    }
    var candidates = [Candidate]()
    let boundaryRanges = [
        prefix > 0 ? 0..<prefix : nil,
        suffix > 0 ? (next.characters.count - suffix)..<next.characters.count : nil,
    ].compactMap { $0 }
    if !boundaryRanges.isEmpty {
        candidates.append(Candidate(
            ranges: boundaryRanges,
            alignment: "same_position_boundaries"
        ))
    }

    let forward = longestSuffixPrefix(
        source: prior.characters,
        prefixOf: next.characters
    )
    if forward > 0 {
        candidates.append(Candidate(
            ranges: [0..<forward],
            alignment: "prior_suffix_to_current_prefix"
        ))
    }
    let backward = longestSuffixPrefix(
        source: next.characters,
        prefixOf: prior.characters
    )
    if backward > 0 {
        candidates.append(Candidate(
            ranges: [(next.characters.count - backward)..<next.characters.count],
            alignment: "prior_prefix_to_current_suffix"
        ))
    }

    let priorInCurrent = occurrenceOffsets(
        needle: prior.characters,
        haystack: next.characters,
        limit: 2
    )
    if priorInCurrent.count == 1 {
        let start = priorInCurrent[0]
        candidates.append(Candidate(
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
        candidates.append(Candidate(
            ranges: [0..<next.characters.count],
            alignment: "current_state_inside_prior"
        ))
    }

    let ranked = candidates.map { candidate in
        let ranges = mergedRanges(candidate.ranges)
        return (
            candidate: Candidate(ranges: ranges, alignment: candidate.alignment),
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
                if !fragment.isEmpty { fragments.append(fragment) }
            }
            cursor = max(cursor, range.upperBound)
        }
        if cursor < rawCharacters.count {
            let fragment = String(rawCharacters[cursor..<rawCharacters.count])
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if !fragment.isEmpty { fragments.append(fragment) }
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
