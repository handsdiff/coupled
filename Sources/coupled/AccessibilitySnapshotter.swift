import AppKit
import ApplicationServices
import Foundation

final class AccessibilitySnapshotter {
    private let configuration: Configuration

    init(configuration: Configuration) {
        self.configuration = configuration
    }

    func capture(reason: String) -> AccessibilitySnapshot? {
        guard let base = captureBaseContext() else { return nil }

        let focusedElement = elementAttribute(base.applicationElement, kAXFocusedUIElementAttribute)
        let focusedRole = focusedElement.flatMap { stringAttribute($0, kAXRoleAttribute) }
        let editable = focusedElement.flatMap {
            editableContext(for: $0, app: base.app, window: base.window)
        }

        var queue: [(AXUIElement, Int)] = [(base.windowElement, 0)]
        var visitedHashes = Set<CFHashCode>()
        var visibleElements: [VisibleElement] = []
        var totalCharacters = 0
        var visitedNodeCount = 0
        var hitNodeLimit = false
        var hitCharacterLimit = false
        let windowFrame = frame(of: base.windowElement)

        while !queue.isEmpty {
            let (element, depth) = queue.removeFirst()
            let hash = CFHash(element)
            guard !visitedHashes.contains(hash) else { continue }
            visitedHashes.insert(hash)
            visitedNodeCount += 1

            if visitedNodeCount > configuration.maxNodes {
                hitNodeLimit = true
                break
            }

            let role = stringAttribute(element, kAXRoleAttribute) ?? "AXUnknown"
            let subrole = stringAttribute(element, kAXSubroleAttribute)
            if isSecure(role: role, subrole: subrole) {
                continue
            }

            let elementFrame = frame(of: element)
            let isVisible = elementFrame.map { candidate in
                guard candidate.width > 0, candidate.height > 0 else { return false }
                guard let windowFrame else { return true }
                return candidate.intersects(windowFrame)
            } ?? true

            if isVisible,
               let text = displayText(for: element, role: role),
               !text.isEmpty {
                let remaining = configuration.maxCharacters - totalCharacters
                if remaining <= 0 {
                    hitCharacterLimit = true
                    break
                }

                let clipped = clip(text, to: remaining)
                if clipped.count < text.count { hitCharacterLimit = true }
                visibleElements.append(
                    VisibleElement(
                        role: role,
                        title: stringAttribute(element, kAXTitleAttribute),
                        text: clipped,
                        frame: elementFrame.map(rectValue)
                    )
                )
                totalCharacters += clipped.count
            }

            if depth < 24, let children = elementArrayAttribute(element, kAXChildrenAttribute) {
                queue.append(contentsOf: children.map { ($0, depth + 1) })
            }
        }

        let deduplicated = deduplicate(elements: visibleElements)
        let visibleText = deduplicated.map(\.text).joined(separator: "\n")
        return AccessibilitySnapshot(
            snapshotID: UUID().uuidString,
            observedAt: nowTimestamp(),
            reason: reason,
            app: base.app,
            window: base.window,
            focusedRole: focusedRole,
            visibleText: visibleText,
            visibleElements: deduplicated,
            editable: editable,
            accessibilityActivation: base.accessibilityActivation,
            visitedNodeCount: visitedNodeCount,
            hitNodeLimit: hitNodeLimit,
            hitCharacterLimit: hitCharacterLimit
        )
    }

    func captureEditable(reason: String) -> EditableObservation? {
        guard let base = captureBaseContext(),
              let focused = elementAttribute(base.applicationElement, kAXFocusedUIElementAttribute),
              let editable = editableContext(for: focused, app: base.app, window: base.window) else {
            return nil
        }

        return EditableObservation(
            observationID: UUID().uuidString,
            observedAt: nowTimestamp(),
            reason: reason,
            app: base.app,
            window: base.window,
            editable: editable
        )
    }

    func captureContextIdentifier() -> String? {
        guard let base = captureBaseContext() else { return nil }
        let focused = elementAttribute(base.applicationElement, kAXFocusedUIElementAttribute)
        let role = focused.flatMap { stringAttribute($0, kAXRoleAttribute) } ?? ""
        let identifier = focused.flatMap { stringAttribute($0, kAXIdentifierAttribute) } ?? ""
        return "\(base.app.bundleIdentifier ?? base.app.name)|\(base.window.identifier)|\(role)|\(identifier)"
    }

    func captureReadTriggerContext() -> ReadTriggerContext? {
        guard let base = captureBaseContext() else { return nil }
        return ReadTriggerContext(app: base.app, window: base.window)
    }

    func frontmostProcessIdentifier() -> Int32? {
        guard let running = NSWorkspace.shared.frontmostApplication else { return nil }
        let name = running.localizedName ?? running.bundleIdentifier ?? "Unknown"
        guard configuration.captures(
            bundleIdentifier: running.bundleIdentifier,
            appName: name
        ) else { return nil }
        return running.processIdentifier
    }

    private struct BaseContext {
        let app: AppContext
        let window: WindowContext
        let applicationElement: AXUIElement
        let windowElement: AXUIElement
        let accessibilityActivation: AccessibilityActivation
    }

    private func captureBaseContext() -> BaseContext? {
        guard let running = NSWorkspace.shared.frontmostApplication else { return nil }
        let bundle = running.bundleIdentifier
        let name = running.localizedName ?? bundle ?? "Unknown"

        guard configuration.captures(bundleIdentifier: bundle, appName: name) else {
            return nil
        }

        let app = AppContext(
            name: name,
            bundleIdentifier: bundle,
            processIdentifier: running.processIdentifier
        )
        let applicationElement = AXUIElementCreateApplication(running.processIdentifier)
        let activation = activateRendererAccessibility(for: applicationElement)
        guard let windowElement = elementAttribute(applicationElement, kAXFocusedWindowAttribute) else {
            return nil
        }
        let enhancedWindow = configuration.activateRendererAccessibility
            ? setBooleanAttribute("AXEnhancedUserInterface", on: windowElement)
            : "disabled"

        let title = stringAttribute(windowElement, kAXTitleAttribute)
        let explicitIdentifier = stringAttribute(windowElement, kAXIdentifierAttribute)
        let framePart = frame(of: windowElement).map {
            "\(Int($0.origin.x)),\(Int($0.origin.y)),\(Int($0.width)),\(Int($0.height))"
        } ?? "unknown-frame"
        let window = WindowContext(
            title: title,
            identifier: explicitIdentifier ?? "\(title ?? "untitled")|\(framePart)"
        )
        return BaseContext(
            app: app,
            window: window,
            applicationElement: applicationElement,
            windowElement: windowElement,
            accessibilityActivation: AccessibilityActivation(
                manualApplication: activation.manual,
                enhancedApplication: activation.enhanced,
                enhancedWindow: enhancedWindow
            )
        )
    }

    private func activateRendererAccessibility(
        for applicationElement: AXUIElement
    ) -> (manual: String, enhanced: String) {
        guard configuration.activateRendererAccessibility else {
            return ("disabled", "disabled")
        }

        // Chromium and Electron avoid constructing their renderer AX trees until
        // an assistive client opts in. These attributes are their documented
        // programmatic equivalents of enabling an assistive technology.
        return (
            setBooleanAttribute("AXManualAccessibility", on: applicationElement),
            setBooleanAttribute("AXEnhancedUserInterface", on: applicationElement)
        )
    }

    private func setBooleanAttribute(_ attribute: String, on element: AXUIElement) -> String {
        let error = AXUIElementSetAttributeValue(element, attribute as CFString, kCFBooleanTrue)
        switch error {
        case .success:
            return "success"
        case .attributeUnsupported:
            return "unsupported"
        case .cannotComplete:
            return "cannot_complete"
        case .notImplemented:
            return "not_implemented"
        case .apiDisabled:
            return "api_disabled"
        case .invalidUIElement:
            return "invalid_ui_element"
        case .illegalArgument:
            return "illegal_argument"
        case .noValue:
            return "no_value"
        case .failure:
            return "failure"
        case .notEnoughPrecision:
            return "not_enough_precision"
        default:
            return "unknown_\(error.rawValue)"
        }
    }

    private func editableContext(
        for element: AXUIElement,
        app: AppContext,
        window: WindowContext
    ) -> EditableContext? {
        let role = stringAttribute(element, kAXRoleAttribute) ?? ""
        let subrole = stringAttribute(element, kAXSubroleAttribute)
        guard isEditable(role: role), !isSecure(role: role, subrole: subrole) else { return nil }

        guard let rawValue = stringAttribute(element, kAXValueAttribute) else { return nil }
        let clipped = clip(rawValue, to: configuration.maxCharacters)
        let explicitIdentifier = stringAttribute(element, kAXIdentifierAttribute)
        let title = stringAttribute(element, kAXTitleAttribute)
            ?? stringAttribute(element, kAXDescriptionAttribute)
        let framePart = frame(of: element).map {
            "\(Int($0.origin.x)),\(Int($0.origin.y)),\(Int($0.width)),\(Int($0.height))"
        } ?? "unknown-frame"
        let identifier = explicitIdentifier
            ?? "\(app.bundleIdentifier ?? app.name)|\(window.identifier)|\(role)|\(title ?? "untitled")|\(framePart)"
        let selectedRange = rangeAttribute(element, kAXSelectedTextRangeAttribute)

        return EditableContext(
            identifier: identifier,
            role: role,
            title: title,
            value: clipped,
            selectedText: stringAttribute(element, kAXSelectedTextAttribute),
            selectedRangeLocation: selectedRange?.location,
            selectedRangeLength: selectedRange?.length,
            valueWasTruncated: clipped.count < rawValue.count
        )
    }

    private func displayText(for element: AXUIElement, role: String) -> String? {
        let textRoles: Set<String> = [
            kAXStaticTextRole as String,
            kAXTextFieldRole as String,
            kAXTextAreaRole as String,
            kAXHeadingRole as String,
            "AXLink",
            kAXButtonRole as String,
            kAXCheckBoxRole as String,
            kAXRadioButtonRole as String,
            kAXMenuItemRole as String,
            kAXCellRole as String,
        ]
        guard textRoles.contains(role) else { return nil }

        if let value = stringAttribute(element, kAXValueAttribute), !value.isEmpty {
            if let visibleRange = rangeAttribute(element, kAXVisibleCharacterRangeAttribute),
               visibleRange.location >= 0,
               visibleRange.length > 0 {
                let nsValue = value as NSString
                let safeLocation = min(visibleRange.location, nsValue.length)
                let safeLength = min(visibleRange.length, nsValue.length - safeLocation)
                return nsValue.substring(with: NSRange(location: safeLocation, length: safeLength))
            }
            return value
        }

        return stringAttribute(element, kAXTitleAttribute)
            ?? stringAttribute(element, kAXDescriptionAttribute)
    }

    private func isEditable(role: String) -> Bool {
        [
            kAXTextFieldRole as String,
            kAXTextAreaRole as String,
            kAXComboBoxRole as String,
        ].contains(role)
    }

    private func isSecure(role: String, subrole: String?) -> Bool {
        role == "AXSecureTextField" || subrole == "AXSecureTextField"
    }

    private func deduplicate(elements: [VisibleElement]) -> [VisibleElement] {
        var seen = Set<String>()
        return elements.filter { element in
            let normalized = element.text
                .split(whereSeparator: { $0.isWhitespace })
                .joined(separator: " ")
            guard !normalized.isEmpty, !seen.contains(normalized) else { return false }
            seen.insert(normalized)
            return true
        }
    }

    private func clip(_ value: String, to limit: Int) -> String {
        guard value.count > limit else { return value }
        return String(value.prefix(limit))
    }
}

private func copyAttribute(_ element: AXUIElement, _ attribute: String) -> CFTypeRef? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, attribute as CFString, &value) == .success else {
        return nil
    }
    return value
}

private func stringAttribute(_ element: AXUIElement, _ attribute: String) -> String? {
    copyAttribute(element, attribute) as? String
}

private func elementAttribute(_ element: AXUIElement, _ attribute: String) -> AXUIElement? {
    guard let value = copyAttribute(element, attribute), CFGetTypeID(value) == AXUIElementGetTypeID() else {
        return nil
    }
    return unsafeBitCast(value, to: AXUIElement.self)
}

private func elementArrayAttribute(_ element: AXUIElement, _ attribute: String) -> [AXUIElement]? {
    guard let values = copyAttribute(element, attribute) as? [CFTypeRef] else { return nil }
    return values.compactMap { value in
        guard CFGetTypeID(value) == AXUIElementGetTypeID() else { return nil }
        return unsafeBitCast(value, to: AXUIElement.self)
    }
}

private func frame(of element: AXUIElement) -> CGRect? {
    guard let positionRef = copyAttribute(element, kAXPositionAttribute),
          let sizeRef = copyAttribute(element, kAXSizeAttribute),
          CFGetTypeID(positionRef) == AXValueGetTypeID(),
          CFGetTypeID(sizeRef) == AXValueGetTypeID() else {
        return nil
    }

    let positionValue = unsafeBitCast(positionRef, to: AXValue.self)
    let sizeValue = unsafeBitCast(sizeRef, to: AXValue.self)
    var point = CGPoint.zero
    var size = CGSize.zero
    guard AXValueGetValue(positionValue, .cgPoint, &point),
          AXValueGetValue(sizeValue, .cgSize, &size) else {
        return nil
    }
    return CGRect(origin: point, size: size)
}

private func rangeAttribute(_ element: AXUIElement, _ attribute: String) -> CFRange? {
    guard let rangeRef = copyAttribute(element, attribute),
          CFGetTypeID(rangeRef) == AXValueGetTypeID() else {
        return nil
    }
    let rangeValue = unsafeBitCast(rangeRef, to: AXValue.self)
    var range = CFRange(location: 0, length: 0)
    guard AXValueGetValue(rangeValue, .cfRange, &range) else { return nil }
    return range
}
