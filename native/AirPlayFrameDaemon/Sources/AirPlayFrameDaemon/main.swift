@preconcurrency import AVFoundation
import AirPlayFrameCore
import CoreImage
import CoreMediaIO
import CryptoKit
import Darwin
import Foundation

_ = umask(0o077)

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
            userInfo: [NSLocalizedDescriptionKey: "CoreMediaIO wireless capture opt-in failed"]
        )
    }
}

private struct DiscoveredDevice: Codable {
    let localizedName: String
    let uniqueID: String

    enum CodingKeys: String, CodingKey {
        case localizedName = "localized_name"
        case uniqueID = "unique_id"
    }
}

private final class FrameSink: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    private let binding: RoomCaptureBinding
    private let store: FrameStore
    private let context = CIContext(options: [.cacheIntermediates: false])
    private let minimumInterval: TimeInterval
    private let onFirstFrame: () -> Void
    private var lastPublishedAt = Date.distantPast
    private var publishedFirstFrame = false

    init(
        binding: RoomCaptureBinding,
        store: FrameStore,
        minimumInterval: TimeInterval,
        onFirstFrame: @escaping () -> Void
    ) {
        self.binding = binding
        self.store = store
        self.minimumInterval = minimumInterval
        self.onFirstFrame = onFirstFrame
    }

    func captureOutput(
        _ output: AVCaptureOutput,
        didOutput sampleBuffer: CMSampleBuffer,
        from connection: AVCaptureConnection
    ) {
        let now = Date()
        guard now.timeIntervalSince(lastPublishedAt) >= minimumInterval,
              let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        lastPublishedAt = now
        let image = CIImage(cvPixelBuffer: pixelBuffer)
        let colorSpace = CGColorSpaceCreateDeviceRGB()
        guard let png = context.pngRepresentation(
            of: image,
            format: .BGRA8,
            colorSpace: colorSpace,
            options: [:]
        ) else {
            _ = try? store.publishStatus(
                "degraded",
                errorCode: "png_encode_failed",
                binding: binding
            )
            return
        }
        do {
            try store.publishFrame(
                png: png,
                width: CVPixelBufferGetWidth(pixelBuffer),
                height: CVPixelBufferGetHeight(pixelBuffer),
                binding: binding,
                capturedAt: now
            )
            if !publishedFirstFrame {
                publishedFirstFrame = true
                onFirstFrame()
            }
        } catch {
            fputs("frame publish failed for \(binding.roomKey)\n", stderr)
        }
    }
}

private struct RetainedSession {
    let session: AVCaptureSession
    let output: AVCaptureVideoDataOutput
    let sink: FrameSink
    let device: AVCaptureDevice
}

private final class CaptureDaemon {
    private let config: CaptureDaemonConfig
    private let store: FrameStore
    private let sessionQueue = DispatchQueue(label: "com.home-media.airplay-frame.sessions")
    private var observerTokens: [NSObjectProtocol] = []
    private var sessions: [String: RetainedSession] = [:]
    private var retainedDevices: [String: AVCaptureDevice] = [:]
    private var readyRooms: Set<String> = []

    init(config: CaptureDaemonConfig, store: FrameStore) {
        self.config = config
        self.store = store
    }

    func start() throws {
        for binding in config.rooms {
            try store.publishStatus("discovering", binding: binding)
        }
        observerTokens.append(NotificationCenter.default.addObserver(
            forName: AVCaptureDevice.wasConnectedNotification,
            object: nil,
            queue: nil
        ) { [weak self] notification in
            guard let device = notification.object as? AVCaptureDevice else { return }
            self?.deviceConnected(device)
        })
        observerTokens.append(NotificationCenter.default.addObserver(
            forName: AVCaptureDevice.wasDisconnectedNotification,
            object: nil,
            queue: nil
        ) { [weak self] notification in
            guard let device = notification.object as? AVCaptureDevice else { return }
            self?.deviceDisconnected(device)
        })
        try enableCMIODevice(kCMIOHardwarePropertyAllowScreenCaptureDevices)
        try enableCMIODevice(kCMIOHardwarePropertyAllowWirelessScreenCaptureDevices)
    }

    private func deviceConnected(_ device: AVCaptureDevice) {
        sessionQueue.async { [weak self] in
            guard let self else { return }
            retainedDevices[device.uniqueID] = device
            guard let binding = config.rooms.first(where: {
                $0.captureUniqueID == device.uniqueID
            }) else { return }
            guard sessions[binding.roomKey] == nil else { return }
            if let expected = binding.expectedLocalizedName,
               expected != device.localizedName {
                _ = try? store.publishStatus(
                    "blocked",
                    errorCode: "capture_identity_mismatch",
                    binding: binding
                )
                return
            }
            do {
                try startSession(device: device, binding: binding)
            } catch {
                _ = try? store.publishStatus(
                    "degraded",
                    errorCode: "capture_setup_failed",
                    binding: binding
                )
            }
        }
    }

    private func deviceDisconnected(_ device: AVCaptureDevice) {
        sessionQueue.async { [weak self] in
            guard let self else { return }
            retainedDevices.removeValue(forKey: device.uniqueID)
            guard let binding = config.rooms.first(where: {
                $0.captureUniqueID == device.uniqueID
            }), let retained = sessions.removeValue(forKey: binding.roomKey) else { return }
            retained.session.stopRunning()
            readyRooms.remove(binding.roomKey)
            _ = try? store.publishStatus(
                "degraded",
                errorCode: "capture_device_disconnected",
                binding: binding
            )
        }
    }

    private func startSession(device: AVCaptureDevice, binding: RoomCaptureBinding) throws {
        let session = AVCaptureSession()
        let input = try AVCaptureDeviceInput(device: device)
        let output = AVCaptureVideoDataOutput()
        let sink = FrameSink(
            binding: binding,
            store: store,
            minimumInterval: config.frameInterval,
            onFirstFrame: { [weak self] in
                self?.sessionQueue.async { [weak self] in
                    self?.readyRooms.insert(binding.roomKey)
                }
            }
        )
        output.alwaysDiscardsLateVideoFrames = true
        output.videoSettings = [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
        ]
        output.setSampleBufferDelegate(
            sink,
            queue: DispatchQueue(label: "com.home-media.airplay-frame.\(binding.roomKey)")
        )
        session.beginConfiguration()
        if session.canSetSessionPreset(.hd1280x720) {
            session.sessionPreset = .hd1280x720
        }
        guard session.canAddInput(input), session.canAddOutput(output) else {
            session.commitConfiguration()
            throw NSError(
                domain: "AirPlayFrameDaemon",
                code: 2,
                userInfo: [NSLocalizedDescriptionKey: "capture input/output rejected"]
            )
        }
        session.addInput(input)
        session.addOutput(output)
        session.commitConfiguration()
        try store.publishStatus("capture_starting", binding: binding)
        session.startRunning()
        sessions[binding.roomKey] = RetainedSession(
            session: session,
            output: output,
            sink: sink,
            device: device
        )
        sessionQueue.asyncAfter(deadline: .now() + 15) { [weak self] in
            guard let self,
                  sessions[binding.roomKey] != nil,
                  !readyRooms.contains(binding.roomKey) else { return }
            _ = try? store.publishStatus(
                "degraded",
                errorCode: "capture_permission_or_pairing_required",
                binding: binding
            )
        }
    }
}

private final class DiscoveryProbe {
    private var devices: [String: AVCaptureDevice] = [:]
    private var observer: NSObjectProtocol?

    func run(seconds: TimeInterval) throws -> [DiscoveredDevice] {
        observer = NotificationCenter.default.addObserver(
            forName: AVCaptureDevice.wasConnectedNotification,
            object: nil,
            queue: .main
        ) { [weak self] notification in
            guard let device = notification.object as? AVCaptureDevice else { return }
            self?.devices[device.uniqueID] = device
        }
        try enableCMIODevice(kCMIOHardwarePropertyAllowScreenCaptureDevices)
        try enableCMIODevice(kCMIOHardwarePropertyAllowWirelessScreenCaptureDevices)
        let deadline = Date(timeIntervalSinceNow: seconds)
        while Date() < deadline {
            RunLoop.current.run(until: min(deadline, Date(timeIntervalSinceNow: 0.1)))
        }
        if let observer { NotificationCenter.default.removeObserver(observer) }
        return devices.values.map {
            DiscoveredDevice(localizedName: $0.localizedName, uniqueID: $0.uniqueID)
        }.sorted {
            ($0.localizedName, $0.uniqueID) < ($1.localizedName, $1.uniqueID)
        }
    }
}

private func argument(after name: String) -> String? {
    guard let index = CommandLine.arguments.firstIndex(of: name) else { return nil }
    let next = CommandLine.arguments.index(after: index)
    return next < CommandLine.arguments.endIndex ? CommandLine.arguments[next] : nil
}

private func fail(_ message: String, code: Int32 = 1) -> Never {
    fputs("airplay-frame-daemon: \(message)\n", stderr)
    exit(code)
}

if CommandLine.arguments.contains("--discover") {
    let seconds = Double(argument(after: "--seconds") ?? "5") ?? 5
    do {
        let probe = DiscoveryProbe()
        let data = try JSONEncoder().encode(probe.run(seconds: seconds))
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data("\n".utf8))
        exit(0)
    } catch {
        fail("discovery failed: \(error)")
    }
}

guard let configPath = argument(after: "--config") else {
    fail("usage: airplay-frame-daemon --config CONFIG.json --state-dir DIRECTORY", code: 64)
}
do {
    let config = try CaptureDaemonConfig.load(from: URL(fileURLWithPath: configPath))
    if CommandLine.arguments.contains("--validate-config") {
        print("configuration valid: \(config.rooms.count) exact room binding(s)")
        exit(0)
    }
    guard let statePath = argument(after: "--state-dir") else {
        fail("--state-dir is required", code: 64)
    }
    let store = try FrameStore(root: URL(fileURLWithPath: statePath, isDirectory: true))
    let daemon = CaptureDaemon(config: config, store: store)
    try daemon.start()
    // Notification closures capture the daemon weakly to avoid cycles. Keep the
    // top-level owner alive explicitly for the entire run loop; Swift may end a
    // local's lifetime after its last syntactic use.
    withExtendedLifetime(daemon) {
        RunLoop.current.run()
    }
} catch {
    fail(String(describing: error))
}
