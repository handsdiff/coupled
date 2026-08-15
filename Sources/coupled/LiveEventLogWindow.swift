import AppKit
import CoupledCore
import Foundation

extension Notification.Name {
    static let coupledDidEmitDerivedEvent = Notification.Name(
        "com.niyant.coupled.did-emit-derived-event"
    )
}

final class LiveEventLogWindowController: NSObject, NSWindowDelegate {
    static let shared = LiveEventLogWindowController()

    private let maximumCharacters = 250_000
    private var window: NSWindow?
    private var textView: NSTextView?

    private override init() {
        super.init()
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(receivedEvent(_:)),
            name: .coupledDidEmitDerivedEvent,
            object: nil
        )
    }

    func show() {
        let application = NSApplication.shared
        application.setActivationPolicy(.accessory)
        application.finishLaunching()

        if window == nil {
            makeWindow()
        }
        window?.orderFrontRegardless()
    }

    @objc private func receivedEvent(_ notification: Notification) {
        guard let data = notification.userInfo?["data"] as? Data,
              let formatted = LiveEventLogFormatter.format(data) else { return }
        DispatchQueue.main.async { [weak self] in
            self?.append(formatted)
        }
    }

    private func makeWindow() {
        let size = NSSize(width: 900, height: 560)
        let screen = NSScreen.screens.dropFirst().first ?? NSScreen.main
        let visible = screen?.visibleFrame ?? NSRect(origin: .zero, size: size)
        let origin = NSPoint(
            x: max(visible.minX, visible.maxX - size.width - 24),
            y: max(visible.minY, visible.minY + 24)
        )
        let frame = NSRect(origin: origin, size: size)
        let window = NSWindow(
            contentRect: frame,
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false,
            screen: screen
        )
        window.title = "Coupled Live Events"
        window.level = .normal
        window.isReleasedWhenClosed = false
        window.delegate = self

        let scrollView = NSScrollView(frame: NSRect(origin: .zero, size: size))
        scrollView.autoresizingMask = [.width, .height]
        scrollView.hasVerticalScroller = true
        scrollView.hasHorizontalScroller = true
        scrollView.borderType = .noBorder

        let textView = NSTextView(frame: scrollView.bounds)
        textView.autoresizingMask = [.width]
        textView.isEditable = false
        textView.isSelectable = true
        textView.isRichText = false
        textView.importsGraphics = false
        textView.usesFindPanel = true
        textView.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        textView.textColor = .labelColor
        textView.backgroundColor = .textBackgroundColor
        textView.textContainer?.widthTracksTextView = false
        textView.textContainer?.containerSize = NSSize(
            width: CGFloat.greatestFiniteMagnitude,
            height: CGFloat.greatestFiniteMagnitude
        )
        scrollView.documentView = textView
        window.contentView = scrollView

        self.window = window
        self.textView = textView
    }

    private func append(_ message: String) {
        guard let textView, let storage = textView.textStorage else { return }
        let separator = storage.length == 0 ? "" : "\n\n"
        storage.append(NSAttributedString(string: separator + message))
        if storage.length > maximumCharacters {
            let excess = storage.length - maximumCharacters
            let source = storage.string as NSString
            let nextNewline = source.range(
                of: "\n",
                options: [],
                range: NSRange(location: excess, length: source.length - excess)
            )
            let removalEnd = nextNewline.location == NSNotFound
                ? excess
                : nextNewline.location + nextNewline.length
            storage.deleteCharacters(in: NSRange(location: 0, length: removalEnd))
        }
        textView.scrollToEndOfDocument(nil)
    }
}
