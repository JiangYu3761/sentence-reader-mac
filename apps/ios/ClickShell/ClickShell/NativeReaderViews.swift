import AVFoundation
import PDFKit
import SwiftUI

private struct ReaderSearchSection: Identifiable, Sendable, Equatable {
    let id: Int
    let label: String
    let text: String
}

private struct ReaderLoadedContent: Sendable {
    let text: String
    let sections: [ReaderSearchSection]
}

enum ReaderThemeChoice: String, CaseIterable, Identifiable {
    case black
    case warm
    case white

    var id: String { rawValue }

    var title: String {
        switch self {
        case .black: return "深黑"
        case .warm: return "暖色"
        case .white: return "白色"
        }
    }

    var background: Color {
        switch self {
        case .black: return Color(red: 0.015, green: 0.015, blue: 0.018)
        case .warm: return Color(red: 0.96, green: 0.92, blue: 0.82)
        case .white: return .white
        }
    }

    var foreground: Color {
        switch self {
        case .black: return Color(white: 0.88)
        case .warm: return Color(red: 0.16, green: 0.13, blue: 0.09)
        case .white: return Color(white: 0.08)
        }
    }

    var colorScheme: ColorScheme {
        self == .black ? .dark : .light
    }

    var readiumBackgroundHex: String {
        switch self {
        case .black: return "#040405"
        case .warm: return "#F5EBD1"
        case .white: return "#FFFFFF"
        }
    }

    var readiumTextHex: String {
        switch self {
        case .black: return "#E0E0E0"
        case .warm: return "#292117"
        case .white: return "#141414"
        }
    }
}

enum ReaderTypefaceChoice: String, CaseIterable, Identifiable {
    case yahei
    case system

    var id: String { rawValue }

    var title: String {
        switch self {
        case .yahei: return "舒适黑体"
        case .system: return "系统默认"
        }
    }

    func font(size: CGFloat) -> Font {
        switch self {
        case .yahei:
            return .system(size: size, weight: .regular, design: .default)
        case .system:
            return .system(size: size)
        }
    }
}

struct NativeReaderView: View {
    let book: ClickBook
    @ObservedObject var workspace: WorkspaceStore
    @ObservedObject var connection: ConnectionStore
    @ObservedObject var listening: ListeningController

    @AppStorage("ClickReader.fontScale") private var fontScale = 1.0
    @AppStorage("ClickReader.lineSpacing") private var lineSpacing = 7.0
    @AppStorage("ClickReader.horizontalMargin") private var horizontalMargin = 0.0
    @AppStorage("ClickReader.topMargin") private var topMargin = 0.0
    @AppStorage("ClickReader.bottomMargin") private var bottomMargin = 0.0
    @AppStorage("ClickReader.theme") private var themeRawValue =
        ReaderThemeChoice.black.rawValue
    @AppStorage("ClickReader.typeface") private var typefaceRawValue =
        ReaderTypefaceChoice.yahei.rawValue
    @AppStorage("ClickReader.typefaceDefaults.v2")
    private var didApplyYaHeiDefault = false
    @AppStorage("ClickReader.typefaceAllowedChoices.v3")
    private var didRestrictTypefaceChoices = false
    @AppStorage("ClickReader.immersiveDefaults.v1")
    private var didApplyImmersiveDefaults = false
    @AppStorage("ClickReader.microsoftTTSDisclosureAccepted.v1")
    private var microsoftTTSDisclosureAccepted = false
    @State private var readerText = ""
    @State private var showDisplaySettings = false
    @State private var showContents = false
    @State private var showSearch = false
    @State private var showNoteComposer = false
    @State private var isPreparingSpeech = false
    @State private var listeningError: String?
    @State private var showMicrosoftTTSDisclosure = false
    @State private var listeningPreparationTask: Task<Void, Never>?
    @State private var isPreparingSearch = false
    @State private var currentPDFPage = 0
    @State private var requestedPDFPage: Int?
    @State private var readerSearchSections: [ReaderSearchSection] = []
    @State private var currentTextSectionID: Int?
    @State private var controlsVisible = false
    @StateObject private var epubCoordinator = EPUBReaderCoordinator()

    var body: some View {
        VStack(spacing: 0) {
            readerContent
        }
        .background(readerTheme.background.ignoresSafeArea())
        .foregroundStyle(readerTheme.foreground)
        .preferredColorScheme(readerTheme.colorScheme)
        .navigationBarBackButtonHidden()
        .toolbar(.hidden, for: .navigationBar)
        .overlay(alignment: .top) {
            if controlsVisible {
                ReaderTopBar(
                    title: book.displayTitle,
                    isFavorite: book.isFavorite,
                    close: { workspace.closeReader() },
                    toggleFavorite: { workspace.toggleFavorite(book) },
                    addNote: { showNoteComposer = true },
                    addBookmark: addBookmark
                )
                .transition(.opacity)
            }
        }
        .overlay(alignment: .bottom) {
            if controlsVisible {
                ReaderBottomBar(
                    canReadAloud: book.format == .epub || !readerText.isEmpty,
                    isPreparingSpeech: isPreparingSpeech,
                    showContents: { showContents = true },
                    showSearch: prepareSearch,
                    startListening: startListening,
                    showDisplay: { showDisplaySettings = true },
                    hideControls: hideReaderControls
                )
                .transition(.opacity)
            }
        }
        .overlay(alignment: .top) {
            if let confirmation = epubCoordinator.navigationConfirmation {
                Label(
                    "已跳转到：\(confirmation.title)",
                    systemImage: "checkmark.circle.fill"
                )
                .font(.callout.weight(.semibold))
                .foregroundStyle(.primary)
                .padding(.horizontal, 16)
                .padding(.vertical, 10)
                .background(.ultraThinMaterial, in: Capsule())
                .padding(.top, 16)
                .transition(.move(edge: .top).combined(with: .opacity))
                .allowsHitTesting(false)
            }
        }
        .animation(.easeInOut(duration: 0.18), value: epubCoordinator.navigationConfirmation)
        .sheet(isPresented: $showDisplaySettings) {
            ReaderDisplaySettingsView(
                fontScale: $fontScale,
                lineSpacing: $lineSpacing,
                horizontalMargin: $horizontalMargin,
                topMargin: $topMargin,
                bottomMargin: $bottomMargin,
                themeRawValue: $themeRawValue,
                typefaceRawValue: $typefaceRawValue
            )
            .presentationDetents([.large])
            .presentationDragIndicator(.visible)
        }
        .sheet(isPresented: $showContents) {
            ReaderContentsView(
                book: book,
                workspace: workspace,
                epubCoordinator: epubCoordinator,
                navigateToAnnotation: navigateToAnnotation,
                navigateToEPUBContents: { item in
                    hideReaderControls()
                    epubCoordinator.navigate(to: item)
                }
            )
                .presentationDetents([.large])
                .presentationDragIndicator(.visible)
        }
        .sheet(isPresented: $showSearch) {
            Group {
                if book.format == .epub {
                    EPUBReaderSearchView(
                        bookTitle: book.displayTitle,
                        coordinator: epubCoordinator
                    )
                } else {
                    ReaderSearchView(
                        bookTitle: book.displayTitle,
                        sections: readerSearchSections,
                        isPreparing: isPreparingSearch,
                        navigate: navigateToSearchSection
                    )
                }
            }
            .presentationDetents([.medium, .large])
            .presentationDragIndicator(.visible)
        }
        .sheet(isPresented: $showNoteComposer) {
            ReaderNoteComposer(title: "添加阅读备注", excerpt: "") { note, audioURL in
                workspace.addNote(
                    bookID: book.id,
                    excerpt: "",
                    note: note,
                    audioDraftURL: audioURL
                )
            }
            .presentationDetents([.height(330)])
            .presentationDragIndicator(.visible)
        }
        .alert(
            "无法开始朗读",
            isPresented: Binding(
                get: { listeningError != nil },
                set: { if !$0 { listeningError = nil } }
            )
        ) {
            Button("知道了", role: .cancel) {}
        } message: {
            Text(listeningError ?? "")
        }
        .alert(
            "使用 Click 微软语音",
            isPresented: $showMicrosoftTTSDisclosure
        ) {
            Button("继续朗读") {
                microsoftTTSDisclosureAccepted = true
                beginListening()
            }
            Button("取消", role: .cancel) {}
        } message: {
            Text("朗读时只发送当前句和顺序缓存所需的后续句段给 Microsoft；不会发送整本书、备注或标红。")
        }
        .task(id: book.id) {
            isPreparingSearch = true
            readerText = ""
            readerSearchSections = []
            currentTextSectionID = nil
            let content = await loadReaderContent()
            guard !Task.isCancelled else { return }
            readerText = content.text
            readerSearchSections = content.sections
            if book.format == .text, currentTextSectionID == nil {
                currentTextSectionID = content.sections.first?.id
            }
            isPreparingSearch = false
        }
        .onAppear {
            if !didApplyYaHeiDefault {
                if typefaceRawValue == ReaderTypefaceChoice.system.rawValue
                    || ReaderTypefaceChoice(rawValue: typefaceRawValue) == nil {
                    typefaceRawValue = ReaderTypefaceChoice.yahei.rawValue
                }
                didApplyYaHeiDefault = true
            }
            if !didRestrictTypefaceChoices {
                if ReaderTypefaceChoice(rawValue: typefaceRawValue) == nil {
                    typefaceRawValue = ReaderTypefaceChoice.yahei.rawValue
                }
                didRestrictTypefaceChoices = true
            }
            if !didApplyImmersiveDefaults {
                themeRawValue = ReaderThemeChoice.black.rawValue
                horizontalMargin = 0
                topMargin = 0
                bottomMargin = 0
                didApplyImmersiveDefaults = true
            }
            if !controlsVisible
                || listening.currentBookID != book.id.uuidString {
                listening.hidePlayer()
            }
        }
        .onDisappear {
            listeningPreparationTask?.cancel()
            listeningPreparationTask = nil
            isPreparingSpeech = false
            if listening.hasContent {
                listening.showPlayer()
            }
        }
    }

    @ViewBuilder
    private var readerContent: some View {
        switch book.format {
        case .pdf:
            PDFDocumentView(
                url: workspace.fileURL(for: book),
                currentPage: $currentPDFPage,
                requestedPage: $requestedPDFPage
            )
            .padding(.top, topMargin)
            .padding(.bottom, bottomMargin)
            .background(readerTheme.background)
            .simultaneousGesture(TapGesture().onEnded { toggleReaderControls() })
        case .text:
            ScrollView {
                if readerSearchSections.isEmpty {
                    Text(readerText.isEmpty ? "正在打开…" : readerText)
                        .font(readerTypeface.font(size: 18 * fontScale))
                        .foregroundStyle(readerTheme.foreground)
                        .lineSpacing(lineSpacing)
                        .textSelection(.enabled)
                        .frame(maxWidth: 760, alignment: .leading)
                        .padding(.horizontal, horizontalMargin)
                        .padding(.top, topMargin)
                        .padding(.bottom, bottomMargin)
                        .frame(maxWidth: .infinity)
                } else {
                    LazyVStack(alignment: .leading, spacing: max(12, lineSpacing)) {
                        ForEach(readerSearchSections) { section in
                            Text(section.text)
                                .font(readerTypeface.font(size: 18 * fontScale))
                                .foregroundStyle(readerTheme.foreground)
                                .lineSpacing(lineSpacing)
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .id(section.id)
                        }
                    }
                    .scrollTargetLayout()
                    .frame(maxWidth: 760, alignment: .leading)
                    .padding(.horizontal, horizontalMargin)
                    .padding(.top, topMargin)
                    .padding(.bottom, bottomMargin)
                    .frame(maxWidth: .infinity)
                }
            }
            .scrollPosition(id: $currentTextSectionID, anchor: .top)
            .background(readerTheme.background)
            .simultaneousGesture(TapGesture().onEnded { toggleReaderControls() })
            .onChange(of: currentTextSectionID) { _, sectionID in
                guard
                    let sectionID,
                    let sectionIndex = readerSearchSections.firstIndex(where: {
                        $0.id == sectionID
                    })
                else {
                    return
                }
                workspace.updateProgress(
                    bookID: book.id,
                    progress: textProgress(at: sectionIndex)
                )
            }
        case .epub:
            ReadiumEPUBReaderView(
                book: book,
                fileURL: workspace.fileURL(for: book),
                fontScale: fontScale,
                lineSpacing: lineSpacing,
                horizontalMargin: horizontalMargin,
                readerTheme: readerTheme,
                readerTypeface: readerTypeface,
                workspace: workspace,
                connection: connection,
                listening: listening,
                coordinator: epubCoordinator,
                onToggleControls: toggleReaderControls
            )
            .padding(.top, topMargin)
            .padding(.bottom, bottomMargin)
        }
    }

    private func startListening() {
        let bookID = book.id.uuidString
        if listening.hasContent,
           listening.currentBookID == bookID {
            listening.showPlayer()
            return
        }
        if listening.hasContent || listening.isPreparing {
            listening.stop()
        }
        if clickTTSConfiguration != nil,
           !microsoftTTSDisclosureAccepted {
            showMicrosoftTTSDisclosure = true
            return
        }
        beginListening()
    }

    private func beginListening() {
        if book.format == .epub {
            guard !isPreparingSpeech else { return }
            listeningPreparationTask?.cancel()
            isPreparingSpeech = true
            let expectedBookID = book.id.uuidString
            listeningPreparationTask = Task {
                let queue = await epubCoordinator.listeningQueue()
                guard !Task.isCancelled else { return }
                isPreparingSpeech = false
                guard let queue, let first = queue.segments.first else {
                    listeningError = "暂时无法从当前阅读位置取得可朗读文字"
                    listeningPreparationTask = nil
                    return
                }
                let configuration = connection.clickTTSConfiguration(
                    bookID: expectedBookID,
                    locatorJSON: first.locatorJSON
                )
                listening.start(
                    queue: queue,
                    title: book.displayTitle,
                    bookID: expectedBookID,
                    configuration: configuration,
                    artworkURL: workspace.coverURL(for: book),
                    onSegmentChange: { locatorJSON in
                        epubCoordinator.navigate(toLocatorJSON: locatorJSON)
                    }
                )
                listeningPreparationTask = nil
            }
            return
        }
        if !readerText.isEmpty {
            listening.start(
                text: readerText,
                title: book.displayTitle,
                bookID: book.id.uuidString,
                configuration: clickTTSConfiguration,
                artworkURL: workspace.coverURL(for: book)
            )
            return
        }
        listeningError = "这份文档没有可朗读文字"
    }

    private var readerTheme: ReaderThemeChoice {
        ReaderThemeChoice(rawValue: themeRawValue) ?? .black
    }

    private var readerTypeface: ReaderTypefaceChoice {
        ReaderTypefaceChoice(rawValue: typefaceRawValue) ?? .yahei
    }

    private func toggleReaderControls() {
        controlsVisible.toggle()
        if controlsVisible,
           listening.hasContent,
           listening.currentBookID == book.id.uuidString {
            listening.showPlayer()
        } else {
            listening.hidePlayer()
        }
    }

    private func hideReaderControls() {
        controlsVisible = false
        listening.hidePlayer()
    }

    private var clickTTSConfiguration: ClickTTSConfiguration? {
        let locatorJSON: String
        switch book.format {
        case .epub:
            locatorJSON = epubCoordinator.currentLocatorJSON
        case .pdf:
            locatorJSON = Self.jsonString([
                "type": "pdf",
                "page": currentPDFPage,
            ])
        case .text:
            locatorJSON = Self.jsonString([
                "type": "text",
                "section": currentTextSectionID ?? readerSearchSections.first?.id ?? 0,
                "progress": currentTextProgress,
            ])
        }
        return connection.clickTTSConfiguration(
            bookID: book.id.uuidString,
            locatorJSON: locatorJSON
        )
    }

    private func prepareSearch() {
        showSearch = true
    }

    private func addBookmark() {
        let locatorJSON: String
        let excerpt: String
        switch book.format {
        case .epub:
            locatorJSON = epubCoordinator.currentLocatorJSON.isEmpty
                ? (book.lastLocatorJSON ?? "")
                : epubCoordinator.currentLocatorJSON
            let percent = Int(((epubCoordinator.currentProgress ?? book.progress) * 100).rounded())
            excerpt = percent > 0 ? "全书 \(percent)%" : "当前阅读位置"
        case .pdf:
            locatorJSON = Self.jsonString([
                "type": "pdf",
                "page": currentPDFPage,
            ])
            excerpt = "第 \(currentPDFPage + 1) 页"
        case .text:
            let sectionID = currentTextSectionID ?? readerSearchSections.first?.id ?? 0
            locatorJSON = Self.jsonString([
                "type": "text",
                "section": sectionID,
                "progress": currentTextProgress,
            ])
            excerpt = readerSearchSections
                .first(where: { $0.id == sectionID })
                .map { Self.compactSnippet($0.text, limit: 52) }
                ?? "当前阅读位置"
        }
        workspace.addBookmark(
            bookID: book.id,
            locatorJSON: locatorJSON,
            excerpt: excerpt
        )
    }

    private func navigateToAnnotation(_ annotation: ClickAnnotation) {
        switch book.format {
        case .epub:
            epubCoordinator.navigate(toLocatorJSON: annotation.locatorJSON)
        case .pdf:
            guard
                let data = annotation.locatorJSON.data(using: .utf8),
                let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                let page = object["page"] as? Int
            else {
                return
            }
            requestedPDFPage = max(0, page)
        case .text:
            guard
                let data = annotation.locatorJSON.data(using: .utf8),
                let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            else {
                return
            }
            if let sectionID = object["section"] as? Int,
               readerSearchSections.contains(where: { $0.id == sectionID }) {
                currentTextSectionID = sectionID
                return
            }
            if let progress = object["progress"] as? Double,
               !readerSearchSections.isEmpty {
                let bounded = min(max(progress, 0), 1)
                let index = min(
                    readerSearchSections.count - 1,
                    Int((bounded * Double(readerSearchSections.count - 1)).rounded())
                )
                currentTextSectionID = readerSearchSections[index].id
            }
        }
    }

    private func navigateToSearchSection(_ sectionID: Int) {
        switch book.format {
        case .pdf:
            requestedPDFPage = max(0, sectionID)
        case .text:
            guard readerSearchSections.contains(where: { $0.id == sectionID }) else {
                return
            }
            currentTextSectionID = sectionID
        case .epub:
            break
        }
    }

    private var currentTextProgress: Double {
        guard
            let currentTextSectionID,
            let index = readerSearchSections.firstIndex(where: {
                $0.id == currentTextSectionID
            })
        else {
            return book.progress
        }
        return textProgress(at: index)
    }

    private func textProgress(at index: Int) -> Double {
        guard readerSearchSections.count > 1 else { return 0 }
        return Double(index) / Double(readerSearchSections.count - 1)
    }

    private static func jsonString(_ object: [String: Any]) -> String {
        guard
            JSONSerialization.isValidJSONObject(object),
            let data = try? JSONSerialization.data(withJSONObject: object),
            let value = String(data: data, encoding: .utf8)
        else {
            return ""
        }
        return value
    }

    private func loadReaderContent() async -> ReaderLoadedContent {
        switch book.format {
        case .text:
            let text = await workspace.textContent(for: book)
            let sections = await Task.detached(priority: .userInitiated) {
                Self.makeTextSections(from: text)
            }.value
            return ReaderLoadedContent(text: text, sections: sections)
        case .pdf:
            let url = workspace.fileURL(for: book)
            return await Task.detached(priority: .userInitiated) {
                guard let document = PDFDocument(url: url) else {
                    return ReaderLoadedContent(text: "", sections: [])
                }
                var sections: [ReaderSearchSection] = []
                var pageTexts: [String] = []
                for pageIndex in 0..<document.pageCount {
                    let text = document.page(at: pageIndex)?.string?
                        .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
                    guard !text.isEmpty else { continue }
                    sections.append(
                        ReaderSearchSection(
                            id: pageIndex,
                            label: "第 \(pageIndex + 1) 页",
                            text: text
                        )
                    )
                    pageTexts.append(text)
                }
                return ReaderLoadedContent(
                    text: pageTexts.joined(separator: "\n\n"),
                    sections: sections
                )
            }.value
        case .epub:
            return ReaderLoadedContent(text: "", sections: [])
        }
    }

    nonisolated private static func makeTextSections(
        from text: String
    ) -> [ReaderSearchSection] {
        let paragraphs = text
            .split(whereSeparator: \.isNewline)
            .map {
                String($0).trimmingCharacters(in: .whitespacesAndNewlines)
            }
            .filter { !$0.isEmpty }

        var sections: [ReaderSearchSection] = []
        for paragraph in paragraphs {
            var start = paragraph.startIndex
            while start < paragraph.endIndex {
                let end = paragraph.index(
                    start,
                    offsetBy: 1_200,
                    limitedBy: paragraph.endIndex
                ) ?? paragraph.endIndex
                let chunk = String(paragraph[start..<end])
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                if !chunk.isEmpty {
                    let id = sections.count
                    sections.append(
                        ReaderSearchSection(
                            id: id,
                            label: "第 \(id + 1) 段",
                            text: chunk
                        )
                    )
                }
                start = end
            }
        }
        return sections
    }

    nonisolated private static func compactSnippet(
        _ text: String,
        limit: Int
    ) -> String {
        let compact = text
            .replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard compact.count > limit else { return compact }
        return String(compact.prefix(limit)) + "…"
    }
}

private struct ReaderTopBar: View {
    let title: String
    let isFavorite: Bool
    let close: () -> Void
    let toggleFavorite: () -> Void
    let addNote: () -> Void
    let addBookmark: () -> Void

    var body: some View {
        HStack(spacing: 16) {
            Button(action: close) {
                Image(systemName: "chevron.backward")
                    .frame(width: 34, height: 34)
            }
            .accessibilityLabel("返回书架")

            Spacer(minLength: 8)

            Text(title)
                .font(.headline)
                .lineLimit(1)

            Spacer(minLength: 8)

            Menu {
                Button(action: toggleFavorite) {
                    Label(
                        isFavorite ? "取消收藏" : "收藏",
                        systemImage: isFavorite ? "star.slash" : "star"
                    )
                }
                Button(action: addNote) {
                    Label("添加备注", systemImage: "square.and.pencil")
                }
                Button(action: addBookmark) {
                    Label("添加书签", systemImage: "bookmark")
                }
            } label: {
                Image(systemName: "ellipsis.circle")
                    .frame(width: 34, height: 34)
            }
            .accessibilityLabel("更多")
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
        .background(.regularMaterial)
    }
}

private struct ReaderBottomBar: View {
    let canReadAloud: Bool
    let isPreparingSpeech: Bool
    let showContents: () -> Void
    let showSearch: () -> Void
    let startListening: () -> Void
    let showDisplay: () -> Void
    let hideControls: () -> Void

    var body: some View {
        HStack {
            Button(action: showContents) {
                Image(systemName: "list.bullet")
            }
            .accessibilityLabel("目录、书签和批注")
            Spacer()
            Button(action: showSearch) {
                Image(systemName: "magnifyingglass")
            }
            .accessibilityLabel("书内搜索")
            Spacer()
            Button(action: startListening) {
                if isPreparingSpeech {
                    ProgressView()
                        .accessibilityLabel("正在准备朗读")
                } else {
                    Image(systemName: "speaker.wave.2")
                }
            }
            .accessibilityLabel("朗读")
            .disabled(!canReadAloud || isPreparingSpeech)
            Spacer()
            Button(action: showDisplay) {
                Image(systemName: "textformat.size")
            }
            .accessibilityLabel("显示设置")
            Spacer()
            Button(action: hideControls) {
                Image(systemName: "arrow.down.right.and.arrow.up.left")
            }
            .accessibilityLabel("进入沉浸阅读")
        }
        .font(.title3.weight(.medium))
        .padding(.horizontal, 34)
        .padding(.vertical, 14)
        .frame(maxWidth: .infinity)
        .background(.regularMaterial)
    }
}

private struct ReaderDisplaySettingsView: View {
    @Binding var fontScale: Double
    @Binding var lineSpacing: Double
    @Binding var horizontalMargin: Double
    @Binding var topMargin: Double
    @Binding var bottomMargin: Double
    @Binding var themeRawValue: String
    @Binding var typefaceRawValue: String
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Form {
                Section("阅读背景") {
                    Picker("背景", selection: $themeRawValue) {
                        ForEach(ReaderThemeChoice.allCases) { theme in
                            Text(theme.title).tag(theme.rawValue)
                        }
                    }
                    .pickerStyle(.segmented)
                }
                Section("字体") {
                    Picker("字体", selection: $typefaceRawValue) {
                        ForEach(ReaderTypefaceChoice.allCases) { typeface in
                            Text(typeface.title).tag(typeface.rawValue)
                        }
                    }
                    .pickerStyle(.segmented)
                }
                Section("字号与行距") {
                LabeledContent("字号") {
                    Slider(value: $fontScale, in: 0.8...1.8)
                        .frame(width: 240)
                }
                LabeledContent("行距") {
                    Slider(value: $lineSpacing, in: 0...18)
                        .frame(width: 240)
                }
                }
                Section("页面空间") {
                LabeledContent("左右边距") {
                    Slider(value: $horizontalMargin, in: 0...100)
                        .frame(width: 240)
                }
                LabeledContent("上边距") {
                    Slider(value: $topMargin, in: 0...120)
                        .frame(width: 240)
                }
                LabeledContent("下边距") {
                    Slider(value: $bottomMargin, in: 0...120)
                        .frame(width: 240)
                }
                }
            }
            .navigationTitle("显示")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                Button("完成") { dismiss() }
            }
        }
    }
}

private struct ReaderContentsView: View {
    let book: ClickBook
    @ObservedObject var workspace: WorkspaceStore
    @ObservedObject var epubCoordinator: EPUBReaderCoordinator
    let navigateToAnnotation: (ClickAnnotation) -> Void
    let navigateToEPUBContents: (EPUBTableOfContentsItem) -> Void
    @State private var selection = 0
    @StateObject private var playback = VoiceNotePlaybackController()
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                Picker("阅读定位", selection: $selection) {
                    Text("目录").tag(0)
                    Text("书签").tag(1)
                    Text("批注").tag(2)
                }
                .pickerStyle(.segmented)
                .padding()

                if selection == 0, book.format == .epub {
                    if epubCoordinator.items.isEmpty {
                        ContentUnavailableView("这本书没有目录", systemImage: "list.bullet")
                    } else {
                        List(epubCoordinator.items) { item in
                            Button {
                                navigateToEPUBContents(item)
                                dismiss()
                            } label: {
                                Text(item.title)
                                    .multilineTextAlignment(.leading)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .padding(.leading, CGFloat(item.depth) * 18)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                } else if selection == 1 {
                    let items = workspace.bookmarks(for: book.id)
                    if items.isEmpty {
                        ContentUnavailableView("暂无书签", systemImage: "bookmark")
                    } else {
                        List {
                            ForEach(items) { item in
                                Button {
                                    navigateToAnnotation(item)
                                    dismiss()
                                } label: {
                                    VStack(alignment: .leading, spacing: 4) {
                                        Text(item.excerpt)
                                            .multilineTextAlignment(.leading)
                                        Text(item.createdAt, style: .date)
                                            .font(.caption)
                                            .foregroundStyle(.secondary)
                                    }
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                }
                                .buttonStyle(.plain)
                                .swipeActions {
                                    Button("删除", role: .destructive) {
                                        workspace.deleteAnnotation(item)
                                    }
                                }
                            }
                        }
                    }
                } else if selection == 2 {
                    let items = workspace.readingAnnotations(for: book.id)
                    if items.isEmpty {
                        ContentUnavailableView("暂无批注", systemImage: "highlighter")
                    } else {
                        List(items) { item in
                            HStack(spacing: 12) {
                                Button {
                                    navigateToAnnotation(item)
                                    dismiss()
                                } label: {
                                    Text(item.excerpt.isEmpty ? item.note : item.excerpt)
                                        .lineLimit(3)
                                        .multilineTextAlignment(.leading)
                                        .frame(maxWidth: .infinity, alignment: .leading)
                                }
                                .buttonStyle(.plain)
                                if let audioURL = workspace.audioURL(for: item) {
                                    Button {
                                        playback.toggle(url: audioURL)
                                    } label: {
                                        Image(
                                            systemName: playback.playingURL == audioURL
                                                ? "stop.circle.fill"
                                                : "waveform.circle"
                                        )
                                    }
                                    .accessibilityLabel("播放语音原音")
                                }
                            }
                        }
                    }
                } else {
                    ContentUnavailableView(
                        "这份文档没有可用目录",
                        systemImage: "list.bullet"
                    )
                }
            }
            .navigationTitle("目录")
            .navigationBarTitleDisplayMode(.inline)
        }
    }
}

@MainActor
final class VoiceNotePlaybackController: NSObject, ObservableObject, AVAudioPlayerDelegate {
    @Published private(set) var playingURL: URL?
    private var player: AVAudioPlayer?

    func toggle(url: URL) {
        if playingURL == url {
            stop()
            return
        }
        do {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.playback, mode: .spokenAudio)
            try session.setActive(true)
            let player = try AVAudioPlayer(contentsOf: url)
            player.delegate = self
            player.prepareToPlay()
            guard player.play() else { return }
            self.player = player
            playingURL = url
        } catch {
            stop()
        }
    }

    func stop() {
        player?.stop()
        player = nil
        playingURL = nil
        try? AVAudioSession.sharedInstance().setActive(
            false,
            options: [.notifyOthersOnDeactivation]
        )
    }

    nonisolated func audioPlayerDidFinishPlaying(
        _ player: AVAudioPlayer,
        successfully flag: Bool
    ) {
        Task { @MainActor in stop() }
    }
}

private struct PDFDocumentView: UIViewRepresentable {
    let url: URL
    @Binding var currentPage: Int
    @Binding var requestedPage: Int?

    func makeCoordinator() -> Coordinator {
        Coordinator(parent: self)
    }

    func makeUIView(context: Context) -> PDFView {
        let view = PDFView()
        view.autoScales = true
        view.displayMode = .singlePageContinuous
        view.displayDirection = .vertical
        view.pageShadowsEnabled = false
        view.backgroundColor = .systemBackground
        view.document = PDFDocument(url: url)
        context.coordinator.observe(view)
        return view
    }

    func updateUIView(_ view: PDFView, context: Context) {
        context.coordinator.parent = self
        if view.document?.documentURL != url {
            view.document = PDFDocument(url: url)
        }
        if let requestedPage {
            if let page = view.document?.page(at: requestedPage),
               view.currentPage != page {
                view.go(to: page)
            }
            let requestedPageBinding = _requestedPage
            DispatchQueue.main.async {
                if requestedPageBinding.wrappedValue == requestedPage {
                    requestedPageBinding.wrappedValue = nil
                }
            }
        }
    }

    final class Coordinator {
        var parent: PDFDocumentView
        private var pageObserver: NSObjectProtocol?

        init(parent: PDFDocumentView) {
            self.parent = parent
        }

        func observe(_ view: PDFView) {
            pageObserver = NotificationCenter.default.addObserver(
                forName: .PDFViewPageChanged,
                object: view,
                queue: .main
            ) { [weak self, weak view] _ in
                guard
                    let self,
                    let view,
                    let page = view.currentPage,
                    let pageIndex = view.document?.index(for: page)
                else {
                    return
                }
                self.parent.currentPage = pageIndex
            }
        }

        deinit {
            if let pageObserver {
                NotificationCenter.default.removeObserver(pageObserver)
            }
        }
    }
}

private struct ReaderSearchResult: Identifiable, Sendable {
    let id: String
    let sectionID: Int
    let locationLabel: String
    let snippet: String
}

private struct EPUBReaderSearchView: View {
    let bookTitle: String
    @ObservedObject var coordinator: EPUBReaderCoordinator

    @State private var query = ""
    @State private var results: [EPUBSearchResult] = []
    @State private var isSearching = false
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Group {
                if isSearching {
                    ProgressView("正在搜索本地 EPUB…")
                } else if query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    ContentUnavailableView(
                        "输入关键词",
                        systemImage: "magnifyingglass",
                        description: Text("搜索只在 iPad 本机进行。")
                    )
                } else if results.isEmpty {
                    ContentUnavailableView(
                        "没有找到结果",
                        systemImage: "magnifyingglass",
                        description: Text("没有与“\(query)”匹配的书内文字。")
                    )
                } else {
                    List(results) { result in
                        Button {
                            coordinator.navigate(toLocatorJSON: result.locatorJSON)
                            dismiss()
                        } label: {
                            Text(result.snippet)
                                .multilineTextAlignment(.leading)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
            .navigationTitle("搜索《\(bookTitle)》")
            .navigationBarTitleDisplayMode(.inline)
            .searchable(text: $query, prompt: "搜索书内文字")
            .task(id: query) {
                do {
                    try await Task.sleep(for: .milliseconds(220))
                } catch {
                    return
                }
                let cleanQuery = query.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !cleanQuery.isEmpty else {
                    results = []
                    isSearching = false
                    return
                }
                isSearching = true
                let matches = await coordinator.search(cleanQuery)
                guard !Task.isCancelled else { return }
                results = matches
                isSearching = false
            }
        }
    }
}

private struct ReaderSearchView: View {
    let bookTitle: String
    let sections: [ReaderSearchSection]
    let isPreparing: Bool
    let navigate: (Int) -> Void

    @State private var query = ""
    @State private var results: [ReaderSearchResult] = []
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Group {
                if isPreparing {
                    ProgressView("正在准备本地全文…")
                } else if sections.isEmpty {
                    ContentUnavailableView(
                        "这本书没有可搜索文字",
                        systemImage: "text.magnifyingglass"
                    )
                } else if query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    ContentUnavailableView(
                        "输入关键词",
                        systemImage: "magnifyingglass",
                        description: Text("搜索只在 iPad 本机进行。")
                    )
                } else if results.isEmpty {
                    ContentUnavailableView(
                        "没有找到结果",
                        systemImage: "magnifyingglass",
                        description: Text("没有与“\(query)”匹配的书内文字。")
                    )
                } else {
                    List(results) { result in
                        Button {
                            navigate(result.sectionID)
                            dismiss()
                        } label: {
                            VStack(alignment: .leading, spacing: 5) {
                                Text(result.locationLabel)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                Text(result.snippet)
                                    .multilineTextAlignment(.leading)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
            .navigationTitle("搜索《\(bookTitle)》")
            .navigationBarTitleDisplayMode(.inline)
            .searchable(text: $query, prompt: "搜索书内文字")
            .task(id: query) {
                do {
                    try await Task.sleep(for: .milliseconds(220))
                } catch {
                    return
                }
                let cleanQuery = query.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !cleanQuery.isEmpty else {
                    results = []
                    return
                }
                results = await Task.detached(priority: .userInitiated) {
                    Self.search(sections: sections, query: cleanQuery)
                }.value
            }
        }
    }

    nonisolated private static func search(
        sections: [ReaderSearchSection],
        query: String
    ) -> [ReaderSearchResult] {
        var matches: [ReaderSearchResult] = []
        for section in sections {
            let source = section.text as NSString
            var cursor = 0
            while cursor < source.length, matches.count < 100 {
                let searchRange = NSRange(
                    location: cursor,
                    length: source.length - cursor
                )
                let found = source.range(
                    of: query,
                    options: [.caseInsensitive, .diacriticInsensitive],
                    range: searchRange
                )
                guard found.location != NSNotFound else { break }
                let start = max(0, found.location - 54)
                let end = min(source.length, NSMaxRange(found) + 86)
                let raw = source.substring(
                    with: NSRange(location: start, length: end - start)
                )
                let snippet = raw
                    .replacingOccurrences(
                        of: #"\s+"#,
                        with: " ",
                        options: .regularExpression
                    )
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                matches.append(
                    ReaderSearchResult(
                        id: "\(section.id):\(found.location)",
                        sectionID: section.id,
                        locationLabel: section.label,
                        snippet: "\(start > 0 ? "…" : "")\(snippet)\(end < source.length ? "…" : "")"
                    )
                )
                cursor = max(NSMaxRange(found), found.location + 1)
            }
            if matches.count >= 100 {
                break
            }
        }
        return matches
    }
}
