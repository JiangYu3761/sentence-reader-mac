import Foundation


final class ClickVoiceAPIClient {
    private let baseURL: URL
    private let session: URLSession
    private let decoder: JSONDecoder

    init(
        baseURL: URL = URL(string: ProcessInfo.processInfo.environment["SENTENCE_READER_API_BASE_URL"] ?? "http://127.0.0.1:18180")!,
        session: URLSession = .shared
    ) {
        self.baseURL = baseURL
        self.session = session
        self.decoder = JSONDecoder()
        self.decoder.keyDecodingStrategy = .convertFromSnakeCase
    }

    func createVoiceRecord(
        from capture: ClickVoiceCaptureManifest,
        completion: @escaping (Result<[String: Any], Error>) -> Void
    ) {
        let body: [String: Any] = [
            "audio_path": capture.audioPath,
            "mime_type": capture.mimeType,
            "duration_seconds": capture.durationSeconds as Any,
            "source": "mac",
            "source_platform": capture.sourcePlatform,
            "origin_ref": capture.originRef,
            "metadata": [
                "capture_schema": capture.schema,
                "capture_id": capture.captureID,
                "native_audio_hash": capture.audioHash as Any,
                "created_by": "tingle_native",
                "start_command_source": capture.startCommandSource as Any,
                "stop_command_source": capture.stopCommandSource as Any,
            ],
            "auto_transcribe": true,
            "auto_understand": false,
        ]
        request(method: "POST", path: "/voice-records", body: body) { result in
            completion(result.flatMap { payload in
                guard let record = payload["record"] as? [String: Any] else {
                    return .failure(ClickVoiceAPIError.invalidResponse)
                }
                return .success(record)
            })
        }
    }

    func health(completion: @escaping (Bool) -> Void) {
        request(method: "GET", path: "/health", body: nil) { result in
            guard case let .success(payload) = result,
                  payload["ok"] as? Bool == true,
                  let runtime = payload["runtime"] as? [String: Any],
                  runtime["contract"] as? String == "click.reader_runtime.v1",
                  let revision = runtime["api_revision"] as? Int,
                  revision >= 3,
                  let capabilities = runtime["capabilities"] as? [String],
                  capabilities.contains("voice.inbox.v2"),
                  capabilities.contains("tingle.permanent_delete.v1"),
                  capabilities.contains("tingle.tombstone_replay_guard.v1")
            else {
                completion(false)
                return
            }
            completion(true)
        }
    }

    func getTingleInspiration(
        voiceRecordID: String,
        completion: @escaping (Result<[String: Any], Error>) -> Void
    ) {
        request(
            method: "GET",
            path: "/tingle/inspirations/by-voice-record/\(voiceRecordID)",
            body: nil
        ) { result in
            completion(result.flatMap { payload in
                guard let inspiration = payload["inspiration"] as? [String: Any] else {
                    return .failure(ClickVoiceAPIError.invalidResponse)
                }
                return .success(inspiration)
            })
        }
    }

    func listTingleInspirations(
        archived: Bool,
        query: String = "",
        completion: @escaping (Result<[TingleInspiration], Error>) -> Void
    ) {
        var components = URLComponents(
            url: baseURL.appendingPathComponent("tingle/inspirations"),
            resolvingAgainstBaseURL: false
        )
        components?.queryItems = [
            URLQueryItem(name: "archived", value: archived ? "true" : "false"),
            URLQueryItem(name: "query", value: query),
            URLQueryItem(name: "limit", value: "500"),
        ]
        guard let endpoint = components?.url else {
            completion(.failure(ClickVoiceAPIError.invalidRequest))
            return
        }
        request(method: "GET", endpoint: endpoint, body: nil) { [weak self] result in
            completion(self?.decodeEnvelope(result, key: "inspirations") ?? .failure(ClickVoiceAPIError.invalidResponse))
        }
    }

    func getTingleInspiration(
        inspirationID: String,
        completion: @escaping (Result<TingleInspiration, Error>) -> Void
    ) {
        request(
            method: "GET",
            path: "/tingle/inspirations/\(inspirationID)",
            body: nil
        ) { [weak self] result in
            completion(self?.decodeEnvelope(result, key: "inspiration") ?? .failure(ClickVoiceAPIError.invalidResponse))
        }
    }

    func updateTingleInspiration(
        inspirationID: String,
        title: String,
        content: String?,
        completion: @escaping (Result<TingleInspiration, Error>) -> Void
    ) {
        var body: [String: Any] = ["title": title]
        if let content {
            body["content"] = content
        }
        request(
            method: "PATCH",
            path: "/tingle/inspirations/\(inspirationID)",
            body: body
        ) { [weak self] result in
            completion(self?.decodeEnvelope(result, key: "inspiration") ?? .failure(ClickVoiceAPIError.invalidResponse))
        }
    }

    func archiveTingleInspiration(
        inspirationID: String,
        completion: @escaping (Result<TingleInspiration, Error>) -> Void
    ) {
        performInspirationAction(inspirationID: inspirationID, action: "archive", completion: completion)
    }

    func restoreTingleInspiration(
        inspirationID: String,
        completion: @escaping (Result<TingleInspiration, Error>) -> Void
    ) {
        performInspirationAction(inspirationID: inspirationID, action: "restore", completion: completion)
    }

    func retryTingleInspiration(
        inspirationID: String,
        completion: @escaping (Result<TingleInspiration, Error>) -> Void
    ) {
        performInspirationAction(inspirationID: inspirationID, action: "retry", completion: completion)
    }

    func permanentlyDeleteTingleInspiration(
        inspirationID: String,
        expectedContentHash: String,
        acknowledgeLinkedOutputs: Bool,
        completion: @escaping (Result<[String: Any], Error>) -> Void
    ) {
        request(
            method: "POST",
            path: "/tingle/inspirations/\(inspirationID)/delete-permanently",
            body: [
                "confirmation_intent": "delete_permanently",
                "expected_content_hash": expectedContentHash,
                "acknowledge_linked_external_targets": acknowledgeLinkedOutputs,
            ]
        ) { result in
            completion(result.flatMap { payload in
                guard payload["receipt"] as? [String: Any] != nil else {
                    return .failure(ClickVoiceAPIError.invalidResponse)
                }
                return .success(payload)
            })
        }
    }

    func originalAudioURL(for inspiration: TingleInspiration) -> URL? {
        guard let path = inspiration.audioURL, !path.isEmpty else { return nil }
        return URL(string: path, relativeTo: baseURL)?.absoluteURL
    }

    private func performInspirationAction(
        inspirationID: String,
        action: String,
        completion: @escaping (Result<TingleInspiration, Error>) -> Void
    ) {
        request(
            method: "POST",
            path: "/tingle/inspirations/\(inspirationID)/\(action)",
            body: nil
        ) { [weak self] result in
            completion(self?.decodeEnvelope(result, key: "inspiration") ?? .failure(ClickVoiceAPIError.invalidResponse))
        }
    }

    private func decodeEnvelope<T: Decodable>(
        _ result: Result<[String: Any], Error>,
        key: String
    ) -> Result<T, Error> {
        result.flatMap { payload in
            guard let value = payload[key],
                  JSONSerialization.isValidJSONObject(value),
                  let data = try? JSONSerialization.data(withJSONObject: value)
            else {
                return .failure(ClickVoiceAPIError.invalidResponse)
            }
            do {
                return .success(try decoder.decode(T.self, from: data))
            } catch {
                return .failure(ClickVoiceAPIError.decoding(error.localizedDescription))
            }
        }
    }

    private func request(
        method: String,
        path: String,
        body: [String: Any]?,
        completion: @escaping (Result<[String: Any], Error>) -> Void
    ) {
        guard let endpoint = URL(string: path, relativeTo: baseURL)?.absoluteURL else {
            completion(.failure(ClickVoiceAPIError.invalidRequest))
            return
        }
        request(method: method, endpoint: endpoint, body: body, completion: completion)
    }

    private func request(
        method: String,
        endpoint: URL,
        body: [String: Any]?,
        completion: @escaping (Result<[String: Any], Error>) -> Void
    ) {
        var request = URLRequest(url: endpoint)
        request.httpMethod = method
        request.timeoutInterval = 30
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let body {
            guard JSONSerialization.isValidJSONObject(body) else {
                completion(.failure(ClickVoiceAPIError.invalidRequest))
                return
            }
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        }
        session.dataTask(with: request) { data, response, error in
            if let error {
                completion(.failure(error))
                return
            }
            guard let http = response as? HTTPURLResponse,
                  let data,
                  let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            else {
                completion(.failure(ClickVoiceAPIError.invalidResponse))
                return
            }
            guard (200..<300).contains(http.statusCode) else {
                let message = String(describing: payload["detail"] ?? payload["error"] ?? "HTTP \(http.statusCode)")
                completion(.failure(ClickVoiceAPIError.server(message)))
                return
            }
            completion(.success(payload))
        }.resume()
    }
}


enum ClickVoiceAPIError: LocalizedError {
    case invalidRequest
    case invalidResponse
    case decoding(String)
    case server(String)

    var errorDescription: String? {
        switch self {
        case .invalidRequest: return "录音请求格式无效"
        case .invalidResponse: return "Click Runtime 返回了无法识别的数据"
        case let .decoding(message): return "Click Runtime 数据格式不兼容：\(message)"
        case let .server(message): return message
        }
    }
}


struct TingleTranscriptVersion: Decodable, Equatable {
    let id: String
    let versionType: String
    let content: String
    let isActive: Bool
    let createdAt: String
}


struct TingleInspiration: Decodable, Equatable {
    let id: String
    let kind: String
    let title: String
    let content: String
    let preview: String
    let source: String
    let voiceRecordID: String?
    let hasAudio: Bool
    let audioURL: String?
    let durationMS: Int?
    let state: String
    let failureMessage: String?
    let createdAt: String
    let updatedAt: String
    let archivedAt: String?
    let purgeAfter: String?
    let contentHash: String
    let linkedExternalTargetCount: Int
    let permanentDeleteAvailable: Bool
    let transcriptVersions: [TingleTranscriptVersion]?

    private enum CodingKeys: String, CodingKey {
        case id
        case kind
        case title
        case content
        case preview
        case source
        case voiceRecordID = "voiceRecordId"
        case hasAudio
        case audioURL = "audioUrl"
        case durationMS = "durationMs"
        case state
        case failureMessage
        case createdAt
        case updatedAt
        case archivedAt
        case purgeAfter
        case contentHash
        case linkedExternalTargetCount
        case permanentDeleteAvailable
        case transcriptVersions
    }

    var rawTranscript: String? {
        transcriptVersions?
            .first(where: { $0.versionType == "asr_raw" })?
            .content
    }

    var hermesCleanedTranscript: String? {
        transcriptVersions?
            .first(where: { $0.versionType == "hermes_cleaned" })?
            .content
    }

    var isVoice: Bool { kind == "voice" }
    var isProcessing: Bool { state == "processing" }
}
