// Opt-in diagnostic for Apple's public CoreMediaIO wireless screen-capture surface.
//
// Compile only (does not enumerate or open a device):
//   xcrun swiftc scripts/airplay_capture_probe.swift -o /tmp/airplay-capture-probe
//
// Enumerate locally for an exact private binding (does not open a capture session):
//   /tmp/airplay-capture-probe --enumerate --seconds 8
//
// Explicit capture. OUTPUT_DIR must not already exist. The probe opens only the
// exact unique ID and never chooses a device by name:
//   /tmp/airplay-capture-probe --capture \
//     --unique-id '<exact_private_cmio_id>' \
//     --expected-name 'Bedroom' \
//     --output-dir /tmp/airplay-capture-$(date +%s)

@preconcurrency import AVFoundation
import CoreImage
import CoreGraphics
import CoreMediaIO
import CryptoKit
import Darwin
import Foundation

private let captureTimeoutSeconds: TimeInterval = 15
private let requiredFrameCount = 10
private let videoPathDiagnosticInterval: TimeInterval = 0.25
private let maximumVideoPathDiagnosticSamples =
    Int(captureTimeoutSeconds / videoPathDiagnosticInterval) + 2

private func formatOSType(_ value: OSType) -> String {
    let bytes: [UInt8] = [
        UInt8((value >> 24) & 0xff),
        UInt8((value >> 16) & 0xff),
        UInt8((value >> 8) & 0xff),
        UInt8(value & 0xff),
    ]
    let printable = bytes.allSatisfy { (0x20...0x7e).contains($0) }
    if printable, let label = String(bytes: bytes, encoding: .ascii) {
        return String(format: "%@ (0x%08x)", label, value)
    }
    return String(format: "0x%08x", value)
}

private struct ListedDevice: Codable {
    let name: String
    let uniqueID: String

    enum CodingKeys: String, CodingKey {
        case name
        case uniqueID = "unique_id"
    }
}

private struct FrameRecord: Codable {
    let index: Int
    let filename: String
    let width: Int
    let height: Int
    let ptsValue: Int64
    let ptsTimescale: Int32
    let ptsSeconds: Double
    let sha256: String

    enum CodingKeys: String, CodingKey {
        case index
        case filename
        case width
        case height
        case ptsValue = "pts_value"
        case ptsTimescale = "pts_timescale"
        case ptsSeconds = "pts_seconds"
        case sha256
    }
}

private struct CaptureManifest: Codable {
    let status: String
    let stage: String
    let errorCode: String?
    let errorMessage: String?
    let deviceName: String
    let deviceUniqueID: String
    let screenCapturePreflight: Bool
    let videoAuthorization: String
    let sessionRunningObserved: Bool
    let videoPathDiagnosticSamples: Int
    let videoConnectionExists: Bool?
    let videoConnectionEnabled: Bool?
    let videoConnectionActive: Bool?
    let videoConnectionEverActive: Bool
    let deviceConnected: Bool?
    let deviceSuspended: Bool?
    let activeFormatWidth: Int?
    let activeFormatHeight: Int?
    let activeFormatMediaSubtype: String?
    let outputAvailablePixelTypes: [String]
    let captureSessionOutputCount: Int?
    let frameCount: Int
    let distinctHashes: Int
    let timeoutSeconds: Int
    let frames: [FrameRecord]

    enum CodingKeys: String, CodingKey {
        case status
        case stage
        case errorCode = "error_code"
        case errorMessage = "error_message"
        case deviceName = "device_name"
        case deviceUniqueID = "device_unique_id"
        case screenCapturePreflight = "screen_capture_preflight"
        case videoAuthorization = "video_authorization"
        case sessionRunningObserved = "session_running_observed"
        case videoPathDiagnosticSamples = "video_path_diagnostic_samples"
        case videoConnectionExists = "video_connection_exists"
        case videoConnectionEnabled = "video_connection_enabled"
        case videoConnectionActive = "video_connection_active"
        case videoConnectionEverActive = "video_connection_ever_active"
        case deviceConnected = "device_connected"
        case deviceSuspended = "device_suspended"
        case activeFormatWidth = "active_format_width"
        case activeFormatHeight = "active_format_height"
        case activeFormatMediaSubtype = "active_format_media_subtype"
        case outputAvailablePixelTypes = "output_available_pixel_types"
        case captureSessionOutputCount = "capture_session_output_count"
        case frameCount = "frame_count"
        case distinctHashes = "distinct_hashes"
        case timeoutSeconds = "timeout_seconds"
        case frames
    }
}

private struct VideoPathDiagnostics {
    var sampleCount = 0
    var connectionExists: Bool?
    var connectionEnabled: Bool?
    var connectionActive: Bool?
    var connectionEverActive = false
    var deviceConnected: Bool?
    var deviceSuspended: Bool?
    var activeFormatWidth: Int?
    var activeFormatHeight: Int?
    var activeFormatMediaSubtype: String?
    var outputAvailablePixelTypes: [String] = []
    var sessionOutputCount: Int?
}

private enum ProbeError: LocalizedError {
    case outputExists
    case outputCreationFailed
    case cmioOptIn
    case expectedNameMismatch
    case captureSetupFailed
    case sessionDidNotRun
    case nonIncreasingPTS
    case invalidPTS
    case frameEncodingFailed
    case frameWriteFailed
    case staticFrames
    case captureRuntimeFailed
    case timeout

    var errorDescription: String? {
        switch self {
        case .outputExists: "output directory already exists"
        case .outputCreationFailed: "private output directory could not be created"
        case .cmioOptIn: "CoreMediaIO capture-device opt-in failed"
        case .expectedNameMismatch: "exact unique ID has a different localized name"
        case .captureSetupFailed: "capture input or output could not be configured"
        case .sessionDidNotRun: "capture session start returned without a running session"
        case .nonIncreasingPTS: "capture delivered a non-increasing presentation timestamp"
        case .invalidPTS: "capture delivered an invalid presentation timestamp"
        case .frameEncodingFailed: "a frame could not be encoded as PNG"
        case .frameWriteFailed: "a private frame artifact could not be written"
        case .staticFrames: "ten frames arrived but all encoded content hashes were identical"
        case .captureRuntimeFailed: "the capture session reported a runtime error"
        case .timeout: "ten accepted frames did not arrive within the fixed 15-second timeout"
        }
    }

    var code: String {
        switch self {
        case .outputExists: "output_exists"
        case .outputCreationFailed: "output_creation_failed"
        case .cmioOptIn: "cmio_opt_in_failed"
        case .expectedNameMismatch: "expected_name_mismatch"
        case .captureSetupFailed: "capture_setup_failed"
        case .sessionDidNotRun: "session_did_not_run"
        case .nonIncreasingPTS: "non_increasing_pts"
        case .invalidPTS: "invalid_pts"
        case .frameEncodingFailed: "frame_encoding_failed"
        case .frameWriteFailed: "frame_write_failed"
        case .staticFrames: "static_frames"
        case .captureRuntimeFailed: "capture_runtime_failed"
        case .timeout: "timeout"
        }
    }
}

private enum DiagnosticStage: Int {
    case outputDirectoryCreated
    case waitingForDevice
    case deviceMatched
    case inputCreated
    case sessionConfigured
    case startRunningCalled
    case startRunningReturned
    case sessionRunning
    case firstBuffer
    case tenFramesCollected
    case completed

    var label: String {
        switch self {
        case .outputDirectoryCreated: "output_directory_created"
        case .waitingForDevice: "waiting_for_device"
        case .deviceMatched: "device_matched"
        case .inputCreated: "input_created"
        case .sessionConfigured: "session_configured"
        case .startRunningCalled: "start_running_called"
        case .startRunningReturned: "start_running_returned"
        case .sessionRunning: "session_running"
        case .firstBuffer: "first_buffer"
        case .tenFramesCollected: "ten_frames_collected"
        case .completed: "completed"
        }
    }
}

private struct PermissionSnapshot {
    let screenCapturePreflight: Bool
    let videoAuthorization: String
}

private func permissionSnapshot() -> PermissionSnapshot {
    let videoAuthorization: String = switch AVCaptureDevice.authorizationStatus(for: .video) {
    case .authorized: "authorized"
    case .denied: "denied"
    case .restricted: "restricted"
    case .notDetermined: "not_determined"
    @unknown default: "unknown"
    }
    return PermissionSnapshot(
        screenCapturePreflight: CGPreflightScreenCaptureAccess(),
        videoAuthorization: videoAuthorization
    )
}

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
    guard status == noErr else { throw ProbeError.cmioOptIn }
}

private func enableCaptureDevices() throws {
    try enableCMIODevice(kCMIOHardwarePropertyAllowScreenCaptureDevices)
    try enableCMIODevice(kCMIOHardwarePropertyAllowWirelessScreenCaptureDevices)
}

private func makeVideoDiscoverySession() -> AVCaptureDevice.DiscoverySession {
    AVCaptureDevice.DiscoverySession(
        deviceTypes: [
            .external,
            .builtInWideAngleCamera,
            .continuityCamera,
            .deskViewCamera,
        ],
        mediaType: .video,
        position: .unspecified
    )
}

private func currentVideoDevices(
    from discoverySession: AVCaptureDevice.DiscoverySession
) -> [AVCaptureDevice] {
    // Building and repeatedly reading a discovery session initializes and
    // keeps AVFoundation's device graph active. Wireless screen sources may
    // still arrive only through wasConnectedNotification, whose actual device
    // object is retained separately by each probe.
    return discoverySession.devices
}

private func privateWrite(_ data: Data, to url: URL) throws {
    do {
        try data.write(to: url, options: .atomic)
        try FileManager.default.setAttributes(
            [.posixPermissions: NSNumber(value: Int16(0o600))],
            ofItemAtPath: url.path
        )
    } catch {
        throw ProbeError.frameWriteFailed
    }
}

private func createPrivateOutputDirectory(_ url: URL) throws {
    let manager = FileManager.default
    guard !manager.fileExists(atPath: url.path) else { throw ProbeError.outputExists }
    do {
        try manager.createDirectory(
            at: url,
            withIntermediateDirectories: false,
            attributes: [.posixPermissions: NSNumber(value: Int16(0o700))]
        )
        try manager.setAttributes(
            [.posixPermissions: NSNumber(value: Int16(0o700))],
            ofItemAtPath: url.path
        )
    } catch {
        throw ProbeError.outputCreationFailed
    }
}

private final class EnumerationProbe: @unchecked Sendable {
    private let discoverySession = makeVideoDiscoverySession()
    private var observer: NSObjectProtocol?
    private var devices: [String: AVCaptureDevice] = [:]

    private func seedExistingDevices() {
        for device in currentVideoDevices(from: discoverySession) {
            devices[device.uniqueID] = device
        }
    }

    func run(seconds: TimeInterval) throws -> [ListedDevice] {
        observer = NotificationCenter.default.addObserver(
            forName: AVCaptureDevice.wasConnectedNotification,
            object: nil,
            queue: .main
        ) { [weak self] notification in
            guard let device = notification.object as? AVCaptureDevice else { return }
            self?.devices[device.uniqueID] = device
        }
        seedExistingDevices()
        try enableCaptureDevices()
        seedExistingDevices()
        let deadline = Date(timeIntervalSinceNow: seconds)
        while Date() < deadline {
            RunLoop.current.run(until: min(deadline, Date(timeIntervalSinceNow: 0.1)))
            seedExistingDevices()
        }
        if let observer { NotificationCenter.default.removeObserver(observer) }
        return devices.values.map {
            ListedDevice(name: $0.localizedName, uniqueID: $0.uniqueID)
        }.sorted {
            ($0.name, $0.uniqueID) < ($1.name, $1.uniqueID)
        }
    }
}

private final class FrameSink: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    private let outputDirectory: URL
    private let completion: (Result<[FrameRecord], Error>) -> Void
    private let onFirstBuffer: () -> Void
    private let onRecord: (FrameRecord) -> Void
    private let context = CIContext(options: [.cacheIntermediates: false])
    private var records: [FrameRecord] = []
    private var lastPTS: CMTime?
    private var finished = false

    init(
        outputDirectory: URL,
        onFirstBuffer: @escaping () -> Void,
        onRecord: @escaping (FrameRecord) -> Void,
        completion: @escaping (Result<[FrameRecord], Error>) -> Void
    ) {
        self.outputDirectory = outputDirectory
        self.onFirstBuffer = onFirstBuffer
        self.onRecord = onRecord
        self.completion = completion
    }

    func captureOutput(
        _ output: AVCaptureOutput,
        didOutput sampleBuffer: CMSampleBuffer,
        from connection: AVCaptureConnection
    ) {
        guard !finished, records.count < requiredFrameCount else { return }
        if records.isEmpty { onFirstBuffer() }
        let pts = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
        guard pts.isValid, pts.isNumeric else {
            finish(.failure(ProbeError.invalidPTS))
            return
        }
        if let lastPTS, CMTimeCompare(pts, lastPTS) <= 0 {
            finish(.failure(ProbeError.nonIncreasingPTS))
            return
        }
        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else {
            finish(.failure(ProbeError.frameEncodingFailed))
            return
        }
        let image = CIImage(cvPixelBuffer: pixelBuffer)
        guard let png = context.pngRepresentation(
            of: image,
            format: .BGRA8,
            colorSpace: CGColorSpaceCreateDeviceRGB(),
            options: [:]
        ) else {
            finish(.failure(ProbeError.frameEncodingFailed))
            return
        }
        let index = records.count + 1
        let filename = String(format: "frame-%04d.png", index)
        do {
            try privateWrite(png, to: outputDirectory.appendingPathComponent(filename))
        } catch {
            finish(.failure(error))
            return
        }
        let hash = SHA256.hash(data: png).map { String(format: "%02x", $0) }.joined()
        let record = FrameRecord(
            index: index,
            filename: filename,
            width: CVPixelBufferGetWidth(pixelBuffer),
            height: CVPixelBufferGetHeight(pixelBuffer),
            ptsValue: pts.value,
            ptsTimescale: pts.timescale,
            ptsSeconds: CMTimeGetSeconds(pts),
            sha256: hash
        )
        records.append(record)
        onRecord(record)
        lastPTS = pts
        if records.count == requiredFrameCount {
            let distinctHashes = Set(records.map(\.sha256)).count
            if distinctHashes < 2 {
                finish(.failure(ProbeError.staticFrames))
            } else {
                finish(.success(records))
            }
        }
    }

    private func finish(_ result: Result<[FrameRecord], Error>) {
        guard !finished else { return }
        finished = true
        completion(result)
    }
}

private final class ExactCaptureProbe: @unchecked Sendable {
    private let uniqueID: String
    private let expectedName: String?
    private let outputDirectory: URL
    private let discoverySession = makeVideoDiscoverySession()
    private let resultLock = NSLock()
    private let sessionQueue = DispatchQueue(label: "com.home-media.airplay-capture-probe.session")
    private var deviceObserver: NSObjectProtocol?
    private var runtimeErrorObserver: NSObjectProtocol?
    private var retainedDevice: AVCaptureDevice?
    private var session: AVCaptureSession?
    private var videoOutput: AVCaptureVideoDataOutput?
    private var sink: FrameSink?
    private var storedResult: Result<[FrameRecord], Error>?
    private var deviceName: String?
    private var stage = DiagnosticStage.outputDirectoryCreated
    private var permissions = PermissionSnapshot(
        screenCapturePreflight: false,
        videoAuthorization: "unknown"
    )
    private var partialFrames: [FrameRecord] = []
    private var sessionRunningObserved = false
    private var videoPathDiagnostics = VideoPathDiagnostics()

    init(uniqueID: String, expectedName: String?, outputDirectory: URL) {
        self.uniqueID = uniqueID
        self.expectedName = expectedName
        self.outputDirectory = outputDirectory
    }

    private func consider(_ device: AVCaptureDevice) {
        guard device.uniqueID == uniqueID, retainedDevice == nil else { return }
        updateStage(.deviceMatched)
        if let expectedName, expectedName != device.localizedName {
            complete(.failure(ProbeError.expectedNameMismatch))
            return
        }
        retainedDevice = device
        deviceName = device.localizedName
        sessionQueue.async { [weak self] in
            guard let self, resultSnapshot == nil else { return }
            do {
                try startSession(device: device)
            } catch {
                complete(.failure((error as? ProbeError) ?? ProbeError.captureSetupFailed))
            }
        }
    }

    private func seedExistingDevices() {
        for device in currentVideoDevices(from: discoverySession) {
            consider(device)
        }
    }

    func run() throws -> CaptureManifest {
        try createPrivateOutputDirectory(outputDirectory)
        permissions = permissionSnapshot()
        updateStage(.waitingForDevice)
        let deadline = Date(timeIntervalSinceNow: captureTimeoutSeconds)
        do {
            let records = try runCapture(until: deadline)
            updateStage(.completed)
            let manifest = makeManifest(status: "passed", error: nil, frames: records)
            try writeManifest(manifest)
            return manifest
        } catch {
            let safeError = (error as? ProbeError) ?? ProbeError.captureSetupFailed
            let manifest = makeManifest(
                status: "failed",
                error: safeError,
                frames: diagnosticSnapshot.frames
            )
            try? writeManifest(manifest)
            throw safeError
        }
    }

    private func runCapture(until deadline: Date) throws -> [FrameRecord] {
        deviceObserver = NotificationCenter.default.addObserver(
            forName: AVCaptureDevice.wasConnectedNotification,
            object: nil,
            queue: .main
        ) { [weak self] notification in
            guard let self, let device = notification.object as? AVCaptureDevice else { return }
            consider(device)
        }
        defer { scheduleCleanup() }
        seedExistingDevices()
        try enableCaptureDevices()
        seedExistingDevices()

        while resultSnapshot == nil, Date() < deadline {
            RunLoop.current.run(until: min(deadline, Date(timeIntervalSinceNow: 0.1)))
            seedExistingDevices()
        }
        if resultSnapshot == nil { complete(.failure(ProbeError.timeout)) }
        return try (resultSnapshot ?? .failure(ProbeError.timeout)).get()
    }

    private func startSession(device: AVCaptureDevice) throws {
        let captureSession = AVCaptureSession()
        let input: AVCaptureDeviceInput
        do {
            input = try AVCaptureDeviceInput(device: device)
        } catch {
            throw ProbeError.captureSetupFailed
        }
        updateStage(.inputCreated)
        let output = AVCaptureVideoDataOutput()
        guard captureSession.canAddInput(input), captureSession.canAddOutput(output) else {
            throw ProbeError.captureSetupFailed
        }
        let frameSink = FrameSink(
            outputDirectory: outputDirectory,
            onFirstBuffer: { [weak self] in self?.updateStage(.firstBuffer) },
            onRecord: { [weak self] record in self?.record(record) },
            completion: { [weak self] result in self?.complete(result) }
        )
        output.alwaysDiscardsLateVideoFrames = true
        output.videoSettings = [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
        ]
        output.setSampleBufferDelegate(
            frameSink,
            queue: DispatchQueue(label: "com.home-media.airplay-capture-probe.frames")
        )
        captureSession.beginConfiguration()
        if captureSession.canSetSessionPreset(.hd1280x720) {
            captureSession.sessionPreset = .hd1280x720
        }
        captureSession.addInput(input)
        captureSession.addOutput(output)
        captureSession.commitConfiguration()
        updateStage(.sessionConfigured)
        runtimeErrorObserver = NotificationCenter.default.addObserver(
            forName: AVCaptureSession.runtimeErrorNotification,
            object: captureSession,
            queue: nil
        ) { [weak self] _ in
            self?.complete(.failure(ProbeError.captureRuntimeFailed))
        }
        session = captureSession
        videoOutput = output
        sink = frameSink
        updateStage(.startRunningCalled)
        captureSession.startRunning()
        updateStage(.startRunningReturned)
        if captureSession.isRunning {
            markSessionRunning()
            updateStage(.sessionRunning)
            sampleVideoPathDiagnostics(
                device: device,
                session: captureSession,
                output: output
            )
        } else {
            complete(.failure(ProbeError.sessionDidNotRun))
        }
    }

    private func sampleVideoPathDiagnostics(
        device: AVCaptureDevice,
        session: AVCaptureSession,
        output: AVCaptureVideoDataOutput
    ) {
        guard resultSnapshot == nil else { return }
        let connection = output.connection(with: .video)
        let formatDescription = device.activeFormat.formatDescription
        let dimensions = CMVideoFormatDescriptionGetDimensions(formatDescription)
        let mediaSubtype = CMFormatDescriptionGetMediaSubType(formatDescription)
        let pixelTypes = output.availableVideoPixelFormatTypes
            .map(formatOSType)
            .sorted()

        resultLock.lock()
        guard storedResult == nil else {
            resultLock.unlock()
            return
        }
        videoPathDiagnostics.sampleCount += 1
        videoPathDiagnostics.connectionExists = connection != nil
        videoPathDiagnostics.connectionEnabled = connection?.isEnabled
        videoPathDiagnostics.connectionActive = connection?.isActive
        videoPathDiagnostics.connectionEverActive =
            videoPathDiagnostics.connectionEverActive || connection?.isActive == true
        videoPathDiagnostics.deviceConnected = device.isConnected
        videoPathDiagnostics.deviceSuspended = device.isSuspended
        videoPathDiagnostics.activeFormatWidth = Int(dimensions.width)
        videoPathDiagnostics.activeFormatHeight = Int(dimensions.height)
        videoPathDiagnostics.activeFormatMediaSubtype = formatOSType(mediaSubtype)
        videoPathDiagnostics.outputAvailablePixelTypes = pixelTypes
        videoPathDiagnostics.sessionOutputCount = session.outputs.count
        let shouldContinue =
            videoPathDiagnostics.sampleCount < maximumVideoPathDiagnosticSamples
        resultLock.unlock()

        guard shouldContinue else { return }
        sessionQueue.asyncAfter(
            deadline: .now() + videoPathDiagnosticInterval
        ) { [weak self] in
            self?.sampleVideoPathDiagnostics(
                device: device,
                session: session,
                output: output
            )
        }
    }

    private func scheduleCleanup() {
        if let deviceObserver { NotificationCenter.default.removeObserver(deviceObserver) }
        // Never synchronize with this queue: startRunning may itself be the
        // blocked stage. Queue cleanup and let process exit remain the hard stop.
        sessionQueue.async { [weak self] in
            guard let self else { return }
            if let session, session.isRunning { session.stopRunning() }
            if let runtimeErrorObserver {
                NotificationCenter.default.removeObserver(runtimeErrorObserver)
            }
        }
    }

    private func complete(_ newResult: Result<[FrameRecord], Error>) {
        resultLock.lock()
        defer { resultLock.unlock() }
        guard storedResult == nil else { return }
        storedResult = newResult
    }

    private var resultSnapshot: Result<[FrameRecord], Error>? {
        resultLock.lock()
        defer { resultLock.unlock() }
        return storedResult
    }

    private func updateStage(_ newStage: DiagnosticStage) {
        resultLock.lock()
        defer { resultLock.unlock() }
        if newStage.rawValue > stage.rawValue { stage = newStage }
    }

    private func record(_ record: FrameRecord) {
        resultLock.lock()
        defer { resultLock.unlock() }
        partialFrames.append(record)
        if partialFrames.count >= requiredFrameCount,
           DiagnosticStage.tenFramesCollected.rawValue > stage.rawValue {
            stage = .tenFramesCollected
        }
    }

    private func markSessionRunning() {
        resultLock.lock()
        defer { resultLock.unlock() }
        sessionRunningObserved = true
    }

    private var diagnosticSnapshot: (
        stage: DiagnosticStage,
        frames: [FrameRecord],
        sessionRunningObserved: Bool,
        videoPath: VideoPathDiagnostics
    ) {
        resultLock.lock()
        defer { resultLock.unlock() }
        return (stage, partialFrames, sessionRunningObserved, videoPathDiagnostics)
    }

    private func makeManifest(
        status: String,
        error: ProbeError?,
        frames: [FrameRecord]
    ) -> CaptureManifest {
        let snapshot = diagnosticSnapshot
        return CaptureManifest(
            status: status,
            stage: snapshot.stage.label,
            errorCode: error?.code,
            errorMessage: error?.localizedDescription,
            deviceName: deviceName ?? "",
            deviceUniqueID: uniqueID,
            screenCapturePreflight: permissions.screenCapturePreflight,
            videoAuthorization: permissions.videoAuthorization,
            sessionRunningObserved: snapshot.sessionRunningObserved,
            videoPathDiagnosticSamples: snapshot.videoPath.sampleCount,
            videoConnectionExists: snapshot.videoPath.connectionExists,
            videoConnectionEnabled: snapshot.videoPath.connectionEnabled,
            videoConnectionActive: snapshot.videoPath.connectionActive,
            videoConnectionEverActive: snapshot.videoPath.connectionEverActive,
            deviceConnected: snapshot.videoPath.deviceConnected,
            deviceSuspended: snapshot.videoPath.deviceSuspended,
            activeFormatWidth: snapshot.videoPath.activeFormatWidth,
            activeFormatHeight: snapshot.videoPath.activeFormatHeight,
            activeFormatMediaSubtype: snapshot.videoPath.activeFormatMediaSubtype,
            outputAvailablePixelTypes: snapshot.videoPath.outputAvailablePixelTypes,
            captureSessionOutputCount: snapshot.videoPath.sessionOutputCount,
            frameCount: frames.count,
            distinctHashes: Set(frames.map(\.sha256)).count,
            timeoutSeconds: Int(captureTimeoutSeconds),
            frames: frames
        )
    }

    private func writeManifest(_ manifest: CaptureManifest) throws {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        try privateWrite(
            encoder.encode(manifest),
            to: outputDirectory.appendingPathComponent("manifest.json")
        )
    }
}

private func argument(after name: String) -> String? {
    guard let index = CommandLine.arguments.firstIndex(of: name) else { return nil }
    let next = CommandLine.arguments.index(after: index)
    return next < CommandLine.arguments.endIndex ? CommandLine.arguments[next] : nil
}

private func usage() -> Never {
    fputs(
        """
        usage:
          airplay-capture-probe --enumerate [--seconds 8]
          airplay-capture-probe --capture --unique-id ID [--expected-name NAME] --output-dir NEW_DIR

        """,
        stderr
    )
    exit(64)
}

_ = umask(0o077)

do {
    if CommandLine.arguments.contains("--enumerate") {
        guard !CommandLine.arguments.contains("--capture") else { usage() }
        let seconds = min(max(Double(argument(after: "--seconds") ?? "8") ?? 8, 1), 30)
        let probe = EnumerationProbe()
        let devices = try probe.run(seconds: seconds)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        FileHandle.standardOutput.write(try encoder.encode(devices))
        FileHandle.standardOutput.write(Data("\n".utf8))
    } else if CommandLine.arguments.contains("--capture") {
        guard let uniqueID = argument(after: "--unique-id"), !uniqueID.isEmpty,
              let outputPath = argument(after: "--output-dir"), !outputPath.isEmpty else {
            usage()
        }
        let probe = ExactCaptureProbe(
            uniqueID: uniqueID,
            expectedName: argument(after: "--expected-name"),
            outputDirectory: URL(fileURLWithPath: outputPath, isDirectory: true)
        )
        let manifest = try probe.run()
        print(
            "capture passed: \(manifest.frameCount) frames, "
                + "\(manifest.distinctHashes) distinct hashes"
        )
    } else {
        usage()
    }
} catch {
    fputs("airplay-capture-probe: \(error.localizedDescription)\n", stderr)
    exit(1)
}
