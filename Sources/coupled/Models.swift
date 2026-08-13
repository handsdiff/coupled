import ApplicationServices
import Foundation
import CoupledCore

struct AppContext: Encodable {
    let name: String
    let bundleIdentifier: String?
    let processIdentifier: Int32
}

struct WindowContext: Encodable {
    let title: String?
    let identifier: String
}

struct ReadTriggerContext: Encodable {
    let app: AppContext
    let window: WindowContext

    var contextIdentifier: String {
        "\(app.bundleIdentifier ?? app.name)|\(window.identifier)"
    }
}

struct RectValue: Encodable {
    let x: Double
    let y: Double
    let width: Double
    let height: Double
}

func rectValue(_ rect: CGRect) -> RectValue {
    RectValue(
        x: rect.origin.x,
        y: rect.origin.y,
        width: rect.width,
        height: rect.height
    )
}

struct DisplayContext {
    let id: UInt32
    let bounds: RectValue
}

struct MutatingWriteInput {
    let attemptID: String
    let observedAt: String
    let eventTimestampNanoseconds: UInt64
    let processIdentifier: Int32
}

func displayContext(at point: CGPoint) -> DisplayContext? {
    var displayID = CGDirectDisplayID()
    var count: UInt32 = 0
    guard CGGetDisplaysWithPoint(point, 1, &displayID, &count) == .success,
          count > 0 else {
        return nil
    }
    let bounds = CGDisplayBounds(displayID)
    return DisplayContext(
        id: displayID,
        bounds: RectValue(
            x: bounds.origin.x,
            y: bounds.origin.y,
            width: bounds.width,
            height: bounds.height
        )
    )
}

struct VisibleElement: Encodable {
    let role: String
    let title: String?
    let text: String
    let frame: RectValue?
}

struct EditableContext: Encodable {
    let identifier: String
    let role: String
    let title: String?
    let value: String
    let selectedText: String?
    let selectedRangeLocation: Int?
    let selectedRangeLength: Int?
    let valueWasTruncated: Bool
}

struct AccessibilityActivation: Encodable {
    let manualApplication: String
    let enhancedApplication: String
    let enhancedWindow: String
}

struct AccessibilitySnapshot: Encodable {
    let snapshotID: String
    let observedAt: String
    let reason: String
    let app: AppContext
    let window: WindowContext
    let focusedRole: String?
    let visibleText: String
    let visibleElements: [VisibleElement]
    let editable: EditableContext?
    let accessibilityActivation: AccessibilityActivation
    let visitedNodeCount: Int
    let hitNodeLimit: Bool
    let hitCharacterLimit: Bool

    var contextIdentifier: String {
        "\(app.bundleIdentifier ?? app.name)|\(window.identifier)"
    }
}

struct EditableObservation: Encodable {
    let observationID: String
    let observedAt: String
    let reason: String
    let app: AppContext
    let window: WindowContext
    let editable: EditableContext
}

struct RawActivityRecord: Encodable {
    let schemaVersion = 2
    let recordType = "input_activity"
    let recordID: String
    let observedAt: String
    let lastObservedAt: String
    let settledAt: String
    let activityTypes: [String]
    let eventCount: Int
    let triggerContext: ReadTriggerContext
    let settledContextIdentifier: String?
    let resolution: String
    let snapshotID: String?
}

struct RawSnapshotRecord: Encodable {
    let schemaVersion = 1
    let recordType = "accessibility_snapshot"
    let snapshot: AccessibilitySnapshot
}

struct RawEditableRecord: Encodable {
    let schemaVersion = 1
    let recordType = "editable_observation"
    let observation: EditableObservation
}

struct UnderstoodEvent: Encodable {
    let schemaVersion = 1
    let eventID: String
    let observedAt: String
    let kind: String
    let app: AppContext
    let window: WindowContext
    let content: String
    let newlyVisibleContent: String?
    let edit: TextEdit?
    let provenance: String?
    let sourceRecordIDs: [String]
    let metadata: [String: String]
}

func nowTimestamp() -> String {
    ISO8601DateFormatter.string(
        from: Date(),
        timeZone: TimeZone(secondsFromGMT: 0)!,
        formatOptions: [.withInternetDateTime, .withFractionalSeconds]
    )
}
