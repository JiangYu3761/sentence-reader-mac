import AVFoundation
import Speech
import SwiftUI
import WebKit

struct EPUBTableOfContentsItem: Identifiable, Hashable {
    let id: String
    let title: String
    let depth: Int
}

struct EPUBSearchResult: Identifiable, Hashable, Sendable {
    let id: String
    let snippet: String
    let locatorJSON: String
}

struct EPUBNavigationConfirmation: Identifiable, Equatable {
    let id = UUID()
    let title: String
}

@MainActor
final class EPUBReaderCoordinator: ObservableObject {
    @Published private(set) var items: [EPUBTableOfContentsItem] = []
    @Published private(set) var currentLocatorJSON = ""
    @Published private(set) var currentProgress: Double?
    @Published private(set) var navigationConfirmation: EPUBNavigationConfirmation?
    private var tableOfContentsNavigationHandler: ((EPUBTableOfContentsItem) -> Void)?
    private var locatorNavigationHandler: ((String) -> Void)?
    private var searchHandler: ((String) async -> [EPUBSearchResult])?
    private var listeningQueueHandler: (() async -> EPUBListeningQueue?)?
    private var navigationConfirmationTask: Task<Void, Never>?

    func install(
        items: [EPUBTableOfContentsItem],
        tableOfContentsNavigationHandler: @escaping (EPUBTableOfContentsItem) -> Void,
        locatorNavigationHandler: @escaping (String) -> Void,
        searchHandler: @escaping (String) async -> [EPUBSearchResult],
        listeningQueueHandler: @escaping () async -> EPUBListeningQueue?
    ) {
        self.items = items
        self.tableOfContentsNavigationHandler = tableOfContentsNavigationHandler
        self.locatorNavigationHandler = locatorNavigationHandler
        self.searchHandler = searchHandler
        self.listeningQueueHandler = listeningQueueHandler
    }

    func navigate(to item: EPUBTableOfContentsItem) {
        tableOfContentsNavigationHandler?(item)
    }

    func confirmNavigation(to item: EPUBTableOfContentsItem) {
        let confirmation = EPUBNavigationConfirmation(title: item.title)
        navigationConfirmation = confirmation
        navigationConfirmationTask?.cancel()
        navigationConfirmationTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(2.4))
            guard !Task.isCancelled else { return }
            if self?.navigationConfirmation?.id == confirmation.id {
                self?.navigationConfirmation = nil
            }
        }
    }

    func navigate(toLocatorJSON locatorJSON: String) {
        locatorNavigationHandler?(locatorJSON)
    }

    func updateLocation(progress: Double?, locatorJSON: String) {
        currentProgress = progress
        currentLocatorJSON = locatorJSON
    }

    func search(_ query: String) async -> [EPUBSearchResult] {
        await searchHandler?(query) ?? []
    }

    func listeningQueue() async -> EPUBListeningQueue? {
        await listeningQueueHandler?()
    }

    func reset() {
        navigationConfirmationTask?.cancel()
        navigationConfirmationTask = nil
        items = []
        currentLocatorJSON = ""
        currentProgress = nil
        navigationConfirmation = nil
        tableOfContentsNavigationHandler = nil
        locatorNavigationHandler = nil
        searchHandler = nil
        listeningQueueHandler = nil
    }
}

struct ReaderNoteComposer: View {
    let title: String
    let excerpt: String
    let onSave: (String, URL?) -> Void

    @Environment(\.dismiss) private var dismiss
    @FocusState private var noteFocused: Bool
    @State private var note = ""
    @StateObject private var voice = VoiceNoteDraftController()

    private var trimmedNote: String {
        note.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    var body: some View {
        NavigationStack {
            Form {
                if !excerpt.isEmpty {
                    Section("选中文字") {
                        Text(excerpt)
                            .lineLimit(4)
                            .foregroundStyle(.secondary)
                    }
                }
                Section("备注") {
                    TextEditor(text: $note)
                        .focused($noteFocused)
                        .frame(minHeight: 120)
                }
                Section {
                    Button {
                        noteFocused = false
                        voice.toggleRecording()
                    } label: {
                        Label(
                            voice.isRecording ? "停止录音" : "语音备注",
                            systemImage: voice.isRecording ? "stop.circle.fill" : "mic.circle"
                        )
                        .foregroundStyle(
                            voice.isRecording ? Color.red : Color.accentColor
                        )
                    }
                    Text(voice.statusMessage)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                } footer: {
                    Text("原音保存在 iPad；优先设备端识别，不支持时使用苹果系统语音识别，不调用收费语音服务。")
                }
            }
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") {
                        voice.stopForDismissal()
                        dismiss()
                    }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存") {
                        onSave(trimmedNote, voice.audioURL)
                        dismiss()
                    }
                    .disabled(
                        voice.isRecording
                            || (trimmedNote.isEmpty && voice.audioURL == nil)
                    )
                }
            }
            .task {
                noteFocused = true
            }
            .onChange(of: voice.transcript) { _, transcript in
                let clean = transcript.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !clean.isEmpty else { return }
                note = clean
            }
            .onDisappear {
                voice.stopForDismissal()
            }
        }
    }
}

@MainActor
private final class VoiceNoteDraftController: NSObject, ObservableObject {
    @Published private(set) var isRecording = false
    @Published private(set) var audioURL: URL?
    @Published private(set) var transcript = ""
    @Published private(set) var statusMessage = "点击后录音，再次点击停止并转成文字"

    private var recorder: AVAudioRecorder?
    private var recognitionTask: SFSpeechRecognitionTask?
    private let draftsURL: URL

    override init() {
        let support = FileManager.default.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first ?? FileManager.default.temporaryDirectory
        draftsURL = support
            .appendingPathComponent("ClickWorkspace", isDirectory: true)
            .appendingPathComponent("VoiceNoteDrafts", isDirectory: true)
        super.init()
        try? FileManager.default.createDirectory(
            at: draftsURL,
            withIntermediateDirectories: true
        )
    }

    func toggleRecording() {
        if isRecording {
            stopAndTranscribe()
        } else {
            Task { await start() }
        }
    }

    func stopForDismissal() {
        guard let recorder else {
            recognitionTask?.cancel()
            recognitionTask = nil
            return
        }
        recorder.stop()
        audioURL = recorder.url
        self.recorder = nil
        isRecording = false
        statusMessage = "原音已保存在 iPad"
        try? AVAudioSession.sharedInstance().setActive(
            false,
            options: [.notifyOthersOnDeactivation]
        )
    }

    private func start() async {
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

        let destination = draftsURL.appendingPathComponent(
            "voice-note-\(UUID().uuidString.lowercased()).m4a"
        )
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatMPEG4AAC,
            AVSampleRateKey: 16_000,
            AVNumberOfChannelsKey: 1,
            AVEncoderAudioQualityKey: AVAudioQuality.high.rawValue,
        ]
        do {
            let session = AVAudioSession.sharedInstance()
            do {
                try session.setCategory(
                    .record,
                    mode: .measurement,
                    options: [.allowBluetoothHFP]
                )
                try session.setActive(true)
            } catch {
                try session.setCategory(
                    .record,
                    mode: .default,
                    options: []
                )
                try session.setActive(true)
            }
            let recorder = try AVAudioRecorder(url: destination, settings: settings)
            recorder.prepareToRecord()
            guard recorder.record() else {
                statusMessage = "录音没有启动"
                return
            }
            self.recorder = recorder
            audioURL = nil
            transcript = ""
            isRecording = true
            statusMessage = "正在录音；点击停止"
        } catch {
            statusMessage = "无法录音：\(error.localizedDescription)"
        }
    }

    private func stopAndTranscribe() {
        guard let recorder else { return }
        recorder.stop()
        let url = recorder.url
        self.recorder = nil
        isRecording = false
        audioURL = url
        statusMessage = "原音已保存，正在尝试设备端转文字…"
        try? AVAudioSession.sharedInstance().setActive(
            false,
            options: [.notifyOthersOnDeactivation]
        )
        Task { await transcribeOnDevice(url: url) }
    }

    private func transcribeOnDevice(url: URL) async {
        let authorization = await withCheckedContinuation { continuation in
            SFSpeechRecognizer.requestAuthorization { status in
                continuation.resume(returning: status)
            }
        }
        guard authorization == .authorized else {
            statusMessage = "原音已保存；未授权语音转文字"
            return
        }
        guard
            let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "zh-CN")),
            recognizer.isAvailable
        else {
            statusMessage = "原音已保存；系统中文语音识别当前不可用"
            return
        }
        let request = SFSpeechURLRecognitionRequest(url: url)
        request.requiresOnDeviceRecognition = recognizer.supportsOnDeviceRecognition
        request.shouldReportPartialResults = true
        statusMessage = recognizer.supportsOnDeviceRecognition
            ? "原音已保存，正在设备端转文字…"
            : "原音已保存，正在用系统语音识别转文字…"
        recognitionTask?.cancel()
        recognitionTask = recognizer.recognitionTask(with: request) {
            [weak self] result, error in
            Task { @MainActor in
                guard let self else { return }
                if let result {
                    self.transcript = result.bestTranscription.formattedString
                    if result.isFinal {
                        self.statusMessage = "原音与设备端文字都已准备好"
                        self.recognitionTask = nil
                    }
                } else if error != nil {
                    self.statusMessage = "原音已保存；本次没有识别出文字"
                    self.recognitionTask = nil
                }
            }
        }
    }
}

#if canImport(ReadiumNavigator) && canImport(ReadiumShared) && canImport(ReadiumStreamer)
@_spi(ExperimentalTargetElement) import ReadiumNavigator
import ReadiumShared
import ReadiumStreamer
import UIKit

@MainActor
private final class ReadiumEnvironment {
    private lazy var httpClient: HTTPClient = DefaultHTTPClient(
        ephemeral: true
    )
    private lazy var assetRetriever = AssetRetriever(
        httpClient: httpClient
    )
    private lazy var publicationOpener = PublicationOpener(
        parser: DefaultPublicationParser(
            httpClient: httpClient,
            assetRetriever: assetRetriever,
            pdfFactory: DefaultPDFDocumentFactory()
        ),
        contentProtections: []
    )

    func open(fileURL: URL) async throws -> Publication {
        guard let absoluteURL = FileURL(url: fileURL) else {
            throw ReadiumBridgeError.invalidFileURL
        }
        let asset = try await assetRetriever.retrieve(url: absoluteURL).get()
        return try await publicationOpener.open(
            asset: asset,
            allowUserInteraction: false,
            sender: nil
        ).get()
    }
}

@MainActor
enum LocalBookCoverExtractor {
    struct Extraction {
        let coverJPEGData: Data?
        let title: String?
        let author: String?
    }

    static func extract(from fileURL: URL) async -> Extraction? {
        let environment = ReadiumEnvironment()
        guard let publication = try? await environment.open(fileURL: fileURL) else {
            return nil
        }
        let image = try? await publication.coverFitting(
            maxSize: CGSize(width: 1_200, height: 1_800)
        ).get()
        let authors = publication.metadata.authors
            .map(\.name)
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        return Extraction(
            coverJPEGData: image?.jpegData(compressionQuality: 0.86),
            title: publication.metadata.title,
            author: authors.isEmpty ? nil : authors.joined(separator: "、")
        )
    }

    static func jpegData(for fileURL: URL) async -> Data? {
        await extract(from: fileURL)?.coverJPEGData
    }
}

private enum ReadiumBridgeError: LocalizedError {
    case invalidFileURL

    var errorDescription: String? {
        "这本 EPUB 的本地文件地址无效"
    }
}

@MainActor
private final class ReadiumBookSession: ObservableObject {
    @Published private(set) var publication: Publication?
    @Published private(set) var errorMessage: String?
    @Published private(set) var isOpening = false

    private let environment = ReadiumEnvironment()

    func open(fileURL: URL) async {
        close()
        isOpening = true
        errorMessage = nil
        do {
            let opened = try await environment.open(fileURL: fileURL)
            guard !Task.isCancelled else {
                isOpening = false
                return
            }
            publication = opened
            isOpening = false
        } catch {
            errorMessage = "无法打开这本 EPUB：\(error.localizedDescription)"
            isOpening = false
        }
    }

    func close() {
        publication = nil
    }
}

private struct EPUBSelectionPayload: Identifiable {
    let id = UUID()
    let excerpt: String
    let locatorJSON: String
    let matchingHighlightIDs: [UUID]
}

struct ReadiumEPUBReaderView: View {
    let book: ClickBook
    let fileURL: URL
    let fontScale: Double
    let lineSpacing: Double
    let horizontalMargin: Double
    let readerTheme: ReaderThemeChoice
    let readerTypeface: ReaderTypefaceChoice
    @ObservedObject var workspace: WorkspaceStore
    @ObservedObject var connection: ConnectionStore
    @ObservedObject var listening: ListeningController
    @ObservedObject var coordinator: EPUBReaderCoordinator
    let onToggleControls: () -> Void

    @StateObject private var session = ReadiumBookSession()
    @State private var pendingNote: EPUBSelectionPayload?
    @State private var navigatorError: String?

    var body: some View {
        Group {
            if let publication = session.publication {
                EPUBNavigatorHost(
                    publication: publication,
                    initialLocatorJSON: book.lastLocatorJSON,
                    preferences: readerPreferences,
                    annotations: workspace.annotations(for: book.id),
                    listeningLocatorJSON: listening.currentBookID == book.id.uuidString
                        ? listening.currentSegmentLocatorJSON
                        : "",
                    onLocation: { progress, locatorJSON in
                        coordinator.updateLocation(
                            progress: progress,
                            locatorJSON: locatorJSON
                        )
                        workspace.updateEPUBLocation(
                            bookID: book.id,
                            progress: progress,
                            locatorJSON: locatorJSON
                        )
                    },
                    onHighlight: { payload in
                        _ = workspace.addHighlight(
                            bookID: book.id,
                            excerpt: payload.excerpt,
                            locatorJSON: payload.locatorJSON
                        )
                    },
                    onAutomaticHighlight: { payload, replacingID in
                        if let replacingID,
                           let updatedID = workspace.updateHighlight(
                               bookID: book.id,
                               annotationID: replacingID,
                               excerpt: payload.excerpt,
                               locatorJSON: payload.locatorJSON
                           ) {
                            return updatedID
                        }
                        return workspace.addHighlight(
                            bookID: book.id,
                            excerpt: payload.excerpt,
                            locatorJSON: payload.locatorJSON
                        )
                    },
                    onRemoveHighlight: { payload in
                        workspace.removeHighlights(
                            bookID: book.id,
                            annotationIDs: payload.matchingHighlightIDs
                        )
                    },
                    onNote: { payload in
                        pendingNote = payload
                    },
                    onPlaySentence: { payload in
                        listening.start(
                            text: payload.excerpt,
                            title: book.displayTitle,
                            bookID: book.id.uuidString,
                            configuration: connection.clickTTSConfiguration(
                                bookID: book.id.uuidString,
                                locatorJSON: payload.locatorJSON
                            )
                        )
                    },
                    onError: { navigatorError = $0 },
                    onToggleControls: onToggleControls,
                    coordinator: coordinator
                )
            } else if let errorMessage = session.errorMessage {
                ContentUnavailableView {
                    Label("无法打开 EPUB", systemImage: "exclamationmark.triangle")
                } description: {
                    Text(errorMessage)
                } actions: {
                    Button("重试") {
                        Task { await session.open(fileURL: fileURL) }
                    }
                }
            } else {
                ProgressView("正在打开 EPUB…")
            }
        }
        .task(id: fileURL) {
            await session.open(fileURL: fileURL)
        }
        .onDisappear {
            session.close()
            coordinator.reset()
        }
        .sheet(item: $pendingNote) { selection in
            ReaderNoteComposer(
                title: "添加备注",
                excerpt: selection.excerpt
            ) { note, audioURL in
                workspace.addNote(
                    bookID: book.id,
                    excerpt: selection.excerpt,
                    note: note,
                    locatorJSON: selection.locatorJSON,
                    audioDraftURL: audioURL
                )
            }
            .presentationDetents([.height(390), .medium])
            .presentationDragIndicator(.visible)
        }
        .alert(
            "阅读器提示",
            isPresented: Binding(
                get: { navigatorError != nil },
                set: { if !$0 { navigatorError = nil } }
            )
        ) {
            Button("知道了", role: .cancel) {}
        } message: {
            Text(navigatorError ?? "")
        }
    }

    private var readerPreferences: EPUBPreferences {
        EPUBPreferences(
            backgroundColor: ReadiumNavigator.Color(
                hex: readerTheme.readiumBackgroundHex
            ),
            // A two-column spread snaps every in-chapter anchor to the start
            // of the whole spread. Nearby TOC entries such as “原理二” can
            // therefore appear to do nothing. One reflowable column keeps
            // every directory anchor addressable as its own page.
            columnCount: .one,
            fontFamily: readiumFontFamily,
            fontSize: fontScale,
            lineHeight: 1.05 + (lineSpacing / 28),
            // Readium's paginated WebView uses fractional column widths.
            // A literal zero gutter can expose a few pixels of the next
            // column after a turn, so keep a visually negligible 2–5px floor.
            pageMargins: max(0.1, horizontalMargin / 30),
            publisherStyles: false,
            scroll: false,
            textColor: ReadiumNavigator.Color(hex: readerTheme.readiumTextHex)
        )
    }

    private var readiumFontFamily: ReadiumNavigator.FontFamily? {
        switch readerTypeface {
        case .yahei:
            return ReadiumNavigator.FontFamily(rawValue: "PingFang SC")
        case .system:
            return .sansSerif
        }
    }
}

private struct EPUBNavigatorHost: UIViewControllerRepresentable {
    let publication: Publication
    let initialLocatorJSON: String?
    let preferences: EPUBPreferences
    let annotations: [ClickAnnotation]
    let listeningLocatorJSON: String
    let onLocation: (Double?, String) -> Void
    let onHighlight: (EPUBSelectionPayload) -> Void
    let onAutomaticHighlight: (EPUBSelectionPayload, UUID?) -> UUID?
    let onRemoveHighlight: (EPUBSelectionPayload) -> Void
    let onNote: (EPUBSelectionPayload) -> Void
    let onPlaySentence: (EPUBSelectionPayload) -> Void
    let onError: (String) -> Void
    let onToggleControls: () -> Void
    @ObservedObject var coordinator: EPUBReaderCoordinator

    func makeUIViewController(context: Context) -> ClickEPUBHostViewController {
        let controller = ClickEPUBHostViewController(
            publication: publication,
            initialLocator: initialLocatorJSON.flatMap {
                try? Locator(jsonString: $0)
            },
            preferences: preferences,
            onLocation: onLocation,
            onHighlight: onHighlight,
            onAutomaticHighlight: onAutomaticHighlight,
            onRemoveHighlight: onRemoveHighlight,
            onNote: onNote,
            onPlaySentence: onPlaySentence,
            onError: onError,
            onToggleControls: onToggleControls
        )
        controller.applyAnnotations(annotations)
        controller.applyListeningIndicator(locatorJSON: listeningLocatorJSON)
        let flattened = EPUBTableOfContentsMap.flatten(
            publication.manifest.tableOfContents
        )
        coordinator.install(
            items: flattened.map { $0.item },
            tableOfContentsNavigationHandler: {
                [weak controller, weak coordinator] item in
                controller?.navigate(
                    toTableOfContentsItem: item.id,
                    onSuccess: {
                        coordinator?.confirmNavigation(to: item)
                    }
                )
            },
            locatorNavigationHandler: { [weak controller] locatorJSON in
                controller?.navigate(toLocatorJSON: locatorJSON)
            },
            searchHandler: { query in
                await EPUBPublicationSearch.search(
                    publication: publication,
                    query: query
                )
            },
            listeningQueueHandler: { [weak controller] in
                guard let controller else { return nil }
                return await controller.makeListeningQueue()
            }
        )
        return controller
    }

    func updateUIViewController(
        _ controller: ClickEPUBHostViewController,
        context: Context
    ) {
        controller.updateCallbacks(
            onLocation: onLocation,
            onHighlight: onHighlight,
            onAutomaticHighlight: onAutomaticHighlight,
            onRemoveHighlight: onRemoveHighlight,
            onNote: onNote,
            onPlaySentence: onPlaySentence,
            onError: onError,
            onToggleControls: onToggleControls
        )
        controller.submit(preferences: preferences)
        controller.applyAnnotations(annotations)
        controller.applyListeningIndicator(locatorJSON: listeningLocatorJSON)
    }
}

@MainActor
private final class ClickEPUBHostViewController:
    UIViewController,
    EPUBNavigatorDelegate
{
    private var navigator: EPUBNavigatorViewController?
    private var onLocation: (Double?, String) -> Void
    private var onHighlight: (EPUBSelectionPayload) -> Void
    private var onAutomaticHighlight: (EPUBSelectionPayload, UUID?) -> UUID?
    private var onRemoveHighlight: (EPUBSelectionPayload) -> Void
    private var onNote: (EPUBSelectionPayload) -> Void
    private var onPlaySentence: (EPUBSelectionPayload) -> Void
    private var onError: (String) -> Void
    private var onToggleControls: () -> Void
    private var pendingAnnotations: [ClickAnnotation] = []
    private var pendingListeningLocatorJSON = ""
    private var appliedListeningLocatorJSON = ""
    private var tableOfContentsTargetTask: Task<Void, Never>?
    private var inputObserverTokens: Set<InputObservableToken> = []
    private let tableOfContentsLinks: [String: ReadiumShared.Link]
    private let publication: Publication
    private var preferredListeningStartLocator: Locator?
    private var programmaticLocatorNavigationDepth = 0
    private var readerBackgroundColor: UIColor
    private var automaticHighlightSettlingTask: Task<Void, Never>?
    private var automaticHighlightSessionWatcherTask: Task<Void, Never>?
    private var automaticHighlightSession: AutomaticHighlightSession?

    private struct AutomaticHighlightSession {
        let annotationID: UUID
        let locator: Locator
    }

    private static func bundledFontFamilyDeclarations()
        -> [AnyHTMLFontFamilyDeclaration]
    {
        guard
            let url = Bundle.main.url(
                forResource: "SourceHanSansCN-VF",
                withExtension: "otf"
            ),
            let file = FileURL(url: url)
        else {
            return []
        }

        return [
            CSSFontFamilyDeclaration(
                fontFamily: ReadiumNavigator.FontFamily(
                    rawValue: "Source Han Sans CN VF"
                ),
                alternates: [
                    ReadiumNavigator.FontFamily(rawValue: "PingFang SC"),
                    .sansSerif,
                ],
                fontFaces: [
                    CSSFontFace(
                        file: file,
                        preload: true,
                        style: .normal,
                        weight: .variable(250 ... 900)
                    )
                ]
            ).eraseToAnyHTMLFontFamilyDeclaration()
        ]
    }

    private static let fullPageTextFlowScript = #"""
    (() => {
      const styleID = "click-full-page-text-flow-v1";
      if (document.getElementById(styleID)) return;
      const style = document.createElement("style");
      style.id = styleID;
      style.textContent = `
        body,
        p,
        blockquote,
        li,
        [role="doc-paragraph"] {
          -webkit-column-break-inside: auto !important;
          page-break-inside: auto !important;
          break-inside: auto !important;
          orphans: 1 !important;
          widows: 1 !important;
        }
      `;
      (document.head || document.documentElement).appendChild(style);
    })();
    """#

    private static let addHighlightAction = EditingAction(
        title: "标红",
        action: #selector(highlightSelection)
    )
    private static let removeHighlightAction = EditingAction(
        title: "取消标红",
        action: #selector(removeHighlightSelection)
    )

    init(
        publication: Publication,
        initialLocator: Locator?,
        preferences: EPUBPreferences,
        onLocation: @escaping (Double?, String) -> Void,
        onHighlight: @escaping (EPUBSelectionPayload) -> Void,
        onAutomaticHighlight: @escaping (
            EPUBSelectionPayload,
            UUID?
        ) -> UUID?,
        onRemoveHighlight: @escaping (EPUBSelectionPayload) -> Void,
        onNote: @escaping (EPUBSelectionPayload) -> Void,
        onPlaySentence: @escaping (EPUBSelectionPayload) -> Void,
        onError: @escaping (String) -> Void,
        onToggleControls: @escaping () -> Void
    ) {
        self.onLocation = onLocation
        self.onHighlight = onHighlight
        self.onAutomaticHighlight = onAutomaticHighlight
        self.onRemoveHighlight = onRemoveHighlight
        self.onNote = onNote
        self.onPlaySentence = onPlaySentence
        self.onError = onError
        self.onToggleControls = onToggleControls
        self.publication = publication
        readerBackgroundColor = preferences.backgroundColor?.uiColor ?? .systemBackground
        tableOfContentsLinks = Dictionary<String, ReadiumShared.Link>(
            uniqueKeysWithValues: EPUBTableOfContentsMap
                .flatten(publication.manifest.tableOfContents)
                .map { ($0.item.id, $0.link) }
        )
        super.init(nibName: nil, bundle: nil)

        do {
            navigator = try EPUBNavigatorViewController(
                publication: publication,
                initialLocation: initialLocator,
                config: EPUBNavigatorViewController.Configuration(
                    preferences: preferences,
                    editingActions: [
                        .copy,
                        Self.addHighlightAction,
                        Self.removeHighlightAction,
                        EditingAction(
                            title: "备注",
                            action: #selector(noteSelection)
                        ),
                        EditingAction(
                            title: "播放本句",
                            action: #selector(playSelection)
                        ),
                    ],
                    contentInset: [
                        .compact: (top: 0, bottom: 0),
                        .regular: (top: 0, bottom: 0),
                        .unspecified: (top: 0, bottom: 0),
                    ],
                    fontFamilyDeclarations: Self.bundledFontFamilyDeclarations()
                )
            )
            navigator?.delegate = self
        } catch {
            self.onError("EPUB 导航器启动失败：\(error.localizedDescription)")
        }
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = readerBackgroundColor
        view.clipsToBounds = true
        guard let navigator else { return }
        addChild(navigator)
        navigator.view.translatesAutoresizingMaskIntoConstraints = false
        navigator.view.clipsToBounds = true
        navigator.view.layer.masksToBounds = true
        view.addSubview(navigator.view)
        NSLayoutConstraint.activate([
            navigator.view.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            navigator.view.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            navigator.view.topAnchor.constraint(equalTo: view.topAnchor),
            navigator.view.bottomAnchor.constraint(equalTo: view.bottomAnchor),
        ])
        navigator.didMove(toParent: self)
        // Readium already provides interactive, paginated horizontal scrolling
        // through its nested scroll views. Do not add a second swipe recognizer:
        // it would compete with the native drag, then start another page turn
        // after the finger is released.
        installControlToggle(on: navigator)
        applyAnnotations(pendingAnnotations)
        applyListeningIndicator(locatorJSON: pendingListeningLocatorJSON)
    }

    func navigator(
        _ navigator: EPUBNavigatorViewController,
        setupUserScripts userContentController: WKUserContentController
    ) {
        userContentController.addUserScript(
            WKUserScript(
                source: Self.fullPageTextFlowScript,
                injectionTime: .atDocumentEnd,
                forMainFrameOnly: false
            )
        )
    }

    private func installControlToggle(on navigator: EPUBNavigatorViewController) {
        navigator.addObserver(.activate { [weak self, weak navigator] event in
            guard
                let self,
                let navigator
            else {
                return false
            }
            if let image = event.targetElement?.content as? ImageContentElement {
                self.presentImagePreview(image)
                return true
            }
            guard navigator.currentSelection == nil else { return false }
            // Use Readium's own pointer event stream instead of adding a
            // competing UIKit recognizer above WKWebView. The activation
            // observer rejects moved pointers, so swipes and selection-handle
            // drags remain exclusively owned by pagination and text selection.
            self.onToggleControls()
            return true
        }).store(in: &inputObserverTokens)
    }

    private func presentImagePreview(_ image: ImageContentElement) {
        guard presentedViewController == nil else { return }
        let preview = ClickImagePreviewViewController(
            publication: publication,
            image: image
        )
        preview.modalPresentationStyle = .fullScreen
        preview.modalTransitionStyle = .crossDissolve
        present(preview, animated: true)
    }

    func updateCallbacks(
        onLocation: @escaping (Double?, String) -> Void,
        onHighlight: @escaping (EPUBSelectionPayload) -> Void,
        onAutomaticHighlight: @escaping (
            EPUBSelectionPayload,
            UUID?
        ) -> UUID?,
        onRemoveHighlight: @escaping (EPUBSelectionPayload) -> Void,
        onNote: @escaping (EPUBSelectionPayload) -> Void,
        onPlaySentence: @escaping (EPUBSelectionPayload) -> Void,
        onError: @escaping (String) -> Void,
        onToggleControls: @escaping () -> Void
    ) {
        self.onLocation = onLocation
        self.onHighlight = onHighlight
        self.onAutomaticHighlight = onAutomaticHighlight
        self.onRemoveHighlight = onRemoveHighlight
        self.onNote = onNote
        self.onPlaySentence = onPlaySentence
        self.onError = onError
        self.onToggleControls = onToggleControls
    }

    func submit(preferences: EPUBPreferences) {
        readerBackgroundColor = preferences.backgroundColor?.uiColor ?? .systemBackground
        view.backgroundColor = readerBackgroundColor
        navigator?.submitPreferences(preferences)
    }

    func navigate(
        toTableOfContentsItem itemID: String,
        onSuccess: @escaping () -> Void
    ) {
        guard let navigator, let link = tableOfContentsLinks[itemID] else { return }
        Task {
            guard let locator = await publication.locate(link) else {
                onError("无法找到这个目录位置，请重试")
                return
            }
            let succeeded = await navigator.go(to: locator)
            guard succeeded else {
                onError("无法跳转到这个目录位置，请重试")
                return
            }
            showTableOfContentsTarget(locator)
            onSuccess()
        }
    }

    private func showTableOfContentsTarget(_ locator: Locator) {
        guard let navigator else { return }
        tableOfContentsTargetTask?.cancel()
        navigator.apply(
            decorations: [
                Decoration(
                    id: "click-toc-current-target",
                    locator: locator,
                    style: .highlight(
                        tint: .systemYellow.withAlphaComponent(0.52)
                    )
                )
            ],
            in: "click-toc-target"
        )
        tableOfContentsTargetTask = Task { [weak navigator] in
            try? await Task.sleep(for: .seconds(2.4))
            guard !Task.isCancelled else { return }
            navigator?.apply(decorations: [], in: "click-toc-target")
        }
    }

    func navigate(toLocatorJSON locatorJSON: String) {
        guard
            let navigator,
            let locator = try? Locator(jsonString: locatorJSON)
        else {
            return
        }
        Task {
            programmaticLocatorNavigationDepth += 1
            defer { programmaticLocatorNavigationDepth -= 1 }
            preferredListeningStartLocator = locator
            _ = await navigator.go(to: locator)
            try? await Task.sleep(for: .milliseconds(600))
        }
    }

    func makeListeningQueue() async -> EPUBListeningQueue? {
        guard let navigator else { return nil }
        let explicitStartLocator = preferredListeningStartLocator
        preferredListeningStartLocator = nil
        let firstVisibleElementLocator = await navigator.firstVisibleElementLocator()
        let startLocator = explicitStartLocator
            ?? firstVisibleElementLocator
            ?? navigator.currentLocation
        return await EPUBListeningQueueBuilder.make(
            publication: publication,
            startLocator: startLocator
        )
    }

    func applyAnnotations(_ annotations: [ClickAnnotation]) {
        pendingAnnotations = annotations
        guard let navigator, isViewLoaded else { return }
        let decorations = annotations.compactMap { item -> Decoration? in
            guard
                item.kind == .highlight,
                let locator = try? Locator(jsonString: item.locatorJSON)
            else {
                return nil
            }
            return Decoration(
                id: item.id.uuidString,
                locator: locator,
                style: .highlight(tint: .systemRed.withAlphaComponent(0.32))
            )
        }
        navigator.apply(decorations: decorations, in: "click-highlights")
    }

    func applyListeningIndicator(locatorJSON: String) {
        pendingListeningLocatorJSON = locatorJSON
        guard let navigator, isViewLoaded else { return }
        guard locatorJSON != appliedListeningLocatorJSON else { return }
        appliedListeningLocatorJSON = locatorJSON
        let decorations: [Decoration]
        if locatorJSON.isEmpty {
            decorations = []
        } else if let locator = try? Locator(jsonString: locatorJSON) {
            decorations = [
                Decoration(
                    id: "click-tts-current-segment",
                    locator: locator,
                    style: .highlight(
                        tint: .systemOrange.withAlphaComponent(0.38)
                    )
                )
            ]
        } else {
            decorations = []
        }
        navigator.apply(decorations: decorations, in: "click-tts-current")
    }

    @objc private func highlightSelection() {
        guard let payload = currentSelectionPayload() else { return }
        guard payload.matchingHighlightIDs.isEmpty else {
            navigator?.clearSelection()
            return
        }
        onHighlight(payload)
        navigator?.clearSelection()
    }

    @objc private func removeHighlightSelection() {
        guard
            let payload = currentSelectionPayload(),
            !payload.matchingHighlightIDs.isEmpty
        else {
            return
        }
        if let session = automaticHighlightSession,
           payload.matchingHighlightIDs.contains(session.annotationID) {
            automaticHighlightSession = nil
        }
        onRemoveHighlight(payload)
        navigator?.clearSelection()
    }

    @objc private func noteSelection() {
        guard let payload = currentSelectionPayload() else { return }
        onNote(payload)
        navigator?.clearSelection()
    }

    @objc private func playSelection() {
        guard let payload = currentSelectionPayload() else { return }
        onPlaySentence(payload)
        navigator?.clearSelection()
    }

    private func currentSelectionPayload() -> EPUBSelectionPayload? {
        guard let selection = navigator?.currentSelection else { return nil }
        return selectionPayload(for: selection)
    }

    private func selectionPayload(
        for selection: Selection
    ) -> EPUBSelectionPayload? {
        guard
            let excerpt = selection.locator.text.highlight?
                .trimmingCharacters(in: .whitespacesAndNewlines),
            !excerpt.isEmpty
        else {
            return nil
        }
        return EPUBSelectionPayload(
            excerpt: excerpt,
            locatorJSON: selection.locator.description,
            matchingHighlightIDs: matchingHighlightIDs(
                for: selection.locator
            )
        )
    }

    private func matchingHighlightIDs(for selectionLocator: Locator) -> [UUID] {
        var ids: [UUID] = pendingAnnotations.compactMap { annotation -> UUID? in
            guard
                annotation.kind == .highlight,
                annotation.serverDeleted != true,
                let locator = try? Locator(jsonString: annotation.locatorJSON),
                Self.isSameSelection(locator, selectionLocator)
            else {
                return nil
            }
            return annotation.id
        }
        if let session = automaticHighlightSession,
           Self.isSameSelection(session.locator, selectionLocator),
           !ids.contains(session.annotationID) {
            ids.append(session.annotationID)
        }
        return ids
    }

    private func scheduleAutomaticHighlight(for selection: Selection) {
        automaticHighlightSettlingTask?.cancel()
        let expectedLocator = selection.locator
        automaticHighlightSettlingTask = Task { @MainActor [weak self] in
            do {
                try await Task.sleep(for: .milliseconds(320))
            } catch {
                return
            }
            guard
                let self,
                let currentSelection = self.navigator?.currentSelection,
                Self.isSameSelection(
                    currentSelection.locator,
                    expectedLocator
                )
            else {
                return
            }
            self.commitAutomaticHighlight(for: currentSelection)
        }
        watchForSelectionEnd()
    }

    private func commitAutomaticHighlight(for selection: Selection) {
        guard let payload = selectionPayload(for: selection) else { return }
        if !payload.matchingHighlightIDs.isEmpty {
            return
        }

        let replacingID: UUID?
        if let session = automaticHighlightSession,
           Self.isSelectionAdjustment(session.locator, selection.locator) {
            replacingID = session.annotationID
        } else {
            replacingID = nil
        }
        guard let annotationID = onAutomaticHighlight(payload, replacingID) else {
            return
        }
        automaticHighlightSession = AutomaticHighlightSession(
            annotationID: annotationID,
            locator: selection.locator
        )
    }

    private func watchForSelectionEnd() {
        guard automaticHighlightSessionWatcherTask == nil else { return }
        automaticHighlightSessionWatcherTask = Task { @MainActor [weak self] in
            while !Task.isCancelled {
                do {
                    try await Task.sleep(for: .milliseconds(120))
                } catch {
                    return
                }
                guard let self else { return }
                if self.navigator?.currentSelection == nil {
                    self.automaticHighlightSettlingTask?.cancel()
                    self.automaticHighlightSettlingTask = nil
                    self.automaticHighlightSession = nil
                    self.automaticHighlightSessionWatcherTask = nil
                    return
                }
            }
        }
    }

    private static func isSameSelection(_ lhs: Locator, _ rhs: Locator) -> Bool {
        guard
            lhs.href.string == rhs.href.string,
            normalizedSelectionText(lhs.text.highlight)
                == normalizedSelectionText(rhs.text.highlight)
        else {
            return false
        }

        let lhsFragments = lhs.locations.fragments
        let rhsFragments = rhs.locations.fragments
        if !lhsFragments.isEmpty, !rhsFragments.isEmpty {
            return lhsFragments == rhsFragments
        }

        if let lhsPosition = lhs.locations.position,
           let rhsPosition = rhs.locations.position,
           lhsPosition != rhsPosition {
            return false
        }

        if let lhsProgression = lhs.locations.progression,
           let rhsProgression = rhs.locations.progression,
           abs(lhsProgression - rhsProgression) > 0.000_001 {
            return false
        }

        return normalizedSelectionContext(lhs.text.before, suffix: true)
                == normalizedSelectionContext(rhs.text.before, suffix: true)
            && normalizedSelectionContext(lhs.text.after, suffix: false)
                == normalizedSelectionContext(rhs.text.after, suffix: false)
    }

    private static func isSelectionAdjustment(
        _ previous: Locator,
        _ current: Locator
    ) -> Bool {
        guard previous.href.string == current.href.string else { return false }
        if isSameSelection(previous, current) { return true }

        let previousBefore = normalizedSelectionContext(
            previous.text.before,
            suffix: true
        )
        let currentBefore = normalizedSelectionContext(
            current.text.before,
            suffix: true
        )
        let previousAfter = normalizedSelectionContext(
            previous.text.after,
            suffix: false
        )
        let currentAfter = normalizedSelectionContext(
            current.text.after,
            suffix: false
        )
        return (!previousBefore.isEmpty && previousBefore == currentBefore)
            || (!previousAfter.isEmpty && previousAfter == currentAfter)
    }

    private static func normalizedSelectionText(_ text: String?) -> String {
        (text ?? "")
            .replacingOccurrences(
                of: #"\s+"#,
                with: " ",
                options: .regularExpression
            )
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static func normalizedSelectionContext(
        _ text: String?,
        suffix: Bool
    ) -> String {
        let normalized = normalizedSelectionText(text)
        guard normalized.count > 96 else { return normalized }
        return suffix
            ? String(normalized.suffix(96))
            : String(normalized.prefix(96))
    }

    func navigator(
        _ navigator: SelectableNavigator,
        shouldShowMenuForSelection selection: Selection
    ) -> Bool {
        scheduleAutomaticHighlight(for: selection)
        return true
    }

    func navigator(
        _ navigator: SelectableNavigator,
        canPerformAction action: EditingAction,
        for selection: Selection
    ) -> Bool {
        let isHighlighted = !matchingHighlightIDs(
            for: selection.locator
        ).isEmpty
        if action == Self.addHighlightAction {
            return !isHighlighted
        }
        if action == Self.removeHighlightAction {
            return isHighlighted
        }
        return true
    }

    func navigator(_ navigator: Navigator, locationDidChange locator: Locator) {
        if programmaticLocatorNavigationDepth == 0 {
            preferredListeningStartLocator = nil
        }
        onLocation(locator.locations.totalProgression, locator.description)
    }

    func navigator(_ navigator: Navigator, presentError error: NavigatorError) {
        switch error {
        case .copyForbidden:
            onError("这本书的版权限制不允许复制所选文字")
        }
    }
}

@MainActor
private final class ClickImagePreviewViewController: UIViewController {
    private let publication: Publication
    private let image: ImageContentElement
    private let imageView = UIImageView()
    private let activity = UIActivityIndicatorView(style: .large)
    private let statusLabel = UILabel()
    private var loadTask: Task<Void, Never>?

    init(publication: Publication, image: ImageContentElement) {
        self.publication = publication
        self.image = image
        super.init(nibName: nil, bundle: nil)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func loadView() {
        let dismissControl = UIControl()
        dismissControl.backgroundColor = .black
        dismissControl.accessibilityLabel = "全屏图片，点击返回阅读"
        dismissControl.addTarget(
            self,
            action: #selector(closePreview),
            for: .touchUpInside
        )
        view = dismissControl
    }

    override func viewDidLoad() {
        super.viewDidLoad()

        imageView.translatesAutoresizingMaskIntoConstraints = false
        imageView.contentMode = .scaleAspectFit
        imageView.isUserInteractionEnabled = false
        imageView.accessibilityLabel = image.text ?? "书内图片"
        view.addSubview(imageView)

        activity.translatesAutoresizingMaskIntoConstraints = false
        activity.color = .white
        activity.startAnimating()
        view.addSubview(activity)

        statusLabel.translatesAutoresizingMaskIntoConstraints = false
        statusLabel.text = "点击屏幕返回"
        statusLabel.textColor = UIColor.white.withAlphaComponent(0.82)
        statusLabel.font = .preferredFont(forTextStyle: .footnote)
        statusLabel.textAlignment = .center
        view.addSubview(statusLabel)

        NSLayoutConstraint.activate([
            imageView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            imageView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            imageView.topAnchor.constraint(equalTo: view.topAnchor),
            imageView.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            activity.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            activity.centerYAnchor.constraint(equalTo: view.centerYAnchor),
            statusLabel.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            statusLabel.bottomAnchor.constraint(
                equalTo: view.safeAreaLayoutGuide.bottomAnchor,
                constant: -16
            ),
        ])

        loadTask = Task { [weak self] in
            guard let self else { return }
            let loadedImage: UIImage? = if
                let resource = publication.get(image.embeddedLink),
                let data = try? await resource.read().get(),
                data.count <= 64 * 1_024 * 1_024
            {
                UIImage(data: data)
            } else {
                nil
            }
            guard !Task.isCancelled else { return }
            activity.stopAnimating()
            imageView.image = loadedImage
            if loadedImage == nil {
                statusLabel.text = "图片无法载入，点击返回"
            }
        }
    }

    override var prefersStatusBarHidden: Bool { true }
    override var prefersHomeIndicatorAutoHidden: Bool { true }

    @objc private func closePreview() {
        loadTask?.cancel()
        dismiss(animated: true)
    }

    deinit {
        loadTask?.cancel()
    }
}

private enum EPUBPublicationSearch {
    static func search(
        publication: Publication,
        query: String
    ) async -> [EPUBSearchResult] {
        guard
            let iterator = try? await publication.search(query: query).get()
        else {
            return []
        }
        var results: [EPUBSearchResult] = []
        while results.count < 100, !Task.isCancelled {
            guard let page = try? await iterator.next().get() else {
                break
            }
            for locator in page.locators {
                let text = locator.text
                let snippet = [text.before, text.highlight, text.after]
                    .compactMap { $0 }
                    .joined()
                    .replacingOccurrences(
                        of: #"\s+"#,
                        with: " ",
                        options: .regularExpression
                    )
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                results.append(
                    EPUBSearchResult(
                        id: "\(locator.href.string)#\(results.count)",
                        snippet: snippet.isEmpty ? query : snippet,
                        locatorJSON: locator.description
                    )
                )
                if results.count == 100 { break }
            }
        }
        return results
    }
}

private enum EPUBListeningQueueBuilder {
    static func make(
        publication: Publication,
        startLocator: Locator?
    ) async -> EPUBListeningQueue? {
        let content = publication.content(from: startLocator)
            ?? publication.content(from: nil)
        guard let iterator = content?.iterator() else {
            return nil
        }
        var segments: [EPUBListeningSegment] = []
        var fallbackSegments: [EPUBListeningSegment] = []
        var characterCount = 0
        var fallbackCharacterCount = 0
        let maximumCharacters = 60_000
        let startHighlight = startLocator?.text.highlight?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        var hasReachedExactStart = startHighlight?.isEmpty != false

        while characterCount < maximumCharacters,
              fallbackCharacterCount < maximumCharacters,
              !Task.isCancelled {
            guard let element = try? await iterator.next() else { break }
            if let textElement = element as? TextContentElement {
                for part in textElement.segments {
                    append(
                        text: part.text,
                        locator: part.locator,
                        publication: publication,
                        to: &segments,
                        fallback: &fallbackSegments,
                        characterCount: &characterCount,
                        fallbackCharacterCount: &fallbackCharacterCount,
                        maximumCharacters: maximumCharacters,
                        startHighlight: startHighlight,
                        hasReachedExactStart: &hasReachedExactStart
                    )
                    if characterCount >= maximumCharacters { break }
                }
            } else if let textElement = element as? TextualContentElement,
                      let text = textElement.text {
                append(
                    text: text,
                    locator: element.locator,
                    publication: publication,
                    to: &segments,
                    fallback: &fallbackSegments,
                    characterCount: &characterCount,
                    fallbackCharacterCount: &fallbackCharacterCount,
                    maximumCharacters: maximumCharacters,
                    startHighlight: startHighlight,
                    hasReachedExactStart: &hasReachedExactStart
                )
            }
        }

        let resolvedSegments = segments.isEmpty ? fallbackSegments : segments
        guard !resolvedSegments.isEmpty else { return nil }
        return EPUBListeningQueue(segments: resolvedSegments)
    }

    private static func append(
        text: String,
        locator: Locator,
        publication: Publication,
        to result: inout [EPUBListeningSegment],
        fallback: inout [EPUBListeningSegment],
        characterCount: inout Int,
        fallbackCharacterCount: inout Int,
        maximumCharacters: Int,
        startHighlight: String?,
        hasReachedExactStart: inout Bool
    ) {
        let readingOrderIndex = publication.readingOrder
            .firstIndexWithHREF(locator.href) ?? 0
        let chapterTitle = publication.readingOrder.indices.contains(readingOrderIndex)
            ? (publication.readingOrder[readingOrderIndex].title ?? "")
            : ""
        for sentence in ListeningController.sentences(in: text) {
            let sentenceLocator = locator.copy(text: { locatorText in
                guard
                    let highlight = locatorText.highlight,
                    let range = highlight.range(of: sentence)
                else {
                    locatorText.highlight = sentence
                    return
                }
                locatorText = locatorText[range]
            })
            if !hasReachedExactStart {
                if fallbackCharacterCount < maximumCharacters {
                    fallback.append(
                        EPUBListeningSegment(
                            id: "\(locator.href.string)#fallback-\(fallback.count)",
                            text: sentence,
                            chapterTitle: chapterTitle,
                            locatorJSON: sentenceLocator.description,
                            readingOrderIndex: readingOrderIndex
                        )
                    )
                    fallbackCharacterCount += sentence.count
                }
                guard let startHighlight,
                      matchesStart(sentence: sentence, highlight: startHighlight) else {
                    continue
                }
                hasReachedExactStart = true
            }
            guard characterCount < maximumCharacters else { break }
            result.append(
                EPUBListeningSegment(
                    id: "\(locator.href.string)#\(result.count)",
                    text: sentence,
                    chapterTitle: chapterTitle,
                    locatorJSON: sentenceLocator.description,
                    readingOrderIndex: readingOrderIndex
                )
            )
            characterCount += sentence.count
        }
    }

    private static func matchesStart(
        sentence: String,
        highlight: String
    ) -> Bool {
        let sentence = normalizedComparisonText(sentence)
        let highlight = normalizedComparisonText(highlight)
        guard !sentence.isEmpty, !highlight.isEmpty else { return false }
        return sentence.localizedStandardContains(highlight)
            || highlight.localizedStandardContains(sentence)
    }

    private static func normalizedComparisonText(_ text: String) -> String {
        text
            .replacingOccurrences(
                of: #"\s+"#,
                with: " ",
                options: .regularExpression
            )
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

private enum EPUBTableOfContentsMap {
    struct Entry {
        let item: EPUBTableOfContentsItem
        let link: ReadiumShared.Link
    }

    static func flatten(_ links: [ReadiumShared.Link]) -> [Entry] {
        var entries: [Entry] = []

        func append(
            _ links: [ReadiumShared.Link],
            depth: Int,
            path: String
        ) {
            for (index, link) in links.enumerated() {
                let itemPath = path.isEmpty ? "\(index)" : "\(path).\(index)"
                let title = link.title?
                    .trimmingCharacters(in: CharacterSet.whitespacesAndNewlines)
                entries.append(
                    Entry(
                        item: EPUBTableOfContentsItem(
                            id: itemPath,
                            title: title?.isEmpty == false ? title! : "未命名章节",
                            depth: depth
                        ),
                        link: link
                    )
                )
                append(link.children, depth: depth + 1, path: itemPath)
            }
        }

        append(links, depth: 0, path: "")
        return entries
    }
}

#else

struct ReadiumEPUBReaderView: View {
    let book: ClickBook
    let fileURL: URL
    let fontScale: Double
    let lineSpacing: Double
    let horizontalMargin: Double
    @ObservedObject var workspace: WorkspaceStore
    @ObservedObject var connection: ConnectionStore
    @ObservedObject var listening: ListeningController
    @ObservedObject var coordinator: EPUBReaderCoordinator

    var body: some View {
        ContentUnavailableView {
            Label("EPUB 阅读组件未载入", systemImage: "book.closed")
        } description: {
            Text("请从 Xcode 工程构建，工程会自动载入固定版本的 Readium。")
        }
    }
}

#endif
