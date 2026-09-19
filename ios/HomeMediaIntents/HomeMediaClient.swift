import Foundation
import Security

/// Thin client for the semantic Mac mini endpoint. It never retries a mutation; an uncertain
/// timeout is returned to Siri and the broker's idempotency ledger prevents duplicate execution.
actor HomeMediaClient {
    static let shared = HomeMediaClient()

    enum ClientError: Error {
        case notConfigured
        case invalidResponse
        case hubMessage(String)
        case timedOut

        var spokenResponse: String {
            switch self {
            case .notConfigured:
                "Open Home Media once to connect it to the Mac mini."
            case .invalidResponse:
                "The Home Media hub returned an invalid response."
            case .hubMessage(let message):
                message
            case .timedOut:
                "Home Media is still working. It will not repeat the command automatically."
            }
        }
    }

    struct Response: Decodable {
        let ok: Bool
        let status: String
        let spokenResponse: String

        enum CodingKeys: String, CodingKey {
            case ok
            case status
            case spokenResponse = "spoken_response"
        }
    }

    private struct Payload: Encodable {
        let schemaVersion = 1
        let utterance: String
        let idempotencyKey: String

        enum CodingKeys: String, CodingKey {
            case schemaVersion = "schema_version"
            case utterance
            case idempotencyKey = "idempotency_key"
        }
    }

    private let session: URLSession

    init() {
        let configuration = URLSessionConfiguration.ephemeral
        // The resident broker intentionally owns the mutation timeout. Stay just outside its
        // 75-second operation / 80-second HTTP window so a slow verified result is not replaced
        // by a premature client timeout. URLSession still performs no automatic mutation retry.
        configuration.timeoutIntervalForRequest = 90
        configuration.timeoutIntervalForResource = 95
        configuration.waitsForConnectivity = false
        session = URLSession(configuration: configuration)
    }

    func execute(utterance: String) async throws -> Response {
        let normalized = utterance.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !normalized.isEmpty, normalized.count <= 512 else {
            throw ClientError.invalidResponse
        }
        guard
            let baseURLString = UserDefaults.standard.string(forKey: "homeMediaBrokerURL"),
            let baseURL = URL(string: baseURLString),
            let token = BrokerTokenStore.read(),
            token.utf8.count >= 32
        else {
            throw ClientError.notConfigured
        }

        let endpoint = baseURL.appending(path: "v1/intent")
        var request = URLRequest(url: endpoint)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.httpBody = try JSONEncoder().encode(
            Payload(
                utterance: normalized,
                idempotencyKey: "ios-\(UUID().uuidString.lowercased())"
            )
        )

        do {
            let (data, urlResponse) = try await session.data(for: request)
            guard let http = urlResponse as? HTTPURLResponse, (200...599).contains(http.statusCode)
            else {
                throw ClientError.invalidResponse
            }
            let response = try JSONDecoder().decode(Response.self, from: data)
            if response.spokenResponse.isEmpty {
                throw ClientError.invalidResponse
            }
            return response
        } catch let error as URLError where error.code == .timedOut {
            throw ClientError.timedOut
        } catch let error as ClientError {
            throw error
        } catch {
            throw ClientError.invalidResponse
        }
    }
}

enum BrokerTokenStore {
    private static let service = "home-media-control.ios-broker"
    private static let account = "default"

    static func read() -> String? {
        let query: [CFString: Any] = [
            kSecClass: kSecClassGenericPassword,
            kSecAttrService: service,
            kSecAttrAccount: account,
            kSecReturnData: true,
            kSecMatchLimit: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        guard
            SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
            let data = item as? Data
        else {
            return nil
        }
        return String(data: data, encoding: .utf8)
    }

    /// Call from the app's one-time setup screen. Tokens never enter UserDefaults or logs.
    static func replace(with token: String) throws {
        let key: [CFString: Any] = [
            kSecClass: kSecClassGenericPassword,
            kSecAttrService: service,
            kSecAttrAccount: account,
        ]
        SecItemDelete(key as CFDictionary)
        let insert = key.merging([
            kSecValueData: Data(token.utf8),
            kSecAttrAccessible: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]) { _, new in new }
        let status = SecItemAdd(insert as CFDictionary, nil)
        guard status == errSecSuccess else {
            throw HomeMediaSetupError.keychain(status)
        }
    }
}

enum HomeMediaSetupError: Error {
    case keychain(OSStatus)
}
