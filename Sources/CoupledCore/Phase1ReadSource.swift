import Foundation

/// The model-facing identity of the surface represented by a READ.
///
/// Schema-7 collection retains the AX ancestry used to select the OCR pane.
/// This projection uses only that contemporaneous evidence. It never infers a
/// surface from OCR text, a later WRITE, or a preceding READ. Historical
/// sessions retain their former application/window serialization exactly.
public struct Phase1ReadSource: Sendable {
    public let application: String
    public let surfaceKind: String?
    public let surfaceLabel: String?
    public let resourceTitle: String?
    public let legacyWindow: String?
    public let category: String
    public let rule: String
    public let selectedRole: String?
    public let selectedSubrole: String?

    public var modelFacingJSONObject: [String: Any] {
        if surfaceKind == nil {
            return compactPhase1ReadSourceObject([
                "application": nonEmptyReadSource(application),
                "window": nonEmptyReadSource(legacyWindow),
            ])
        }
        return compactPhase1ReadSourceObject([
            "schemaVersion": 1,
            "application": nonEmptyReadSource(application),
            "surfaceKind": nonEmptyReadSource(surfaceKind),
            "surfaceLabel": nonEmptyReadSource(surfaceLabel),
            "resourceTitle": nonEmptyReadSource(resourceTitle),
        ])
    }
}

public enum Phase1ReadSourceNormalizer {
    public static let version = "phase1-read-source-v1"

    public static func normalize(_ event: [String: Any]) -> Phase1ReadSource {
        let bundle = readSourceString(event["bundleIdentifier"])
        let rawApplication = readSourceString(event["appName"])
        let rawWindow = readSourceString(event["windowTitle"])
        let readSurface = event["readSurface"] as? [String: Any]
        let ruleVersion = readSourceString(readSurface?["ruleVersion"])

        // Preserve the exact pre-v16 representation for sessions that lack
        // schema-7 AX-pane evidence. Their window title is known only as the
        // outer captured window and must not be retroactively reinterpreted.
        guard [
            "ax-pane-read-v2", "ax-pane-read-v3", "ax-pane-read-v4",
            "ax-pane-read-v5",
        ]
            .contains(ruleVersion) else {
            return Phase1ReadSource(
                application: rawApplication,
                surfaceKind: nil,
                surfaceLabel: nil,
                resourceTitle: nil,
                legacyWindow: rawWindow,
                category: "legacy_pre_schema7",
                rule: "preserve_legacy_application_window",
                selectedRole: nil,
                selectedSubrole: nil
            )
        }

        let application = canonicalReadApplication(
            bundle: bundle, raw: rawApplication
        )
        guard readSourceString(readSurface?["status"])
                == "surface_ocr_replacement",
              let selection = readSurface?["surfaceSelection"]
                as? [String: Any] else {
            return Phase1ReadSource(
                application: application,
                surfaceKind: "unresolved_surface",
                surfaceLabel: nil,
                resourceTitle: nil,
                legacyWindow: nil,
                category: "schema7_unresolved",
                rule: "ax_pane_evidence_unresolved",
                selectedRole: nil,
                selectedSubrole: nil
            )
        }

        let selectedRole = nonEmptyReadSource(
            readSourceString(selection["selectedRole"])
        )
        let selectedSubrole = nonEmptyReadSource(
            readSourceString(selection["selectedSubrole"])
        )
        let method = readSourceString(selection["method"])
        let isFallback = (selection["isV1Fallback"] as? Bool) == true
            || method == "v1_fallback"
        if isFallback {
            return Phase1ReadSource(
                application: application,
                surfaceKind: "active_surface_proxy",
                surfaceLabel: nil,
                resourceTitle: nil,
                legacyWindow: nil,
                category: "schema7_pointer_fallback",
                rule: "schema7_v1_fallback_has_no_proven_ax_identity",
                selectedRole: selectedRole,
                selectedSubrole: selectedSubrole
            )
        }

        let selectedNode = selectedAccessibilityNode(
            event: event, selection: selection
        )
        let role = selectedRole
            ?? nonEmptyReadSource(readSourceString(selectedNode?["role"]))
        let subrole = selectedSubrole
            ?? nonEmptyReadSource(readSourceString(selectedNode?["subrole"]))
        let kindAndRule = classifyReadSurface(
            role: role, subrole: subrole, method: method
        )
        let label = firstBoundedReadLabel(
            readSourceString(selectedNode?["title"]),
            readSourceString(selectedNode?["elementDescription"])
        )
        // The outer window title is model-facing only when AX proves that the
        // selected surface is the main resource. A subpane must not inherit a
        // background editor or document title.
        let resourceTitle = kindAndRule.kind == "main_content"
            || kindAndRule.kind == "web_content"
            ? nonEmptyReadSource(rawWindow)
            : nil
        return Phase1ReadSource(
            application: application,
            surfaceKind: kindAndRule.kind,
            surfaceLabel: label,
            resourceTitle: resourceTitle,
            legacyWindow: nil,
            category: "schema7_ax_selected",
            rule: kindAndRule.rule,
            selectedRole: role,
            selectedSubrole: subrole
        )
    }

    public static func provenance(
        event: [String: Any], normalized: Phase1ReadSource
    ) -> [String: Any] {
        var result: [String: Any] = [
            "schemaVersion": 1,
            "normalizerVersion": version,
            "source": "captured_read_surface_evidence",
            "category": normalized.category,
            "rule": normalized.rule,
            "originalApplication": readSourceString(event["appName"]),
            "originalWindowTitle": readSourceString(event["windowTitle"]),
        ]
        if let role = normalized.selectedRole { result["selectedRole"] = role }
        if let subrole = normalized.selectedSubrole {
            result["selectedSubrole"] = subrole
        }
        if let readSurface = event["readSurface"] as? [String: Any],
           let selection = readSurface["surfaceSelection"] as? [String: Any] {
            result["selectionMethod"] = readSourceString(selection["method"])
            result["selectionConfidence"] = readSourceString(
                selection["confidence"]
            )
            result["selectionReason"] = readSourceString(selection["reason"])
        }
        return result
    }
}

private func classifyReadSurface(
    role: String?, subrole: String?, method: String
) -> (kind: String, rule: String) {
    switch subrole {
    case "AXLandmarkMain":
        return ("main_content", "selected_ax_main_landmark")
    case "AXSearchField":
        return ("search_surface", "selected_ax_search_surface")
    default:
        break
    }
    switch role {
    case "AXWebArea":
        return ("web_content", "selected_ax_web_area")
    case "AXTextArea", "AXTextField":
        return ("text_content", "selected_ax_text_surface")
    case "AXScrollArea":
        return ("scrollable_pane", "selected_ax_scroll_area")
    default:
        break
    }
    if method == "ax_semantic_container"
        || method == "ax_semantic_container_expanded" {
        return ("semantic_container", "selected_ax_semantic_container")
    }
    return ("active_pane", "selected_ax_pane_without_semantic_label")
}

private func selectedAccessibilityNode(
    event: [String: Any], selection: [String: Any]
) -> [String: Any]? {
    guard let depth = readSourceInt(selection["selectedDepth"]),
          let surface = event["accessibilitySurface"] as? [String: Any],
          let ancestors = surface["ancestors"] as? [[String: Any]] else {
        return nil
    }
    return ancestors.first { readSourceInt($0["depth"]) == depth }
}

private func canonicalReadApplication(bundle: String, raw: String) -> String {
    switch bundle {
    case "com.microsoft.VSCode": return "Visual Studio Code"
    case "com.google.Chrome": return "Google Chrome"
    case "company.thebrowser.Browser": return "Arc"
    case "com.openai.codex": return "ChatGPT"
    case "md.obsidian": return "Obsidian"
    default: return raw.isEmpty ? bundle : raw
    }
}

private func firstBoundedReadLabel(_ values: String...) -> String? {
    for raw in values {
        let value = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else { continue }
        return String(value.prefix(160))
    }
    return nil
}

private func readSourceString(_ value: Any?) -> String {
    value as? String ?? ""
}

private func readSourceInt(_ value: Any?) -> Int? {
    if let value = value as? Int { return value }
    if let value = value as? NSNumber { return value.intValue }
    return nil
}

private func nonEmptyReadSource(_ value: String?) -> String? {
    guard let value, !value.isEmpty else { return nil }
    return value
}

private func compactPhase1ReadSourceObject(
    _ values: [String: Any?]
) -> [String: Any] {
    values.reduce(into: [String: Any]()) { result, entry in
        if let value = entry.value { result[entry.key] = value }
    }
}
