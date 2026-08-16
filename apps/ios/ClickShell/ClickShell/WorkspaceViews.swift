import SwiftUI
import UniformTypeIdentifiers
import UIKit

struct WorkspaceView: View {
    @ObservedObject var workspace: WorkspaceStore
    @ObservedObject var connection: ConnectionStore
    @ObservedObject var listening: ListeningController
    @ObservedObject var recording: RecordingController
    @ObservedObject var sync: ClickSyncCoordinator

    @State private var destination: WorkspaceDestination? = .library
    @State private var columnVisibility: NavigationSplitViewVisibility = .automatic

    var body: some View {
        NavigationSplitView(columnVisibility: $columnVisibility) {
            List(WorkspaceDestination.allCases, selection: $destination) { item in
                Label(item.title, systemImage: item.symbol)
                    .tag(item)
            }
            .navigationTitle("Click")
            .navigationSplitViewColumnWidth(min: 220, ideal: 250, max: 300)
        } detail: {
            destinationContent
        }
        .navigationSplitViewStyle(.balanced)
        #if targetEnvironment(macCatalyst)
        .background(
            CatalystWorkspaceWindowTitleKeeper(title: "Click")
        )
        #endif
        .overlay(alignment: .bottom) {
            if listening.hasContent && listening.isPlayerVisible {
                ListeningMiniPlayer(listening: listening)
                    .padding(.horizontal, 14)
                    .padding(
                        .bottom,
                        workspace.selectedBook == nil ? 8 : 76
                    )
            }
        }
        .onChange(of: workspace.books) { _, _ in
            refreshListeningArtwork()
        }
        .onChange(of: workspace.selectedBookID) { _, _ in
            refreshListeningArtwork()
        }
    }

    private func refreshListeningArtwork() {
        guard listening.hasContent,
              let book = workspace.selectedBook else { return }
        listening.setArtwork(url: workspace.coverURL(for: book))
    }

    @ViewBuilder
    private var destinationContent: some View {
        switch destination ?? .library {
        case .library:
            LibraryWorkspaceView(
                workspace: workspace,
                connection: connection,
                listening: listening
            )
        case .myContent:
            MyContentView(workspace: workspace)
        case .recording:
            RecordingWorkspaceView(recording: recording)
        case .hermes:
            HermesWorkspaceView(
                connection: connection,
                openSettings: { destination = .settings }
            )
        case .settings:
            SettingsWorkspaceView(
                workspace: workspace,
                connection: connection,
                listening: listening,
                recording: recording,
                sync: sync
            )
        }
    }
}

private enum LibraryFilter: String, CaseIterable, Identifiable {
    case all = "全部"
    case recent = "最近"
    case favorites = "收藏"

    var id: String { rawValue }
}

struct LibraryWorkspaceView: View {
    @ObservedObject var workspace: WorkspaceStore
    @ObservedObject var connection: ConnectionStore
    @ObservedObject var listening: ListeningController

    @State private var filter: LibraryFilter = .all
    @State private var selectedFolderPath = ClickFolderPath.root
    @State private var searchText = ""
    @State private var importerPresented = false
    @State private var folderBook: ClickBook?
    @State private var isSelecting = false
    @State private var selectedBookIDs: Set<UUID> = []
    @State private var bulkOrganizerPresented = false
    @State private var folderRenameTarget: FolderRenameTarget?

    private let columns = [
        GridItem(.adaptive(minimum: 150, maximum: 210), spacing: 22, alignment: .top),
    ]

    var body: some View {
        Group {
            if let selected = workspace.selectedBook {
                NativeReaderView(
                    book: selected,
                    workspace: workspace,
                    connection: connection,
                    listening: listening
                )
            } else {
                shelf
            }
        }
        .fileImporter(
            isPresented: $importerPresented,
            allowedContentTypes: supportedTypes,
            allowsMultipleSelection: true
        ) { result in
            switch result {
            case let .success(urls):
                Task { await workspace.importDocuments(urls) }
            case let .failure(error):
                workspace.showStatus(
                    "没有导入文件：\(error.localizedDescription)"
                )
            }
        }
    }

    private var shelf: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                if !selectedFolderPath.isRoot {
                    HStack(spacing: 12) {
                        Button {
                            selectedFolderPath = selectedFolderPath.parent
                        } label: {
                            Label("返回上一级", systemImage: "chevron.left")
                        }
                        .buttonStyle(.plain)

                        Text("书架 › \(selectedFolderPath.displayValue)")
                            .font(.title2.weight(.semibold))
                            .lineLimit(1)

                        Spacer()

                        Menu {
                            Button {
                                folderRenameTarget = FolderRenameTarget(path: selectedFolderPath)
                            } label: {
                                Label("重命名", systemImage: "pencil")
                            }
                            Button {
                                let parent = selectedFolderPath.parent
                                workspace.moveFolderContentsToParent(selectedFolderPath)
                                selectedFolderPath = parent
                            } label: {
                                Label("把内容移到上一级", systemImage: "tray.and.arrow.up")
                            }
                        } label: {
                            Image(systemName: "ellipsis.circle")
                        }
                        .accessibilityLabel("文件夹菜单")
                    }
                } else {
                    Picker("书架范围", selection: $filter) {
                        ForEach(LibraryFilter.allCases) { item in
                            Text(item.rawValue).tag(item)
                        }
                    }
                    .pickerStyle(.segmented)
                    .frame(maxWidth: 380)
                }

                if workspace.libraryBooks.isEmpty {
                    ContentUnavailableView {
                        Label("书架还是空的", systemImage: "books.vertical")
                    } description: {
                        Text("导入 EPUB、PDF 或文本后，即使 Mac 不在身边也能从本机打开。")
                    } actions: {
                        Button("导入书籍") {
                            importerPresented = true
                        }
                        .buttonStyle(.borderedProminent)
                    }
                    .frame(maxWidth: .infinity, minHeight: 420)
                } else if visibleFolders.isEmpty && filteredBooks.isEmpty {
                    ContentUnavailableView(
                        emptyShelfTitle,
                        systemImage: emptyShelfSymbol,
                        description: Text(emptyShelfDescription)
                    )
                        .frame(maxWidth: .infinity, minHeight: 360)
                } else {
                    if !visibleFolders.isEmpty {
                        Text("文件夹")
                            .font(.title3.weight(.semibold))
                        LazyVGrid(columns: columns, alignment: .leading, spacing: 26) {
                            ForEach(visibleFolders) { folder in
                                FolderCard(
                                    name: folder.leafName,
                                    books: workspace.books(inFolderPath: folder),
                                    coverURL: workspace.coverURL(for:),
                                    requestCover: workspace.requestLocalCover(for:),
                                    open: { selectedFolderPath = folder },
                                    rename: {
                                        folderRenameTarget = FolderRenameTarget(path: folder)
                                    },
                                    remove: { workspace.moveFolderContentsToParent(folder) },
                                    receiveBook: { moveBook($0, into: folder) }
                                )
                            }
                        }
                    }

                    if !filteredBooks.isEmpty {
                        Text(bookSectionTitle)
                            .font(.title3.weight(.semibold))
                    }
                    LazyVGrid(columns: columns, alignment: .leading, spacing: 26) {
                        ForEach(filteredBooks) { book in
                            BookCard(
                                book: book,
                                coverURL: workspace.coverURL(for: book),
                                isDownloaded: workspace.isBookDownloaded(book),
                                isSelecting: isSelecting,
                                isSelected: selectedBookIDs.contains(book.id),
                                open: { workspace.select(book) },
                                toggleSelection: { toggleSelection(book.id) },
                                toggleFavorite: { workspace.toggleFavorite(book) },
                                editFolder: { folderBook = book },
                                receiveBook: { mergeBook($0, onto: book) }
                            )
                            .task(id: book.id) {
                                workspace.requestLocalCover(for: book.id)
                            }
                        }
                    }
                }
            }
            .padding(24)
        }
        .navigationTitle(selectedFolderPath.leafName)
        .searchable(text: $searchText, prompt: "搜索书名或作者")
        .toolbar {
            ToolbarItemGroup(placement: .primaryAction) {
                if workspace.isImporting {
                    ProgressView()
                }
                if !workspace.libraryBooks.isEmpty {
                    Button(isSelecting ? "完成" : "整理") {
                        isSelecting.toggle()
                        if !isSelecting {
                            selectedBookIDs.removeAll()
                        }
                    }
                }
                Button {
                    importerPresented = true
                } label: {
                    Label("导入书籍", systemImage: "plus")
                }
            }
        }
        .overlay(alignment: .bottom) {
            if isSelecting && !selectedBookIDs.isEmpty {
                Button {
                    bulkOrganizerPresented = true
                } label: {
                    Label(
                        "整理已选的 \(selectedBookIDs.count) 本书",
                        systemImage: "folder.badge.plus"
                    )
                    .font(.headline)
                    .padding(.horizontal, 18)
                    .padding(.vertical, 12)
                }
                .buttonStyle(.borderedProminent)
                .background(.regularMaterial, in: Capsule())
                .padding(.bottom, 14)
            } else if !workspace.statusMessage.isEmpty {
                Text(workspace.statusMessage)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 8)
                    .background(.regularMaterial, in: Capsule())
                    .padding(.bottom, 14)
            }
        }
        .animation(.easeOut(duration: 0.2), value: workspace.statusMessage)
        .sheet(item: $folderBook) { book in
            BookFolderEditor(
                book: book,
                existingFolders: workspace.folderNames
            ) { folderName in
                workspace.setFolder(folderName, for: book)
            }
            .presentationDetents([.height(330)])
            .presentationDragIndicator(.visible)
        }
        .sheet(isPresented: $bulkOrganizerPresented) {
            BulkFolderEditor(
                selectedCount: selectedBookIDs.count,
                existingFolders: workspace.folderNames,
                parentPath: selectedFolderPath
            ) { folderName in
                let selectedBooks = selectedBookIDs.compactMap(workspace.book(id:))
                workspace.setFolder(folderName, forBooks: selectedBooks)
                selectedBookIDs.removeAll()
                isSelecting = false
            }
            .presentationDetents([.height(390), .medium])
            .presentationDragIndicator(.visible)
        }
        .sheet(item: $folderRenameTarget) { target in
            FolderRenameEditor(folderName: target.path.leafName) { newName in
                workspace.renameFolder(at: target.path, to: newName)
                if selectedFolderPath == target.path,
                   let renamedPath = target.path.parent.appending(newName) {
                    selectedFolderPath = renamedPath
                }
            }
            .presentationDetents([.height(230)])
            .presentationDragIndicator(.visible)
        }
        .onChange(of: filter) {
            selectedFolderPath = .root
            selectedBookIDs.removeAll()
        }
    }

    private var visibleFolders: [ClickFolderPath] {
        guard filter == .all, normalizedQuery.isEmpty else {
            return []
        }
        return workspace.childFolderPaths(at: selectedFolderPath)
    }

    private var filteredBooks: [ClickBook] {
        let source: [ClickBook]
        switch filter {
        case .all:
            source = workspace.libraryBooks
        case .recent:
            source = Array(workspace.recentBooks.prefix(20))
        case .favorites:
            source = workspace.favoriteBooks
        }
        if !normalizedQuery.isEmpty {
            return matchingBooks(in: source)
        }
        if filter == .all {
            return source.filter { workspace.folderPath(for: $0) == selectedFolderPath }
        }
        return source
    }

    private var normalizedQuery: String {
        searchText.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func matchingBooks(in books: [ClickBook]) -> [ClickBook] {
        guard !normalizedQuery.isEmpty else { return books }
        return books.filter {
            $0.displayTitle.localizedCaseInsensitiveContains(normalizedQuery)
                || $0.displayAuthor.localizedCaseInsensitiveContains(normalizedQuery)
                || ($0.folderName?.localizedCaseInsensitiveContains(normalizedQuery) ?? false)
        }
    }

    private var bookSectionTitle: String {
        if !selectedFolderPath.isRoot { return "直属书籍" }
        if !normalizedQuery.isEmpty { return "搜索结果" }
        return filter == .all ? "未整理" : filter.rawValue
    }

    private func toggleSelection(_ bookID: UUID) {
        if selectedBookIDs.contains(bookID) {
            selectedBookIDs.remove(bookID)
        } else {
            selectedBookIDs.insert(bookID)
        }
    }

    private func moveBook(_ bookID: UUID, into folderPath: ClickFolderPath) {
        guard let book = workspace.book(id: bookID) else { return }
        workspace.setFolderPath(folderPath, for: book)
    }

    private func mergeBook(_ sourceBookID: UUID, onto targetBook: ClickBook) {
        guard sourceBookID != targetBook.id,
              let sourceBook = workspace.book(id: sourceBookID) else { return }
        let targetPath = workspace.folderPath(for: targetBook)
        if targetPath != selectedFolderPath {
            workspace.setFolderPath(targetPath, for: sourceBook)
            return
        }
        guard let folderPath = workspace.uniqueNewFolderPath(under: selectedFolderPath) else {
            workspace.showTransientStatus("最多只能建立三级文件夹")
            return
        }
        workspace.setFolderPath(folderPath, forBooks: [sourceBook, targetBook])
        selectedFolderPath = folderPath
        DispatchQueue.main.async {
            folderRenameTarget = FolderRenameTarget(path: folderPath)
        }
    }

    private var emptyShelfTitle: String {
        if !normalizedQuery.isEmpty {
            return "没有找到书籍"
        }
        if filter == .favorites {
            return "还没有收藏"
        }
        if !selectedFolderPath.isRoot {
            return "“\(selectedFolderPath.leafName)”里还没有书"
        }
        return filter == .recent ? "最近还没有打开过书" : "这里还没有书"
    }

    private var emptyShelfSymbol: String {
        if !normalizedQuery.isEmpty {
            return "magnifyingglass"
        }
        return filter == .favorites ? "star" : "books.vertical"
    }

    private var emptyShelfDescription: String {
        let query = normalizedQuery
        if !query.isEmpty {
            return "没有与“\(query)”匹配的书名或作者。"
        }
        if filter == .favorites {
            return "在书籍菜单里点收藏，之后会集中显示在这里。"
        }
        if !selectedFolderPath.isRoot {
            return "可以从书籍菜单把书整理进这个文件夹。"
        }
        return "打开一本书后，它会出现在这里。"
    }

    private var supportedTypes: [UTType] {
        [
            UTType(filenameExtension: "epub") ?? .data,
            .pdf,
            .plainText,
            .text,
        ]
    }
}

private struct FolderRenameTarget: Identifiable {
    let path: ClickFolderPath
    var id: String { path.rawValue }
}

private struct FolderCard: View {
    let name: String
    let books: [ClickBook]
    let coverURL: (ClickBook) -> URL?
    let requestCover: (UUID) -> Void
    let open: () -> Void
    let rename: () -> Void
    let remove: () -> Void
    let receiveBook: (UUID) -> Void

    @State private var isDropTarget = false

    var body: some View {
        Button(action: open) {
            VStack(alignment: .leading, spacing: 10) {
                FolderCoverGrid(
                    books: Array(books.prefix(4)),
                    coverURL: coverURL,
                    requestCover: requestCover
                )
                    .aspectRatio(0.98, contentMode: .fit)
                    .padding(12)
                    .background(
                        Color.accentColor.opacity(isDropTarget ? 0.2 : 0.09),
                        in: RoundedRectangle(cornerRadius: 18, style: .continuous)
                    )
                    .overlay {
                        RoundedRectangle(cornerRadius: 18, style: .continuous)
                            .stroke(
                                Color.accentColor.opacity(isDropTarget ? 0.8 : 0.12),
                                lineWidth: isDropTarget ? 3 : 1
                            )
                    }

                HStack(spacing: 7) {
                    Image(systemName: "folder.fill")
                        .foregroundStyle(.tint)
                    Text(name)
                        .font(.headline)
                        .lineLimit(2)
                        .multilineTextAlignment(.leading)
                }
                Text("\(books.count) 本")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .dropDestination(for: String.self) { values, _ in
            guard let rawValue = values.first,
                  let bookID = UUID(uuidString: rawValue) else { return false }
            receiveBook(bookID)
            return true
        } isTargeted: { isDropTarget = $0 }
        .contextMenu {
            Button(action: open) {
                Label("打开", systemImage: "folder")
            }
            Button(action: rename) {
                Label("重命名", systemImage: "pencil")
            }
            Button(action: remove) {
                Label("把内容移到上一级", systemImage: "tray.and.arrow.up")
            }
        }
        .accessibilityLabel("文件夹 \(name)，\(books.count) 本书")
        .accessibilityHint("打开文件夹；也可以把书拖到这里")
    }
}

private struct FolderCoverGrid: View {
    let books: [ClickBook]
    let coverURL: (ClickBook) -> URL?
    let requestCover: (UUID) -> Void
    private let columns = [GridItem(.flexible()), GridItem(.flexible())]

    var body: some View {
        LazyVGrid(columns: columns, spacing: 8) {
            ForEach(0..<4, id: \.self) { index in
                Group {
                    if index < books.count {
                        BookCoverArtwork(
                            book: books[index],
                            coverURL: coverURL(books[index]),
                            cornerRadius: 7
                        )
                        .task(id: books[index].id) {
                            requestCover(books[index].id)
                        }
                    } else {
                        RoundedRectangle(cornerRadius: 7, style: .continuous)
                            .fill(Color.secondary.opacity(0.08))
                    }
                }
                    .aspectRatio(0.78, contentMode: .fit)
                    .frame(maxWidth: .infinity)
                    .clipped()
            }
        }
        .frame(maxWidth: .infinity)
        .clipped()
    }
}

private struct BookCard: View {
    let book: ClickBook
    let coverURL: URL?
    let isDownloaded: Bool
    let isSelecting: Bool
    let isSelected: Bool
    let open: () -> Void
    let toggleSelection: () -> Void
    let toggleFavorite: () -> Void
    let editFolder: () -> Void
    let receiveBook: (UUID) -> Void

    @State private var isDropTarget = false

    var body: some View {
        Button(action: isSelecting ? toggleSelection : open) {
            VStack(alignment: .leading, spacing: 10) {
                BookCoverArtwork(
                    book: book,
                    coverURL: coverURL,
                    cornerRadius: 12
                )
                .aspectRatio(0.72, contentMode: .fit)
                .overlay(alignment: .bottom) {
                    if book.progress > 0 {
                        ProgressView(value: book.progress)
                            .tint(.white)
                            .padding(10)
                    }
                }
                .overlay(alignment: .topTrailing) {
                    if isSelecting {
                        Image(systemName: isSelected ? "checkmark.circle.fill" : "circle")
                            .font(.title2)
                            .foregroundStyle(isSelected ? Color.accentColor : .white)
                            .padding(8)
                            .background(.black.opacity(0.2), in: Circle())
                            .padding(8)
                    } else if !isDownloaded {
                        Image(systemName: "icloud.and.arrow.down")
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(.white)
                            .padding(8)
                            .background(.black.opacity(0.22), in: Circle())
                            .padding(8)
                    }
                }

                Text(book.displayTitle)
                    .font(.headline)
                    .foregroundStyle(.primary)
                    .lineLimit(2)
                    .multilineTextAlignment(.leading)
                Text(book.displayAuthor)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            .contentShape(Rectangle())
            .opacity(isSelecting && !isSelected ? 0.74 : 1)
            .overlay {
                if isDropTarget {
                    RoundedRectangle(cornerRadius: 14, style: .continuous)
                        .stroke(Color.accentColor, lineWidth: 3)
                }
            }
        }
        .buttonStyle(.plain)
        .draggable(book.id.uuidString)
        .dropDestination(for: String.self) { values, _ in
            guard let rawValue = values.first,
                  let bookID = UUID(uuidString: rawValue) else { return false }
            receiveBook(bookID)
            return true
        } isTargeted: { isDropTarget = $0 }
        .contextMenu {
            Button(action: toggleFavorite) {
                Label(
                    book.isFavorite ? "取消收藏" : "收藏",
                    systemImage: book.isFavorite ? "star.slash" : "star"
                )
            }
            Button(action: open) {
                Label("继续阅读", systemImage: "book.pages")
            }
            Button(action: editFolder) {
                Label("移动到文件夹", systemImage: "folder")
            }
        }
        .accessibilityLabel("\(book.displayTitle)，\(book.displayAuthor)")
        .accessibilityHint(isSelecting ? "选择或取消选择" : "打开并继续阅读；也可以拖到另一本书上建立文件夹")
    }
}

private struct BookFolderEditor: View {
    let book: ClickBook
    let existingFolders: [String]
    let onSave: (String?) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var folderName: String

    init(
        book: ClickBook,
        existingFolders: [String],
        onSave: @escaping (String?) -> Void
    ) {
        self.book = book
        self.existingFolders = existingFolders
        self.onSave = onSave
        _folderName = State(initialValue: book.folderName ?? "")
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("例如：中医与健康 / 经典", text: $folderName)
                        .textInputAutocapitalization(.never)
                } header: {
                    Text("分类路径（最多三级）")
                } footer: {
                    if ClickFolderPath.hasExcessDepth(folderName) {
                        Text("最多只能使用三级文件夹。")
                            .foregroundStyle(.red)
                    } else {
                        Text("用 / 分隔层级；留空表示移回根书架。")
                    }
                }
                if !existingFolders.isEmpty {
                    Section("已有分类路径") {
                        ForEach(existingFolders, id: \.self) { folder in
                            Button(ClickFolderPath(rawValue: folder).displayValue) {
                                folderName = folder
                            }
                        }
                    }
                }
            }
            .navigationTitle("整理《\(book.displayTitle)》")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存") {
                        onSave(folderName)
                        dismiss()
                    }
                    .disabled(ClickFolderPath.hasExcessDepth(folderName))
                }
            }
        }
    }
}

private struct BulkFolderEditor: View {
    let selectedCount: Int
    let existingFolders: [String]
    let parentPath: ClickFolderPath
    let onSave: (String?) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var folderName = ""

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    if parentPath.depth < ClickFolderPath.maxDepth {
                        TextField("新文件夹名称", text: $folderName)
                            .textInputAutocapitalization(.never)
                    } else {
                        Text("当前位置已经是第三级，不能再建立下一层。")
                            .foregroundStyle(.secondary)
                    }
                } header: {
                    Text("整理 \(selectedCount) 本书")
                } footer: {
                    Text(
                        parentPath.depth >= ClickFolderPath.maxDepth
                            ? "这里只能选择已有分类路径，或把书移回根书架。"
                            : parentPath.isRoot
                                ? "新文件夹会建立在根书架；也可以直接选择已有路径。"
                                : "新文件夹会建立在 \(parentPath.displayValue) 下面。"
                    )
                }

                if !existingFolders.isEmpty {
                    Section("已有分类路径") {
                        ForEach(existingFolders, id: \.self) { folder in
                            Button {
                                onSave(folder)
                                dismiss()
                            } label: {
                                Label(
                                    ClickFolderPath(rawValue: folder).displayValue,
                                    systemImage: "folder"
                                )
                            }
                        }
                    }
                }

                Section {
                    Button {
                        onSave(nil)
                        dismiss()
                    } label: {
                        Label("移回书架", systemImage: "tray.and.arrow.up")
                    }
                }
            }
            .navigationTitle("移动到文件夹")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("新建并移动") {
                        let target = parentPath.appending(folderName)
                        onSave(target?.rawValue)
                        dismiss()
                    }
                    .disabled(
                        parentPath.depth >= ClickFolderPath.maxDepth
                            || folderName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                    )
                }
            }
        }
    }
}

private struct FolderRenameEditor: View {
    let folderName: String
    let onSave: (String) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var newName: String

    init(folderName: String, onSave: @escaping (String) -> Void) {
        self.folderName = folderName
        self.onSave = onSave
        _newName = State(initialValue: folderName)
    }

    var body: some View {
        NavigationStack {
            Form {
                TextField("文件夹名称", text: $newName)
                    .textInputAutocapitalization(.never)
            }
            .navigationTitle("重命名文件夹")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存") {
                        onSave(newName)
                        dismiss()
                    }
                    .disabled(newName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
        }
    }
}

struct MyContentView: View {
    @ObservedObject var workspace: WorkspaceStore
    @StateObject private var playback = VoiceNotePlaybackController()
    @State private var searchText = ""

    var body: some View {
        Group {
            if !hasReadingContent && workspace.syncConflicts.isEmpty {
                ContentUnavailableView {
                    Label("还没有阅读沉淀", systemImage: "text.book.closed")
                } description: {
                    Text("标红和备注会按书汇总在这里。")
                }
            } else if groupedBookIDs.isEmpty && !searchText.isEmpty {
                ContentUnavailableView(
                    "没有找到内容",
                    systemImage: "magnifyingglass",
                    description: Text(
                        "没有与“\(searchText.trimmingCharacters(in: .whitespacesAndNewlines))”匹配的标红或备注。"
                    )
                )
            } else {
                List {
                    ForEach(groupedBookIDs, id: \.self) { bookID in
                        Section(bookTitle(for: bookID)) {
                            ForEach(filteredAnnotations(for: bookID)) { annotation in
                                VStack(alignment: .leading, spacing: 5) {
                                    HStack {
                                        Text(
                                            annotation.excerpt.isEmpty
                                                ? annotation.note
                                                : annotation.excerpt
                                        )
                                        .lineLimit(3)
                                        Spacer()
                                        if let audioURL = workspace.audioURL(for: annotation) {
                                            Button {
                                                playback.toggle(url: audioURL)
                                            } label: {
                                                Image(
                                                    systemName: playback.playingURL == audioURL
                                                        ? "stop.circle.fill"
                                                        : "waveform.circle"
                                                )
                                            }
                                            .buttonStyle(.plain)
                                            .accessibilityLabel("播放语音原音")
                                        }
                                    }
                                    if !annotation.excerpt.isEmpty,
                                       !annotation.note.isEmpty,
                                       annotation.note != annotation.excerpt {
                                        Text(annotation.note)
                                            .font(.subheadline)
                                            .foregroundStyle(.secondary)
                                            .lineLimit(2)
                                    }
                                }
                                .padding(.vertical, 4)
                            }
                        }
                    }
                    if !workspace.syncConflicts.isEmpty {
                        Section("同步时保留的内容") {
                            ForEach(workspace.syncConflicts) { conflict in
                                VStack(alignment: .leading, spacing: 5) {
                                    Text(conflict.message)
                                    Text(conflict.createdAt, style: .relative)
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                                .padding(.vertical, 4)
                            }
                        }
                    }
                }
            }
        }
        .navigationTitle("我的内容")
        .searchable(text: $searchText, prompt: "搜索书名、标红或备注")
    }

    private var groupedBookIDs: [UUID] {
        Array(
            Set(
                workspace.annotations
                    .filter { $0.kind != .bookmark && $0.serverDeleted != true }
                    .filter { annotation in
                        let query = searchText.trimmingCharacters(
                            in: .whitespacesAndNewlines
                        )
                        guard !query.isEmpty else { return true }
                        return annotation.excerpt.localizedCaseInsensitiveContains(query)
                            || annotation.note.localizedCaseInsensitiveContains(query)
                            || bookTitle(for: annotation.bookID)
                                .localizedCaseInsensitiveContains(query)
                    }
                    .map(\.bookID)
            )
        ).sorted {
            bookTitle(for: $0) < bookTitle(for: $1)
        }
    }

    private var hasReadingContent: Bool {
        workspace.annotations.contains {
            $0.kind != .bookmark && $0.serverDeleted != true
        }
    }

    private func bookTitle(for id: UUID) -> String {
        workspace.books.first(where: { $0.id == id })?.displayTitle ?? "未知书籍"
    }

    private func filteredAnnotations(for bookID: UUID) -> [ClickAnnotation] {
        let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else {
            return workspace.readingAnnotations(for: bookID)
        }
        return workspace.readingAnnotations(for: bookID).filter {
            $0.excerpt.localizedCaseInsensitiveContains(query)
                || $0.note.localizedCaseInsensitiveContains(query)
                || bookTitle(for: bookID).localizedCaseInsensitiveContains(query)
        }
    }
}

#if targetEnvironment(macCatalyst)
private struct CatalystWorkspaceWindowTitleKeeper: UIViewControllerRepresentable {
    let title: String

    func makeUIViewController(context: Context) -> Controller {
        Controller(title: title)
    }

    func updateUIViewController(_ controller: Controller, context: Context) {
        controller.workspaceTitle = title
        controller.applyTitle()
    }

    final class Controller: UIViewController {
        var workspaceTitle: String

        init(title: String) {
            workspaceTitle = title
            super.init(nibName: nil, bundle: nil)
            view.isHidden = true
            view.isUserInteractionEnabled = false
        }

        @available(*, unavailable)
        required init?(coder: NSCoder) {
            fatalError("init(coder:) has not been implemented")
        }

        override func viewDidAppear(_ animated: Bool) {
            super.viewDidAppear(animated)
            applyTitle()
        }

        override func viewDidLayoutSubviews() {
            super.viewDidLayoutSubviews()
            applyTitle()
        }

        func applyTitle() {
            view.window?.windowScene?.title = workspaceTitle
        }
    }
}
#endif

struct RecordingWorkspaceView: View {
    @ObservedObject var recording: RecordingController

    var body: some View {
        VStack(spacing: 26) {
                Spacer()

                Button {
                    recording.toggleRecording()
                } label: {
                    ZStack {
                        Circle()
                            .fill(recording.isRecording ? Color.red : Color.accentColor)
                            .frame(width: 118, height: 118)
                        Image(systemName: recording.isRecording ? "stop.fill" : "mic.fill")
                            .font(.system(size: 40, weight: .semibold))
                            .foregroundStyle(.white)
                    }
                }
                .buttonStyle(.plain)
                .accessibilityLabel(recording.isRecording ? "停止录音" : "开始录音")

                Text(recording.isRecording ? durationText(recording.duration) : "点击开始录音")
                    .font(.title2.monospacedDigit())

                Text(recording.statusMessage)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)

                Spacer()

                if !recording.captures.isEmpty {
                    List(recording.captures.prefix(6)) { capture in
                        HStack {
                            Image(systemName: captureStatusSymbol(capture.syncState))
                                .foregroundStyle(
                                    capture.syncState == "permanent_failed"
                                        ? Color.orange
                                        : Color.accentColor
                                )
                            VStack(alignment: .leading) {
                                Text(capture.createdAt, style: .date)
                                Text(durationText(capture.duration))
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            Spacer()
                            Text(captureStatusText(capture.syncState))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                    .frame(maxHeight: 280)
                }
        }
        .padding(24)
        .navigationTitle("录音")
    }

    private func durationText(_ value: TimeInterval) -> String {
        let total = max(0, Int(value.rounded(.down)))
        return String(format: "%02d:%02d", total / 60, total % 60)
    }

    private func captureStatusSymbol(_ state: String) -> String {
        switch state {
        case "synced":
            return "checkmark.circle"
        case "permanent_failed":
            return "exclamationmark.circle"
        case "uploading":
            return "arrow.up.circle"
        default:
            return "waveform"
        }
    }

    private func captureStatusText(_ state: String) -> String {
        switch state {
        case "synced":
            return "已同步"
        case "permanent_failed":
            return "保留在本机"
        case "uploading":
            return "同步中"
        default:
            return "待同步"
        }
    }
}

struct HermesWorkspaceView: View {
    @ObservedObject var connection: ConnectionStore
    let openSettings: () -> Void

    @State private var currentURL: URL?
    @State private var reloadToken = UUID()
    @State private var loadError: String?

    var body: some View {
        Group {
            if let baseURL = connection.connectedBaseURL {
                ReaderWebView(
                    url: Binding(
                        get: { currentURL ?? connection.hermesURL(for: baseURL) },
                        set: { currentURL = $0 }
                    ),
                    reloadToken: $reloadToken,
                    loadError: $loadError
                )
                .overlay {
                    if let loadError {
                        ContentUnavailableView(
                            "Hermes 暂时不可用",
                            systemImage: "wifi.slash",
                            description: Text(loadError)
                        )
                        .background(.regularMaterial)
                    }
                }
            } else {
                ContentUnavailableView {
                    Label("Hermes 当前离线", systemImage: "message")
                } description: {
                    Text("阅读、朗读和录音仍可使用。连接已配对的 Mac 后再进入在线 Hermes。")
                } actions: {
                    Button("打开连接设置", action: openSettings)
                        .buttonStyle(.borderedProminent)
                }
            }
        }
        .navigationTitle("Hermes")
        .toolbar {
            if connection.connectedBaseURL != nil {
                Button {
                    reloadToken = UUID()
                } label: {
                    Label("刷新", systemImage: "arrow.clockwise")
                }
            }
        }
    }
}

struct SettingsWorkspaceView: View {
    @ObservedObject var workspace: WorkspaceStore
    @ObservedObject var connection: ConnectionStore
    @ObservedObject var listening: ListeningController
    @ObservedObject var recording: RecordingController
    @ObservedObject var sync: ClickSyncCoordinator
    @StateObject private var updateController = AppUpdateController()
    @Environment(\.openURL) private var openURL

    var body: some View {
        Form {
                Section("阅读") {
                    LabeledContent("本地书籍", value: "\(workspace.books.count) 本")
                    LabeledContent("待同步操作", value: "\(workspace.pendingOperations.count) 项")
                    LabeledContent("待同步录音", value: "\(recording.pendingCount) 条")
                    if !workspace.syncConflicts.isEmpty {
                        LabeledContent(
                            "已保留的冲突",
                            value: "\(workspace.syncConflicts.count) 项"
                        )
                    }
                }

                Section("朗读") {
                    LabeledContent(
                        "默认声音",
                        value: "Click 微软 · 云健"
                    )
                    HStack {
                        Text("语速")
                        Slider(
                            value: Binding(
                                get: { Double(listening.speechRate) },
                                set: { listening.speechRate = Float($0) }
                            ),
                            in: 0.35...0.62
                        )
                    }
                    Text("只使用 Click 微软语音。断网时只播放已缓存的微软音频；未缓存时停止并提示，不会切换苹果系统声音。")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }

                ConnectionView(store: connection)

                Section {
                    HStack {
                        Label(
                            sync.statusMessage.isEmpty
                                ? "进入前台时自动同步"
                                : sync.statusMessage,
                            systemImage: sync.isRunning
                                ? "arrow.triangle.2.circlepath"
                                : "checkmark.circle"
                        )
                        Spacer()
                        if sync.isRunning {
                            ProgressView()
                        }
                    }
                    Text("只在 App 前台运行一次有界同步；不轮询，也不要求 Mac 或 iPad 常驻后台。")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                } header: {
                    Text("自动同步")
                }

                Section("关于") {
                    LabeledContent("应用", value: "Click")
                    LabeledContent("平台", value: "iPad 原生版")
                    LabeledContent(
                        "版本",
                        value: "\(updateController.currentVersion)（\(updateController.currentBuild)）"
                    )
                    Button {
                        Task { await updateController.checkForUpdates() }
                    } label: {
                        HStack {
                            Label("检查更新", systemImage: "arrow.triangle.2.circlepath")
                            Spacer()
                            if updateController.isChecking {
                                ProgressView()
                            }
                        }
                    }
                    .disabled(updateController.isChecking)
                    if !updateController.statusMessage.isEmpty {
                        Text(updateController.statusMessage)
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                    Text("阅读和录音离线可用；Hermes 只连接在线服务，不启动本地模型。")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
        }
        .navigationTitle("设置")
        .sheet(item: $updateController.availableUpdate) { update in
            NavigationStack {
                VStack(alignment: .leading, spacing: 18) {
                    Label("发现新版本", systemImage: "sparkles")
                        .font(.title2.weight(.semibold))
                    Text("版本 \(update.version)（\(update.build)）")
                        .font(.headline)
                    if !update.releaseNotes.isEmpty {
                        ScrollView {
                            Text(update.releaseNotes)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                    Spacer()
                    Button {
                        openURL(update.installURL)
                    } label: {
                        Label("去更新", systemImage: "arrow.up.circle.fill")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
                }
                .padding(24)
                .navigationTitle("应用更新")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("稍后") {
                            updateController.availableUpdate = nil
                        }
                    }
                }
            }
            .presentationDetents([.medium, .large])
            .presentationDragIndicator(.visible)
        }
    }
}

private struct ClickUpdateManifest: Decodable, Identifiable {
    let version: String
    let build: Int
    let minimumOS: String?
    let releaseNotes: String
    let installURL: URL

    var id: String { "\(version)-\(build)" }

    private enum CodingKeys: String, CodingKey {
        case version
        case build
        case minimumOS = "minimum_os"
        case releaseNotes = "release_notes"
        case installURL = "install_url"
    }
}

@MainActor
private final class AppUpdateController: ObservableObject {
    @Published var isChecking = false
    @Published var statusMessage = ""
    @Published var availableUpdate: ClickUpdateManifest?

    let currentVersion: String
    let currentBuild: Int

    init(bundle: Bundle = .main) {
        currentVersion = bundle.object(
            forInfoDictionaryKey: "CFBundleShortVersionString"
        ) as? String ?? "未知"
        currentBuild = Int(
            bundle.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "0"
        ) ?? 0
    }

    func checkForUpdates() async {
        guard !isChecking else { return }
        availableUpdate = nil
        guard let manifestURL = configuredManifestURL() else {
            statusMessage = "尚未配置可信的 HTTPS 更新来源"
            return
        }

        isChecking = true
        statusMessage = "正在检查…"
        defer { isChecking = false }

        do {
            var request = URLRequest(url: manifestURL)
            request.cachePolicy = .reloadIgnoringLocalCacheData
            request.timeoutInterval = 12
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let httpResponse = response as? HTTPURLResponse,
                  (200..<300).contains(httpResponse.statusCode) else {
                throw UpdateCheckError.invalidResponse
            }
            let manifest = try JSONDecoder().decode(ClickUpdateManifest.self, from: data)
            guard isTrustedInstallURL(manifest.installURL) else {
                throw UpdateCheckError.invalidInstallURL
            }
            if manifest.build > currentBuild {
                availableUpdate = manifest
                statusMessage = "发现版本 \(manifest.version)"
            } else {
                statusMessage = "当前已经是最新版"
            }
        } catch {
            statusMessage = "检查失败，请稍后重试"
        }
    }

    private func configuredManifestURL() -> URL? {
        guard let value = Bundle.main.object(
            forInfoDictionaryKey: "ClickUpdateManifestURL"
        ) as? String else { return nil }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: trimmed),
              url.scheme?.lowercased() == "https",
              url.host != nil else { return nil }
        return url
    }

    private func isTrustedInstallURL(_ url: URL) -> Bool {
        let scheme = url.scheme?.lowercased()
        if scheme == "https" {
            return url.host != nil
        }
        if scheme == "itms-services" {
            guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
                  let manifestValue = components.queryItems?.first(
                    where: { $0.name == "url" }
                  )?.value,
                  let manifestURL = URL(string: manifestValue) else { return false }
            return manifestURL.scheme?.lowercased() == "https" && manifestURL.host != nil
        }
        return false
    }
}

private enum UpdateCheckError: Error {
    case invalidResponse
    case invalidInstallURL
}

struct ListeningMiniPlayer: View {
    @ObservedObject var listening: ListeningController
    @State private var expanded = false

    var body: some View {
        HStack(spacing: 12) {
            Button {
                expanded = true
            } label: {
                HStack(spacing: 12) {
                    ListeningArtwork(
                        image: listening.artworkImage,
                        title: listening.bookTitle
                    )
                    .frame(width: 38, height: 50)

                    VStack(alignment: .leading, spacing: 2) {
                        Text(listening.currentSentence)
                            .font(.subheadline)
                            .lineLimit(1)
                        Text(miniPlayerDetail)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            Button(action: listening.previous) {
                Image(systemName: "backward.end.fill")
            }
            .accessibilityLabel("上一句")
            Button(action: listening.togglePlayback) {
                Group {
                    if listening.isPreparing {
                        ProgressView()
                    } else {
                        Image(systemName: listening.isPlaying ? "pause.fill" : "play.fill")
                    }
                }
                .frame(width: 30, height: 30)
            }
            .disabled(listening.isPreparing)
            .accessibilityLabel(
                listening.isPreparing
                    ? "正在准备微软语音"
                    : (listening.isPlaying ? "暂停" : "播放")
            )
            Button(action: listening.next) {
                Image(systemName: "forward.end.fill")
            }
            .accessibilityLabel("下一句")
            Button(role: .destructive) {
                listening.stop()
            } label: {
                Image(systemName: "xmark")
            }
            .foregroundStyle(.secondary)
            .accessibilityLabel("退出朗读")
        }
        .font(.title3)
        .padding(.horizontal, 16)
        .padding(.vertical, 11)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
        .shadow(color: .black.opacity(0.12), radius: 14, y: 5)
        .sheet(isPresented: $expanded) {
            ListeningExpandedView(listening: listening)
                .presentationDetents([.large])
                .presentationDragIndicator(.hidden)
        }
    }

    private var miniPlayerDetail: String {
        if !listening.isPlaying,
           !listening.isPreparing,
           !listening.statusMessage.isEmpty,
           listening.statusMessage != "已暂停" {
            return listening.statusMessage
        }
        return "\(listening.bookTitle) · \(listening.engineLabel)"
    }
}

private struct ListeningExpandedView: View {
    @ObservedObject var listening: ListeningController
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(spacing: 22) {
            HStack(alignment: .top, spacing: 16) {
                ListeningArtwork(
                    image: listening.artworkImage,
                    title: listening.bookTitle
                )
                .frame(width: 96, height: 128)

                VStack(alignment: .leading, spacing: 3) {
                    Text(listening.bookTitle)
                        .font(.headline)
                    if !listening.chapterTitle.isEmpty {
                        Text(listening.chapterTitle)
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                    }
                    Text(listening.engineLabel)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    if !listening.statusMessage.isEmpty {
                        Text(listening.statusMessage)
                            .font(.caption)
                            .foregroundStyle(
                                listening.isPlaying ? Color.secondary : Color.orange
                            )
                            .lineLimit(2)
                    }
                }
                Spacer()
                Button("完成") { dismiss() }
            }

            ScrollView {
                Text(listening.currentSentence)
                    .font(.title3)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(
                maxWidth: .infinity,
                minHeight: 120,
                maxHeight: 260,
                alignment: .topLeading
            )
            .padding(16)
            .background(
                Color.secondary.opacity(0.08),
                in: RoundedRectangle(cornerRadius: 14, style: .continuous)
            )

            HStack(spacing: 36) {
                Button(action: listening.previous) {
                    Image(systemName: "backward.end.fill")
                }
                .accessibilityLabel("上一句")
                Button(action: listening.togglePlayback) {
                    Group {
                        if listening.isPreparing {
                            ProgressView()
                                .controlSize(.large)
                        } else {
                            Image(
                                systemName: listening.isPlaying
                                    ? "pause.circle.fill"
                                    : "play.circle.fill"
                            )
                            .font(.system(size: 56))
                        }
                    }
                }
                .disabled(listening.isPreparing)
                .accessibilityLabel(
                    listening.isPreparing
                        ? "正在准备微软语音"
                        : (listening.isPlaying ? "暂停" : "播放")
                )
                Button(action: listening.next) {
                    Image(systemName: "forward.end.fill")
                }
                .accessibilityLabel("下一句")
            }
            .font(.title2)

            Spacer(minLength: 0)

            HStack {
                Menu {
                    ForEach([15, 30, 60], id: \.self) { minutes in
                        Button("\(minutes) 分钟") {
                            listening.startCountdown(minutes: minutes)
                        }
                    }
                    if listening.countdownRemaining != nil {
                        Button("取消倒计时", role: .destructive) {
                            listening.cancelCountdown()
                        }
                    }
                } label: {
                    Label(countdownLabel, systemImage: "timer")
                }

                Spacer()

                Button(role: .destructive) {
                    listening.stop()
                    dismiss()
                } label: {
                    Label("退出朗读", systemImage: "xmark")
                }
            }
            .buttonStyle(.bordered)
        }
        .padding(24)
    }

    private var countdownLabel: String {
        guard let remaining = listening.countdownRemaining else { return "倒计时" }
        let total = max(0, Int(remaining))
        return String(format: "%d:%02d", total / 60, total % 60)
    }
}

private struct BookCoverArtwork: View {
    let book: ClickBook
    let coverURL: URL?
    let cornerRadius: CGFloat

    var body: some View {
        Group {
            if let coverURL,
               let image = UIImage(contentsOfFile: coverURL.path) {
                Image(uiImage: image)
                    .resizable()
                    .scaledToFill()
            } else {
                ZStack {
                    LinearGradient(
                        colors: [
                            Color.accentColor.opacity(0.88),
                            Color.accentColor.opacity(0.46),
                        ],
                        startPoint: .topLeading,
                        endPoint: .bottomTrailing
                    )
                    VStack(spacing: 8) {
                        Image(systemName: book.format.symbol)
                            .font(.title2.weight(.medium))
                        Text(book.displayTitle)
                            .font(.caption.weight(.semibold))
                            .lineLimit(3)
                            .multilineTextAlignment(.center)
                    }
                    .foregroundStyle(.white.opacity(0.96))
                    .padding(8)
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .clipped()
        .clipShape(RoundedRectangle(cornerRadius: cornerRadius, style: .continuous))
    }
}

private struct ListeningArtwork: View {
    let image: UIImage?
    let title: String

    var body: some View {
        Group {
            if let image {
                Image(uiImage: image)
                    .resizable()
                    .scaledToFit()
            } else {
                ZStack {
                    LinearGradient(
                        colors: [.indigo.opacity(0.9), .blue.opacity(0.58)],
                        startPoint: .topLeading,
                        endPoint: .bottomTrailing
                    )
                    Text(String(title.prefix(1)))
                        .font(.headline.weight(.bold))
                        .foregroundStyle(.white)
                }
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .stroke(.white.opacity(0.14), lineWidth: 1)
        }
    }
}
