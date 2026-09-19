// swift-tools-version: 5.10

import PackageDescription

let package = Package(
    name: "AirPlayFrameDaemon",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "airplay-frame-daemon", targets: ["AirPlayFrameDaemon"]),
        .library(name: "AirPlayFrameCore", targets: ["AirPlayFrameCore"]),
    ],
    targets: [
        .target(name: "AirPlayFrameCore"),
        .executableTarget(
            name: "AirPlayFrameDaemon",
            dependencies: ["AirPlayFrameCore"]
        ),
    ]
)
