// swift-tools-version: 5.10

import PackageDescription

let package = Package(
    name: "Coupled",
    platforms: [.macOS(.v13)],
    products: [
        .library(name: "CoupledCore", targets: ["CoupledCore"]),
        .executable(name: "coupled", targets: ["coupled"]),
        .executable(name: "coupled-logs", targets: ["coupled-logs"]),
    ],
    targets: [
        .target(name: "CoupledCore"),
        .executableTarget(
            name: "coupled",
            dependencies: ["CoupledCore"]
        ),
        .executableTarget(
            name: "coupled-logs",
            dependencies: ["CoupledCore"]
        ),
        .testTarget(
            name: "CoupledCoreTests",
            dependencies: ["CoupledCore"]
        ),
    ]
)
