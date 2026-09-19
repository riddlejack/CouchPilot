import CryptoKit
import Foundation

public final class FrameStore: @unchecked Sendable {
    private let root: URL
    private let encoder: JSONEncoder
    private let fileManager: FileManager
    private let lock = NSLock()
    private var sequences: [String: UInt64] = [:]

    public init(root: URL, fileManager: FileManager = .default) throws {
        self.root = root.standardizedFileURL
        self.fileManager = fileManager
        self.encoder = JSONEncoder()
        self.encoder.outputFormatting = [.sortedKeys]
        try fileManager.createDirectory(
            at: self.root,
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        let rootAttributes = try fileManager.attributesOfItem(atPath: self.root.path)
        guard rootAttributes[.type] as? FileAttributeType == .typeDirectory else {
            throw FrameStoreError.invalidConfig("frame store must be a real directory")
        }
        try fileManager.setAttributes(
            [.posixPermissions: 0o700],
            ofItemAtPath: self.root.path
        )
    }

    @discardableResult
    public func publishFrame(
        png: Data,
        width: Int,
        height: Int,
        binding: RoomCaptureBinding,
        capturedAt: Date = Date()
    ) throws -> PublishedFrameState {
        lock.lock()
        defer { lock.unlock() }
        let roomDirectory = try ensureRoomDirectory(binding.roomKey)
        let priorSequence = currentSequence(
            roomKey: binding.roomKey,
            roomDirectory: roomDirectory
        )
        guard priorSequence < UInt64.max else {
            throw FrameStoreError.writeFailed("frame sequence exhausted")
        }
        let sequence = priorSequence + 1
        sequences[binding.roomKey] = sequence
        let digest = SHA256.hash(data: png).map { String(format: "%02x", $0) }.joined()
        let filename = "frame-\(sequence).png"
        let state = PublishedFrameState(
            protocolVersion: frameStoreProtocolVersion,
            roomKey: binding.roomKey,
            stableDeviceID: binding.stableDeviceID,
            captureUniqueIDFingerprint: identityFingerprint(binding.captureUniqueID),
            sequence: sequence,
            capturedAt: Self.timestamp(capturedAt),
            frameFilename: filename,
            sha256: digest,
            width: width,
            height: height,
            status: "ready",
            errorCode: nil
        )
        try writePrivate(png, to: roomDirectory.appendingPathComponent(filename))
        try writeState(state, roomDirectory: roomDirectory)
        pruneOldFrames(in: roomDirectory, keeping: filename)
        return state
    }

    @discardableResult
    public func publishStatus(
        _ status: String,
        errorCode: String? = nil,
        binding: RoomCaptureBinding
    ) throws -> PublishedFrameState {
        lock.lock()
        defer { lock.unlock() }
        let roomDirectory = try ensureRoomDirectory(binding.roomKey)
        let currentSequence = currentSequence(
            roomKey: binding.roomKey,
            roomDirectory: roomDirectory
        )
        let state = PublishedFrameState(
            protocolVersion: frameStoreProtocolVersion,
            roomKey: binding.roomKey,
            stableDeviceID: binding.stableDeviceID,
            captureUniqueIDFingerprint: identityFingerprint(binding.captureUniqueID),
            sequence: currentSequence,
            capturedAt: nil,
            frameFilename: nil,
            sha256: nil,
            width: nil,
            height: nil,
            status: status,
            errorCode: errorCode
        )
        try writeState(state, roomDirectory: roomDirectory)
        return state
    }

    private func ensureRoomDirectory(_ roomKey: String) throws -> URL {
        let directory = root.appendingPathComponent(roomKey, isDirectory: true)
        try fileManager.createDirectory(
            at: directory,
            withIntermediateDirectories: false,
            attributes: [.posixPermissions: 0o700]
        )
        let attributes = try fileManager.attributesOfItem(atPath: directory.path)
        guard attributes[.type] as? FileAttributeType == .typeDirectory else {
            throw FrameStoreError.invalidConfig("room frame store must be a real directory")
        }
        try fileManager.setAttributes(
            [.posixPermissions: 0o700],
            ofItemAtPath: directory.path
        )
        return directory
    }

    private func currentSequence(roomKey: String, roomDirectory: URL) -> UInt64 {
        if let sequence = sequences[roomKey] {
            return sequence
        }
        let stateURL = roomDirectory.appendingPathComponent("state.json")
        guard let data = try? Data(contentsOf: stateURL),
              let state = try? JSONDecoder().decode(PublishedFrameState.self, from: data),
              state.protocolVersion == frameStoreProtocolVersion,
              state.roomKey == roomKey else {
            sequences[roomKey] = 0
            return 0
        }
        sequences[roomKey] = state.sequence
        return state.sequence
    }

    private func writeState(_ state: PublishedFrameState, roomDirectory: URL) throws {
        try writePrivate(encoder.encode(state), to: roomDirectory.appendingPathComponent("state.json"))
    }

    private func writePrivate(_ data: Data, to url: URL) throws {
        do {
            try data.write(to: url, options: .atomic)
            try fileManager.setAttributes(
                [.posixPermissions: 0o600],
                ofItemAtPath: url.path
            )
        } catch {
            throw FrameStoreError.writeFailed(String(describing: error))
        }
    }

    private func pruneOldFrames(in directory: URL, keeping filename: String) {
        guard let files = try? fileManager.contentsOfDirectory(
            at: directory,
            includingPropertiesForKeys: nil
        ) else { return }
        for file in files where file.lastPathComponent.hasPrefix("frame-")
            && file.pathExtension == "png" && file.lastPathComponent != filename {
            try? fileManager.removeItem(at: file)
        }
    }

    private static func timestamp(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.string(from: date)
    }
}
