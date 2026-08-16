import CryptoKit
import Foundation
import ImageIO

private enum WorkspaceStoreError: LocalizedError {
    case invalidCoverImage

    var errorDescription: String? {
        "封面不是 iPad 可解码的位图"
    }
}

@MainActor
final class WorkspaceStore: ObservableObject {
    @Published private(set) var books: [ClickBook] = []
    @Published private(set) var annotations: [ClickAnnotation] = []
    @Published private(set) var pendingOperations: [PendingMobileOperation] = []
    @Published private(set) var syncConflicts: [ClickSyncConflict] = []
    @Published var selectedBookID: UUID?
    @Published private(set) var requestedDownloadBookID: UUID?
    @Published private(set) var statusMessage = ""
    @Published var isImporting = false

    private(set) var syncSequence: Int64 = 0
    private(set) var hasFullBaseline = false
    private let fileManager: FileManager
    private let rootURL: URL
    private let booksURL: URL
    private let voiceNotesURL: URL
    private let manifestURL: URL
    private var statusDismissTask: Task<Void, Never>?
    private var localCoverWorkerTask: Task<Void, Never>?
    private var pendingLocalCoverBookIDs: [UUID] = []
    private var queuedLocalCoverBookIDs = Set<UUID>()
    private var localCoverAttemptedBookIDs = Set<UUID>()
    private var validCoverPaths = Set<String>()
    private var invalidCoverPaths = Set<String>()

    init(fileManager: FileManager = .default) {
        self.fileManager = fileManager
        let support = fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? fileManager.temporaryDirectory
        rootURL = support.appendingPathComponent("ClickWorkspace", isDirectory: true)
        booksURL = rootURL.appendingPathComponent("Books", isDirectory: true)
        voiceNotesURL = rootURL.appendingPathComponent("VoiceNotes", isDirectory: true)
        manifestURL = rootURL.appendingPathComponent("library-v1.json")
        prepareStorage()
        load()
    }

    func showStatus(_ message: String) {
        statusDismissTask?.cancel()
        statusDismissTask = nil
        statusMessage = message
    }

    func showTransientStatus(
        _ message: String,
        dismissAfterNanoseconds: UInt64 = 3_000_000_000
    ) {
        showStatus(message)
        let expectedMessage = message
        statusDismissTask = Task { [weak self] in
            do {
                try await Task.sleep(nanoseconds: dismissAfterNanoseconds)
            } catch {
                return
            }
            guard
                !Task.isCancelled,
                self?.statusMessage == expectedMessage
            else {
                return
            }
            self?.statusMessage = ""
            self?.statusDismissTask = nil
        }
    }

    func clearStatus() {
        showStatus("")
    }

    var selectedBook: ClickBook? {
        guard let selectedBookID else { return nil }
        return books.first(where: { $0.id == selectedBookID })
    }

    var libraryBooks: [ClickBook] {
        books.filter {
            !$0.serverRemoved.orFalse || isBookDownloaded($0)
        }
    }

    var favoriteBooks: [ClickBook] {
        libraryBooks.filter(\.isFavorite)
    }

    var recentBooks: [ClickBook] {
        libraryBooks.sorted {
            ($0.lastOpenedAt ?? $0.importedAt) > ($1.lastOpenedAt ?? $1.importedAt)
        }
    }

    var folderPaths: [ClickFolderPath] {
        Array(
            Set(
                libraryBooks.compactMap { book in
                    let path = folderPath(for: book)
                    return path.isRoot ? nil : path
                }
            )
        )
        .sorted { $0.rawValue.localizedStandardCompare($1.rawValue) == .orderedAscending }
    }

    var folderNames: [String] {
        folderPaths.map(\.rawValue)
    }

    func book(id: UUID) -> ClickBook? {
        libraryBooks.first(where: { $0.id == id })
    }

    func folderPath(for book: ClickBook) -> ClickFolderPath {
        ClickFolderPath(rawValue: book.folderName)
    }

    func childFolderPaths(at parent: ClickFolderPath) -> [ClickFolderPath] {
        guard parent.depth < ClickFolderPath.maxDepth else { return [] }
        return Array(
            Set(
                libraryBooks.compactMap { book in
                    let path = folderPath(for: book)
                    guard parent.contains(path), path.depth > parent.depth else { return nil }
                    return path.prefix(parent.depth + 1)
                }
            )
        )
        .sorted { $0.leafName.localizedStandardCompare($1.leafName) == .orderedAscending }
    }

    func books(directlyIn path: ClickFolderPath) -> [ClickBook] {
        libraryBooks.filter { folderPath(for: $0) == path }
    }

    func books(inFolderPath path: ClickFolderPath) -> [ClickBook] {
        guard !path.isRoot else { return libraryBooks }
        return libraryBooks.filter { path.contains(folderPath(for: $0)) }
    }

    func books(inFolder folderName: String) -> [ClickBook] {
        books(inFolderPath: ClickFolderPath(rawValue: folderName))
    }

    func uniqueNewFolderPath(under parent: ClickFolderPath) -> ClickFolderPath? {
        guard parent.depth < ClickFolderPath.maxDepth else { return nil }
        let base = "新文件夹"
        let existingNames = Set(childFolderPaths(at: parent).map(\.leafName))
        guard !existingNames.contains(base) else {
            var suffix = 2
            while existingNames.contains("\(base) \(suffix)") {
                suffix += 1
            }
            return parent.appending("\(base) \(suffix)")
        }
        return parent.appending(base)
    }

    func uniqueNewFolderName() -> String {
        uniqueNewFolderPath(under: .root)?.rawValue ?? "新文件夹"
    }

    func fileURL(for book: ClickBook) -> URL {
        rootURL.appendingPathComponent(book.localRelativePath)
    }

    func coverURL(for book: ClickBook) -> URL? {
        guard let relativePath = book.localCoverRelativePath,
              !relativePath.isEmpty else { return nil }
        let candidate = rootURL
            .appendingPathComponent(relativePath)
            .standardizedFileURL
        guard candidate.path.hasPrefix(rootURL.standardizedFileURL.path + "/") else {
            return nil
        }
        guard fileManager.fileExists(atPath: candidate.path) else {
            validCoverPaths.remove(candidate.path)
            invalidCoverPaths.remove(candidate.path)
            return nil
        }
        if validCoverPaths.contains(candidate.path) { return candidate }
        if invalidCoverPaths.contains(candidate.path) { return nil }
        guard Self.isDecodableCover(at: candidate) else {
            invalidCoverPaths.insert(candidate.path)
            return nil
        }
        validCoverPaths.insert(candidate.path)
        return candidate
    }

    static func isDecodableCoverData(_ data: Data) -> Bool {
        guard let source = CGImageSourceCreateWithData(
            data as CFData,
            [kCGImageSourceShouldCache: false] as CFDictionary
        ) else {
            return false
        }
        return isDecodableCoverSource(source)
    }

    private static func isDecodableCover(at url: URL) -> Bool {
        guard let source = CGImageSourceCreateWithURL(
            url as CFURL,
            [kCGImageSourceShouldCache: false] as CFDictionary
        ) else {
            return false
        }
        return isDecodableCoverSource(source)
    }

    private static func isDecodableCoverSource(_ source: CGImageSource) -> Bool {
        guard CGImageSourceGetCount(source) > 0 else { return false }
        return CGImageSourceCreateThumbnailAtIndex(
            source,
            0,
            [
                kCGImageSourceCreateThumbnailFromImageAlways: true,
                kCGImageSourceThumbnailMaxPixelSize: 8,
                kCGImageSourceShouldCacheImmediately: true,
            ] as CFDictionary
        ) != nil
    }

    func remoteCoverPath(for book: ClickBook) -> String? {
        if let path = book.remoteCoverPath?.trimmingCharacters(
            in: .whitespacesAndNewlines
        ), !path.isEmpty {
            return path
        }
        guard let serverID = book.serverID?.trimmingCharacters(
            in: .whitespacesAndNewlines
        ), !serverID.isEmpty else {
            return nil
        }
        return "/v1/android/books/\(serverID)/cover"
    }

    func audioURL(for annotation: ClickAnnotation) -> URL? {
        guard
            let relativePath = annotation.audioRelativePath,
            !relativePath.isEmpty
        else {
            return nil
        }
        let candidate = rootURL
            .appendingPathComponent(relativePath)
            .standardizedFileURL
        guard candidate.path.hasPrefix(rootURL.standardizedFileURL.path + "/"),
              fileManager.fileExists(atPath: candidate.path) else {
            return nil
        }
        return candidate
    }

    func isBookDownloaded(_ book: ClickBook) -> Bool {
        guard !book.localRelativePath.isEmpty else { return false }
        return fileManager.fileExists(atPath: fileURL(for: book).path)
    }

    func annotations(for bookID: UUID) -> [ClickAnnotation] {
        annotations
            .filter { $0.bookID == bookID && !$0.serverDeleted.orFalse }
            .sorted { $0.updatedAt > $1.updatedAt }
    }

    func bookmarks(for bookID: UUID) -> [ClickAnnotation] {
        annotations(for: bookID).filter { $0.kind == .bookmark }
    }

    func readingAnnotations(for bookID: UUID) -> [ClickAnnotation] {
        annotations(for: bookID).filter { $0.kind != .bookmark }
    }

    func importDocuments(_ urls: [URL]) async {
        guard !urls.isEmpty else { return }
        isImporting = true
        showStatus("正在导入…")
        defer { isImporting = false }

        var importedCount = 0
        var skippedCount = 0

        for sourceURL in urls {
            guard let format = ClickBookFormat.infer(from: sourceURL) else {
                skippedCount += 1
                continue
            }

            let didStartAccess = sourceURL.startAccessingSecurityScopedResource()
            defer {
                if didStartAccess {
                    sourceURL.stopAccessingSecurityScopedResource()
                }
            }

            do {
                let contentHash = try Self.sha256(of: sourceURL)
                if books.contains(where: { $0.contentHash == contentHash }) {
                    skippedCount += 1
                    continue
                }

                let bookID = UUID()
                let bookDirectory = booksURL.appendingPathComponent(bookID.uuidString, isDirectory: true)
                try fileManager.createDirectory(
                    at: bookDirectory,
                    withIntermediateDirectories: true
                )
                let safeName = sourceURL.lastPathComponent.isEmpty
                    ? "book.\(sourceURL.pathExtension.lowercased())"
                    : sourceURL.lastPathComponent
                let destination = bookDirectory.appendingPathComponent(safeName)
                try fileManager.copyItem(at: sourceURL, to: destination)

                let relativePath = destination.path.replacingOccurrences(
                    of: rootURL.path + "/",
                    with: ""
                )
                let title = sourceURL.deletingPathExtension().lastPathComponent
                let book = ClickBook(
                    id: bookID,
                    title: ClickBook.normalizedTitle(title),
                    author: "",
                    format: format,
                    contentHash: contentHash,
                    localRelativePath: relativePath,
                    importedAt: Date(),
                    lastOpenedAt: nil,
                    progress: 0,
                    lastLocatorJSON: nil,
                    isFavorite: false,
                    folderName: nil
                )
                books.append(book)
                requestLocalCover(for: bookID)
                enqueue(entityID: bookID.uuidString, entityType: "book", action: "import")
                importedCount += 1
            } catch {
                skippedCount += 1
                showStatus("有文件未能导入：\(error.localizedDescription)")
            }
        }

        sortBooks()
        persist()
        if importedCount > 0 {
            showTransientStatus(
                skippedCount == 0
                    ? "已导入 \(importedCount) 本书"
                    : "已导入 \(importedCount) 本，跳过 \(skippedCount) 个文件"
            )
        } else if skippedCount > 0 {
            showStatus("没有导入新书；可能是重复文件或不支持的格式")
        } else {
            clearStatus()
        }
    }

    func select(_ book: ClickBook) {
        guard isBookDownloaded(book) else {
            requestedDownloadBookID = book.id
            showStatus("正在下载《\(book.displayTitle)》…")
            return
        }
        selectedBookID = book.id
        guard let index = books.firstIndex(where: { $0.id == book.id }) else { return }
        books[index].lastOpenedAt = Date()
        sortBooks()
        persist()
    }

    func closeReader() {
        selectedBookID = nil
    }

    func consumeDownloadRequest(_ bookID: UUID) {
        guard requestedDownloadBookID == bookID else { return }
        requestedDownloadBookID = nil
    }

    func toggleFavorite(_ book: ClickBook) {
        guard let index = books.firstIndex(where: { $0.id == book.id }) else { return }
        books[index].isFavorite.toggle()
        enqueue(
            entityID: book.id.uuidString,
            entityType: "book",
            action: books[index].isFavorite ? "favorite" : "unfavorite"
        )
        persist()
    }

    func setFolder(_ folderName: String?, for book: ClickBook) {
        setFolder(folderName, forBooks: [book])
    }

    func setFolder(_ folderName: String?, forBooks selectedBooks: [ClickBook]) {
        setFolderPath(
            ClickFolderPath(rawValue: folderName),
            forBooks: selectedBooks
        )
    }

    func setFolderPath(_ path: ClickFolderPath, for book: ClickBook) {
        setFolderPath(path, forBooks: [book])
    }

    func setFolderPath(_ path: ClickFolderPath, forBooks selectedBooks: [ClickBook]) {
        let normalized = path.isRoot ? nil : path.rawValue
        let selectedIDs = Set(selectedBooks.map(\.id))
        guard !selectedIDs.isEmpty else { return }

        var changedIDs: [UUID] = []
        for index in books.indices where selectedIDs.contains(books[index].id) {
            guard books[index].folderName != normalized else { continue }
            books[index].folderName = normalized
            changedIDs.append(books[index].id)
        }
        guard !changedIDs.isEmpty else { return }

        for id in changedIDs {
            enqueue(entityID: id.uuidString, entityType: "book", action: "organize")
        }
        if changedIDs.count == 1, let book = selectedBooks.first(where: { changedIDs.contains($0.id) }) {
            showTransientStatus(
                normalized.map { "已将《\(book.displayTitle)》放入“\($0)”" }
                    ?? "已将《\(book.displayTitle)》移出文件夹"
            )
        } else {
            showTransientStatus(
                normalized.map { "已将 \(changedIDs.count) 本书放入“\($0)”" }
                    ?? "已将 \(changedIDs.count) 本书移出文件夹"
            )
        }
        persist()
    }

    func renameFolder(_ oldName: String, to newName: String) {
        renameFolder(at: ClickFolderPath(rawValue: oldName), to: newName)
    }

    func renameFolder(at path: ClickFolderPath, to newLeafName: String) {
        guard !path.isRoot,
              let renamedPath = path.parent.appending(newLeafName),
              renamedPath != path else { return }
        let affected = books(inFolderPath: path)
        guard !affected.isEmpty else { return }
        var changedIDs: [UUID] = []
        let assignments = Dictionary(uniqueKeysWithValues: affected.map { book in
            let sourcePath = folderPath(for: book)
            let suffix = sourcePath.components.dropFirst(path.depth)
            let target = ClickFolderPath(components: renamedPath.components + suffix)
            return (book.id, target)
        })
        for index in books.indices {
            guard let target = assignments[books[index].id],
                  books[index].folderName != target.rawValue else { continue }
            books[index].folderName = target.rawValue
            changedIDs.append(books[index].id)
        }
        guard !changedIDs.isEmpty else { return }
        for id in changedIDs {
            enqueue(entityID: id.uuidString, entityType: "book", action: "organize")
        }
        persist()
        showTransientStatus("已将“\(path.leafName)”重命名为“\(renamedPath.leafName)”")
    }

    func removeFolder(_ folderName: String) {
        moveFolderContentsToParent(ClickFolderPath(rawValue: folderName))
    }

    func moveFolderContentsToParent(_ path: ClickFolderPath) {
        guard !path.isRoot else { return }
        let affected = books(inFolderPath: path)
        guard !affected.isEmpty else { return }
        var changedIDs: [UUID] = []
        let assignments = Dictionary(uniqueKeysWithValues: affected.map { book in
            let sourcePath = folderPath(for: book)
            let suffix = sourcePath.components.dropFirst(path.depth)
            let target = ClickFolderPath(components: path.parent.components + suffix)
            return (book.id, target)
        })
        for index in books.indices {
            guard let target = assignments[books[index].id] else { continue }
            let normalized = target.isRoot ? nil : target.rawValue
            guard books[index].folderName != normalized else { continue }
            books[index].folderName = normalized
            changedIDs.append(books[index].id)
        }
        guard !changedIDs.isEmpty else { return }
        for id in changedIDs {
            enqueue(entityID: id.uuidString, entityType: "book", action: "organize")
        }
        persist()
        showTransientStatus("已将“\(path.leafName)”中的 \(changedIDs.count) 本书移到上一级")
    }

    func updateProgress(bookID: UUID, progress: Double) {
        guard let index = books.firstIndex(where: { $0.id == bookID }) else { return }
        let bounded = min(max(progress, 0), 1)
        guard abs(books[index].progress - bounded) >= 0.005 else { return }
        books[index].progress = bounded
        books[index].lastOpenedAt = Date()
        enqueue(entityID: bookID.uuidString, entityType: "position", action: "upsert")
        persist()
    }

    func updateEPUBLocation(bookID: UUID, progress: Double?, locatorJSON: String) {
        guard let index = books.firstIndex(where: { $0.id == bookID }) else { return }
        let previousLocator = books[index].lastLocatorJSON
        let boundedProgress = progress.map { min(max($0, 0), 1) }
        let progressed = boundedProgress.map {
            abs(books[index].progress - $0) >= 0.005
        } ?? false
        guard previousLocator != locatorJSON || progressed else { return }

        books[index].lastLocatorJSON = locatorJSON
        if let boundedProgress {
            books[index].progress = boundedProgress
        }
        books[index].lastOpenedAt = Date()
        enqueue(entityID: bookID.uuidString, entityType: "position", action: "upsert")
        persist()
    }

    func addNote(
        bookID: UUID,
        excerpt: String,
        note: String,
        locatorJSON: String = "",
        audioDraftURL: URL? = nil
    ) {
        let trimmed = note.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty || audioDraftURL != nil else { return }
        let annotationID = UUID()
        var audioRelativePath: String?
        if let audioDraftURL {
            do {
                try fileManager.createDirectory(
                    at: voiceNotesURL,
                    withIntermediateDirectories: true
                )
                let destination = voiceNotesURL
                    .appendingPathComponent("\(annotationID.uuidString.lowercased()).m4a")
                try fileManager.copyItem(at: audioDraftURL, to: destination)
                audioRelativePath = destination.path.replacingOccurrences(
                    of: rootURL.path + "/",
                    with: ""
                )
            } catch {
                showStatus("语音原音没有写入阅读资料：\(error.localizedDescription)")
                return
            }
        }
        let annotation = ClickAnnotation(
            id: annotationID,
            bookID: bookID,
            kind: audioRelativePath == nil ? .note : .voiceNote,
            excerpt: excerpt,
            note: trimmed.isEmpty ? "语音备注" : trimmed,
            locatorJSON: locatorJSON,
            createdAt: Date(),
            updatedAt: Date(),
            syncState: "pending",
            audioRelativePath: audioRelativePath,
            transcriptState: audioRelativePath == nil
                ? nil
                : (trimmed.isEmpty ? "audio_only" : "on_device_complete")
        )
        annotations.append(annotation)
        enqueue(
            entityID: annotation.id.uuidString,
            entityType: "annotation",
            action: "upsert"
        )
        persist()
    }

    @discardableResult
    func addHighlight(
        bookID: UUID,
        excerpt: String,
        locatorJSON: String = ""
    ) -> UUID? {
        let trimmed = excerpt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        let annotation = ClickAnnotation(
            id: UUID(),
            bookID: bookID,
            kind: .highlight,
            excerpt: trimmed,
            note: "",
            locatorJSON: locatorJSON,
            createdAt: Date(),
            updatedAt: Date(),
            syncState: "pending"
        )
        annotations.append(annotation)
        enqueue(
            entityID: annotation.id.uuidString,
            entityType: "annotation",
            action: "upsert"
        )
        persist()
        return annotation.id
    }

    @discardableResult
    func updateHighlight(
        bookID: UUID,
        annotationID: UUID,
        excerpt: String,
        locatorJSON: String
    ) -> UUID? {
        let trimmed = excerpt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard
            !trimmed.isEmpty,
            let index = annotations.firstIndex(where: {
                $0.id == annotationID
                    && $0.bookID == bookID
                    && $0.kind == .highlight
                    && $0.serverDeleted != true
            })
        else {
            return nil
        }

        annotations[index].excerpt = trimmed
        annotations[index].locatorJSON = locatorJSON
        annotations[index].updatedAt = Date()
        annotations[index].syncState = "pending"
        let hasMutablePendingUpsert = pendingOperations.contains {
            $0.entityID == annotationID.uuidString
                && $0.entityType == "annotation"
                && $0.action == "upsert"
                && ($0.syncState ?? "pending").isActionableSyncState
                && $0.payloadJSON == nil
        }
        if !hasMutablePendingUpsert {
            enqueue(
                entityID: annotationID.uuidString,
                entityType: "annotation",
                action: "upsert"
            )
        }
        persist()
        return annotationID
    }

    func removeHighlights(bookID: UUID, annotationIDs: [UUID]) {
        let uniqueIDs = Set(annotationIDs)
        guard !uniqueIDs.isEmpty else { return }

        let removableIDs = Set(
            annotations.compactMap { annotation -> UUID? in
                guard
                    uniqueIDs.contains(annotation.id),
                    annotation.bookID == bookID,
                    annotation.kind == .highlight,
                    !annotation.serverDeleted.orFalse
                else {
                    return nil
                }
                return annotation.id
            }
        )
        guard !removableIDs.isEmpty else { return }

        for annotationID in removableIDs {
            guard let index = annotations.firstIndex(where: {
                $0.id == annotationID
            }) else {
                continue
            }
            if annotations[index].serverID == nil {
                pendingOperations.removeAll {
                    $0.entityType == "annotation"
                        && $0.entityID == annotationID.uuidString
                }
                annotations.remove(at: index)
            } else {
                annotations[index].serverDeleted = true
                annotations[index].syncState = "pending"
                pendingOperations.removeAll {
                    $0.entityType == "annotation"
                        && $0.entityID == annotationID.uuidString
                        && ($0.syncState ?? "pending").isActionableSyncState
                }
                enqueue(
                    entityID: annotationID.uuidString,
                    entityType: "annotation",
                    action: "delete"
                )
            }
        }
        showTransientStatus(
            removableIDs.count == 1
                ? "已取消标红"
                : "已清除这个区域的重复标红"
        )
        persist()
    }

    func addBookmark(
        bookID: UUID,
        locatorJSON: String,
        excerpt: String = "当前阅读位置"
    ) {
        let locator = locatorJSON.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !locator.isEmpty else {
            showStatus("阅读位置还没有准备好，请翻动一页后再试")
            return
        }
        if bookmarks(for: bookID).contains(where: { $0.locatorJSON == locator }) {
            showTransientStatus("这里已经有书签")
            return
        }
        let cleanExcerpt = excerpt.trimmingCharacters(in: .whitespacesAndNewlines)
        let annotation = ClickAnnotation(
            id: UUID(),
            bookID: bookID,
            kind: .bookmark,
            excerpt: cleanExcerpt.isEmpty ? "当前阅读位置" : cleanExcerpt,
            note: "",
            locatorJSON: locator,
            createdAt: Date(),
            updatedAt: Date(),
            syncState: "pending"
        )
        annotations.append(annotation)
        enqueue(
            entityID: annotation.id.uuidString,
            entityType: "annotation",
            action: "upsert"
        )
        showTransientStatus("书签已保存")
        persist()
    }

    func deleteAnnotation(_ annotation: ClickAnnotation) {
        guard let index = annotations.firstIndex(where: { $0.id == annotation.id }) else {
            return
        }
        if annotations[index].serverID == nil {
            pendingOperations.removeAll {
                $0.entityType == "annotation" && $0.entityID == annotation.id.uuidString
            }
            annotations.remove(at: index)
        } else {
            annotations[index].serverDeleted = true
            annotations[index].syncState = "pending"
            pendingOperations.removeAll {
                $0.entityType == "annotation"
                    && $0.entityID == annotation.id.uuidString
                    && ($0.syncState ?? "pending").isActionableSyncState
            }
            enqueue(
                entityID: annotation.id.uuidString,
                entityType: "annotation",
                action: "delete"
            )
        }
        persist()
    }

    func textContent(for book: ClickBook) async -> String {
        let url = fileURL(for: book)
        return await Task.detached(priority: .userInitiated) {
            switch book.format {
            case .text:
                return (try? String(contentsOf: url, encoding: .utf8))
                    ?? (try? String(contentsOf: url, encoding: .unicode))
                    ?? ""
            case .pdf, .epub:
                return ""
            }
        }.value
    }

    private func prepareStorage() {
        do {
            try fileManager.createDirectory(at: booksURL, withIntermediateDirectories: true)
            try fileManager.createDirectory(
                at: voiceNotesURL,
                withIntermediateDirectories: true
            )
        } catch {
            showStatus("无法创建本地书库：\(error.localizedDescription)")
        }
    }

    private func load() {
        guard fileManager.fileExists(atPath: manifestURL.path) else { return }
        do {
            let data = try Data(contentsOf: manifestURL)
            let snapshot = try JSONDecoder.click.decode(LibrarySnapshot.self, from: data)
            books = snapshot.books.filter {
                isBookDownloaded($0) || !($0.serverID ?? "").isEmpty
            }
            annotations = snapshot.annotations
            pendingOperations = snapshot.pendingOperations
            syncSequence = max(0, snapshot.syncSequence ?? 0)
            hasFullBaseline = snapshot.hasFullBaseline ?? false
            syncConflicts = snapshot.conflicts ?? []
            let repairedMetadata = normalizeStoredBookMetadata()
            sortBooks()
            if repairedMetadata {
                persist()
            }
        } catch {
            showStatus("本地书库索引无法读取；原书文件仍保留")
        }
    }

    private func normalizeStoredBookMetadata() -> Bool {
        var changed = false
        for index in books.indices {
            let title = ClickBook.normalizedTitle(books[index].title)
            let author = ClickBook.normalizedAuthor(books[index].author)
            if books[index].title != title {
                books[index].title = title
                changed = true
            }
            if books[index].author != author {
                books[index].author = author
                changed = true
            }
        }
        return changed
    }

    private func persist() {
        let snapshot = LibrarySnapshot(
            schemaVersion: 2,
            books: books,
            annotations: annotations,
            pendingOperations: pendingOperations,
            syncSequence: syncSequence,
            hasFullBaseline: hasFullBaseline,
            conflicts: syncConflicts
        )
        do {
            let data = try JSONEncoder.click.encode(snapshot)
            try data.write(to: manifestURL, options: .atomic)
        } catch {
            showStatus("本地状态暂未写入：\(error.localizedDescription)")
        }
    }

    private func enqueue(entityID: String, entityType: String, action: String) {
        if entityType == "position" {
            pendingOperations.removeAll {
                $0.entityID == entityID
                    && $0.entityType == entityType
                    && ($0.syncState ?? "pending").isActionableSyncState
                    && $0.payloadJSON == nil
            }
        } else if entityType == "book", action != "import" {
            pendingOperations.removeAll {
                $0.entityID == entityID
                    && $0.entityType == entityType
                    && $0.action != "import"
                    && ($0.syncState ?? "pending").isActionableSyncState
                    && $0.payloadJSON == nil
            }
        }
        pendingOperations.append(
            PendingMobileOperation(
                id: UUID(),
                operationID: "ios-\(UUID().uuidString.lowercased())",
                entityID: entityID,
                entityType: entityType,
                action: action,
                createdAt: Date(),
                attemptCount: 0,
                lastError: nil,
                syncState: "pending"
            )
        )
    }

    func localImportOperations() -> [(operation: PendingMobileOperation, book: ClickBook)] {
        pendingOperations.compactMap { operation in
            guard operation.entityType == "book",
                  operation.action == "import",
                  (operation.syncState ?? "pending").isActionableSyncState,
                  operation.nextAttemptAt.map({ $0 <= Date() }) ?? true,
                  let bookID = UUID(uuidString: operation.entityID),
                  let book = books.first(where: { $0.id == bookID }),
                  book.serverID == nil,
                  isBookDownloaded(book) else {
                return nil
            }
            return (operation, book)
        }
    }

    func nextVoiceNoteUploadCandidate() -> VoiceNoteUploadCandidate? {
        for operationIndex in pendingOperations.indices {
            let operation = pendingOperations[operationIndex]
            guard operation.entityType == "annotation",
                  operation.payloadJSON == nil,
                  (operation.syncState ?? "pending").isActionableSyncState,
                  operation.nextAttemptAt.map({ $0 <= Date() }) ?? true,
                  let annotationID = UUID(uuidString: operation.entityID),
                  let annotation = annotations.first(where: {
                      $0.id == annotationID && $0.kind == .voiceNote
                  }),
                  let book = books.first(where: { $0.id == annotation.bookID }),
                  let bookServerID = book.serverID else {
                continue
            }
            guard let audioURL = audioURL(for: annotation) else {
                pendingOperations[operationIndex].syncState = "permanent_failed"
                pendingOperations[operationIndex].lastError =
                    "语音备注原音不存在；文字与记录仍保留在 iPad"
                persist()
                return nil
            }
            return VoiceNoteUploadCandidate(
                operationID: operation.operationID,
                annotationID: annotationID,
                bookServerID: bookServerID,
                audioURL: audioURL,
                excerpt: annotation.excerpt,
                note: annotation.note,
                locatorJSON: annotation.locatorJSON
            )
        }
        return nil
    }

    func voiceNoteOperationEnvelope(
        candidate: VoiceNoteUploadCandidate,
        deviceID: String,
        audioBase64: String,
        audioHash: String
    ) -> [String: Any]? {
        guard let operationIndex = pendingOperations.firstIndex(where: {
            $0.operationID == candidate.operationID
        }) else {
            return nil
        }
        let locator = Self.jsonObject(candidate.locatorJSON) ?? [:]
        let chapterLocator = locator["href"] as? String
            ?? (candidate.locatorJSON.isEmpty
                ? "local:\(candidate.annotationID.uuidString)"
                : candidate.locatorJSON)
        let payload: [String: Any] = [
            "book_id": candidate.bookServerID,
            "audio_base64": audioBase64,
            "mime_type": "audio/mp4",
            "source_text": candidate.excerpt,
            "note_text": candidate.note,
            "chapter_locator": chapterLocator,
            "range_locator": locator,
            "metadata": [
                "schema": "click.ipad.readium_audio_note.v1",
                "audio_hash": audioHash,
                "durability": "durable",
            ],
        ]
        pendingOperations[operationIndex].operationType = "audio_note_created"
        pendingOperations[operationIndex].bookServerID = candidate.bookServerID
        pendingOperations[operationIndex].baseServerVersion = nil
        if let annotationIndex = annotations.firstIndex(where: {
            $0.id == candidate.annotationID
        }) {
            annotations[annotationIndex].audioHash = audioHash
        }
        persist()
        return operationEnvelope(
            operation: pendingOperations[operationIndex],
            operationType: "audio_note_created",
            bookServerID: candidate.bookServerID,
            payload: payload,
            deviceID: deviceID
        )
    }

    func markOperationPreparationFailure(
        operationID: String,
        message: String,
        retryable: Bool
    ) {
        guard let index = pendingOperations.firstIndex(where: {
            $0.operationID == operationID
        }) else {
            return
        }
        pendingOperations[index].attemptCount += 1
        pendingOperations[index].lastError = message
        pendingOperations[index].syncState = retryable
            ? "retryable"
            : "permanent_failed"
        if retryable {
            let exponent = min(pendingOperations[index].attemptCount, 8)
            pendingOperations[index].nextAttemptAt = Date().addingTimeInterval(
                min(21_600.0, 30.0 * pow(2.0, Double(exponent)))
            )
        }
        persist()
    }

    func markImportSucceeded(
        operationID: String,
        localBookID: UUID,
        serverID: String,
        sourceHash: String,
        sourceByteSize: Int64
    ) {
        guard let bookIndex = books.firstIndex(where: { $0.id == localBookID }) else { return }
        books[bookIndex].serverID = serverID
        books[bookIndex].remoteSourceHash = sourceHash
        books[bookIndex].remoteSourceByteSize = sourceByteSize
        books[bookIndex].serverRemoved = false
        pendingOperations.removeAll { $0.operationID == operationID }
        persist()
    }

    func markOperationTransportFailure(operationIDs: Set<String>, message: String) {
        let now = Date()
        for index in pendingOperations.indices
        where operationIDs.contains(pendingOperations[index].operationID) {
            pendingOperations[index].attemptCount += 1
            pendingOperations[index].lastError = message
            pendingOperations[index].syncState = "retryable"
            let exponent = min(pendingOperations[index].attemptCount, 8)
            let delay = min(21_600.0, 30.0 * pow(2.0, Double(exponent)))
            pendingOperations[index].nextAttemptAt = now.addingTimeInterval(delay)
        }
        persist()
    }

    func markImportFailure(operationID: String, message: String, retryable: Bool) {
        guard let index = pendingOperations.firstIndex(where: {
            $0.operationID == operationID
        }) else { return }
        pendingOperations[index].attemptCount += 1
        pendingOperations[index].lastError = message
        pendingOperations[index].syncState = retryable ? "retryable" : "permanent_failed"
        if retryable {
            let exponent = min(pendingOperations[index].attemptCount, 8)
            pendingOperations[index].nextAttemptAt = Date().addingTimeInterval(
                min(21_600.0, 30.0 * pow(2.0, Double(exponent)))
            )
        }
        persist()
    }

    func upsertRemoteBook(
        serverID: String,
        title: String,
        author: String,
        format: ClickBookFormat,
        contentHash: String,
        sourcePath: String,
        sourceHash: String,
        sourceByteSize: Int64,
        serverVersion: Int,
        favorite: Bool,
        folderName: String?,
        coverPath: String?
    ) -> UUID {
        let normalizedTitle = ClickBook.normalizedTitle(title)
        let normalizedAuthor = ClickBook.normalizedAuthor(author)
        let index = books.firstIndex {
            $0.serverID == serverID
                || (!$0.contentHash.isEmpty && $0.contentHash == contentHash)
                || (!$0.contentHash.isEmpty && $0.contentHash == sourceHash)
        }
        if let index {
            let previousSourceHash = books[index].remoteSourceHash?
                .trimmingCharacters(in: .whitespacesAndNewlines)
                .lowercased()
            let normalizedSourceHash = sourceHash
                .trimmingCharacters(in: .whitespacesAndNewlines)
                .lowercased()
            let sourceChanged = !(previousSourceHash ?? "").isEmpty
                && !normalizedSourceHash.isEmpty
                && previousSourceHash != normalizedSourceHash
            let hasPendingOrganization = pendingOperations.contains {
                $0.entityType == "book"
                    && $0.entityID == books[index].id.uuidString
                    && $0.action != "import"
                    && ($0.syncState ?? "pending").isActionableSyncState
            }
            books[index].serverID = serverID
            books[index].serverVersion = max(1, serverVersion)
            if ClickBook.hasUsefulTitle(title)
                || !ClickBook.hasUsefulTitle(books[index].title) {
                books[index].title = normalizedTitle
            }
            if ClickBook.hasUsefulAuthor(author)
                || !ClickBook.hasUsefulAuthor(books[index].author) {
                books[index].author = normalizedAuthor
            }
            books[index].remoteSourcePath = sourcePath
            books[index].remoteSourceHash = sourceHash
            books[index].remoteSourceByteSize = sourceByteSize
            if sourceChanged {
                books[index].remoteDownloadState = "pending"
                books[index].remoteDownloadAttemptCount = 0
                books[index].remoteDownloadNextAttemptAt = nil
                books[index].remoteDownloadError = nil
            }
            let normalizedCoverPath = coverPath?.trimmingCharacters(
                in: .whitespacesAndNewlines
            )
            if books[index].remoteCoverPath != normalizedCoverPath {
                books[index].remoteCoverPath = normalizedCoverPath
                books[index].remoteCoverState = "pending"
                books[index].remoteCoverNextAttemptAt = nil
            }
            if !hasPendingOrganization {
                books[index].isFavorite = favorite
                books[index].folderName = folderName
            }
            books[index].serverRemoved = false
            persist()
            return books[index].id
        }

        let localID = UUID()
        books.append(
            ClickBook(
                id: localID,
                title: normalizedTitle,
                author: normalizedAuthor,
                format: format,
                contentHash: contentHash.isEmpty ? sourceHash : contentHash,
                localRelativePath: "",
                importedAt: Date(),
                lastOpenedAt: nil,
                progress: 0,
                lastLocatorJSON: nil,
                isFavorite: favorite,
                folderName: folderName,
                serverID: serverID,
                serverVersion: max(1, serverVersion),
                remoteSourcePath: sourcePath,
                remoteSourceHash: sourceHash,
                remoteSourceByteSize: sourceByteSize,
                serverRemoved: false,
                remoteDownloadState: "pending",
                remoteDownloadAttemptCount: 0,
                remoteDownloadNextAttemptAt: nil,
                remoteDownloadError: nil,
                localCoverRelativePath: nil,
                remoteCoverPath: coverPath,
                remoteCoverState: "pending",
                remoteCoverNextAttemptAt: nil
            )
        )
        sortBooks()
        persist()
        return localID
    }

    func installDownloadedBook(
        localBookID: UUID,
        temporaryURL: URL,
        suggestedFilename: String
    ) throws {
        guard let index = books.firstIndex(where: { $0.id == localBookID }) else { return }
        let directory = booksURL.appendingPathComponent(localBookID.uuidString, isDirectory: true)
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        let fallbackExtension = books[index].format.rawValue
        let rawExtension = (suggestedFilename as NSString).pathExtension.lowercased()
        let fileExtension = rawExtension.isEmpty ? fallbackExtension : rawExtension
        let destination = directory.appendingPathComponent("source.\(fileExtension)")
        let staging = directory.appendingPathComponent(".source-\(UUID().uuidString).tmp")
        if fileManager.fileExists(atPath: staging.path) {
            try fileManager.removeItem(at: staging)
        }
        try fileManager.moveItem(at: temporaryURL, to: staging)
        if fileManager.fileExists(atPath: destination.path) {
            try fileManager.removeItem(at: destination)
        }
        try fileManager.moveItem(at: staging, to: destination)
        books[index].localRelativePath = destination.path.replacingOccurrences(
            of: rootURL.path + "/",
            with: ""
        )
        books[index].remoteDownloadState = "complete"
        books[index].remoteDownloadAttemptCount = 0
        books[index].remoteDownloadNextAttemptAt = nil
        books[index].remoteDownloadError = nil
        showTransientStatus("《\(books[index].displayTitle)》已可离线阅读")
        persist()
        localCoverAttemptedBookIDs.remove(localBookID)
        requestLocalCover(for: localBookID)
    }

    func requestLocalCover(for bookID: UUID) {
        guard !queuedLocalCoverBookIDs.contains(bookID),
              !localCoverAttemptedBookIDs.contains(bookID),
              let book = books.first(where: { $0.id == bookID }),
              isBookDownloaded(book),
              book.format == .epub || book.format == .pdf,
              coverURL(for: book) == nil || book.needsLocalMetadataRepair else {
            return
        }
        localCoverAttemptedBookIDs.insert(bookID)
        queuedLocalCoverBookIDs.insert(bookID)
        pendingLocalCoverBookIDs.append(bookID)
        startLocalCoverWorkerIfNeeded()
    }

    private func startLocalCoverWorkerIfNeeded() {
        guard localCoverWorkerTask == nil else { return }
        localCoverWorkerTask = Task(priority: .utility) { [weak self] in
            guard let self else { return }
            while !Task.isCancelled,
                  let bookID = self.nextPendingLocalCoverBookID() {
                await self.extractLocalCover(for: bookID)
            }
            self.localCoverWorkerTask = nil
        }
    }

    private func nextPendingLocalCoverBookID() -> UUID? {
        guard !pendingLocalCoverBookIDs.isEmpty else { return nil }
        let bookID = pendingLocalCoverBookIDs.removeFirst()
        queuedLocalCoverBookIDs.remove(bookID)
        return bookID
    }

    private func extractLocalCover(for bookID: UUID) async {
        guard let book = books.first(where: { $0.id == bookID }),
              isBookDownloaded(book),
              coverURL(for: book) == nil || book.needsLocalMetadataRepair else {
            return
        }
        let sourceURL = fileURL(for: book)
        let extraction = await LocalBookCoverExtractor.extract(from: sourceURL)
        guard !Task.isCancelled else { return }
        if let extraction {
            repairLocalMetadata(
                for: bookID,
                title: extraction.title,
                author: extraction.author
            )
        }
        if let data = extraction?.coverJPEGData,
           let current = books.first(where: { $0.id == bookID }),
           coverURL(for: current) == nil {
            try? installDownloadedCover(
                localBookID: bookID,
                data: data
            )
        }
    }

    private func repairLocalMetadata(
        for bookID: UUID,
        title: String?,
        author: String?
    ) {
        guard let index = books.firstIndex(where: { $0.id == bookID }) else { return }
        var changed = false
        if !ClickBook.hasUsefulTitle(books[index].title),
           let title,
           ClickBook.hasUsefulTitle(title) {
            books[index].title = ClickBook.normalizedTitle(title)
            changed = true
        }
        if !ClickBook.hasUsefulAuthor(books[index].author),
           let author,
           ClickBook.hasUsefulAuthor(author) {
            books[index].author = ClickBook.normalizedAuthor(author)
            changed = true
        }
        guard changed else { return }
        sortBooks()
        persist()
    }

    func installDownloadedCover(localBookID: UUID, data: Data) throws {
        guard let index = books.firstIndex(where: { $0.id == localBookID }) else {
            return
        }
        guard Self.isDecodableCoverData(data) else {
            throw WorkspaceStoreError.invalidCoverImage
        }
        let directory = booksURL.appendingPathComponent(
            localBookID.uuidString,
            isDirectory: true
        )
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        let destination = directory.appendingPathComponent("cover.img")
        try data.write(to: destination, options: .atomic)
        invalidCoverPaths.remove(destination.path)
        validCoverPaths.insert(destination.path)
        books[index].localCoverRelativePath = destination.path.replacingOccurrences(
            of: rootURL.path + "/",
            with: ""
        )
        books[index].remoteCoverState = "complete"
        books[index].remoteCoverNextAttemptAt = nil
        persist()
    }

    func nextRemoteBookNeedingCover(preferredBookID: UUID? = nil) -> ClickBook? {
        let now = Date()
        func isCandidate(_ book: ClickBook) -> Bool {
            guard !book.serverRemoved.orFalse,
                  coverURL(for: book) == nil,
                  remoteCoverPath(for: book) != nil,
                  book.remoteCoverState != "unavailable" else {
                return false
            }
            return book.remoteCoverNextAttemptAt.map { $0 <= now } ?? true
        }
        if let preferredBookID,
           let preferred = books.first(where: { $0.id == preferredBookID }),
           isCandidate(preferred) {
            return preferred
        }
        return books.first(where: isCandidate)
    }

    func markRemoteCoverFailure(bookID: UUID, retryable: Bool) {
        guard let index = books.firstIndex(where: { $0.id == bookID }) else { return }
        books[index].remoteCoverState = retryable ? "retryable" : "unavailable"
        books[index].remoteCoverNextAttemptAt = retryable
            ? Date().addingTimeInterval(300)
            : nil
        persist()
    }

    func nextRemoteBookNeedingDownload(preferredBookID: UUID? = nil) -> ClickBook? {
        let now = Date()
        func isCandidate(_ book: ClickBook) -> Bool {
            let hasPendingReplacement = book.remoteDownloadState == "pending"
                || book.remoteDownloadState == "retryable"
            guard !book.serverRemoved.orFalse,
                  (!isBookDownloaded(book) || hasPendingReplacement),
                  book.remoteDownloadState != "terminal",
                  book.remoteDownloadNextAttemptAt.map({ $0 <= now }) ?? true,
                  !(book.remoteSourcePath ?? "").isEmpty else {
                return false
            }
            return true
        }
        if let preferredBookID,
           let preferred = books.first(where: { $0.id == preferredBookID }),
           isCandidate(preferred) {
            return preferred
        }
        return books.first(where: isCandidate)
    }

    func markRemoteDownloadFailure(
        bookID: UUID,
        message: String,
        retryable: Bool
    ) {
        guard let index = books.firstIndex(where: { $0.id == bookID }) else { return }
        let attempts = (books[index].remoteDownloadAttemptCount ?? 0) + 1
        books[index].remoteDownloadAttemptCount = attempts
        books[index].remoteDownloadError = message
        books[index].remoteDownloadState = retryable ? "retryable" : "terminal"
        if retryable {
            let exponent = min(attempts, 8)
            books[index].remoteDownloadNextAttemptAt = Date().addingTimeInterval(
                min(21_600.0, 30.0 * pow(2.0, Double(exponent)))
            )
        } else {
            books[index].remoteDownloadNextAttemptAt = nil
        }
        persist()
    }

    func markRemoteBooksRemoved(serverIDs: [String]) {
        guard !serverIDs.isEmpty else { return }
        let removed = Set(serverIDs)
        for index in books.indices where removed.contains(books[index].serverID ?? "") {
            books[index].serverRemoved = true
        }
        persist()
    }

    func applyRemotePosition(
        serverBookID: String,
        progress: Double,
        locatorJSON: String
    ) {
        guard let index = books.firstIndex(where: { $0.serverID == serverBookID }) else { return }
        let hasPendingLocalPosition = pendingOperations.contains {
            $0.entityType == "position"
                && $0.entityID == books[index].id.uuidString
                && ($0.syncState ?? "pending").isActionableSyncState
        }
        guard !hasPendingLocalPosition else { return }
        books[index].progress = min(max(progress, 0), 1)
        books[index].lastLocatorJSON = locatorJSON.isEmpty
            ? books[index].lastLocatorJSON
            : locatorJSON
        persist()
    }

    func applyRemoteAnnotation(
        serverID: String,
        serverBookID: String,
        kind: AnnotationKind,
        excerpt: String,
        note: String,
        locatorJSON: String,
        createdAt: Date,
        updatedAt: Date,
        serverVersion: Int
    ) {
        guard let book = books.first(where: { $0.serverID == serverBookID }) else { return }
        if let index = annotations.firstIndex(where: { $0.serverID == serverID }) {
            let hasPendingLocalChange = pendingOperations.contains {
                $0.entityType == "annotation"
                    && $0.entityID == annotations[index].id.uuidString
                    && ($0.syncState ?? "pending").isActionableSyncState
            }
            guard !hasPendingLocalChange else { return }
            annotations[index].excerpt = excerpt
            annotations[index].note = note
            annotations[index].locatorJSON = locatorJSON
            annotations[index].updatedAt = updatedAt
            annotations[index].serverVersion = max(1, serverVersion)
            annotations[index].serverDeleted = false
            annotations[index].syncState = "synced"
        } else {
            annotations.append(
                ClickAnnotation(
                    id: UUID(),
                    bookID: book.id,
                    kind: kind,
                    excerpt: excerpt,
                    note: note,
                    locatorJSON: locatorJSON,
                    createdAt: createdAt,
                    updatedAt: updatedAt,
                    syncState: "synced",
                    serverID: serverID,
                    serverVersion: max(1, serverVersion),
                    serverDeleted: false
                )
            )
        }
        persist()
    }

    func markRemoteAnnotationsRemoved(serverIDs: [String]) {
        let removed = Set(serverIDs)
        guard !removed.isEmpty else { return }
        for index in annotations.indices where removed.contains(annotations[index].serverID ?? "") {
            let pending = pendingOperations.contains {
                $0.entityType == "annotation"
                    && $0.entityID == annotations[index].id.uuidString
                    && ($0.syncState ?? "pending").isActionableSyncState
            }
            annotations[index].serverDeleted = true
            annotations[index].syncState = pending ? "conflict" : "server_deleted"
            if pending {
                let operation = pendingOperations.first {
                    $0.entityType == "annotation" && $0.entityID == annotations[index].id.uuidString
                }
                appendConflict(
                    operationID: operation?.operationID ?? "remote-\(UUID().uuidString)",
                    entityID: annotations[index].id.uuidString,
                    entityType: "annotation",
                    message: "Mac 已删除此批注；iPad 内容已保留，未静默覆盖。",
                    clientPayloadJSON: operation?.payloadJSON ?? "",
                    serverRecordJSON: #"{"deleted":true}"#
                )
            }
        }
        persist()
    }

    func pendingOperationBatch(deviceID: String, limit: Int = 50) -> [[String: Any]] {
        let now = Date()
        var result: [[String: Any]] = []
        var changed = false
        for index in pendingOperations.indices {
            guard result.count < limit else { break }
            let state = pendingOperations[index].syncState ?? "pending"
            guard state.isActionableSyncState,
                  pendingOperations[index].action != "import",
                  pendingOperations[index].nextAttemptAt.map({ $0 <= now }) ?? true,
                  let envelope = materializeOperation(at: index, deviceID: deviceID) else {
                continue
            }
            result.append(envelope)
            changed = true
        }
        if changed {
            persist()
        }
        return result
    }

    func markOperationReceipt(_ item: [String: Any]) {
        let operationID = item["operation_id"] as? String ?? ""
        guard let operationIndex = pendingOperations.firstIndex(where: {
            $0.operationID == operationID
        }) else { return }
        let operation = pendingOperations[operationIndex]
        let receiptJSON = Self.jsonString(item)
        let ok = item["ok"] as? Bool ?? false
        let result = item["result"] as? [String: Any] ?? [:]
        if ok {
            if operation.entityType == "annotation",
               let annotationID = UUID(uuidString: operation.entityID),
               let annotationIndex = annotations.firstIndex(where: { $0.id == annotationID }) {
                if operation.operationType == "audio_note_created",
                   let expectedHash = annotations[annotationIndex].audioHash {
                    let audioNote = result["audio_note"] as? [String: Any] ?? [:]
                    let receiptHash = (
                        audioNote["audio_hash"] as? String ?? ""
                    ).lowercased()
                    guard receiptHash == expectedHash.lowercased() else {
                        pendingOperations[operationIndex].syncState = "permanent_failed"
                        pendingOperations[operationIndex].lastError =
                            "语音备注回执哈希不一致；iPad 原音已保留"
                        annotations[annotationIndex].syncState = "conflict"
                        persist()
                        return
                    }
                }
                if operation.action == "delete" {
                    annotations.remove(at: annotationIndex)
                } else {
                    let serverID = result["id"] as? String
                    annotations[annotationIndex].serverID =
                        serverID ?? annotations[annotationIndex].serverID
                    annotations[annotationIndex].serverVersion =
                        Self.intValue(result["server_version"])
                            ?? annotations[annotationIndex].serverVersion
                    annotations[annotationIndex].syncState = "synced"
                }
            }
            pendingOperations.remove(at: operationIndex)
            persist()
            return
        }

        let conflict = item["conflict"] as? Bool ?? false
        let retryable = item["retryable"] as? Bool ?? false
        let message = item["error"] as? String ?? "同步未完成"
        pendingOperations[operationIndex].receiptJSON = receiptJSON
        pendingOperations[operationIndex].lastError = message
        if conflict {
            pendingOperations[operationIndex].syncState = "conflict"
            appendConflict(
                operationID: operationID,
                entityID: operation.entityID,
                entityType: operation.entityType,
                message: message,
                clientPayloadJSON: Self.jsonString(item["client_payload"] ?? [:]),
                serverRecordJSON: Self.jsonString(item["server_record"] ?? [:])
            )
        } else if retryable {
            pendingOperations[operationIndex].syncState = "retryable"
            pendingOperations[operationIndex].attemptCount += 1
            let exponent = min(pendingOperations[operationIndex].attemptCount, 8)
            pendingOperations[operationIndex].nextAttemptAt = Date().addingTimeInterval(
                min(21_600.0, 30.0 * pow(2.0, Double(exponent)))
            )
        } else {
            pendingOperations[operationIndex].syncState = "permanent_failed"
        }
        persist()
    }

    func acceptFullBaseline(sequence: Int64) {
        syncSequence = max(syncSequence, sequence)
        hasFullBaseline = true
        persist()
    }

    func acceptChangePage(sequence: Int64) {
        syncSequence = max(syncSequence, sequence)
        persist()
    }

    private func materializeOperation(
        at index: Int,
        deviceID: String
    ) -> [String: Any]? {
        let operation = pendingOperations[index]
        if let operationType = operation.operationType,
           let payloadJSON = operation.payloadJSON,
           let payload = Self.jsonObject(payloadJSON) {
            return operationEnvelope(
                operation: operation,
                operationType: operationType,
                bookServerID: operation.bookServerID,
                payload: payload,
                deviceID: deviceID
            )
        }

        if operation.entityType == "position",
           let bookID = UUID(uuidString: operation.entityID),
           let book = books.first(where: { $0.id == bookID }),
           let serverBookID = book.serverID {
            let locator = Self.jsonObject(book.lastLocatorJSON ?? "") ?? [:]
            let chapterLocator = locator["href"] as? String
                ?? book.lastLocatorJSON
                ?? "local:\(book.id.uuidString)"
            let payload: [String: Any] = [
                "book_id": serverBookID,
                "chapter_locator": chapterLocator,
                "page_index": 0,
                "total_pages": 1,
                "page_ratio": book.progress,
                "locator": locator,
            ]
            return freezeOperation(
                at: index,
                operationType: "reading_position_updated",
                bookServerID: serverBookID,
                payload: payload,
                baseServerVersion: nil,
                deviceID: deviceID
            )
        }

        if operation.entityType == "annotation",
           let annotationID = UUID(uuidString: operation.entityID),
           let annotation = annotations.first(where: { $0.id == annotationID }),
           let book = books.first(where: { $0.id == annotation.bookID }),
           let serverBookID = book.serverID {
            if operation.action == "delete",
               let serverAnnotationID = annotation.serverID {
                let payload: [String: Any] = [
                    "book_id": serverBookID,
                    "annotation_id": serverAnnotationID,
                ]
                return freezeOperation(
                    at: index,
                    operationType: "annotation_deleted",
                    bookServerID: serverBookID,
                    payload: payload,
                    baseServerVersion: annotation.serverVersion,
                    deviceID: deviceID
                )
            }
            if annotation.kind == .voiceNote {
                return nil
            }
            let locator = Self.jsonObject(annotation.locatorJSON) ?? [:]
            let chapterLocator = locator["href"] as? String
                ?? (annotation.locatorJSON.isEmpty
                    ? "local:\(annotation.id.uuidString)"
                    : annotation.locatorJSON)
            var payload: [String: Any] = [
                "book_id": serverBookID,
                "chapter_locator": chapterLocator,
                "source_text": annotation.excerpt,
                "note_text": annotation.note,
                "kind": annotation.serverKind,
                "color": annotation.kind == .highlight ? "red" : "",
                "range_locator": locator,
            ]
            let operationType: String
            let baseVersion: Int?
            if let serverAnnotationID = annotation.serverID {
                operationType = "annotation_updated"
                payload["annotation_id"] = serverAnnotationID
                baseVersion = annotation.serverVersion
            } else {
                switch annotation.kind {
                case .highlight:
                    operationType = "red_highlight_created"
                case .bookmark:
                    operationType = "annotation_created"
                case .note, .voiceNote:
                    operationType = "note_created"
                }
                baseVersion = nil
            }
            return freezeOperation(
                at: index,
                operationType: operationType,
                bookServerID: serverBookID,
                payload: payload,
                baseServerVersion: baseVersion,
                deviceID: deviceID
            )
        }

        if operation.entityType == "book",
           let bookID = UUID(uuidString: operation.entityID),
           let book = books.first(where: { $0.id == bookID }),
           let serverBookID = book.serverID {
            let payload: [String: Any] = [
                "book_id": serverBookID,
                "favorite": book.isFavorite,
                "custom_category": book.folderName ?? "",
            ]
            return freezeOperation(
                at: index,
                operationType: "book_organization_updated",
                bookServerID: serverBookID,
                payload: payload,
                baseServerVersion: nil,
                deviceID: deviceID
            )
        }
        return nil
    }

    private func freezeOperation(
        at index: Int,
        operationType: String,
        bookServerID: String?,
        payload: [String: Any],
        baseServerVersion: Int?,
        deviceID: String
    ) -> [String: Any] {
        pendingOperations[index].operationType = operationType
        pendingOperations[index].bookServerID = bookServerID
        pendingOperations[index].payloadJSON = Self.jsonString(payload)
        pendingOperations[index].baseServerVersion = baseServerVersion
        pendingOperations[index].syncState = "pending"
        return operationEnvelope(
            operation: pendingOperations[index],
            operationType: operationType,
            bookServerID: bookServerID,
            payload: payload,
            deviceID: deviceID
        )
    }

    private func operationEnvelope(
        operation: PendingMobileOperation,
        operationType: String,
        bookServerID: String?,
        payload: [String: Any],
        deviceID: String
    ) -> [String: Any] {
        var envelope: [String: Any] = [
            "operation_id": operation.operationID,
            "device_id": deviceID,
            "operation_type": operationType,
            "payload": payload,
            "created_at": ISO8601DateFormatter().string(from: operation.createdAt),
            "updated_at": ISO8601DateFormatter().string(from: operation.createdAt),
        ]
        if let bookServerID {
            envelope["book_id"] = bookServerID
        }
        if let version = operation.baseServerVersion {
            envelope["base_server_version"] = String(version)
        }
        return envelope
    }

    private func appendConflict(
        operationID: String,
        entityID: String,
        entityType: String,
        message: String,
        clientPayloadJSON: String,
        serverRecordJSON: String
    ) {
        guard !syncConflicts.contains(where: { $0.operationID == operationID }) else { return }
        syncConflicts.append(
            ClickSyncConflict(
                id: UUID(),
                operationID: operationID,
                entityID: entityID,
                entityType: entityType,
                message: message,
                clientPayloadJSON: clientPayloadJSON,
                serverRecordJSON: serverRecordJSON,
                createdAt: Date(),
                resolvedAt: nil
            )
        )
    }

    private func sortBooks() {
        books.sort {
            ($0.lastOpenedAt ?? $0.importedAt) > ($1.lastOpenedAt ?? $1.importedAt)
        }
    }

    private nonisolated static func sha256(of url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hasher = SHA256()
        while true {
            guard let data = try handle.read(upToCount: 1_048_576),
                  !data.isEmpty else {
                break
            }
            hasher.update(data: data)
        }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }

    nonisolated static func fileSHA256(_ url: URL) throws -> String {
        try sha256(of: url)
    }

    private static func jsonObject(_ text: String) -> [String: Any]? {
        guard let data = text.data(using: .utf8),
              let value = try? JSONSerialization.jsonObject(with: data),
              let object = value as? [String: Any] else {
            return nil
        }
        return object
    }

    private static func jsonString(_ value: Any) -> String {
        guard JSONSerialization.isValidJSONObject(value),
              let data = try? JSONSerialization.data(
                withJSONObject: value,
                options: [.sortedKeys]
              ) else {
            return ""
        }
        return String(data: data, encoding: .utf8) ?? ""
    }

    private static func intValue(_ value: Any?) -> Int? {
        if let value = value as? Int { return value }
        if let value = value as? NSNumber { return value.intValue }
        if let value = value as? String { return Int(value) }
        return nil
    }
}

private extension Optional where Wrapped == Bool {
    var orFalse: Bool { self ?? false }
}

private extension String {
    var isActionableSyncState: Bool {
        self == "pending" || self == "retryable"
    }
}

private extension JSONEncoder {
    static var click: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        return encoder
    }
}

private extension JSONDecoder {
    static var click: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }
}
