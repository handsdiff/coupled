import CoreGraphics

/// Removes equal side borders plus top and bottom borders from a viewport.
public func croppedViewport(
    in bounds: CGRect,
    sideCropFraction: Double,
    topCropFraction: Double,
    bottomCropFraction: Double
) -> CGRect {
    precondition(sideCropFraction >= 0 && sideCropFraction < 0.5)
    precondition(topCropFraction >= 0)
    precondition(bottomCropFraction >= 0)
    precondition(topCropFraction + bottomCropFraction < 1)

    let sideInset = bounds.width * sideCropFraction
    let topInset = bounds.height * topCropFraction
    return CGRect(
        x: bounds.minX + sideInset,
        y: bounds.minY + topInset,
        width: bounds.width - 2 * sideInset,
        height: bounds.height * (1 - topCropFraction - bottomCropFraction)
    )
}

/// Chromium browsers can publish tab strips, address bars, menus, and
/// scrollbars as separate Core Graphics windows. Keep them in raw capture, but
/// do not treat these small surfaces as primary reading viewports.
public func isChromiumBrowserBundleIdentifier(_ bundleIdentifier: String?) -> Bool {
    bundleIdentifier == "com.google.Chrome"
        || bundleIdentifier == "company.thebrowser.Browser"
}

public func isChromiumAuxiliarySurface(
    bundleIdentifier: String?,
    width: Double,
    height: Double
) -> Bool {
    isChromiumBrowserBundleIdentifier(bundleIdentifier)
        && (height < 300 || width < 100)
}
