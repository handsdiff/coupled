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
