import CoreGraphics
import Foundation
import ImageIO

public enum ReadImageSimilarityError: Error {
    case couldNotDecode(String)
    case couldNotRender(String)
}

/// Resolution-independent grayscale SSIM for determining whether two sensor
/// pathways observed the same screen state. Images are decoded one pair at a
/// time and reduced to a bounded square buffer so replay memory is bounded.
///
/// Window and visual-frame capture can disagree about the exact image origin
/// by a narrow border even when both observed the same application pixels. A
/// small translation search aligns those capture origins before SSIM. It does
/// not scale, crop, or otherwise deform either observation.
public func normalizedReadImageSSIM(
    _ first: URL,
    _ second: URL,
    dimension: Int = 96,
    maximumTranslationFraction: Double = 0.07,
    firstRegionOfInterest: CGRect? = nil,
    secondRegionOfInterest: CGRect? = nil
) throws -> Double {
    let left = try normalizedReadGrayscale(
        first, dimension: dimension, regionOfInterest: firstRegionOfInterest
    )
    let right = try normalizedReadGrayscale(
        second, dimension: dimension, regionOfInterest: secondRegionOfInterest
    )
    guard left.count == right.count, !left.isEmpty else {
        throw ReadImageSimilarityError.couldNotRender("incompatible buffers")
    }
    let direct = readImageSSIM(
        left, right, dimension: dimension, offsetX: 0, offsetY: 0
    )
    guard direct < 0.995, maximumTranslationFraction > 0 else { return direct }

    let maximumOffset = min(
        max(0, Int((Double(dimension) * maximumTranslationFraction).rounded())),
        dimension / 5
    )
    var bestOffset = (x: 0, y: 0)
    var bestError = readImageSampledMeanSquaredError(
        left, right, dimension: dimension, offsetX: 0, offsetY: 0
    )
    func consider(_ offsetX: Int, _ offsetY: Int) {
        guard abs(offsetX) <= maximumOffset, abs(offsetY) <= maximumOffset,
              offsetX != 0 || offsetY != 0 else { return }
        let error = readImageSampledMeanSquaredError(
            left, right, dimension: dimension,
            offsetX: offsetX, offsetY: offsetY
        )
        if error < bestError {
            bestError = error
            bestOffset = (offsetX, offsetY)
        }
    }
    let coarseStep = max(2, maximumOffset / 4)
    for offsetY in stride(
        from: -maximumOffset, through: maximumOffset, by: coarseStep
    ) {
        for offsetX in stride(
            from: -maximumOffset, through: maximumOffset, by: coarseStep
        ) {
            consider(offsetX, offsetY)
        }
    }
    if coarseStep > 1 {
        let coarseBest = bestOffset
        for offsetY in (coarseBest.y - coarseStep + 1)...(coarseBest.y + coarseStep - 1) {
            for offsetX in (coarseBest.x - coarseStep + 1)...(coarseBest.x + coarseStep - 1) {
                consider(offsetX, offsetY)
            }
        }
    }
    return max(
        direct,
        readImageSSIM(
            left, right, dimension: dimension,
            offsetX: bestOffset.x, offsetY: bestOffset.y
        )
    )
}

private func readImageOverlap(
    dimension: Int,
    offsetX: Int,
    offsetY: Int
) -> (Range<Int>, Range<Int>)? {
    let xStart = max(0, -offsetX)
    let xEnd = min(dimension, dimension - offsetX)
    let yStart = max(0, -offsetY)
    let yEnd = min(dimension, dimension - offsetY)
    guard xStart < xEnd, yStart < yEnd else { return nil }
    return (xStart..<xEnd, yStart..<yEnd)
}

private func readImageSampledMeanSquaredError(
    _ left: [UInt8],
    _ right: [UInt8],
    dimension: Int,
    offsetX: Int,
    offsetY: Int
) -> Double {
    guard let (xs, ys) = readImageOverlap(
        dimension: dimension, offsetX: offsetX, offsetY: offsetY
    ) else { return .infinity }
    var squaredError = 0.0
    var count = 0
    for y in stride(from: ys.lowerBound, to: ys.upperBound, by: 3) {
        for x in stride(from: xs.lowerBound, to: xs.upperBound, by: 3) {
            let lhs = Double(left[y * dimension + x])
            let rhs = Double(right[(y + offsetY) * dimension + x + offsetX])
            let difference = lhs - rhs
            squaredError += difference * difference
            count += 1
        }
    }
    return count > 0 ? squaredError / Double(count) : .infinity
}

private func readImageSSIM(
    _ left: [UInt8],
    _ right: [UInt8],
    dimension: Int,
    offsetX: Int,
    offsetY: Int
) -> Double {
    guard let (xs, ys) = readImageOverlap(
        dimension: dimension, offsetX: offsetX, offsetY: offsetY
    ) else { return -1 }
    let count = Double(xs.count * ys.count)
    var leftSum = 0.0
    var rightSum = 0.0
    for y in ys {
        for x in xs {
            leftSum += Double(left[y * dimension + x])
            rightSum += Double(right[(y + offsetY) * dimension + x + offsetX])
        }
    }
    let leftMean = leftSum / count
    let rightMean = rightSum / count
    var leftVariance = 0.0
    var rightVariance = 0.0
    var covariance = 0.0
    for y in ys {
        for x in xs {
            let a = Double(left[y * dimension + x]) - leftMean
            let b = Double(right[(y + offsetY) * dimension + x + offsetX])
                - rightMean
            leftVariance += a * a
            rightVariance += b * b
            covariance += a * b
        }
    }
    leftVariance /= count
    rightVariance /= count
    covariance /= count
    let c1 = pow(0.01 * 255.0, 2.0) as Double
    let c2 = pow(0.03 * 255.0, 2.0) as Double
    return ((2 * leftMean * rightMean + c1) * (2 * covariance + c2))
        / ((leftMean * leftMean + rightMean * rightMean + c1)
            * (leftVariance + rightVariance + c2))
}

private func normalizedReadGrayscale(
    _ url: URL,
    dimension: Int,
    regionOfInterest: CGRect?
) throws -> [UInt8] {
    guard dimension > 0,
          let source = CGImageSourceCreateWithURL(url as CFURL, nil),
          let decoded = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        throw ReadImageSimilarityError.couldNotDecode(url.path)
    }
    let image: CGImage
    if let regionOfInterest {
        let normalized = CGRect(
            x: max(0, min(1, regionOfInterest.origin.x)),
            y: max(0, min(1, regionOfInterest.origin.y)),
            width: max(0, min(1 - regionOfInterest.origin.x, regionOfInterest.width)),
            height: max(0, min(1 - regionOfInterest.origin.y, regionOfInterest.height))
        )
        let pixelRegion = CGRect(
            x: normalized.origin.x * Double(decoded.width),
            y: normalized.origin.y * Double(decoded.height),
            width: normalized.width * Double(decoded.width),
            height: normalized.height * Double(decoded.height)
        ).integral.intersection(CGRect(
            x: 0, y: 0, width: decoded.width, height: decoded.height
        ))
        guard pixelRegion.width >= 1, pixelRegion.height >= 1,
              let cropped = decoded.cropping(to: pixelRegion) else {
            throw ReadImageSimilarityError.couldNotRender(url.path)
        }
        image = cropped
    } else {
        image = decoded
    }
    var pixels = Array(repeating: UInt8(0), count: dimension * dimension)
    let rendered = pixels.withUnsafeMutableBytes { bytes -> Bool in
        guard let base = bytes.baseAddress,
              let context = CGContext(
                data: base,
                width: dimension,
                height: dimension,
                bitsPerComponent: 8,
                bytesPerRow: dimension,
                space: CGColorSpaceCreateDeviceGray(),
                bitmapInfo: CGImageAlphaInfo.none.rawValue
              ) else { return false }
        context.interpolationQuality = .high
        context.draw(
            image,
            in: CGRect(x: 0, y: 0, width: dimension, height: dimension)
        )
        return true
    }
    guard rendered else {
        throw ReadImageSimilarityError.couldNotRender(url.path)
    }
    return pixels
}
