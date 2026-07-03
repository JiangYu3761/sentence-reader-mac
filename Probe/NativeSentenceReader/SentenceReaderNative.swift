import Cocoa
import WebKit
import AVFoundation
import Speech
import UniformTypeIdentifiers

private enum SpeechTranscriptionProvider: String {
    case appleSpeech = "apple_speech"
    case funASR = "funasr"

    static let defaultsKey = "SentenceReader.speechTranscriptionProvider.v1"

    var title: String {
        switch self {
        case .appleSpeech: return "苹果"
        case .funASR: return "FunASR"
        }
    }

    static var current: SpeechTranscriptionProvider {
        guard let raw = UserDefaults.standard.string(forKey: defaultsKey),
              let provider = SpeechTranscriptionProvider(rawValue: raw)
        else {
            return .funASR
        }
        return provider
    }

    static func fromTitle(_ title: String?) -> SpeechTranscriptionProvider {
        if title == SpeechTranscriptionProvider.appleSpeech.title {
            return .appleSpeech
        }
        return .funASR
    }

    static func save(_ provider: SpeechTranscriptionProvider) {
        UserDefaults.standard.set(provider.rawValue, forKey: defaultsKey)
    }
}

private final class WindowDragView: NSView {
    override var mouseDownCanMoveWindow: Bool { true }

    override func mouseDown(with event: NSEvent) {
        window?.performDrag(with: event)
    }
}

private final class LookupActionTarget: NSObject {
    private let action: () -> Void

    init(action: @escaping () -> Void) {
        self.action = action
        super.init()
    }

    @objc func perform(_ sender: NSButton) {
        action()
    }
}

private final class LookupWordButton: NSButton {
    override var acceptsFirstResponder: Bool { true }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool {
        true
    }

    override func resetCursorRects() {
        super.resetCursorRects()
        addCursorRect(bounds, cursor: .pointingHand)
    }
}

private enum NoteTextNormalizer {
    private static let spokenPunctuation: [(String, String)] = [
        ("新的一行", "\n"),
        ("另起一行", "\n"),
        ("换行", "\n"),
        ("句号", "。"),
        ("逗号", "，"),
        ("顿号", "、"),
        ("问号", "？"),
        ("感叹号", "！"),
        ("叹号", "！"),
        ("冒号", "："),
        ("分号", "；"),
        ("省略号", "……"),
    ]

    private static let closingPunctuation = CharacterSet(charactersIn: "。！？!?…；;：:，,、）)]】》」』”’\"'")

    static func normalized(_ rawText: String) -> String {
        var text = rawText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return text }

        for (spoken, mark) in spokenPunctuation {
            text = text.replacingOccurrences(of: spoken, with: mark)
        }
        text = text.replacingOccurrences(of: "\\s+([，。！？；：、,.!?;:])", with: "$1", options: .regularExpression)
        text = text.replacingOccurrences(of: "([，。！？；：、,.!?;:])\\s+", with: "$1", options: .regularExpression)
        text = text.replacingOccurrences(of: "([。！？!?]){2,}", with: "$1", options: .regularExpression)
        text = text.replacingOccurrences(of: "\\n{3,}", with: "\n\n", options: .regularExpression)
        return ensureSentenceEnding(text.trimmingCharacters(in: .whitespacesAndNewlines))
    }

    private static func ensureSentenceEnding(_ text: String) -> String {
        guard let lastScalar = text.unicodeScalars.last,
              !closingPunctuation.contains(lastScalar)
        else {
            return text
        }
        return isEnglishOnly(text) ? text + "." : text + "。"
    }

    private static func isEnglishOnly(_ text: String) -> Bool {
        let hasLetter = text.unicodeScalars.contains { scalar in
            (65...90).contains(Int(scalar.value)) || (97...122).contains(Int(scalar.value))
        }
        let hasCJK = text.unicodeScalars.contains { scalar in
            (0x4E00...0x9FFF).contains(Int(scalar.value))
        }
        return hasLetter && !hasCJK
    }
}

final class NoteSpeechController: NSObject, AVAudioRecorderDelegate {
    private weak var textView: NSTextView?
    private weak var statusLabel: NSTextField?
    private weak var recordButton: NSButton?
    private weak var providerPopup: NSPopUpButton?
    private let bookID: String?
    private let readerAPI: ReaderAPIClient
    private var recorder: AVAudioRecorder?
    private var audioURL: URL?
    private var audioDurationSeconds: Double?
    private var appleSpeechTask: SFSpeechRecognitionTask?
    private var isTranscribing = false
    private(set) var latestAudioNoteID: String?

    private struct FunASRPaths {
        let python: URL
        let worker: URL
        let source: String
    }

    private let funASRPythonDefaultsKey = "SentenceReader.funASRPythonPath.v1"
    private let funASRWorkerDefaultsKey = "SentenceReader.funASRWorkerPath.v1"
    private let funASRPythonEnvKey = "SENTENCE_READER_FUNASR_PYTHON"
    private let funASRWorkerEnvKey = "SENTENCE_READER_FUNASR_WORKER"
    private let funASRPythonDefaultPath = (NSHomeDirectory() as NSString).appendingPathComponent("Library/Application Support/SentenceReader/FunASR/.venv/bin/python")
    private let funASRWorkerDefaultPath = (NSHomeDirectory() as NSString).appendingPathComponent("Library/Application Support/SentenceReader/FunASR/funasr_worker.py")
    private let funASRServerPort = 18081

    init(textView: NSTextView, statusLabel: NSTextField, recordButton: NSButton, providerPopup: NSPopUpButton, bookID: String?, readerAPI: ReaderAPIClient) {
        self.textView = textView
        self.statusLabel = statusLabel
        self.recordButton = recordButton
        self.providerPopup = providerPopup
        self.bookID = bookID
        self.readerAPI = readerAPI
        super.init()
    }

    @objc func toggleRecording(_ sender: NSButton) {
        if recorder?.isRecording == true {
            stopRecordingAndTranscribe()
        } else if !isTranscribing {
            requestMicrophoneAndStart()
        }
    }

    func cancel() {
        recorder?.stop()
        recorder = nil
        appleSpeechTask?.cancel()
        appleSpeechTask = nil
    }

    @objc func providerChanged(_ sender: NSPopUpButton) {
        SpeechTranscriptionProvider.save(SpeechTranscriptionProvider.fromTitle(sender.titleOfSelectedItem))
        updateStatus("")
    }

    private func selectedProvider() -> SpeechTranscriptionProvider {
        SpeechTranscriptionProvider.fromTitle(providerPopup?.titleOfSelectedItem)
    }

    private func requestMicrophoneAndStart() {
        let status = AVCaptureDevice.authorizationStatus(for: .audio)
        if status == .authorized {
            startRecording()
            return
        }
        if status == .denied || status == .restricted {
            updateStatus("麦克风权限未开启，请到系统设置允许 Click 使用麦克风。")
            return
        }

        updateStatus("正在请求麦克风权限...")
        AVCaptureDevice.requestAccess(for: .audio) { [weak self] granted in
            DispatchQueue.main.async {
                guard let self else { return }
                granted ? self.startRecording() : self.updateStatus("麦克风权限未允许，无法录音。")
            }
        }
    }

    private func startRecording() {
        do {
            let directory = try audioNotesDirectory()
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            let url = directory.appendingPathComponent("note-\(UUID().uuidString).wav")
            let settings: [String: Any] = [
                AVFormatIDKey: Int(kAudioFormatLinearPCM),
                AVSampleRateKey: 16_000,
                AVNumberOfChannelsKey: 1,
                AVLinearPCMBitDepthKey: 16,
                AVLinearPCMIsFloatKey: false,
                AVLinearPCMIsBigEndianKey: false,
            ]

            let recorder = try AVAudioRecorder(url: url, settings: settings)
            recorder.delegate = self
            recorder.prepareToRecord()
            guard recorder.record() else {
                updateStatus("录音启动失败，请检查麦克风输入设备。")
                return
            }

            self.audioURL = url
            self.audioDurationSeconds = nil
            self.recorder = recorder
            recordButton?.title = "停止并转写"
            updateStatus("正在录音。说完后点“停止并转写”。")
        } catch {
            updateStatus("录音启动失败：\(error.localizedDescription)")
        }
    }

    private func stopRecordingAndTranscribe() {
        guard let recorder,
              let audioURL
        else {
            return
        }

        let duration = max(0, recorder.currentTime)
        recorder.stop()
        self.recorder = nil
        self.audioDurationSeconds = duration
        isTranscribing = true
        recordButton?.isEnabled = false
        recordButton?.title = "转写中..."
        let provider = selectedProvider()
        SpeechTranscriptionProvider.save(provider)
        updateStatus(provider == .appleSpeech ? "苹果转写中..." : "FunASR 转写中...")
        let audioNoteID = createPendingAudioNote(audioURL: audioURL, durationSeconds: duration, provider: provider.rawValue)

        if provider == .appleSpeech {
            transcribeWithAppleSpeech(audioURL: audioURL, audioNoteID: audioNoteID)
            return
        }

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                let text = try self.transcribeWithFunASR(audioURL: audioURL)
                DispatchQueue.main.async {
                    self.latestAudioNoteID = audioNoteID
                    self.finishTranscription(text: text, provider: "funasr", displayProvider: "FunASR", audioNoteID: audioNoteID)
                }
            } catch {
                DispatchQueue.main.async {
                    self.latestAudioNoteID = audioNoteID
                    self.updateStatus("FunASR 转写失败，正在改用系统语音识别...")
                    self.transcribeWithAppleSpeech(audioURL: audioURL, audioNoteID: audioNoteID)
                }
            }
        }
    }

    private func audioNotesDirectory() throws -> URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support", isDirectory: true)
        let directory = base.appendingPathComponent("SentenceReader/AudioNotes", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    private func appSupportDirectory() -> URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support", isDirectory: true)
        return base.appendingPathComponent("SentenceReader", isDirectory: true)
    }

    private func runtimeConfigURL() -> URL {
        if let value = ProcessInfo.processInfo.environment["SENTENCE_READER_RUNTIME_CONFIG"],
           !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return URL(fileURLWithPath: (value as NSString).expandingTildeInPath)
        }
        return appSupportDirectory().appendingPathComponent("config/runtime_config.json")
    }

    private func runtimeConfigFunASRValue(_ key: String) -> String? {
        guard let data = try? Data(contentsOf: runtimeConfigURL()),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let funasr = object["funasr"] as? [String: Any],
              let value = funasr[key] as? String
        else {
            return nil
        }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    private func configuredPath(userDefaultsKey: String, envKey: String, runtimeConfigKey: String, legacyDefault: String) -> (path: String, source: String) {
        if let value = UserDefaults.standard.string(forKey: userDefaultsKey),
           !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return (value, "user_defaults")
        }
        if let value = ProcessInfo.processInfo.environment[envKey],
           !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return (value, "environment")
        }
        if let value = runtimeConfigFunASRValue(runtimeConfigKey) {
            return (value, "runtime_config")
        }
        return (legacyDefault, "legacy_default")
    }

    private func resolvedFunASRPaths() -> FunASRPaths {
        let python = configuredPath(
            userDefaultsKey: funASRPythonDefaultsKey,
            envKey: funASRPythonEnvKey,
            runtimeConfigKey: "python",
            legacyDefault: funASRPythonDefaultPath
        )
        let worker = configuredPath(
            userDefaultsKey: funASRWorkerDefaultsKey,
            envKey: funASRWorkerEnvKey,
            runtimeConfigKey: "worker",
            legacyDefault: funASRWorkerDefaultPath
        )
        return FunASRPaths(
            python: URL(fileURLWithPath: (python.path as NSString).expandingTildeInPath),
            worker: URL(fileURLWithPath: (worker.path as NSString).expandingTildeInPath),
            source: "python=\(python.source), worker=\(worker.source)"
        )
    }

    private func createPendingAudioNote(audioURL: URL, durationSeconds: Double, provider: String) -> String? {
        guard let bookID else {
            return nil
        }
        return readerAPI.createAudioNote(
            bookID: bookID,
            audioPath: audioURL.path,
            provider: provider,
            status: "pending",
            transcript: nil,
            durationSeconds: durationSeconds,
            errorMessage: nil
        )
    }

    private func funASRServerURL(path: String) -> URL {
        URL(string: "http://127.0.0.1:\(funASRServerPort)\(path)")!
    }

    private func requestFunASRServer(path: String, method: String, payload: [String: Any]? = nil, timeout: TimeInterval) throws -> [String: Any] {
        var request = URLRequest(url: funASRServerURL(path: path))
        request.httpMethod = method
        request.timeoutInterval = timeout
        if let payload {
            request.httpBody = try JSONSerialization.data(withJSONObject: payload)
            request.setValue("application/json; charset=utf-8", forHTTPHeaderField: "Content-Type")
        }

        let semaphore = DispatchSemaphore(value: 0)
        var responseData: Data?
        var responseStatus = 0
        var responseError: Error?
        let task = URLSession.shared.dataTask(with: request) { data, response, error in
            responseData = data
            responseStatus = (response as? HTTPURLResponse)?.statusCode ?? 0
            responseError = error
            semaphore.signal()
        }
        task.resume()

        let waitResult = semaphore.wait(timeout: .now() + .milliseconds(Int((timeout + 0.5) * 1000)))
        if waitResult == .timedOut {
            task.cancel()
            throw NSError(domain: "SentenceReader.FunASRServer", code: 1, userInfo: [NSLocalizedDescriptionKey: "FunASR 后台服务请求超时"])
        }
        if let responseError {
            throw responseError
        }
        guard responseStatus == 200,
              let responseData,
              let object = try JSONSerialization.jsonObject(with: responseData) as? [String: Any]
        else {
            throw NSError(domain: "SentenceReader.FunASRServer", code: responseStatus, userInfo: [NSLocalizedDescriptionKey: "FunASR 后台服务响应不可用"])
        }
        return object
    }

    private func transcribeWithFunASRServer(audioURL: URL) throws -> String {
        let health = try requestFunASRServer(path: "/health", method: "GET", timeout: 1.0)
        guard health["ok"] as? Bool == true else {
            throw NSError(domain: "SentenceReader.FunASRServer", code: 2, userInfo: [NSLocalizedDescriptionKey: "FunASR 后台服务未就绪"])
        }

        let response = try requestFunASRServer(
            path: "/transcribe",
            method: "POST",
            payload: ["audio": audioURL.path],
            timeout: 120
        )
        guard response["ok"] as? Bool == true else {
            let detail = response["error"] as? String ?? "FunASR 后台服务转写失败"
            throw NSError(domain: "SentenceReader.FunASRServer", code: 3, userInfo: [NSLocalizedDescriptionKey: detail])
        }
        let text = (response["text"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else {
            throw NSError(domain: "SentenceReader.FunASRServer", code: 4, userInfo: [NSLocalizedDescriptionKey: "FunASR 后台服务没有识别出文字"])
        }
        return text
    }

    private func transcribeWithFunASR(audioURL: URL) throws -> String {
        let funASRPaths = resolvedFunASRPaths()
        guard FileManager.default.isExecutableFile(atPath: funASRPaths.python.path),
              FileManager.default.fileExists(atPath: funASRPaths.worker.path)
        else {
            throw NSError(
                domain: "SentenceReader.FunASR",
                code: 1,
                userInfo: [
                    NSLocalizedDescriptionKey:
                        "FunASR Python 或 worker 不存在（\(funASRPaths.source)）。请通过 runtime_config.json、SENTENCE_READER_FUNASR_PYTHON/SENTENCE_READER_FUNASR_WORKER 或本机设置配置。"
                ]
            )
        }

        if let serverText = try? transcribeWithFunASRServer(audioURL: audioURL) {
            return serverText
        }

        let outputURL = audioURL.deletingPathExtension().appendingPathExtension("funasr.json")
        let process = Process()
        process.executableURL = funASRPaths.python
        process.arguments = [
            funASRPaths.worker.path,
            "--audio", audioURL.path,
            "--out", outputURL.path,
            "--model", "paraformer-zh",
            "--vad-model", "fsmn-vad",
            "--punc-model", "ct-punc",
            "--device", "cpu",
        ]

        let stderr = Pipe()
        process.standardError = stderr
        process.standardOutput = Pipe()
        try process.run()

        let deadline = Date().addingTimeInterval(120)
        while process.isRunning && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.2)
        }
        if process.isRunning {
            process.terminate()
            throw NSError(domain: "SentenceReader.FunASR", code: 2, userInfo: [NSLocalizedDescriptionKey: "FunASR 转写超时"])
        }

        guard process.terminationStatus == 0 else {
            let data = stderr.fileHandleForReading.readDataToEndOfFile()
            let detail = String(data: data, encoding: .utf8) ?? "未知错误"
            throw NSError(domain: "SentenceReader.FunASR", code: Int(process.terminationStatus), userInfo: [NSLocalizedDescriptionKey: detail])
        }

        let data = try Data(contentsOf: outputURL)
        let json = try JSONSerialization.jsonObject(with: data)
        guard let items = json as? [[String: Any]] else {
            throw NSError(domain: "SentenceReader.FunASR", code: 3, userInfo: [NSLocalizedDescriptionKey: "FunASR 输出格式不可读"])
        }

        let text = items.compactMap { $0["text"] as? String }.joined(separator: " ").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else {
            throw NSError(domain: "SentenceReader.FunASR", code: 4, userInfo: [NSLocalizedDescriptionKey: "FunASR 没有识别出文字"])
        }
        return text
    }

    private func transcribeWithAppleSpeech(audioURL: URL, audioNoteID: String?) {
        SFSpeechRecognizer.requestAuthorization { [weak self] status in
            DispatchQueue.main.async {
                guard let self else { return }
                guard status == .authorized,
                      let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "zh-CN")),
                      recognizer.isAvailable
                else {
                    self.failTranscription("苹果语音不可用", audioNoteID: audioNoteID)
                    return
                }

                let request = SFSpeechURLRecognitionRequest(url: audioURL)
                request.shouldReportPartialResults = false
                self.appleSpeechTask = recognizer.recognitionTask(with: request) { [weak self] result, error in
                    guard let self else { return }
                    if let result, result.isFinal {
                        self.finishTranscription(text: result.bestTranscription.formattedString, provider: "apple_speech", displayProvider: "系统语音识别", audioNoteID: audioNoteID)
                        return
                    }
                    if let error {
                        self.failTranscription("语音识别失败：\(error.localizedDescription)", audioNoteID: audioNoteID)
                    }
                }
            }
        }
    }

    private func finishTranscription(text: String, provider: String, displayProvider: String, audioNoteID: String?) {
        let cleaned = NoteTextNormalizer.normalized(text)
        guard !cleaned.isEmpty else {
            failTranscription("\(displayProvider) 没有识别出文字。", audioNoteID: audioNoteID)
            return
        }

        if let textView {
            let existing = textView.string.trimmingCharacters(in: .whitespacesAndNewlines)
            textView.string = existing.isEmpty ? cleaned : existing + "\n" + cleaned
        }
        if let audioNoteID {
            DispatchQueue.global(qos: .utility).async { [readerAPI] in
                _ = readerAPI.updateAudioNote(
                    audioNoteID: audioNoteID,
                    annotationID: nil,
                    provider: provider,
                    transcript: cleaned,
                    status: "transcribed",
                    errorMessage: nil
                )
            }
        }
        isTranscribing = false
        appleSpeechTask = nil
        recordButton?.isEnabled = true
        recordButton?.title = "开始录音"
        updateStatus("已插入并记录语音笔记（\(displayProvider)）。")
    }

    private func failTranscription(_ message: String, audioNoteID: String? = nil) {
        if let audioNoteID {
            DispatchQueue.global(qos: .utility).async { [readerAPI] in
                _ = readerAPI.updateAudioNote(
                    audioNoteID: audioNoteID,
                    annotationID: nil,
                    provider: nil,
                    transcript: nil,
                    status: "failed",
                    errorMessage: message
                )
            }
        }
        isTranscribing = false
        appleSpeechTask = nil
        recordButton?.isEnabled = true
        recordButton?.title = "开始录音"
        updateStatus(message)
    }

    private func updateStatus(_ text: String) {
        statusLabel?.stringValue = text
    }
}

final class ReaderAPIClient {
    private let baseURL = URL(string: "http://127.0.0.1:18180")!
    private let timeout: TimeInterval = 2.0

    func health() -> Bool {
        guard let json = request(method: "GET", path: "/health", body: nil) as? [String: Any] else {
            return false
        }
        return json["ok"] as? Bool == true
    }

    func createBook(title: String, author: String?, bookHash: String, filePath: String?) -> String? {
        var body: [String: Any] = [
            "title": title,
            "source_kind": "epub",
            "book_hash": bookHash,
        ]
        if let author {
            body["author"] = author
        }
        if let filePath {
            body["file_path"] = filePath
        }
        return (request(method: "POST", path: "/books", body: body) as? [String: Any])?["id"] as? String
    }

    func listBooks() -> [[String: Any]] {
        request(method: "GET", path: "/books", body: nil) as? [[String: Any]] ?? []
    }

    func libraryDashboard() -> [String: Any]? {
        request(method: "GET", path: "/api/library/dashboard", body: nil) as? [String: Any]
    }

    func getPosition(bookID: String) -> [String: Any]? {
        request(method: "GET", path: "/books/\(bookID)/position", body: nil) as? [String: Any]
    }

    func savePosition(bookID: String, chapterLocator: String, chapterIndex: Int, pageIndex: Int, totalPages: Int, pageRatio: Double) {
        let body: [String: Any] = [
            "chapter_locator": chapterLocator,
            "page_index": pageIndex,
            "total_pages": totalPages,
            "page_ratio": pageRatio,
            "locator": [
                "chapterIndex": chapterIndex,
                "chapterLocator": chapterLocator,
                "pageIndex": pageIndex,
                "totalPages": totalPages,
            ],
        ]
        _ = request(method: "PUT", path: "/books/\(bookID)/position", body: body)
    }

    func listAnnotations(bookID: String) -> [[String: Any]] {
        request(method: "GET", path: "/books/\(bookID)/annotations", body: nil) as? [[String: Any]] ?? []
    }

    func exportBook(bookID: String, outputDir: String?) -> [String: Any]? {
        var body: [String: Any] = ["include_json": true]
        if let outputDir {
            body["output_dir"] = outputDir
        }
        return request(method: "POST", path: "/books/\(bookID)/export", body: body) as? [String: Any]
    }

    func syncBookToHermes(bookID: String, outputDir: String?) -> [String: Any]? {
        var body: [String: Any] = ["include_red_highlights": true]
        if let outputDir {
            body["output_dir"] = outputDir
        }
        return request(method: "POST", path: "/books/\(bookID)/sync/hermes", body: body) as? [String: Any]
    }

    func cognitiveReviewQueue() -> [String: Any]? {
        request(method: "POST", path: "/cognitive/review-queue", body: ["limit": 100]) as? [String: Any]
    }

    func cognitiveDashboard() -> [String: Any]? {
        request(method: "POST", path: "/cognitive/dashboard", body: ["limit": 100, "history_limit": 20]) as? [String: Any]
    }

    func cognitiveOperatorDryRun() -> [String: Any]? {
        request(
            method: "POST",
            path: "/cognitive/operator/dry-run",
            body: ["all_ready": true, "allow_empty": true]
        ) as? [String: Any]
    }

    func cognitiveReviewItem() -> [String: Any]? {
        request(
            method: "POST",
            path: "/cognitive/review-item",
            body: ["prefer_statuses": ["ready_to_approve", "needs_review", "blocked"]]
        ) as? [String: Any]
    }

    func cognitiveApprove(candidateID: String, confirmationText: String) -> [String: Any]? {
        request(
            method: "POST",
            path: "/cognitive/operator/approve",
            body: [
                "candidate_intake_id": candidateID,
                "confirmation_text": confirmationText,
            ]
        ) as? [String: Any]
    }

    func createAudioNote(
        bookID: String,
        audioPath: String,
        provider: String,
        status: String,
        transcript: String?,
        durationSeconds: Double?,
        errorMessage: String?
    ) -> String? {
        var body: [String: Any] = [
            "book_id": bookID,
            "audio_path": audioPath,
            "provider": provider,
            "status": status,
        ]
        if let transcript {
            body["transcript"] = transcript
        }
        if let durationSeconds {
            body["duration_seconds"] = durationSeconds
        }
        if let errorMessage {
            body["error_message"] = errorMessage
        }
        return (request(method: "POST", path: "/audio-notes", body: body) as? [String: Any])?["id"] as? String
    }

    func updateAudioNote(
        audioNoteID: String,
        annotationID: String?,
        provider: String?,
        transcript: String?,
        status: String?,
        errorMessage: String?
    ) -> Bool {
        var body: [String: Any] = [:]
        if let annotationID {
            body["annotation_id"] = annotationID
        }
        if let provider {
            body["provider"] = provider
        }
        if let transcript {
            body["transcript"] = transcript
        }
        if let status {
            body["status"] = status
        }
        if let errorMessage {
            body["error_message"] = errorMessage
        }
        return request(method: "PATCH", path: "/audio-notes/\(audioNoteID)", body: body) != nil
    }

    func createAnnotation(
        bookID: String,
        kind: String,
        sourceText: String,
        noteText: String?,
        color: String?,
        chapterTitle: String?,
        chapterLocator: String,
        sentenceIndex: String
    ) -> String? {
        var body: [String: Any] = [
            "book_id": bookID,
            "kind": kind,
            "source_text": sourceText,
            "chapter_locator": chapterLocator,
            "range_locator": [
                "chapterLocator": chapterLocator,
                "sentenceIndex": sentenceIndex,
            ],
            "metadata": [
                "source": "SentenceReaderNative",
                "sentenceIndex": sentenceIndex,
            ],
        ]
        if let noteText {
            body["note_text"] = noteText
        }
        if let color {
            body["color"] = color
        }
        if let chapterTitle {
            body["chapter_title"] = chapterTitle
        }
        return (request(method: "POST", path: "/annotations", body: body) as? [String: Any])?["id"] as? String
    }

    func deleteAnnotation(annotationID: String) -> Bool {
        request(method: "DELETE", path: "/annotations/\(annotationID)", body: nil) != nil
    }

    func updateAnnotation(annotationID: String, noteText: String?, color: String?) -> Bool {
        var body: [String: Any] = [:]
        if let noteText {
            body["note_text"] = noteText
        }
        if let color {
            body["color"] = color
        }
        return request(method: "PATCH", path: "/annotations/\(annotationID)", body: body) != nil
    }

    func lookupWord(bookID: String, word: String, sentenceIndex: String?) -> [String: Any]? {
        var components = URLComponents()
        components.path = "/books/\(bookID)/lookup"
        var queryItems = [URLQueryItem(name: "word", value: word)]
        if let sentenceIndex, !sentenceIndex.isEmpty {
            queryItems.append(URLQueryItem(name: "sentence_id", value: sentenceIndex))
        }
        components.queryItems = queryItems
        return request(method: "GET", path: components.string ?? "/books/\(bookID)/lookup", body: nil) as? [String: Any]
    }

    func lookupTTS(text: String, voice: String? = nil) -> URL? {
        let url = baseURL.appendingPathComponent("lookup/tts")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 20
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var body: [String: Any] = ["text": text]
        if let voice, !voice.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            body["voice"] = voice
        }
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)

        let semaphore = DispatchSemaphore(value: 0)
        var payload: [String: Any]?
        let task = URLSession.shared.dataTask(with: request) { data, response, _ in
            defer { semaphore.signal() }
            guard let http = response as? HTTPURLResponse,
                  (200..<300).contains(http.statusCode),
                  let data,
                  !data.isEmpty
            else {
                return
            }
            payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        }
        task.resume()
        _ = semaphore.wait(timeout: .now() + 21)

        guard payload?["ok"] as? Bool == true,
              let audioPath = payload?["audio_url"] as? String,
              !audioPath.isEmpty
        else {
            return nil
        }
        if let absolute = URL(string: audioPath), absolute.scheme != nil {
            return absolute
        }
        let relativePath = audioPath.hasPrefix("/") ? String(audioPath.dropFirst()) : audioPath
        return baseURL.appendingPathComponent(relativePath)
    }

    func lookupTTSData(text: String, voice: String? = nil) -> Data? {
        guard let audioURL = lookupTTS(text: text, voice: voice) else {
            return nil
        }
        return downloadLookupAudioData(from: audioURL)
    }

    func lookupCachedTTSData(text: String, voice: String? = nil) -> Data? {
        let url = baseURL.appendingPathComponent("lookup/tts/status")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 0.35
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var body: [String: Any] = ["text": text]
        if let voice, !voice.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            body["voice"] = voice
        }
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)

        let semaphore = DispatchSemaphore(value: 0)
        var payload: [String: Any]?
        let task = URLSession.shared.dataTask(with: request) { data, response, _ in
            defer { semaphore.signal() }
            guard let http = response as? HTTPURLResponse,
                  (200..<300).contains(http.statusCode),
                  let data,
                  !data.isEmpty
            else {
                return
            }
            payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        }
        task.resume()
        _ = semaphore.wait(timeout: .now() + 0.45)

        guard payload?["cached"] as? Bool == true,
              let audioPath = payload?["audio_url"] as? String,
              !audioPath.isEmpty
        else {
            return nil
        }
        if let absolute = URL(string: audioPath), absolute.scheme != nil {
            return downloadLookupAudioData(from: absolute)
        }
        let relativePath = audioPath.hasPrefix("/") ? String(audioPath.dropFirst()) : audioPath
        return downloadLookupAudioData(from: baseURL.appendingPathComponent(relativePath))
    }

    private func downloadLookupAudioData(from audioURL: URL) -> Data? {
        var request = URLRequest(url: audioURL)
        request.timeoutInterval = 20
        let semaphore = DispatchSemaphore(value: 0)
        var audioData: Data?
        let task = URLSession.shared.dataTask(with: request) { data, response, _ in
            defer { semaphore.signal() }
            guard let http = response as? HTTPURLResponse,
                  (200..<300).contains(http.statusCode),
                  let data,
                  !data.isEmpty
            else {
                return
            }
            audioData = data
        }
        task.resume()
        _ = semaphore.wait(timeout: .now() + 21)
        return audioData
    }

    func updateVocabItem(bookID: String, itemID: String, status: String) -> Bool {
        request(method: "PATCH", path: "/books/\(bookID)/vocab/\(itemID)", body: ["status": status]) != nil
    }

    func correctLookupMeaning(bookID: String, word: String, meaning: String, sentenceIndex: String?, sentence: String?) -> [String: Any]? {
        var body: [String: Any] = [
            "word": word,
            "meaning_zh": meaning,
        ]
        if let sentenceIndex, !sentenceIndex.isEmpty {
            body["sentence_id"] = sentenceIndex
        }
        if let sentence, !sentence.isEmpty {
            body["sentence"] = sentence
        }
        return request(method: "POST", path: "/books/\(bookID)/lookup-corrections", body: body) as? [String: Any]
    }

    func createLookupEvent(bookID: String, surface: String, lemma: String?, sentenceIndex: String?, sentence: String?) {
        var context: [String: Any] = [:]
        if let sentenceIndex, !sentenceIndex.isEmpty {
            context["sentenceIndex"] = sentenceIndex
        }
        if let sentence, !sentence.isEmpty {
            context["sentence"] = sentence
        }
        var body: [String: Any] = [
            "surface": surface,
            "event_kind": "lookup",
            "context": context,
        ]
        if let lemma, !lemma.isEmpty {
            body["lemma"] = lemma
        }
        _ = request(method: "POST", path: "/books/\(bookID)/lookup-events", body: body)
    }

    private func request(method: String, path: String, body: [String: Any]?) -> Any? {
        let url: URL
        if path.contains("?"), let fullURL = URL(string: baseURL.absoluteString + path) {
            url = fullURL
        } else {
            url = baseURL.appendingPathComponent(String(path.dropFirst()))
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = timeout
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        }

        let semaphore = DispatchSemaphore(value: 0)
        var output: Any?
        let task = URLSession.shared.dataTask(with: request) { data, response, _ in
            defer { semaphore.signal() }
            guard let http = response as? HTTPURLResponse,
                  (200..<300).contains(http.statusCode),
                  let data,
                  !data.isEmpty
            else {
                return
            }
            output = try? JSONSerialization.jsonObject(with: data)
        }
        task.resume()
        _ = semaphore.wait(timeout: .now() + timeout + 0.5)
        return output
    }
}

private struct CognitiveDashboardDraftRow {
    let candidateID: String
    let draftID: String
    let status: String
    let bookTitle: String
    let qualityStatus: String
    let qualityScore: String
    let modelID: String
    let warnings: [String]
    let blockingReasons: [String]
    let draftPath: String

    init(payload: [String: Any]) {
        candidateID = Self.text(payload["candidate_intake_id"])
        draftID = Self.text(payload["draft_id"])
        status = Self.text(payload["status"])
        let title = Self.text(payload["book_title"])
        bookTitle = title.isEmpty ? (candidateID.isEmpty ? draftID : candidateID) : title
        qualityStatus = Self.text(payload["quality_status"])
        qualityScore = Self.text(payload["quality_score"])
        modelID = Self.text(payload["model_id"])
        warnings = Self.stringList(payload["warnings"])
        blockingReasons = Self.stringList(payload["blocking_reasons"])
        draftPath = Self.text(payload["draft_path"])
    }

    var statusTitle: String {
        switch status {
        case "ready_to_approve": return "可批准"
        case "needs_review": return "待审"
        case "blocked": return "阻塞"
        case "already_promoted": return "已入库"
        default: return status.isEmpty ? "未知" : status
        }
    }

    var qualityTitle: String {
        let score = qualityScore.isEmpty ? "-" : qualityScore
        return "\(qualityStatus.isEmpty ? "unknown" : qualityStatus) / \(score)"
    }

    var issueTitle: String {
        let issues = blockingReasons + warnings
        return issues.isEmpty ? "无" : issues.prefix(3).joined(separator: "；")
    }

    private static func text(_ value: Any?) -> String {
        if let text = value as? String {
            return text
        }
        if let number = value as? NSNumber {
            return number.stringValue
        }
        if let value {
            return "\(value)"
        }
        return ""
    }

    private static func stringList(_ value: Any?) -> [String] {
        guard let values = value as? [Any] else {
            return []
        }
        return values.map { text($0) }.filter { !$0.isEmpty }
    }
}

private struct CognitiveDashboardHistoryRow {
    let status: String
    let approved: String
    let dryRun: String
    let selectedCount: String
    let rebuildOK: String
    let qualityOK: String
    let qualitySkipped: String
    let reportPath: String
    let rollbackPath: String

    init(payload: [String: Any]) {
        status = Self.text(payload["status"])
        approved = Self.text(payload["approved"])
        dryRun = Self.text(payload["dry_run"])
        selectedCount = Self.text(payload["selected_count"])
        reportPath = Self.text(payload["report_path"])
        rollbackPath = Self.text(payload["rollback_manifest_path"])
        let rebuild = payload["active_pack_rebuild"] as? [String: Any] ?? [:]
        let quality = payload["quality_gate"] as? [String: Any] ?? [:]
        rebuildOK = Self.text(rebuild["ok"])
        qualityOK = Self.text(quality["ok"])
        qualitySkipped = Self.text(quality["skipped"])
    }

    var summary: String {
        [
            "status=\(status.isEmpty ? "unknown" : status)",
            "approved=\(approved.isEmpty ? "-" : approved)",
            "dry_run=\(dryRun.isEmpty ? "-" : dryRun)",
            "selected=\(selectedCount.isEmpty ? "0" : selectedCount)",
            "rebuild=\(rebuildOK.isEmpty ? "-" : rebuildOK)",
            "quality=\(qualityOK.isEmpty ? "-" : qualityOK)",
            "skipped=\(qualitySkipped.isEmpty ? "-" : qualitySkipped)",
            "rollback=\(rollbackPath.isEmpty ? "-" : rollbackPath)",
        ].joined(separator: " · ")
    }

    private static func text(_ value: Any?) -> String {
        if let text = value as? String {
            return text
        }
        if let number = value as? NSNumber {
            return number.stringValue
        }
        if let value {
            return "\(value)"
        }
        return ""
    }
}

private final class CognitiveDashboardWindowController: NSWindowController, NSTableViewDataSource, NSTableViewDelegate {
    private let dashboard: [String: Any]
    private let openPath: (String) -> Void
    private let allRows: [CognitiveDashboardDraftRow]
    private var filteredRows: [CognitiveDashboardDraftRow]
    private let historyRows: [CognitiveDashboardHistoryRow]
    private let tableView = NSTableView()
    private let statusFilterControl = NSSegmentedControl(labels: ["全部", "可批准", "待审", "阻塞", "已入库"], trackingMode: .selectOne, target: nil, action: nil)
    private let summaryLabel = NSTextField(labelWithString: "")
    private let approvalHistoryTextView = NSTextView()
    private let dashboardMarkdownPath: String

    init(dashboard: [String: Any], openPath: @escaping (String) -> Void) {
        self.dashboard = dashboard
        self.openPath = openPath
        let items = dashboard["items"] as? [[String: Any]] ?? []
        self.allRows = items.map(CognitiveDashboardDraftRow.init(payload:))
        self.filteredRows = self.allRows
        let history = dashboard["approval_history"] as? [[String: Any]] ?? []
        self.historyRows = history.map(CognitiveDashboardHistoryRow.init(payload:))
        self.dashboardMarkdownPath = Self.text(dashboard["markdown_path"])

        let window = NSWindow(
            contentRect: NSRect(x: 120, y: 120, width: 980, height: 620),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "认知审核台"
        super.init(window: window)
        window.contentView = makeContentView()
        updateSummary()
        updateApprovalHistory()
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    private func makeContentView() -> NSView {
        let root = NSView()
        root.wantsLayer = true
        root.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor

        let title = NSTextField(labelWithString: "认知审核台")
        title.font = NSFont(name: "Microsoft YaHei", size: 18) ?? NSFont.systemFont(ofSize: 18, weight: .semibold)

        summaryLabel.font = NSFont(name: "Microsoft YaHei", size: 12) ?? NSFont.systemFont(ofSize: 12)
        summaryLabel.textColor = .secondaryLabelColor
        summaryLabel.lineBreakMode = .byTruncatingTail

        statusFilterControl.target = self
        statusFilterControl.action = #selector(filterChanged(_:))
        statusFilterControl.selectedSegment = 0

        tableView.delegate = self
        tableView.dataSource = self
        tableView.rowHeight = 42
        tableView.usesAlternatingRowBackgroundColors = true
        addColumn(identifier: "status", title: "状态", width: 86)
        addColumn(identifier: "title", title: "书/候选", width: 260)
        addColumn(identifier: "quality", title: "质量", width: 120)
        addColumn(identifier: "model", title: "模型", width: 180)
        addColumn(identifier: "issues", title: "问题", width: 300)

        let tableScroll = NSScrollView()
        tableScroll.documentView = tableView
        tableScroll.hasVerticalScroller = true
        tableScroll.borderType = .bezelBorder

        let historyTitle = NSTextField(labelWithString: "审批历史")
        historyTitle.font = NSFont(name: "Microsoft YaHei", size: 13) ?? NSFont.systemFont(ofSize: 13, weight: .semibold)

        approvalHistoryTextView.isEditable = false
        approvalHistoryTextView.isSelectable = true
        approvalHistoryTextView.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        approvalHistoryTextView.textContainerInset = NSSize(width: 8, height: 8)

        let historyScroll = NSScrollView()
        historyScroll.documentView = approvalHistoryTextView
        historyScroll.hasVerticalScroller = true
        historyScroll.borderType = .bezelBorder

        let openMarkdownButton = NSButton(title: "打开Markdown仪表盘", target: self, action: #selector(openMarkdownDashboard(_:)))
        openMarkdownButton.bezelStyle = .rounded

        let openDraftButton = NSButton(title: "打开所选草稿", target: self, action: #selector(openSelectedDraft(_:)))
        openDraftButton.bezelStyle = .rounded

        let closeButton = NSButton(title: "关闭", target: self, action: #selector(closeWindow(_:)))
        closeButton.bezelStyle = .rounded

        let topStack = NSStackView(views: [title, NSView(), statusFilterControl])
        topStack.orientation = .horizontal
        topStack.alignment = .centerY
        topStack.spacing = 12

        let buttonStack = NSStackView(views: [openMarkdownButton, openDraftButton, NSView(), closeButton])
        buttonStack.orientation = .horizontal
        buttonStack.alignment = .centerY
        buttonStack.spacing = 10

        let stack = NSStackView(views: [topStack, summaryLabel, tableScroll, historyTitle, historyScroll, buttonStack])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 10

        [stack, topStack, summaryLabel, tableScroll, historyTitle, historyScroll, buttonStack].forEach {
            $0.translatesAutoresizingMaskIntoConstraints = false
        }
        root.addSubview(stack)

        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: root.topAnchor, constant: 16),
            stack.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 16),
            stack.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -16),
            stack.bottomAnchor.constraint(equalTo: root.bottomAnchor, constant: -16),
            topStack.widthAnchor.constraint(equalTo: stack.widthAnchor),
            summaryLabel.widthAnchor.constraint(equalTo: stack.widthAnchor),
            tableScroll.widthAnchor.constraint(equalTo: stack.widthAnchor),
            tableScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 280),
            historyTitle.widthAnchor.constraint(equalTo: stack.widthAnchor),
            historyScroll.widthAnchor.constraint(equalTo: stack.widthAnchor),
            historyScroll.heightAnchor.constraint(equalToConstant: 120),
            buttonStack.widthAnchor.constraint(equalTo: stack.widthAnchor),
        ])

        return root
    }

    private func addColumn(identifier: String, title: String, width: CGFloat) {
        let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(identifier))
        column.title = title
        column.width = width
        column.minWidth = 72
        column.resizingMask = .autoresizingMask
        tableView.addTableColumn(column)
    }

    private func updateSummary() {
        let counts = dashboard["counts"] as? [String: Any] ?? [:]
        let ready = Self.text(counts["ready_to_approve"])
        let needsReview = Self.text(counts["needs_review"])
        let blocked = Self.text(counts["blocked"])
        let alreadyPromoted = Self.text(counts["already_promoted"])
        let total = Self.text(dashboard["draft_count"])
        summaryLabel.stringValue = "草稿 \(total.isEmpty ? "0" : total) · 可批准 \(ready.isEmpty ? "0" : ready) · 待审 \(needsReview.isEmpty ? "0" : needsReview) · 阻塞 \(blocked.isEmpty ? "0" : blocked) · 已入库 \(alreadyPromoted.isEmpty ? "0" : alreadyPromoted)"
    }

    private func updateApprovalHistory() {
        guard !historyRows.isEmpty else {
            approvalHistoryTextView.string = "暂无审批历史"
            return
        }
        approvalHistoryTextView.string = historyRows.prefix(12).map { $0.summary }.joined(separator: "\n")
    }

    @objc private func filterChanged(_ sender: NSSegmentedControl) {
        let selectedStatus: String?
        switch sender.selectedSegment {
        case 1: selectedStatus = "ready_to_approve"
        case 2: selectedStatus = "needs_review"
        case 3: selectedStatus = "blocked"
        case 4: selectedStatus = "already_promoted"
        default: selectedStatus = nil
        }
        if let selectedStatus {
            filteredRows = allRows.filter { $0.status == selectedStatus }
        } else {
            filteredRows = allRows
        }
        tableView.reloadData()
    }

    @objc private func openMarkdownDashboard(_ sender: Any?) {
        guard !dashboardMarkdownPath.isEmpty else {
            NSSound.beep()
            return
        }
        openPath(dashboardMarkdownPath)
    }

    @objc private func openSelectedDraft(_ sender: Any?) {
        let row = tableView.selectedRow
        guard filteredRows.indices.contains(row),
              !filteredRows[row].draftPath.isEmpty
        else {
            NSSound.beep()
            return
        }
        openPath(filteredRows[row].draftPath)
    }

    @objc private func closeWindow(_ sender: Any?) {
        close()
    }

    func numberOfRows(in tableView: NSTableView) -> Int {
        filteredRows.count
    }

    func tableView(_ tableView: NSTableView, heightOfRow row: Int) -> CGFloat {
        42
    }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        guard filteredRows.indices.contains(row),
              let identifier = tableColumn?.identifier.rawValue
        else {
            return nil
        }
        let item = filteredRows[row]
        let value: String
        switch identifier {
        case "status": value = item.statusTitle
        case "title": value = item.bookTitle
        case "quality": value = item.qualityTitle
        case "model": value = item.modelID.isEmpty ? "-" : item.modelID
        case "issues": value = item.issueTitle
        default: value = ""
        }
        let label = NSTextField(labelWithString: value)
        label.font = NSFont(name: "Microsoft YaHei", size: 12) ?? NSFont.systemFont(ofSize: 12)
        label.lineBreakMode = .byTruncatingTail
        label.maximumNumberOfLines = identifier == "issues" ? 2 : 1
        if identifier == "status" {
            label.textColor = statusColor(for: item.status)
        }
        return label
    }

    private func statusColor(for status: String) -> NSColor {
        switch status {
        case "ready_to_approve": return .systemGreen
        case "needs_review": return .systemOrange
        case "blocked": return .systemRed
        case "already_promoted": return .systemBlue
        default: return .secondaryLabelColor
        }
    }

    private static func text(_ value: Any?) -> String {
        if let text = value as? String {
            return text
        }
        if let number = value as? NSNumber {
            return number.stringValue
        }
        if let value {
            return "\(value)"
        }
        return ""
    }
}

private final class RuntimeEnvironmentWindowController: NSWindowController {
    private let loadPreflightReport: () -> [String: Any]?
    private let saveFunASRPaths: (String, String) -> Bool
    private let saveSpeechProvider: (SpeechTranscriptionProvider) -> Void
    private let openPath: (String) -> Void
    private let summaryLabel = NSTextField(labelWithString: "等待检查")
    private let detailTextView = NSTextView()
    private let guideTextView = NSTextView()
    private let speechProviderPopup = NSPopUpButton(frame: .zero, pullsDown: false)
    private let funASRPythonField = NSTextField(string: "")
    private let funASRWorkerField = NSTextField(string: "")
    private var latestReportPath = ""
    private var latestMarkdownPath = ""
    private var latestRuntimeConfigPath = ""
    private var latestGuideText = ""

    init(
        funasrPython: String,
        funasrWorker: String,
        speechProvider: SpeechTranscriptionProvider,
        loadPreflightReport: @escaping () -> [String: Any]?,
        saveFunASRPaths: @escaping (String, String) -> Bool,
        saveSpeechProvider: @escaping (SpeechTranscriptionProvider) -> Void,
        openPath: @escaping (String) -> Void
    ) {
        self.loadPreflightReport = loadPreflightReport
        self.saveFunASRPaths = saveFunASRPaths
        self.saveSpeechProvider = saveSpeechProvider
        self.openPath = openPath
        funASRPythonField.stringValue = funasrPython
        funASRWorkerField.stringValue = funasrWorker
        speechProviderPopup.addItems(withTitles: [SpeechTranscriptionProvider.funASR.title, SpeechTranscriptionProvider.appleSpeech.title])
        speechProviderPopup.selectItem(withTitle: speechProvider.title)

        let window = NSWindow(
            contentRect: NSRect(x: 140, y: 120, width: 820, height: 680),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "运行环境"
        super.init(window: window)
        window.contentView = makeContentView()
        refreshPreflight(nil)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    private func makeContentView() -> NSView {
        let root = NSView()
        root.wantsLayer = true
        root.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor

        let title = NSTextField(labelWithString: "运行环境")
        title.font = NSFont(name: "Microsoft YaHei", size: 18) ?? NSFont.systemFont(ofSize: 18, weight: .semibold)

        summaryLabel.font = NSFont(name: "Microsoft YaHei", size: 12) ?? NSFont.systemFont(ofSize: 12)
        summaryLabel.textColor = .secondaryLabelColor
        summaryLabel.lineBreakMode = .byTruncatingTail

        funASRPythonField.placeholderString = "FunASR Python 路径"
        funASRWorkerField.placeholderString = "funasr_worker.py 路径"

        let pythonRow = labeledRow(label: "FunASR Python", field: funASRPythonField)
        let workerRow = labeledRow(label: "FunASR Worker", field: funASRWorkerField)
        let providerRow = popupRow(label: "语音识别", popup: speechProviderPopup)

        detailTextView.isEditable = false
        detailTextView.isSelectable = true
        detailTextView.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        detailTextView.textContainerInset = NSSize(width: 8, height: 8)

        let detailScroll = NSScrollView()
        detailScroll.documentView = detailTextView
        detailScroll.hasVerticalScroller = true
        detailScroll.borderType = .bezelBorder

        let guideTitle = NSTextField(labelWithString: "首启修复引导")
        guideTitle.font = NSFont(name: "Microsoft YaHei", size: 13) ?? NSFont.systemFont(ofSize: 13, weight: .semibold)

        guideTextView.isEditable = false
        guideTextView.isSelectable = true
        guideTextView.font = NSFont(name: "Microsoft YaHei", size: 12) ?? NSFont.systemFont(ofSize: 12)
        guideTextView.textContainerInset = NSSize(width: 8, height: 8)
        guideTextView.string = "如果运行环境不可用，会在这里显示修复步骤。"

        let guideScroll = NSScrollView()
        guideScroll.documentView = guideTextView
        guideScroll.hasVerticalScroller = true
        guideScroll.borderType = .bezelBorder

        let refreshButton = NSButton(title: "重新检查", target: self, action: #selector(refreshPreflight(_:)))
        refreshButton.bezelStyle = .rounded

        let saveButton = NSButton(title: "保存", target: self, action: #selector(saveRuntimeSettings(_:)))
        saveButton.bezelStyle = .rounded

        let openReportButton = NSButton(title: "打开预检报告", target: self, action: #selector(openPreflightReport(_:)))
        openReportButton.bezelStyle = .rounded

        let copyGuideButton = NSButton(title: "复制修复指引", target: self, action: #selector(copyFirstRunGuide(_:)))
        copyGuideButton.bezelStyle = .rounded

        let openConfigButton = NSButton(title: "打开配置目录", target: self, action: #selector(openRuntimeConfigFolder(_:)))
        openConfigButton.bezelStyle = .rounded

        let closeButton = NSButton(title: "关闭", target: self, action: #selector(closeWindow(_:)))
        closeButton.bezelStyle = .rounded

        let topStack = NSStackView(views: [title, NSView(), refreshButton])
        topStack.orientation = .horizontal
        topStack.alignment = .centerY
        topStack.spacing = 12

        let buttonStack = NSStackView(views: [saveButton, openReportButton, copyGuideButton, openConfigButton, NSView(), closeButton])
        buttonStack.orientation = .horizontal
        buttonStack.alignment = .centerY
        buttonStack.spacing = 10

        let stack = NSStackView(views: [topStack, summaryLabel, providerRow, pythonRow, workerRow, detailScroll, guideTitle, guideScroll, buttonStack])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 10

        [stack, topStack, summaryLabel, providerRow, pythonRow, workerRow, detailScroll, guideTitle, guideScroll, buttonStack].forEach {
            $0.translatesAutoresizingMaskIntoConstraints = false
        }
        root.addSubview(stack)

        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: root.topAnchor, constant: 16),
            stack.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 16),
            stack.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -16),
            stack.bottomAnchor.constraint(equalTo: root.bottomAnchor, constant: -16),
            topStack.widthAnchor.constraint(equalTo: stack.widthAnchor),
            summaryLabel.widthAnchor.constraint(equalTo: stack.widthAnchor),
            providerRow.widthAnchor.constraint(equalTo: stack.widthAnchor),
            pythonRow.widthAnchor.constraint(equalTo: stack.widthAnchor),
            workerRow.widthAnchor.constraint(equalTo: stack.widthAnchor),
            detailScroll.widthAnchor.constraint(equalTo: stack.widthAnchor),
            detailScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 250),
            guideTitle.widthAnchor.constraint(equalTo: stack.widthAnchor),
            guideScroll.widthAnchor.constraint(equalTo: stack.widthAnchor),
            guideScroll.heightAnchor.constraint(equalToConstant: 130),
            buttonStack.widthAnchor.constraint(equalTo: stack.widthAnchor),
        ])

        return root
    }

    private func labeledRow(label: String, field: NSTextField) -> NSStackView {
        let title = NSTextField(labelWithString: label)
        title.font = NSFont(name: "Microsoft YaHei", size: 12) ?? NSFont.systemFont(ofSize: 12, weight: .medium)
        title.textColor = .secondaryLabelColor
        let stack = NSStackView(views: [title, field])
        stack.orientation = .horizontal
        stack.alignment = .centerY
        stack.spacing = 10
        title.translatesAutoresizingMaskIntoConstraints = false
        field.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            title.widthAnchor.constraint(equalToConstant: 116),
            field.heightAnchor.constraint(equalToConstant: 24),
        ])
        return stack
    }

    private func popupRow(label: String, popup: NSPopUpButton) -> NSStackView {
        let title = NSTextField(labelWithString: label)
        title.font = NSFont(name: "Microsoft YaHei", size: 12) ?? NSFont.systemFont(ofSize: 12, weight: .medium)
        title.textColor = .secondaryLabelColor
        let stack = NSStackView(views: [title, popup])
        stack.orientation = .horizontal
        stack.alignment = .centerY
        stack.spacing = 10
        title.translatesAutoresizingMaskIntoConstraints = false
        popup.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            title.widthAnchor.constraint(equalToConstant: 116),
            popup.widthAnchor.constraint(equalToConstant: 120),
            popup.heightAnchor.constraint(equalToConstant: 26),
        ])
        return stack
    }

    @objc private func refreshPreflight(_ sender: Any?) {
        summaryLabel.stringValue = "正在检查运行环境..."
        detailTextView.string = "正在运行 first-run preflight..."
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let report = self.loadPreflightReport()
            DispatchQueue.main.async {
                guard let report else {
                    self.summaryLabel.stringValue = "运行环境检查失败"
                    self.detailTextView.string = "无法运行 first-run preflight。"
                    return
                }
                self.applyPreflightReport(report)
            }
        }
    }

    @objc private func saveRuntimeSettings(_ sender: Any?) {
        let provider = SpeechTranscriptionProvider.fromTitle(speechProviderPopup.titleOfSelectedItem)
        saveSpeechProvider(provider)
        guard provider == .funASR else {
            summaryLabel.stringValue = "已保存"
            return
        }

        let python = funASRPythonField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        let worker = funASRWorkerField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !python.isEmpty, !worker.isEmpty else {
            summaryLabel.stringValue = "FunASR 路径不能为空"
            NSSound.beep()
            return
        }
        if saveFunASRPaths(python, worker) {
            summaryLabel.stringValue = "语音路径已保存，正在重新检查..."
            refreshPreflight(nil)
        } else {
            summaryLabel.stringValue = "语音路径保存失败"
            NSSound.beep()
        }
    }

    @objc private func openPreflightReport(_ sender: Any?) {
        let path = latestMarkdownPath.isEmpty ? latestReportPath : latestMarkdownPath
        guard !path.isEmpty else {
            NSSound.beep()
            return
        }
        openPath(path)
    }

    @objc private func copyFirstRunGuide(_ sender: Any?) {
        guard !latestGuideText.isEmpty else {
            NSSound.beep()
            return
        }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(latestGuideText, forType: .string)
        summaryLabel.stringValue = "修复指引已复制"
    }

    @objc private func openRuntimeConfigFolder(_ sender: Any?) {
        let path = latestRuntimeConfigPath.isEmpty
            ? (NSHomeDirectory() as NSString).appendingPathComponent("Library/Application Support/SentenceReader/config")
            : (latestRuntimeConfigPath as NSString).deletingLastPathComponent
        openPath(path)
    }

    @objc private func closeWindow(_ sender: Any?) {
        close()
    }

    private func applyPreflightReport(_ report: [String: Any]) {
        latestReportPath = Self.text(report["_report_path"])
        latestMarkdownPath = Self.text(report["_markdown_path"])
        latestRuntimeConfigPath = Self.text((report["runtime_config"] as? [String: Any] ?? [:])["path"])
        let ready = Self.bool(report["first_run_ready"])
        summaryLabel.stringValue = ready ? "运行环境可用" : "运行环境需要处理"
        summaryLabel.textColor = ready ? .systemGreen : .systemOrange
        detailTextView.string = Self.formatPreflightReport(report)
        latestGuideText = Self.formatFirstRunGuide(report)
        guideTextView.string = latestGuideText
    }

    private static func formatPreflightReport(_ report: [String: Any]) -> String {
        let postgres = report["postgres"] as? [String: Any] ?? [:]
        let readerAPI = report["reader_api"] as? [String: Any] ?? [:]
        let health = readerAPI["health"] as? [String: Any] ?? [:]
        let bootstrap = report["runtime_bootstrap"] as? [String: Any] ?? [:]
        let bootstrapPayload = bootstrap["payload"] as? [String: Any] ?? [:]
        let funasr = report["funasr"] as? [String: Any] ?? [:]
        let runtimeConfig = report["runtime_config"] as? [String: Any] ?? [:]
        let actions = report["next_actions"] as? [Any] ?? []

        var lines = [
            "first_run_ready: \(text(report["first_run_ready"]))",
            "PostgreSQL: \(text(postgres["decision"])) · server=\(text(postgres["server_ready"])) · tools=\(text(postgres["tools_ready"]))",
            "Reader API: health=\(text(health["ok"])) · database=\(text(readerAPI["database_url"]))",
            "Runtime bootstrap: ok=\(text(bootstrap["ok"])) · python_ready=\(text(bootstrapPayload["python_ready"])) · startup_ready=\(text(bootstrapPayload["startup_ready"]))",
            "FunASR: \(text(funasr["decision"])) · ready=\(text(funasr["ready"]))",
            "FunASR Python: \(text(funasr["python"]))",
            "FunASR Worker: \(text(funasr["worker"]))",
            "Runtime config: \(text(runtimeConfig["path"]))",
            "Report: \(text(report["_report_path"]))",
            "Markdown: \(text(report["_markdown_path"]))",
            "",
            "Next actions:",
        ]
        if actions.isEmpty {
            lines.append("- none")
        } else {
            lines.append(contentsOf: actions.map { "- \(text($0))" })
        }
        return lines.joined(separator: "\n")
    }

    private static func formatFirstRunGuide(_ report: [String: Any]) -> String {
        let ready = bool(report["first_run_ready"])
        let postgres = report["postgres"] as? [String: Any] ?? [:]
        let bootstrap = report["runtime_bootstrap"] as? [String: Any] ?? [:]
        let bootstrapPayload = bootstrap["payload"] as? [String: Any] ?? [:]
        let funasr = report["funasr"] as? [String: Any] ?? [:]
        let actions = (report["next_actions"] as? [Any] ?? []).map { text($0) }.filter { !$0.isEmpty && $0 != "-" }

        if ready {
            return [
                "当前运行环境可用。",
                "PostgreSQL、Reader API、Runtime bootstrap、FunASR 配置都已通过预检。",
                "如果后续语音转写失败，优先检查上面的 FunASR Python 和 Worker 路径。",
            ].joined(separator: "\n")
        }

        var lines = [
            "环境未准备好。按下面顺序处理，不要做破坏性数据库操作。",
            "",
        ]
        if !(bool(postgres["server_ready"]) || bool(postgres["tools_ready"])) {
            lines.append("1. PostgreSQL：安装或启动 Postgres.app，或者设置 POSTGRES_APP_BIN 指向包含 postgres/pg_ctl/initdb/psql 的 bin 目录。")
        } else {
            lines.append("1. PostgreSQL：已有服务器或 Postgres.app 工具可用。")
        }

        if !bool(bootstrapPayload["python_ready"]) || !bool(bootstrapPayload["startup_ready"]) {
            lines.append("2. Runtime Python：在终端显式运行启动修复，不会自动安装。需要时使用 SENTENCE_READER_BOOTSTRAP_REPAIR=1，安装依赖还要额外设置 SENTENCE_READER_BOOTSTRAP_INSTALL_DEPS=1。")
        } else {
            lines.append("2. Runtime Python：Reader API Python 运行时可用。")
        }

        if !bool(funasr["ready"]) {
            lines.append("3. FunASR：填写 FunASR Python 和 funasr_worker.py 路径后点“保存语音路径”。缺 FunASR 不阻止阅读，只会退回系统语音识别。")
        } else {
            lines.append("3. FunASR：本机语音转写路径可用。")
        }

        if !actions.isEmpty {
            lines.append("")
            lines.append("预检给出的 next_actions：")
            lines.append(contentsOf: actions.map { "- \($0)" })
        }
        return lines.joined(separator: "\n")
    }

    private static func bool(_ value: Any?) -> Bool {
        if let value = value as? Bool {
            return value
        }
        if let value = value as? NSNumber {
            return value.boolValue
        }
        return false
    }

    private static func text(_ value: Any?) -> String {
        if let text = value as? String {
            return text
        }
        if let number = value as? NSNumber {
            return number.stringValue
        }
        if let bool = value as? Bool {
            return bool ? "true" : "false"
        }
        if let value {
            return "\(value)"
        }
        return "-"
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKScriptMessageHandler, WKNavigationDelegate, WKUIDelegate, NSTableViewDataSource, NSTableViewDelegate, NSSearchFieldDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var notesRail: NSVisualEffectView!
    private var notesRailWidthConstraint: NSLayoutConstraint!
    private var notesRailToggleButton: NSButton!
    private var notesTableView: NSTableView!
    private var notesSearchField: NSSearchField!
    private var notesFilterControl: NSSegmentedControl!
    private var notesSummaryLabel: NSTextField!
    private var cognitiveDashboardWindowController: CognitiveDashboardWindowController?
    private var runtimeEnvironmentWindowController: RuntimeEnvironmentWindowController?
    private var libraryWindowController: NSWindowController?
    private var libraryWindowUsesWebUI = false
    private var mainRootView: NSView?
    private var libraryHomeWebView: WKWebView?
    private var mainTitlebarDragView: NSView?
    private var libraryTableView: NSTableView?
    private var librarySummaryLabel: NSTextField?
    private var didShowFirstRunGuide = false
    private var undoEventMonitor: Any?
    private var bookRootURL: URL?
    private var chapters: [URL] = []
    private var chapterTitles: [String] = []
    private var tocEntries: [TocEntry] = []
    private var allNoteRows: [NoteRow] = []
    private var visibleNoteRows: [NoteRow] = []
    private var currentChapterIndex = 0
    private var pendingInitialPage: InitialPage = .start
    private var suppressReadingPositionSave = true
    private var noteSpeechController: NoteSpeechController?
    private var readerAPIProcess: Process?
    private var funASRServerProcess: Process?
    private var funASRWarmupStarted = false
    private var readerHeaderView: NSView?
    private var readerFooterView: NSView?
    private var readerChromeEventMonitor: Any?
    private var readerChromeAutoHideWorkItem: DispatchWorkItem?
    private var readerChromeVisible = false
    private var readerAPIAvailable = false
    private var readerAPILANModeEnabled = false
    private var readerBookID: String?
    private var redAnnotationIDs: [String: String] = [:]
    private var pendingNoteJumpIndex: String?
    private var pendingExternalEPUBURLs: [URL] = []
    private var didFinishLaunching = false
    private var bookEntries: [BookEntry] = []
    private var currentBookEntry: BookEntry?
    private var readerSettings = ReaderSettings.defaultSettings
    private let readerAPI = ReaderAPIClient()
    private let speechSynthesizer = AVSpeechSynthesizer()
    private var lookupAudioPlayer: AVPlayer?
    private var lookupAudioDataPlayer: AVAudioPlayer?
    private var lookupSoundPlayer: NSSound?
    private var lookupAudioProcess: Process?
    private var lookupActionTargets: [LookupActionTarget] = []
    private let readingPositionKeyPrefix = "SentenceReader.lastReadingPosition.v1"
    private let bookLibraryKey = "SentenceReader.bookLibrary.v1"
    private let readerSettingsKey = "SentenceReader.readerSettings.v1"
    private let funASRPythonDefaultsKey = "SentenceReader.funASRPythonPath.v1"
    private let funASRWorkerDefaultsKey = "SentenceReader.funASRWorkerPath.v1"
    private let funASRPythonDefaultPath = (NSHomeDirectory() as NSString).appendingPathComponent("Library/Application Support/SentenceReader/FunASR/.venv/bin/python")
    private let funASRWorkerDefaultPath = (NSHomeDirectory() as NSString).appendingPathComponent("Library/Application Support/SentenceReader/FunASR/funasr_worker.py")
    private let funASRServerPort = 18081
    private let bookTitleLabel = NSTextField(labelWithString: "Click 示例书")
    private let openBookButton = NSButton(title: "打开", target: nil, action: nil)
    private let libraryButton = NSButton(title: "书库", target: nil, action: nil)
    private let bookSwitcherButton = NSButton(title: "书籍", target: nil, action: nil)
    private let exportButton = NSButton(title: "导出", target: nil, action: nil)
    private let syncButton = NSButton(title: "同步", target: nil, action: nil)
    private let cognitiveButton = NSButton(title: "认知", target: nil, action: nil)
    private let runtimeEnvironmentButton = NSButton(title: "环境", target: nil, action: nil)
    private let iPadLANButton = NSButton(title: "iPad", target: nil, action: nil)
    private let settingsButton = NSButton(title: "设置", target: nil, action: nil)
    private let contentsButton = NSButton(title: "目录", target: nil, action: nil)
    private let notesButton = NSButton(title: "笔记", target: nil, action: nil)
    private let readerMoreButton = NSButton(title: "更多", target: nil, action: nil)
    private let statusLabel = NSTextField(labelWithString: "英文单击查词 · 双击备注 · 右键/双指点按整句标红")
    private let redLabel = NSTextField(labelWithString: "红标 0")
    private let notesFilterLabels = ["全部", "备注", "红标"]

    private enum InitialPage {
        case start
        case end
        case position(pageIndex: Int, pageRatio: Double, totalPages: Int)
    }

    private struct TocEntry {
        let title: String
        let chapterIndex: Int
        let level: Int
    }

    private struct ReadingPosition: Codable {
        let chapterRelativePath: String
        let chapterIndex: Int
        let pageIndex: Int
        let totalPages: Int
        let pageRatio: Double
        let updatedAt: TimeInterval
    }

    private struct BookEntry: Codable, Equatable {
        let title: String
        let author: String?
        let bookHash: String
        let epubPath: String
        let bookRootPath: String
        let isBundled: Bool
    }

    private struct ReaderSettings: Codable {
        var fontSize: Int
        var lineHeight: Double
        var marginX: Int
        var theme: String

        static let defaultSettings = ReaderSettings(fontSize: 18, lineHeight: 1.58, marginX: 8, theme: "dark")
    }

    private struct NoteRow {
        let id: String
        let kind: String
        let sourceText: String
        let noteText: String
        let chapterTitle: String
        let chapterLocator: String
        let sentenceIndex: String

        var isRedHighlight: Bool {
            kind == "red_highlight"
        }

        var kindTitle: String {
            isRedHighlight ? "红标" : "备注"
        }

        var primaryText: String {
            noteText.isEmpty ? sourceText : noteText
        }

        var secondaryText: String {
            sourceText
        }
    }

    private func installApplicationMenu() {
        let mainMenu = NSMenu()
        let appMenuItem = NSMenuItem()
        mainMenu.addItem(appMenuItem)

        let appMenu = NSMenu(title: "Sentence Reader")
        let quitItem = NSMenuItem(title: "退出 Sentence Reader", action: #selector(quitApplication(_:)), keyEquivalent: "q")
        quitItem.keyEquivalentModifierMask = [.command]
        quitItem.target = self
        appMenu.addItem(quitItem)

        appMenuItem.submenu = appMenu
        NSApp.mainMenu = mainMenu
    }

    @objc private func quitApplication(_ sender: Any?) {
        NSApp.terminate(sender)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        installApplicationMenu()
        readerSettings = loadReaderSettings()
        buildWindow()
        installCommandUndoMonitor()
        installReaderChromeMonitor()
        ensureReaderAPIAvailable()
        loadBundledBook()
        showMainLibrary()
        didFinishLaunching = true
        openPendingExternalEPUBs()
        NSApp.activate(ignoringOtherApps: true)
        showFirstRunGuideIfNeeded()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
            self.refreshSpeechWarmServiceForCurrentProvider()
        }
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        enqueueExternalEPUBs(urls)
    }

    func application(_ sender: NSApplication, openFiles filenames: [String]) {
        let urls = filenames.map { URL(fileURLWithPath: $0) }
        enqueueExternalEPUBs(urls)
        sender.reply(toOpenOrPrint: .success)
    }

    func application(_ sender: NSApplication, openFile filename: String) -> Bool {
        enqueueExternalEPUBs([URL(fileURLWithPath: filename)])
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationWillTerminate(_ notification: Notification) {
        if let readerChromeEventMonitor {
            NSEvent.removeMonitor(readerChromeEventMonitor)
            self.readerChromeEventMonitor = nil
        }
        stopFunASRWarmService()
    }

    private func enqueueExternalEPUBs(_ urls: [URL]) {
        let epubURLs = urls.filter { $0.pathExtension.lowercased() == "epub" }
        guard !epubURLs.isEmpty else {
            return
        }
        pendingExternalEPUBURLs.append(contentsOf: epubURLs)
        openPendingExternalEPUBs()
    }

    private func openPendingExternalEPUBs() {
        guard didFinishLaunching else {
            return
        }
        let urls = pendingExternalEPUBURLs
        pendingExternalEPUBURLs.removeAll()
        for url in urls {
            importEPUB(url)
        }
    }

    private func installReaderChromeMonitor() {
        readerChromeEventMonitor = NSEvent.addLocalMonitorForEvents(matching: [.mouseMoved, .keyDown]) { [weak self] event in
            guard let self,
                  let contentView = self.window.contentView
            else {
                return event
            }

            if event.type == .keyDown,
               event.keyCode == 53,
               self.dismissAttachedSheetIfNeeded() {
                return nil
            }

            guard event.window === self.window else {
                return event
            }

            if self.libraryHomeWebView?.isHidden == false {
                return event
            }

            if event.type == .mouseMoved {
                let point = event.locationInWindow
                let height = contentView.bounds.height
                if point.y >= height - 54 || point.y <= 44 {
                    self.revealReaderChromeTemporarily()
                }
                return event
            }

            if event.type == .keyDown,
               event.keyCode == 53,
               self.readerChromeVisible {
                self.setReaderChromeVisible(false)
                return nil
            }
            return event
        }
    }

    private func dismissAttachedSheetIfNeeded() -> Bool {
        guard let sheet = window.attachedSheet else {
            return false
        }
        window.endSheet(sheet, returnCode: .cancel)
        return true
    }

    private func revealReaderChromeTemporarily() {
        setReaderChromeVisible(true)
        scheduleReaderChromeAutoHide()
    }

    private func scheduleReaderChromeAutoHide() {
        readerChromeAutoHideWorkItem?.cancel()
        let item = DispatchWorkItem { [weak self] in
            guard let self,
                  let contentView = self.window.contentView
            else {
                return
            }

            let point = self.window.mouseLocationOutsideOfEventStream
            let height = contentView.bounds.height
            if point.y >= height - 54 || point.y <= 44 {
                self.scheduleReaderChromeAutoHide()
                return
            }
            self.setReaderChromeVisible(false)
        }
        readerChromeAutoHideWorkItem = item
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.4, execute: item)
    }

    private func setReaderChromeVisible(_ visible: Bool) {
        readerChromeVisible = visible
        [readerHeaderView, readerFooterView].forEach { view in
            view?.isHidden = !visible
            view?.alphaValue = visible ? 1 : 0
        }
    }

    private func makeNotesRail() -> NSVisualEffectView {
        let rail = NSVisualEffectView()
        rail.material = .sidebar
        rail.blendingMode = .withinWindow
        rail.state = .active
        rail.wantsLayer = true
        rail.layer?.borderColor = NSColor.white.withAlphaComponent(0.12).cgColor
        rail.layer?.borderWidth = 1

        let title = NSTextField(labelWithString: "笔记")
        title.font = NSFont(name: "Microsoft YaHei", size: 13) ?? NSFont.systemFont(ofSize: 13, weight: .semibold)
        title.textColor = .white

        let toggleButton = NSButton(title: "收起笔记", target: self, action: #selector(toggleNotesRail(_:)))
        toggleButton.bezelStyle = .rounded
        toggleButton.controlSize = .small
        toggleButton.font = NSFont(name: "Microsoft YaHei", size: 12) ?? NSFont.systemFont(ofSize: 12, weight: .medium)
        notesRailToggleButton = toggleButton

        notesSearchField = NSSearchField()
        notesSearchField.placeholderString = "搜索笔记和原句"
        notesSearchField.delegate = self

        notesFilterControl = NSSegmentedControl(labels: notesFilterLabels, trackingMode: .selectOne, target: self, action: #selector(notesFilterChanged(_:)))
        notesFilterControl.selectedSegment = 0
        notesFilterControl.controlSize = .small

        notesSummaryLabel = NSTextField(labelWithString: "0 条")
        notesSummaryLabel.font = NSFont(name: "Microsoft YaHei", size: 11) ?? NSFont.systemFont(ofSize: 11)
        notesSummaryLabel.textColor = NSColor.secondaryLabelColor

        notesTableView = NSTableView()
        notesTableView.headerView = nil
        notesTableView.rowHeight = 76
        notesTableView.delegate = self
        notesTableView.dataSource = self
        notesTableView.target = self
        notesTableView.doubleAction = #selector(openSelectedNote(_:))
        let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("note"))
        column.resizingMask = .autoresizingMask
        notesTableView.addTableColumn(column)

        let scroll = NSScrollView()
        scroll.documentView = notesTableView
        scroll.hasVerticalScroller = true
        scroll.borderType = .noBorder

        let editButton = NSButton(title: "编辑", target: self, action: #selector(editSelectedNote(_:)))
        editButton.bezelStyle = .rounded
        editButton.controlSize = .small

        let deleteButton = NSButton(title: "删除", target: self, action: #selector(deleteSelectedNote(_:)))
        deleteButton.bezelStyle = .rounded
        deleteButton.controlSize = .small

        let topStack = NSStackView(views: [title, NSView(), toggleButton])
        topStack.orientation = .horizontal
        topStack.alignment = .centerY
        topStack.spacing = 8

        let buttonStack = NSStackView(views: [editButton, deleteButton, NSView(), notesSummaryLabel])
        buttonStack.orientation = .horizontal
        buttonStack.alignment = .centerY
        buttonStack.spacing = 8

        let stack = NSStackView(views: [topStack, notesSearchField, notesFilterControl, scroll, buttonStack])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 8

        [stack, topStack, notesSearchField, notesFilterControl, scroll, buttonStack].forEach {
            $0.translatesAutoresizingMaskIntoConstraints = false
        }
        rail.addSubview(stack)

        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: rail.topAnchor, constant: 10),
            stack.leadingAnchor.constraint(equalTo: rail.leadingAnchor, constant: 10),
            stack.trailingAnchor.constraint(equalTo: rail.trailingAnchor, constant: -10),
            stack.bottomAnchor.constraint(equalTo: rail.bottomAnchor, constant: -10),

            topStack.widthAnchor.constraint(equalTo: stack.widthAnchor),
            notesSearchField.widthAnchor.constraint(equalTo: stack.widthAnchor),
            notesFilterControl.widthAnchor.constraint(equalTo: stack.widthAnchor),
            scroll.widthAnchor.constraint(equalTo: stack.widthAnchor),
            scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 260),
            buttonStack.widthAnchor.constraint(equalTo: stack.widthAnchor),
        ])

        return rail
    }

    private func configureReaderChromeButton(_ button: NSButton) {
        button.bezelStyle = .rounded
        button.controlSize = .regular
        button.font = NSFont(name: "Microsoft YaHei", size: 14) ?? NSFont.systemFont(ofSize: 14, weight: .medium)
        button.contentTintColor = .white
        button.setContentHuggingPriority(.required, for: .horizontal)
        button.widthAnchor.constraint(greaterThanOrEqualToConstant: 62).isActive = true
        button.heightAnchor.constraint(greaterThanOrEqualToConstant: 30).isActive = true
    }

    private func makeMainLibraryWebView() -> WKWebView {
        let config = WKWebViewConfiguration()
        let webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.setValue(false, forKey: "drawsBackground")
        return webView
    }

    private func mainLibraryURL() -> URL? {
        URL(string: "http://127.0.0.1:18180/library?surface=mac-app")
    }

    private func showMainLibrary() {
        guard ensureReaderAPIForLibraryUI(),
              let libraryHomeWebView,
              let url = mainLibraryURL()
        else {
            statusLabel.stringValue = "主界面需要 Reader API，当前无法打开书库"
            return
        }
        libraryHomeWebView.isHidden = false
        mainTitlebarDragView?.isHidden = false
        libraryHomeWebView.load(URLRequest(url: url))
        setReaderChromeVisible(false)
        window.title = "Click 书库"
        statusLabel.stringValue = "已进入主界面；选择书籍后会在本窗口打开正文"
    }

    private func hideMainLibraryForReading() {
        libraryHomeWebView?.isHidden = true
        mainTitlebarDragView?.isHidden = true
        window.title = "Click"
        revealReaderChromeTemporarily()
    }

    private func buildWindow() {
        let config = WKWebViewConfiguration()
        let userContent = WKUserContentController()
        userContent.addUserScript(WKUserScript(source: Self.readerScript, injectionTime: .atDocumentEnd, forMainFrameOnly: false))
        userContent.add(self, name: "sentenceReader")
        config.userContentController = userContent

        webView = WKWebView(frame: .zero, configuration: config)
        webView.setValue(false, forKey: "drawsBackground")

        let root = NSView()
        root.wantsLayer = true
        root.layer?.backgroundColor = NSColor.black.cgColor

        let header = NSView()
        header.wantsLayer = true
        header.layer?.backgroundColor = NSColor.black.cgColor

        bookTitleLabel.font = NSFont(name: "Microsoft YaHei", size: 14) ?? NSFont.systemFont(ofSize: 14, weight: .semibold)
        bookTitleLabel.textColor = .white
        bookTitleLabel.lineBreakMode = .byTruncatingTail

        openBookButton.target = self
        openBookButton.action = #selector(openBookPanel(_:))
        openBookButton.bezelStyle = .rounded
        openBookButton.controlSize = .small
        openBookButton.font = NSFont(name: "Microsoft YaHei", size: 12) ?? NSFont.systemFont(ofSize: 12, weight: .medium)
        openBookButton.contentTintColor = .white

        libraryButton.target = self
        libraryButton.action = #selector(showLibraryWindow(_:))
        libraryButton.bezelStyle = .rounded
        libraryButton.controlSize = .small
        libraryButton.font = NSFont(name: "Microsoft YaHei", size: 12) ?? NSFont.systemFont(ofSize: 12, weight: .medium)
        libraryButton.contentTintColor = .white

        bookSwitcherButton.target = self
        bookSwitcherButton.action = #selector(showBookSwitcher(_:))
        bookSwitcherButton.bezelStyle = .rounded
        bookSwitcherButton.controlSize = .small
        bookSwitcherButton.font = NSFont(name: "Microsoft YaHei", size: 12) ?? NSFont.systemFont(ofSize: 12, weight: .medium)
        bookSwitcherButton.contentTintColor = .white

        exportButton.target = self
        exportButton.action = #selector(exportCurrentBook(_:))
        exportButton.bezelStyle = .rounded
        exportButton.controlSize = .small
        exportButton.font = NSFont(name: "Microsoft YaHei", size: 12) ?? NSFont.systemFont(ofSize: 12, weight: .medium)
        exportButton.contentTintColor = .white

        syncButton.target = self
        syncButton.action = #selector(syncCurrentBook(_:))
        syncButton.bezelStyle = .rounded
        syncButton.controlSize = .small
        syncButton.font = NSFont(name: "Microsoft YaHei", size: 12) ?? NSFont.systemFont(ofSize: 12, weight: .medium)
        syncButton.contentTintColor = .white

        cognitiveButton.target = self
        cognitiveButton.action = #selector(reviewCognitiveQueue(_:))
        cognitiveButton.bezelStyle = .rounded
        cognitiveButton.controlSize = .small
        cognitiveButton.font = NSFont(name: "Microsoft YaHei", size: 12) ?? NSFont.systemFont(ofSize: 12, weight: .medium)
        cognitiveButton.contentTintColor = .white

        runtimeEnvironmentButton.target = self
        runtimeEnvironmentButton.action = #selector(showRuntimeEnvironment(_:))
        runtimeEnvironmentButton.bezelStyle = .rounded
        runtimeEnvironmentButton.controlSize = .small
        runtimeEnvironmentButton.font = NSFont(name: "Microsoft YaHei", size: 12) ?? NSFont.systemFont(ofSize: 12, weight: .medium)
        runtimeEnvironmentButton.contentTintColor = .white

        iPadLANButton.target = self
        iPadLANButton.action = #selector(showIPadLANReader(_:))
        iPadLANButton.bezelStyle = .rounded
        iPadLANButton.controlSize = .small
        iPadLANButton.font = NSFont(name: "Microsoft YaHei", size: 12) ?? NSFont.systemFont(ofSize: 12, weight: .medium)
        iPadLANButton.contentTintColor = .white

        settingsButton.target = self
        settingsButton.action = #selector(showReaderSettings(_:))
        settingsButton.bezelStyle = .rounded
        settingsButton.controlSize = .small
        settingsButton.font = NSFont(name: "Microsoft YaHei", size: 12) ?? NSFont.systemFont(ofSize: 12, weight: .medium)
        settingsButton.contentTintColor = .white

        let opened = NSTextField(labelWithString: "Opened")
        opened.font = NSFont.systemFont(ofSize: 11, weight: .medium)
        opened.textColor = NSColor.systemGreen

        contentsButton.target = self
        contentsButton.action = #selector(showContents(_:))
        contentsButton.bezelStyle = .rounded
        contentsButton.controlSize = .small
        contentsButton.font = NSFont(name: "Microsoft YaHei", size: 12) ?? NSFont.systemFont(ofSize: 12, weight: .medium)
        contentsButton.contentTintColor = .white

        notesButton.target = self
        notesButton.action = #selector(toggleNotesRail(_:))
        notesButton.bezelStyle = .rounded
        notesButton.controlSize = .small
        notesButton.font = NSFont(name: "Microsoft YaHei", size: 12) ?? NSFont.systemFont(ofSize: 12, weight: .medium)
        notesButton.contentTintColor = .white

        readerMoreButton.target = self
        readerMoreButton.action = #selector(showReaderMoreMenu(_:))
        readerMoreButton.bezelStyle = .rounded
        readerMoreButton.controlSize = .regular
        readerMoreButton.font = NSFont(name: "Microsoft YaHei", size: 14) ?? NSFont.systemFont(ofSize: 14, weight: .medium)
        readerMoreButton.contentTintColor = .white

        notesButton.title = "笔记"
        [libraryButton, contentsButton, notesButton, settingsButton, readerMoreButton].forEach {
            configureReaderChromeButton($0)
        }
        bookTitleLabel.font = NSFont(name: "Microsoft YaHei", size: 15) ?? NSFont.systemFont(ofSize: 15, weight: .semibold)

        let headerStack = NSStackView(views: [libraryButton, bookTitleLabel, NSView(), contentsButton, notesButton, settingsButton, readerMoreButton])
        headerStack.orientation = .horizontal
        headerStack.alignment = .centerY
        headerStack.spacing = 14

        let footer = NSVisualEffectView()
        footer.material = .hudWindow
        footer.blendingMode = .withinWindow
        footer.state = .active

        statusLabel.font = NSFont(name: "Microsoft YaHei", size: 11) ?? NSFont.systemFont(ofSize: 11)
        statusLabel.textColor = NSColor.white.withAlphaComponent(0.80)
        statusLabel.lineBreakMode = .byTruncatingTail
        redLabel.font = NSFont(name: "Microsoft YaHei", size: 11) ?? NSFont.systemFont(ofSize: 11)
        redLabel.textColor = NSColor.white.withAlphaComponent(0.80)

        let footerStack = NSStackView(views: [statusLabel, NSView(), redLabel])
        footerStack.orientation = .horizontal
        footerStack.alignment = .centerY
        footerStack.spacing = 10

        notesRail = makeNotesRail()

        let libraryWebView = makeMainLibraryWebView()
        let titlebarDragView = WindowDragView()
        titlebarDragView.wantsLayer = true
        titlebarDragView.layer?.backgroundColor = NSColor.black.cgColor

        [header, headerStack, webView, notesRail, footer, footerStack, libraryWebView, titlebarDragView].forEach {
            $0.translatesAutoresizingMaskIntoConstraints = false
        }
        notesRailWidthConstraint = notesRail.widthAnchor.constraint(equalToConstant: 0)
        notesRail.isHidden = true

        root.addSubview(webView)
        root.addSubview(notesRail)
        root.addSubview(header)
        header.addSubview(headerStack)
        root.addSubview(footer)
        footer.addSubview(footerStack)
        root.addSubview(libraryWebView)
        root.addSubview(titlebarDragView)

        NSLayoutConstraint.activate([
            header.topAnchor.constraint(equalTo: root.topAnchor),
            header.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            header.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            header.heightAnchor.constraint(equalToConstant: 48),

            headerStack.leadingAnchor.constraint(equalTo: header.leadingAnchor, constant: 72),
            headerStack.trailingAnchor.constraint(equalTo: header.trailingAnchor, constant: -14),
            headerStack.centerYAnchor.constraint(equalTo: header.centerYAnchor),

            webView.topAnchor.constraint(equalTo: root.topAnchor),
            webView.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: notesRail.leadingAnchor),
            webView.bottomAnchor.constraint(equalTo: root.bottomAnchor),

            notesRail.topAnchor.constraint(equalTo: header.bottomAnchor, constant: 4),
            notesRail.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            notesRail.bottomAnchor.constraint(equalTo: footer.topAnchor, constant: -4),
            notesRailWidthConstraint,

            footer.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 4),
            footer.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -4),
            footer.bottomAnchor.constraint(equalTo: root.bottomAnchor, constant: -4),
            footer.heightAnchor.constraint(equalToConstant: 20),

            footerStack.leadingAnchor.constraint(equalTo: footer.leadingAnchor, constant: 10),
            footerStack.trailingAnchor.constraint(equalTo: footer.trailingAnchor, constant: -10),
            footerStack.centerYAnchor.constraint(equalTo: footer.centerYAnchor),

            titlebarDragView.topAnchor.constraint(equalTo: root.topAnchor),
            titlebarDragView.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 86),
            titlebarDragView.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            titlebarDragView.heightAnchor.constraint(equalToConstant: 36),

            libraryWebView.topAnchor.constraint(equalTo: titlebarDragView.bottomAnchor),
            libraryWebView.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            libraryWebView.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            libraryWebView.bottomAnchor.constraint(equalTo: root.bottomAnchor),
        ])

        window = NSWindow(
            contentRect: NSRect(x: 80, y: 80, width: 1180, height: 760),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Click"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.styleMask.insert(.fullSizeContentView)
        window.isMovableByWindowBackground = true
        window.acceptsMouseMovedEvents = true
        window.contentView = root
        window.center()
        mainRootView = root
        libraryHomeWebView = libraryWebView
        libraryWebView.isHidden = true
        mainTitlebarDragView = titlebarDragView
        titlebarDragView.isHidden = true
        readerHeaderView = header
        readerFooterView = footer
        setReaderChromeVisible(false)
        window.makeKeyAndOrderFront(nil)
    }

    private func loadBundledBook() {
        guard let resourceURL = Bundle.main.resourceURL else {
            statusLabel.stringValue = "没有找到资源目录"
            return
        }

        let bookRoot = resourceURL.appendingPathComponent("default-book", isDirectory: true)
        let defaultEntry = BookEntry(
            title: "Click 示例书",
            author: "Click",
            bookHash: "sentence-reader-default-good-strategy-bad-strategy-v1",
            epubPath: resourceURL.appendingPathComponent("default-fixture.epub").path,
            bookRootPath: bookRoot.path,
            isBundled: true
        )
        bookEntries = loadBookLibrary(defaultEntry: defaultEntry)
        saveBookLibrary()
        let entry = currentBookEntry ?? bookEntries.first ?? defaultEntry
        loadBookEntry(entry)
    }

    private func loadBookEntry(_ entry: BookEntry) {
        if !entry.isBundled {
            let epubURL = URL(fileURLWithPath: entry.epubPath)
            guard isOwnedImportedBookFile(epubURL),
                  FileManager.default.fileExists(atPath: epubURL.path)
            else {
                statusLabel.stringValue = "书籍内部副本缺失，请重新导入：\(entry.title)"
                return
            }
        }

        let bookRoot = URL(fileURLWithPath: entry.bookRootPath, isDirectory: true)
        guard FileManager.default.fileExists(atPath: bookRoot.path) else {
            statusLabel.stringValue = "没有找到书籍目录：\(entry.title)"
            return
        }

        currentBookEntry = entry
        bookTitleLabel.stringValue = entry.title
        bookSwitcherButton.title = bookEntries.count > 1 ? "书籍(\(bookEntries.count))" : "书籍"
        refreshLibraryWindow()
        bookRootURL = bookRoot
        chapters = collectHTMLChapters(in: bookRoot)
        chapterTitles = chapters.enumerated().map { index, url in
            chapterDisplayTitle(for: url, index: index)
        }
        readerBookID = readerAPIAvailable
            ? readerAPI.createBook(
                title: entry.title,
                author: entry.author,
                bookHash: entry.bookHash,
                filePath: entry.epubPath
            )
            : nil
        redAnnotationIDs = [:]
        pendingNoteJumpIndex = nil
        refreshNotes()
        tocEntries = collectTOCEntries(in: bookRoot, chapters: chapters)
        contentsButton.title = tocEntries.isEmpty ? "目录(\(chapters.count))" : "目录(\(tocEntries.count))"

        guard !chapters.isEmpty else {
            statusLabel.stringValue = "没有找到 EPUB 正文 HTML"
            return
        }

        if let position = loadSavedReadingPosition(),
           let restoredIndex = chapterIndex(for: position) {
            loadChapter(
                at: restoredIndex,
                initialPage: .position(
                    pageIndex: max(0, position.pageIndex),
                    pageRatio: max(0, min(1, position.pageRatio)),
                    totalPages: max(1, position.totalPages)
                )
            )
            return
        }

        if entry.isBundled {
            let preferred = bookRoot.appendingPathComponent("OEBPS/Text/part0010.xhtml")
            currentChapterIndex = chapters.firstIndex(of: preferred) ?? 0
        } else {
            currentChapterIndex = 0
        }
        loadChapter(at: currentChapterIndex, initialPage: .start)
    }

    private func loadBookLibrary(defaultEntry: BookEntry) -> [BookEntry] {
        var entries: [BookEntry] = [defaultEntry]
        if let data = UserDefaults.standard.data(forKey: bookLibraryKey),
           let saved = try? JSONDecoder().decode([BookEntry].self, from: data) {
            for entry in saved {
                guard let normalized = normalizedImportedBookEntry(entry) else {
                    continue
                }
                if !entries.contains(where: { $0.bookHash == normalized.bookHash }) {
                    entries.append(normalized)
                }
            }
        }
        return entries
    }

    private func normalizedImportedBookEntry(_ entry: BookEntry) -> BookEntry? {
        if entry.isBundled {
            return entry
        }
        guard let root = ownedBookRootURL(for: entry.bookHash) else {
            return nil
        }
        let epubCopy = root.appendingPathComponent("book.epub")
        let extractedRoot = root.appendingPathComponent("book", isDirectory: true)
        guard isOwnedImportedBookFile(epubCopy),
              FileManager.default.fileExists(atPath: epubCopy.path),
              FileManager.default.fileExists(atPath: extractedRoot.path)
        else {
            return nil
        }
        if entry.epubPath == epubCopy.path && entry.bookRootPath == extractedRoot.path {
            return entry
        }
        return BookEntry(
            title: entry.title,
            author: entry.author,
            bookHash: entry.bookHash,
            epubPath: epubCopy.path,
            bookRootPath: extractedRoot.path,
            isBundled: false
        )
    }

    private func saveBookLibrary() {
        guard let data = try? JSONEncoder().encode(bookEntries) else {
            return
        }
        UserDefaults.standard.set(data, forKey: bookLibraryKey)
    }

    private func loadReaderSettings() -> ReaderSettings {
        guard let data = UserDefaults.standard.data(forKey: readerSettingsKey),
              let saved = try? JSONDecoder().decode(ReaderSettings.self, from: data)
        else {
            return .defaultSettings
        }
        return sanitizedReaderSettings(saved)
    }

    private func saveReaderSettings() {
        readerSettings = sanitizedReaderSettings(readerSettings)
        if let data = try? JSONEncoder().encode(readerSettings) {
            UserDefaults.standard.set(data, forKey: readerSettingsKey)
        }
    }

    private func sanitizedReaderSettings(_ settings: ReaderSettings) -> ReaderSettings {
        ReaderSettings(
            fontSize: max(15, min(28, settings.fontSize)),
            lineHeight: max(1.2, min(2.05, settings.lineHeight)),
            marginX: max(2, min(40, settings.marginX)),
            theme: settings.theme == "warm" ? "warm" : "dark"
        )
    }

    private func readerSettingsPayload() -> [String: Any] {
        let settings = sanitizedReaderSettings(readerSettings)
        return [
            "fontSize": settings.fontSize,
            "lineHeight": settings.lineHeight,
            "marginX": settings.marginX,
            "theme": settings.theme,
        ]
    }

    private func applyReaderSettingsToWebView() {
        guard let data = try? JSONSerialization.data(withJSONObject: readerSettingsPayload()),
              let json = String(data: data, encoding: .utf8)
        else {
            return
        }
        webView?.evaluateJavaScript("window.__sentenceReaderApplySettings && window.__sentenceReaderApplySettings(\(json));")
    }

    @objc private func showReaderSettings(_ sender: NSButton) {
        let menu = NSMenu(title: "阅读设置")
        menu.autoenablesItems = false

        let summary = NSMenuItem(title: "字号 \(readerSettings.fontSize) · 行距 \(String(format: "%.2f", readerSettings.lineHeight)) · 边距 \(readerSettings.marginX)", action: nil, keyEquivalent: "")
        summary.isEnabled = false
        menu.addItem(summary)
        menu.addItem(.separator())

        addReaderSettingItem("字号 +", action: "fontPlus", to: menu)
        addReaderSettingItem("字号 -", action: "fontMinus", to: menu)
        addReaderSettingItem("行距 +", action: "linePlus", to: menu)
        addReaderSettingItem("行距 -", action: "lineMinus", to: menu)
        addReaderSettingItem("边距 +", action: "marginPlus", to: menu)
        addReaderSettingItem("边距 -", action: "marginMinus", to: menu)
        menu.addItem(.separator())
        addReaderSettingItem("黑底白字", action: "themeDark", to: menu, isOn: readerSettings.theme == "dark")
        addReaderSettingItem("暖白纸张", action: "themeWarm", to: menu, isOn: readerSettings.theme == "warm")
        menu.addItem(.separator())
        addReaderSettingItem("重置阅读设置", action: "reset", to: menu)

        menu.popUp(positioning: menu.items.first, at: NSPoint(x: 0, y: sender.bounds.maxY + 4), in: sender)
    }

    private func addReaderSettingItem(_ title: String, action: String, to menu: NSMenu, isOn: Bool = false) {
        let item = NSMenuItem(title: title, action: #selector(changeReaderSetting(_:)), keyEquivalent: "")
        item.target = self
        item.representedObject = action
        item.state = isOn ? .on : .off
        item.isEnabled = true
        menu.addItem(item)
    }

    @objc private func changeReaderSetting(_ sender: NSMenuItem) {
        guard let action = sender.representedObject as? String else {
            return
        }
        switch action {
        case "fontPlus":
            readerSettings.fontSize += 1
        case "fontMinus":
            readerSettings.fontSize -= 1
        case "linePlus":
            readerSettings.lineHeight += 0.08
        case "lineMinus":
            readerSettings.lineHeight -= 0.08
        case "marginPlus":
            readerSettings.marginX += 4
        case "marginMinus":
            readerSettings.marginX -= 4
        case "themeDark":
            readerSettings.theme = "dark"
        case "themeWarm":
            readerSettings.theme = "warm"
        case "reset":
            readerSettings = .defaultSettings
        default:
            return
        }
        saveReaderSettings()
        applyReaderSettingsToWebView()
        statusLabel.stringValue = "阅读设置已保存：字号 \(readerSettings.fontSize) · 行距 \(String(format: "%.2f", readerSettings.lineHeight)) · 边距 \(readerSettings.marginX)"
    }

    @objc private func showRuntimeEnvironment(_ sender: NSButton) {
        openRuntimeEnvironmentWindow()
    }

    private func openRuntimeEnvironmentWindow() {
        let paths = currentFunASRRuntimePaths()
        let controller = RuntimeEnvironmentWindowController(
            funasrPython: paths.python,
            funasrWorker: paths.worker,
            speechProvider: SpeechTranscriptionProvider.current,
            loadPreflightReport: { [weak self] in
                self?.runFirstRunPreflightForApp()
            },
            saveFunASRPaths: { [weak self] python, worker in
                self?.saveFunASRRuntimePaths(python: python, worker: worker) ?? false
            },
            saveSpeechProvider: { [weak self] provider in
                SpeechTranscriptionProvider.save(provider)
                self?.refreshSpeechWarmServiceForCurrentProvider()
            },
            openPath: { path in
                NSWorkspace.shared.open(URL(fileURLWithPath: path))
            }
        )
        runtimeEnvironmentWindowController = controller
        controller.showWindow(nil)
        controller.window?.makeKeyAndOrderFront(nil)
    }

    private func showFirstRunGuideIfNeeded() {
        guard !didShowFirstRunGuide else {
            return
        }
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            let report = self.runFirstRunPreflightForApp()
            let ready = Self.reportBool(report?["first_run_ready"])
            DispatchQueue.main.async {
                guard !ready, !self.didShowFirstRunGuide else {
                    return
                }
                self.didShowFirstRunGuide = true
                self.statusLabel.stringValue = "运行环境需要处理，已打开首启引导"
                self.openRuntimeEnvironmentWindow()
            }
        }
    }

    private static func reportBool(_ value: Any?) -> Bool {
        if let value = value as? Bool {
            return value
        }
        if let value = value as? NSNumber {
            return value.boolValue
        }
        return false
    }

    private func sentenceReaderAppSupportDirectory() -> URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support", isDirectory: true)
        return base.appendingPathComponent("SentenceReader", isDirectory: true)
    }

    private func runtimeConfigURLForApp() -> URL {
        if let value = ProcessInfo.processInfo.environment["SENTENCE_READER_RUNTIME_CONFIG"],
           !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return URL(fileURLWithPath: (value as NSString).expandingTildeInPath)
        }
        return sentenceReaderAppSupportDirectory().appendingPathComponent("config/runtime_config.json")
    }

    private func runtimeConfigObjectForApp() -> [String: Any] {
        guard let data = try? Data(contentsOf: runtimeConfigURLForApp()),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            return [:]
        }
        return object
    }

    private func runtimeConfigFunASRValueForApp(_ key: String) -> String? {
        guard let funasr = runtimeConfigObjectForApp()["funasr"] as? [String: Any],
              let value = funasr[key] as? String
        else {
            return nil
        }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    private func currentFunASRRuntimePaths() -> (python: String, worker: String) {
        let environment = ProcessInfo.processInfo.environment
        let python = UserDefaults.standard.string(forKey: funASRPythonDefaultsKey)
            ?? environment["SENTENCE_READER_FUNASR_PYTHON"]
            ?? runtimeConfigFunASRValueForApp("python")
            ?? funASRPythonDefaultPath
        let worker = UserDefaults.standard.string(forKey: funASRWorkerDefaultsKey)
            ?? environment["SENTENCE_READER_FUNASR_WORKER"]
            ?? runtimeConfigFunASRValueForApp("worker")
            ?? funASRWorkerDefaultPath
        return (python, worker)
    }

    private func expandedPath(_ path: String) -> String {
        (path.trimmingCharacters(in: .whitespacesAndNewlines) as NSString).expandingTildeInPath
    }

    private func funASRServerURL(path: String) -> URL {
        URL(string: "http://127.0.0.1:\(funASRServerPort)\(path)")!
    }

    private func isFunASRServerHealthy(timeout: TimeInterval) -> Bool {
        var request = URLRequest(url: funASRServerURL(path: "/health"))
        request.timeoutInterval = timeout
        let semaphore = DispatchSemaphore(value: 0)
        var healthy = false
        let task = URLSession.shared.dataTask(with: request) { data, response, _ in
            if (response as? HTTPURLResponse)?.statusCode == 200,
               let data,
               let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               object["ok"] as? Bool == true {
                healthy = true
            }
            semaphore.signal()
        }
        task.resume()
        let waitResult = semaphore.wait(timeout: .now() + .milliseconds(Int((timeout + 0.5) * 1000)))
        if waitResult == .timedOut {
            task.cancel()
            return false
        }
        return healthy
    }

    private func startFunASRWarmServiceIfAvailable() {
        guard !funASRWarmupStarted else {
            return
        }
        funASRWarmupStarted = true

        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            let paths = self.currentFunASRRuntimePaths()
            let pythonURL = URL(fileURLWithPath: self.expandedPath(paths.python))
            let workerURL = URL(fileURLWithPath: self.expandedPath(paths.worker))
            guard FileManager.default.isExecutableFile(atPath: pythonURL.path),
                  FileManager.default.fileExists(atPath: workerURL.path)
            else {
                return
            }

            if self.isFunASRServerHealthy(timeout: 0.8) {
                DispatchQueue.main.async {
                    self.statusLabel.stringValue = "FunASR 后台服务已就绪"
                }
                return
            }

            do {
                let appSupport = self.sentenceReaderAppSupportDirectory()
                let outputDirectory = appSupport.appendingPathComponent("FunASRServer", isDirectory: true)
                let logDirectory = appSupport.appendingPathComponent("Logs", isDirectory: true)
                try FileManager.default.createDirectory(at: outputDirectory, withIntermediateDirectories: true)
                try FileManager.default.createDirectory(at: logDirectory, withIntermediateDirectories: true)
                let logURL = logDirectory.appendingPathComponent("funasr_server.log")
                if !FileManager.default.fileExists(atPath: logURL.path) {
                    FileManager.default.createFile(atPath: logURL.path, contents: nil)
                }
                let logHandle = try FileHandle(forWritingTo: logURL)
                _ = try? logHandle.seekToEnd()

                let process = Process()
                process.executableURL = pythonURL
                process.arguments = [
                    workerURL.path,
                    "--server",
                    "--host", "127.0.0.1",
                    "--port", String(self.funASRServerPort),
                    "--output-dir", outputDirectory.path,
                    "--model", "paraformer-zh",
                    "--vad-model", "fsmn-vad",
                    "--punc-model", "ct-punc",
                    "--device", "cpu",
                ]
                process.standardOutput = logHandle
                process.standardError = logHandle
                try process.run()

                DispatchQueue.main.async {
                    self.funASRServerProcess = process
                    self.statusLabel.stringValue = "FunASR 正在后台预热..."
                }

                for _ in 0..<120 {
                    if self.isFunASRServerHealthy(timeout: 1.0) {
                        DispatchQueue.main.async {
                            self.statusLabel.stringValue = "FunASR 后台服务已就绪"
                        }
                        return
                    }
                    if !process.isRunning {
                        DispatchQueue.main.async {
                            if self.funASRServerProcess === process {
                                self.funASRServerProcess = nil
                            }
                        }
                        return
                    }
                    Thread.sleep(forTimeInterval: 1.0)
                }

                DispatchQueue.main.async {
                    if process.isRunning {
                        self.statusLabel.stringValue = "FunASR 仍在后台加载，首次语音可能稍慢"
                    }
                }
            } catch {
                DispatchQueue.main.async {
                    self.statusLabel.stringValue = "FunASR 后台预热未启动，语音会自动使用原转写路径"
                }
            }
        }
    }

    private func stopFunASRWarmService() {
        guard let process = funASRServerProcess else {
            return
        }
        if process.isRunning {
            process.terminate()
        }
        funASRServerProcess = nil
    }

    private func restartFunASRWarmServiceAfterConfigurationChange() {
        stopFunASRWarmService()
        funASRWarmupStarted = false
        refreshSpeechWarmServiceForCurrentProvider()
    }

    private func refreshSpeechWarmServiceForCurrentProvider() {
        if SpeechTranscriptionProvider.current == .funASR {
            startFunASRWarmServiceIfAvailable()
        } else {
            stopFunASRWarmService()
            funASRWarmupStarted = false
        }
    }

    private func saveFunASRRuntimePaths(python: String, worker: String) -> Bool {
        let python = python.trimmingCharacters(in: .whitespacesAndNewlines)
        let worker = worker.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !python.isEmpty, !worker.isEmpty else {
            return false
        }
        UserDefaults.standard.set(python, forKey: funASRPythonDefaultsKey)
        UserDefaults.standard.set(worker, forKey: funASRWorkerDefaultsKey)

        let configURL = runtimeConfigURLForApp()
        var payload = runtimeConfigObjectForApp()
        payload["schema"] = "sentence_reader.runtime_config.v1"
        if payload["created_at"] == nil {
            payload["created_at"] = ISO8601DateFormatter().string(from: Date())
        }
        payload["updated_at"] = ISO8601DateFormatter().string(from: Date())
        var funasr = payload["funasr"] as? [String: Any] ?? [:]
        funasr["python"] = python
        funasr["worker"] = worker
        payload["funasr"] = funasr
        payload["postgres"] = payload["postgres"] as? [String: Any] ?? [:]

        do {
            try FileManager.default.createDirectory(at: configURL.deletingLastPathComponent(), withIntermediateDirectories: true)
            let data = try JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted, .sortedKeys])
            try data.write(to: configURL, options: .atomic)
            restartFunASRWarmServiceAfterConfigurationChange()
            return true
        } catch {
            return false
        }
    }

    private func firstRunPreflightScriptCandidates() -> [URL] {
        var candidates: [URL] = []
        if let resourceURL = Bundle.main.resourceURL {
            candidates.append(resourceURL.appendingPathComponent("ReaderRuntime/scripts/sentence_reader_first_run_preflight.py"))
        }
        candidates.append(URL(fileURLWithPath: FileManager.default.currentDirectoryPath).appendingPathComponent("scripts/sentence_reader_first_run_preflight.py"))
        return candidates
    }

    private func firstRunPreflightScriptURL() -> URL? {
        firstRunPreflightScriptCandidates().first { FileManager.default.fileExists(atPath: $0.path) }
    }

    private func runFirstRunPreflightForApp() -> [String: Any]? {
        guard let scriptURL = firstRunPreflightScriptURL() else {
            return [
                "schema": "sentence_reader.first_run_preflight_report.v1",
                "first_run_ready": false,
                "_error": "first_run_preflight_script_missing",
            ]
        }

        let diagnostics = sentenceReaderAppSupportDirectory().appendingPathComponent("Diagnostics", isDirectory: true)
        let reportURL = diagnostics.appendingPathComponent("sentence_reader_first_run_preflight_report.json")
        let markdownURL = diagnostics.appendingPathComponent("sentence_reader_first_run_preflight_report.md")
        try? FileManager.default.createDirectory(at: diagnostics, withIntermediateDirectories: true)

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = [
            "python3",
            scriptURL.path,
            "--output", reportURL.path,
            "--markdown", markdownURL.path,
            "--require-postgres-decision",
            "--require-runtime-bootstrap",
            "--require-first-run-ready",
            "--require-funasr-configurable",
        ]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            return [
                "schema": "sentence_reader.first_run_preflight_report.v1",
                "first_run_ready": false,
                "_error": error.localizedDescription,
            ]
        }

        let output = pipe.fileHandleForReading.readDataToEndOfFile()
        let stdout = String(data: output, encoding: .utf8) ?? ""
        guard let data = try? Data(contentsOf: reportURL),
              var report = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            return [
                "schema": "sentence_reader.first_run_preflight_report.v1",
                "first_run_ready": false,
                "_returncode": Int(process.terminationStatus),
                "_stdout": stdout,
                "_report_path": reportURL.path,
                "_markdown_path": markdownURL.path,
            ]
        }
        report["_returncode"] = Int(process.terminationStatus)
        report["_stdout"] = stdout
        report["_report_path"] = reportURL.path
        report["_markdown_path"] = markdownURL.path
        return report
    }

    private func readingPositionKey() -> String {
        let suffix = currentBookEntry?.bookHash ?? "unknown"
        return "\(readingPositionKeyPrefix).\(suffix)"
    }

    @objc private func openBookPanel(_ sender: NSButton) {
        let panel = NSOpenPanel()
        panel.title = "打开 EPUB"
        panel.prompt = "打开"
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        if let epubType = UTType(filenameExtension: "epub") {
            panel.allowedContentTypes = [epubType]
        }
        guard panel.runModal() == .OK,
              let url = panel.url
        else {
            return
        }
        importEPUB(url)
    }

    @objc private func showLibraryWindow(_ sender: Any?) {
        showMainLibrary()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func ensureReaderAPIForLibraryUI() -> Bool {
        if readerAPI.health() {
            readerAPIAvailable = true
            return true
        }
        return launchReaderAPI(host: "0.0.0.0")
    }

    private func showNativeLibraryFallbackWindow() {
        if libraryWindowController == nil {
            libraryWindowController = buildLibraryWindowController()
            libraryWindowUsesWebUI = false
        }
        refreshLibraryWindow()
        libraryWindowController?.showWindow(nil)
        libraryWindowController?.window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func buildLibraryWebWindowController() -> NSWindowController {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1180, height: 760),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Click 书库"
        window.minSize = NSSize(width: 920, height: 620)
        let config = WKWebViewConfiguration()
        let webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.setValue(false, forKey: "drawsBackground")
        window.contentView = webView
        if let url = URL(string: "http://127.0.0.1:18180/library?surface=mac-app") {
            webView.load(URLRequest(url: url))
        }
        window.center()
        statusLabel.stringValue = "已打开产品级书库主界面；旧书库表格仅作降级入口"
        return NSWindowController(window: window)
    }

    func webView(
        _ webView: WKWebView,
        runOpenPanelWith parameters: WKOpenPanelParameters,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping ([URL]?) -> Void
    ) {
        let panel = NSOpenPanel()
        panel.title = "导入 EPUB"
        panel.prompt = "导入"
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.allowedContentTypes = [.epub]
        panel.begin { response in
            completionHandler(response == .OK ? panel.urls : nil)
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        let isMainLibraryWebView = libraryHomeWebView.map { webView === $0 } ?? false
        let isDetachedLibraryWebView = (libraryWindowController?.window?.contentView as? WKWebView).map { webView === $0 } ?? false
        guard (isMainLibraryWebView || isDetachedLibraryWebView),
              let url = navigationAction.request.url,
              let bookID = nativeLibraryBookID(from: url)
        else {
            decisionHandler(.allow)
            return
        }

        decisionHandler(.cancel)
        openNativeReaderFromLibraryBookID(bookID)
    }

    private func nativeLibraryBookID(from url: URL) -> String? {
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            return nil
        }
        if url.scheme == "sentence-reader",
           url.host == "open-native",
           let bookID = components.queryItems?.first(where: { $0.name == "book_id" })?.value,
           !bookID.isEmpty {
            return bookID
        }
        guard url.path == "/lan/reader",
              let bookID = components.queryItems?.first(where: { $0.name == "book_id" })?.value,
              !bookID.isEmpty
        else {
            return nil
        }
        return bookID
    }

    private func openNativeReaderFromLibraryBookID(_ bookID: String) {
        statusLabel.stringValue = "正在用原生阅读器打开书库书籍..."
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            guard let dashboard = self.readerAPI.libraryDashboard(),
                  let book = self.libraryDashboardBook(bookID: bookID, dashboard: dashboard)
            else {
                DispatchQueue.main.async {
                    self.statusLabel.stringValue = "书库没有找到这本书：\(bookID)"
                }
                return
            }

            do {
                let entry = try self.nativeBookEntry(fromLibraryBook: book)
                DispatchQueue.main.async {
                    self.upsertAndLoadNativeLibraryEntry(entry)
                }
            } catch {
                DispatchQueue.main.async {
                    self.statusLabel.stringValue = "原生阅读器打开失败：\(error.localizedDescription)"
                }
            }
        }
    }

    private func libraryDashboardBook(bookID: String, dashboard: [String: Any]) -> [String: Any]? {
        let books = dashboard["books"] as? [[String: Any]] ?? []
        if let book = books.first(where: { $0["id"] as? String == bookID }) {
            return book
        }
        if let current = dashboard["current_book"] as? [String: Any],
           current["id"] as? String == bookID {
            return current
        }
        return nil
    }

    private func nativeBookEntry(fromLibraryBook book: [String: Any]) throws -> BookEntry {
        let title = ((book["title"] as? String) ?? "未命名").trimmingCharacters(in: .whitespacesAndNewlines)
        let author = book["author"] as? String
        guard let rawBookHash = book["book_hash"] as? String,
              !rawBookHash.isEmpty
        else {
            throw NSError(domain: "SentenceReader.LibraryBridge", code: 1, userInfo: [NSLocalizedDescriptionKey: "缺少 book_hash"])
        }

        if let existing = bookEntries.first(where: { $0.bookHash == rawBookHash }) {
            return existing
        }

        let file = book["file"] as? [String: Any] ?? [:]
        guard let filePath = file["file_path"] as? String,
              !filePath.isEmpty
        else {
            throw NSError(domain: "SentenceReader.LibraryBridge", code: 2, userInfo: [NSLocalizedDescriptionKey: "缺少 EPUB 文件路径"])
        }
        let sourceURL = URL(fileURLWithPath: filePath)
        guard FileManager.default.fileExists(atPath: sourceURL.path) else {
            throw NSError(domain: "SentenceReader.LibraryBridge", code: 3, userInfo: [NSLocalizedDescriptionKey: "EPUB 文件不存在：\(filePath)"])
        }

        if let bundled = bundledBookEntryIfMatches(bookHash: rawBookHash, fileURL: sourceURL) {
            return bundled
        }

        let epubURL: URL
        let rootURL: URL
        if isOwnedImportedBookFile(sourceURL) {
            epubURL = sourceURL
            rootURL = sourceURL.deletingLastPathComponent()
        } else {
            rootURL = try ownedBookRootURLOrThrow(for: safeBookDirectoryName(rawBookHash))
            try FileManager.default.createDirectory(at: rootURL, withIntermediateDirectories: true)
            epubURL = rootURL.appendingPathComponent("book.epub")
            if !FileManager.default.fileExists(atPath: epubURL.path) {
                try FileManager.default.copyItem(at: sourceURL, to: epubURL)
            }
        }

        let extractedRoot = rootURL.appendingPathComponent("book", isDirectory: true)
        guard unzipEPUBIfNeeded(epubURL: epubURL, rootURL: extractedRoot) else {
            throw NSError(domain: "SentenceReader.LibraryBridge", code: 4, userInfo: [NSLocalizedDescriptionKey: "EPUB 解压失败：\(title)"])
        }

        return BookEntry(
            title: title.isEmpty ? sourceURL.deletingPathExtension().lastPathComponent : title,
            author: author,
            bookHash: rawBookHash,
            epubPath: epubURL.path,
            bookRootPath: extractedRoot.path,
            isBundled: false
        )
    }

    private func bundledBookEntryIfMatches(bookHash: String, fileURL: URL) -> BookEntry? {
        guard let resourceURL = Bundle.main.resourceURL else {
            return nil
        }
        let defaultHash = "sentence-reader-default-good-strategy-bad-strategy-v1"
        let defaultEPUB = resourceURL.appendingPathComponent("default-fixture.epub")
        guard bookHash == defaultHash || fileURL.standardizedFileURL.path == defaultEPUB.standardizedFileURL.path else {
            return nil
        }
        return BookEntry(
            title: "Click 示例书",
            author: "Click",
            bookHash: defaultHash,
            epubPath: defaultEPUB.path,
            bookRootPath: resourceURL.appendingPathComponent("default-book", isDirectory: true).path,
            isBundled: true
        )
    }

    private func safeBookDirectoryName(_ raw: String) -> String {
        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_"))
        let scalars = raw.unicodeScalars.map { allowed.contains($0) ? Character($0) : "-" }
        let value = String(scalars).trimmingCharacters(in: CharacterSet(charactersIn: "-"))
        return value.isEmpty ? "library-book" : value
    }

    private func upsertAndLoadNativeLibraryEntry(_ entry: BookEntry) {
        if let index = bookEntries.firstIndex(where: { $0.bookHash == entry.bookHash }) {
            bookEntries[index] = entry
        } else {
            bookEntries.append(entry)
        }
        saveBookLibrary()
        loadBookEntry(entry)
        hideMainLibraryForReading()
        libraryWindowController?.window?.close()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        statusLabel.stringValue = "已用原生阅读器打开：\(entry.title)"
    }

    private func buildLibraryWindowController() -> NSWindowController {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 860, height: 540),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "书库"
        window.minSize = NSSize(width: 720, height: 420)

        let root = NSView()
        root.wantsLayer = true
        root.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor

        let title = NSTextField(labelWithString: "书库")
        title.font = NSFont(name: "Microsoft YaHei", size: 20) ?? NSFont.systemFont(ofSize: 20, weight: .semibold)
        title.textColor = .labelColor

        let subtitle = NSTextField(labelWithString: "所有导入的 EPUB 会复制进 Click 内部书库；导入成功后原 EPUB 可删除。")
        subtitle.font = NSFont(name: "Microsoft YaHei", size: 12) ?? NSFont.systemFont(ofSize: 12)
        subtitle.textColor = .secondaryLabelColor
        subtitle.lineBreakMode = .byTruncatingTail

        let summary = NSTextField(labelWithString: "")
        summary.font = NSFont(name: "Microsoft YaHei", size: 12) ?? NSFont.systemFont(ofSize: 12, weight: .medium)
        summary.textColor = .secondaryLabelColor
        librarySummaryLabel = summary

        let importButton = NSButton(title: "导入 EPUB", target: self, action: #selector(importBookFromLibrary(_:)))
        let openButton = NSButton(title: "打开所选", target: self, action: #selector(openSelectedLibraryBook(_:)))
        let revealButton = NSButton(title: "显示内部副本", target: self, action: #selector(revealSelectedLibraryBook(_:)))
        let removeButton = NSButton(title: "从书库移除", target: self, action: #selector(removeSelectedLibraryBook(_:)))
        [importButton, openButton, revealButton, removeButton].forEach { button in
            button.bezelStyle = .rounded
            button.controlSize = .regular
            button.font = NSFont(name: "Microsoft YaHei", size: 13) ?? NSFont.systemFont(ofSize: 13, weight: .medium)
        }

        let tableView = NSTableView()
        tableView.delegate = self
        tableView.dataSource = self
        tableView.headerView = nil
        tableView.rowHeight = 72
        tableView.selectionHighlightStyle = .regular
        tableView.usesAlternatingRowBackgroundColors = false
        tableView.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle
        tableView.doubleAction = #selector(openSelectedLibraryBook(_:))
        tableView.target = self

        let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("libraryBook"))
        column.title = "书籍"
        column.width = 800
        column.minWidth = 520
        column.resizingMask = .autoresizingMask
        tableView.addTableColumn(column)
        libraryTableView = tableView

        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.borderType = .noBorder
        scroll.documentView = tableView

        let titleStack = NSStackView(views: [title, NSView(), summary])
        titleStack.orientation = .horizontal
        titleStack.alignment = .firstBaseline
        titleStack.spacing = 12

        let actionStack = NSStackView(views: [importButton, openButton, revealButton, removeButton, NSView()])
        actionStack.orientation = .horizontal
        actionStack.alignment = .centerY
        actionStack.spacing = 10

        let stack = NSStackView(views: [titleStack, subtitle, actionStack, scroll])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 12

        [stack, titleStack, subtitle, actionStack, scroll].forEach {
            $0.translatesAutoresizingMaskIntoConstraints = false
        }

        root.addSubview(stack)
        window.contentView = root
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: root.topAnchor, constant: 18),
            stack.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 18),
            stack.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -18),
            stack.bottomAnchor.constraint(equalTo: root.bottomAnchor, constant: -18),

            titleStack.widthAnchor.constraint(equalTo: stack.widthAnchor),
            subtitle.widthAnchor.constraint(equalTo: stack.widthAnchor),
            actionStack.widthAnchor.constraint(equalTo: stack.widthAnchor),
            scroll.widthAnchor.constraint(equalTo: stack.widthAnchor),
            scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 280),
        ])
        window.center()
        return NSWindowController(window: window)
    }

    private func refreshLibraryWindow() {
        libraryTableView?.reloadData()
        librarySummaryLabel?.stringValue = "\(bookEntries.count) 本 · 当前：\(currentBookEntry?.title ?? "-")"

        guard let libraryTableView,
              let currentBookEntry,
              let index = bookEntries.firstIndex(where: { $0.bookHash == currentBookEntry.bookHash })
        else {
            return
        }
        libraryTableView.selectRowIndexes(IndexSet(integer: index), byExtendingSelection: false)
        libraryTableView.scrollRowToVisible(index)
    }

    private func makeLibraryBookCell(row: Int, tableView: NSTableView) -> NSView? {
        guard bookEntries.indices.contains(row) else {
            return nil
        }
        let entry = bookEntries[row]
        let identifier = NSUserInterfaceItemIdentifier("libraryBookCell")
        let cell = tableView.makeView(withIdentifier: identifier, owner: self) as? NSTableCellView ?? NSTableCellView()
        cell.identifier = identifier
        cell.subviews.forEach { $0.removeFromSuperview() }

        let title = NSTextField(labelWithString: entry.title)
        title.font = NSFont(name: "Microsoft YaHei", size: 14) ?? NSFont.systemFont(ofSize: 14, weight: .semibold)
        title.textColor = .labelColor
        title.lineBreakMode = .byTruncatingTail

        let statusText = currentBookEntry?.bookHash == entry.bookHash ? "当前阅读" : (entry.isBundled ? "内置样书" : "内部书库副本")
        let status = NSTextField(labelWithString: statusText)
        status.font = NSFont(name: "Microsoft YaHei", size: 11) ?? NSFont.systemFont(ofSize: 11, weight: .medium)
        status.textColor = currentBookEntry?.bookHash == entry.bookHash ? .systemGreen : .secondaryLabelColor

        let detail = NSTextField(labelWithString: libraryDetailText(for: entry))
        detail.font = NSFont(name: "Microsoft YaHei", size: 11) ?? NSFont.systemFont(ofSize: 11)
        detail.textColor = .secondaryLabelColor
        detail.lineBreakMode = .byTruncatingMiddle
        detail.maximumNumberOfLines = 1

        let top = NSStackView(views: [title, NSView(), status])
        top.orientation = .horizontal
        top.alignment = .firstBaseline
        top.spacing = 10

        let stack = NSStackView(views: [top, detail])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 5
        stack.translatesAutoresizingMaskIntoConstraints = false
        cell.addSubview(stack)

        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: cell.topAnchor, constant: 10),
            stack.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 12),
            stack.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -12),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: cell.bottomAnchor, constant: -8),
            top.widthAnchor.constraint(equalTo: stack.widthAnchor),
            detail.widthAnchor.constraint(equalTo: stack.widthAnchor),
        ])
        return cell
    }

    private func libraryDetailText(for entry: BookEntry) -> String {
        if entry.isBundled {
            return "随 App 资源提供 · 不从书库移除"
        }
        return "已托管：\(entry.epubPath) · 原 EPUB 可删除"
    }

    private func selectedLibraryEntry() -> (index: Int, entry: BookEntry)? {
        guard let libraryTableView else {
            return nil
        }
        let row = libraryTableView.selectedRow
        guard bookEntries.indices.contains(row) else {
            statusLabel.stringValue = "请先在书库中选择一本书"
            return nil
        }
        return (row, bookEntries[row])
    }

    @objc private func importBookFromLibrary(_ sender: Any?) {
        openBookPanel(openBookButton)
        refreshLibraryWindow()
    }

    @objc private func openSelectedLibraryBook(_ sender: Any?) {
        guard let selected = selectedLibraryEntry() else {
            return
        }
        loadBookEntry(selected.entry)
        libraryWindowController?.window?.close()
    }

    @objc private func revealSelectedLibraryBook(_ sender: Any?) {
        guard let selected = selectedLibraryEntry() else {
            return
        }
        let url = URL(fileURLWithPath: selected.entry.epubPath)
        NSWorkspace.shared.activateFileViewerSelecting([url])
        statusLabel.stringValue = "已在 Finder 中显示内部 EPUB 副本"
    }

    @objc private func removeSelectedLibraryBook(_ sender: Any?) {
        guard let selected = selectedLibraryEntry() else {
            return
        }
        guard !selected.entry.isBundled else {
            statusLabel.stringValue = "内置样书不能从书库移除"
            return
        }

        let alert = NSAlert()
        alert.messageText = "从书库移除《\(selected.entry.title)》？"
        alert.informativeText = "这只会移除书库列表索引，不删除内部 EPUB 副本、阅读位置、标红、笔记或 PostgreSQL 数据。需要恢复时可以重新导入同一本 EPUB。"
        alert.addButton(withTitle: "从书库移除")
        alert.addButton(withTitle: "取消")
        alert.alertStyle = .warning

        let removeAction = {
            guard self.bookEntries.indices.contains(selected.index),
                  self.bookEntries[selected.index].bookHash == selected.entry.bookHash
            else {
                self.statusLabel.stringValue = "书库列表已变化，请重新选择"
                self.refreshLibraryWindow()
                return
            }

            self.bookEntries.remove(at: selected.index)
            self.saveBookLibrary()
            if self.currentBookEntry?.bookHash == selected.entry.bookHash,
               let fallback = self.bookEntries.first {
                self.loadBookEntry(fallback)
            }
            self.refreshLibraryWindow()
            self.statusLabel.stringValue = "已从书库列表移除，内部副本和笔记仍保留：\(selected.entry.title)"
        }

        if let libraryWindow = libraryWindowController?.window {
            alert.beginSheetModal(for: libraryWindow) { response in
                if response == .alertFirstButtonReturn {
                    removeAction()
                }
            }
        } else if alert.runModal() == .alertFirstButtonReturn {
            removeAction()
        }
    }

    @objc private func showBookSwitcher(_ sender: NSButton) {
        let menu = NSMenu(title: "书籍")
        menu.autoenablesItems = false
        for (index, entry) in bookEntries.enumerated() {
            let item = NSMenuItem(title: entry.title, action: #selector(selectBookFromSwitcher(_:)), keyEquivalent: "")
            item.target = self
            item.tag = index
            item.state = entry.bookHash == currentBookEntry?.bookHash ? .on : .off
            item.isEnabled = true
            menu.addItem(item)
        }
        menu.addItem(.separator())
        let openItem = NSMenuItem(title: "打开 EPUB...", action: #selector(openBookFromMenu(_:)), keyEquivalent: "")
        openItem.target = self
        openItem.isEnabled = true
        menu.addItem(openItem)
        menu.popUp(positioning: menu.items.first, at: NSPoint(x: 0, y: sender.bounds.maxY + 4), in: sender)
    }

    @objc private func selectBookFromSwitcher(_ sender: NSMenuItem) {
        guard bookEntries.indices.contains(sender.tag) else {
            return
        }
        loadBookEntry(bookEntries[sender.tag])
    }

    @objc private func openBookFromMenu(_ sender: NSMenuItem) {
        openBookPanel(openBookButton)
    }

    @objc private func showReaderMoreMenu(_ sender: NSButton) {
        let menu = NSMenu(title: "更多")
        let items: [(String, Selector)] = [
            ("打开 EPUB...", #selector(openBookFromMenu(_:))),
            ("切换书籍", #selector(showBookSwitcherFromMenu(_:))),
            ("单词本", #selector(showVocabularyFromMenu(_:))),
            ("导出笔记", #selector(exportCurrentBookFromMenu(_:))),
            ("同步到 Hermes", #selector(syncCurrentBookFromMenu(_:))),
            ("认知队列", #selector(reviewCognitiveQueueFromMenu(_:))),
            ("运行环境", #selector(showRuntimeEnvironmentFromMenu(_:))),
            ("iPad 地址", #selector(showIPadLANReaderFromMenu(_:))),
        ]
        for (title, action) in items {
            let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
            item.target = self
            menu.addItem(item)
        }
        menu.popUp(positioning: nil, at: NSPoint(x: 0, y: sender.bounds.height + 4), in: sender)
    }

    @objc private func showBookSwitcherFromMenu(_ sender: NSMenuItem) {
        showBookSwitcher(bookSwitcherButton)
    }

    @objc private func showVocabularyFromMenu(_ sender: NSMenuItem) {
        showVocabularyPage()
    }

    @objc private func exportCurrentBookFromMenu(_ sender: NSMenuItem) {
        exportCurrentBook(exportButton)
    }

    @objc private func syncCurrentBookFromMenu(_ sender: NSMenuItem) {
        syncCurrentBook(syncButton)
    }

    @objc private func reviewCognitiveQueueFromMenu(_ sender: NSMenuItem) {
        reviewCognitiveQueue(cognitiveButton)
    }

    @objc private func showRuntimeEnvironmentFromMenu(_ sender: NSMenuItem) {
        showRuntimeEnvironment(runtimeEnvironmentButton)
    }

    @objc private func showIPadLANReaderFromMenu(_ sender: NSMenuItem) {
        showIPadLANReader(iPadLANButton)
    }

    private func showVocabularyPage() {
        guard ensureReaderAPIForLibraryUI(),
              let libraryHomeWebView
        else {
            statusLabel.stringValue = "Reader API 未连接，暂时打不开单词本"
            return
        }
        var components = URLComponents(string: "http://127.0.0.1:18180/vocab")
        var queryItems = [URLQueryItem(name: "surface", value: "mac-app")]
        if let readerBookID, !readerBookID.isEmpty {
            queryItems.append(URLQueryItem(name: "book_id", value: readerBookID))
        }
        components?.queryItems = queryItems
        guard let url = components?.url else {
            statusLabel.stringValue = "单词本地址生成失败"
            return
        }
        libraryHomeWebView.isHidden = false
        mainTitlebarDragView?.isHidden = false
        libraryHomeWebView.load(URLRequest(url: url))
        setReaderChromeVisible(false)
        window.title = "Click 单词本"
        statusLabel.stringValue = "已打开当前书的单词本"
    }

    private func importEPUB(_ url: URL) {
        guard url.pathExtension.lowercased() == "epub" else {
            statusLabel.stringValue = "只支持 EPUB 文件"
            return
        }
        guard let bookHash = fileHash(for: url) else {
            statusLabel.stringValue = "读取 EPUB 失败"
            return
        }

        do {
            let (root, epubCopy) = try copyImportedEPUBToOwnedLibrary(sourceURL: url, bookHash: bookHash)
            let extractedRoot = root.appendingPathComponent("book", isDirectory: true)
            guard unzipEPUBIfNeeded(epubURL: epubCopy, rootURL: extractedRoot) else {
                statusLabel.stringValue = "EPUB 解压失败：\(url.lastPathComponent)"
                return
            }
            guard isOwnedImportedBookFile(epubCopy),
                  verifyOwnedEPUBCopy(sourceURL: url, copiedURL: epubCopy, expectedHash: bookHash)
            else {
                statusLabel.stringValue = "导入失败：内部书库副本校验失败"
                return
            }

            let entry = BookEntry(
                title: url.deletingPathExtension().lastPathComponent,
                author: nil,
                bookHash: bookHash,
                epubPath: epubCopy.path,
                bookRootPath: extractedRoot.path,
                isBundled: false
            )
            if let existingIndex = bookEntries.firstIndex(where: { $0.bookHash == bookHash }) {
                bookEntries[existingIndex] = entry
            } else {
                bookEntries.append(entry)
            }
            saveBookLibrary()
            statusLabel.stringValue = "已导入到内部书库，原 EPUB 可删除：\(entry.title)"
            loadBookEntry(entry)
            hideMainLibraryForReading()
            refreshLibraryWindow()
        } catch {
            statusLabel.stringValue = "导入失败：\(error.localizedDescription)"
        }
    }

    private func copyImportedEPUBToOwnedLibrary(sourceURL: URL, bookHash: String) throws -> (root: URL, epubCopy: URL) {
        let root = try ownedBookRootURLOrThrow(for: bookHash)
        let epubCopy = root.appendingPathComponent("book.epub")
        let fileManager = FileManager.default
        try fileManager.createDirectory(at: root, withIntermediateDirectories: true)

        if fileManager.fileExists(atPath: epubCopy.path),
           verifyOwnedEPUBCopy(sourceURL: sourceURL, copiedURL: epubCopy, expectedHash: bookHash) {
            return (root, epubCopy)
        }

        if fileManager.fileExists(atPath: epubCopy.path) {
            try fileManager.removeItem(at: epubCopy)
        }
        try fileManager.copyItem(at: sourceURL, to: epubCopy)

        guard verifyOwnedEPUBCopy(sourceURL: sourceURL, copiedURL: epubCopy, expectedHash: bookHash) else {
            try? fileManager.removeItem(at: epubCopy)
            throw NSError(
                domain: "SentenceReader.ImportOwnership",
                code: 1,
                userInfo: [NSLocalizedDescriptionKey: "内部书库副本与原 EPUB 不一致"]
            )
        }
        return (root, epubCopy)
    }

    private func ownedBookRootURL(for bookHash: String) -> URL? {
        try? ownedBookRootURLOrThrow(for: bookHash)
    }

    private func ownedBookRootURLOrThrow(for bookHash: String) throws -> URL {
        try appSupportBooksDirectory().appendingPathComponent(bookHash, isDirectory: true)
    }

    private func isOwnedImportedBookFile(_ url: URL) -> Bool {
        guard let booksDirectory = try? appSupportBooksDirectory() else {
            return false
        }
        let target = url.standardizedFileURL.path
        let root = booksDirectory.standardizedFileURL.path
        return target.hasPrefix(root + "/") && url.lastPathComponent == "book.epub"
    }

    private func verifyOwnedEPUBCopy(sourceURL: URL, copiedURL: URL, expectedHash: String) -> Bool {
        guard isOwnedImportedBookFile(copiedURL),
              FileManager.default.fileExists(atPath: copiedURL.path),
              fileHash(for: copiedURL) == expectedHash
        else {
            return false
        }
        let sourceSize = (try? sourceURL.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? -1
        let copiedSize = (try? copiedURL.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? -2
        return sourceSize >= 0 && sourceSize == copiedSize
    }

    private func appSupportBooksDirectory() throws -> URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support", isDirectory: true)
        let directory = base.appendingPathComponent("SentenceReader/Books", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    private func appSupportExportsDirectory() throws -> URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support", isDirectory: true)
        let directory = base.appendingPathComponent("SentenceReader/Exports", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    private func appSupportHermesSyncDirectory() throws -> URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support", isDirectory: true)
        let directory = base.appendingPathComponent("SentenceReader/HermesSync", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    @objc private func exportCurrentBook(_ sender: NSButton) {
        guard let bookID = readerBookID else {
            statusLabel.stringValue = "Reader API 未连接，无法导出"
            return
        }
        let outputDir = try? appSupportExportsDirectory().path
        statusLabel.stringValue = "正在导出当前书笔记..."
        sender.isEnabled = false
        DispatchQueue.global(qos: .utility).async { [readerAPI] in
            let result = readerAPI.exportBook(bookID: bookID, outputDir: outputDir)
            DispatchQueue.main.async {
                sender.isEnabled = true
                guard let result,
                      result["ok"] as? Bool == true
                else {
                    self.statusLabel.stringValue = "导出失败：Reader API 未返回结果"
                    return
                }
                let count = result["annotation_count"] as? Int ?? 0
                let markdownPath = result["markdown_path"] as? String ?? ""
                self.statusLabel.stringValue = "已导出 \(count) 条：\(markdownPath)"
            }
        }
    }

    @objc private func syncCurrentBook(_ sender: NSButton) {
        guard let bookID = readerBookID else {
            statusLabel.stringValue = "Reader API 未连接，无法同步"
            return
        }
        let outputDir = try? appSupportHermesSyncDirectory().path
        statusLabel.stringValue = "正在生成 Hermes/Cognitive OS 同步包..."
        sender.isEnabled = false
        DispatchQueue.global(qos: .utility).async { [readerAPI] in
            let result = readerAPI.syncBookToHermes(bookID: bookID, outputDir: outputDir)
            DispatchQueue.main.async {
                sender.isEnabled = true
                guard let result,
                      result["ok"] as? Bool == true
                else {
                    self.statusLabel.stringValue = "同步失败：Reader API 未返回结果"
                    return
                }
                let count = result["annotation_count"] as? Int ?? 0
                let payloadPath = result["payload_path"] as? String ?? ""
                self.statusLabel.stringValue = "已生成 Hermes 同步包 \(count) 条：\(payloadPath)"
            }
        }
    }

    @objc private func reviewCognitiveQueue(_ sender: NSButton) {
        guard readerAPIAvailable else {
            statusLabel.stringValue = "Reader API 未连接，无法查看认知队列"
            return
        }
        statusLabel.stringValue = "正在检查认知队列和 dry-run..."
        sender.isEnabled = false
        DispatchQueue.global(qos: .utility).async { [readerAPI] in
            let dashboard = readerAPI.cognitiveDashboard()
            let queue = readerAPI.cognitiveReviewQueue()
                let dryRun = readerAPI.cognitiveOperatorDryRun()
                let detail = readerAPI.cognitiveReviewItem()
                DispatchQueue.main.async {
                    sender.isEnabled = true
                    guard let queue,
                      queue["ok"] as? Bool == true
                else {
                    self.statusLabel.stringValue = "认知队列检查失败：Reader API 未返回结果"
                    return
                }
                let counts = queue["counts"] as? [String: Any] ?? [:]
                let ready = counts["ready_to_approve"] as? Int ?? 0
                let needsReview = counts["needs_review"] as? Int ?? 0
                let blocked = counts["blocked"] as? Int ?? 0
                let alreadyPromoted = counts["already_promoted"] as? Int ?? 0
                let selected = dryRun?["selected_count"] as? Int ?? 0
                let markdownPath = queue["markdown_path"] as? String ?? ""
                let dashboardPath = dashboard?["markdown_path"] as? String ?? ""
                let canOpenNativeDashboard = dashboard?["schema"] as? String == "sentence_reader.cognitive_dashboard.v1"
                let queueItem = detail?["queue_item"] as? [String: Any] ?? [:]
                let candidateID = queueItem["candidate_intake_id"] as? String ?? ""
                let itemStatus = queueItem["status"] as? String ?? ""
                let detailMarkdownPath = detail?["markdown_path"] as? String ?? ""
                let canApprove = !candidateID.isEmpty && itemStatus == "ready_to_approve"
                self.statusLabel.stringValue = "认知队列：可批准 \(ready) · 待审 \(needsReview) · 阻塞 \(blocked) · 已入库 \(alreadyPromoted) · dry-run 选中 \(selected)"

                let alert = NSAlert()
                alert.messageText = "认知队列检查完成"
                alert.informativeText = self.statusLabel.stringValue
                var actions: [String] = []
                if canOpenNativeDashboard {
                    alert.addButton(withTitle: "打开仪表盘")
                    actions.append("nativeDashboard")
                } else if !dashboardPath.isEmpty {
                    alert.addButton(withTitle: "打开Markdown仪表盘")
                    actions.append("markdownDashboard")
                }
                if !detailMarkdownPath.isEmpty {
                    alert.addButton(withTitle: "打开详情报告")
                    actions.append("detail")
                } else if !markdownPath.isEmpty {
                    alert.addButton(withTitle: "打开队列报告")
                    actions.append("queue")
                }
                if canApprove {
                    alert.addButton(withTitle: "批准入库...")
                    actions.append("approve")
                }
                alert.addButton(withTitle: "关闭")
                actions.append("close")
                let response = alert.runModal()
                let actionIndex = response.rawValue - NSApplication.ModalResponse.alertFirstButtonReturn.rawValue
                guard actionIndex >= 0, actionIndex < actions.count else {
                    return
                }
                switch actions[actionIndex] {
                case "nativeDashboard":
                    if let dashboard {
                        self.showCognitiveDashboardWindow(dashboard)
                    }
                case "markdownDashboard":
                    NSWorkspace.shared.open(URL(fileURLWithPath: dashboardPath))
                case "detail":
                    NSWorkspace.shared.open(URL(fileURLWithPath: detailMarkdownPath))
                case "queue":
                    NSWorkspace.shared.open(URL(fileURLWithPath: markdownPath))
                case "approve":
                    self.promptCognitiveApproval(candidateID: candidateID, detailPath: detailMarkdownPath)
                default:
                    break
                }
            }
        }
    }

    private func showCognitiveDashboardWindow(_ dashboard: [String: Any]) {
        let controller = CognitiveDashboardWindowController(dashboard: dashboard) { path in
            NSWorkspace.shared.open(URL(fileURLWithPath: path))
        }
        cognitiveDashboardWindowController = controller
        controller.showWindow(nil)
        controller.window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func promptCognitiveApproval(candidateID: String, detailPath: String) {
        let expected = "APPROVE \(candidateID)"
        let alert = NSAlert()
        alert.messageText = "批准认知入库"
        alert.informativeText = "这会把当前 draft 写入 Hermes Cognitive OS formal intakes，并重建 active pack。请先确认详情报告，再手动输入：\(expected)"
        alert.addButton(withTitle: "确认批准")
        alert.addButton(withTitle: "取消")

        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 420, height: 24))
        field.placeholderString = expected
        alert.accessoryView = field

        let response = alert.runModal()
        guard response == .alertFirstButtonReturn else {
            statusLabel.stringValue = "已取消认知入库"
            return
        }
        let confirmation = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard confirmation == expected else {
            statusLabel.stringValue = "确认短语不匹配，已拒绝认知入库"
            return
        }
        approveCognitiveDraft(candidateID: candidateID, confirmation: confirmation, detailPath: detailPath)
    }

    private func approveCognitiveDraft(candidateID: String, confirmation: String, detailPath: String) {
        statusLabel.stringValue = "正在批准认知入库：\(candidateID)"
        DispatchQueue.global(qos: .utility).async { [readerAPI] in
            let result = readerAPI.cognitiveApprove(candidateID: candidateID, confirmationText: confirmation)
            DispatchQueue.main.async {
                guard let result,
                      result["ok"] as? Bool == true
                else {
                    self.statusLabel.stringValue = "认知入库失败：请查看详情报告和 Reader API 日志"
                    if !detailPath.isEmpty {
                        NSWorkspace.shared.open(URL(fileURLWithPath: detailPath))
                    }
                    return
                }
                let reportPath = result["report_path"] as? String ?? ""
                self.statusLabel.stringValue = "认知入库完成：\(candidateID)"
                let alert = NSAlert()
                alert.messageText = "认知入库完成"
                alert.informativeText = reportPath.isEmpty ? candidateID : reportPath
                if !reportPath.isEmpty {
                    alert.addButton(withTitle: "打开报告")
                }
                alert.addButton(withTitle: "关闭")
                let response = alert.runModal()
                if response == .alertFirstButtonReturn, !reportPath.isEmpty {
                    NSWorkspace.shared.open(URL(fileURLWithPath: reportPath))
                }
            }
        }
    }

    private func fileHash(for url: URL) -> String? {
        guard let data = try? Data(contentsOf: url) else {
            return nil
        }
        var hash: UInt64 = 1469598103934665603
        for byte in data {
            hash ^= UInt64(byte)
            hash = hash &* 1099511628211
        }
        return String(format: "reader-%016llx", hash)
    }

    private func unzipEPUBIfNeeded(epubURL: URL, rootURL: URL) -> Bool {
        let containerURL = rootURL.appendingPathComponent("META-INF/container.xml")
        if FileManager.default.fileExists(atPath: containerURL.path) {
            return true
        }
        do {
            try FileManager.default.createDirectory(at: rootURL, withIntermediateDirectories: true)
        } catch {
            return false
        }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/unzip")
        process.arguments = ["-q", epubURL.path, "-d", rootURL.path]
        process.standardOutput = Pipe()
        process.standardError = Pipe()
        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            return false
        }
        return process.terminationStatus == 0 && FileManager.default.fileExists(atPath: containerURL.path)
    }

    private func loadSavedReadingPosition() -> ReadingPosition? {
        if let bookID = readerBookID,
           let row = readerAPI.getPosition(bookID: bookID),
           let chapterLocator = row["chapter_locator"] as? String {
            let locator = row["locator"] as? [String: Any] ?? [:]
            let chapterIndex = (locator["chapterIndex"] as? Int)
                ?? chapters.firstIndex(where: { relativeChapterPath(for: $0) == chapterLocator })
                ?? 0
            return ReadingPosition(
                chapterRelativePath: chapterLocator,
                chapterIndex: chapterIndex,
                pageIndex: row["page_index"] as? Int ?? 0,
                totalPages: row["total_pages"] as? Int ?? 1,
                pageRatio: row["page_ratio"] as? Double ?? 0,
                updatedAt: Date().timeIntervalSince1970
            )
        }

        let storedData = UserDefaults.standard.data(forKey: readingPositionKey())
            ?? (currentBookEntry?.isBundled == true ? UserDefaults.standard.data(forKey: readingPositionKeyPrefix) : nil)
        guard let data = storedData else {
            return nil
        }
        return try? JSONDecoder().decode(ReadingPosition.self, from: data)
    }

    private func chapterIndex(for position: ReadingPosition) -> Int? {
        if let index = chapters.firstIndex(where: { relativeChapterPath(for: $0) == position.chapterRelativePath }) {
            return index
        }
        if chapters.indices.contains(position.chapterIndex) {
            return position.chapterIndex
        }
        return nil
    }

    private func saveReadingPosition(pageIndex: Int, totalPages: Int) {
        guard !suppressReadingPositionSave,
              chapters.indices.contains(currentChapterIndex)
        else {
            return
        }

        let clampedTotal = max(1, totalPages)
        let clampedPage = max(0, min(pageIndex, clampedTotal - 1))
        let denominator = max(1, clampedTotal - 1)
        let position = ReadingPosition(
            chapterRelativePath: relativeChapterPath(for: chapters[currentChapterIndex]),
            chapterIndex: currentChapterIndex,
            pageIndex: clampedPage,
            totalPages: clampedTotal,
            pageRatio: Double(clampedPage) / Double(denominator),
            updatedAt: Date().timeIntervalSince1970
        )

        if let data = try? JSONEncoder().encode(position) {
            UserDefaults.standard.set(data, forKey: readingPositionKey())
        }
        if let bookID = readerBookID {
            let chapterLocator = position.chapterRelativePath
            DispatchQueue.global(qos: .utility).async { [readerAPI = self.readerAPI] in
                readerAPI.savePosition(
                    bookID: bookID,
                    chapterLocator: chapterLocator,
                    chapterIndex: position.chapterIndex,
                    pageIndex: clampedPage,
                    totalPages: clampedTotal,
                    pageRatio: position.pageRatio
                )
            }
        }
    }

    private func readerAPIScriptCandidates() -> [URL] {
        var candidates: [URL] = []
        if let resourceURL = Bundle.main.resourceURL {
            candidates.append(resourceURL.appendingPathComponent("ReaderRuntime/scripts/run_reader_api.sh"))
        }
        candidates.append(URL(fileURLWithPath: FileManager.default.currentDirectoryPath).appendingPathComponent("scripts/run_reader_api.sh"))
        return candidates
    }

    private func readerAPIScriptURL() -> URL? {
        readerAPIScriptCandidates().first { FileManager.default.isExecutableFile(atPath: $0.path) }
    }

    private func launchReaderAPI(host: String) -> Bool {
        guard let scriptURL = readerAPIScriptURL() else {
            readerAPIAvailable = false
            statusLabel.stringValue = "Reader API 启动脚本不可用，暂用本机临时状态"
            return false
        }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = [scriptURL.path]
        var environment = ProcessInfo.processInfo.environment
        environment["READER_API_HOST"] = host
        environment["READER_API_PORT"] = "18180"
        process.environment = environment
        process.standardOutput = Pipe()
        process.standardError = Pipe()
        do {
            try process.run()
            readerAPIProcess = process
        } catch {
            readerAPIAvailable = false
            statusLabel.stringValue = "Reader API 启动失败：\(error.localizedDescription)"
            return false
        }

        for _ in 0..<25 {
            Thread.sleep(forTimeInterval: 0.2)
            if readerAPI.health() {
                readerAPIAvailable = true
                readerAPILANModeEnabled = host == "0.0.0.0"
                statusLabel.stringValue = host == "0.0.0.0" ? "Reader API 局域网模式已连接" : "Reader API 已连接"
                return true
            }
        }
        readerAPIAvailable = false
        statusLabel.stringValue = "Reader API 未连接，暂用本机临时状态"
        return false
    }

    private func ensureReaderAPIAvailable() {
        if readerAPI.health() {
            readerAPIAvailable = true
            statusLabel.stringValue = "Reader API 已连接"
            return
        }

        _ = launchReaderAPI(host: "0.0.0.0")
    }

    private func localLANAddresses() -> [String] {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/sbin/ifconfig")
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = Pipe()
        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            return []
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        let output = String(data: data, encoding: .utf8) ?? ""
        let pattern = #"inet\s+(\d+\.\d+\.\d+\.\d+)"#
        guard let regex = try? NSRegularExpression(pattern: pattern) else {
            return []
        }
        let range = NSRange(output.startIndex..<output.endIndex, in: output)
        var addresses: [String] = []
        for match in regex.matches(in: output, range: range) {
            guard let ipRange = Range(match.range(at: 1), in: output) else {
                continue
            }
            let ip = String(output[ipRange])
            if ip == "127.0.0.1" || ip.hasPrefix("169.254.") || ip.hasPrefix("0.") {
                continue
            }
            if !addresses.contains(ip) {
                addresses.append(ip)
            }
        }
        return addresses
    }

    private func preferredIPadLANReaderURL() -> String {
        let host = localLANAddresses().first ?? "127.0.0.1"
        return "http://\(host):18180/lan/reader"
    }

    private func preferredIPadLibraryURL() -> String {
        let host = localLANAddresses().first ?? "127.0.0.1"
        return "http://\(host):18180/library"
    }

    private func ensureReaderAPILANAvailable() -> Bool {
        if readerAPILANModeEnabled && readerAPI.health() {
            return true
        }
        if let process = readerAPIProcess, process.isRunning {
            process.terminate()
            process.waitUntilExit()
            readerAPIProcess = nil
            Thread.sleep(forTimeInterval: 0.4)
            return launchReaderAPI(host: "0.0.0.0")
        }
        if !readerAPI.health() {
            return launchReaderAPI(host: "0.0.0.0")
        }
        return false
    }

    @objc private func showIPadLANReader(_ sender: NSButton) {
        let lanReady = ensureReaderAPILANAvailable()
        let url = preferredIPadLibraryURL()
        let readerURL = preferredIPadLANReaderURL()
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(url, forType: .string)

        let alert = NSAlert()
        alert.messageText = "iPad 局域网阅读"
        let modeLine = lanReady
            ? "Reader API 已切换到局域网模式。"
            : "Reader API 已响应本机访问，但未确认是局域网模式；如果 iPad 打不开，请退出 Sentence Reader 后重新打开并先点 iPad。"
        alert.informativeText = "\(modeLine)\n\n同一 Wi-Fi 下在 iPad Safari 打开主界面：\n\(url)\n\n直接阅读入口仍可用：\n\(readerURL)\n\n主界面地址已复制到剪贴板。"
        alert.addButton(withTitle: "打开本机主界面")
        alert.addButton(withTitle: "复制地址")
        alert.addButton(withTitle: "关闭")
        alert.beginSheetModal(for: window) { response in
            if response == .alertFirstButtonReturn,
               let localURL = URL(string: "http://127.0.0.1:18180/library") {
                NSWorkspace.shared.open(localURL)
            } else if response == .alertSecondButtonReturn {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(url, forType: .string)
                self.statusLabel.stringValue = "iPad 书库主界面地址已复制"
            }
        }
    }

    private func relativeChapterPath(for url: URL) -> String {
        guard let bookRootURL else {
            return url.lastPathComponent
        }

        let rootPath = bookRootURL.standardizedFileURL.path
        let chapterPath = url.standardizedFileURL.path
        if chapterPath.hasPrefix(rootPath + "/") {
            return String(chapterPath.dropFirst(rootPath.count + 1))
        }
        return url.lastPathComponent
    }

    private func chapterDisplayTitle(for url: URL, index: Int) -> String {
        let fallback = "第 \(index + 1) 章"
        guard let document = try? XMLDocument(contentsOf: url, options: []) else {
            return fallback
        }

        if let heading = firstMeaningfulText(
            in: document,
            xpath: "//*[local-name()='h1' or local-name()='h2' or local-name()='h3']",
            includeTitleAttribute: true
        ) {
            return heading
        }

        if let title = firstMeaningfulText(in: document, xpath: "//*[local-name()='title']", includeTitleAttribute: false) {
            return title
        }

        return fallback
    }

    private func firstMeaningfulText(in document: XMLDocument, xpath: String, includeTitleAttribute: Bool) -> String? {
        let nodes = (try? document.nodes(forXPath: xpath))?.compactMap { $0 as? XMLElement } ?? []
        for node in nodes {
            var text = node.stringValue ?? ""
            if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
               includeTitleAttribute,
               let title = node.attribute(forName: "title")?.stringValue {
                text = title
            }

            let cleaned = cleanChapterTitle(text)
            if !cleaned.isEmpty && cleaned != "■ ■ ■" && cleaned != "■■■" {
                return cleaned
            }
        }

        return nil
    }

    private func cleanChapterTitle(_ text: String) -> String {
        text
            .replacingOccurrences(of: "\n", with: " ")
            .replacingOccurrences(of: "\r", with: " ")
            .replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func collectTOCEntries(in root: URL, chapters: [URL]) -> [TocEntry] {
        let ncxURLs = allFiles(in: root).filter { $0.pathExtension.lowercased() == "ncx" }
        for ncxURL in ncxURLs {
            guard let document = try? XMLDocument(contentsOf: ncxURL, options: []),
                  let navMap = firstElement(in: document, localName: "navMap")
            else {
                continue
            }

            let baseURL = ncxURL.deletingLastPathComponent()
            var entries: [TocEntry] = []
            for navPoint in childElements(of: navMap, localName: "navPoint") {
                appendTOCEntries(from: navPoint, baseURL: baseURL, chapters: chapters, level: 0, into: &entries)
            }
            if !entries.isEmpty {
                return entries
            }
        }

        return []
    }

    private func allFiles(in root: URL) -> [URL] {
        guard let enumerator = FileManager.default.enumerator(at: root, includingPropertiesForKeys: nil) else {
            return []
        }

        var urls: [URL] = []
        for case let url as URL in enumerator {
            urls.append(url)
        }
        return urls
    }

    private func appendTOCEntries(from navPoint: XMLElement, baseURL: URL, chapters: [URL], level: Int, into entries: inout [TocEntry]) {
        let labelText = firstElement(in: navPoint, localName: "text")?.stringValue ?? ""
        let title = cleanChapterTitle(labelText)
        let src = firstElement(in: navPoint, localName: "content")?.attribute(forName: "src")?.stringValue ?? ""
        let href = src.components(separatedBy: "#").first ?? src
        let targetURL = baseURL.appendingPathComponent(href.removingPercentEncoding ?? href).standardizedFileURL

        if !title.isEmpty,
           let chapterIndex = nearestChapterIndex(for: targetURL, chapters: chapters) {
            entries.append(TocEntry(title: title, chapterIndex: chapterIndex, level: level))
        }

        for child in childElements(of: navPoint, localName: "navPoint") {
            appendTOCEntries(from: child, baseURL: baseURL, chapters: chapters, level: level + 1, into: &entries)
        }
    }

    private func nearestChapterIndex(for targetURL: URL, chapters: [URL]) -> Int? {
        if let exact = chapters.firstIndex(where: { $0.standardizedFileURL == targetURL }) {
            return exact
        }

        let targetPath = targetURL.path
        return chapters.indices.first { index in
            chapters[index].path == targetPath
        }
    }

    private func firstElement(in document: XMLDocument, localName: String) -> XMLElement? {
        (try? document.nodes(forXPath: "//*[local-name()='\(localName)']"))?.compactMap { $0 as? XMLElement }.first
    }

    private func firstElement(in element: XMLElement, localName: String) -> XMLElement? {
        if elementMatches(element, localName: localName) {
            return element
        }
        for child in childElements(of: element) {
            if let match = firstElement(in: child, localName: localName) {
                return match
            }
        }
        return nil
    }

    private func childElements(of element: XMLElement, localName: String? = nil) -> [XMLElement] {
        let children = element.children?.compactMap { $0 as? XMLElement } ?? []
        guard let localName else {
            return children
        }
        return children.filter { elementMatches($0, localName: localName) }
    }

    private func elementMatches(_ element: XMLElement, localName: String) -> Bool {
        guard let name = element.name else {
            return false
        }
        return name == localName || name.hasSuffix(":\(localName)")
    }

    private func collectHTMLChapters(in root: URL) -> [URL] {
        if let spineChapters = collectOPFSpineChapters(in: root), !spineChapters.isEmpty {
            return spineChapters
        }

        guard let enumerator = FileManager.default.enumerator(at: root, includingPropertiesForKeys: nil) else {
            return []
        }

        var urls: [URL] = []
        for case let url as URL in enumerator {
            let name = url.lastPathComponent.lowercased()
            guard name.hasSuffix(".xhtml") || name.hasSuffix(".html") else {
                continue
            }
            if name.contains("cover") || name.contains("nav") || name.contains("toc") {
                continue
            }
            urls.append(url)
        }

        return urls.sorted { lhs, rhs in
            lhs.path.localizedStandardCompare(rhs.path) == .orderedAscending
        }
    }

    private func collectOPFSpineChapters(in root: URL) -> [URL]? {
        let containerURL = root.appendingPathComponent("META-INF/container.xml")
        guard let container = try? XMLDocument(contentsOf: containerURL, options: []) else {
            return nil
        }

        guard let rootfile = (try? container.nodes(forXPath: "//*[local-name()='rootfile']"))?.compactMap({ $0 as? XMLElement }).first,
              let packagePath = rootfile.attribute(forName: "full-path")?.stringValue,
              !packagePath.isEmpty
        else {
            return nil
        }

        let packageURL = root.appendingPathComponent(packagePath)
        guard let package = try? XMLDocument(contentsOf: packageURL, options: []) else {
            return nil
        }

        let baseURL = packageURL.deletingLastPathComponent()
        let manifestItems = (try? package.nodes(forXPath: "//*[local-name()='manifest']/*[local-name()='item']"))?.compactMap { $0 as? XMLElement } ?? []
        var manifest: [String: (href: String, mediaType: String)] = [:]
        for item in manifestItems {
            guard let id = item.attribute(forName: "id")?.stringValue,
                  let href = item.attribute(forName: "href")?.stringValue
            else {
                continue
            }
            manifest[id] = (href, item.attribute(forName: "media-type")?.stringValue ?? "")
        }

        let spineItems = (try? package.nodes(forXPath: "//*[local-name()='spine']/*[local-name()='itemref']"))?.compactMap { $0 as? XMLElement } ?? []
        let spineURLs = spineItems.compactMap { item -> URL? in
            if item.attribute(forName: "linear")?.stringValue?.lowercased() == "no" {
                return nil
            }
            guard let idref = item.attribute(forName: "idref")?.stringValue,
                  let entry = manifest[idref]
            else {
                return nil
            }

            let href = entry.href.removingPercentEncoding ?? entry.href
            let lowerHref = href.lowercased()
            guard entry.mediaType == "application/xhtml+xml" || lowerHref.hasSuffix(".xhtml") || lowerHref.hasSuffix(".html") else {
                return nil
            }
            return baseURL.appendingPathComponent(href).standardizedFileURL
        }

        return spineURLs.filter { FileManager.default.fileExists(atPath: $0.path) }
    }

    private func loadChapter(at index: Int, initialPage: InitialPage) {
        guard let bookRootURL,
              chapters.indices.contains(index)
        else {
            return
        }

        currentChapterIndex = index
        pendingInitialPage = initialPage
        suppressReadingPositionSave = true
        redLabel.stringValue = "红标 0"
        statusLabel.stringValue = "正在打开第 \(index + 1) / \(chapters.count) 章"
        webView.loadFileURL(chapters[index], allowingReadAccessTo: bookRootURL)
    }

    private func refreshNotes() {
        guard let bookID = readerBookID else {
            allNoteRows = []
            applyNotesFilter()
            return
        }

        DispatchQueue.global(qos: .utility).async { [readerAPI] in
            let annotations = readerAPI.listAnnotations(bookID: bookID)
            let rows = annotations.compactMap { self.noteRow(from: $0) }
            DispatchQueue.main.async {
                self.allNoteRows = rows
                self.applyNotesFilter()
            }
        }
    }

    private func noteRow(from annotation: [String: Any]) -> NoteRow? {
        guard let id = annotation["id"] as? String,
              let kind = annotation["kind"] as? String,
              let sourceText = annotation["source_text"] as? String,
              let chapterLocator = annotation["chapter_locator"] as? String
        else {
            return nil
        }

        let range = annotation["range_locator"] as? [String: Any] ?? [:]
        let metadata = annotation["metadata"] as? [String: Any] ?? [:]
        let sentenceIndex = sentenceIndexString(from: range["sentenceIndex"] ?? metadata["sentenceIndex"]) ?? ""

        return NoteRow(
            id: id,
            kind: kind,
            sourceText: sourceText,
            noteText: annotation["note_text"] as? String ?? "",
            chapterTitle: annotation["chapter_title"] as? String ?? chapterLocator,
            chapterLocator: chapterLocator,
            sentenceIndex: sentenceIndex
        )
    }

    private func applyNotesFilter() {
        let query = (notesSearchField?.stringValue ?? "").trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let selected = notesFilterControl?.selectedSegment ?? 0
        visibleNoteRows = allNoteRows.filter { row in
            if selected == 1 && row.kind != "note" {
                return false
            }
            if selected == 2 && row.kind != "red_highlight" {
                return false
            }
            if query.isEmpty {
                return true
            }
            return row.primaryText.lowercased().contains(query)
                || row.secondaryText.lowercased().contains(query)
                || row.chapterTitle.lowercased().contains(query)
        }
        notesTableView?.reloadData()
        let noteCount = allNoteRows.filter { $0.kind == "note" }.count
        let redCount = allNoteRows.filter { $0.kind == "red_highlight" }.count
        notesSummaryLabel?.stringValue = "\(visibleNoteRows.count) 条 · 备注 \(noteCount) · 红标 \(redCount)"
    }

    @objc private func toggleNotesRail(_ sender: NSButton) {
        let shouldShow = notesRailWidthConstraint.constant <= 1
        setNotesRailVisible(shouldShow)
    }

    private func setNotesRailVisible(_ visible: Bool) {
        notesRailWidthConstraint.constant = visible ? 304 : 0
        notesRail.isHidden = !visible
        notesRailToggleButton?.title = "收起笔记"
        notesButton.title = visible ? "隐藏笔记" : "笔记"
        window.contentView?.layoutSubtreeIfNeeded()
    }

    @objc private func notesFilterChanged(_ sender: NSSegmentedControl) {
        applyNotesFilter()
    }

    func controlTextDidChange(_ obj: Notification) {
        applyNotesFilter()
    }

    func numberOfRows(in tableView: NSTableView) -> Int {
        if let libraryTableView, tableView === libraryTableView {
            return bookEntries.count
        }
        return visibleNoteRows.count
    }

    func tableView(_ tableView: NSTableView, heightOfRow row: Int) -> CGFloat {
        if let libraryTableView, tableView === libraryTableView {
            return 72
        }
        return 76
    }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        if let libraryTableView, tableView === libraryTableView {
            return makeLibraryBookCell(row: row, tableView: tableView)
        }

        guard visibleNoteRows.indices.contains(row) else {
            return nil
        }

        let item = visibleNoteRows[row]
        let container = NSView()
        let kind = NSTextField(labelWithString: "\(item.kindTitle) · \(item.chapterTitle)")
        kind.font = NSFont(name: "Microsoft YaHei", size: 11) ?? NSFont.systemFont(ofSize: 11, weight: .medium)
        kind.textColor = item.isRedHighlight ? NSColor.systemRed : NSColor.systemBlue

        let primary = NSTextField(labelWithString: item.primaryText)
        primary.font = NSFont(name: "Microsoft YaHei", size: 12) ?? NSFont.systemFont(ofSize: 12)
        primary.textColor = .labelColor
        primary.maximumNumberOfLines = 2
        primary.lineBreakMode = .byTruncatingTail

        let source = NSTextField(labelWithString: item.secondaryText)
        source.font = NSFont(name: "Microsoft YaHei", size: 10) ?? NSFont.systemFont(ofSize: 10)
        source.textColor = .secondaryLabelColor
        source.maximumNumberOfLines = 1
        source.lineBreakMode = .byTruncatingTail

        let stack = NSStackView(views: [kind, primary, source])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 3
        stack.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(stack)

        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: container.topAnchor, constant: 6),
            stack.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 8),
            stack.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -8),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: container.bottomAnchor, constant: -6),
        ])
        return container
    }

    @objc private func openSelectedNote(_ sender: Any?) {
        let row = notesTableView.selectedRow
        guard visibleNoteRows.indices.contains(row) else {
            return
        }
        jumpToNote(visibleNoteRows[row])
    }

    @objc private func editSelectedNote(_ sender: Any?) {
        let row = notesTableView.selectedRow
        guard visibleNoteRows.indices.contains(row) else {
            statusLabel.stringValue = "请选择一条笔记"
            return
        }

        let item = visibleNoteRows[row]
        let alert = NSAlert()
        alert.messageText = item.isRedHighlight ? "编辑红标备注" : "编辑备注"
        alert.informativeText = item.sourceText
        alert.addButton(withTitle: "保存")
        alert.addButton(withTitle: "取消")

        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 520, height: 150))
        let textView = NSTextView(frame: scroll.bounds)
        textView.font = NSFont(name: "Microsoft YaHei", size: 14) ?? NSFont.systemFont(ofSize: 14)
        textView.string = item.noteText
        scroll.documentView = textView
        scroll.hasVerticalScroller = true
        alert.accessoryView = scroll

        alert.beginSheetModal(for: window) { response in
            guard response == .alertFirstButtonReturn else {
                return
            }
            let updated = NoteTextNormalizer.normalized(textView.string)
            self.statusLabel.stringValue = "正在保存笔记到 Reader API..."
            DispatchQueue.global(qos: .utility).async { [readerAPI = self.readerAPI] in
                let ok = readerAPI.updateAnnotation(annotationID: item.id, noteText: updated, color: item.isRedHighlight ? "red" : nil)
                DispatchQueue.main.async {
                    self.statusLabel.stringValue = ok ? "笔记已保存到 Reader API" : "笔记保存失败"
                    self.refreshNotes()
                    self.restoreAnnotationsForCurrentChapter()
                }
            }
        }
    }

    @objc private func deleteSelectedNote(_ sender: Any?) {
        let row = notesTableView.selectedRow
        guard visibleNoteRows.indices.contains(row) else {
            statusLabel.stringValue = "请选择一条笔记"
            return
        }

        let item = visibleNoteRows[row]
        let alert = NSAlert()
        alert.messageText = "删除这条\(item.kindTitle)？"
        alert.informativeText = item.primaryText
        alert.addButton(withTitle: "删除")
        alert.addButton(withTitle: "取消")
        alert.alertStyle = .warning
        alert.beginSheetModal(for: window) { response in
            guard response == .alertFirstButtonReturn else {
                return
            }
            self.statusLabel.stringValue = "正在删除\(item.kindTitle)..."
            DispatchQueue.global(qos: .utility).async { [readerAPI = self.readerAPI] in
                let ok = readerAPI.deleteAnnotation(annotationID: item.id)
                DispatchQueue.main.async {
                    if ok {
                        for sentenceIndex in self.sentenceIndexList(from: item.sentenceIndex) {
                            self.redAnnotationIDs[self.annotationKey(chapterLocator: item.chapterLocator, sentenceIndex: sentenceIndex)] = nil
                        }
                    }
                    self.restoreAnnotationsForCurrentChapter()
                    self.statusLabel.stringValue = ok ? "已删除\(item.kindTitle)" : "\(item.kindTitle)删除失败，已恢复数据库状态"
                    self.refreshNotes()
                }
            }
        }
    }

    private func jumpToNote(_ item: NoteRow) {
        guard !item.sentenceIndex.isEmpty else {
            statusLabel.stringValue = "这条笔记没有句子定位"
            return
        }

        if currentChapterLocator() == item.chapterLocator {
            jumpToSentence(index: item.sentenceIndex)
            return
        }
        guard let chapterIndex = chapters.firstIndex(where: { relativeChapterPath(for: $0) == item.chapterLocator }) else {
            statusLabel.stringValue = "没有找到笔记所在章节"
            return
        }
        pendingNoteJumpIndex = item.sentenceIndex
        loadChapter(at: chapterIndex, initialPage: .start)
    }

    private func jumpToSentence(index: String) {
        let targetIndex = sentenceIndexList(from: index).first ?? index
        guard let data = try? JSONSerialization.data(withJSONObject: [targetIndex]),
              let json = String(data: data, encoding: .utf8)
        else {
            return
        }
        webView.evaluateJavaScript("window.__sentenceReaderFocusSentence && window.__sentenceReaderFocusSentence(\(json)[0]);")
        statusLabel.stringValue = "已跳回笔记原句"
    }

    private func turnChapter(direction: Int) {
        let target = currentChapterIndex + direction
        guard chapters.indices.contains(target) else {
            statusLabel.stringValue = direction > 0 ? "已经到最后一章" : "已经到第一章"
            return
        }

        loadChapter(at: target, initialPage: direction > 0 ? .start : .end)
    }

    @objc private func showContents(_ sender: NSButton) {
        let entries = tocEntries.isEmpty ? fallbackTOCEntries() : tocEntries
        guard !entries.isEmpty else {
            statusLabel.stringValue = "这本书没有可用目录"
            return
        }

        let menu = NSMenu(title: "目录")
        menu.autoenablesItems = false

        for entry in entries {
            let visibleLevel = min(max(entry.level, 0), 6)
            let visibleTitle = String(repeating: "    ", count: visibleLevel) + entry.title
            let item = NSMenuItem(title: visibleTitle, action: #selector(selectChapterFromContents(_:)), keyEquivalent: "")
            item.target = self
            item.tag = entry.chapterIndex
            item.indentationLevel = visibleLevel
            item.toolTip = entry.title
            item.state = entry.chapterIndex == currentChapterIndex ? .on : .off
            item.isEnabled = true
            menu.addItem(item)
        }

        menu.popUp(positioning: menu.items.first, at: NSPoint(x: 0, y: sender.bounds.maxY + 4), in: sender)
    }

    @objc private func selectChapterFromContents(_ sender: NSMenuItem) {
        loadChapter(at: sender.tag, initialPage: .start)
    }

    private func fallbackTOCEntries() -> [TocEntry] {
        chapters.indices.map { index in
            let title = chapterTitles.indices.contains(index) ? chapterTitles[index] : "第 \(index + 1) 章"
            return TocEntry(title: title, chapterIndex: index, level: 0)
        }
    }

    private func installCommandUndoMonitor() {
        undoEventMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self else {
                return event
            }

            let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
            let key = event.charactersIgnoringModifiers?.lowercased() ?? ""

            if flags.contains(.command), key == "q" {
                NSApp.terminate(nil)
                return nil
            }

            guard self.window?.attachedSheet == nil,
                  flags.contains(.command),
                  key == "z"
            else {
                return event
            }

            self.webView.evaluateJavaScript("window.__sentenceReaderUndo && window.__sentenceReaderUndo();")
            return nil
        }
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let payload = message.body as? [String: Any],
              let type = payload["type"] as? String
        else {
            return
        }

        let text = (payload["text"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        DispatchQueue.main.async {
            switch type {
            case "ready":
                let count = payload["sentenceCount"] as? Int ?? 0
                self.statusLabel.stringValue = "第 \(self.currentChapterIndex + 1) / \(self.chapters.count) 章 · 句子 \(count)"
                let initialPage = self.pendingInitialPage
                self.pendingInitialPage = .start
                self.applyReaderSettingsToWebView()
                self.suppressReadingPositionSave = false
                switch initialPage {
                case .start:
                    self.webView.evaluateJavaScript("window.__sentenceReaderRestorePage && window.__sentenceReaderRestorePage(0, 0, 1);")
                case .end:
                    self.webView.evaluateJavaScript("window.__sentenceReaderGoToEnd && window.__sentenceReaderGoToEnd();")
                case let .position(pageIndex, pageRatio, totalPages):
                    self.webView.evaluateJavaScript("window.__sentenceReaderRestorePage && window.__sentenceReaderRestorePage(\(pageIndex), \(pageRatio), \(totalPages));")
                }
                self.restoreAnnotationsForCurrentChapter()
                if let pendingIndex = self.pendingNoteJumpIndex {
                    self.pendingNoteJumpIndex = nil
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) {
                        self.jumpToSentence(index: pendingIndex)
                    }
                }
            case "focus":
                self.statusLabel.stringValue = text.isEmpty ? "已聚焦当前句" : "已聚焦：\(text)"
            case "note":
                self.statusLabel.stringValue = "正在为当前句添加备注"
                self.showNotePanel(sentence: text, sentenceIndex: self.sentenceIndexPayload(from: payload))
            case "lookup":
                self.showLookupPanel(
                    word: payload["word"] as? String ?? "",
                    sentence: text,
                    sentenceIndex: self.sentenceIndexPayload(from: payload)
                )
            case "notePreview":
                self.showNotePreview(
                    sourceText: text,
                    noteText: payload["noteText"] as? String ?? "",
                    annotationID: payload["noteID"] as? String ?? ""
                )
            case "red":
                let count = payload["redCount"] as? Int ?? 0
                self.redLabel.stringValue = "红标 \(count)"
                self.statusLabel.stringValue = (payload["isRed"] as? Bool ?? true) ? "正在保存红标..." : "正在取消红标..."
                self.persistRed(
                    sentence: text,
                    sentenceIndex: self.sentenceIndexPayload(from: payload),
                    isRed: payload["isRed"] as? Bool ?? true
                )
            case "undo":
                let count = payload["redCount"] as? Int ?? 0
                self.redLabel.stringValue = "红标 \(count)"
                self.statusLabel.stringValue = text.isEmpty ? "已撤销上一步" : "已撤销：\(text)"
                if payload["actionType"] as? String == "red" {
                    self.statusLabel.stringValue = "正在保存撤销结果..."
                    self.persistRed(
                        sentence: text,
                        sentenceIndex: self.sentenceIndexPayload(from: payload),
                        isRed: payload["isRed"] as? Bool ?? false
                    )
                }
            case "annotationsRestored":
                let count = payload["redCount"] as? Int ?? 0
                self.redLabel.stringValue = "红标 \(count)"
            case "page":
                let page = payload["page"] as? Int ?? 1
                let total = payload["total"] as? Int ?? 1
                let pageIndex = payload["pageIndex"] as? Int ?? max(0, page - 1)
                self.statusLabel.stringValue = "第 \(self.currentChapterIndex + 1) / \(self.chapters.count) 章 · 第 \(page) / \(total) 页"
                self.saveReadingPosition(pageIndex: pageIndex, totalPages: total)
            case "edge":
                let direction = payload["direction"] as? String
                self.turnChapter(direction: direction == "previous" ? -1 : 1)
            default:
                break
            }
        }
    }

    private func showNotePanel(sentence: String) {
        showNotePanel(sentence: sentence, sentenceIndex: "")
    }

    private func showNotePanel(sentence: String, sentenceIndex: String) {
        let alert = NSAlert()
        alert.messageText = "添加备注"
        alert.informativeText = sentence
        alert.addButton(withTitle: "保存")
        alert.addButton(withTitle: "取消")

        let accessory = NSView(frame: NSRect(x: 0, y: 0, width: 520, height: 178))
        let scroll = NSScrollView(frame: NSRect(x: 0, y: 44, width: 520, height: 134))
        let textView = NSTextView(frame: scroll.bounds)
        textView.font = NSFont(name: "Microsoft YaHei", size: 14) ?? NSFont.systemFont(ofSize: 14)
        scroll.documentView = textView
        scroll.hasVerticalScroller = true

        let recordButton = NSButton(title: "开始录音", target: nil, action: nil)
        recordButton.bezelStyle = .rounded
        recordButton.frame = NSRect(x: 0, y: 6, width: 92, height: 28)

        let providerPopup = NSPopUpButton(frame: NSRect(x: 104, y: 6, width: 92, height: 28), pullsDown: false)
        providerPopup.addItems(withTitles: [SpeechTranscriptionProvider.funASR.title, SpeechTranscriptionProvider.appleSpeech.title])
        providerPopup.selectItem(withTitle: SpeechTranscriptionProvider.current.title)

        let speechStatus = NSTextField(labelWithString: "")
        speechStatus.frame = NSRect(x: 208, y: 8, width: 312, height: 22)
        speechStatus.font = NSFont(name: "Microsoft YaHei", size: 11) ?? NSFont.systemFont(ofSize: 11)
        speechStatus.textColor = NSColor.secondaryLabelColor
        speechStatus.lineBreakMode = .byTruncatingTail

        let speechController = NoteSpeechController(
            textView: textView,
            statusLabel: speechStatus,
            recordButton: recordButton,
            providerPopup: providerPopup,
            bookID: readerBookID,
            readerAPI: readerAPI
        )
        recordButton.target = speechController
        recordButton.action = #selector(NoteSpeechController.toggleRecording(_:))
        providerPopup.target = speechController
        providerPopup.action = #selector(NoteSpeechController.providerChanged(_:))
        noteSpeechController = speechController

        accessory.addSubview(scroll)
        accessory.addSubview(recordButton)
        accessory.addSubview(providerPopup)
        accessory.addSubview(speechStatus)
        alert.accessoryView = accessory
        alert.beginSheetModal(for: window) { response in
            let audioNoteID = self.noteSpeechController?.latestAudioNoteID
            self.noteSpeechController?.cancel()
            self.noteSpeechController = nil
            if response == .alertFirstButtonReturn {
                let noteText = NoteTextNormalizer.normalized(textView.string)
                self.persistNote(sentence: sentence, sentenceIndex: sentenceIndex, noteText: noteText, audioNoteID: audioNoteID)
            }
        }
    }

    private func showLookupPanel(word: String, sentence: String, sentenceIndex: String) {
        let trimSet = CharacterSet.whitespacesAndNewlines.union(CharacterSet(charactersIn: "\"'.,;:!?()[]{}<>“”‘’"))
        let cleanedWord = word.trimmingCharacters(in: trimSet)
        guard !cleanedWord.isEmpty else {
            showNotePanel(sentence: sentence, sentenceIndex: sentenceIndex)
            return
        }
        guard let bookID = readerBookID, !bookID.isEmpty else {
            statusLabel.stringValue = "Reader API 未连接，无法查词"
            return
        }

        statusLabel.stringValue = "正在查词：\(cleanedWord)"
        DispatchQueue.global(qos: .userInitiated).async { [readerAPI] in
            let payload = readerAPI.lookupWord(bookID: bookID, word: cleanedWord, sentenceIndex: sentenceIndex)
            let lemma = Self.lookupLemma(from: payload) ?? cleanedWord.lowercased()
            readerAPI.createLookupEvent(
                bookID: bookID,
                surface: "mac-native",
                lemma: lemma,
                sentenceIndex: sentenceIndex,
                sentence: sentence
            )
            DispatchQueue.main.async {
                self.showLookupAlert(
                    bookID: bookID,
                    word: cleanedWord,
                    sentence: sentence,
                    sentenceIndex: sentenceIndex,
                    payload: payload
                )
            }
        }
    }

    private func showLookupAlert(bookID: String, word: String, sentence: String, sentenceIndex: String, payload: [String: Any]?) {
        guard window.attachedSheet == nil else {
            statusLabel.stringValue = "已有弹窗打开，查词结果未显示"
            return
        }

        let item = payload?["item"] as? [String: Any]
        let occurrence = Self.firstLookupOccurrence(from: payload)
        let displayWord = Self.lookupText(item?["surface"]).isEmpty ? word : Self.lookupText(item?["surface"])
        let meaning = Self.lookupText(item?["context_meaning_zh"])
        let meaningSource = Self.lookupText(item?["meaning_source"])
        let metadata = item?["metadata"] as? [String: Any]
        let rawPartOfSpeechZH = Self.lookupText(item?["part_of_speech_zh"]).isEmpty
            ? Self.lookupText(metadata?["part_of_speech_zh"])
            : Self.lookupText(item?["part_of_speech_zh"])
        let rawPartOfSpeech = Self.lookupText(item?["part_of_speech"]).isEmpty
            ? Self.lookupText(metadata?["part_of_speech"])
            : Self.lookupText(item?["part_of_speech"])
        let partOfSpeechTitle = Self.lookupPartOfSpeechTitle(primary: rawPartOfSpeechZH, fallback: rawPartOfSpeech)
        let popupSpeakText = Self.lookupText(item?["popup_speak_text_zh"]).isEmpty
            ? Self.lookupText(metadata?["popup_speak_text_zh"])
            : Self.lookupText(item?["popup_speak_text_zh"])
        let alignmentStatus = Self.lookupText(item?["alignment_status"])
        let itemStatus = Self.lookupText(item?["status"])
        let occurrenceCount = Self.lookupText(item?["occurrence_count"])
        let sourceTitle = Self.lookupText(metadata?["source_title"])
        let sourceVolume = Self.lookupText(metadata?["volume"])
        let sourcePage = Self.lookupText(metadata?["source_page"])
        let representativeEN = Self.lookupText(occurrence?["english_sentence"]).isEmpty
            ? (Self.lookupText(item?["representative_sentence_en"]).isEmpty ? sentence : Self.lookupText(item?["representative_sentence_en"]))
            : Self.lookupText(occurrence?["english_sentence"])
        let representativeZH = Self.lookupText(occurrence?["chinese_sentence"]).isEmpty
            ? Self.lookupText(item?["representative_sentence_zh"])
            : Self.lookupText(occurrence?["chinese_sentence"])

        let partOfSpeechShortTitle = Self.lookupPartOfSpeechShortTitle(primary: rawPartOfSpeechZH, fallback: rawPartOfSpeech)
        var meaningLines: [String] = []
        if item == nil {
            meaningLines.append("未收录。可以先读词，或回到原句添加备注。")
        } else {
            let compactMeaning = [partOfSpeechShortTitle, meaning.isEmpty ? "未确认释义" : meaning]
                .filter { !$0.isEmpty }
                .joined(separator: " ")
            meaningLines.append(compactMeaning.isEmpty ? "未确认释义" : compactMeaning)
        }

        var evidenceLines: [String] = []
        if !meaningSource.isEmpty {
            evidenceLines.append("来源：\(Self.lookupMeaningSourceTitle(meaningSource))")
        }
        if !sourceTitle.isEmpty {
            evidenceLines.append("批次：\(sourceTitle)")
        }
        if !sourceVolume.isEmpty || !sourcePage.isEmpty {
            let pageText = sourcePage.isEmpty ? "" : "第 \(sourcePage) 页"
            evidenceLines.append("出处：\([sourceVolume, pageText].filter { !$0.isEmpty }.joined(separator: " · "))")
        }
        if !alignmentStatus.isEmpty {
            evidenceLines.append("对齐：\(Self.lookupAlignmentTitle(alignmentStatus))")
        }
        if !itemStatus.isEmpty {
            evidenceLines.append("状态：\(Self.lookupStatusTitle(itemStatus))")
        }
        if !occurrenceCount.isEmpty {
            evidenceLines.append("本书出现：\(occurrenceCount) 次")
        }
        if !representativeEN.isEmpty {
            evidenceLines.append("")
            evidenceLines.append("英文上下文：")
            evidenceLines.append(representativeEN)
        }
        if !representativeZH.isEmpty {
            evidenceLines.append("")
            evidenceLines.append("中文证据：")
            evidenceLines.append(representativeZH)
        }
        if evidenceLines.isEmpty {
            evidenceLines.append("暂无更多证据。")
        }

        let alert = NSAlert()
        alert.messageText = ""
        alert.informativeText = ""

        let accessory = NSView(frame: NSRect(x: 0, y: 0, width: 430, height: 172))
        lookupActionTargets.removeAll()
        func makeLookupActionButton(title: String, symbolName: String?, frame: NSRect, action: @escaping () -> Void) -> NSButton {
            let target = LookupActionTarget(action: action)
            lookupActionTargets.append(target)
            let button = NSButton(title: title, target: target, action: #selector(LookupActionTarget.perform(_:)))
            button.frame = frame
            button.bezelStyle = .rounded
            button.font = NSFont(name: "Microsoft YaHei", size: 13) ?? NSFont.systemFont(ofSize: 13, weight: .medium)
            if let symbolName,
               #available(macOS 11.0, *),
               let image = NSImage(systemSymbolName: symbolName, accessibilityDescription: title) {
                button.image = image
                button.imagePosition = .imageLeading
            }
            return button
        }

        let wordTarget = LookupActionTarget { [weak self] in
            self?.statusLabel.stringValue = "已点击单词，准备朗读：\(displayWord)"
            self?.speakEnglish(displayWord)
        }
        lookupActionTargets.append(wordTarget)
        let clickableWordButton = LookupWordButton(title: displayWord, target: wordTarget, action: #selector(LookupActionTarget.perform(_:)))
        clickableWordButton.frame = NSRect(x: 0, y: 118, width: 430, height: 52)
        clickableWordButton.setButtonType(.momentaryPushIn)
        clickableWordButton.bezelStyle = .rounded
        clickableWordButton.isBordered = true
        clickableWordButton.wantsLayer = true
        clickableWordButton.layer?.cornerRadius = 8
        clickableWordButton.layer?.backgroundColor = NSColor.controlAccentColor.withAlphaComponent(0.08).cgColor
        clickableWordButton.toolTip = "点击朗读单词"
        if #available(macOS 11.0, *),
           let image = NSImage(systemSymbolName: "speaker.wave.2.fill", accessibilityDescription: "朗读") {
            clickableWordButton.image = image
            clickableWordButton.imagePosition = .imageTrailing
        }
        clickableWordButton.font = NSFont(name: "Microsoft YaHei", size: 34) ?? NSFont.systemFont(ofSize: 34, weight: .bold)
        clickableWordButton.alignment = .center
        clickableWordButton.contentTintColor = NSColor.labelColor
        accessory.addSubview(clickableWordButton)

        let scroll = NSScrollView(frame: NSRect(x: 0, y: 48, width: 430, height: 64))
        let textView = NSTextView(frame: scroll.bounds)
        textView.font = NSFont(name: "Microsoft YaHei", size: 19) ?? NSFont.systemFont(ofSize: 19, weight: .medium)
        textView.string = meaningLines.joined(separator: "\n")
        textView.isEditable = false
        textView.isSelectable = true
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = false
        textView.autoresizingMask = [.width]
        textView.textContainer?.containerSize = NSSize(width: scroll.contentSize.width, height: CGFloat.greatestFiniteMagnitude)
        textView.textContainer?.widthTracksTextView = true
        textView.alignment = .center
        textView.textContainerInset = NSSize(width: 0, height: 17)
        textView.drawsBackground = false
        textView.textColor = NSColor.labelColor
        scroll.documentView = textView
        scroll.hasVerticalScroller = false
        scroll.borderType = .noBorder
        accessory.addSubview(scroll)

        let speakMeaning = popupSpeakText.isEmpty
            ? [partOfSpeechTitle, meaning].filter { !$0.isEmpty }.joined(separator: "，")
            : popupSpeakText

        let buttonY: CGFloat = 8
        let readMeaningButton = makeLookupActionButton(
            title: "读释义",
            symbolName: "text.bubble.fill",
            frame: NSRect(x: 0, y: buttonY, width: 92, height: 30)
        ) { [weak self] in
            self?.speakChineseMeaning(speakMeaning)
        }
        accessory.addSubview(readMeaningButton)

        if item != nil {
            var showingEvidence = false
            var evidenceButton: NSButton?
            evidenceButton = makeLookupActionButton(
                title: "证据",
                symbolName: "doc.text.magnifyingglass",
                frame: NSRect(x: 100, y: buttonY, width: 82, height: 30)
            ) {
                showingEvidence.toggle()
                textView.string = showingEvidence ? evidenceLines.joined(separator: "\n") : meaningLines.joined(separator: "\n")
                scroll.hasVerticalScroller = showingEvidence
                textView.alignment = showingEvidence ? .left : .center
                textView.font = showingEvidence
                    ? (NSFont(name: "Microsoft YaHei", size: 14) ?? NSFont.systemFont(ofSize: 14))
                    : (NSFont(name: "Microsoft YaHei", size: 19) ?? NSFont.systemFont(ofSize: 19, weight: .medium))
                textView.textContainerInset = showingEvidence ? NSSize(width: 0, height: 4) : NSSize(width: 0, height: 17)
                evidenceButton?.title = showingEvidence ? "释义" : "证据"
            }
            if let evidenceButton {
                accessory.addSubview(evidenceButton)
            }
        } else {
            let noteButton = makeLookupActionButton(
                title: "添加备注",
                symbolName: "note.text",
                frame: NSRect(x: 100, y: buttonY, width: 104, height: 30)
            ) { [weak self] in
                guard let self else { return }
                if let sheet = self.window.attachedSheet {
                    self.window.endSheet(sheet)
                }
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) {
                    self.showNotePanel(sentence: sentence, sentenceIndex: sentenceIndex)
                }
            }
            accessory.addSubview(noteButton)
        }

        let correctionButton = makeLookupActionButton(
            title: "纠错",
            symbolName: "pencil",
            frame: NSRect(x: 214, y: buttonY, width: 84, height: 30)
        ) { [weak self] in
            guard let self else { return }
            if let sheet = self.window.attachedSheet {
                self.window.endSheet(sheet)
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) {
                self.showLookupCorrectionPanel(
                    bookID: bookID,
                    word: displayWord,
                    currentMeaning: meaning,
                    sentence: sentence,
                    sentenceIndex: sentenceIndex
                )
            }
        }
        accessory.addSubview(correctionButton)

        alert.accessoryView = accessory
        alert.addButton(withTitle: "关闭")

        alert.beginSheetModal(for: window) { response in
            _ = response
            self.lookupActionTargets.removeAll()
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { [weak self] in
            guard let self, self.window.attachedSheet != nil else {
                return
            }
            self.speakEnglish(displayWord)
        }
    }

    private func showLookupCorrectionPanel(
        bookID: String,
        word: String,
        currentMeaning: String,
        sentence: String,
        sentenceIndex: String
    ) {
        guard window.attachedSheet == nil else {
            statusLabel.stringValue = "已有弹窗打开，暂不能保存纠错"
            return
        }

        let alert = NSAlert()
        alert.messageText = "修正释义"
        alert.informativeText = word

        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 420, height: 110))
        let textView = NSTextView(frame: scroll.bounds)
        textView.font = NSFont(name: "Microsoft YaHei", size: 16) ?? NSFont.systemFont(ofSize: 16)
        textView.string = currentMeaning
        textView.isEditable = true
        textView.isSelectable = true
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = false
        textView.autoresizingMask = [.width]
        textView.textContainer?.containerSize = NSSize(width: scroll.contentSize.width, height: CGFloat.greatestFiniteMagnitude)
        textView.textContainer?.widthTracksTextView = true
        textView.textContainerInset = NSSize(width: 8, height: 8)
        scroll.documentView = textView
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        alert.accessoryView = scroll

        alert.addButton(withTitle: "保存")
        alert.addButton(withTitle: "取消")

        alert.beginSheetModal(for: window) { response in
            guard response == .alertFirstButtonReturn else {
                return
            }
            let nextMeaning = textView.string.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !nextMeaning.isEmpty else {
                self.statusLabel.stringValue = "释义不能为空"
                return
            }
            DispatchQueue.global(qos: .userInitiated).async {
                let updated = self.readerAPI.correctLookupMeaning(
                    bookID: bookID,
                    word: word,
                    meaning: nextMeaning,
                    sentenceIndex: sentenceIndex,
                    sentence: sentence
                )
                DispatchQueue.main.async {
                    guard let updated else {
                        self.statusLabel.stringValue = "纠错保存失败"
                        return
                    }
                    self.statusLabel.stringValue = "已保存纠错：\(word)"
                    self.showLookupAlert(
                        bookID: bookID,
                        word: word,
                        sentence: sentence,
                        sentenceIndex: sentenceIndex,
                        payload: ["item": updated]
                    )
                }
            }
        }
    }

    private func showLookupEvidenceAlert(
        bookID: String,
        itemID: String,
        word: String,
        meaningSource: String,
        sourceTitle: String,
        sourceVolume: String,
        sourcePage: String,
        alignmentStatus: String,
        itemStatus: String,
        occurrenceCount: String,
        representativeEN: String,
        representativeZH: String,
        reviewable: Bool
    ) {
        guard window.attachedSheet == nil else {
            statusLabel.stringValue = "已有弹窗打开，证据未显示"
            return
        }

        var lines: [String] = []
        if !meaningSource.isEmpty {
            lines.append("来源：\(Self.lookupMeaningSourceTitle(meaningSource))")
        }
        if !sourceTitle.isEmpty {
            lines.append("批次：\(sourceTitle)")
        }
        if !sourceVolume.isEmpty || !sourcePage.isEmpty {
            let pageText = sourcePage.isEmpty ? "" : "第 \(sourcePage) 页"
            lines.append("出处：\([sourceVolume, pageText].filter { !$0.isEmpty }.joined(separator: " · "))")
        }
        if !alignmentStatus.isEmpty {
            lines.append("对齐：\(Self.lookupAlignmentTitle(alignmentStatus))")
        }
        if !itemStatus.isEmpty {
            lines.append("状态：\(Self.lookupStatusTitle(itemStatus))")
        }
        if !occurrenceCount.isEmpty {
            lines.append("本书出现：\(occurrenceCount) 次")
        }
        if !representativeEN.isEmpty {
            lines.append("")
            lines.append("英文上下文：")
            lines.append(representativeEN)
        }
        if !representativeZH.isEmpty {
            lines.append("")
            lines.append("中文证据：")
            lines.append(representativeZH)
        }
        if lines.isEmpty {
            lines.append("暂无更多证据。")
        }

        let alert = NSAlert()
        alert.messageText = "\(word) 的证据"
        alert.informativeText = "这些信息用于复核，不作为默认查词内容。"

        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 560, height: 220))
        let textView = NSTextView(frame: scroll.bounds)
        textView.font = NSFont(name: "Microsoft YaHei", size: 14) ?? NSFont.systemFont(ofSize: 14)
        textView.string = lines.joined(separator: "\n")
        textView.isEditable = false
        textView.isSelectable = true
        textView.drawsBackground = false
        textView.textColor = NSColor.labelColor
        scroll.documentView = textView
        scroll.hasVerticalScroller = true
        alert.accessoryView = scroll

        var actions: [(title: String, id: String)] = []
        if reviewable && !itemID.isEmpty {
            actions.append(("复习", "reviewing"))
            actions.append(("掌握", "known"))
        }
        actions.append(("关闭", "close"))
        actions.forEach { alert.addButton(withTitle: $0.title) }
        decorateLookupActionButtons(alert.buttons, actions: actions)

        alert.beginSheetModal(for: window) { response in
            let firstRaw = NSApplication.ModalResponse.alertFirstButtonReturn.rawValue
            let actionIndex = response.rawValue - firstRaw
            guard actions.indices.contains(actionIndex) else {
                return
            }
            switch actions[actionIndex].id {
            case "reviewing", "known":
                self.updateVocabItemStatus(bookID: bookID, itemID: itemID, status: actions[actionIndex].id, word: word)
            default:
                break
            }
        }
    }

    private func decorateLookupActionButtons(_ buttons: [NSButton], actions: [(title: String, id: String)]) {
        guard #available(macOS 11.0, *) else {
            return
        }
        for (button, action) in zip(buttons, actions) {
            let symbolName: String?
            switch action.id {
            case "speak_word", "speak_meaning":
                symbolName = "speaker.wave.2.fill"
            case "evidence":
                symbolName = "doc.text.magnifyingglass"
            case "note":
                symbolName = "note.text"
            case "reviewing":
                symbolName = "arrow.triangle.2.circlepath"
            case "known":
                symbolName = "checkmark.circle.fill"
            case "close":
                symbolName = "xmark.circle"
            default:
                symbolName = nil
            }
            if let symbolName,
               let image = NSImage(systemSymbolName: symbolName, accessibilityDescription: action.title) {
                button.image = image
                button.imagePosition = .imageLeading
            }
        }
    }

    private func playLookupAudioData(_ data: Data, status: String) -> Bool {
        speechSynthesizer.stopSpeaking(at: .immediate)
        lookupAudioPlayer?.pause()
        lookupAudioProcess?.terminate()
        lookupAudioProcess = nil
        do {
            let audioURL = FileManager.default.temporaryDirectory
                .appendingPathComponent("click-lookup-tts-\(UUID().uuidString)")
                .appendingPathExtension("mp3")
            try data.write(to: audioURL, options: .atomic)
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/afplay")
            process.arguments = [audioURL.path]
            process.terminationHandler = { _ in
                try? FileManager.default.removeItem(at: audioURL)
            }
            try process.run()
            lookupAudioProcess = process
            statusLabel.stringValue = status
            return true
        } catch {
            statusLabel.stringValue = "afplay 播放失败，尝试 App 内播放器：\(error.localizedDescription)"
        }
        if let sound = NSSound(data: data) {
            lookupSoundPlayer?.stop()
            sound.volume = 1.0
            lookupSoundPlayer = sound
            let ok = sound.play()
            statusLabel.stringValue = ok ? status : "音频已生成，但 NSSound 未能启动"
            if ok {
                return true
            }
        }
        do {
            let player = try AVAudioPlayer(data: data)
            player.prepareToPlay()
            lookupAudioDataPlayer = player
            let ok = player.play()
            statusLabel.stringValue = ok ? status : "音频已生成，但播放器未能启动"
            return ok
        } catch {
            statusLabel.stringValue = "音频播放失败：\(error.localizedDescription)"
            return false
        }
    }

    private func speakEnglish(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            statusLabel.stringValue = "没有可朗读的英文"
            return
        }
        statusLabel.stringValue = "正在准备英文朗读"
        DispatchQueue.global(qos: .utility).async { [readerAPI] in
            let audioData = readerAPI.lookupCachedTTSData(text: trimmed, voice: "en-US-BrianNeural")
            DispatchQueue.main.async {
                if let audioData,
                   self.playLookupAudioData(audioData, status: "正在朗读英文：\(trimmed)") {
                    return
                } else {
                    self.statusLabel.stringValue = "首次生成高质量读音，先用本机语音朗读：\(trimmed)"
                    self.speakEnglishFallback(trimmed)
                    DispatchQueue.global(qos: .utility).async {
                        _ = readerAPI.lookupTTS(text: trimmed, voice: "en-US-BrianNeural")
                    }
                }
            }
        }
    }

    private func speakEnglishFallback(_ text: String) {
        lookupAudioProcess?.terminate()
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/say")
        process.arguments = ["-v", "Samantha", text]
        do {
            try process.run()
            lookupAudioProcess = process
            statusLabel.stringValue = "正在用本机语音朗读英文：\(text)"
            return
        } catch {
            statusLabel.stringValue = "本机 say 启动失败，退回系统语音：\(error.localizedDescription)"
        }
        let utterance = AVSpeechUtterance(string: text)
        utterance.voice = AVSpeechSynthesisVoice(language: "en-US")
        utterance.rate = 0.38
        utterance.pitchMultiplier = 0.92
        speechSynthesizer.stopSpeaking(at: .immediate)
        speechSynthesizer.speak(utterance)
        statusLabel.stringValue = "正在朗读英文：\(text)"
    }

    private func speakChineseMeaning(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            statusLabel.stringValue = "没有可朗读的释义"
            return
        }
        statusLabel.stringValue = "正在准备释义朗读"
        DispatchQueue.global(qos: .utility).async { [readerAPI] in
            let audioData = readerAPI.lookupCachedTTSData(text: trimmed, voice: "zh-CN-YunjianNeural")
            DispatchQueue.main.async {
                if let audioData,
                   self.playLookupAudioData(audioData, status: "正在朗读释义：\(trimmed)") {
                    return
                } else {
                    self.statusLabel.stringValue = "首次生成高质量释义朗读，先用本机语音朗读"
                    self.speakChineseFallback(trimmed)
                    DispatchQueue.global(qos: .utility).async {
                        _ = readerAPI.lookupTTS(text: trimmed, voice: "zh-CN-YunjianNeural")
                    }
                }
            }
        }
    }

    private func speakChineseFallback(_ text: String) {
        let utterance = AVSpeechUtterance(string: text)
        utterance.voice = AVSpeechSynthesisVoice(language: "zh-CN")
        utterance.rate = 0.44
        speechSynthesizer.stopSpeaking(at: .immediate)
        speechSynthesizer.speak(utterance)
        statusLabel.stringValue = "正在朗读释义：\(text)"
    }

    private func updateVocabItemStatus(bookID: String, itemID: String, status: String, word: String) {
        DispatchQueue.global(qos: .utility).async { [readerAPI] in
            let ok = readerAPI.updateVocabItem(bookID: bookID, itemID: itemID, status: status)
            DispatchQueue.main.async {
                let title = Self.lookupStatusTitle(status)
                self.statusLabel.stringValue = ok ? "\(word) 已标记为\(title)" : "\(word) 状态更新失败"
            }
        }
    }

    private static func lookupLemma(from payload: [String: Any]?) -> String? {
        guard let item = payload?["item"] as? [String: Any] else {
            return nil
        }
        let lemma = lookupText(item["lemma"])
        return lemma.isEmpty ? nil : lemma
    }

    private static func firstLookupOccurrence(from payload: [String: Any]?) -> [String: Any]? {
        guard let occurrences = payload?["occurrences"] as? [[String: Any]] else {
            return nil
        }
        return occurrences.first
    }

    private static func lookupText(_ value: Any?) -> String {
        if let text = value as? String {
            return text
        }
        if let number = value as? NSNumber {
            return number.stringValue
        }
        if let bool = value as? Bool {
            return bool ? "true" : "false"
        }
        if let value {
            return "\(value)"
        }
        return ""
    }

    private static func lookupAlignmentTitle(_ status: String) -> String {
        switch status {
        case "confirmed_context_meaning":
            return "直译确认"
        case "paraphrased_context_meaning":
            return "上下文意译"
        case "context_sentence_available":
            return "有书内对应句"
        case "suspected_alignment_mismatch":
            return "疑似中英错配"
        case "needs_review":
            return "需要人工复核"
        case "missing_chinese_sentence":
            return "缺中文对应句"
        default:
            return status
        }
    }

    private static func lookupMeaningSourceTitle(_ source: String) -> String {
        switch source {
        case "lifestudy_domain_glossary": return "生命读经词库"
        case "book_glossary": return "本书术语表"
        case "user_glossary": return "用户修正"
        case "dictionary_fallback": return "本地词典"
        case "lifestudy_context": return "本书语境"
        default: return source
        }
    }

    private static func lookupPartOfSpeechTitle(primary: String, fallback: String) -> String {
        let raw = primary.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? fallback : primary
        let key = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            .replacingOccurrences(of: ".", with: "")
            .replacingOccurrences(of: "_", with: " ")
            .lowercased()
        switch key {
        case "n", "noun", "名词":
            return "名词"
        case "v", "verb", "动词":
            return "动词"
        case "adj", "a", "adjective", "形容词":
            return "形容词"
        case "adv", "adverb", "副词":
            return "副词"
        case "proper noun", "proper_noun", "专有名词":
            return "专有名词"
        case "noun or verb", "noun_or_verb", "名词或动词":
            return "名词或动词"
        case "adjective or noun", "adjective_or_noun", "形容词或名词":
            return "形容词或名词"
        case "prep", "preposition", "介词":
            return "介词"
        case "conj", "conjunction", "连词":
            return "连词"
        case "pron", "pronoun", "代词":
            return "代词"
        case "num", "numeral", "number", "数词":
            return "数词"
        case "interj", "interjection", "感叹词":
            return "感叹词"
        case "article", "art", "冠词":
            return "冠词"
        default:
            return raw
        }
    }

    private static func lookupPartOfSpeechShortTitle(primary: String, fallback: String) -> String {
        let raw = primary.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? fallback : primary
        let key = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            .replacingOccurrences(of: ".", with: "")
            .replacingOccurrences(of: "_", with: " ")
            .lowercased()
        switch key {
        case "n", "noun", "名词":
            return "n."
        case "v", "verb", "动词":
            return "v."
        case "adj", "a", "adjective", "形容词":
            return "adj."
        case "adv", "adverb", "副词":
            return "adv."
        case "proper noun", "proper_noun", "专有名词":
            return "prop. n."
        case "noun or verb", "noun_or_verb", "名词或动词":
            return "n./v."
        case "adjective or noun", "adjective_or_noun", "形容词或名词":
            return "adj./n."
        case "prep", "preposition", "介词":
            return "prep."
        case "conj", "conjunction", "连词":
            return "conj."
        case "pron", "pronoun", "代词":
            return "pron."
        case "num", "numeral", "number", "数词":
            return "num."
        case "interj", "interjection", "感叹词":
            return "interj."
        case "article", "art", "冠词":
            return "art."
        default:
            return raw
        }
    }

    private static func lookupStatusTitle(_ status: String) -> String {
        switch status {
        case "reviewing":
            return "复习"
        case "known":
            return "掌握"
        case "ignored":
            return "忽略"
        case "candidate":
            return "候选"
        case "saved":
            return "已保存"
        default:
            return status
        }
    }

    private func showNotePreview(sourceText: String, noteText: String, annotationID: String) {
        guard window.attachedSheet == nil else {
            return
        }

        let alert = NSAlert()
        alert.messageText = "注释"
        alert.informativeText = sourceText.isEmpty ? "当前句已有备注" : sourceText
        if annotationID.isEmpty {
            alert.addButton(withTitle: "关闭")
        } else {
            alert.addButton(withTitle: "编辑")
            alert.addButton(withTitle: "关闭")
        }

        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 520, height: 150))
        let textView = NSTextView(frame: scroll.bounds)
        textView.font = NSFont(name: "Microsoft YaHei", size: 14) ?? NSFont.systemFont(ofSize: 14)
        textView.string = noteText.isEmpty ? "这条注释没有正文。" : noteText
        textView.isEditable = false
        textView.drawsBackground = false
        scroll.documentView = textView
        scroll.hasVerticalScroller = true
        alert.accessoryView = scroll

        alert.beginSheetModal(for: window) { response in
            guard !annotationID.isEmpty,
                  response == .alertFirstButtonReturn
            else {
                return
            }
            self.showExistingNoteEditor(annotationID: annotationID, sourceText: sourceText, noteText: noteText)
        }
    }

    private func showExistingNoteEditor(annotationID: String, sourceText: String, noteText: String) {
        let alert = NSAlert()
        alert.messageText = "编辑注释"
        alert.informativeText = sourceText
        alert.addButton(withTitle: "保存")
        alert.addButton(withTitle: "取消")

        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 520, height: 150))
        let textView = NSTextView(frame: scroll.bounds)
        textView.font = NSFont(name: "Microsoft YaHei", size: 14) ?? NSFont.systemFont(ofSize: 14)
        textView.string = noteText
        scroll.documentView = textView
        scroll.hasVerticalScroller = true
        alert.accessoryView = scroll

        alert.beginSheetModal(for: window) { response in
            guard response == .alertFirstButtonReturn else {
                return
            }
            let updated = NoteTextNormalizer.normalized(textView.string)
            DispatchQueue.global(qos: .utility).async { [readerAPI = self.readerAPI] in
                let ok = readerAPI.updateAnnotation(annotationID: annotationID, noteText: updated, color: nil)
                DispatchQueue.main.async {
                    self.statusLabel.stringValue = ok ? "注释已更新" : "注释更新失败"
                    self.refreshNotes()
                    self.restoreAnnotationsForCurrentChapter()
                }
            }
        }
    }

    private func currentChapterLocator() -> String? {
        guard chapters.indices.contains(currentChapterIndex) else {
            return nil
        }
        return relativeChapterPath(for: chapters[currentChapterIndex])
    }

    private func annotationKey(chapterLocator: String, sentenceIndex: String) -> String {
        "\(chapterLocator)#\(sentenceIndex)"
    }

    private func sentenceIndexString(from rawIndex: Any?) -> String? {
        if let stringIndex = rawIndex as? String {
            let trimmed = stringIndex.trimmingCharacters(in: .whitespacesAndNewlines)
            return trimmed.isEmpty ? nil : trimmed
        }
        if let intIndex = rawIndex as? Int {
            return String(intIndex)
        }
        if let doubleIndex = rawIndex as? Double, doubleIndex.rounded() == doubleIndex {
            return String(Int(doubleIndex))
        }
        return nil
    }

    private func sentenceIndexList(from sentenceIndex: String) -> [String] {
        var seen = Set<String>()
        return sentenceIndex
            .split(separator: ",")
            .map { String($0).trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
            .filter { index in
                if seen.contains(index) {
                    return false
                }
                seen.insert(index)
                return true
            }
    }

    private func sentenceIndexPayload(from payload: [String: Any]) -> String {
        if let indexes = payload["indexes"] as? [String], !indexes.isEmpty {
            return sentenceIndexList(from: indexes.joined(separator: ",")).joined(separator: ",")
        }
        if let indexes = payload["indexes"] as? [Any], !indexes.isEmpty {
            let joined = indexes.compactMap { sentenceIndexString(from: $0) }.joined(separator: ",")
            return sentenceIndexList(from: joined).joined(separator: ",")
        }
        return sentenceIndexString(from: payload["index"]) ?? ""
    }

    private func persistNote(sentence: String, sentenceIndex: String, noteText: String, audioNoteID: String? = nil) {
        guard let bookID = readerBookID,
              let chapterLocator = currentChapterLocator(),
              !noteText.isEmpty
        else {
            statusLabel.stringValue = noteText.isEmpty ? "备注为空，未保存" : "Reader API 未连接，备注未写入数据库"
            return
        }
        let chapterTitle = chapterTitles.indices.contains(currentChapterIndex) ? chapterTitles[currentChapterIndex] : nil
        statusLabel.stringValue = "正在保存备注到 Reader API..."
        DispatchQueue.global(qos: .utility).async { [readerAPI] in
            let annotationID = readerAPI.createAnnotation(
                bookID: bookID,
                kind: "note",
                sourceText: sentence,
                noteText: noteText,
                color: nil,
                chapterTitle: chapterTitle,
                chapterLocator: chapterLocator,
                sentenceIndex: sentenceIndex
            )
            DispatchQueue.main.async {
                self.statusLabel.stringValue = annotationID == nil ? "备注保存失败：Reader API 未写入" : "已保存备注到 Reader API"
                if let annotationID,
                   let audioNoteID {
                    DispatchQueue.global(qos: .utility).async { [readerAPI = self.readerAPI] in
                        _ = readerAPI.updateAudioNote(
                            audioNoteID: audioNoteID,
                            annotationID: annotationID,
                            provider: nil,
                            transcript: nil,
                            status: nil,
                            errorMessage: nil
                        )
                    }
                }
                self.refreshNotes()
                self.restoreAnnotationsForCurrentChapter()
            }
        }
    }

    private func persistRed(sentence: String, sentenceIndex: String, isRed: Bool) {
        guard let bookID = readerBookID,
              let chapterLocator = currentChapterLocator(),
              !sentenceIndex.isEmpty
        else {
            return
        }
        let indexList = sentenceIndexList(from: sentenceIndex)
        guard !indexList.isEmpty else {
            return
        }
        let keys = indexList.map { annotationKey(chapterLocator: chapterLocator, sentenceIndex: $0) }
        let chapterTitle = chapterTitles.indices.contains(currentChapterIndex) ? chapterTitles[currentChapterIndex] : nil
        if isRed {
            let missingPairs = zip(indexList, keys).filter { _, key in redAnnotationIDs[key] == nil }
            if missingPairs.isEmpty {
                return
            }
            let missingIndexes = missingPairs.map { index, _ in index }
            let missingKeys = missingPairs.map { _, key in key }
            let missingSentenceIndex = missingIndexes.joined(separator: ",")
            DispatchQueue.global(qos: .utility).async { [readerAPI] in
                let annotationID = readerAPI.createAnnotation(
                    bookID: bookID,
                    kind: "red_highlight",
                    sourceText: sentence,
                    noteText: nil,
                    color: "red",
                    chapterTitle: chapterTitle,
                    chapterLocator: chapterLocator,
                    sentenceIndex: missingSentenceIndex
                )
                DispatchQueue.main.async {
                    if let annotationID {
                        for key in missingKeys {
                            self.redAnnotationIDs[key] = annotationID
                        }
                        self.statusLabel.stringValue = "红标已保存到 Reader API"
                    } else {
                        self.statusLabel.stringValue = "红标保存失败，已恢复数据库状态"
                        self.restoreAnnotationsForCurrentChapter()
                    }
                    self.refreshNotes()
                }
            }
            return
        }

        let annotationIDs = Set(keys.compactMap { redAnnotationIDs[$0] })
        for key in keys {
            redAnnotationIDs[key] = nil
        }
        guard !annotationIDs.isEmpty else {
            refreshNotes()
            return
        }
        DispatchQueue.global(qos: .utility).async { [readerAPI] in
            var allDeleted = true
            for annotationID in annotationIDs {
                allDeleted = readerAPI.deleteAnnotation(annotationID: annotationID) && allDeleted
            }
            DispatchQueue.main.async {
                self.statusLabel.stringValue = allDeleted ? "红标取消已保存到 Reader API" : "红标取消保存失败，已恢复数据库状态"
                self.refreshNotes()
                self.restoreAnnotationsForCurrentChapter()
            }
        }
    }

    private func restoreAnnotationsForCurrentChapter() {
        guard let bookID = readerBookID,
              let chapterLocator = currentChapterLocator()
        else {
            return
        }

        DispatchQueue.global(qos: .utility).async { [readerAPI] in
            let annotations = readerAPI.listAnnotations(bookID: bookID)
            var redIndexes: [String] = []
            var redIDs: [String: String] = [:]
            var noteItems: [[String: Any]] = []
            for annotation in annotations {
                guard let kind = annotation["kind"] as? String,
                      annotation["chapter_locator"] as? String == chapterLocator,
                      let id = annotation["id"] as? String
                else {
                    continue
                }
                let range = annotation["range_locator"] as? [String: Any] ?? [:]
                let metadata = annotation["metadata"] as? [String: Any] ?? [:]
                guard let rawIndex = self.sentenceIndexString(from: range["sentenceIndex"] ?? metadata["sentenceIndex"]) else {
                    continue
                }
                let indexes = self.sentenceIndexList(from: rawIndex)
                guard !indexes.isEmpty else {
                    continue
                }

                if kind == "red_highlight" {
                    redIndexes.append(contentsOf: indexes)
                    for index in indexes {
                        redIDs[self.annotationKey(chapterLocator: chapterLocator, sentenceIndex: index)] = id
                    }
                    continue
                }

                if kind == "note" {
                    noteItems.append([
                        "id": id,
                        "index": indexes.joined(separator: ","),
                        "indexes": indexes,
                        "sourceText": annotation["source_text"] as? String ?? "",
                        "noteText": annotation["note_text"] as? String ?? "",
                    ])
                }
            }

            DispatchQueue.main.async {
                let prefix = "\(chapterLocator)#"
                self.redAnnotationIDs = self.redAnnotationIDs.filter { !$0.key.hasPrefix(prefix) }
                for (key, value) in redIDs {
                    self.redAnnotationIDs[key] = value
                }
                let payload: [String: Any] = [
                    "redIndexes": Array(Set(redIndexes)).sorted { lhs, rhs in
                        let leftNumber = Int(lhs)
                        let rightNumber = Int(rhs)
                        if let leftNumber, let rightNumber {
                            return leftNumber < rightNumber
                        }
                        if leftNumber != nil {
                            return true
                        }
                        if rightNumber != nil {
                            return false
                        }
                        return lhs < rhs
                    },
                    "notes": noteItems,
                ]
                if let data = try? JSONSerialization.data(withJSONObject: payload),
                   let json = String(data: data, encoding: .utf8) {
                    self.webView.evaluateJavaScript("window.__sentenceReaderApplyAnnotations && window.__sentenceReaderApplyAnnotations(\(json));")
                }
            }
        }
    }

    private static let readerScript = """
    (function () {
      if (window.__sentenceReaderNativeReady) { return; }
      window.__sentenceReaderNativeReady = true;

      const style = document.createElement('style');
      style.textContent = `
        :root {
          --sr-bg: #000;
          --sr-text: #f5f5f5;
          --sr-focus-bg: rgba(64, 156, 255, .34);
          --sr-focus-ring: rgba(124, 190, 255, .52);
          --sr-font-size: 18px;
          --sr-line-height: 1.72;
          --sr-page-margin-x: 10px;
          --sr-page-width-subtract: 20px;
          --sr-wide-page-margin-x: 12px;
          --sr-wide-width-subtract: 48px;
          --sr-wide-column-gap: 24px;
          --sr-column-gap: 20px;
        }
        html, body {
          background: var(--sr-bg) !important;
          color: var(--sr-text) !important;
          font-family: "Microsoft YaHei", "微软雅黑", "PingFang SC", "Heiti SC", "Helvetica Neue", Arial, sans-serif !important;
          width: 100vw !important;
          height: 100vh !important;
          margin: 0 !important;
          padding: 0 !important;
          overflow: hidden !important;
          font-size: var(--sr-font-size) !important;
          line-height: var(--sr-line-height) !important;
        }
        body {
          position: fixed !important;
          inset: 0 !important;
          perspective: 1600px !important;
        }
        #sr-page-surface {
          box-sizing: border-box !important;
          width: 100vw !important;
          height: 100vh !important;
          min-height: 100vh !important;
          padding: 2px var(--sr-page-margin-x) 4px !important;
          column-width: calc(100vw - var(--sr-page-width-subtract)) !important;
          column-gap: var(--sr-column-gap) !important;
          -webkit-column-fill: auto !important;
          column-fill: auto !important;
          overflow: visible !important;
          will-change: transform !important;
          transform-style: preserve-3d !important;
          backface-visibility: hidden !important;
        }
        #sr-page-surface, #sr-page-surface * {
          box-sizing: border-box !important;
          max-width: 100% !important;
          overflow-wrap: anywhere !important;
          word-break: break-word !important;
        }
        #sr-page-surface p, #sr-page-surface li, #sr-page-surface blockquote,
        #sr-page-surface div, #sr-page-surface section, #sr-page-surface article {
          min-width: 0 !important;
        }
        #sr-page-surface table {
          width: 100% !important;
          max-width: 100% !important;
          table-layout: fixed !important;
          border-collapse: collapse !important;
        }
        #sr-page-surface td, #sr-page-surface th {
          width: auto !important;
          max-width: 100% !important;
          min-width: 0 !important;
          overflow-wrap: anywhere !important;
          word-break: break-word !important;
        }
        #sr-page-surface pre, #sr-page-surface code {
          white-space: pre-wrap !important;
          overflow-wrap: anywhere !important;
        }
        @media (min-width: 1180px) {
          #sr-page-surface {
            padding-left: var(--sr-wide-page-margin-x) !important;
            padding-right: var(--sr-wide-page-margin-x) !important;
            column-width: calc((100vw - var(--sr-wide-width-subtract)) / 2) !important;
            column-gap: var(--sr-wide-column-gap) !important;
          }
        }
        body.sr-large-font #sr-page-surface {
          padding-left: var(--sr-page-margin-x) !important;
          padding-right: var(--sr-page-margin-x) !important;
          column-width: calc(100vw - var(--sr-page-width-subtract)) !important;
          column-gap: var(--sr-column-gap) !important;
        }
        #sr-page-flip-overlay {
          position: fixed !important;
          z-index: 9999 !important;
          pointer-events: none !important;
          top: 32px !important;
          right: 0 !important;
          bottom: 22px !important;
          left: 0 !important;
          opacity: 0 !important;
          transform: translateX(100%) skewX(-6deg) !important;
          will-change: transform, opacity !important;
        }
        #sr-page-flip-overlay.sr-next {
          background:
            linear-gradient(90deg,
              rgba(255,255,255,0) 0%,
              rgba(255,255,255,.08) 36%,
              rgba(0,0,0,.38) 52%,
              rgba(255,255,255,.20) 63%,
              rgba(255,255,255,0) 100%) !important;
        }
        #sr-page-flip-overlay.sr-previous {
          background:
            linear-gradient(270deg,
              rgba(255,255,255,0) 0%,
              rgba(255,255,255,.08) 36%,
              rgba(0,0,0,.38) 52%,
              rgba(255,255,255,.20) 63%,
              rgba(255,255,255,0) 100%) !important;
        }
        #sr-page-flip-overlay.sr-active.sr-next {
          animation: sr-page-curl-next 280ms cubic-bezier(.18,.86,.20,1) both !important;
        }
        #sr-page-flip-overlay.sr-active.sr-previous {
          animation: sr-page-curl-previous 280ms cubic-bezier(.18,.86,.20,1) both !important;
        }
        @keyframes sr-page-curl-next {
          0% { opacity: 0; transform: translateX(96%) skewX(-9deg); }
          18% { opacity: .62; }
          54% { opacity: .78; transform: translateX(4%) skewX(-3deg); }
          100% { opacity: 0; transform: translateX(-96%) skewX(-1deg); }
        }
        @keyframes sr-page-curl-previous {
          0% { opacity: 0; transform: translateX(-96%) skewX(9deg); }
          18% { opacity: .62; }
          54% { opacity: .78; transform: translateX(-4%) skewX(3deg); }
          100% { opacity: 0; transform: translateX(96%) skewX(1deg); }
        }
        h1, h2, h3, h4, h5, h6 {
          break-inside: avoid !important;
        }
        p, blockquote, li {
          break-inside: auto !important;
          page-break-inside: auto !important;
          orphans: 1 !important;
          widows: 1 !important;
        }
        img, svg, image, figure {
          break-inside: avoid !important;
          page-break-inside: avoid !important;
        }
        img, svg {
          display: block !important;
          max-width: 100% !important;
          max-height: calc(100vh - 16px) !important;
          width: auto !important;
          height: auto !important;
          object-fit: contain !important;
          margin: 6px auto 10px !important;
        }
        a:has(img) {
          display: block !important;
          break-inside: avoid !important;
        }
        div.right, #main1 {
          display: block !important;
          float: none !important;
          width: auto !important;
          min-width: 0 !important;
          max-width: 100% !important;
          text-align: left !important;
          break-inside: auto !important;
        }
        * {
          color: var(--sr-text) !important;
          background: transparent !important;
          font-family: "Microsoft YaHei", "微软雅黑", "PingFang SC", "Heiti SC", "Helvetica Neue", Arial, sans-serif !important;
        }
        p, li, blockquote, div { line-height: var(--sr-line-height) !important; }
        .sr-sentence { border-radius: 3px !important; cursor: text !important; }
        .sr-sentence.sr-focused { background: var(--sr-focus-bg) !important; box-shadow: 0 0 0 1px var(--sr-focus-ring) inset !important; }
        .sr-word-focused {
          border-radius: .24em !important;
          background: rgba(0, 122, 255, .34) !important;
          box-shadow: 0 0 0 1.5px rgba(98, 180, 255, .88) inset, 0 0 0 1px rgba(10, 132, 255, .32) !important;
          padding: 0 .08em !important;
          margin: 0 -.08em !important;
          color: inherit !important;
        }
        .sr-sentence.sr-note { cursor: pointer !important; text-decoration-line: underline !important; text-decoration-style: dotted !important; text-decoration-color: rgba(96, 165, 250, .95) !important; text-underline-offset: .18em !important; }
        .sr-sentence.sr-red, .sr-sentence.sr-red.sr-focused { background: rgba(255, 59, 48, .62) !important; color: #fff !important; box-shadow: 0 0 0 1px rgba(255, 170, 160, .72) inset !important; }
      `;
      document.head.appendChild(style);

      function applyReaderSettings(settings) {
        settings = settings || {};
        const root = document.documentElement;
        const theme = settings.theme === 'warm' ? 'warm' : 'dark';
        const fontSize = Math.max(15, Math.min(28, Number(settings.fontSize) || 18));
        const lineHeight = Math.max(1.2, Math.min(2.05, Number(settings.lineHeight) || 1.58));
        const marginX = Math.max(2, Math.min(40, Number(settings.marginX) || 8));
        const columnGap = marginX * 2;
        const wideMargin = marginX + 2;
        const wideGap = wideMargin * 2;
        root.style.setProperty('--sr-bg', theme === 'warm' ? '#f6f0e5' : '#000');
        root.style.setProperty('--sr-text', theme === 'warm' ? '#1f1f1f' : '#f5f5f5');
        root.style.setProperty('--sr-focus-bg', theme === 'warm' ? 'rgba(28, 115, 220, .22)' : 'rgba(64, 156, 255, .34)');
        root.style.setProperty('--sr-focus-ring', theme === 'warm' ? 'rgba(28, 115, 220, .46)' : 'rgba(124, 190, 255, .52)');
        root.style.setProperty('--sr-font-size', fontSize + 'px');
        root.style.setProperty('--sr-line-height', String(lineHeight));
        root.style.setProperty('--sr-page-margin-x', marginX + 'px');
        root.style.setProperty('--sr-page-width-subtract', (marginX * 2) + 'px');
        root.style.setProperty('--sr-column-gap', columnGap + 'px');
        root.style.setProperty('--sr-wide-page-margin-x', wideMargin + 'px');
        root.style.setProperty('--sr-wide-width-subtract', ((wideMargin * 2) + wideGap) + 'px');
        root.style.setProperty('--sr-wide-column-gap', wideGap + 'px');
        document.body.classList.toggle('sr-large-font', fontSize >= 24);
        if (typeof applyPage === 'function') {
          invalidatePagination();
          pageIndex = Math.max(0, Math.min(pageIndex, maxPageIndex()));
          applyPage(false, 0);
        }
        return true;
      }
      window.__sentenceReaderApplySettings = applyReaderSettings;

      function post(payload) {
        try { window.webkit.messageHandlers.sentenceReader.postMessage(payload); } catch (error) {}
      }
      function ensureSurface() {
        let surface = document.getElementById('sr-page-surface');
        if (surface) { return surface; }
        surface = document.createElement('main');
        surface.id = 'sr-page-surface';
        while (document.body.firstChild) {
          surface.appendChild(document.body.firstChild);
        }
        document.body.appendChild(surface);
        return surface;
      }
      function parts(text) {
        const out = [];
        const nonSentenceBoundaryCharacters = '：:；;';
        const sentenceBoundaryRegex = /([^。！？!?\\n]+[。！？!?]+[”’」』）】》〕〉]*|[^。！？!?\\n]+$|\\n+)/g;
        let match;
        while ((match = sentenceBoundaryRegex.exec(text)) !== null) { out.push(match[0]); }
        return out.length ? out : [text];
      }
      function skip(node) {
        const parent = node.parentElement;
        return !parent || parent.closest('script,style,noscript,code,pre,textarea,input,.sr-sentence') || !node.nodeValue.trim();
      }
      function wrapNode(node, state) {
        if (skip(node)) { return; }
        const split = parts(node.nodeValue);
        if (split.length <= 1 && split[0].trim().length < 8) { return; }
        const fragment = document.createDocumentFragment();
        for (const item of split) {
          if (!item.trim()) { fragment.appendChild(document.createTextNode(item)); continue; }
          const span = document.createElement('span');
          span.className = 'sr-sentence';
          span.dataset.srIndex = String(state.nextIndex++);
          span.textContent = item;
          fragment.appendChild(span);
        }
        node.parentNode.replaceChild(fragment, node);
      }
      function wrap() {
        ensureSurface();
        const state = { nextIndex: document.querySelectorAll('.sr-sentence').length };
        document.querySelectorAll('#sr-page-surface p, #sr-page-surface li, #sr-page-surface blockquote, #sr-page-surface h1, #sr-page-surface h2, #sr-page-surface h3, #sr-page-surface h4, #sr-page-surface h5, #sr-page-surface h6').forEach(function (root) {
          const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
          const nodes = [];
          while (walker.nextNode()) { nodes.push(walker.currentNode); }
          nodes.forEach(function (node) { wrapNode(node, state); });
        });
      }
      function sentenceFromTarget(target) {
        if (!target) { return null; }
        target = target.nodeType === Node.TEXT_NODE ? target.parentElement : target;
        return target && target.closest ? target.closest('.sr-sentence') : null;
      }
      function fallbackSentence() {
        return document.querySelector('.sr-sentence.sr-focused') || document.querySelector('.sr-sentence');
      }
      function indexesFrom(value) {
        return String(value || '').split(',').map(function (item) { return item.trim(); }).filter(Boolean);
      }
      function compareSentenceIndex(left, right) {
        const leftNumber = Number(left.dataset.srIndex || 0);
        const rightNumber = Number(right.dataset.srIndex || 0);
        if (Number.isFinite(leftNumber) && Number.isFinite(rightNumber)) { return leftNumber - rightNumber; }
        return String(left.dataset.srIndex || '').localeCompare(String(right.dataset.srIndex || ''));
      }
      function uniqueSentences(sentences) {
        const seen = new Set();
        return (sentences || []).filter(Boolean).filter(function (sentence) {
          const index = String(sentence.dataset.srIndex || '');
          if (!index || seen.has(index)) { return false; }
          seen.add(index);
          return true;
        }).sort(compareSentenceIndex);
      }
      function rangesIntersect(selectionRange, sentenceRange) {
        try {
          return selectionRange.compareBoundaryPoints(Range.END_TO_START, sentenceRange) > 0
            && selectionRange.compareBoundaryPoints(Range.START_TO_END, sentenceRange) < 0;
        } catch (error) {
          return false;
        }
      }
      function selectedSentences() {
        const selection = window.getSelection ? window.getSelection() : null;
        if (!selection || selection.isCollapsed || selection.rangeCount === 0) { return []; }
        const matched = new Set();
        const sentences = Array.from(document.querySelectorAll('.sr-sentence'));
        for (let rangeIndex = 0; rangeIndex < selection.rangeCount; rangeIndex += 1) {
          const selectionRange = selection.getRangeAt(rangeIndex);
          sentences.forEach(function (sentence) {
            const sentenceRange = document.createRange();
            sentenceRange.selectNodeContents(sentence);
            if (rangesIntersect(selectionRange, sentenceRange)) {
              matched.add(sentence);
            }
            sentenceRange.detach && sentenceRange.detach();
          });
        }
        return uniqueSentences(sentences.filter(function (sentence) { return matched.has(sentence); }));
      }
      function hasSystemTextSelection() {
        const selection = window.getSelection ? window.getSelection() : null;
        return !!(selection && !selection.isCollapsed && String(selection.toString() || '').trim().length > 0);
      }
      window.__SentenceReaderInteractionRouter = {
        contractVersion: 'sentence-reader-interaction-v1',
        priority: 'sentence-reader-first',
        systemWhen: ['editable-target', 'active-text-selection'],
        sentenceWhen: ['plain-click', 'english-click-lookup', 'double-click-note', 'context-click-red'],
        sentenceContextWinsEvenWithSelection: true,
        copyPath: 'command-c-or-non-sentence-context-menu'
      };
      function isEditableTarget(target) {
        const node = target && target.nodeType === Node.ELEMENT_NODE ? target : target && target.parentElement;
        if (!node || !node.closest) { return false; }
        return !!node.closest('input, textarea, select, button, [contenteditable="true"], [contenteditable=""]');
      }
      function shouldLetSystemHandle(event, options) {
        options = options || {};
        if (isEditableTarget(event && event.target)) { return true; }
        if (options.respectSelection !== false && hasSystemTextSelection()) { return true; }
        return false;
      }
      function claimSentenceEvent(event) {
        if (!event) { return; }
        event.preventDefault();
        event.stopPropagation();
        if (event.stopImmediatePropagation) { event.stopImmediatePropagation(); }
      }
      function shouldLetSystemHandleContext(event) {
        if (isEditableTarget(event && event.target)) { return true; }
        const sentence = sentenceFromTarget(event && event.target);
        if (sentence) { return false; }
        return hasSystemTextSelection();
      }
      const undoStack = [];
      let notePreviewTimer = 0;
      let pageIndex = 0;
      let cachedContentWidth = 0;
      let lastSecondaryRedAt = 0;
      let lastSecondaryRedIndex = '';
      let wheelGestureDirection = 0;
      let wheelGestureDistance = 0;
      let lastWheelEventAt = 0;
      let wheelInertiaLockUntil = 0;
      let pageTurnLockUntil = 0;
      let pendingPageTurnDirection = 0;
      let pendingPageTurnTimer = 0;
      let wheelGestureConsumed = false;
      // Keep the physical wheel gesture boundary separate from page animation:
      // one swipe triggers one page, while the next clear swipe may arrive before the animation ends.
      const pageTurnCooldownMs = 120;
      const wheelInertiaLockMs = 180;
      const wheelGestureIdleMs = 220;
      const wheelPageTurnThreshold = 96;
      const wheelDominanceRatio = 1.25;
      function pageSurface() {
        return document.getElementById('sr-page-surface') || ensureSurface();
      }
      function invalidatePagination() {
        cachedContentWidth = 0;
      }
      function viewportWidth() {
        return Math.max(320, Math.floor(window.innerWidth || document.documentElement.clientWidth || 0));
      }
      function pageStep() {
        return viewportWidth();
      }
      function pixelAlignedOffset(value) {
        const scale = Math.max(1, Number(window.devicePixelRatio || 1));
        return Math.round(Math.max(0, Number(value) || 0) * scale) / scale;
      }
      function leftPageSliverGuard(index) {
        return index > 0 ? 1 : 0;
      }
      function paginationEpsilon() {
        return Math.max(8, Math.round(pageStep() * 0.012));
      }
      function measuredContentWidth() {
        if (cachedContentWidth > 0) { return cachedContentWidth; }
        const surface = pageSurface();
        const viewport = viewportWidth();
        const scrollWidth = Math.max(viewport, Math.ceil(surface.scrollWidth || 0));
        const surfaceRect = surface.getBoundingClientRect();
        let rightEdge = 0;
        const nodes = surface.querySelectorAll('.sr-sentence, img, svg, image, figure, h1, h2, h3, h4, h5, h6, p, li, blockquote');
        nodes.forEach(function (node) {
          if (!node.getClientRects) { return; }
          const rects = node.getClientRects();
          for (let index = 0; index < rects.length; index += 1) {
            const rect = rects[index];
            if (rect.width <= 0 || rect.height <= 0) { continue; }
            rightEdge = Math.max(rightEdge, rect.right - surfaceRect.left);
          }
        });
        if (rightEdge > 0) {
          cachedContentWidth = Math.max(viewport, Math.ceil(rightEdge));
        } else {
          cachedContentWidth = scrollWidth;
        }
        if (cachedContentWidth <= viewport + 2 && scrollWidth > viewport + 128) {
          cachedContentWidth = scrollWidth;
        }
        return cachedContentWidth;
      }
      function maxPageIndex() {
        const overflow = Math.max(0, measuredContentWidth() - viewportWidth());
        if (overflow <= paginationEpsilon()) { return 0; }
        return Math.max(0, Math.ceil((overflow - paginationEpsilon()) / pageStep()));
      }
      function pageOffsetForIndex(index) {
        const rawOffset = Math.max(0, index) * pageStep();
        const contentWidth = measuredContentWidth();
        if (rawOffset >= contentWidth - paginationEpsilon()) {
          return pixelAlignedOffset(Math.max(0, contentWidth - viewportWidth()));
        }
        return pixelAlignedOffset(rawOffset);
      }
      function publishPage() {
        post({ type: 'page', page: pageIndex + 1, pageIndex: pageIndex, total: maxPageIndex() + 1 });
      }
      window.__sentenceReaderAnnotationCoreV2 = true;
      function applyNoteMarkers(notes) {
        document.querySelectorAll('.sr-sentence.sr-note').forEach(function (node) {
          node.classList.remove('sr-note');
          delete node.dataset.srNoteId;
          delete node.dataset.srNoteText;
          delete node.dataset.srNoteSource;
        });
        (notes || []).forEach(function (note) {
          const indexes = Array.isArray(note.indexes) ? note.indexes.map(String) : indexesFrom(note.index || note.sentenceIndex);
          indexes.forEach(function (index) {
            const node = document.querySelector('.sr-sentence[data-sr-index="' + String(index) + '"]');
            if (!node) { return; }
            node.classList.add('sr-note');
            node.dataset.srNoteId = String(note.id || '');
            node.dataset.srNoteText = String(note.noteText || '');
            node.dataset.srNoteSource = String(note.sourceText || node.textContent || '');
          });
        });
      }
      window.__sentenceReaderApplyAnnotations = function (payload) {
        payload = payload || {};
        const redIndexes = payload.redIndexes || payload.red || [];
        const wanted = new Set((redIndexes || []).map(function (item) { return String(item); }));
        document.querySelectorAll('.sr-sentence.sr-red').forEach(function (node) { node.classList.remove('sr-red'); });
        document.querySelectorAll('.sr-sentence').forEach(function (node) {
          if (wanted.has(String(node.dataset.srIndex || ''))) {
            node.classList.add('sr-red');
          }
        });
        if (Array.isArray(payload.notes)) {
          applyNoteMarkers(payload.notes);
        }
        post({
          type: 'annotationsRestored',
          redCount: document.querySelectorAll('.sr-sentence.sr-red').length,
          noteCount: document.querySelectorAll('.sr-sentence.sr-note').length
        });
        return true;
      };
      window.__sentenceReaderApplyRedHighlights = function (indexes) {
        return window.__sentenceReaderApplyAnnotations({ redIndexes: indexes || [] });
      };
      window.__sentenceReaderFocusSentence = function (index) {
        const sentence = document.querySelector('.sr-sentence[data-sr-index="' + String(index) + '"]');
        if (!sentence) { return false; }
        pageIndex = Math.max(0, Math.min(Math.round(sentence.offsetLeft / pageStep()), maxPageIndex()));
        applyPage(false, 0);
        window.setTimeout(function () { focus(sentence); }, 30);
        return true;
      };
      function flipOverlay() {
        let overlay = document.getElementById('sr-page-flip-overlay');
        if (overlay) { return overlay; }
        overlay = document.createElement('div');
        overlay.id = 'sr-page-flip-overlay';
        document.body.appendChild(overlay);
        return overlay;
      }
      function playPageCurl(direction) {
        const overlay = flipOverlay();
        overlay.className = direction < 0 ? 'sr-previous' : 'sr-next';
        void overlay.offsetWidth;
        overlay.classList.add('sr-active');
        window.setTimeout(function () {
          overlay.className = '';
        }, 320);
      }
      function applyPage(animated, direction) {
        const surface = pageSurface();
        pageIndex = Math.max(0, Math.min(pageIndex, maxPageIndex()));
        if (animated) {
          playPageCurl(direction || 1);
          surface.style.transformOrigin = direction < 0 ? 'left center' : 'right center';
          surface.style.transition = 'transform 280ms cubic-bezier(.18,.86,.20,1), filter 280ms cubic-bezier(.18,.86,.20,1)';
          surface.style.filter = 'drop-shadow(' + (direction < 0 ? '18px' : '-18px') + ' 0 22px rgba(0,0,0,.32))';
          window.setTimeout(function () {
            surface.style.filter = 'none';
          }, 300);
        } else {
          surface.style.transition = 'none';
          surface.style.filter = 'none';
        }
        const offset = pageOffsetForIndex(pageIndex) + leftPageSliverGuard(pageIndex);
        surface.style.transform = 'translate3d(' + (-offset) + 'px, 0, 0)';
        window.scrollTo(0, 0);
        publishPage();
      }
      function schedulePendingPageTurn(direction) {
        pendingPageTurnDirection = direction < 0 ? -1 : 1;
        if (pendingPageTurnTimer) { return; }
        const delay = Math.max(0, pageTurnLockUntil - Date.now()) + 8;
        pendingPageTurnTimer = window.setTimeout(function () {
          pendingPageTurnTimer = 0;
          const queuedDirection = pendingPageTurnDirection;
          pendingPageTurnDirection = 0;
          if (queuedDirection) {
            turnPage(queuedDirection, { fromPending: true });
          }
        }, delay);
      }
      function turnPage(direction, options) {
        options = options || {};
        const now = Date.now();
        if (now < pageTurnLockUntil) {
          schedulePendingPageTurn(direction);
          return false;
        }
        if (options.fromPending) {
          pendingPageTurnDirection = 0;
        }
        pageTurnLockUntil = now + pageTurnCooldownMs;
        const before = pageIndex;
        pageIndex = Math.max(0, Math.min(pageIndex + direction, maxPageIndex()));
        if (pageIndex !== before) {
          applyPage(true, direction);
        } else {
          post({ type: 'edge', direction: direction < 0 ? 'previous' : 'next' });
          publishPage();
        }
        return true;
      }
      function resetWheelGesture() {
        wheelGestureDirection = 0;
        wheelGestureDistance = 0;
        wheelGestureConsumed = false;
      }
      function handleHorizontalWheel(event) {
        const now = Date.now();
        const absX = Math.abs(event.deltaX);
        const absY = Math.abs(event.deltaY);

        const startsNewGesture = now - lastWheelEventAt > wheelGestureIdleMs;
        if (startsNewGesture) {
          resetWheelGesture();
        }
        lastWheelEventAt = now;
        if (absX < 10 || absX < absY * wheelDominanceRatio) {
          resetWheelGesture();
          return false;
        }
        if (now < wheelInertiaLockUntil && !startsNewGesture) {
          wheelGestureConsumed = true;
          return false;
        }
        const eventDirection = event.deltaX > 0 ? 1 : -1;
        if (wheelGestureDirection !== 0 && eventDirection !== wheelGestureDirection) {
          if (absX < 18) {
            return false;
          }
          resetWheelGesture();
        }
        if (wheelGestureDirection === 0) {
          wheelGestureDirection = eventDirection;
        }
        if (wheelGestureConsumed) {
          return false;
        }

        wheelGestureDistance += absX;
        if (wheelGestureDistance >= wheelPageTurnThreshold) {
          const turnDirection = wheelGestureDirection;
          wheelGestureConsumed = true;
          wheelInertiaLockUntil = now + wheelInertiaLockMs;
          turnPage(turnDirection);
        }
        return false;
      }
      function focus(sentence) {
        if (!sentence) { return; }
        document.querySelectorAll('.sr-sentence.sr-focused').forEach(function (node) { node.classList.remove('sr-focused'); });
        sentence.classList.add('sr-focused');
        post({ type: 'focus', text: sentence.textContent || '', index: sentence.dataset.srIndex || '' });
      }
      function postNotePreview(sentence) {
        if (!sentence || !sentence.classList.contains('sr-note')) { return; }
        post({
          type: 'notePreview',
          text: sentence.dataset.srNoteSource || sentence.textContent || '',
          index: sentence.dataset.srIndex || '',
          noteID: sentence.dataset.srNoteId || '',
          noteText: sentence.dataset.srNoteText || ''
        });
      }
      function clearTextSelection() {
        const selection = window.getSelection ? window.getSelection() : null;
        if (selection && selection.removeAllRanges) {
          selection.removeAllRanges();
        }
      }
      function clearWordFocus() {
        document.querySelectorAll('.sr-word-focused').forEach(function (node) {
          const parent = node.parentNode;
          if (!parent) { return; }
          while (node.firstChild) {
            parent.insertBefore(node.firstChild, node);
          }
          parent.removeChild(node);
          if (parent.normalize) { parent.normalize(); }
        });
      }
      function selectedEnglishWord() {
        const selection = window.getSelection ? window.getSelection() : null;
        const text = selection ? String(selection.toString() || '').replace(/\\s+/g, ' ').trim() : '';
        if (!text) { return ''; }
        if (/^[A-Za-z][A-Za-z' -]{0,96}[A-Za-z]$/.test(text)) {
          return text;
        }
        const match = text.match(/[A-Za-z][A-Za-z'-]*/);
        return match ? match[0] : '';
      }
      function caretRangeFromEvent(event) {
        if (document.caretRangeFromPoint) {
          return document.caretRangeFromPoint(event.clientX, event.clientY);
        }
        if (document.caretPositionFromPoint) {
          const position = document.caretPositionFromPoint(event.clientX, event.clientY);
          if (!position) { return null; }
          const range = document.createRange();
          range.setStart(position.offsetNode, position.offset);
          range.collapse(true);
          return range;
        }
        return null;
      }
      function wordHitFromRange(range) {
        if (!range || !range.startContainer || range.startContainer.nodeType !== Node.TEXT_NODE) { return null; }
        const text = range.startContainer.nodeValue || '';
        if (!text) { return null; }
        function isWordChar(character) {
          return /[A-Za-z'-]/.test(character || '');
        }
        let cursor = Math.max(0, Math.min(text.length, range.startOffset || 0));
        if (cursor > 0 && !isWordChar(text.charAt(cursor)) && isWordChar(text.charAt(cursor - 1))) {
          cursor -= 1;
        }
        if (!isWordChar(text.charAt(cursor))) { return null; }
        let start = cursor;
        let end = cursor;
        while (start > 0 && isWordChar(text.charAt(start - 1))) { start -= 1; }
        while (end < text.length && isWordChar(text.charAt(end))) { end += 1; }
        const word = text.slice(start, end).replace(/^[^A-Za-z]+|[^A-Za-z]+$/g, '');
        return /^[A-Za-z][A-Za-z'-]*$/.test(word)
          ? { word: word, node: range.startContainer, start: start, end: end }
          : null;
      }
      function wordFromRange(range) {
        const hit = wordHitFromRange(range);
        return hit ? hit.word : '';
      }
      function lookupWordHitFromEvent(event) {
        const selected = selectedEnglishWord();
        if (selected) { return { word: selected, node: null, start: 0, end: 0 }; }
        return wordHitFromRange(caretRangeFromEvent(event));
      }
      function lookupWordFromEvent(event) {
        const hit = lookupWordHitFromEvent(event);
        return hit ? hit.word : '';
      }
      function focusWordHit(hit) {
        clearWordFocus();
        if (!hit || !hit.node || hit.node.nodeType !== Node.TEXT_NODE) { return false; }
        try {
          const range = document.createRange();
          range.setStart(hit.node, hit.start);
          range.setEnd(hit.node, hit.end);
          const span = document.createElement('span');
          span.className = 'sr-word-focused';
          range.surroundContents(span);
          range.detach && range.detach();
          return true;
        } catch (error) {
          return false;
        }
      }
      function toggleRedSentences(sentences, event) {
        if (event) { claimSentenceEvent(event); }
        const targets = uniqueSentences(sentences && sentences.length ? sentences : [fallbackSentence()]);
        if (!targets.length) { return false; }
        const states = targets.map(function (sentence) {
          return {
            index: sentence.dataset.srIndex || '',
            wasRed: sentence.classList.contains('sr-red'),
            text: sentence.textContent || ''
          };
        });
        const shouldRed = states.some(function (state) { return !state.wasRed; });
        targets.forEach(function (sentence) {
          sentence.classList.toggle('sr-red', shouldRed);
        });
        focus(targets[0]);
        clearTextSelection();
        const indexes = states.map(function (state) { return state.index; }).filter(Boolean);
        const text = states.map(function (state) { return state.text; }).join('');
        undoStack.push({ type: 'redBatch', states: states });
        post({
          type: 'red',
          text: text,
          index: indexes.join(','),
          indexes: indexes,
          isRed: shouldRed,
          redCount: document.querySelectorAll('.sr-sentence.sr-red').length
        });
        return false;
      }
      function toggleRed(sentence, event) {
        if (event) { event.preventDefault(); event.stopPropagation(); }
        if (sentence) {
          return toggleRedSentences([sentence], event);
        }
        const selected = selectedSentences();
        if (selected.length) {
          return toggleRedSentences(selected, event);
        }
        sentence = fallbackSentence();
        if (!sentence) { return false; }
        return toggleRedSentences([sentence], event);
      }
      function toggleRedFromSecondaryEvent(event) {
        if (shouldLetSystemHandleContext(event)) { return true; }
        const sentence = sentenceFromTarget(event && event.target);
        if (!sentence) { return true; }
        const now = Date.now();
        const index = sentence.dataset.srIndex || '';
        if (index && lastSecondaryRedIndex === index && now - lastSecondaryRedAt < 320) {
          claimSentenceEvent(event);
          return false;
        }
        lastSecondaryRedAt = now;
        lastSecondaryRedIndex = index;
        return toggleRed(sentence, event);
      }
      function undoLast() {
        const action = undoStack.pop();
        if (!action) {
          post({ type: 'undo', text: '没有可撤销操作', redCount: document.querySelectorAll('.sr-sentence.sr-red').length });
          return false;
        }
        if (action.type === 'redBatch') {
          let firstSentence = null;
          (action.states || []).forEach(function (state) {
            const sentence = document.querySelector('.sr-sentence[data-sr-index="' + state.index + '"]');
            if (!sentence) { return; }
            sentence.classList.toggle('sr-red', state.wasRed);
            firstSentence = firstSentence || sentence;
          });
          if (firstSentence) { focus(firstSentence); }
          (action.states || []).forEach(function (state) {
            post({
              type: 'undo',
              actionType: 'red',
              text: state.text || '',
              index: state.index || '',
              indexes: state.index ? [state.index] : [],
              isRed: state.wasRed,
              redCount: document.querySelectorAll('.sr-sentence.sr-red').length
            });
          });
        }
        return false;
      }
      window.__sentenceReaderUndo = undoLast;
      window.__sentenceReaderTurnPage = turnPage;
      document.addEventListener('keydown', function (event) {
        if (shouldLetSystemHandle(event)) { return; }
        if ((event.metaKey || event.ctrlKey) && !event.shiftKey && event.key && event.key.toLowerCase() === 'z') {
          event.preventDefault();
          event.stopPropagation();
          undoLast();
          return;
        }
        if (event.key === 'ArrowRight' || event.key === 'PageDown' || event.key === ' ') {
          event.preventDefault();
          event.stopPropagation();
          turnPage(1);
          return;
        }
        if (event.key === 'ArrowLeft' || event.key === 'PageUp') {
          event.preventDefault();
          event.stopPropagation();
          turnPage(-1);
          return;
        }
        if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
          event.preventDefault();
          event.stopPropagation();
        }
      }, true);
      document.addEventListener('wheel', function (event) {
        if (shouldLetSystemHandle(event)) { return; }
        event.preventDefault();
        event.stopPropagation();
        return handleHorizontalWheel(event);
      }, { capture: true, passive: false });
      window.addEventListener('resize', function () {
        invalidatePagination();
        window.setTimeout(function () { applyPage(false, 0); }, 60);
      });
      window.__sentenceReaderGoToEnd = function () {
        pageIndex = maxPageIndex();
        applyPage(false, 0);
      };
      window.__sentenceReaderRestorePage = function (savedPageIndex, savedPageRatio, savedTotalPages) {
        const max = maxPageIndex();
        const savedIndex = Number(savedPageIndex);
        const ratio = Number(savedPageRatio);
        const total = Number(savedTotalPages);
        if (Number.isFinite(total) && total > 0 && total !== max + 1 && Number.isFinite(ratio)) {
          pageIndex = Math.round(max * Math.max(0, Math.min(1, ratio)));
        } else if (Number.isFinite(savedIndex)) {
          pageIndex = savedIndex;
        } else {
          pageIndex = 0;
        }
        applyPage(false, 0);
      };
      document.addEventListener('click', function (event) {
        if (shouldLetSystemHandle(event)) { return; }
        const sentence = sentenceFromTarget(event.target);
        if (sentence) {
          const hit = lookupWordHitFromEvent(event);
          if (notePreviewTimer) { window.clearTimeout(notePreviewTimer); }
          if (hit && hit.word) {
            claimSentenceEvent(event);
            focusWordHit(hit);
            notePreviewTimer = 0;
            post({ type: 'lookup', word: hit.word, text: sentence.textContent || '', index: sentence.dataset.srIndex || '' });
            return;
          }
          clearWordFocus();
          focus(sentence);
          notePreviewTimer = window.setTimeout(function () {
            notePreviewTimer = 0;
            if (sentence.classList.contains('sr-note')) {
              postNotePreview(sentence);
            }
          }, 180);
        }
      }, true);
      document.addEventListener('dblclick', function (event) {
        if (shouldLetSystemHandle(event, { respectSelection: false })) { return; }
        if (notePreviewTimer) {
          window.clearTimeout(notePreviewTimer);
          notePreviewTimer = 0;
        }
        const sentence = sentenceFromTarget(event.target) || fallbackSentence();
        if (!sentence) { return; }
        claimSentenceEvent(event);
        clearWordFocus();
        focus(sentence);
        const hit = event.altKey ? lookupWordHitFromEvent(event) : null;
        if (hit && hit.word) {
          focusWordHit(hit);
          post({ type: 'lookup', word: hit.word, text: sentence.textContent || '', index: sentence.dataset.srIndex || '' });
          return;
        }
        post({ type: 'note', text: sentence.textContent || '', index: sentence.dataset.srIndex || '' });
      }, true);
      document.addEventListener('mousedown', function (event) {
        if (!event || event.button !== 2) { return; }
        return toggleRedFromSecondaryEvent(event);
      }, true);
      document.addEventListener('auxclick', function (event) {
        if (!event || event.button !== 2) { return; }
        return toggleRedFromSecondaryEvent(event);
      }, true);
      document.addEventListener('contextmenu', function (event) {
        return toggleRedFromSecondaryEvent(event);
      }, true);
      document.oncontextmenu = function (event) {
        return toggleRedFromSecondaryEvent(event);
      };

      wrap();
      invalidatePagination();
      applyPage(false, 0);
      post({ type: 'ready', sentenceCount: document.querySelectorAll('.sr-sentence').length });
    })();
    """
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.setActivationPolicy(.regular)
app.delegate = delegate
app.run()
