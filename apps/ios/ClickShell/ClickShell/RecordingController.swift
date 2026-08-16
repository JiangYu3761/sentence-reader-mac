import AVFoundation
import Foundation

struct LocalVoiceCapture: Identifiable, Hashable, Sendable {
    let id: String
    let url: URL
    let createdAt: Date
    let duration: TimeInterval
    let syncState: String
    let serverRecordingID: String?
    let audioHash: String?
    let attemptCount: Int
    let lastError: String?
    let nextAttemptAt: Date?
}

private struct VoiceCaptureManifest: Codable {
    var schema: String
    var captureID: String
    var createdAt: Date
    var duration: TimeInterval
    var syncState: String
    var serverRecordingID: String?
    var audioHash: String?
    var attemptCount: Int
    var lastError: String?
    var nextAttemptAt: Date?
}

@MainActor
final class RecordingController: NSObject, ObservableObject {
    @Published private(set) var isRecording = false
    @Published private(set) var duration: TimeInterval = 0
    @Published private(set) var captures: [LocalVoiceCapture] = []
    @Published private(set) var syncRequestID = UUID()
    @Published var statusMessage = "录音先保存在 iPad，连接后再同步"

    private var recorder: AVAudioRecorder?
    private var meterTimer: Timer?
    private let recordingsURL: URL

    override init() {
        let support = FileManager.default.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first ?? FileManager.default.temporaryDirectory
        recordingsURL = support
            .appendingPathComponent("ClickWorkspace", isDirectory: true)
            .appendingPathComponent("Recordings", isDirectory: true)
        super.init()
        try? FileManager.default.createDirectory(
            at: recordingsURL,
            withIntermediateDirectories: true
        )
        reloadCaptures()
    }

    func toggleRecording() {
        if isRecording {
            stop()
        } else {
            Task { await start() }
        }
    }

    func start() async {
        guard !isRecording else { return }
        let granted = await withCheckedContinuation { continuation in
            AVAudioApplication.requestRecordPermission { allowed in
                continuation.resume(returning: allowed)
            }
        }
        guard granted else {
            statusMessage = "没有麦克风权限；可以在系统设置中重新允许"
            return
        }

        let captureID = "ipad-\(UUID().uuidString.lowercased())"
        let destination = recordingsURL.appendingPathComponent("\(captureID).m4a")
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatMPEG4AAC,
            AVSampleRateKey: 44_100,
            AVNumberOfChannelsKey: 1,
            AVEncoderAudioQualityKey: AVAudioQuality.high.rawValue,
        ]

        do {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(
                .playAndRecord,
                mode: .spokenAudio,
                options: [.defaultToSpeaker, .allowBluetoothHFP]
            )
            try session.setActive(true)

            let recorder = try AVAudioRecorder(url: destination, settings: settings)
            recorder.delegate = self
            recorder.isMeteringEnabled = true
            recorder.prepareToRecord()
            guard recorder.record() else {
                statusMessage = "录音没有启动"
                return
            }
            self.recorder = recorder
            duration = 0
            isRecording = true
            statusMessage = "正在录音"
            meterTimer?.invalidate()
            meterTimer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) {
                [weak self] timer in
                Task { @MainActor in
                    guard let self, let recorder = self.recorder else {
                        timer.invalidate()
                        return
                    }
                    self.duration = recorder.currentTime
                }
            }
        } catch {
            statusMessage = "无法开始录音：\(error.localizedDescription)"
        }
    }

    func stop() {
        guard let recorder else { return }
        let finalDuration = recorder.currentTime
        recorder.stop()
        meterTimer?.invalidate()
        meterTimer = nil
        duration = finalDuration
        self.recorder = nil
        isRecording = false
        statusMessage = "原音已保存在 iPad，连接后同步"
        try? AVAudioSession.sharedInstance().setActive(
            false,
            options: [.notifyOthersOnDeactivation]
        )
        saveManifest(
            VoiceCaptureManifest(
                schema: "click.ipad.recording.v1",
                captureID: recorder.url.deletingPathExtension().lastPathComponent,
                createdAt: Date(),
                duration: duration,
                syncState: "pending",
                serverRecordingID: nil,
                audioHash: nil,
                attemptCount: 0,
                lastError: nil,
                nextAttemptAt: nil
            )
        )
        reloadCaptures()
        syncRequestID = UUID()
    }

    var pendingCount: Int {
        captures.filter {
            ($0.syncState == "pending" || $0.syncState == "retryable")
        }.count
    }

    func nextPendingCapture() -> LocalVoiceCapture? {
        captures
            .filter {
                ($0.syncState == "pending" || $0.syncState == "retryable")
                    && ($0.nextAttemptAt.map { $0 <= Date() } ?? true)
            }
            .sorted { $0.createdAt < $1.createdAt }
            .first
    }

    func markHash(captureID: String, audioHash: String) {
        guard var manifest = loadManifest(captureID: captureID) else { return }
        manifest.audioHash = audioHash.lowercased()
        saveManifest(manifest)
        reloadCaptures()
    }

    func markUploading(captureID: String) {
        guard var manifest = loadManifest(captureID: captureID) else { return }
        manifest.syncState = "uploading"
        manifest.lastError = nil
        saveManifest(manifest)
        reloadCaptures()
    }

    func markSynced(
        captureID: String,
        serverRecordingID: String,
        audioHash: String
    ) {
        guard var manifest = loadManifest(captureID: captureID) else { return }
        manifest.syncState = "synced"
        manifest.serverRecordingID = serverRecordingID
        manifest.audioHash = audioHash.lowercased()
        manifest.lastError = nil
        manifest.nextAttemptAt = nil
        saveManifest(manifest)
        statusMessage = "录音已同步；iPad 原音仍保留"
        reloadCaptures()
    }

    func markUploadFailure(
        captureID: String,
        message: String,
        retryable: Bool
    ) {
        guard var manifest = loadManifest(captureID: captureID) else { return }
        manifest.attemptCount += 1
        manifest.syncState = retryable ? "retryable" : "permanent_failed"
        manifest.lastError = message
        if retryable {
            let exponent = min(manifest.attemptCount, 8)
            manifest.nextAttemptAt = Date().addingTimeInterval(
                min(21_600, 30 * pow(2, Double(exponent)))
            )
        } else {
            manifest.nextAttemptAt = nil
        }
        saveManifest(manifest)
        statusMessage = retryable
            ? "录音保留在 iPad，下次连接后继续同步"
            : "这条录音未被服务接受；iPad 原音仍保留"
        reloadCaptures()
    }

    private func reloadCaptures() {
        let keys: Set<URLResourceKey> = [.creationDateKey, .contentModificationDateKey]
        let urls = (
            try? FileManager.default.contentsOfDirectory(
                at: recordingsURL,
                includingPropertiesForKeys: Array(keys),
                options: [.skipsHiddenFiles]
            )
        ) ?? []
        captures = urls
            .filter { $0.pathExtension.lowercased() == "m4a" }
            .compactMap { url in
                let values = try? url.resourceValues(forKeys: keys)
                let seconds = (try? AVAudioPlayer(contentsOf: url).duration) ?? 0
                let captureID = url.deletingPathExtension().lastPathComponent
                let manifest = loadManifest(captureID: captureID)
                    ?? VoiceCaptureManifest(
                        schema: "click.ipad.recording.v1",
                        captureID: captureID,
                        createdAt: values?.creationDate
                            ?? values?.contentModificationDate
                            ?? .distantPast,
                        duration: seconds.isFinite ? seconds : 0,
                        syncState: "pending",
                        serverRecordingID: nil,
                        audioHash: nil,
                        attemptCount: 0,
                        lastError: nil,
                        nextAttemptAt: nil
                    )
                if loadManifest(captureID: captureID) == nil {
                    saveManifest(manifest)
                }
                return LocalVoiceCapture(
                    id: captureID,
                    url: url,
                    createdAt: manifest.createdAt,
                    duration: manifest.duration,
                    syncState: manifest.syncState == "uploading"
                        ? "retryable"
                        : manifest.syncState,
                    serverRecordingID: manifest.serverRecordingID,
                    audioHash: manifest.audioHash,
                    attemptCount: manifest.attemptCount,
                    lastError: manifest.lastError,
                    nextAttemptAt: manifest.nextAttemptAt
                )
            }
            .sorted { $0.createdAt > $1.createdAt }
    }

    private func manifestURL(captureID: String) -> URL {
        recordingsURL.appendingPathComponent("\(captureID).json")
    }

    private func loadManifest(captureID: String) -> VoiceCaptureManifest? {
        guard
            let data = try? Data(contentsOf: manifestURL(captureID: captureID))
        else {
            return nil
        }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return try? decoder.decode(VoiceCaptureManifest.self, from: data)
    }

    private func saveManifest(_ manifest: VoiceCaptureManifest) {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        guard let data = try? encoder.encode(manifest) else { return }
        try? data.write(
            to: manifestURL(captureID: manifest.captureID),
            options: .atomic
        )
    }
}

extension RecordingController: AVAudioRecorderDelegate {
    nonisolated func audioRecorderEncodeErrorDidOccur(
        _ recorder: AVAudioRecorder,
        error: Error?
    ) {
        Task { @MainActor in
            isRecording = false
            meterTimer?.invalidate()
            meterTimer = nil
            self.recorder = nil
            statusMessage = error.map {
                "录音中断，已有原音仍保留：\($0.localizedDescription)"
            } ?? "录音中断，已有原音仍保留"
            reloadCaptures()
        }
    }
}
