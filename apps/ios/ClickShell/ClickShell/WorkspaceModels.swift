import Foundation

enum WorkspaceDestination: String, CaseIterable, Identifiable {
    case library
    case myContent
    case recording
    case hermes
    case settings

    var id: String { rawValue }

    var title: String {
        switch self {
        case .library:
            return "阅读"
        case .myContent:
            return "我的内容"
        case .recording:
            return "录音"
        case .hermes:
            return "Hermes"
        case .settings:
            return "设置"
        }
    }

    var symbol: String {
        switch self {
        case .library:
            return "books.vertical"
        case .myContent:
            return "text.book.closed"
        case .recording:
            return "waveform"
        case .hermes:
            return "message"
        case .settings:
            return "gearshape"
        }
    }
}

enum ClickBookFormat: String, Codable, CaseIterable {
    case epub
    case pdf
    case text

    var displayName: String {
        switch self {
        case .epub:
            return "EPUB"
        case .pdf:
            return "PDF"
        case .text:
            return "文本"
        }
    }

    var symbol: String {
        switch self {
        case .epub:
            return "book.closed"
        case .pdf:
            return "doc.richtext"
        case .text:
            return "doc.text"
        }
    }

    static func infer(from url: URL) -> ClickBookFormat? {
        switch url.pathExtension.lowercased() {
        case "epub":
            return .epub
        case "pdf":
            return .pdf
        case "txt", "md", "markdown":
            return .text
        default:
            return nil
        }
    }
}

struct ClickBook: Codable, Identifiable, Hashable {
    let id: UUID
    var title: String
    var author: String
    let format: ClickBookFormat
    let contentHash: String
    var localRelativePath: String
    let importedAt: Date
    var lastOpenedAt: Date?
    var progress: Double
    var lastLocatorJSON: String?
    var isFavorite: Bool
    var folderName: String?
    var serverID: String? = nil
    var serverVersion: Int? = nil
    var remoteSourcePath: String? = nil
    var remoteSourceHash: String? = nil
    var remoteSourceByteSize: Int64? = nil
    var serverRemoved: Bool? = nil
    var remoteDownloadState: String? = nil
    var remoteDownloadAttemptCount: Int? = nil
    var remoteDownloadNextAttemptAt: Date? = nil
    var remoteDownloadError: String? = nil
    var localCoverRelativePath: String? = nil
    var remoteCoverPath: String? = nil
    var remoteCoverState: String? = nil
    var remoteCoverNextAttemptAt: Date? = nil

    var displayTitle: String {
        Self.normalizedTitle(title)
    }

    var displayAuthor: String {
        let normalized = Self.normalizedAuthor(author)
        return normalized.isEmpty ? "未知作者" : normalized
    }

    var needsLocalMetadataRepair: Bool {
        !Self.hasUsefulTitle(title) || !Self.hasUsefulAuthor(author)
    }

    static func normalizedTitle(_ value: String) -> String {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return hasUsefulTitle(trimmed) ? trimmed : "未命名书籍"
    }

    static func normalizedAuthor(_ value: String) -> String {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return hasUsefulAuthor(trimmed) ? trimmed : ""
    }

    static func hasUsefulTitle(_ value: String) -> Bool {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        let lowercase = trimmed.lowercased()
        guard !trimmed.isEmpty,
              ![
                "book",
                "untitled",
                "unknown",
                "unknown book",
                "未命名",
                "未命名书籍",
              ].contains(lowercase) else {
            return false
        }
        let identifier = lowercase.hasPrefix("book_")
            ? String(lowercase.dropFirst(5))
            : lowercase.replacingOccurrences(of: "-", with: "")
        return identifier.count < 24 || !identifier.unicodeScalars.allSatisfy {
            (48...57).contains($0.value) || (97...102).contains($0.value)
        }
    }

    static func hasUsefulAuthor(_ value: String) -> Bool {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return false }
        return ![
            "unknown",
            "unknown author",
            "n/a",
            "none",
            "null",
            "未知",
            "未知作者",
        ].contains(trimmed.lowercased())
    }
}

struct ClickFolderPath: Hashable, Identifiable {
    static let maxDepth = 3
    static let separator = " / "
    static let root = ClickFolderPath(components: [])

    let components: [String]

    init(components: [String]) {
        self.components = Array(
            Self.normalizedComponents(components).prefix(Self.maxDepth)
        )
    }

    init(rawValue: String?) {
        let parts = (rawValue ?? "")
            .split(separator: "/", omittingEmptySubsequences: true)
            .map(String.init)
        self.init(components: parts)
    }

    var id: String { rawValue }
    var rawValue: String { components.joined(separator: Self.separator) }
    var displayValue: String { components.joined(separator: " › ") }
    var leafName: String { components.last ?? "书架" }
    var depth: Int { components.count }
    var isRoot: Bool { components.isEmpty }

    var parent: ClickFolderPath {
        guard !components.isEmpty else { return .root }
        return ClickFolderPath(components: Array(components.dropLast()))
    }

    func appending(_ component: String) -> ClickFolderPath? {
        guard depth < Self.maxDepth else { return nil }
        let normalized = Self.normalizedComponents([component])
        guard let leaf = normalized.first else { return nil }
        return ClickFolderPath(components: components + [leaf])
    }

    func prefix(_ depth: Int) -> ClickFolderPath {
        ClickFolderPath(components: Array(components.prefix(max(0, depth))))
    }

    func contains(_ candidate: ClickFolderPath) -> Bool {
        candidate.components.starts(with: components)
    }

    static func hasExcessDepth(_ rawValue: String) -> Bool {
        normalizedComponents(
            rawValue
                .split(separator: "/", omittingEmptySubsequences: true)
                .map(String.init)
        ).count > maxDepth
    }

    private static func normalizedComponents(_ components: [String]) -> [String] {
        components.compactMap { component in
            let trimmed = component
                .replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
                .trimmingCharacters(in: .whitespacesAndNewlines)
            return trimmed.isEmpty ? nil : trimmed
        }
    }
}

enum AnnotationKind: String, Codable {
    case highlight
    case note
    case voiceNote
    case bookmark
}

extension ClickAnnotation {
    var serverKind: String {
        switch kind {
        case .highlight:
            return "red_highlight"
        case .note:
            return "note"
        case .voiceNote:
            return "audio_note"
        case .bookmark:
            return "bookmark"
        }
    }
}

struct ClickAnnotation: Codable, Identifiable, Hashable {
    let id: UUID
    let bookID: UUID
    let kind: AnnotationKind
    var excerpt: String
    var note: String
    var locatorJSON: String
    let createdAt: Date
    var updatedAt: Date
    var syncState: String
    var serverID: String? = nil
    var serverVersion: Int? = nil
    var serverDeleted: Bool? = nil
    var audioRelativePath: String? = nil
    var audioHash: String? = nil
    var transcriptState: String? = nil
}

struct VoiceNoteUploadCandidate: Sendable {
    let operationID: String
    let annotationID: UUID
    let bookServerID: String
    let audioURL: URL
    let excerpt: String
    let note: String
    let locatorJSON: String
}

struct PendingMobileOperation: Codable, Identifiable, Hashable {
    let id: UUID
    let operationID: String
    let entityID: String
    let entityType: String
    let action: String
    let createdAt: Date
    var attemptCount: Int
    var lastError: String?
    var operationType: String? = nil
    var bookServerID: String? = nil
    var payloadJSON: String? = nil
    var baseServerVersion: Int? = nil
    var syncState: String? = nil
    var receiptJSON: String? = nil
    var nextAttemptAt: Date? = nil
}

struct ClickSyncConflict: Codable, Identifiable, Hashable {
    let id: UUID
    let operationID: String
    let entityID: String
    let entityType: String
    let message: String
    let clientPayloadJSON: String
    let serverRecordJSON: String
    let createdAt: Date
    var resolvedAt: Date?
}

struct LibrarySnapshot: Codable {
    var schemaVersion: Int
    var books: [ClickBook]
    var annotations: [ClickAnnotation]
    var pendingOperations: [PendingMobileOperation]
    var syncSequence: Int64? = nil
    var hasFullBaseline: Bool? = nil
    var conflicts: [ClickSyncConflict]? = nil

    static let empty = LibrarySnapshot(
        schemaVersion: 1,
        books: [],
        annotations: [],
        pendingOperations: []
    )
}
