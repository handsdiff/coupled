import Foundation

/// The model-facing identity of the editable surface that received a WRITE.
///
/// This is deliberately derived only from the synchronous pre-mutation AX
/// destination. The complete source dictionary remains in conditioningState
/// for audit; this smaller projection is the only destination serialized into
/// model input by phase1-causal-v15+.
public struct Phase1WriteDestination: Equatable, Sendable {
    public let application: String
    public let surfaceKind: String
    public let surfaceLabel: String?
    public let resourceTitle: String?
    public let terminalProgramLabel: String?
    public let interactionMode: String?
    public let agent: String?
    public let logicalDestinationKey: String
    public let rule: String

    public var modelFacingJSONObject: [String: Any] {
        compactPhase1DestinationObject([
            "schemaVersion": 1,
            "application": application,
            "surfaceKind": surfaceKind,
            "surfaceLabel": surfaceLabel,
            "resourceTitle": resourceTitle,
            "terminalProgramLabel": terminalProgramLabel,
            "interactionMode": interactionMode,
            "agent": agent,
        ])
    }

    public init(
        application: String,
        surfaceKind: String,
        surfaceLabel: String?,
        resourceTitle: String?,
        terminalProgramLabel: String? = nil,
        interactionMode: String? = nil,
        agent: String? = nil,
        logicalDestinationKey: String,
        rule: String
    ) {
        self.application = application
        self.surfaceKind = surfaceKind
        self.surfaceLabel = surfaceLabel
        self.resourceTitle = resourceTitle
        self.terminalProgramLabel = terminalProgramLabel
        self.interactionMode = interactionMode
        self.agent = agent
        self.logicalDestinationKey = logicalDestinationKey
        self.rule = rule
    }
}

public enum Phase1WriteDestinationNormalizer {
    public static let version = "phase1-write-destination-v1"

    public static func normalize(
        _ evidence: [String: Any],
        terminalAgentProgramMappings: [String: String] = [:]
    ) -> Phase1WriteDestination {
        let bundle = string(evidence["bundleIdentifier"])
        let rawApp = string(evidence["appName"])
        let role = string(evidence["role"])
        let subrole = string(evidence["subrole"])
        let description = string(evidence["fieldDescription"])
        let label = string(evidence["fieldLabel"])
        let placeholder = string(evidence["placeholder"])
        let identifier = string(evidence["fieldIdentifier"])
        let window = string(evidence["windowTitle"])
        let combined = [description, label, placeholder, identifier, subrole]
            .filter { !$0.isEmpty }.joined(separator: " ").lowercased()
        let application = canonicalApplication(bundle: bundle, raw: rawApp)

        var kind = "editable_text_field"
        var surfaceLabel = firstNonEmpty(label, description, placeholder, identifier)
        var resourceTitle = cleanedResourceTitle(window, bundle: bundle)
        var rule = "generic_focused_editable"
        var terminalProgramLabel: String?
        var interactionMode: String?
        var agent: String?

        if bundle == "com.microsoft.VSCode" {
            if combined.contains("terminal") {
                kind = "integrated_terminal"
                let terminal = parseTerminalDescription(
                    firstNonEmpty(description, label, identifier)
                )
                surfaceLabel = terminal.surfaceLabel ?? "Terminal"
                terminalProgramLabel = terminal.programLabel
                let titleMode = terminalInteractionMode(
                    programLabel: terminal.programLabel,
                    configuredAgentProgramMappings: terminalAgentProgramMappings
                )
                interactionMode = titleMode.mode
                agent = titleMode.agent
                rule = titleMode.rule
                // A VS Code window title commonly names a background editor,
                // not the focused integrated terminal.
                resourceTitle = nil
            } else if combined.contains("find") {
                kind = "editor_find"
                surfaceLabel = firstNonEmpty(label, description, placeholder) ?? "Find"
                rule = "vscode_focused_find"
            } else if role == "AXTextArea" {
                kind = "code_editor"
                surfaceLabel = nil
                rule = "vscode_focused_editor"
            }
        } else if bundle == "com.google.Chrome" || bundle == "company.thebrowser.Browser" {
            if identifier == "commandBarTextField"
                || description.lowercased().contains("address and search bar")
                || combined.contains("search or enter url") {
                kind = "browser_address_bar"
                surfaceLabel = "Address and search bar"
                // The page behind the omnibox is not the receiving surface.
                resourceTitle = nil
                rule = "chromium_focused_address_bar"
            } else if isChatPrompt(combined: combined, window: window) {
                kind = "chat_prompt"
                surfaceLabel = firstNonEmpty(label, description, placeholder) ?? "Prompt"
                rule = "chromium_focused_chat_prompt"
            } else if subrole == "AXSearchField"
                || role == "AXComboBox"
                || combined.contains("search") {
                kind = "search_field"
                surfaceLabel = firstNonEmpty(label, description, placeholder) ?? "Search"
                rule = "chromium_focused_search_field"
            } else if !label.isEmpty || !placeholder.isEmpty {
                kind = "web_form_field"
                surfaceLabel = firstNonEmpty(label, placeholder, description)
                rule = "chromium_labeled_form_field"
            } else {
                kind = "web_editable"
                rule = "chromium_focused_editable"
            }
        } else if bundle == "com.openai.codex" {
            kind = "chat_prompt"
            surfaceLabel = firstNonEmpty(label, description, placeholder) ?? "Do anything"
            resourceTitle = nil
            rule = "chatgpt_focused_prompt"
        } else if bundle == "md.obsidian" && role == "AXTextArea" {
            kind = "note_editor"
            surfaceLabel = nil
            resourceTitle = cleanedObsidianTitle(window)
            rule = "obsidian_focused_note_editor"
        }

        let keyParts = [
            application, kind, surfaceLabel ?? "", resourceTitle ?? "",
            interactionMode ?? "", agent ?? "",
        ]
        return Phase1WriteDestination(
            application: application,
            surfaceKind: kind,
            surfaceLabel: nonEmpty(surfaceLabel),
            resourceTitle: nonEmpty(resourceTitle),
            terminalProgramLabel: nonEmpty(terminalProgramLabel),
            interactionMode: nonEmpty(interactionMode),
            agent: nonEmpty(agent),
            logicalDestinationKey: keyParts.joined(separator: "\u{001f}"),
            rule: rule
        )
    }

    public static func provenance(
        evidence: [String: Any],
        normalized: Phase1WriteDestination
    ) -> [String: Any] {
        var result: [String: Any] = [
            "schemaVersion": 1,
            "normalizerVersion": version,
            "rule": normalized.rule,
            "source": "pre_mutation_accessibility_destination",
            "originalDestination": evidence,
        ]
        if normalized.rule == "vscode_terminal_configured_agent_title",
           let programLabel = normalized.terminalProgramLabel,
           let agent = normalized.agent {
            result["configuredTerminalAgentMapping"] = [
                "terminalProgramLabel": programLabel,
                "agent": agent,
            ]
        }
        return result
    }
}

private func canonicalApplication(bundle: String, raw: String) -> String {
    switch bundle {
    case "com.microsoft.VSCode": return "Visual Studio Code"
    case "com.google.Chrome": return "Google Chrome"
    case "company.thebrowser.Browser": return "Arc"
    case "com.openai.codex": return "ChatGPT"
    case "md.obsidian": return "Obsidian"
    default: return raw.isEmpty ? bundle : raw
    }
}

private func isChatPrompt(combined: String, window: String) -> Bool {
    let promptMarkers = [
        "ask gemini", "ask anything", "message claude", "reply to claude",
        "do anything", "write a message", "send a message", "prompt",
    ]
    if promptMarkers.contains(where: combined.contains) { return true }
    let lowerWindow = window.lowercased()
    return (lowerWindow.contains("gemini") || lowerWindow.contains("claude"))
        && combined.contains("ask")
}

private func parseTerminalDescription(
    _ value: String?
) -> (surfaceLabel: String?, programLabel: String?) {
    guard var result = nonEmpty(value) else { return (nil, nil) }
    result = result.replacingOccurrences(
        of: #"\s+Use\s+⌥F1.*$"#,
        with: "",
        options: .regularExpression
    )
    result = result.replacingOccurrences(
        of: #"(?<=,\s)[\u2800-\u28ff]+\s*"#,
        with: "",
        options: .regularExpression
    )
    result = result.trimmingCharacters(in: .whitespacesAndNewlines)
    let pattern = #"^(Terminal\s+\d+)\s*,\s*(.+)$"#
    guard let expression = try? NSRegularExpression(pattern: pattern),
          let match = expression.firstMatch(
            in: result, range: NSRange(result.startIndex..., in: result)
          ), match.numberOfRanges == 3,
          let surfaceRange = Range(match.range(at: 1), in: result),
          let programRange = Range(match.range(at: 2), in: result) else {
        return (nonEmpty(result), nil)
    }
    return (
        nonEmpty(String(result[surfaceRange])),
        nonEmpty(String(result[programRange]))
    )
}

private func terminalInteractionMode(
    programLabel: String?,
    configuredAgentProgramMappings: [String: String]
) -> (mode: String, agent: String?, rule: String) {
    let value = (programLabel ?? "").lowercased()
    if ["zsh", "bash", "fish", "sh", "dash", "ksh", "tcsh"].contains(value) {
        return ("shell", nil, "vscode_terminal_known_shell_title")
    }
    if value == "codex" {
        return ("agent_cli", "Codex", "vscode_terminal_known_codex_title")
    }
    if value.contains("claude") {
        return ("agent_cli", "Claude", "vscode_terminal_known_claude_title")
    }
    if let configuredAgent = configuredAgentProgramMappings[value] {
        return (
            "agent_cli", configuredAgent,
            "vscode_terminal_configured_agent_title"
        )
    }
    return ("unknown", nil, "vscode_terminal_mode_unresolved")
}

private func cleanedResourceTitle(_ value: String, bundle: String) -> String? {
    guard !value.isEmpty else { return nil }
    var result = value
    if bundle == "com.google.Chrome" {
        result = result.replacingOccurrences(
            of: #"\s+-\s+(?:High memory usage\s+-\s+[^-]+\s+-\s+)?Google Chrome(?:\s+-\s+.*)?$"#,
            with: "",
            options: .regularExpression
        )
    }
    return nonEmpty(result)
}

private func cleanedObsidianTitle(_ value: String) -> String? {
    guard !value.isEmpty else { return nil }
    let suffix = #"\s+-\s+Notes\s+-\s+Obsidian(?:\s+[0-9.]+)?$"#
    let result = value.replacingOccurrences(
        of: suffix,
        with: "",
        options: .regularExpression
    )
    return nonEmpty(result)
}

private func firstNonEmpty(_ values: String...) -> String? {
    values.first(where: { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty })
}

private func nonEmpty(_ value: String?) -> String? {
    guard let value else { return nil }
    let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
    return trimmed.isEmpty ? nil : trimmed
}

private func string(_ value: Any?) -> String {
    value as? String ?? ""
}

private func compactPhase1DestinationObject(_ source: [String: Any?]) -> [String: Any] {
    var result: [String: Any] = [:]
    for (key, value) in source {
        guard let value else { continue }
        if let string = value as? String, string.isEmpty { continue }
        result[key] = value
    }
    return result
}
