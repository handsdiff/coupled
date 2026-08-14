import Foundation

/// Keys which can relocate the caret or selection without changing the
/// focused AX editable. They close an active write before the event is passed
/// to the application so the next mutation receives fresh conditioning.
public func isWriteSelectionBoundaryKey(
    keyCode: Int64,
    commandPressed: Bool
) -> Bool {
    let navigationCodes: Set<Int64> = [
        48,  // Tab / focus traversal
        115, // Home
        116, // Page Up
        119, // End
        121, // Page Down
        123, // Left Arrow
        124, // Right Arrow
        125, // Down Arrow
        126, // Up Arrow
    ]
    return navigationCodes.contains(keyCode)
        || (commandPressed && keyCode == 0) // Command-A / Select All
}
