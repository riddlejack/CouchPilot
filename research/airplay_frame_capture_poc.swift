// Opt-in proof of concept for Apple TV's public CoreMediaIO capture surface.
//
// This file does nothing unless invoked with an exact device name and an output
// path. Opening a device can trigger the one-time AirPlay/TCC permission flow.
// Compile without connecting to a TV:
//
//   xcrun swiftc -parse research/airplay_frame_capture_poc.swift
//
// Explicit live probe (run only while watching the named Apple TV):
//
//   xcrun swift research/airplay_frame_capture_poc.swift \
//     --device "Living Room" --output /tmp/living-room-frame.png

import AVFoundation
import CoreImage
import CoreMediaIO
import Darwin
import Foundation

private func enableCMIODevice(_ selector: Int) throws {
    var address = CMIOObjectPropertyAddress(
        mSelector: UInt32(selector),
        mScope: UInt32(kCMIOObjectPropertyScopeGlobal),
        mElement: UInt32(kCMIOObjectPropertyElementMain)
    )
    var enabled: UInt32 = 1
    let status = CMIOObjectSetPropertyData(
        CMIOObjectID(kCMIOObjectSystemObject),
        &address,
        0,
        nil,
        UInt32(MemoryLayout<UInt32>.size),
        &enabled
    )
    guard status == noErr else {
        throw NSError(
            domain: NSOSStatusErrorDomain,
            code: Int(status),
            userInfo: [NSLocalizedDescriptionKey: "CMIO opt-in failed"]
        )
    }
}

private final class FirstFrameSink: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    let outputURL: URL
    let finished: DispatchSemaphore
    private let context = CIContext()
    private var wroteFrame = false

    init(outputURL: URL, finished: DispatchSemaphore) {
        self.outputURL = outputURL
        self.finished = finished
    }

    func captureOutput(
        _ output: AVCaptureOutput,
        didOutput sampleBuffer: CMSampleBuffer,
        from connection: AVCaptureConnection
    ) {
        guard !wroteFrame, let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else {
            return
        }
        wroteFrame = true
        let image = CIImage(cvPixelBuffer: pixelBuffer)
        let colorSpace = CGColorSpaceCreateDeviceRGB()
        do {
            try context.writePNGRepresentation(
                of: image,
                to: outputURL,
                format: .BGRA8,
                colorSpace: colorSpace
            )
            print("wrote first frame: \(outputURL.path)")
        } catch {
            fputs("frame write failed: \(error)\n", stderr)
        }
        finished.signal()
    }
}

private final class CaptureProbe {
    let targetName: String
    let outputURL: URL
    private let finished = DispatchSemaphore(value: 0)
    private let session = AVCaptureSession()
    private let videoOutput = AVCaptureVideoDataOutput()
    private let outputQueue = DispatchQueue(label: "apple-tv-frame-probe")
    private var sink: FirstFrameSink?
    private var observer: NSObjectProtocol?
    private var retainedDevices: [String: AVCaptureDevice] = [:]

    init(targetName: String, outputURL: URL) {
        self.targetName = targetName
        self.outputURL = outputURL
    }

    func run(timeoutSeconds: TimeInterval) throws {
        observer = NotificationCenter.default.addObserver(
            forName: AVCaptureDevice.wasConnectedNotification,
            object: nil,
            queue: .main
        ) { [weak self] notification in
            guard let self, let device = notification.object as? AVCaptureDevice else { return }
            // Wireless CMIO sources arrive here before they appear in the
            // ordinary AVCaptureDevice discovery list. Retain the object itself.
            retainedDevices[device.uniqueID] = device
            print("discovered: \(device.localizedName)")
            if device.localizedName == targetName, !session.isRunning {
                do {
                    try start(device: device)
                } catch {
                    fputs("capture setup failed: \(error)\n", stderr)
                    finished.signal()
                }
            }
        }

        try enableCMIODevice(kCMIOHardwarePropertyAllowScreenCaptureDevices)
        try enableCMIODevice(kCMIOHardwarePropertyAllowWirelessScreenCaptureDevices)

        let deadline = Date(timeIntervalSinceNow: timeoutSeconds)
        while finished.wait(timeout: .now()) != .success, Date() < deadline {
            RunLoop.current.run(until: min(deadline, Date(timeIntervalSinceNow: 0.1)))
        }

        if session.isRunning { session.stopRunning() }
        if let observer { NotificationCenter.default.removeObserver(observer) }
        guard FileManager.default.fileExists(atPath: outputURL.path) else {
            throw NSError(
                domain: "AppleTVFrameProbe",
                code: 1,
                userInfo: [NSLocalizedDescriptionKey: "No frame arrived before timeout"]
            )
        }
    }

    private func start(device: AVCaptureDevice) throws {
        let input = try AVCaptureDeviceInput(device: device)
        guard session.canAddInput(input), session.canAddOutput(videoOutput) else {
            throw NSError(
                domain: "AppleTVFrameProbe",
                code: 2,
                userInfo: [NSLocalizedDescriptionKey: "Capture input/output rejected"]
            )
        }
        let frameSink = FirstFrameSink(outputURL: outputURL, finished: finished)
        sink = frameSink
        videoOutput.alwaysDiscardsLateVideoFrames = true
        videoOutput.videoSettings = [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
        ]
        videoOutput.setSampleBufferDelegate(frameSink, queue: outputQueue)
        session.beginConfiguration()
        session.addInput(input)
        session.addOutput(videoOutput)
        session.commitConfiguration()
        session.startRunning()
    }
}

private func argument(after name: String) -> String? {
    guard let index = CommandLine.arguments.firstIndex(of: name) else { return nil }
    let next = CommandLine.arguments.index(after: index)
    return next < CommandLine.arguments.endIndex ? CommandLine.arguments[next] : nil
}

guard
    let deviceName = argument(after: "--device"),
    let outputPath = argument(after: "--output")
else {
    fputs("usage: airplay_frame_capture_poc.swift --device NAME --output FILE.png\n", stderr)
    exit(64)
}

do {
    let probe = CaptureProbe(
        targetName: deviceName,
        outputURL: URL(fileURLWithPath: outputPath)
    )
    try probe.run(timeoutSeconds: 30)
} catch {
    fputs("probe failed: \(error)\n", stderr)
    exit(1)
}
