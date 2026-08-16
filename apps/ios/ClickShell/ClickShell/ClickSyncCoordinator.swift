import Foundation

@MainActor
final class ClickSyncCoordinator: ObservableObject {
    @Published private(set) var isRunning = false
    @Published private(set) var statusMessage = ""
    @Published private(set) var lastCompletedAt: Date?

    private static let operationBatchLimit = 50
    private static let maximumOperationBatches = 10
    private static let changePageLimit = 100
    private static let maximumChangePages = 20
    private var queuedPreferredBookID: UUID?

    func run(
        workspace: WorkspaceStore,
        connection: ConnectionStore,
        recording: RecordingController,
        preferredBookID: UUID? = nil
    ) async {
        guard !isRunning else {
            if let preferredBookID {
                queuedPreferredBookID = preferredBookID
            }
            return
        }
        if preferredBookID == nil,
           recording.pendingCount == 0,
           let lastCompletedAt,
           Date().timeIntervalSince(lastCompletedAt) < 5 {
            return
        }
        isRunning = true
        statusMessage = "正在同步…"
        defer {
            isRunning = false
            if let queuedBookID = queuedPreferredBookID {
                queuedPreferredBookID = nil
                Task { @MainActor [weak self] in
                    await self?.run(
                        workspace: workspace,
                        connection: connection,
                        recording: recording,
                        preferredBookID: queuedBookID
                    )
                }
            }
        }

        if connection.connectedBaseURL == nil {
            await connection.restoreConnectionOrDiscover()
        }
        guard let baseURL = connection.connectedBaseURL,
              connection.hasAccessToken else {
            statusMessage = "当前离线"
            return
        }

        do {
            try await uploadOneLocalBook(
                workspace: workspace,
                connection: connection,
                baseURL: baseURL
            )
            try await uploadOneVoiceNoteOperation(
                workspace: workspace,
                connection: connection,
                baseURL: baseURL
            )
            try await uploadPendingOperations(
                workspace: workspace,
                connection: connection,
                baseURL: baseURL
            )
            if workspace.hasFullBaseline {
                try await pullChanges(
                    workspace: workspace,
                    connection: connection,
                    baseURL: baseURL
                )
            } else {
                try await pullFullBaseline(
                    workspace: workspace,
                    connection: connection,
                    baseURL: baseURL
                )
            }
            await downloadMissingCovers(
                workspace: workspace,
                connection: connection,
                baseURL: baseURL,
                preferredBookID: preferredBookID
            )
            try await downloadOneBook(
                workspace: workspace,
                connection: connection,
                baseURL: baseURL,
                preferredBookID: preferredBookID
            )
            try await uploadOneRecording(
                recording: recording,
                connection: connection,
                baseURL: baseURL
            )
            lastCompletedAt = Date()
            let pendingCount = workspace.pendingOperations.count + recording.pendingCount
            statusMessage = pendingCount == 0
                ? "同步完成"
                : "已同步；仍有 \(pendingCount) 项待处理"
        } catch let error as SyncFailure {
            statusMessage = error.userMessage
            if error.disconnectsCurrentOrigin {
                connection.resetConnection()
            }
        } catch {
            statusMessage = "同步暂未完成；下次进入前台会继续。"
        }
    }

    private func uploadOneVoiceNoteOperation(
        workspace: WorkspaceStore,
        connection: ConnectionStore,
        baseURL: URL
    ) async throws {
        guard let candidate = workspace.nextVoiceNoteUploadCandidate() else {
            return
        }
        let prepared: (String, String)
        do {
            prepared = try await Task.detached(priority: .utility) {
                let values = try candidate.audioURL.resourceValues(
                    forKeys: [.isRegularFileKey, .fileSizeKey]
                )
                guard values.isRegularFile == true,
                      let size = values.fileSize,
                      size > 0,
                      size <= 16 * 1_024 * 1_024 else {
                    throw SyncFailure.contract(
                        "语音备注原音无效或超过 16MB 上限"
                    )
                }
                let data = try Data(
                    contentsOf: candidate.audioURL,
                    options: [.mappedIfSafe]
                )
                return (
                    data.base64EncodedString(),
                    try WorkspaceStore.fileSHA256(candidate.audioURL)
                )
            }.value
        } catch let error as SyncFailure {
            workspace.markOperationPreparationFailure(
                operationID: candidate.operationID,
                message: error.userMessage,
                retryable: error.isRetryable
            )
            if error.isRetryable {
                throw error
            }
            return
        } catch {
            workspace.markOperationPreparationFailure(
                operationID: candidate.operationID,
                message: "语音备注原音暂时无法读取",
                retryable: true
            )
            throw SyncFailure.transport
        }
        guard let operation = workspace.voiceNoteOperationEnvelope(
            candidate: candidate,
            deviceID: connection.deviceID,
            audioBase64: prepared.0,
            audioHash: prepared.1
        ) else {
            return
        }
        let body: [String: Any] = [
            "device_id": connection.deviceID,
            "operations": [operation],
        ]
        var request = connection.authorizedRequest(
            path: "/v1/android/sync/operations",
            baseURL: baseURL,
            method: "POST"
        )
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 180
        do {
            request.httpBody = try JSONSerialization.data(
                withJSONObject: body,
                options: [.sortedKeys]
            )
            let payload = try await requestJSON(
                request,
                maximumBytes: 2 * 1_024 * 1_024
            )
            guard
                let results = payload["results"] as? [[String: Any]],
                results.count == 1,
                results[0]["operation_id"] as? String == candidate.operationID
            else {
                throw SyncFailure.contract("语音备注同步缺少精确回执")
            }
            workspace.markOperationReceipt(results[0])
        } catch let error as SyncFailure {
            if error.isRetryable || error.disconnectsCurrentOrigin {
                workspace.markOperationTransportFailure(
                    operationIDs: [candidate.operationID],
                    message: "语音备注网络中断；iPad 原音已保留"
                )
            } else {
                workspace.markOperationPreparationFailure(
                    operationID: candidate.operationID,
                    message: error.userMessage,
                    retryable: false
                )
            }
            throw error
        } catch {
            workspace.markOperationTransportFailure(
                operationIDs: [candidate.operationID],
                message: "语音备注网络中断；iPad 原音已保留"
            )
            throw SyncFailure.transport
        }
    }

    private func uploadOneRecording(
        recording: RecordingController,
        connection: ConnectionStore,
        baseURL: URL
    ) async throws {
        guard let capture = recording.nextPendingCapture() else { return }
        let audioHash: String
        do {
            audioHash = try await Task.detached(priority: .utility) {
                try WorkspaceStore.fileSHA256(capture.url)
            }.value.lowercased()
        } catch {
            recording.markUploadFailure(
                captureID: capture.id,
                message: "录音文件无法读取",
                retryable: false
            )
            return
        }
        guard audioHash.range(
            of: #"^[0-9a-f]{64}$"#,
            options: .regularExpression
        ) != nil else {
            recording.markUploadFailure(
                captureID: capture.id,
                message: "录音哈希无效",
                retryable: false
            )
            return
        }
        recording.markHash(captureID: capture.id, audioHash: audioHash)
        recording.markUploading(captureID: capture.id)

        let bodyURL: URL
        let deviceID = connection.deviceID
        do {
            bodyURL = try await Task.detached(priority: .utility) {
                try Self.makeRecordingUploadBody(
                    capture: capture,
                    deviceID: deviceID
                )
            }.value
        } catch {
            recording.markUploadFailure(
                captureID: capture.id,
                message: "录音上传包无法生成",
                retryable: true
            )
            throw SyncFailure.transport
        }
        defer { try? FileManager.default.removeItem(at: bodyURL) }

        var request = connection.authorizedRequest(
            path: "/v1/recordings",
            baseURL: baseURL,
            method: "POST"
        )
        request.timeoutInterval = 180
        request.setValue(
            "application/json; charset=utf-8",
            forHTTPHeaderField: "Content-Type"
        )
        if let size = try? bodyURL.resourceValues(forKeys: [.fileSizeKey]).fileSize {
            request.setValue(String(size), forHTTPHeaderField: "Content-Length")
        }

        do {
            let (data, response) = try await URLSession.shared.upload(
                for: request,
                fromFile: bodyURL
            )
            let payload = try Self.validatedJSON(
                data: data,
                response: response,
                maximumBytes: 2 * 1_024 * 1_024
            )
            let receipt = payload["recording"] as? [String: Any] ?? [:]
            guard payload["ok"] as? Bool == true,
                  let recordingID = receipt["recording_id"] as? String,
                  !recordingID.isEmpty,
                  let receiptHash = receipt["audio_hash"] as? String,
                  receiptHash.lowercased() == audioHash else {
                recording.markUploadFailure(
                    captureID: capture.id,
                    message: "录音同步回执不完整或哈希不一致",
                    retryable: false
                )
                return
            }
            recording.markSynced(
                captureID: capture.id,
                serverRecordingID: recordingID,
                audioHash: receiptHash
            )
        } catch let error as SyncFailure {
            recording.markUploadFailure(
                captureID: capture.id,
                message: error.userMessage,
                retryable: error.isRetryable || error.disconnectsCurrentOrigin
            )
            if error.isRetryable || error.disconnectsCurrentOrigin {
                throw error
            }
        } catch {
            recording.markUploadFailure(
                captureID: capture.id,
                message: "录音上传中断",
                retryable: true
            )
            throw SyncFailure.transport
        }
    }

    private nonisolated static func makeRecordingUploadBody(
        capture: LocalVoiceCapture,
        deviceID: String
    ) throws -> URL {
        let values = try capture.url.resourceValues(
            forKeys: [.isRegularFileKey, .fileSizeKey]
        )
        guard values.isRegularFile == true, (values.fileSize ?? 0) > 0 else {
            throw SyncFailure.contract("录音文件不存在")
        }
        let metadata: [String: Any] = [
            "mime_type": "audio/mp4",
            "duration_seconds": capture.duration,
            "client_capture_id": capture.id,
            "device_id": deviceID,
            "source": "click_ipad_native_tingle",
            "source_app": "Click",
            "source_feature": "Tingle",
            "durability": "durable",
            "contexts": [[
                "schema": "click.tingle.local_metadata.v1",
                "title": "",
                "note": "",
            ]],
        ]
        let metadataData = try JSONSerialization.data(
            withJSONObject: metadata,
            options: [.sortedKeys]
        )
        guard metadataData.first == 123 else {
            throw SyncFailure.contract("录音元数据无效")
        }

        let destination = FileManager.default.temporaryDirectory
            .appendingPathComponent("click-recording-\(UUID().uuidString).json")
        guard FileManager.default.createFile(
            atPath: destination.path,
            contents: nil
        ) else {
            throw SyncFailure.transport
        }
        do {
            let output = try FileHandle(forWritingTo: destination)
            defer { try? output.close() }
            try output.write(contentsOf: Data("{\"audio_base64\":\"".utf8))
            let input = try FileHandle(forReadingFrom: capture.url)
            defer { try? input.close() }
            var pending = Data()
            while let chunk = try input.read(upToCount: 65_535), !chunk.isEmpty {
                pending.append(chunk)
                let completeCount = (pending.count / 3) * 3
                guard completeCount > 0 else { continue }
                try output.write(
                    contentsOf: Data(pending.prefix(completeCount)).base64EncodedData()
                )
                pending.removeFirst(completeCount)
            }
            if !pending.isEmpty {
                try output.write(contentsOf: pending.base64EncodedData())
            }
            try output.write(contentsOf: Data("\",".utf8))
            try output.write(contentsOf: metadataData.dropFirst())
            return destination
        } catch {
            try? FileManager.default.removeItem(at: destination)
            throw error
        }
    }

    private func uploadOneLocalBook(
        workspace: WorkspaceStore,
        connection: ConnectionStore,
        baseURL: URL
    ) async throws {
        guard let item = workspace.localImportOperations().first else { return }
        let sourceURL = workspace.fileURL(for: item.book)
        let values = try sourceURL.resourceValues(
            forKeys: [.fileSizeKey, .isRegularFileKey]
        )
        guard values.isRegularFile == true,
              let fileSize = values.fileSize,
              fileSize > 0,
              item.book.format == .epub || item.book.format == .pdf else {
            workspace.markImportFailure(
                operationID: item.operation.operationID,
                message: "本地原书不存在或格式不支持",
                retryable: false
            )
            return
        }
        let expectedHash = item.book.contentHash.lowercased()
        guard expectedHash.range(
            of: #"^[0-9a-f]{64}$"#,
            options: .regularExpression
        ) != nil else {
            workspace.markImportFailure(
                operationID: item.operation.operationID,
                message: "本地原书哈希无效",
                retryable: false
            )
            return
        }

        var request = connection.authorizedRequest(
            path: "/v1/android/imports/\(expectedHash)",
            baseURL: baseURL,
            method: "PUT"
        )
        request.timeoutInterval = 120
        request.setValue(
            "application/octet-stream",
            forHTTPHeaderField: "Content-Type"
        )
        request.setValue(String(fileSize), forHTTPHeaderField: "Content-Length")
        request.setValue(
            Self.percentEncodedHeader(sourceURL.lastPathComponent),
            forHTTPHeaderField: "X-Click-Filename"
        )
        request.setValue(
            item.book.format.rawValue,
            forHTTPHeaderField: "X-Click-Source-Kind"
        )
        request.setValue(
            String(fileSize),
            forHTTPHeaderField: "X-Click-Byte-Size"
        )

        do {
            let (data, response) = try await URLSession.shared.upload(
                for: request,
                fromFile: sourceURL
            )
            let payload = try Self.validatedJSON(
                data: data,
                response: response,
                maximumBytes: 262_144
            )
            guard payload["ok"] as? Bool == true,
                  let serverID = payload["book_id"] as? String,
                  !serverID.isEmpty,
                  let receiptHash = payload["file_hash"] as? String,
                  receiptHash.lowercased() == expectedHash,
                  Self.int64Value(payload["byte_size"]) == Int64(fileSize) else {
                throw SyncFailure.contract("原书导入回执不完整")
            }
            workspace.markImportSucceeded(
                operationID: item.operation.operationID,
                localBookID: item.book.id,
                serverID: serverID,
                sourceHash: receiptHash.lowercased(),
                sourceByteSize: Int64(fileSize)
            )
        } catch let error as SyncFailure {
            workspace.markImportFailure(
                operationID: item.operation.operationID,
                message: error.userMessage,
                retryable: error.isRetryable || error.disconnectsCurrentOrigin
            )
            throw error
        } catch {
            workspace.markImportFailure(
                operationID: item.operation.operationID,
                message: "原书上传中断",
                retryable: true
            )
            throw SyncFailure.transport
        }
    }

    private func uploadPendingOperations(
        workspace: WorkspaceStore,
        connection: ConnectionStore,
        baseURL: URL
    ) async throws {
        for _ in 0..<Self.maximumOperationBatches {
            let operations = workspace.pendingOperationBatch(
                deviceID: connection.deviceID,
                limit: Self.operationBatchLimit
            )
            guard !operations.isEmpty else { return }
            let expectedIDs = Set(
                operations.compactMap { $0["operation_id"] as? String }
            )
            let body: [String: Any] = [
                "device_id": connection.deviceID,
                "operations": operations,
            ]
            var request = connection.authorizedRequest(
                path: "/v1/android/sync/operations",
                baseURL: baseURL,
                method: "POST"
            )
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(
                withJSONObject: body,
                options: [.sortedKeys]
            )

            do {
                let payload = try await requestJSON(
                    request,
                    maximumBytes: 4 * 1_024 * 1_024
                )
                guard let results = payload["results"] as? [[String: Any]] else {
                    throw SyncFailure.contract("操作同步缺少回执")
                }
                let actualIDs = Set(
                    results.compactMap { $0["operation_id"] as? String }
                )
                guard results.count == operations.count,
                      actualIDs.count == results.count,
                      actualIDs == expectedIDs else {
                    throw SyncFailure.contract("操作同步回执不完整")
                }
                results.forEach(workspace.markOperationReceipt)
                if results.contains(where: {
                    $0["retryable"] as? Bool == true
                }) {
                    return
                }
            } catch {
                workspace.markOperationTransportFailure(
                    operationIDs: expectedIDs,
                    message: "网络中断；保留在本机等待下次重试"
                )
                throw error
            }
        }
    }

    private func pullFullBaseline(
        workspace: WorkspaceStore,
        connection: ConnectionStore,
        baseURL: URL
    ) async throws {
        let request = connection.authorizedRequest(
            path: "/v1/android/sync/full?include_chapters=false",
            baseURL: baseURL
        )
        let payload = try await requestJSON(
            request,
            maximumBytes: 32 * 1_024 * 1_024
        )
        guard payload["ok"] as? Bool == true,
              let sequence = Self.int64Value(payload["watermark_sequence"]) else {
            throw SyncFailure.contract("全量同步缺少顺序游标")
        }
        try applyPayload(payload, to: workspace)
        workspace.acceptFullBaseline(sequence: sequence)
    }

    private func pullChanges(
        workspace: WorkspaceStore,
        connection: ConnectionStore,
        baseURL: URL
    ) async throws {
        var sequence = workspace.syncSequence
        for _ in 0..<Self.maximumChangePages {
            let path = "/v1/android/sync/changes?after_sequence=\(sequence)&limit=\(Self.changePageLimit)"
            let request = connection.authorizedRequest(path: path, baseURL: baseURL)
            let payload = try await requestJSON(
                request,
                maximumBytes: 16 * 1_024 * 1_024
            )
            guard payload["ok"] as? Bool == true,
                  let nextSequence = Self.int64Value(payload["next_sequence"]),
                  nextSequence >= sequence else {
                throw SyncFailure.contract("增量同步顺序游标无效")
            }
            try applyPayload(payload, to: workspace)
            workspace.acceptChangePage(sequence: nextSequence)
            let hasMore = payload["has_more"] as? Bool ?? false
            guard hasMore else { return }
            guard nextSequence > sequence else {
                throw SyncFailure.contract("增量同步分页没有前进")
            }
            sequence = nextSequence
        }
        throw SyncFailure.retryable("本轮同步达到有界分页上限")
    }

    private func applyPayload(
        _ payload: [String: Any],
        to workspace: WorkspaceStore
    ) throws {
        let rawBooks = payload["books"] as? [[String: Any]] ?? []
        for raw in rawBooks {
            let book = raw["book"] as? [String: Any] ?? raw
            let source = raw["source"] as? [String: Any] ?? [
                "kind": book["source_kind"] ?? raw["source_kind"] ?? "",
                "url": book["source_url"] ?? raw["source_url"] ?? "",
                "file_hash": book["source_hash"] ?? raw["source_hash"] ?? "",
                "byte_size": book["source_byte_size"] ?? raw["source_byte_size"] ?? -1,
            ]
            guard let serverID = book["id"] as? String,
                  !serverID.isEmpty,
                  let sourceKind = (source["kind"] as? String)
                    ?? (book["source_kind"] as? String),
                  let format = ClickBookFormat(rawValue: sourceKind.lowercased()),
                  format == .epub || format == .pdf else {
                continue
            }
            let organization = raw["organization"] as? [String: Any]
                ?? book["organization"] as? [String: Any]
                ?? [:]
            let sourceHash = (
                source["file_hash"] as? String
                    ?? book["file_hash"] as? String
                    ?? book["book_hash"] as? String
                    ?? ""
            ).lowercased()
            let localBookID = workspace.upsertRemoteBook(
                serverID: serverID,
                title: book["title"] as? String ?? "未命名书籍",
                author: book["author"] as? String ?? "",
                format: format,
                contentHash: (
                    book["book_hash"] as? String
                        ?? book["file_hash"] as? String
                        ?? sourceHash
                ).lowercased(),
                sourcePath: source["url"] as? String
                    ?? book["source_url"] as? String
                    ?? "",
                sourceHash: sourceHash,
                sourceByteSize: Self.int64Value(source["byte_size"])
                    ?? Self.int64Value(book["source_byte_size"])
                    ?? -1,
                serverVersion: Self.intValue(book["server_version"]) ?? 1,
                favorite: organization["favorite"] as? Bool ?? false,
                folderName: (organization["custom_category"] as? String)
                    ?? (organization["category"] as? String),
                coverPath: (raw["cover"] as? [String: Any])?["url"] as? String
                    ?? (book["cover"] as? [String: Any])?["url"] as? String
            )

            if let position = raw["position"] as? [String: Any] {
                applyPosition(position, serverBookID: serverID, to: workspace)
            }
            let annotations = raw["annotations"] as? [[String: Any]] ?? []
            for annotation in annotations {
                applyAnnotation(
                    annotation,
                    fallbackServerBookID: serverID,
                    to: workspace
                )
            }
            _ = localBookID
        }

        let positions = payload["positions"] as? [[String: Any]] ?? []
        for position in positions {
            guard let serverBookID = position["book_id"] as? String else { continue }
            applyPosition(position, serverBookID: serverBookID, to: workspace)
        }

        let annotations = payload["annotations"] as? [[String: Any]] ?? []
        for annotation in annotations {
            applyAnnotation(annotation, fallbackServerBookID: "", to: workspace)
        }

        workspace.markRemoteBooksRemoved(
            serverIDs: payload["removed_book_ids"] as? [String] ?? []
        )
        workspace.markRemoteAnnotationsRemoved(
            serverIDs: payload["removed_annotation_ids"] as? [String] ?? []
        )
    }

    private func applyPosition(
        _ position: [String: Any],
        serverBookID: String,
        to workspace: WorkspaceStore
    ) {
        let locator = position["locator"] as? [String: Any] ?? [:]
        let locatorJSON = Self.jsonString(locator)
        let progress = Self.doubleValue(position["page_ratio"]) ?? 0
        workspace.applyRemotePosition(
            serverBookID: serverBookID,
            progress: progress,
            locatorJSON: locatorJSON
        )
    }

    private func applyAnnotation(
        _ annotation: [String: Any],
        fallbackServerBookID: String,
        to workspace: WorkspaceStore
    ) {
        let serverID = annotation["id"] as? String ?? ""
        let serverBookID = annotation["book_id"] as? String
            ?? fallbackServerBookID
        guard !serverID.isEmpty, !serverBookID.isEmpty else { return }
        let rawKind = (annotation["kind"] as? String ?? "").lowercased()
        let kind: AnnotationKind
        if rawKind.contains("highlight") || rawKind == "red" {
            kind = .highlight
        } else if rawKind == "bookmark" {
            kind = .bookmark
        } else if rawKind.contains("audio") || rawKind.contains("voice") {
            kind = .voiceNote
        } else {
            kind = .note
        }
        let rangeLocator = annotation["range_locator"] as? [String: Any]
            ?? annotation["locator"] as? [String: Any]
            ?? [:]
        let fallbackLocator = annotation["chapter_locator"] as? String ?? ""
        let locatorJSON = rangeLocator.isEmpty
            ? fallbackLocator
            : Self.jsonString(rangeLocator)
        workspace.applyRemoteAnnotation(
            serverID: serverID,
            serverBookID: serverBookID,
            kind: kind,
            excerpt: annotation["source_text"] as? String ?? "",
            note: annotation["note_text"] as? String ?? "",
            locatorJSON: locatorJSON,
            createdAt: Self.dateValue(annotation["created_at"]) ?? Date(),
            updatedAt: Self.dateValue(annotation["updated_at"]) ?? Date(),
            serverVersion: Self.intValue(annotation["server_version"]) ?? 1
        )
    }

    private func downloadOneBook(
        workspace: WorkspaceStore,
        connection: ConnectionStore,
        baseURL: URL,
        preferredBookID: UUID?
    ) async throws {
        guard let book = workspace.nextRemoteBookNeedingDownload(
            preferredBookID: preferredBookID
        ),
        let sourcePath = book.remoteSourcePath,
        let expectedHash = book.remoteSourceHash?.lowercased(),
        expectedHash.range(
            of: #"^[0-9a-f]{64}$"#,
            options: .regularExpression
        ) != nil,
        let expectedBytes = book.remoteSourceByteSize,
        expectedBytes > 0 else {
            return
        }
        do {
            var request = connection.authorizedRequest(
                path: sourcePath,
                baseURL: baseURL
            )
            request.timeoutInterval = 120
            let (temporaryURL, response) = try await URLSession.shared.download(
                for: request
            )
            guard let http = response as? HTTPURLResponse else {
                throw SyncFailure.transport
            }
            guard (200..<300).contains(http.statusCode) else {
                throw SyncFailure.http(http.statusCode)
            }
            let verification = try await Task.detached(priority: .utility) {
                let values = try temporaryURL.resourceValues(
                    forKeys: [.fileSizeKey, .isRegularFileKey]
                )
                guard values.isRegularFile == true,
                      let fileSize = values.fileSize else {
                    throw SyncFailure.contract("下载的原书不是普通文件")
                }
                let digest = try WorkspaceStore.fileSHA256(temporaryURL)
                return (Int64(fileSize), digest)
            }.value
            guard verification.0 == expectedBytes,
                  verification.1.lowercased() == expectedHash else {
                throw SyncFailure.contract("下载的原书校验失败")
            }
            try workspace.installDownloadedBook(
                localBookID: book.id,
                temporaryURL: temporaryURL,
                suggestedFilename: "source.\(book.format.rawValue)"
            )
            workspace.consumeDownloadRequest(book.id)
            if preferredBookID == book.id,
               let refreshed = workspace.books.first(where: { $0.id == book.id }) {
                workspace.select(refreshed)
            }
        } catch let error as SyncFailure {
            workspace.markRemoteDownloadFailure(
                bookID: book.id,
                message: error.userMessage,
                retryable: error.isRetryable || error.disconnectsCurrentOrigin
            )
            throw error
        } catch {
            workspace.markRemoteDownloadFailure(
                bookID: book.id,
                message: "原书下载中断",
                retryable: true
            )
            throw SyncFailure.transport
        }
    }

    private func downloadMissingCovers(
        workspace: WorkspaceStore,
        connection: ConnectionStore,
        baseURL: URL,
        preferredBookID: UUID?
    ) async {
        // Keep foreground sync bounded. The open book goes first; the remaining
        // slots gradually fill the offline shelf without a resident worker.
        for index in 0..<4 {
            guard let book = workspace.nextRemoteBookNeedingCover(
                preferredBookID: index == 0 ? preferredBookID : nil
            ), let coverPath = workspace.remoteCoverPath(for: book) else {
                return
            }
            do {
                var request = connection.authorizedRequest(
                    path: coverPath,
                    baseURL: baseURL
                )
                request.timeoutInterval = 20
                request.setValue("image/*", forHTTPHeaderField: "Accept")
                let (data, response) = try await URLSession.shared.data(for: request)
                guard let http = response as? HTTPURLResponse else {
                    throw SyncFailure.transport
                }
                guard (200..<300).contains(http.statusCode) else {
                    throw SyncFailure.http(http.statusCode)
                }
                guard data.count >= 128, data.count <= 12 * 1_024 * 1_024 else {
                    throw SyncFailure.contract("封面文件大小无效")
                }
                guard WorkspaceStore.isDecodableCoverData(data) else {
                    workspace.markRemoteCoverFailure(bookID: book.id, retryable: false)
                    continue
                }
                try workspace.installDownloadedCover(
                    localBookID: book.id,
                    data: data
                )
            } catch let error as SyncFailure {
                workspace.markRemoteCoverFailure(
                    bookID: book.id,
                    retryable: error.isRetryable || error.disconnectsCurrentOrigin
                )
            } catch {
                workspace.markRemoteCoverFailure(bookID: book.id, retryable: true)
            }
        }
    }

    private func requestJSON(
        _ request: URLRequest,
        maximumBytes: Int
    ) async throws -> [String: Any] {
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            return try Self.validatedJSON(
                data: data,
                response: response,
                maximumBytes: maximumBytes
            )
        } catch let error as SyncFailure {
            throw error
        } catch {
            throw SyncFailure.transport
        }
    }

    private nonisolated static func validatedJSON(
        data: Data,
        response: URLResponse,
        maximumBytes: Int
    ) throws -> [String: Any] {
        guard let http = response as? HTTPURLResponse else {
            throw SyncFailure.transport
        }
        guard (200..<300).contains(http.statusCode) else {
            throw SyncFailure.http(http.statusCode)
        }
        guard data.count <= maximumBytes,
              let payload = try JSONSerialization.jsonObject(with: data)
                as? [String: Any] else {
            throw SyncFailure.contract("同步响应无效或过大")
        }
        return payload
    }

    private nonisolated static func percentEncodedHeader(_ value: String) -> String {
        var allowed = CharacterSet.urlPathAllowed
        allowed.remove(charactersIn: "/\\")
        return value.addingPercentEncoding(withAllowedCharacters: allowed)
            ?? "book.epub"
    }

    private nonisolated static func jsonString(_ value: Any) -> String {
        guard JSONSerialization.isValidJSONObject(value),
              let data = try? JSONSerialization.data(
                withJSONObject: value,
                options: [.sortedKeys]
              ) else {
            return ""
        }
        return String(data: data, encoding: .utf8) ?? ""
    }

    private nonisolated static func int64Value(_ value: Any?) -> Int64? {
        if let value = value as? Int64 { return value }
        if let value = value as? Int { return Int64(value) }
        if let value = value as? NSNumber { return value.int64Value }
        if let value = value as? String { return Int64(value) }
        return nil
    }

    private nonisolated static func intValue(_ value: Any?) -> Int? {
        int64Value(value).flatMap(Int.init)
    }

    private nonisolated static func doubleValue(_ value: Any?) -> Double? {
        if let value = value as? Double { return value }
        if let value = value as? NSNumber { return value.doubleValue }
        if let value = value as? String { return Double(value) }
        return nil
    }

    private nonisolated static func dateValue(_ value: Any?) -> Date? {
        guard let text = value as? String else { return nil }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.date(from: text)
            ?? ISO8601DateFormatter().date(from: text)
    }
}

private enum SyncFailure: Error {
    case transport
    case http(Int)
    case invalidContract(String)
    case boundedRetry(String)

    static func contract(_ message: String) -> SyncFailure {
        .invalidContract(message)
    }

    static func retryable(_ message: String) -> SyncFailure {
        .boundedRetry(message)
    }

    var isRetryable: Bool {
        switch self {
        case .transport, .boundedRetry:
            return true
        case let .http(status):
            return status == 408
                || status == 425
                || status == 429
                || status >= 500
        case .invalidContract:
            return false
        }
    }

    var disconnectsCurrentOrigin: Bool {
        switch self {
        case .transport:
            return true
        case let .http(status):
            return status == 401 || status == 403
        case .invalidContract, .boundedRetry:
            return false
        }
    }

    var userMessage: String {
        switch self {
        case .transport:
            return "网络已断开；本地修改已保留。"
        case let .http(status) where status == 401 || status == 403:
            return "Mac 授权已失效；本地修改已保留。"
        case let .http(status) where status == 429 || status >= 500:
            return "Mac 暂时忙；下次进入前台会继续。"
        case let .http(status):
            return "同步请求未被接受（\(status)）；本地修改已保留。"
        case let .invalidContract(message):
            return "\(message)；没有覆盖本地数据。"
        case let .boundedRetry(message):
            return "\(message)；下次进入前台会继续。"
        }
    }
}
