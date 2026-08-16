import AVFoundation
import CryptoKit
import Foundation
import MediaPlayer
import UIKit

struct ClickTTSConfiguration: Sendable {
    let baseURL: URL
    let deviceID: String
    let accessToken: String
    let bookID: String
    let locatorJSON: String
}

struct EPUBListeningSegment: Sendable, Hashable {
    let id: String
    let text: String
    let chapterTitle: String
    let locatorJSON: String
    let readingOrderIndex: Int
}

struct EPUBListeningQueue: Sendable, Hashable {
    let segments: [EPUBListeningSegment]
}

private enum ClickTTSError: LocalizedError {
    case audioSession(String)
    case invalidResponse
    case server(String)
    case emptyAudio
    case playback

    var errorDescription: String? {
        switch self {
        case let .audioSession(message):
            return "音频设备不可用：\(message)"
        case .invalidResponse:
            return "Click TTS 返回了无效响应"
        case let .server(message):
            return message.isEmpty ? "Click 微软语音暂时不可用" : message
        case .emptyAudio:
            return "Click TTS 返回了空音频"
        case .playback:
            return "微软语音文件无法播放"
        }
    }
}

private struct ListeningAudioCacheEntry: Codable, Sendable {
    let filename: String
    var duration: TimeInterval
    var byteCount: Int
    var lastAccessedAt: Date
    var bookIDs: Set<String>
}

private struct ListeningAudioCacheIndex: Codable {
    let schema: String
    var entries: [String: ListeningAudioCacheEntry]
}

@MainActor
final class ListeningController: NSObject, ObservableObject {
    static let microsoftVoice = "zh-CN-YunjianNeural"
    private static let cacheIndexSchema = "click.ipad.microsoft_tts_cache.v2"
    private static let initialCacheSeconds: TimeInterval = 60 * 60
    private static let refillLowWaterSeconds: TimeInterval = 30 * 60
    private static let refillChunkSeconds: TimeInterval = 30 * 60
    private static let urgentCacheSeconds: TimeInterval = 5 * 60
    private static let globalCacheSeconds: TimeInterval = 200 * 60
    private static let globalCacheBytes = 512 * 1_024 * 1_024
    private static let prefetchFailureCooldown: TimeInterval = 5 * 60

    @Published private(set) var sentences: [String] = []
    @Published private(set) var currentIndex = 0
    @Published private(set) var isPlaying = false
    @Published private(set) var isPreparing = false
    @Published private(set) var countdownRemaining: TimeInterval?
    @Published private(set) var bookTitle = ""
    @Published private(set) var chapterTitle = ""
    @Published private(set) var currentBookID = ""
    @Published private(set) var currentSegmentLocatorJSON = ""
    @Published private(set) var engineLabel = "Click 微软 · 云健"
    @Published private(set) var statusMessage = ""
    @Published private(set) var isPlayerVisible = true
    @Published private(set) var artworkImage: UIImage?
    @Published var speechRate: Float = 0.47

    private let urlSession: URLSession
    private let cacheRoot: URL
    private let cacheIndexURL: URL
    private var audioPlayer: AVAudioPlayer?
    private var playbackTask: Task<Void, Never>?
    private var prefetchTask: Task<Void, Never>?
    private var prefetchRunID: UUID?
    private var prefetchRemainingSeconds: TimeInterval = 0
    private var pendingInitialPrefetch = false
    private var prefetchRetryAfter = Date.distantPast
    private var audioFetchTasks: [String: (id: UUID, task: Task<URL, Error>)] = [:]
    private var cacheEntries: [String: ListeningAudioCacheEntry] = [:]
    private var cacheIndexPersistTask: Task<Void, Never>?
    private var legacyCacheMigrationTask: Task<Void, Never>?
    private var configuration: ClickTTSConfiguration?
    private var segmentLocatorJSONs: [String] = []
    private var segmentChapterTitles: [String] = []
    private var onSegmentChange: ((String) -> Void)?
    private var playbackGeneration = 0
    private var countdownTimer: Timer?
    private var shouldStopAfterCurrentSentence = false
    private var remoteCommandsInstalled = false
    private var corruptCacheRecoveryKey: String?

    init(
        urlSession: URLSession = .shared,
        cacheRoot: URL? = nil
    ) {
        self.urlSession = urlSession
        let resolvedCacheRoot: URL
        if let cacheRoot {
            resolvedCacheRoot = cacheRoot
        } else {
            let support = FileManager.default.urls(
                for: .applicationSupportDirectory,
                in: .userDomainMask
            ).first ?? FileManager.default.temporaryDirectory
            resolvedCacheRoot = support
                .appendingPathComponent("ClickWorkspace", isDirectory: true)
                .appendingPathComponent("ReaderTTS", isDirectory: true)
                .appendingPathComponent("microsoft-v1", isDirectory: true)
        }
        self.cacheRoot = resolvedCacheRoot
        self.cacheIndexURL = resolvedCacheRoot.appendingPathComponent(
            "cache-index-v2.json"
        )
        super.init()
        try? FileManager.default.createDirectory(
            at: self.cacheRoot,
            withIntermediateDirectories: true
        )
        loadCacheIndex()
        migrateLegacyCacheEntries()
        installRemoteCommands()
    }

    var currentSentence: String {
        guard sentences.indices.contains(currentIndex) else { return "" }
        return sentences[currentIndex]
    }

    var hasContent: Bool {
        !sentences.isEmpty
    }

    func start(
        text: String,
        title: String,
        chapter: String = "",
        bookID: String = "",
        configuration: ClickTTSConfiguration? = nil,
        artworkURL: URL? = nil
    ) {
        let prepared = Self.sentences(in: text)
        guard !prepared.isEmpty else { return }
        stop(clearContent: false)
        sentences = prepared
        isPlayerVisible = true
        currentIndex = 0
        bookTitle = title
        chapterTitle = chapter
        artworkImage = Self.image(at: artworkURL)
        self.configuration = configuration
        currentBookID = bookID.isEmpty
            ? (configuration?.bookID ?? "")
            : bookID
        if let locatorJSON = configuration?.locatorJSON,
           !locatorJSON.isEmpty {
            segmentLocatorJSONs = Array(
                repeating: locatorJSON,
                count: prepared.count
            )
            synchronizeCurrentSegment()
        }
        prefetchRemainingSeconds = 0
        pendingInitialPrefetch = true
        prefetchRetryAfter = .distantPast
        playCurrentSentence()
    }

    func start(
        queue: EPUBListeningQueue,
        title: String,
        bookID: String,
        configuration: ClickTTSConfiguration? = nil,
        artworkURL: URL? = nil,
        onSegmentChange: @escaping (String) -> Void
    ) {
        let preparedSegments = queue.segments.filter {
            Self.containsSpeakableContent($0.text)
        }
        guard !preparedSegments.isEmpty else { return }
        stop(clearContent: false)
        sentences = preparedSegments.map(\.text)
        isPlayerVisible = true
        segmentLocatorJSONs = preparedSegments.map(\.locatorJSON)
        segmentChapterTitles = preparedSegments.map(\.chapterTitle)
        currentIndex = 0
        bookTitle = title
        artworkImage = Self.image(at: artworkURL)
        self.configuration = configuration
        currentBookID = bookID
        prefetchRemainingSeconds = 0
        pendingInitialPrefetch = true
        prefetchRetryAfter = .distantPast
        self.onSegmentChange = onSegmentChange
        synchronizeCurrentSegment()
        playCurrentSentence()
    }

    func togglePlayback() {
        if let audioPlayer {
            if audioPlayer.isPlaying {
                audioPlayer.pause()
                isPlaying = false
                statusMessage = "已暂停"
            } else {
                do {
                    try activateAudioSession()
                    guard audioPlayer.play() else { throw ClickTTSError.playback }
                    isPlaying = true
                    statusMessage = "Click 微软语音"
                } catch {
                    failPlayback(error)
                }
            }
            updateNowPlaying()
            return
        }
        guard hasContent, !isPreparing else { return }
        playCurrentSentence()
    }

    func previous() {
        guard hasContent else { return }
        haltCurrentPlayback()
        currentIndex = max(0, currentIndex - 1)
        prefetchRemainingSeconds = 0
        pendingInitialPrefetch = true
        synchronizeCurrentSegment()
        playCurrentSentence()
    }

    func next() {
        guard hasContent else { return }
        haltCurrentPlayback()
        currentIndex = min(sentences.count - 1, currentIndex + 1)
        prefetchRemainingSeconds = 0
        pendingInitialPrefetch = true
        synchronizeCurrentSegment()
        playCurrentSentence()
    }

    func startCountdown(minutes: Int) {
        countdownTimer?.invalidate()
        let duration = TimeInterval(max(minutes, 1) * 60)
        countdownRemaining = duration
        shouldStopAfterCurrentSentence = false

        countdownTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] timer in
            Task { @MainActor in
                guard let self else {
                    timer.invalidate()
                    return
                }
                let remaining = max(0, (self.countdownRemaining ?? 0) - 1)
                self.countdownRemaining = remaining
                if remaining <= 0 {
                    timer.invalidate()
                    self.countdownTimer = nil
                    self.shouldStopAfterCurrentSentence = true
                    if !self.isPlaying && !self.isPreparing {
                        self.stop()
                    }
                }
            }
        }
    }

    func cancelCountdown() {
        countdownTimer?.invalidate()
        countdownTimer = nil
        countdownRemaining = nil
        shouldStopAfterCurrentSentence = false
    }

    func hidePlayer() {
        isPlayerVisible = false
    }

    func showPlayer() {
        guard hasContent else { return }
        isPlayerVisible = true
    }

    func setArtwork(url: URL?) {
        guard let image = Self.image(at: url) else { return }
        artworkImage = image
        updateNowPlaying()
    }

    func stop(clearContent: Bool = true) {
        playbackGeneration += 1
        playbackTask?.cancel()
        playbackTask = nil
        prefetchTask?.cancel()
        prefetchTask = nil
        prefetchRunID = nil
        prefetchRemainingSeconds = 0
        pendingInitialPrefetch = false
        prefetchRetryAfter = .distantPast
        audioFetchTasks.values.forEach { $0.task.cancel() }
        audioFetchTasks.removeAll()
        persistCacheIndexNow()
        audioPlayer?.stop()
        audioPlayer = nil
        isPlaying = false
        isPreparing = false
        cancelCountdown()
        configuration = nil
        segmentLocatorJSONs = []
        segmentChapterTitles = []
        currentBookID = ""
        currentSegmentLocatorJSON = ""
        onSegmentChange = nil
        if clearContent {
            sentences = []
            isPlayerVisible = false
            currentIndex = 0
            bookTitle = ""
            chapterTitle = ""
            engineLabel = "Click 微软 · 云健"
            statusMessage = ""
            artworkImage = nil
        }
        MPNowPlayingInfoCenter.default().nowPlayingInfo = nil
        try? AVAudioSession.sharedInstance().setActive(
            false,
            options: [.notifyOthersOnDeactivation]
        )
    }

    private func haltCurrentPlayback() {
        playbackGeneration += 1
        playbackTask?.cancel()
        playbackTask = nil
        prefetchTask?.cancel()
        prefetchTask = nil
        prefetchRunID = nil
        audioFetchTasks.values.forEach { $0.task.cancel() }
        audioFetchTasks.removeAll()
        audioPlayer?.stop()
        audioPlayer = nil
        isPlaying = false
        isPreparing = false
    }

    private func playCurrentSentence() {
        while sentences.indices.contains(currentIndex),
              !Self.containsSpeakableContent(sentences[currentIndex]) {
            currentIndex += 1
            if sentences.indices.contains(currentIndex) {
                synchronizeCurrentSegment()
            }
        }
        guard sentences.indices.contains(currentIndex) else {
            stop()
            return
        }
        let sentence = sentences[currentIndex]
        let generation = playbackGeneration
        let cachedURL = cacheURL(for: sentence)

        if FileManager.default.fileExists(atPath: cachedURL.path) {
            playMicrosoftAudio(at: cachedURL, generation: generation)
            schedulePrefetch(generation: generation)
            return
        }

        guard let activeConfiguration = configurationForCurrentIndex() else {
            failMicrosoftOnlyPlayback(
                reason: "当前无法连接 Click 微软语音，且这句尚未缓存",
                generation: generation
            )
            return
        }

        isPreparing = true
        isPlaying = false
        engineLabel = "Click 微软 · 云健"
        statusMessage = "正在准备 Click 微软语音"
        updateNowPlaying()
        playbackTask = Task { [weak self] in
            guard let self else { return }
            do {
                let url = try await self.microsoftAudioURL(
                    for: sentence,
                    configuration: activeConfiguration
                )
                try Task.checkCancellation()
                guard generation == self.playbackGeneration,
                      sentence == self.currentSentence else {
                    return
                }
                self.playMicrosoftAudio(at: url, generation: generation)
                self.prefetchRetryAfter = .distantPast
                self.schedulePrefetch(generation: generation)
            } catch is CancellationError {
                return
            } catch {
                guard generation == self.playbackGeneration else { return }
                let detail = error.localizedDescription
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                self.failMicrosoftOnlyPlayback(
                    reason: detail.isEmpty
                        ? "Click 微软语音暂不可用"
                        : "Click 微软语音失败：\(detail.prefix(100))",
                    generation: generation
                )
            }
        }
    }

    private func playMicrosoftAudio(at url: URL, generation: Int) {
        guard generation == playbackGeneration else { return }
        do {
            try activateAudioSession()
            let player = try AVAudioPlayer(contentsOf: url)
            player.delegate = self
            player.enableRate = true
            player.rate = microsoftPlaybackRate
            player.prepareToPlay()
            guard player.play() else { throw ClickTTSError.playback }
            audioPlayer = player
            isPreparing = false
            isPlaying = true
            engineLabel = "Click 微软 · 云健"
            statusMessage = "Click 微软语音"
            try? FileManager.default.setAttributes(
                [.modificationDate: Date()],
                ofItemAtPath: url.path
            )
            registerCacheFile(
                url,
                duration: player.duration,
                bookID: configurationForCurrentIndex()?.bookID
            )
            updateNowPlaying()
            corruptCacheRecoveryKey = nil
        } catch {
            let cacheKey = url.lastPathComponent
            if corruptCacheRecoveryKey != cacheKey,
               configurationForCurrentIndex() != nil {
                corruptCacheRecoveryKey = cacheKey
                try? FileManager.default.removeItem(at: url)
                audioPlayer = nil
                isPreparing = false
                isPlaying = false
                playCurrentSentence()
            } else {
                failMicrosoftOnlyPlayback(
                    reason: "微软语音文件无法播放",
                    generation: generation
                )
            }
        }
    }

    private func failMicrosoftOnlyPlayback(reason: String, generation: Int) {
        guard generation == playbackGeneration else { return }
        playbackTask = nil
        audioPlayer?.stop()
        audioPlayer = nil
        isPreparing = false
        isPlaying = false
        engineLabel = "Click 微软 · 云健"
        statusMessage = reason
        updateNowPlaying()
    }

    private func finishCurrentSentence() {
        guard hasContent else { return }
        isPlaying = false
        isPreparing = false
        audioPlayer = nil
        if shouldStopAfterCurrentSentence || currentIndex >= sentences.count - 1 {
            stop()
            return
        }
        currentIndex += 1
        synchronizeCurrentSegment()
        playCurrentSentence()
    }

    private func synchronizeCurrentSegment() {
        if segmentChapterTitles.indices.contains(currentIndex) {
            chapterTitle = segmentChapterTitles[currentIndex]
        }
        guard segmentLocatorJSONs.indices.contains(currentIndex) else { return }
        let locatorJSON = segmentLocatorJSONs[currentIndex]
        currentSegmentLocatorJSON = locatorJSON
        onSegmentChange?(locatorJSON)
    }

    private func configurationForCurrentIndex() -> ClickTTSConfiguration? {
        guard let configuration else { return nil }
        let locatorJSON = segmentLocatorJSONs.indices.contains(currentIndex)
            ? segmentLocatorJSONs[currentIndex]
            : configuration.locatorJSON
        return ClickTTSConfiguration(
            baseURL: configuration.baseURL,
            deviceID: configuration.deviceID,
            accessToken: configuration.accessToken,
            bookID: configuration.bookID,
            locatorJSON: locatorJSON
        )
    }

    private func failPlayback(_ error: Error) {
        isPreparing = false
        isPlaying = false
        engineLabel = "Click 微软 · 云健"
        statusMessage = error.localizedDescription
        updateNowPlaying()
    }

    private func activateAudioSession() throws {
        let session = AVAudioSession.sharedInstance()
        do {
            try session.setCategory(
                .playback,
                mode: .spokenAudio,
                options: []
            )
            try session.setActive(true)
        } catch {
            do {
                // Some physical routes reject spokenAudio even though ordinary
                // playback is available. Keep the fallback deliberately plain;
                // .playback already follows wired and Bluetooth output routes.
                try session.setCategory(
                    .playback,
                    mode: .default,
                    options: []
                )
                try session.setActive(true)
            } catch {
                throw ClickTTSError.audioSession(error.localizedDescription)
            }
        }
    }

    private var microsoftPlaybackRate: Float {
        let normalized = (speechRate - 0.35) / (0.62 - 0.35)
        return min(max(0.8 + normalized * 0.7, 0.8), 1.5)
    }

    private func microsoftAudioURL(
        for text: String,
        configuration: ClickTTSConfiguration
    ) async throws -> URL {
        let destination = cacheURL(for: text)
        if FileManager.default.fileExists(atPath: destination.path) {
            registerExistingCacheFile(
                destination,
                bookID: configuration.bookID
            )
            return destination
        }

        let cacheKey = destination.lastPathComponent
        if let existing = audioFetchTasks[cacheKey] {
            let url = try await existing.task.value
            registerExistingCacheFile(url, bookID: configuration.bookID)
            return url
        }
        let fetchID = UUID()
        let fetchTask = Task<URL, Error> { [weak self] in
            guard let self else { throw CancellationError() }
            var lastError: Error = ClickTTSError.invalidResponse
            for attempt in 0..<2 {
                do {
                    return try await self.downloadMicrosoftAudio(
                        text: text,
                        configuration: configuration,
                        destination: destination
                    )
                } catch is CancellationError {
                    throw CancellationError()
                } catch {
                    lastError = error
                    guard attempt == 0, Self.shouldRetryMicrosoftFetch(error) else {
                        throw error
                    }
                    try await Task.sleep(for: .milliseconds(650))
                }
            }
            throw lastError
        }
        audioFetchTasks[cacheKey] = (fetchID, fetchTask)
        do {
            let url = try await fetchTask.value
            if audioFetchTasks[cacheKey]?.id == fetchID {
                audioFetchTasks.removeValue(forKey: cacheKey)
            }
            registerExistingCacheFile(url, bookID: configuration.bookID)
            return url
        } catch {
            if audioFetchTasks[cacheKey]?.id == fetchID {
                audioFetchTasks.removeValue(forKey: cacheKey)
            }
            throw error
        }
    }

    private func downloadMicrosoftAudio(
        text: String,
        configuration: ClickTTSConfiguration,
        destination: URL
    ) async throws -> URL {
        if FileManager.default.fileExists(atPath: destination.path) {
            return destination
        }

        let locator = Self.jsonObject(from: configuration.locatorJSON)
        let requestBody = try JSONSerialization.data(withJSONObject: [
            "text": text,
            "kind": "reader_sentence",
            "voice": Self.microsoftVoice,
            "book_id": configuration.bookID,
            "locator": locator,
        ])

        // Current Click releases expose /v1/mobile/tts. Older paired Mac
        // releases expose the same Microsoft Edge provider at /lookup/tts.
        // A 404 may use that protocol-compatible route; no other error and no
        // non-Microsoft engine is allowed to fall through.
        let metadataPaths = ["/v1/mobile/tts", "/lookup/tts"]
        var payload: [String: Any]?
        for (index, path) in metadataPaths.enumerated() {
            var request = authorizedRequest(
                path: path,
                baseURL: configuration.baseURL,
                configuration: configuration,
                method: "POST"
            )
            request.timeoutInterval = 60
            request.setValue(
                "application/json; charset=utf-8",
                forHTTPHeaderField: "Content-Type"
            )
            request.httpBody = requestBody
            let (metadata, response) = try await urlSession.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                throw ClickTTSError.invalidResponse
            }
            if http.statusCode == 404, index == 0 {
                continue
            }
            guard (200..<300).contains(http.statusCode),
                  let object = try? JSONSerialization.jsonObject(
                    with: metadata
                  ) as? [String: Any] else {
                throw ClickTTSError.invalidResponse
            }
            payload = object
            break
        }
        guard let payload,
              payload["ok"] as? Bool == true,
              payload["engine"] as? String == "edge-tts",
              payload["voice"] as? String == Self.microsoftVoice,
              let rawAudioURL = payload["audio_url"] as? String,
              !rawAudioURL.isEmpty else {
            throw ClickTTSError.server(payload?["error"] as? String ?? "")
        }

        guard let audioURL = URL(
            string: rawAudioURL,
            relativeTo: configuration.baseURL
        )?.absoluteURL,
        Self.isSameOrigin(audioURL, configuration.baseURL) else {
            throw ClickTTSError.invalidResponse
        }
        var audioRequest = authorizedRequest(
            url: audioURL,
            configuration: configuration
        )
        audioRequest.timeoutInterval = 60
        let (audioData, audioResponse) = try await urlSession.data(for: audioRequest)
        guard let audioHTTP = audioResponse as? HTTPURLResponse,
              (200..<300).contains(audioHTTP.statusCode),
              audioData.count > 512 else {
            throw ClickTTSError.emptyAudio
        }
        guard let probe = try? AVAudioPlayer(data: audioData),
              probe.duration > 0 else {
            throw ClickTTSError.playback
        }
        try FileManager.default.createDirectory(
            at: cacheRoot,
            withIntermediateDirectories: true
        )
        try audioData.write(to: destination, options: .atomic)
        registerCacheFile(
            destination,
            duration: probe.duration,
            byteCount: audioData.count,
            bookID: configuration.bookID
        )
        return destination
    }

    private func authorizedRequest(
        path: String,
        baseURL: URL,
        configuration: ClickTTSConfiguration,
        method: String
    ) -> URLRequest {
        let url = URL(string: path, relativeTo: baseURL)?.absoluteURL
            ?? baseURL.appendingPathComponent(path)
        var request = authorizedRequest(url: url, configuration: configuration)
        request.httpMethod = method
        return request
    }

    private func authorizedRequest(
        url: URL,
        configuration: ClickTTSConfiguration
    ) -> URLRequest {
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.timeoutInterval = 15
        request.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue(configuration.deviceID, forHTTPHeaderField: "X-Click-Device-ID")
        let token = configuration.accessToken
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if !token.isEmpty {
            request.setValue(token, forHTTPHeaderField: "X-Click-Access-Token")
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        return request
    }

    private func schedulePrefetch(generation: Int) {
        guard let configuration,
              generation == playbackGeneration,
              Date() >= prefetchRetryAfter,
              currentIndex < sentences.count - 1 else {
            return
        }
        let cachedSeconds = cachedListeningSeconds(from: currentIndex + 1)
        if pendingInitialPrefetch {
            pendingInitialPrefetch = false
            prefetchRemainingSeconds = max(
                0,
                Self.initialCacheSeconds - cachedSeconds
            )
        } else if prefetchRemainingSeconds <= 0 {
            guard cachedSeconds < Self.refillLowWaterSeconds else { return }
            prefetchRemainingSeconds = Self.refillChunkSeconds
        }
        guard prefetchRemainingSeconds > 0,
              prefetchTask == nil else {
            return
        }
        let runID = UUID()
        prefetchRunID = runID
        let startIndex = currentIndex + 1
        prefetchTask = Task(priority: .utility) { [weak self] in
            guard let self else { return }
            var index = startIndex
            var reachedEnd = false
            var failed = false
            while index < self.sentences.count {
                if Task.isCancelled || generation != self.playbackGeneration {
                    break
                }
                if index <= self.currentIndex {
                    index = self.currentIndex + 1
                    continue
                }
                if self.prefetchRemainingSeconds <= 0 {
                    break
                }
                let sentence = self.sentences[index]
                guard Self.containsSpeakableContent(sentence) else {
                    index += 1
                    continue
                }
                let destination = self.cacheURL(for: sentence)
                let wasCached = FileManager.default.fileExists(
                    atPath: destination.path
                )
                let locatorJSON = self.segmentLocatorJSONs.indices.contains(index)
                    ? self.segmentLocatorJSONs[index]
                    : configuration.locatorJSON
                let candidateConfiguration = ClickTTSConfiguration(
                    baseURL: configuration.baseURL,
                    deviceID: configuration.deviceID,
                    accessToken: configuration.accessToken,
                    bookID: configuration.bookID,
                    locatorJSON: locatorJSON
                )
                do {
                    let url = try await self.microsoftAudioURL(
                        for: sentence,
                        configuration: candidateConfiguration
                    )
                    if !wasCached,
                       let entry = self.cacheEntries[url.lastPathComponent] {
                        self.prefetchRemainingSeconds = max(
                            0,
                            self.prefetchRemainingSeconds
                                - entry.duration / Double(self.microsoftPlaybackRate)
                        )
                    }
                } catch is CancellationError {
                    break
                } catch {
                    failed = true
                    break
                }
                index += 1
                if self.cachedListeningSeconds(from: self.currentIndex + 1)
                    >= Self.urgentCacheSeconds {
                    try? await Task.sleep(for: .milliseconds(150))
                }
            }
            reachedEnd = index >= self.sentences.count
            guard self.prefetchRunID == runID else { return }
            self.prefetchTask = nil
            self.prefetchRunID = nil
            if !failed && (
                reachedEnd
                    || self.prefetchRemainingSeconds <= 0
            ) {
                self.prefetchRemainingSeconds = 0
            } else if failed {
                self.prefetchRetryAfter = Date().addingTimeInterval(
                    Self.prefetchFailureCooldown
                )
            }
        }
    }

    private func cacheURL(for text: String) -> URL {
        let material = "\(Self.microsoftVoice)\u{0}\(Self.normalizedCacheText(text))"
        let digest = SHA256.hash(data: Data(material.utf8))
            .map { String(format: "%02x", $0) }
            .joined()
        return cacheRoot.appendingPathComponent("\(digest).mp3")
    }

    private func cachedListeningSeconds(from startIndex: Int) -> TimeInterval {
        guard sentences.indices.contains(startIndex) else { return 0 }
        var total: TimeInterval = 0
        for index in startIndex..<sentences.count {
            let url = cacheURL(for: sentences[index])
            guard FileManager.default.fileExists(atPath: url.path) else { break }
            if cacheEntries[url.lastPathComponent] == nil {
                registerExistingCacheFile(url, bookID: configuration?.bookID)
            }
            guard let entry = cacheEntries[url.lastPathComponent] else { break }
            total += entry.duration / Double(microsoftPlaybackRate)
        }
        return total
    }

    private func registerExistingCacheFile(_ url: URL, bookID: String?) {
        let key = url.lastPathComponent
        if var entry = cacheEntries[key] {
            entry.lastAccessedAt = Date()
            if let bookID, !bookID.isEmpty {
                entry.bookIDs.insert(bookID)
            }
            cacheEntries[key] = entry
            scheduleCacheIndexPersist()
            return
        }
        guard let player = try? AVAudioPlayer(contentsOf: url),
              player.duration > 0 else {
            return
        }
        registerCacheFile(
            url,
            duration: player.duration,
            bookID: bookID
        )
    }

    private func registerCacheFile(
        _ url: URL,
        duration: TimeInterval,
        byteCount: Int? = nil,
        bookID: String?
    ) {
        guard duration > 0 else { return }
        let key = url.lastPathComponent
        let resolvedBytes = byteCount
            ?? (try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize)
            ?? 0
        var bookIDs = cacheEntries[key]?.bookIDs ?? []
        if let bookID, !bookID.isEmpty {
            bookIDs.insert(bookID)
        }
        cacheEntries[key] = ListeningAudioCacheEntry(
            filename: key,
            duration: duration,
            byteCount: resolvedBytes,
            lastAccessedAt: Date(),
            bookIDs: bookIDs
        )
        enforceCacheLimits()
        scheduleCacheIndexPersist()
    }

    private func protectedCacheKeys() -> Set<String> {
        guard hasContent else { return [] }
        var result: Set<String> = []
        var protectedSeconds: TimeInterval = 0
        for index in currentIndex..<sentences.count {
            let key = cacheURL(for: sentences[index]).lastPathComponent
            guard let entry = cacheEntries[key] else { break }
            result.insert(key)
            protectedSeconds += entry.duration / Double(microsoftPlaybackRate)
            if protectedSeconds >= Self.initialCacheSeconds {
                break
            }
        }
        return result
    }

    private func enforceCacheLimits() {
        cacheEntries = cacheEntries.filter { key, _ in
            FileManager.default.fileExists(
                atPath: cacheRoot.appendingPathComponent(key).path
            )
        }
        let protected = protectedCacheKeys()
        var effectiveSeconds = cacheEntries.values.reduce(0) {
            $0 + $1.duration / Double(microsoftPlaybackRate)
        }
        var totalBytes = cacheEntries.values.reduce(0) { $0 + $1.byteCount }
        let candidates = cacheEntries.values
            .filter { !protected.contains($0.filename) }
            .sorted { $0.lastAccessedAt < $1.lastAccessedAt }
        for entry in candidates where
            effectiveSeconds > Self.globalCacheSeconds
                || totalBytes > Self.globalCacheBytes {
            let url = cacheRoot.appendingPathComponent(entry.filename)
            do {
                try FileManager.default.removeItem(at: url)
                cacheEntries.removeValue(forKey: entry.filename)
                effectiveSeconds -= entry.duration / Double(microsoftPlaybackRate)
                totalBytes -= entry.byteCount
            } catch {
                continue
            }
        }
    }

    private func loadCacheIndex() {
        guard let data = try? Data(contentsOf: cacheIndexURL),
              let index = try? JSONDecoder().decode(
                ListeningAudioCacheIndex.self,
                from: data
              ),
              index.schema == Self.cacheIndexSchema else {
            return
        }
        cacheEntries = index.entries
        enforceCacheLimits()
    }

    private func scheduleCacheIndexPersist() {
        cacheIndexPersistTask?.cancel()
        cacheIndexPersistTask = Task { [weak self] in
            do {
                try await Task.sleep(for: .seconds(1))
            } catch {
                return
            }
            self?.persistCacheIndexNow()
        }
    }

    private func persistCacheIndexNow() {
        cacheIndexPersistTask?.cancel()
        cacheIndexPersistTask = nil
        let index = ListeningAudioCacheIndex(
            schema: Self.cacheIndexSchema,
            entries: cacheEntries
        )
        guard let data = try? JSONEncoder().encode(index) else { return }
        try? data.write(to: cacheIndexURL, options: .atomic)
    }

    private func migrateLegacyCacheEntries() {
        let knownKeys = Set(cacheEntries.keys)
        let cacheRoot = cacheRoot
        legacyCacheMigrationTask = Task { [weak self] in
            let discovered = await Task.detached(priority: .background) {
                let keys: Set<URLResourceKey> = [
                    .isRegularFileKey,
                    .fileSizeKey,
                    .contentModificationDateKey,
                ]
                let files = (try? FileManager.default.contentsOfDirectory(
                    at: cacheRoot,
                    includingPropertiesForKeys: Array(keys),
                    options: [.skipsHiddenFiles]
                )) ?? []
                var entries: [ListeningAudioCacheEntry] = []
                for url in files where
                    url.pathExtension == "mp3"
                        && !knownKeys.contains(url.lastPathComponent) {
                    if Task.isCancelled { break }
                    guard let values = try? url.resourceValues(forKeys: keys),
                          values.isRegularFile == true,
                          let player = try? AVAudioPlayer(contentsOf: url),
                          player.duration > 0 else {
                        continue
                    }
                    entries.append(
                        ListeningAudioCacheEntry(
                            filename: url.lastPathComponent,
                            duration: player.duration,
                            byteCount: values.fileSize ?? 0,
                            lastAccessedAt: values.contentModificationDate ?? .distantPast,
                            bookIDs: []
                        )
                    )
                }
                return entries
            }.value
            guard let self else { return }
            for entry in discovered where self.cacheEntries[entry.filename] == nil {
                self.cacheEntries[entry.filename] = entry
            }
            self.enforceCacheLimits()
            self.persistCacheIndexNow()
            self.legacyCacheMigrationTask = nil
        }
    }

    private func updateNowPlaying() {
        guard hasContent else {
            MPNowPlayingInfoCenter.default().nowPlayingInfo = nil
            return
        }
        var info: [String: Any] = [
            MPMediaItemPropertyTitle: currentSentence,
            MPMediaItemPropertyAlbumTitle: bookTitle,
            MPNowPlayingInfoPropertyPlaybackRate: isPlaying ? 1.0 : 0.0,
            MPNowPlayingInfoPropertyElapsedPlaybackTime: TimeInterval(currentIndex),
            MPMediaItemPropertyPlaybackDuration: TimeInterval(sentences.count),
        ]
        if !chapterTitle.isEmpty {
            info[MPMediaItemPropertyArtist] = chapterTitle
        } else {
            info[MPMediaItemPropertyArtist] = engineLabel
        }
        if let artworkImage {
            info[MPMediaItemPropertyArtwork] = MPMediaItemArtwork(
                boundsSize: artworkImage.size
            ) { _ in artworkImage }
        }
        MPNowPlayingInfoCenter.default().nowPlayingInfo = info
    }

    private func installRemoteCommands() {
        guard !remoteCommandsInstalled else { return }
        remoteCommandsInstalled = true
        let commands = MPRemoteCommandCenter.shared()

        commands.playCommand.addTarget { [weak self] _ in
            Task { @MainActor in
                guard let self, !self.isPlaying else { return }
                self.togglePlayback()
            }
            return .success
        }
        commands.pauseCommand.addTarget { [weak self] _ in
            Task { @MainActor in
                guard let self, self.isPlaying else { return }
                self.togglePlayback()
            }
            return .success
        }
        commands.nextTrackCommand.addTarget { [weak self] _ in
            Task { @MainActor in self?.next() }
            return .success
        }
        commands.previousTrackCommand.addTarget { [weak self] _ in
            Task { @MainActor in self?.previous() }
            return .success
        }
    }

    nonisolated static func sentences(in text: String) -> [String] {
        let normalized = text
            .replacingOccurrences(of: "\r\n", with: "\n")
            .replacingOccurrences(of: "\r", with: "\n")
        var result: [String] = []
        normalized.enumerateSubstrings(
            in: normalized.startIndex..<normalized.endIndex,
            options: [.bySentences, .substringNotRequired]
        ) { _, range, _, _ in
            let sentence = normalized[range]
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if !sentence.isEmpty {
                result.append(sentence)
            }
        }
        if result.isEmpty {
            result = normalized
                .split(whereSeparator: \.isNewline)
                .map { String($0).trimmingCharacters(in: .whitespacesAndNewlines) }
                .filter { !$0.isEmpty }
        }
        return result.filter(containsSpeakableContent)
    }

    nonisolated static func containsSpeakableContent(_ text: String) -> Bool {
        text.unicodeScalars.contains {
            CharacterSet.alphanumerics.contains($0)
        }
    }

    private nonisolated static func jsonObject(from raw: String) -> [String: Any] {
        guard let data = raw.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return [:]
        }
        return object
    }

    private nonisolated static func normalizedCacheText(_ text: String) -> String {
        text
            .precomposedStringWithCanonicalMapping
            .replacingOccurrences(
                of: #"\s+"#,
                with: " ",
                options: .regularExpression
            )
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static func image(at url: URL?) -> UIImage? {
        guard let url else { return nil }
        return UIImage(contentsOfFile: url.path)
    }

    private static func shouldRetryMicrosoftFetch(_ error: Error) -> Bool {
        if error is CancellationError { return false }
        if let urlError = error as? URLError {
            return urlError.code != .cancelled
        }
        guard let clickError = error as? ClickTTSError else { return true }
        switch clickError {
        case .invalidResponse, .server, .emptyAudio, .playback:
            return true
        case .audioSession:
            return false
        }
    }

    private nonisolated static func isSameOrigin(_ lhs: URL, _ rhs: URL) -> Bool {
        guard
            lhs.user == nil,
            lhs.password == nil,
            rhs.user == nil,
            rhs.password == nil,
            let lhsScheme = lhs.scheme?.lowercased(),
            let rhsScheme = rhs.scheme?.lowercased(),
            let lhsHost = lhs.host?.lowercased(),
            let rhsHost = rhs.host?.lowercased()
        else {
            return false
        }
        return lhsScheme == rhsScheme
            && lhsHost == rhsHost
            && effectivePort(of: lhs, scheme: lhsScheme)
                == effectivePort(of: rhs, scheme: rhsScheme)
    }

    private nonisolated static func effectivePort(
        of url: URL,
        scheme: String
    ) -> Int? {
        if let port = url.port {
            return port
        }
        switch scheme {
        case "https":
            return 443
        case "http":
            return 80
        default:
            return nil
        }
    }

    private nonisolated static func prefetchIndexes(
        sentences: [String],
        startIndex: Int,
        targetSeconds: TimeInterval
    ) -> [Int] {
        guard sentences.indices.contains(startIndex) else { return [] }
        var duration: TimeInterval = 0
        var result: [Int] = []
        for index in startIndex..<sentences.count {
            let sentence = sentences[index]
            result.append(index)
            duration += estimatedDuration(for: sentence)
            if duration >= targetSeconds {
                break
            }
        }
        return result
    }

    private nonisolated static func estimatedDuration(for text: String) -> TimeInterval {
        let characters = max(1, text.count)
        let punctuation = text.filter { "，。！？；：,.!?;:".contains($0) }.count
        return max(1.2, Double(characters) / 4.2 + Double(punctuation) * 0.18)
    }
}

extension ListeningController: AVAudioPlayerDelegate {
    nonisolated func audioPlayerDidFinishPlaying(
        _ player: AVAudioPlayer,
        successfully flag: Bool
    ) {
        Task { @MainActor in
            guard player === audioPlayer else { return }
            if flag {
                finishCurrentSentence()
            } else {
                failPlayback(ClickTTSError.playback)
            }
        }
    }

    nonisolated func audioPlayerDecodeErrorDidOccur(
        _ player: AVAudioPlayer,
        error: Error?
    ) {
        Task { @MainActor in
            guard player === audioPlayer else { return }
            failPlayback(error ?? ClickTTSError.playback)
        }
    }
}
