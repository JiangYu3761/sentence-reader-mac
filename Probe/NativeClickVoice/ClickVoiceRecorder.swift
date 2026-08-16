import AVFoundation
import CryptoKit
import Foundation


enum ClickVoiceCaptureState: String, Codable {
    case preparing
    case recording
    case paused
    case pendingUpload = "pending_upload"
    case uploaded
    case uploadFailed = "upload_failed"
    case captureFailed = "capture_failed"
}


struct ClickVoiceCaptureManifest: Codable {
    static let schemaName = "click.voice.native_capture.v1"

    var schema = schemaName
    var captureID: String
    var createdAt: String
    var updatedAt: String
    var state: ClickVoiceCaptureState
    var audioPath: String
    var mimeType = "audio/wav"
    var durationSeconds: Double?
    var audioHash: String?
    var originRef: String
    var voiceRecordID: String?
    var sourcePlatform = "macOS Tingle"
    var startCommandSource: String?
    var stopCommandSource: String?
    var errorMessage: String?

    enum CodingKeys: String, CodingKey {
        case schema
        case captureID = "capture_id"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case state
        case audioPath = "audio_path"
        case mimeType = "mime_type"
        case durationSeconds = "duration_seconds"
        case audioHash = "audio_hash"
        case originRef = "origin_ref"
        case voiceRecordID = "voice_record_id"
        case sourcePlatform = "source_platform"
        case startCommandSource = "start_command_source"
        case stopCommandSource = "stop_command_source"
        case errorMessage = "error_message"
    }
}


protocol ClickVoiceRecorderDelegate: AnyObject {
    func clickVoiceRecorder(_ recorder: ClickVoiceRecorder, didChange manifest: ClickVoiceCaptureManifest)
    func clickVoiceRecorder(_ recorder: ClickVoiceRecorder, didFail message: String)
}


typealias ClickVoicePermissionRequester = (@escaping (Bool) -> Void) -> Void


final class ClickVoiceRecorder: NSObject, AVAudioRecorderDelegate {
    weak var delegate: ClickVoiceRecorderDelegate?

    private(set) var manifest: ClickVoiceCaptureManifest?
    private var audioRecorder: AVAudioRecorder?
    private let storageRoot: URL
    private let encoder: JSONEncoder
    private let decoder: JSONDecoder
    private let permissionRequester: ClickVoicePermissionRequester?

    init(
        storageRoot: URL = ClickVoiceRecorder.defaultStorageRoot(),
        permissionRequester: ClickVoicePermissionRequester? = nil
    ) {
        self.storageRoot = storageRoot
        self.encoder = JSONEncoder()
        self.decoder = JSONDecoder()
        self.permissionRequester = permissionRequester
        self.encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        super.init()
    }

    static func defaultStorageRoot() -> URL {
        let appSupport = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Library/Application Support", isDirectory: true)
        return appSupport
            .appendingPathComponent("Click", isDirectory: true)
            .appendingPathComponent("VoiceInbox", isDirectory: true)
            .appendingPathComponent("NativeRecords", isDirectory: true)
    }

    var state: ClickVoiceCaptureState? { manifest?.state }
    var isRecording: Bool { audioRecorder?.isRecording == true && manifest?.state == .recording }
    var isPaused: Bool { manifest?.state == .paused }
    var currentTime: TimeInterval { audioRecorder?.currentTime ?? manifest?.durationSeconds ?? 0 }
    var inputLevel: Double {
        guard let recorder = audioRecorder, recorder.isRecording, manifest?.state == .recording else { return 0 }
        recorder.updateMeters()
        let decibels = max(-60, min(0, recorder.averagePower(forChannel: 0)))
        return pow(10, Double(decibels) / 20)
    }

    func requestPermission(_ completion: @escaping (Bool) -> Void) {
        if let permissionRequester {
            permissionRequester(completion)
            return
        }
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            completion(true)
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .audio) { granted in
                DispatchQueue.main.async { completion(granted) }
            }
        default:
            completion(false)
        }
    }

    func start(
        commandSource: String = "unknown",
        _ completion: @escaping (Result<ClickVoiceCaptureManifest, Error>) -> Void
    ) {
        guard audioRecorder == nil else {
            completion(.failure(ClickVoiceRecorderError.alreadyActive))
            return
        }
        requestPermission { [weak self] granted in
            guard let self else { return }
            guard granted else {
                completion(.failure(ClickVoiceRecorderError.microphoneDenied))
                return
            }
            do {
                let capture = try self.prepareCapture(commandSource: commandSource)
                guard self.audioRecorder?.record() == true else {
                    throw ClickVoiceRecorderError.recordingDidNotStart
                }
                let updated = try self.updateManifest(capture, state: .recording)
                self.delegate?.clickVoiceRecorder(self, didChange: updated)
                completion(.success(updated))
            } catch {
                self.failCapture(error.localizedDescription)
                completion(.failure(error))
            }
        }
    }

    @discardableResult
    func pause() throws -> ClickVoiceCaptureManifest {
        guard let recorder = audioRecorder, recorder.isRecording, let current = manifest else {
            throw ClickVoiceRecorderError.notRecording
        }
        recorder.pause()
        let updated = try updateManifest(current, state: .paused, duration: recorder.currentTime)
        delegate?.clickVoiceRecorder(self, didChange: updated)
        return updated
    }

    @discardableResult
    func resume() throws -> ClickVoiceCaptureManifest {
        guard let recorder = audioRecorder, manifest?.state == .paused, let current = manifest else {
            throw ClickVoiceRecorderError.notPaused
        }
        guard recorder.record() else {
            throw ClickVoiceRecorderError.recordingDidNotStart
        }
        let updated = try updateManifest(current, state: .recording)
        delegate?.clickVoiceRecorder(self, didChange: updated)
        return updated
    }

    @discardableResult
    func stop(commandSource: String = "unknown") throws -> ClickVoiceCaptureManifest {
        guard let recorder = audioRecorder, let current = manifest else {
            throw ClickVoiceRecorderError.notRecording
        }
        let recorderDuration = recorder.currentTime
        recorder.stop()
        audioRecorder = nil
        let audioURL = URL(fileURLWithPath: current.audioPath)
        let duration = Self.audioDuration(at: audioURL) ?? recorderDuration
        guard FileManager.default.fileExists(atPath: audioURL.path),
              (try? audioURL.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0) ?? 0 > 0
        else {
            let failed = try updateManifest(current, state: .captureFailed, duration: duration, error: "录音文件为空")
            delegate?.clickVoiceRecorder(self, didChange: failed)
            throw ClickVoiceRecorderError.emptyAudio
        }
        let hash = try Self.sha256(audioURL)
        var stopped = current
        stopped.stopCommandSource = commandSource
        let updated = try updateManifest(
            stopped,
            state: .pendingUpload,
            duration: duration,
            audioHash: hash,
            error: nil
        )
        delegate?.clickVoiceRecorder(self, didChange: updated)
        return updated
    }

    func markUploaded(voiceRecordID: String) throws -> ClickVoiceCaptureManifest {
        guard let current = manifest else { throw ClickVoiceRecorderError.missingManifest }
        try markCaptureUploaded(current, voiceRecordID: voiceRecordID)
        return try loadManifest(for: current.captureID)
    }

    func markUploadFailed(_ message: String) throws -> ClickVoiceCaptureManifest {
        guard let current = manifest else { throw ClickVoiceRecorderError.missingManifest }
        try markCaptureUploadFailed(current, message: message)
        return try loadManifest(for: current.captureID)
    }

    func markCaptureUploaded(_ capture: ClickVoiceCaptureManifest, voiceRecordID: String) throws {
        var updated = capture
        updated.voiceRecordID = voiceRecordID
        updated.state = .uploaded
        updated.updatedAt = Self.timestamp()
        updated.errorMessage = nil
        try writeManifest(updated)
        if manifest?.captureID == capture.captureID {
            manifest = updated
            delegate?.clickVoiceRecorder(self, didChange: updated)
        }
    }

    func markCaptureUploadFailed(_ capture: ClickVoiceCaptureManifest, message: String) throws {
        var updated = capture
        updated.state = .uploadFailed
        updated.updatedAt = Self.timestamp()
        updated.errorMessage = message
        try writeManifest(updated)
        if manifest?.captureID == capture.captureID {
            manifest = updated
            delegate?.clickVoiceRecorder(self, didChange: updated)
        }
    }

    func markRecoveredCaptureUploaded(
        _ capture: ClickVoiceCaptureManifest,
        voiceRecordID: String
    ) throws -> ClickVoiceCaptureManifest {
        var updated = capture
        updated.voiceRecordID = voiceRecordID
        updated.state = .uploaded
        updated.updatedAt = Self.timestamp()
        updated.errorMessage = nil
        try writeManifest(updated)
        return updated
    }

    func markRecoveredCaptureUploadFailed(
        _ capture: ClickVoiceCaptureManifest,
        message: String
    ) throws -> ClickVoiceCaptureManifest {
        var updated = capture
        updated.state = .uploadFailed
        updated.updatedAt = Self.timestamp()
        updated.errorMessage = message
        try writeManifest(updated)
        return updated
    }

    func preserveActiveCaptureForTermination() {
        guard let recorder = audioRecorder,
              let current = manifest,
              current.state == .recording || current.state == .paused
        else {
            return
        }
        let recorderDuration = recorder.currentTime
        recorder.stop()
        audioRecorder = nil
        let audioURL = URL(fileURLWithPath: current.audioPath)
        let duration = Self.audioDuration(at: audioURL) ?? recorderDuration
        let size = (try? audioURL.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
        guard size > 0,
              let hash = try? Self.sha256(audioURL)
        else {
            _ = try? updateManifest(current, state: .captureFailed, duration: duration, error: "退出时录音文件为空")
            return
        }
        _ = try? updateManifest(
            current,
            state: .pendingUpload,
            duration: duration,
            audioHash: hash,
            error: "App 退出前已封存录音，等待下次启动同步"
        )
    }

    func loadPendingCaptures() -> [ClickVoiceCaptureManifest] {
        Self.recoverInterruptedCaptures(at: storageRoot)
    }

    func loadLatestCapture() -> ClickVoiceCaptureManifest? {
        guard let directories = try? FileManager.default.contentsOfDirectory(
            at: storageRoot,
            includingPropertiesForKeys: [.isDirectoryKey],
            options: [.skipsHiddenFiles]
        ) else { return nil }
        return directories.compactMap { directory -> ClickVoiceCaptureManifest? in
            let manifestURL = directory.appendingPathComponent("capture.json")
            guard let data = try? Data(contentsOf: manifestURL),
                  let capture = try? decoder.decode(ClickVoiceCaptureManifest.self, from: data)
            else { return nil }
            return capture
        }
        .max { lhs, rhs in lhs.createdAt < rhs.createdAt }
    }

    static func recoverInterruptedCaptures(at root: URL) -> [ClickVoiceCaptureManifest] {
        let decoder = JSONDecoder()
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        guard let directories = try? FileManager.default.contentsOfDirectory(
            at: root,
            includingPropertiesForKeys: [.isDirectoryKey],
            options: [.skipsHiddenFiles]
        ) else {
            return []
        }
        var pending: [ClickVoiceCaptureManifest] = []
        for directory in directories {
            let manifestURL = directory.appendingPathComponent("capture.json")
            guard let data = try? Data(contentsOf: manifestURL),
                  var capture = try? decoder.decode(ClickVoiceCaptureManifest.self, from: data)
            else {
                continue
            }
            if capture.state == .recording || capture.state == .paused || capture.state == .preparing {
                let audioURL = URL(fileURLWithPath: capture.audioPath)
                if audioURL.pathExtension.lowercased() == "wav" {
                    _ = repairInterruptedWAV(at: audioURL)
                }
                let audioFile = try? AVAudioFile(forReading: audioURL)
                let readable = (audioFile?.length ?? 0) > 0
                capture.state = readable ? .pendingUpload : .captureFailed
                capture.updatedAt = Self.timestamp()
                capture.errorMessage = readable
                    ? "上次录音被中断，音频已保留并等待同步"
                    : "上次录音被中断，音频容器未能恢复；原始文件仍保留"
                capture.durationSeconds = audioDuration(at: audioURL)
                if readable {
                    capture.audioHash = try? sha256(audioURL)
                }
                if let encoded = try? encoder.encode(capture) {
                    try? encoded.write(to: manifestURL, options: .atomic)
                }
            }
            if capture.state == .pendingUpload || capture.state == .uploadFailed {
                pending.append(capture)
            }
        }
        return pending.sorted { $0.createdAt < $1.createdAt }
    }

    func audioRecorderEncodeErrorDidOccur(_ recorder: AVAudioRecorder, error: Error?) {
        failCapture(error?.localizedDescription ?? "录音编码失败")
    }

    private func prepareCapture(commandSource: String) throws -> ClickVoiceCaptureManifest {
        try FileManager.default.createDirectory(at: storageRoot, withIntermediateDirectories: true)
        let captureID = UUID().uuidString.lowercased()
        let directory = storageRoot.appendingPathComponent(captureID, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        let audioURL = directory.appendingPathComponent("original.wav")
        let timestamp = Self.timestamp()
        let capture = ClickVoiceCaptureManifest(
            captureID: captureID,
            createdAt: timestamp,
            updatedAt: timestamp,
            state: .preparing,
            audioPath: audioURL.path,
            originRef: "mac-native:\(captureID)",
            startCommandSource: commandSource
        )
        try writeManifest(capture)
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: 16_000,
            AVNumberOfChannelsKey: 1,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsBigEndianKey: false,
            AVLinearPCMIsNonInterleaved: false,
        ]
        let recorder = try AVAudioRecorder(url: audioURL, settings: settings)
        recorder.delegate = self
        recorder.isMeteringEnabled = true
        guard recorder.prepareToRecord() else {
            throw ClickVoiceRecorderError.recordingDidNotPrepare
        }
        manifest = capture
        audioRecorder = recorder
        return capture
    }

    private func updateManifest(
        _ source: ClickVoiceCaptureManifest,
        state: ClickVoiceCaptureState,
        duration: Double? = nil,
        audioHash: String? = nil,
        error: String? = nil
    ) throws -> ClickVoiceCaptureManifest {
        var updated = source
        updated.state = state
        updated.updatedAt = Self.timestamp()
        if let duration { updated.durationSeconds = duration }
        if let audioHash { updated.audioHash = audioHash }
        updated.errorMessage = error
        try writeManifest(updated)
        manifest = updated
        return updated
    }

    private func writeManifest(_ value: ClickVoiceCaptureManifest) throws {
        let audioURL = URL(fileURLWithPath: value.audioPath)
        let manifestURL = audioURL.deletingLastPathComponent().appendingPathComponent("capture.json")
        let data = try encoder.encode(value)
        try data.write(to: manifestURL, options: .atomic)
    }

    private func loadManifest(for captureID: String) throws -> ClickVoiceCaptureManifest {
        let manifestURL = storageRoot
            .appendingPathComponent(captureID, isDirectory: true)
            .appendingPathComponent("capture.json")
        guard let data = try? Data(contentsOf: manifestURL),
              let decoded = try? decoder.decode(ClickVoiceCaptureManifest.self, from: data)
        else {
            throw ClickVoiceRecorderError.missingManifest
        }
        return decoded
    }

    private func failCapture(_ message: String) {
        let recorderDuration = audioRecorder?.currentTime ?? manifest?.durationSeconds
        audioRecorder?.stop()
        audioRecorder = nil
        let duration = manifest.flatMap { Self.audioDuration(at: URL(fileURLWithPath: $0.audioPath)) } ?? recorderDuration
        guard let current = manifest,
              let updated = try? updateManifest(current, state: .captureFailed, duration: duration, error: message)
        else {
            delegate?.clickVoiceRecorder(self, didFail: message)
            return
        }
        delegate?.clickVoiceRecorder(self, didChange: updated)
        delegate?.clickVoiceRecorder(self, didFail: message)
    }

    private static func sha256(_ url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var digest = SHA256()
        while true {
            let data = try handle.read(upToCount: 1_048_576) ?? Data()
            if data.isEmpty { break }
            digest.update(data: data)
        }
        return digest.finalize().map { String(format: "%02x", $0) }.joined()
    }

    static func audioDuration(at url: URL) -> Double? {
        guard let audioFile = try? AVAudioFile(forReading: url),
              audioFile.length > 0,
              audioFile.fileFormat.sampleRate > 0
        else {
            return nil
        }
        return Double(audioFile.length) / audioFile.fileFormat.sampleRate
    }

    @discardableResult
    private static func repairInterruptedWAV(at url: URL) -> Bool {
        guard var data = try? Data(contentsOf: url),
              data.count >= 44,
              String(data: data[0..<4], encoding: .ascii) == "RIFF",
              String(data: data[8..<12], encoding: .ascii) == "WAVE"
        else {
            return false
        }
        var offset = 12
        var dataChunkOffset: Int?
        while offset + 8 <= data.count {
            let chunkID = String(data: data[offset..<(offset + 4)], encoding: .ascii)
            let declaredSize = Int(littleEndianUInt32(data, at: offset + 4))
            if chunkID == "data" {
                dataChunkOffset = offset
                break
            }
            let next = offset + 8 + declaredSize + (declaredSize % 2)
            guard next > offset, next <= data.count else { return false }
            offset = next
        }
        guard let dataChunkOffset else { return false }
        let audioByteCount = data.count - dataChunkOffset - 8
        guard audioByteCount > 0,
              audioByteCount <= Int(UInt32.max),
              data.count - 8 <= Int(UInt32.max)
        else {
            return false
        }
        writeLittleEndianUInt32(UInt32(data.count - 8), into: &data, at: 4)
        writeLittleEndianUInt32(UInt32(audioByteCount), into: &data, at: dataChunkOffset + 4)
        do {
            try data.write(to: url, options: .atomic)
            return true
        } catch {
            return false
        }
    }

    private static func littleEndianUInt32(_ data: Data, at offset: Int) -> UInt32 {
        UInt32(data[offset])
            | (UInt32(data[offset + 1]) << 8)
            | (UInt32(data[offset + 2]) << 16)
            | (UInt32(data[offset + 3]) << 24)
    }

    private static func writeLittleEndianUInt32(_ value: UInt32, into data: inout Data, at offset: Int) {
        data[offset] = UInt8(value & 0xff)
        data[offset + 1] = UInt8((value >> 8) & 0xff)
        data[offset + 2] = UInt8((value >> 16) & 0xff)
        data[offset + 3] = UInt8((value >> 24) & 0xff)
    }

    private static func timestamp() -> String {
        ISO8601DateFormatter().string(from: Date())
    }
}


enum ClickVoiceRecorderError: LocalizedError {
    case alreadyActive
    case microphoneDenied
    case recordingDidNotPrepare
    case recordingDidNotStart
    case notRecording
    case notPaused
    case emptyAudio
    case missingManifest

    var errorDescription: String? {
        switch self {
        case .alreadyActive: return "已有录音正在进行"
        case .microphoneDenied: return "麦克风权限未允许"
        case .recordingDidNotPrepare: return "录音设备未准备好"
        case .recordingDidNotStart: return "录音未能开始"
        case .notRecording: return "当前没有正在录制的语音"
        case .notPaused: return "当前录音没有暂停"
        case .emptyAudio: return "录音文件为空"
        case .missingManifest: return "录音恢复清单不存在"
        }
    }
}
