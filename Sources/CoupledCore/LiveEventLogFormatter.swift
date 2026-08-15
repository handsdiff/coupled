import Foundation

/// Formats one derived event for the compact terminal follower and standalone
/// native viewer without changing the stored JSONL.
public enum LiveEventLogFormatter {
    public static func format(_ data: Data) -> String? {
        guard let object = try? JSONSerialization.jsonObject(with: data),
              let event = object as? [String: Any] else {
            return String(data: data, encoding: .utf8)?
                .trimmingCharacters(in: .newlines)
        }
        return format(event)
    }

    public static func format(_ event: [String: Any]) -> String {
        let kind = string(event["kind"])
        let provenance = string(event["provenance"])
        if kind == "read", provenance == "screen_ocr" {
            return formatScreenRead(event)
        }
        if kind == "read_candidate" {
            let triggers = (event["triggerTypes"] as? [String] ?? []).joined(separator: ",")
            return "\(timestamp(event))  READ  app=\(appName(event))"
                + "  window=\(windowName(event))  activity=\(triggers)"
                + "  items=\(integer(event["eventCount"]) ?? 0)"
                + "  after=\(numberText(event["readDelaySeconds"]))s"
                + "  display=\(displayID(event))"
        }
        if kind == "write", event["operation"] != nil {
            return "\(timestamp(event))  WRITE  app=\(appName(event))"
                + "  window=\(windowName(event))"
                + "  operation=\(string(event["operation"]) ?? "-")"
                + "  provenance=\(provenance ?? "-")"
                + "  inserted=\(quoted(string(event["content"]) ?? ""))"
                + "  removed=\(quoted(string(event["removedContent"]) ?? ""))"
                + "  offset=\(integer(event["characterOffset"]) ?? 0)"
                + "  configured-delay=\(numberText(event["configuredWriteDelaySeconds"] ?? event["writeDelaySeconds"]))s"
                + " boundary=\(string(event["boundaryReason"]) ?? "write_delay_elapsed")"
                + " source=\(string(event["derivationObservationSource"]) ?? "terminal_after")"
                + " fallback=\(string(event["fallbackReason"]) ?? "-")"
        }
        if kind == "write", provenance == "typed_character_burst" {
            return "\(timestamp(event))  WRITE  app=\(appName(event))"
                + "  text=\(quoted(string(event["content"]) ?? ""))"
                + "  characters=\(integer(event["characterCount"]) ?? 0)"
                + "  after=\(numberText(event["writeDelaySeconds"]))s"
        }
        if kind == "character_write" {
            return "\(timestamp(event))  CHARACTER  app=\(appName(event))"
                + "  value=\(quoted(string(event["character"]) ?? ""))"
        }
        if let kind {
            return "\(timestamp(event))  \(kind.uppercased())"
                + "  app=\(appName(event))  display=\(displayID(event))"
        }
        if let encoded = try? JSONSerialization.data(
            withJSONObject: event,
            options: [.sortedKeys, .withoutEscapingSlashes]
        ) {
            return String(data: encoded, encoding: .utf8) ?? "{}"
        }
        return "{}"
    }

    private static func formatScreenRead(_ event: [String: Any]) -> String {
        let content = string(event["content"]) ?? ""
        let lines = content.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        let preview = lines.prefix(8).enumerated().map {
            "    \($0.offset + 1) | \($0.element)"
        }.joined(separator: "\n")
        let remainder = lines.count > 8
            ? "\n    … \(lines.count - 8) more recognized lines"
            : ""
        let side = percentage(event["viewportSideCropFraction"])
        let top = percentage(event["viewportTopCropFraction"])
        let bottom = percentage(event["viewportBottomCropFraction"])
        let header = "\(timestamp(event))  READ  app=\(appName(event))"
            + "  window=\(windowName(event))  characters=\(content.count)"
            + "  lines=\(integer(event["emittedLineCount"] ?? event["recognizedLineCount"]) ?? 0)"
            + "  viewport-lines=\(integer(event["recognizedLineCount"]) ?? 0)"
            + "  overlap-removed=\(integer(event["overlapRemovedLineCount"]) ?? 0)"
            + "  crop=\(side)% sides/\(top)% top/\(bottom)% bottom"
            + "  display=\(displayID(event))"
        return "\(header)\n\(preview)\(remainder)"
    }

    private static func appName(_ event: [String: Any]) -> String {
        if let value = string(event["appName"])
            ?? string(event["frontmostAppName"]) {
            return value
        }
        if let app = event["app"] as? [String: Any] {
            return string(app["name"]) ?? string(app["bundleIdentifier"]) ?? "-"
        }
        return string(event["bundleIdentifier"]) ?? "-"
    }

    private static func windowName(_ event: [String: Any]) -> String {
        if let title = string(event["windowTitle"]), !title.isEmpty {
            return quoted(title)
        }
        if let identifier = integer(event["windowID"]) {
            return "#\(identifier)"
        }
        return "-"
    }

    private static func timestamp(_ event: [String: Any]) -> String {
        string(event["observedAt"]) ?? string(event["lastObservedAt"]) ?? "-"
    }

    private static func displayID(_ event: [String: Any]) -> String {
        integer(event["displayID"]).map(String.init) ?? "-"
    }

    private static func percentage(_ value: Any?) -> Int {
        Int((((value as? NSNumber)?.doubleValue ?? 0) * 100.0).rounded())
    }

    private static func integer(_ value: Any?) -> Int? {
        (value as? NSNumber)?.intValue
    }

    private static func string(_ value: Any?) -> String? {
        value as? String
    }

    private static func numberText(_ value: Any?) -> String {
        guard let number = value as? NSNumber else { return "-" }
        let double = number.doubleValue
        return double.rounded() == double ? String(Int(double)) : String(double)
    }

    private static func quoted(_ value: String) -> String {
        guard let data = try? JSONEncoder().encode(value),
              let encoded = String(data: data, encoding: .utf8) else {
            return "\"\""
        }
        return encoded
    }
}
