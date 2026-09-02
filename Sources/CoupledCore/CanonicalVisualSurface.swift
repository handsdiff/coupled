import Foundation

public struct VisualCaptureWindowBounds: Equatable, Sendable {
    public let x: Double
    public let y: Double
    public let width: Double
    public let height: Double

    public init(x: Double, y: Double, width: Double, height: Double) {
        self.x = x
        self.y = y
        self.width = width
        self.height = height
    }
}

public struct VisualCaptureWindowCandidate: Equatable, Sendable {
    public let windowID: UInt32
    public let processIdentifier: Int32
    public let title: String?
    public let bounds: VisualCaptureWindowBounds

    public init(
        windowID: UInt32,
        processIdentifier: Int32,
        title: String?,
        bounds: VisualCaptureWindowBounds
    ) {
        self.windowID = windowID
        self.processIdentifier = processIdentifier
        self.title = title
        self.bounds = bounds
    }
}

public struct VisualFocusedWindowHint: Equatable, Sendable {
    public let title: String?
    public let bounds: VisualCaptureWindowBounds?

    public init(title: String?, bounds: VisualCaptureWindowBounds?) {
        self.title = title
        self.bounds = bounds
    }
}

public struct CanonicalVisualSurfaceSelection: Equatable, Sendable {
    public let windowID: UInt32
    public let reason: String

    public init(windowID: UInt32, reason: String) {
        self.windowID = windowID
        self.reason = reason
    }
}

/// Chooses a stable top-level capture window after a low-level interaction
/// surface has identified the owning process. Candidate order is front-to-back.
/// AX focused-window geometry is evidence, while size/title checks keep narrow
/// toolbars and display-sized helpers from becoming capture surfaces.
public func selectCanonicalVisualCaptureSurface(
    processIdentifier: Int32,
    pointX: Double,
    pointY: Double,
    rawWindowID: UInt32?,
    focusedWindow: VisualFocusedWindowHint?,
    candidates: [VisualCaptureWindowCandidate],
    lastValidWindowID: UInt32?
) -> CanonicalVisualSurfaceSelection? {
    let owned = candidates.filter { $0.processIdentifier == processIdentifier }
    let plausible = owned.filter(isPlausibleContentWindow)
    guard !plausible.isEmpty else { return nil }

    let raw = rawWindowID.flatMap { id in owned.first { $0.windowID == id } }
    let focused = focusedWindow.flatMap { hint in
        bestFocusedWindowMatch(hint, candidates: plausible)
    }

    // A normal titled surface under the pointer can be a legitimate dialog;
    // do not replace it merely because a larger application window exists.
    if let raw, isPlausibleContentWindow(raw), hasTitle(raw) {
        return CanonicalVisualSurfaceSelection(
            windowID: raw.windowID,
            reason: focused?.windowID == raw.windowID
                ? "focused_raw_top_level_window"
                : "raw_top_level_window"
        )
    }

    // Narrow/untitled helper surfaces are mapped back to the focused content
    // window only when their geometry associates them with that window.
    if let focused {
        let canReplaceRaw = raw.map { associated($0, with: focused) } ?? true
        if canReplaceRaw {
            return CanonicalVisualSurfaceSelection(
                windowID: focused.windowID,
                reason: "focused_top_level_window"
            )
        }
    }

    if let pointed = plausible.first(where: {
        hasTitle($0) && contains($0.bounds, x: pointX, y: pointY)
    }) {
        return CanonicalVisualSurfaceSelection(
            windowID: pointed.windowID,
            reason: "frontmost_pointed_top_level_window"
        )
    }

    if let focused {
        return CanonicalVisualSurfaceSelection(
            windowID: focused.windowID,
            reason: "focused_top_level_window"
        )
    }

    // Retention is deliberately process-local. The caller must not pass a
    // last-valid ID from another process, and this lookup cannot select one.
    if let lastValidWindowID,
       let retained = plausible.first(where: { $0.windowID == lastValidWindowID }),
       raw.map({ associated($0, with: retained) }) ?? true {
        return CanonicalVisualSurfaceSelection(
            windowID: retained.windowID,
            reason: "retained_same_process_top_level_window"
        )
    }

    if let titled = plausible.first(where: hasTitle) {
        return CanonicalVisualSurfaceSelection(
            windowID: titled.windowID,
            reason: "frontmost_titled_top_level_fallback"
        )
    }
    let largest = plausible.max { area($0.bounds) < area($1.bounds) }!
    return CanonicalVisualSurfaceSelection(
        windowID: largest.windowID,
        reason: "largest_plausible_top_level_fallback"
    )
}

private func isPlausibleContentWindow(_ candidate: VisualCaptureWindowCandidate) -> Bool {
    candidate.bounds.width >= 100
        && candidate.bounds.height >= 100
        && area(candidate.bounds) >= 40_000
}

private func hasTitle(_ candidate: VisualCaptureWindowCandidate) -> Bool {
    candidate.title?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false
}

private func bestFocusedWindowMatch(
    _ hint: VisualFocusedWindowHint,
    candidates: [VisualCaptureWindowCandidate]
) -> VisualCaptureWindowCandidate? {
    let normalizedTitle = hint.title?.trimmingCharacters(in: .whitespacesAndNewlines)
    let scored = candidates.compactMap { candidate -> (VisualCaptureWindowCandidate, Double)? in
        let titleMatches = normalizedTitle?.isEmpty == false
            && candidate.title?.trimmingCharacters(in: .whitespacesAndNewlines) == normalizedTitle
        let geometryScore = hint.bounds.map { boundsSimilarity($0, candidate.bounds) }
        if hint.bounds != nil {
            guard (geometryScore ?? 0) >= 0.70 else { return nil }
        } else {
            guard titleMatches else { return nil }
        }
        return (candidate, (titleMatches ? 2 : 0) + (geometryScore ?? 0))
    }
    return scored.max { $0.1 < $1.1 }?.0
}

private func boundsSimilarity(
    _ left: VisualCaptureWindowBounds,
    _ right: VisualCaptureWindowBounds
) -> Double {
    let intersectionWidth = max(
        0,
        min(left.x + left.width, right.x + right.width) - max(left.x, right.x)
    )
    let intersectionHeight = max(
        0,
        min(left.y + left.height, right.y + right.height) - max(left.y, right.y)
    )
    let intersection = intersectionWidth * intersectionHeight
    let union = area(left) + area(right) - intersection
    guard union > 0 else { return 0 }
    return intersection / union
}

private func associated(
    _ helper: VisualCaptureWindowCandidate,
    with content: VisualCaptureWindowCandidate
) -> Bool {
    if contains(content.bounds, x: helper.bounds.x + helper.bounds.width / 2,
                y: helper.bounds.y + helper.bounds.height / 2) {
        return true
    }
    let horizontalOverlap = max(
        0,
        min(helper.bounds.x + helper.bounds.width, content.bounds.x + content.bounds.width)
            - max(helper.bounds.x, content.bounds.x)
    )
    let overlapRatio = horizontalOverlap / max(1, min(helper.bounds.width, content.bounds.width))
    let verticalGap = max(
        0,
        max(helper.bounds.y, content.bounds.y)
            - min(helper.bounds.y + helper.bounds.height, content.bounds.y + content.bounds.height)
    )
    return overlapRatio >= 0.75 && verticalGap <= 120
}

private func contains(_ bounds: VisualCaptureWindowBounds, x: Double, y: Double) -> Bool {
    x >= bounds.x && x <= bounds.x + bounds.width
        && y >= bounds.y && y <= bounds.y + bounds.height
}

private func area(_ bounds: VisualCaptureWindowBounds) -> Double {
    max(0, bounds.width) * max(0, bounds.height)
}
