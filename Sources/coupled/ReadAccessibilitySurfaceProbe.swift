import ApplicationServices
import Foundation

/// Bounded, metadata-only Accessibility evidence for the UI region beneath a
/// settled READ's interaction point. This is raw evidence, not a semantic pane
/// decision: no AX value or selected-text attribute is queried.
final class ReadAccessibilitySurfaceProbe {
    private let systemWideElement = AXUIElementCreateSystemWide()
    private let maximumAncestorCount = 12

    func capture(
        at point: CGPoint,
        expectedProcessIdentifier: Int32
    ) -> RawReadAccessibilitySurfaceProbe {
        let requestedAt = nowTimestamp()
        let started = DispatchTime.now().uptimeNanoseconds
        var hitElement: AXUIElement?
        let hitError = AXUIElementCopyElementAtPosition(
            systemWideElement,
            Float(point.x),
            Float(point.y),
            &hitElement
        )
        guard hitError == .success, var candidate = hitElement else {
            return result(
                requestedAt: requestedAt,
                started: started,
                point: point,
                expectedProcessIdentifier: expectedProcessIdentifier,
                hitProcessIdentifier: nil,
                nodes: [],
                errors: ["element_at_position:\(readAXErrorName(hitError))"],
                termination: "hit_test_failed"
            )
        }

        var hitPID: pid_t = 0
        let pidError = AXUIElementGetPid(candidate, &hitPID)
        guard pidError == .success,
              Int32(hitPID) == expectedProcessIdentifier else {
            let error = pidError == .success
                ? "hit_process_mismatch:\(hitPID)"
                : "hit_process:\(readAXErrorName(pidError))"
            return result(
                requestedAt: requestedAt,
                started: started,
                point: point,
                expectedProcessIdentifier: expectedProcessIdentifier,
                hitProcessIdentifier: pidError == .success ? Int32(hitPID) : nil,
                nodes: [],
                errors: [error],
                termination: "process_validation_failed"
            )
        }

        var nodes = [RawReadAccessibilityAncestor]()
        var errors = [String]()
        var visited = Set<CFHashCode>()
        var termination = "maximum_ancestor_count"
        for depth in 0..<maximumAncestorCount {
            let hash = CFHash(candidate)
            guard visited.insert(hash).inserted else {
                termination = "ancestor_cycle"
                break
            }
            var nodePID: pid_t = 0
            let nodePIDError = AXUIElementGetPid(candidate, &nodePID)
            var attributeErrors = [String]()
            let role = readAXString(candidate, kAXRoleAttribute, errors: &attributeErrors)
            let subrole = readAXString(
                candidate, kAXSubroleAttribute, errors: &attributeErrors
            )
            let title = readAXString(candidate, kAXTitleAttribute, errors: &attributeErrors)
            let description = readAXString(
                candidate, kAXDescriptionAttribute, errors: &attributeErrors
            )
            let identifier = readAXString(
                candidate, kAXIdentifierAttribute, errors: &attributeErrors
            )
            let frame = readAXFrame(candidate, errors: &attributeErrors)
            if nodePIDError != .success {
                attributeErrors.append("process:\(readAXErrorName(nodePIDError))")
            }
            nodes.append(RawReadAccessibilityAncestor(
                depth: depth,
                elementHash: UInt64(hash),
                processIdentifier: nodePIDError == .success ? Int32(nodePID) : nil,
                role: role,
                subrole: subrole,
                title: title,
                elementDescription: description,
                identifier: identifier,
                frame: frame.map(rectValue),
                errors: attributeErrors
            ))
            errors.append(contentsOf: attributeErrors.map { "ancestor_\(depth):\($0)" })

            var parentError = [String]()
            guard let parent = readAXElement(
                candidate, kAXParentAttribute, errors: &parentError
            ) else {
                errors.append(contentsOf: parentError.map { "ancestor_\(depth):\($0)" })
                termination = parentError.isEmpty ? "root_reached" : "parent_unavailable"
                break
            }
            candidate = parent
        }
        return result(
            requestedAt: requestedAt,
            started: started,
            point: point,
            expectedProcessIdentifier: expectedProcessIdentifier,
            hitProcessIdentifier: Int32(hitPID),
            nodes: nodes,
            errors: errors,
            termination: termination
        )
    }

    private func result(
        requestedAt: String,
        started: UInt64,
        point: CGPoint,
        expectedProcessIdentifier: Int32,
        hitProcessIdentifier: Int32?,
        nodes: [RawReadAccessibilityAncestor],
        errors: [String],
        termination: String
    ) -> RawReadAccessibilitySurfaceProbe {
        let elapsed = DispatchTime.now().uptimeNanoseconds - started
        return RawReadAccessibilitySurfaceProbe(
            schemaVersion: 1,
            source: "accessibility_element_at_position",
            requestedAt: requestedAt,
            capturedAt: nowTimestamp(),
            durationMilliseconds: Double(elapsed) / 1_000_000,
            pointerX: point.x,
            pointerY: point.y,
            expectedProcessIdentifier: expectedProcessIdentifier,
            hitProcessIdentifier: hitProcessIdentifier,
            maximumAncestorCount: maximumAncestorCount,
            termination: termination,
            ancestors: nodes,
            errors: errors
        )
    }
}

struct RawReadAccessibilitySurfaceProbe: Encodable {
    let schemaVersion: Int
    let source: String
    let requestedAt: String
    let capturedAt: String
    let durationMilliseconds: Double
    let pointerX: Double
    let pointerY: Double
    let expectedProcessIdentifier: Int32
    let hitProcessIdentifier: Int32?
    let maximumAncestorCount: Int
    let termination: String
    let ancestors: [RawReadAccessibilityAncestor]
    let errors: [String]
}

struct RawReadAccessibilityAncestor: Encodable {
    let depth: Int
    let elementHash: UInt64
    let processIdentifier: Int32?
    let role: String?
    let subrole: String?
    let title: String?
    let elementDescription: String?
    let identifier: String?
    let frame: RectValue?
    let errors: [String]
}

private func readAXCopyAttribute(
    _ element: AXUIElement,
    _ attribute: String,
    errors: inout [String]
) -> CFTypeRef? {
    var value: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(element, attribute as CFString, &value)
    if error != .success && error != .noValue && error != .attributeUnsupported {
        errors.append("\(attribute):\(readAXErrorName(error))")
    }
    return error == .success ? value : nil
}

private func readAXString(
    _ element: AXUIElement,
    _ attribute: String,
    errors: inout [String]
) -> String? {
    readAXCopyAttribute(element, attribute, errors: &errors) as? String
}

private func readAXElement(
    _ element: AXUIElement,
    _ attribute: String,
    errors: inout [String]
) -> AXUIElement? {
    guard let value = readAXCopyAttribute(element, attribute, errors: &errors),
          CFGetTypeID(value) == AXUIElementGetTypeID() else { return nil }
    return unsafeBitCast(value, to: AXUIElement.self)
}

private func readAXFrame(
    _ element: AXUIElement,
    errors: inout [String]
) -> CGRect? {
    guard let positionValue = readAXCopyAttribute(
        element, kAXPositionAttribute, errors: &errors
    ), let sizeValue = readAXCopyAttribute(
        element, kAXSizeAttribute, errors: &errors
    ), CFGetTypeID(positionValue) == AXValueGetTypeID(),
       CFGetTypeID(sizeValue) == AXValueGetTypeID() else { return nil }
    let positionAX = unsafeBitCast(positionValue, to: AXValue.self)
    let sizeAX = unsafeBitCast(sizeValue, to: AXValue.self)
    var position = CGPoint.zero
    var size = CGSize.zero
    guard AXValueGetValue(positionAX, .cgPoint, &position),
          AXValueGetValue(sizeAX, .cgSize, &size) else {
        errors.append("frame:invalid_ax_value")
        return nil
    }
    return CGRect(origin: position, size: size)
}

private func readAXErrorName(_ error: AXError) -> String {
    switch error {
    case .success: return "success"
    case .failure: return "failure"
    case .illegalArgument: return "illegal_argument"
    case .invalidUIElement: return "invalid_ui_element"
    case .invalidUIElementObserver: return "invalid_ui_element_observer"
    case .cannotComplete: return "cannot_complete"
    case .attributeUnsupported: return "attribute_unsupported"
    case .actionUnsupported: return "action_unsupported"
    case .notificationUnsupported: return "notification_unsupported"
    case .notImplemented: return "not_implemented"
    case .notificationAlreadyRegistered: return "notification_already_registered"
    case .notificationNotRegistered: return "notification_not_registered"
    case .apiDisabled: return "api_disabled"
    case .noValue: return "no_value"
    case .parameterizedAttributeUnsupported: return "parameterized_attribute_unsupported"
    case .notEnoughPrecision: return "not_enough_precision"
    @unknown default: return "unknown_\(error.rawValue)"
    }
}
