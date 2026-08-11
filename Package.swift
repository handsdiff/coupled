// swift-tools-version: 5.10

import PackageDescription

let package = Package(
    name: "Coupled",
    platforms: [.macOS(.v13)],
    products: [
        .library(name: "CoupledCore", targets: ["CoupledCore"]),
        .executable(name: "coupled", targets: ["coupled"]),
    ],
    targets: [
        .target(name: "CoupledCore"),
        .executableTarget(
            name: "coupled",
            dependencies: ["CoupledCore"]
        ),
        .testTarget(
            name: "CoupledCoreTests",
            dependencies: ["CoupledCore"]
        ),
    ]
)
