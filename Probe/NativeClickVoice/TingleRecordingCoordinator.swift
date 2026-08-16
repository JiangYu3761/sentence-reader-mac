import Foundation


protocol TingleCaptureDriver: AnyObject {
    var currentTime: TimeInterval { get }
    var inputLevel: Double { get }
    func start(commandSource: String, _ completion: @escaping (Result<ClickVoiceCaptureManifest, Error>) -> Void)
    func pause() throws -> ClickVoiceCaptureManifest
    func resume() throws -> ClickVoiceCaptureManifest
    func stop(commandSource: String) throws -> ClickVoiceCaptureManifest
    func loadPendingCaptures() -> [ClickVoiceCaptureManifest]
    func loadLatestCapture() -> ClickVoiceCaptureManifest?
    func markCaptureUploaded(_ capture: ClickVoiceCaptureManifest, voiceRecordID: String) throws
    func markCaptureUploadFailed(_ capture: ClickVoiceCaptureManifest, message: String) throws
    func preserveActiveCaptureForTermination()
}


extension ClickVoiceRecorder: TingleCaptureDriver {}


protocol TingleCaptureUploader: AnyObject {
    func createVoiceRecord(
        from capture: ClickVoiceCaptureManifest,
        completion: @escaping (Result<[String: Any], Error>) -> Void
    )
}


extension ClickVoiceAPIClient: TingleCaptureUploader {}


protocol TingleRecordingCoordinatorDelegate: AnyObject {
    func tingleRecordingCoordinator(_ coordinator: TingleRecordingCoordinator, didChange snapshot: TingleRecordingSnapshot)
    func tingleRecordingCoordinator(_ coordinator: TingleRecordingCoordinator, didSaveLocally capture: ClickVoiceCaptureManifest)
    func tingleRecordingCoordinator(
        _ coordinator: TingleRecordingCoordinator,
        didUpload capture: ClickVoiceCaptureManifest,
        voiceRecordID: String
    )
    func tingleRecordingCoordinatorNeedsRuntimeRecovery(_ coordinator: TingleRecordingCoordinator)
}


extension TingleRecordingCoordinatorDelegate {
    func tingleRecordingCoordinatorNeedsRuntimeRecovery(_ coordinator: TingleRecordingCoordinator) {}
}


final class TingleRecordingCoordinator {
    typealias Scheduler = (_ delay: TimeInterval, _ action: @escaping () -> Void) -> Void
    typealias StartCue = (_ completion: @escaping () -> Void) -> Void
    typealias StopCue = () -> Void

    weak var delegate: TingleRecordingCoordinatorDelegate?

    private let captureDriver: TingleCaptureDriver
    private let uploader: TingleCaptureUploader
    private let scheduler: Scheduler
    private let playStartCue: StartCue
    private let playStopCue: StopCue
    private var runtimeAvailable = false
    private var currentCapture: ClickVoiceCaptureManifest?
    private var pendingCaptures: [String: ClickVoiceCaptureManifest] = [:]
    private var uploadsInFlight = Set<String>()
    private var saveGeneration = 0

    private(set) var snapshot = TingleRecordingSnapshot(
        state: .idle,
        syncState: .ready,
        captureID: nil,
        voiceRecordID: nil,
        elapsedSeconds: 0,
        inputLevel: 0,
        pendingSyncCount: 0,
        errorMessage: nil
    )

    init(
        captureDriver: TingleCaptureDriver,
        uploader: TingleCaptureUploader,
        scheduler: @escaping Scheduler = { delay, action in
            DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: action)
        },
        playStartCue: @escaping StartCue = { completion in completion() },
        playStopCue: @escaping StopCue = {}
    ) {
        self.captureDriver = captureDriver
        self.uploader = uploader
        self.scheduler = scheduler
        self.playStartCue = playStartCue
        self.playStopCue = playStopCue
    }

    @discardableResult
    func recoverPendingCaptures() -> Int {
        for capture in captureDriver.loadPendingCaptures() {
            pendingCaptures[capture.captureID] = capture
        }
        if let latest = captureDriver.loadLatestCapture(),
           let voiceRecordID = latest.voiceRecordID,
           !voiceRecordID.isEmpty {
            delegate?.tingleRecordingCoordinator(
                self,
                didUpload: latest,
                voiceRecordID: voiceRecordID
            )
        }
        publishPendingCount()
        if runtimeAvailable {
            syncPendingCaptures()
        }
        return pendingCaptures.count
    }

    func setRuntimeAvailable(_ available: Bool) {
        runtimeAvailable = available
        if available {
            if snapshot.syncState == .pending || snapshot.syncState == .failed {
                snapshot.syncState = pendingCaptures.isEmpty ? .ready : .uploading
            }
            publish()
            syncPendingCaptures()
        } else if !pendingCaptures.isEmpty {
            snapshot.syncState = .pending
            publish()
        }
    }

    @discardableResult
    func toggle(source: TingleCommandSource) -> TingleCommandDisposition {
        switch snapshot.state {
        case .idle:
            return start(source: source)
        case .recording, .paused:
            return stopAndSave(source: source)
        case .preparing, .finalizing, .savedLocally:
            return .busy
        case .failed:
            resetToIdle()
            return start(source: source)
        }
    }

    @discardableResult
    func start(source: TingleCommandSource) -> TingleCommandDisposition {
        guard snapshot.state == .idle else {
            return snapshot.state == .recording || snapshot.state == .paused ? .noOp : .busy
        }
        snapshot = TingleRecordingSnapshot(
            state: .preparing,
            syncState: snapshot.syncState,
            captureID: nil,
            voiceRecordID: nil,
            elapsedSeconds: 0,
            inputLevel: 0,
            pendingSyncCount: pendingCaptures.count,
            errorMessage: nil
        )
        publish()
        playStartCue { [weak self] in
            guard let self, self.snapshot.state == .preparing else { return }
            self.captureDriver.start(commandSource: source.rawValue) { [weak self] result in
                guard let self else { return }
                switch result {
                case let .success(capture):
                    guard self.snapshot.state == .preparing else { return }
                    self.currentCapture = capture
                    self.snapshot.state = .recording
                    self.snapshot.captureID = capture.captureID
                    self.snapshot.elapsedSeconds = capture.durationSeconds ?? 0
                    self.snapshot.inputLevel = 0
                    self.snapshot.errorMessage = nil
                case let .failure(error):
                    self.currentCapture = nil
                    self.snapshot.state = .failed
                    self.snapshot.captureID = nil
                    self.snapshot.errorMessage = error.localizedDescription
                }
                self.publish()
            }
        }
        return .accepted
    }

    @discardableResult
    func pauseOrResume(source: TingleCommandSource) -> TingleCommandDisposition {
        do {
            switch snapshot.state {
            case .recording:
                currentCapture = try captureDriver.pause()
                snapshot.state = .paused
            case .paused:
                currentCapture = try captureDriver.resume()
                snapshot.state = .recording
            case .preparing, .finalizing, .savedLocally:
                return .busy
            default:
                return .noOp
            }
            refreshElapsedTime()
            publish()
            return .accepted
        } catch {
            fail(error.localizedDescription)
            return .unavailable
        }
    }

    @discardableResult
    func stopAndSave(source: TingleCommandSource) -> TingleCommandDisposition {
        guard snapshot.state == .recording || snapshot.state == .paused else {
            return snapshot.state == .preparing || snapshot.state == .finalizing || snapshot.state == .savedLocally
                ? .busy
                : .noOp
        }
        snapshot.state = .finalizing
        snapshot.errorMessage = nil
        publish()
        do {
            let capture = try captureDriver.stop(commandSource: source.rawValue)
            playStopCue()
            currentCapture = capture
            pendingCaptures[capture.captureID] = capture
            snapshot.state = .savedLocally
            snapshot.syncState = runtimeAvailable ? .uploading : .pending
            snapshot.captureID = capture.captureID
            snapshot.elapsedSeconds = capture.durationSeconds ?? captureDriver.currentTime
            snapshot.inputLevel = 0
            snapshot.pendingSyncCount = pendingCaptures.count
            snapshot.errorMessage = nil
            publish()
            delegate?.tingleRecordingCoordinator(self, didSaveLocally: capture)
            if runtimeAvailable {
                upload(capture)
            }
            scheduleCaptureReady(after: 0.35)
            return .accepted
        } catch {
            fail(error.localizedDescription)
            return .unavailable
        }
    }

    func refreshElapsedTime() {
        guard snapshot.state == .recording || snapshot.state == .paused else { return }
        snapshot.elapsedSeconds = captureDriver.currentTime
        snapshot.inputLevel = captureDriver.inputLevel
    }

    func preserveForTermination() {
        captureDriver.preserveActiveCaptureForTermination()
    }

    private func syncPendingCaptures() {
        guard runtimeAvailable else { return }
        for capture in pendingCaptures.values.sorted(by: { $0.createdAt < $1.createdAt }) {
            upload(capture)
        }
    }

    private func upload(_ capture: ClickVoiceCaptureManifest) {
        guard runtimeAvailable, !uploadsInFlight.contains(capture.captureID) else { return }
        uploadsInFlight.insert(capture.captureID)
        if snapshot.captureID == capture.captureID {
            snapshot.syncState = .uploading
            publish()
        }
        uploader.createVoiceRecord(from: capture) { [weak self] result in
            guard let self else { return }
            self.uploadsInFlight.remove(capture.captureID)
            switch result {
            case let .success(record):
                let recordID = String(describing: record["id"] ?? "")
                guard !recordID.isEmpty else {
                    self.markUploadFailure(capture, message: "Click Runtime 未返回 VoiceRecord 标识")
                    return
                }
                try? self.captureDriver.markCaptureUploaded(capture, voiceRecordID: recordID)
                self.pendingCaptures.removeValue(forKey: capture.captureID)
                if self.snapshot.captureID == capture.captureID {
                    self.snapshot.voiceRecordID = recordID
                    self.snapshot.syncState = .synced
                    self.snapshot.errorMessage = nil
                }
                self.publishPendingCount()
                self.delegate?.tingleRecordingCoordinator(
                    self,
                    didUpload: capture,
                    voiceRecordID: recordID
                )
            case let .failure(error):
                self.markUploadFailure(capture, message: error.localizedDescription)
            }
        }
    }

    private func markUploadFailure(_ capture: ClickVoiceCaptureManifest, message: String) {
        try? captureDriver.markCaptureUploadFailed(capture, message: message)
        pendingCaptures[capture.captureID] = capture
        runtimeAvailable = false
        if snapshot.captureID == capture.captureID {
            snapshot.syncState = .pending
            snapshot.errorMessage = "录音已安全保存在本机，等待重新同步：\(message)"
        }
        publishPendingCount()
        delegate?.tingleRecordingCoordinatorNeedsRuntimeRecovery(self)
    }

    private func scheduleCaptureReady(after delay: TimeInterval) {
        saveGeneration += 1
        let generation = saveGeneration
        scheduler(delay) { [weak self] in
            guard let self, generation == self.saveGeneration, self.snapshot.state == .savedLocally else { return }
            self.resetToIdle()
        }
    }

    private func resetToIdle() {
        currentCapture = nil
        snapshot.state = .idle
        snapshot.captureID = nil
        snapshot.voiceRecordID = nil
        snapshot.elapsedSeconds = 0
        snapshot.inputLevel = 0
        snapshot.errorMessage = nil
        if pendingCaptures.isEmpty {
            snapshot.syncState = .ready
        } else if runtimeAvailable && !uploadsInFlight.isEmpty {
            snapshot.syncState = .uploading
        } else {
            snapshot.syncState = .pending
        }
        publish()
    }

    private func fail(_ message: String) {
        snapshot.state = .failed
        snapshot.errorMessage = message
        publish()
    }

    private func publishPendingCount() {
        snapshot.pendingSyncCount = pendingCaptures.count
        if snapshot.state == .idle {
            snapshot.syncState = pendingCaptures.isEmpty ? .ready : (runtimeAvailable && !uploadsInFlight.isEmpty ? .uploading : .pending)
        }
        publish()
    }

    private func publish() {
        delegate?.tingleRecordingCoordinator(self, didChange: snapshot)
    }
}
