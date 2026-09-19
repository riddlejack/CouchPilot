import CryptoKit
import Foundation

public let frameStoreProtocolVersion = 1

public struct RoomCaptureBinding: Codable, Equatable, Sendable {
    public let roomKey: String
    public let stableDeviceID: String
    public let captureUniqueID: String
    public let expectedLocalizedName: String?

    enum CodingKeys: String, CodingKey {
        case roomKey = "room_key"
        case stableDeviceID = "stable_device_id"
        case captureUniqueID = "capture_unique_id"
        case expectedLocalizedName = "expected_localized_name"
    }

    public init(
        roomKey: String,
        stableDeviceID: String,
        captureUniqueID: String,
        expectedLocalizedName: String? = nil
    ) {
        self.roomKey = roomKey
        self.stableDeviceID = stableDeviceID
        self.captureUniqueID = captureUniqueID
        self.expectedLocalizedName = expectedLocalizedName
    }
}

public struct CaptureDaemonConfig: Codable, Equatable, Sendable {
    public let schemaVersion: Int
    public let minimumFrameIntervalMS: Int?
    public let rooms: [RoomCaptureBinding]

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case minimumFrameIntervalMS = "minimum_frame_interval_ms"
        case rooms
    }

    public init(
        schemaVersion: Int = 1,
        minimumFrameIntervalMS: Int? = nil,
        rooms: [RoomCaptureBinding]
    ) {
        self.schemaVersion = schemaVersion
        self.minimumFrameIntervalMS = minimumFrameIntervalMS
        self.rooms = rooms
    }

    public var frameInterval: TimeInterval {
        TimeInterval(min(max(minimumFrameIntervalMS ?? 250, 100), 5_000)) / 1_000
    }

    public func validate() throws {
        guard schemaVersion == 1 else {
            throw FrameStoreError.invalidConfig("unsupported schema_version")
        }
        guard !rooms.isEmpty else {
            throw FrameStoreError.invalidConfig("at least one exact room binding is required")
        }
        let roomPattern = try NSRegularExpression(pattern: "^[a-z0-9][a-z0-9_-]{0,63}$")
        var roomKeys = Set<String>()
        var stableIDs = Set<String>()
        var captureIDs = Set<String>()
        for room in rooms {
            let roomRange = NSRange(room.roomKey.startIndex..., in: room.roomKey)
            guard roomPattern.firstMatch(in: room.roomKey, range: roomRange) != nil else {
                throw FrameStoreError.invalidConfig("room_key is invalid")
            }
            guard !room.stableDeviceID.trimmingCharacters(in: .whitespaces).isEmpty,
                  !room.captureUniqueID.trimmingCharacters(in: .whitespaces).isEmpty else {
                throw FrameStoreError.invalidConfig("device identities must be non-empty")
            }
            guard roomKeys.insert(room.roomKey).inserted,
                  stableIDs.insert(room.stableDeviceID).inserted,
                  captureIDs.insert(room.captureUniqueID).inserted else {
                throw FrameStoreError.invalidConfig(
                    "room, stable-device, and capture identities must each be unique"
                )
            }
        }
    }

    public static func load(from url: URL) throws -> CaptureDaemonConfig {
        let attributes = try FileManager.default.attributesOfItem(atPath: url.path)
        guard attributes[.type] as? FileAttributeType == .typeRegular else {
            throw FrameStoreError.invalidConfig("config must be a regular file, not a symlink")
        }
        if let permissions = attributes[.posixPermissions] as? NSNumber,
           permissions.intValue & 0o077 != 0 {
            throw FrameStoreError.invalidConfig("config must not be group/world accessible")
        }
        let config = try JSONDecoder().decode(Self.self, from: Data(contentsOf: url))
        try config.validate()
        return config
    }
}

public struct PublishedFrameState: Codable, Equatable, Sendable {
    public let protocolVersion: Int
    public let roomKey: String
    public let stableDeviceID: String
    public let captureUniqueIDFingerprint: String
    public let sequence: UInt64
    public let capturedAt: String?
    public let frameFilename: String?
    public let sha256: String?
    public let width: Int?
    public let height: Int?
    public let status: String
    public let errorCode: String?

    enum CodingKeys: String, CodingKey {
        case protocolVersion = "protocol_version"
        case roomKey = "room_key"
        case stableDeviceID = "stable_device_id"
        case captureUniqueIDFingerprint = "capture_unique_id_fingerprint"
        case sequence
        case capturedAt = "captured_at"
        case frameFilename = "frame_filename"
        case sha256
        case width
        case height
        case status
        case errorCode = "error_code"
    }
}

public enum FrameStoreError: Error, LocalizedError {
    case invalidConfig(String)
    case writeFailed(String)

    public var errorDescription: String? {
        switch self {
        case .invalidConfig(let detail): "invalid config: \(detail)"
        case .writeFailed(let detail): "frame publish failed: \(detail)"
        }
    }
}

public func identityFingerprint(_ identity: String) -> String {
    SHA256.hash(data: Data(identity.utf8)).prefix(8).map { String(format: "%02x", $0) }.joined()
}
