import XCTest
@testable import CoupledCore

final class VisualChangeTests: XCTestCase {
    func testRejectsSinglePixelNoise() {
        let before = VisualFrameFingerprint(
            width: 10,
            height: 10,
            samples: Array(repeating: 0, count: 100)
        )
        var changed = before.samples
        changed[50] = 255
        let after = VisualFrameFingerprint(width: 10, height: 10, samples: changed)

        let result = visualDifference(
            from: before,
            to: after,
            pixelThreshold: 20,
            fractionThreshold: 0.02
        )
        XCTAssertEqual(result?.changedSampleCount, 1)
        XCTAssertEqual(result?.isMaterial, false)
    }

    func testAcceptsDistributedTextLikeChange() {
        let before = VisualFrameFingerprint(
            width: 10,
            height: 10,
            samples: Array(repeating: 0, count: 100)
        )
        var changed = before.samples
        for index in 40..<50 {
            changed[index] = 180
        }
        let after = VisualFrameFingerprint(width: 10, height: 10, samples: changed)

        let result = visualDifference(
            from: before,
            to: after,
            pixelThreshold: 20,
            fractionThreshold: 0.02
        )
        XCTAssertEqual(result?.changedSampleCount, 10)
        XCTAssertEqual(result?.isMaterial, true)
    }

    func testRejectsMismatchedFingerprints() {
        let first = VisualFrameFingerprint(width: 1, height: 2, samples: [0, 0])
        let second = VisualFrameFingerprint(width: 2, height: 1, samples: [0, 0])
        XCTAssertNil(
            visualDifference(
                from: first,
                to: second,
                pixelThreshold: 20,
                fractionThreshold: 0.01
            )
        )
    }
}
