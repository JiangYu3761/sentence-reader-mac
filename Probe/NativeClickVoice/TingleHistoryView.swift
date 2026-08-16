import AppKit
import AVFoundation
import Foundation


protocol TingleHistoryViewDelegate: AnyObject {
    func tingleHistoryViewDidRequestCapture(_ historyView: TingleHistoryView)
    func tingleHistoryView(_ historyView: TingleHistoryView, didChange state: TingleWebState)
}


final class TingleHistoryView: NSView, NSTableViewDataSource, NSTableViewDelegate, NSSearchFieldDelegate, NSTextFieldDelegate, NSTextViewDelegate {
    weak var delegate: TingleHistoryViewDelegate?

    private let api: ClickVoiceAPIClient
    private let backButton = NSButton()
    private let statusLabel = NSTextField(labelWithString: "")
    private let searchField = NSSearchField()
    private let collectionControl = NSSegmentedControl(
        labels: ["灵感", "回收站"],
        trackingMode: .selectOne,
        target: nil,
        action: nil
    )
    private let tableView = NSTableView()
    private let detailScroll = TinglePageAwareVerticalScrollView()
    private let detailDocument = NSView()
    private let detailStack = NSStackView()

    private var items: [TingleInspiration] = []
    private var visibleItems: [TingleInspiration] = []
    private var selected: TingleInspiration?
    private var titleField: NSTextField?
    private var contentTextView: NSTextView?
    private var saveStateLabel: NSTextField?
    private var playButton: NSButton?
    private var baselineTitle = ""
    private var baselineContent = ""
    private var showArchived = false
    private var isActive = false
    private var editorHasFocus = false
    private var destructiveConfirmationActive = false
    private var suppressSelectionChange = false
    private var inFlightRequestCount = 0
    private var pollingTimer: Timer?
    private var player: AVPlayer?
    private var playbackObserver: NSObjectProtocol?

    init(api: ClickVoiceAPIClient) {
        self.api = api
        super.init(frame: .zero)
        translatesAutoresizingMaskIntoConstraints = false
        buildInterface()
        renderEmptyDetail()
        publishState()
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    deinit {
        stopPolling()
        stopPlayback()
    }

    var hasUnsavedChanges: Bool {
        guard selected != nil,
              let titleField,
              let contentTextView
        else { return false }
        return titleField.stringValue != baselineTitle
            || contentTextView.string != baselineContent
    }

    var canRelease: Bool {
        !hasUnsavedChanges
            && !destructiveConfirmationActive
            && inFlightRequestCount == 0
    }

    var activeRequestCount: Int { inFlightRequestCount }

    func resume(openInspirationID: String? = nil, focusSearch: Bool = false) {
        isActive = true
        if let openInspirationID {
            openInspiration(openInspirationID)
        } else {
            reloadList(preserveSelection: true)
        }
        if focusSearch {
            focusSearchField()
        }
        updatePolling()
    }

    func suspend() {
        isActive = false
        stopPolling()
        stopPlayback()
        publishState()
    }

    func focusSearchField() {
        window?.makeFirstResponder(searchField)
    }

    func showInspirations() {
        switchCollection(archived: false)
    }

    func showRecycleBin() {
        switchCollection(archived: true)
    }

    func reload() {
        guard !hasUnsavedChanges else {
            showStatus("请先保存当前修改", isError: true)
            return
        }
        reloadList(preserveSelection: true)
    }

    func showRuntimeUnavailable(_ message: String) {
        stopPolling()
        showStatus(message, isError: true)
    }

    func openInspiration(
        _ inspirationID: String,
        completion: ((Result<Void, Error>) -> Void)? = nil
    ) {
        beginRequest()
        showStatus("正在打开灵感…")
        api.getTingleInspiration(inspirationID: inspirationID) { [weak self] result in
            DispatchQueue.main.async {
                guard let self else { return }
                self.endRequest()
                switch result {
                case let .success(inspiration):
                    self.showArchived = inspiration.archivedAt != nil
                    self.collectionControl.selectedSegment = self.showArchived ? 1 : 0
                    self.selected = inspiration
                    self.renderDetail(inspiration)
                    self.reloadList(preserveSelection: true)
                    self.showStatus("")
                    completion?(.success(()))
                case let .failure(error):
                    self.showStatus(error.localizedDescription, isError: true)
                    completion?(.failure(error))
                }
            }
        }
    }

    func saveChanges(completion: ((Result<Void, Error>) -> Void)? = nil) {
        guard let selected,
              let titleField,
              let contentTextView
        else {
            completeFailure("请先选择一条灵感", completion: completion)
            return
        }
        let title = titleField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        let content = contentTextView.string.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty else {
            completeFailure("标题不能为空", completion: completion)
            return
        }
        if content.isEmpty && !selected.content.isEmpty {
            completeFailure("已有文字不能清空", completion: completion)
            return
        }
        if content.isEmpty && !selected.isVoice {
            completeFailure("文字不能为空", completion: completion)
            return
        }

        beginRequest()
        setSaveState("正在保存…")
        api.updateTingleInspiration(
            inspirationID: selected.id,
            title: title,
            content: content.isEmpty ? nil : content
        ) { [weak self] result in
            DispatchQueue.main.async {
                guard let self else { return }
                self.endRequest()
                switch result {
                case let .success(updated):
                    self.selected = updated
                    self.renderDetail(updated)
                    self.setSaveState("已保存")
                    self.reloadList(preserveSelection: true)
                    completion?(.success(()))
                case let .failure(error):
                    self.setSaveState(error.localizedDescription, isError: true)
                    completion?(.failure(error))
                }
            }
        }
    }

    func togglePlayback(completion: ((Result<Void, Error>) -> Void)? = nil) {
        guard let selected, selected.hasAudio else {
            completeFailure("这条灵感没有原始音频", completion: completion)
            return
        }
        guard let url = api.originalAudioURL(for: selected) else {
            completeFailure("原音地址暂不可用，请稍后重试", completion: completion)
            return
        }

        if let player {
            if player.rate > 0 {
                player.pause()
            } else {
                player.play()
            }
        } else {
            let item = AVPlayerItem(url: url)
            player = AVPlayer(playerItem: item)
            playbackObserver = NotificationCenter.default.addObserver(
                forName: .AVPlayerItemDidPlayToEndTime,
                object: item,
                queue: .main
            ) { [weak self] _ in
                self?.stopPlayback()
                self?.publishState()
            }
            player?.play()
        }
        updatePlaybackButton()
        publishState()
        completion?(.success(()))
    }

    func archiveSelected(completion: ((Result<Void, Error>) -> Void)? = nil) {
        guard let selected else {
            completeFailure("请先选择一条灵感", completion: completion)
            return
        }
        guard !hasUnsavedChanges else {
            completeFailure("请先保存当前修改", completion: completion)
            return
        }
        performLifecycleAction(
            selected: selected,
            operation: api.archiveTingleInspiration,
            successMessage: "已移到回收站",
            completion: completion
        )
    }

    func restoreSelected(completion: ((Result<Void, Error>) -> Void)? = nil) {
        guard let selected else {
            completeFailure("请先选择一条灵感", completion: completion)
            return
        }
        guard !hasUnsavedChanges else {
            completeFailure("请先保存当前修改", completion: completion)
            return
        }
        performLifecycleAction(
            selected: selected,
            operation: api.restoreTingleInspiration,
            successMessage: "已恢复",
            completion: completion
        )
    }

    func retrySelected(completion: ((Result<Void, Error>) -> Void)? = nil) {
        guard let selected else {
            completeFailure("请先选择一条灵感", completion: completion)
            return
        }
        beginRequest()
        showStatus("正在重新提交…")
        api.retryTingleInspiration(inspirationID: selected.id) { [weak self] result in
            DispatchQueue.main.async {
                guard let self else { return }
                self.endRequest()
                switch result {
                case let .success(updated):
                    self.selected = updated
                    self.renderDetail(updated)
                    self.reloadList(preserveSelection: true)
                    self.showStatus("已重新提交")
                    completion?(.success(()))
                case let .failure(error):
                    self.showStatus(error.localizedDescription, isError: true)
                    completion?(.failure(error))
                }
            }
        }
    }

    func confirmAndDeleteSelected() {
        guard let selected else {
            showStatus("请先选择一条灵感", isError: true)
            return
        }
        guard !hasUnsavedChanges else {
            showStatus("请先保存当前修改", isError: true)
            return
        }
        guard selected.permanentDeleteAvailable, !selected.contentHash.isEmpty else {
            showStatus("永久删除确认尚未就绪，请重新载入", isError: true)
            return
        }

        destructiveConfirmationActive = true
        publishState()
        let alert = NSAlert()
        alert.alertStyle = .critical
        alert.messageText = "永久删除这条灵感？"
        let audioSentence = selected.hasAudio ? " 原始音频也会永久删除。" : ""
        let linkedSentence = selected.linkedExternalTargetCount > 0
            ? " 已沉淀到其他位置的 \(selected.linkedExternalTargetCount) 项内容会保留。"
            : " 其他笔记、任务和知识库内容不会被联动删除。"
        alert.informativeText = "“\(selected.title)”将无法恢复。\(audioSentence)\(linkedSentence)"
        alert.addButton(withTitle: "取消")
        let deleteButton = alert.addButton(withTitle: "永久删除")
        deleteButton.hasDestructiveAction = true
        let response = alert.runModal()
        destructiveConfirmationActive = false
        publishState()
        guard response == .alertSecondButtonReturn else { return }
        permanentlyDelete(selected)
    }

    private func buildInterface() {
        wantsLayer = true
        layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor

        let header = NSView()
        header.translatesAutoresizingMaskIntoConstraints = false
        let headerTitle = NSTextField(labelWithString: "灵感")
        headerTitle.font = .systemFont(ofSize: 18, weight: .semibold)
        headerTitle.translatesAutoresizingMaskIntoConstraints = false

        backButton.image = NSImage(
            systemSymbolName: "chevron.left",
            accessibilityDescription: "返回录音"
        )
        backButton.imagePosition = .imageOnly
        backButton.isBordered = false
        backButton.focusRingType = .exterior
        backButton.toolTip = "返回录音"
        backButton.setAccessibilityLabel("返回录音")
        backButton.target = self
        backButton.action = #selector(requestCapture)
        backButton.translatesAutoresizingMaskIntoConstraints = false

        statusLabel.font = .systemFont(ofSize: 12)
        statusLabel.textColor = .secondaryLabelColor
        statusLabel.alignment = .right
        statusLabel.lineBreakMode = .byTruncatingTail
        statusLabel.translatesAutoresizingMaskIntoConstraints = false

        header.addSubview(backButton)
        header.addSubview(headerTitle)
        header.addSubview(statusLabel)

        let headerSeparator = NSBox()
        headerSeparator.boxType = .separator
        headerSeparator.translatesAutoresizingMaskIntoConstraints = false

        let listPane = NSView()
        listPane.translatesAutoresizingMaskIntoConstraints = false
        searchField.placeholderString = "搜索灵感"
        searchField.delegate = self
        searchField.translatesAutoresizingMaskIntoConstraints = false
        collectionControl.selectedSegment = 0
        collectionControl.target = self
        collectionControl.action = #selector(collectionChanged)
        collectionControl.translatesAutoresizingMaskIntoConstraints = false

        let tableColumn = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("inspiration"))
        tableColumn.resizingMask = .autoresizingMask
        tableView.addTableColumn(tableColumn)
        tableView.headerView = nil
        tableView.rowHeight = 82
        tableView.intercellSpacing = NSSize(width: 0, height: 1)
        tableView.selectionHighlightStyle = .regular
        tableView.dataSource = self
        tableView.delegate = self
        tableView.setAccessibilityLabel("灵感列表")

        let listScroll = TinglePageAwareVerticalScrollView()
        listScroll.documentView = tableView
        listScroll.hasVerticalScroller = true
        listScroll.drawsBackground = false
        listScroll.borderType = .noBorder
        listScroll.translatesAutoresizingMaskIntoConstraints = false
        listPane.addSubview(searchField)
        listPane.addSubview(collectionControl)
        listPane.addSubview(listScroll)

        let bodySeparator = NSBox()
        bodySeparator.boxType = .separator
        bodySeparator.translatesAutoresizingMaskIntoConstraints = false

        detailScroll.hasVerticalScroller = true
        detailScroll.drawsBackground = false
        detailScroll.borderType = .noBorder
        detailScroll.translatesAutoresizingMaskIntoConstraints = false
        detailDocument.translatesAutoresizingMaskIntoConstraints = false
        detailStack.orientation = .vertical
        detailStack.alignment = .leading
        detailStack.spacing = 14
        detailStack.translatesAutoresizingMaskIntoConstraints = false
        detailDocument.addSubview(detailStack)
        detailScroll.documentView = detailDocument

        addSubview(header)
        addSubview(headerSeparator)
        addSubview(listPane)
        addSubview(bodySeparator)
        addSubview(detailScroll)

        NSLayoutConstraint.activate([
            header.topAnchor.constraint(equalTo: topAnchor),
            header.leadingAnchor.constraint(equalTo: leadingAnchor),
            header.trailingAnchor.constraint(equalTo: trailingAnchor),
            header.heightAnchor.constraint(equalToConstant: 64),
            backButton.leadingAnchor.constraint(equalTo: header.leadingAnchor, constant: 14),
            backButton.centerYAnchor.constraint(equalTo: header.centerYAnchor, constant: 7),
            backButton.widthAnchor.constraint(equalToConstant: 44),
            backButton.heightAnchor.constraint(equalToConstant: 44),
            headerTitle.leadingAnchor.constraint(equalTo: backButton.trailingAnchor, constant: 4),
            headerTitle.centerYAnchor.constraint(equalTo: backButton.centerYAnchor),
            statusLabel.leadingAnchor.constraint(greaterThanOrEqualTo: headerTitle.trailingAnchor, constant: 16),
            statusLabel.trailingAnchor.constraint(equalTo: header.trailingAnchor, constant: -18),
            statusLabel.centerYAnchor.constraint(equalTo: backButton.centerYAnchor),

            headerSeparator.topAnchor.constraint(equalTo: header.bottomAnchor),
            headerSeparator.leadingAnchor.constraint(equalTo: leadingAnchor),
            headerSeparator.trailingAnchor.constraint(equalTo: trailingAnchor),

            listPane.topAnchor.constraint(equalTo: headerSeparator.bottomAnchor),
            listPane.leadingAnchor.constraint(equalTo: leadingAnchor),
            listPane.bottomAnchor.constraint(equalTo: bottomAnchor),
            listPane.widthAnchor.constraint(equalToConstant: 300),
            searchField.topAnchor.constraint(equalTo: listPane.topAnchor, constant: 12),
            searchField.leadingAnchor.constraint(equalTo: listPane.leadingAnchor, constant: 12),
            searchField.trailingAnchor.constraint(equalTo: listPane.trailingAnchor, constant: -12),
            collectionControl.topAnchor.constraint(equalTo: searchField.bottomAnchor, constant: 8),
            collectionControl.leadingAnchor.constraint(equalTo: searchField.leadingAnchor),
            collectionControl.trailingAnchor.constraint(equalTo: searchField.trailingAnchor),
            listScroll.topAnchor.constraint(equalTo: collectionControl.bottomAnchor, constant: 10),
            listScroll.leadingAnchor.constraint(equalTo: listPane.leadingAnchor),
            listScroll.trailingAnchor.constraint(equalTo: listPane.trailingAnchor),
            listScroll.bottomAnchor.constraint(equalTo: listPane.bottomAnchor),

            bodySeparator.topAnchor.constraint(equalTo: headerSeparator.bottomAnchor),
            bodySeparator.leadingAnchor.constraint(equalTo: listPane.trailingAnchor),
            bodySeparator.bottomAnchor.constraint(equalTo: bottomAnchor),

            detailScroll.topAnchor.constraint(equalTo: headerSeparator.bottomAnchor),
            detailScroll.leadingAnchor.constraint(equalTo: bodySeparator.trailingAnchor),
            detailScroll.trailingAnchor.constraint(equalTo: trailingAnchor),
            detailScroll.bottomAnchor.constraint(equalTo: bottomAnchor),

            detailDocument.leadingAnchor.constraint(equalTo: detailScroll.contentView.leadingAnchor),
            detailDocument.trailingAnchor.constraint(equalTo: detailScroll.contentView.trailingAnchor),
            detailDocument.topAnchor.constraint(equalTo: detailScroll.contentView.topAnchor),
            detailDocument.widthAnchor.constraint(equalTo: detailScroll.contentView.widthAnchor),
            detailStack.topAnchor.constraint(equalTo: detailDocument.topAnchor, constant: 26),
            detailStack.leadingAnchor.constraint(equalTo: detailDocument.leadingAnchor, constant: 28),
            detailStack.trailingAnchor.constraint(equalTo: detailDocument.trailingAnchor, constant: -28),
            detailStack.bottomAnchor.constraint(equalTo: detailDocument.bottomAnchor, constant: -36),
        ])
    }

    private func reloadList(preserveSelection: Bool) {
        let selectedID = preserveSelection ? selected?.id : nil
        beginRequest()
        showStatus("正在载入…")
        api.listTingleInspirations(archived: showArchived) { [weak self] result in
            DispatchQueue.main.async {
                guard let self else { return }
                self.endRequest()
                switch result {
                case let .success(items):
                    self.items = items
                    self.applySearchFilter()
                    if let selectedID,
                       let row = self.visibleItems.firstIndex(where: { $0.id == selectedID }) {
                        self.suppressSelectionChange = true
                        self.tableView.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false)
                        self.suppressSelectionChange = false
                    }
                    self.showStatus("")
                    self.updatePolling()
                case let .failure(error):
                    self.items = []
                    self.applySearchFilter()
                    self.showStatus(error.localizedDescription, isError: true)
                }
            }
        }
    }

    private func applySearchFilter() {
        let query = searchField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        if query.isEmpty {
            visibleItems = items
        } else {
            visibleItems = items.filter {
                $0.title.localizedCaseInsensitiveContains(query)
                    || $0.content.localizedCaseInsensitiveContains(query)
                    || $0.preview.localizedCaseInsensitiveContains(query)
            }
        }
        tableView.reloadData()
    }

    private func selectVisibleRow(_ row: Int) {
        guard visibleItems.indices.contains(row) else { return }
        let candidate = visibleItems[row]
        if candidate.id == selected?.id { return }
        guard canLeaveCurrentEditor() else {
            restoreSelectedRow()
            return
        }
        openInspiration(candidate.id)
    }

    private func canLeaveCurrentEditor() -> Bool {
        guard hasUnsavedChanges else { return true }
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "当前修改尚未保存"
        alert.informativeText = "继续切换会丢弃刚才对标题或文字的修改。"
        alert.addButton(withTitle: "继续编辑")
        alert.addButton(withTitle: "放弃修改")
        return alert.runModal() == .alertSecondButtonReturn
    }

    private func restoreSelectedRow() {
        suppressSelectionChange = true
        if let selected,
           let row = visibleItems.firstIndex(where: { $0.id == selected.id }) {
            tableView.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false)
        } else {
            tableView.deselectAll(nil)
        }
        suppressSelectionChange = false
    }

    private func renderEmptyDetail() {
        stopPlayback()
        clearDetailStack()
        let placeholder = NSTextField(labelWithString: "选择一条灵感")
        placeholder.textColor = .secondaryLabelColor
        placeholder.font = .systemFont(ofSize: 13)
        detailStack.addArrangedSubview(placeholder)
        titleField = nil
        contentTextView = nil
        saveStateLabel = nil
        baselineTitle = ""
        baselineContent = ""
        publishState()
    }

    private func renderDetail(_ inspiration: TingleInspiration) {
        stopPlayback()
        clearDetailStack()

        let title = NSTextField(string: inspiration.title)
        title.font = .systemFont(ofSize: 22, weight: .semibold)
        title.isBordered = false
        title.drawsBackground = false
        title.focusRingType = .exterior
        title.delegate = self
        title.setAccessibilityLabel("灵感标题")
        title.translatesAutoresizingMaskIntoConstraints = false
        titleField = title

        let save = commandButton(
            title: "保存",
            symbol: "checkmark",
            action: #selector(saveFromButton)
        )
        save.keyEquivalent = "\r"
        let titleRow = horizontalStack([title, save], spacing: 10)
        titleRow.distribution = .fill
        title.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        detailStack.addArrangedSubview(titleRow)
        titleRow.widthAnchor.constraint(equalTo: detailStack.widthAnchor).isActive = true

        let meta = NSTextField(labelWithString: detailMetadata(inspiration))
        meta.font = .systemFont(ofSize: 11)
        meta.textColor = inspiration.state == "failed" ? .systemRed : .secondaryLabelColor
        meta.maximumNumberOfLines = 2
        meta.lineBreakMode = .byWordWrapping
        detailStack.addArrangedSubview(meta)
        meta.widthAnchor.constraint(equalTo: detailStack.widthAnchor).isActive = true

        let saveState = NSTextField(labelWithString: "")
        saveState.font = .systemFont(ofSize: 11)
        saveState.textColor = .secondaryLabelColor
        saveStateLabel = saveState
        detailStack.addArrangedSubview(saveState)
        addSeparator()

        if let failure = inspiration.failureMessage, !failure.isEmpty {
            let failureLabel = NSTextField(wrappingLabelWithString: failure)
            failureLabel.textColor = .systemRed
            failureLabel.font = .systemFont(ofSize: 12)
            let retry = commandButton(
                title: "重试",
                symbol: "arrow.clockwise",
                action: #selector(retryFromButton)
            )
            detailStack.addArrangedSubview(failureLabel)
            failureLabel.widthAnchor.constraint(equalTo: detailStack.widthAnchor).isActive = true
            detailStack.addArrangedSubview(retry)
            addSeparator()
        }

        if inspiration.isVoice {
            detailStack.addArrangedSubview(sectionLabel("原音"))
            let canPlayAudio = inspiration.hasAudio
                && api.originalAudioURL(for: inspiration) != nil
            let play = commandButton(
                title: canPlayAudio ? "播放" : "原音暂不可用",
                symbol: "play.fill",
                action: #selector(playFromButton)
            )
            play.isEnabled = canPlayAudio
            playButton = play
            let duration = NSTextField(labelWithString: formatDuration(inspiration.durationMS))
            duration.textColor = .secondaryLabelColor
            duration.font = .monospacedDigitSystemFont(ofSize: 12, weight: .regular)
            detailStack.addArrangedSubview(horizontalStack([play, duration], spacing: 10))
            addSeparator()
        }

        detailStack.addArrangedSubview(sectionLabel("当前文字"))
        let editor = editableTextView(inspiration.content)
        contentTextView = editor.textView
        detailStack.addArrangedSubview(editor.scroll)
        editor.scroll.widthAnchor.constraint(equalTo: detailStack.widthAnchor).isActive = true
        editor.scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 210).isActive = true

        if let cleaned = inspiration.hermesCleanedTranscript, !cleaned.isEmpty {
            detailStack.addArrangedSubview(sectionLabel("Hermes 忠实整理"))
            let cleanedView = readOnlyTextView(cleaned)
            detailStack.addArrangedSubview(cleanedView)
            cleanedView.widthAnchor.constraint(equalTo: detailStack.widthAnchor).isActive = true
            cleanedView.heightAnchor.constraint(greaterThanOrEqualToConstant: 110).isActive = true
        }

        if let raw = inspiration.rawTranscript, !raw.isEmpty {
            detailStack.addArrangedSubview(sectionLabel("FunASR 原始转写"))
            let rawView = readOnlyTextView(raw)
            detailStack.addArrangedSubview(rawView)
            rawView.widthAnchor.constraint(equalTo: detailStack.widthAnchor).isActive = true
            rawView.heightAnchor.constraint(greaterThanOrEqualToConstant: 110).isActive = true
        }

        addSeparator()
        let lifecycle = commandButton(
            title: showArchived ? "恢复" : "归档",
            symbol: showArchived ? "arrow.uturn.backward" : "archivebox",
            action: showArchived ? #selector(restoreFromButton) : #selector(archiveFromButton)
        )
        let delete = commandButton(
            title: "永久删除",
            symbol: "trash",
            action: #selector(deleteFromButton)
        )
        delete.contentTintColor = .systemRed
        detailStack.addArrangedSubview(horizontalStack([lifecycle, delete], spacing: 8))

        baselineTitle = inspiration.title
        baselineContent = inspiration.content
        editorHasFocus = false
        publishState()
    }

    private func clearDetailStack() {
        for view in detailStack.arrangedSubviews {
            detailStack.removeArrangedSubview(view)
            view.removeFromSuperview()
        }
        playButton = nil
    }

    private func performLifecycleAction(
        selected: TingleInspiration,
        operation: @escaping (
            String,
            @escaping (Result<TingleInspiration, Error>) -> Void
        ) -> Void,
        successMessage: String,
        completion: ((Result<Void, Error>) -> Void)?
    ) {
        beginRequest()
        operation(selected.id) { [weak self] result in
            DispatchQueue.main.async {
                guard let self else { return }
                self.endRequest()
                switch result {
                case .success:
                    self.selected = nil
                    self.tableView.deselectAll(nil)
                    self.renderEmptyDetail()
                    self.reloadList(preserveSelection: false)
                    self.showStatus(successMessage)
                    completion?(.success(()))
                case let .failure(error):
                    self.showStatus(error.localizedDescription, isError: true)
                    completion?(.failure(error))
                }
            }
        }
    }

    private func permanentlyDelete(_ inspiration: TingleInspiration) {
        beginRequest()
        showStatus("正在永久删除…")
        api.permanentlyDeleteTingleInspiration(
            inspirationID: inspiration.id,
            expectedContentHash: inspiration.contentHash,
            acknowledgeLinkedOutputs: true
        ) { [weak self] result in
            DispatchQueue.main.async {
                guard let self else { return }
                self.endRequest()
                switch result {
                case .success:
                    self.selected = nil
                    self.tableView.deselectAll(nil)
                    self.renderEmptyDetail()
                    self.reloadList(preserveSelection: false)
                    self.showStatus("已永久删除")
                case let .failure(error):
                    self.showStatus(error.localizedDescription, isError: true)
                }
            }
        }
    }

    private func switchCollection(archived: Bool) {
        guard archived != showArchived else { return }
        guard canLeaveCurrentEditor() else {
            collectionControl.selectedSegment = showArchived ? 1 : 0
            return
        }
        selected = nil
        tableView.deselectAll(nil)
        renderEmptyDetail()
        showArchived = archived
        collectionControl.selectedSegment = archived ? 1 : 0
        reloadList(preserveSelection: false)
    }

    private func beginRequest() {
        inFlightRequestCount += 1
        publishState()
    }

    private func endRequest() {
        inFlightRequestCount = max(0, inFlightRequestCount - 1)
        publishState()
    }

    private func updatePolling() {
        let needsPolling = isActive && items.contains(where: \.isProcessing)
        if needsPolling, pollingTimer == nil {
            pollingTimer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
                guard let self,
                      self.isActive,
                      !self.hasUnsavedChanges
                else { return }
                self.reloadList(preserveSelection: true)
                if let selected = self.selected, selected.isProcessing {
                    self.openInspiration(selected.id)
                }
            }
        } else if !needsPolling {
            stopPolling()
        }
    }

    private func stopPolling() {
        pollingTimer?.invalidate()
        pollingTimer = nil
    }

    private func stopPlayback() {
        player?.pause()
        player = nil
        if let playbackObserver {
            NotificationCenter.default.removeObserver(playbackObserver)
            self.playbackObserver = nil
        }
        updatePlaybackButton()
    }

    private func updatePlaybackButton() {
        let playing = player?.rate ?? 0 > 0
        playButton?.title = playing ? "暂停" : "播放"
        playButton?.image = NSImage(
            systemSymbolName: playing ? "pause.fill" : "play.fill",
            accessibilityDescription: playing ? "暂停原音" : "播放原音"
        )
    }

    private func publishState() {
        let state = TingleWebState(
            collection: showArchived ? .recycleBin : .inspirations,
            selectedInspirationID: selected?.id,
            selectedTitle: selected?.title,
            selectedContentHash: selected?.contentHash,
            linkedExternalTargetCount: selected?.linkedExternalTargetCount ?? 0,
            selectedHasAudio: selected?.hasAudio ?? false,
            editorFocused: editorHasFocus,
            hasUnsavedChanges: hasUnsavedChanges,
            playbackState: player == nil ? .idle : ((player?.rate ?? 0) > 0 ? .playing : .paused),
            permanentDeleteAvailable: selected?.permanentDeleteAvailable ?? false,
            destructiveConfirmationActive: destructiveConfirmationActive
        )
        delegate?.tingleHistoryView(self, didChange: state)
    }

    private func showStatus(_ message: String, isError: Bool = false) {
        statusLabel.stringValue = message
        statusLabel.textColor = isError ? .systemRed : .secondaryLabelColor
    }

    private func setSaveState(_ message: String, isError: Bool = false) {
        saveStateLabel?.stringValue = message
        saveStateLabel?.textColor = isError ? .systemRed : .secondaryLabelColor
        showStatus(isError ? message : "")
        publishState()
    }

    private func completeFailure(
        _ message: String,
        completion: ((Result<Void, Error>) -> Void)?
    ) {
        let error = ClickVoiceAPIError.server(message)
        setSaveState(message, isError: true)
        completion?(.failure(error))
    }

    private func detailMetadata(_ inspiration: TingleInspiration) -> String {
        var parts = [sourceText(inspiration.source), formatDate(inspiration.createdAt)]
        if let duration = inspiration.durationMS {
            parts.append(formatDuration(duration))
        }
        if inspiration.state == "processing" {
            parts.append("整理中")
        } else if inspiration.state == "failed" {
            parts.append("处理失败")
        } else if showArchived {
            parts.append(archiveState(inspiration))
        }
        return parts.filter { !$0.isEmpty }.joined(separator: " · ")
    }

    private func sourceText(_ source: String) -> String {
        switch source {
        case "mac": return "Mac"
        case "click_mobile": return "手机"
        default: return "Hermes"
        }
    }

    private func archiveState(_ inspiration: TingleInspiration) -> String {
        guard let purgeAfter = inspiration.purgeAfter else { return "历史归档" }
        guard let deadline = ISO8601DateFormatter().date(from: purgeAfter) else { return "已归档" }
        let days = max(0, Int(ceil(deadline.timeIntervalSinceNow / 86_400)))
        return days == 0 ? "等待清理" : "\(days) 天后删除"
    }

    private func formatDate(_ value: String) -> String {
        guard let date = ISO8601DateFormatter().date(from: value) else { return value }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.dateFormat = Calendar.current.isDate(date, equalTo: Date(), toGranularity: .year)
            ? "M月d日 HH:mm"
            : "yyyy年M月d日"
        return formatter.string(from: date)
    }

    private func formatDuration(_ milliseconds: Int?) -> String {
        guard let milliseconds else { return "" }
        let total = max(0, Int((Double(milliseconds) / 1000).rounded()))
        return String(format: "%02d:%02d", total / 60, total % 60)
    }

    private func commandButton(title: String, symbol: String, action: Selector) -> NSButton {
        let button = NSButton(title: title, target: self, action: action)
        button.bezelStyle = .rounded
        button.image = NSImage(systemSymbolName: symbol, accessibilityDescription: title)
        button.imagePosition = .imageLeading
        button.toolTip = title
        return button
    }

    private func horizontalStack(_ views: [NSView], spacing: CGFloat) -> NSStackView {
        let stack = NSStackView(views: views)
        stack.orientation = .horizontal
        stack.alignment = .centerY
        stack.spacing = spacing
        return stack
    }

    private func sectionLabel(_ text: String) -> NSTextField {
        let label = NSTextField(labelWithString: text)
        label.font = .systemFont(ofSize: 13, weight: .semibold)
        return label
    }

    private func addSeparator() {
        let box = NSBox()
        box.boxType = .separator
        detailStack.addArrangedSubview(box)
        box.widthAnchor.constraint(equalTo: detailStack.widthAnchor).isActive = true
    }

    private func editableTextView(_ text: String) -> (scroll: NSScrollView, textView: NSTextView) {
        let textView = NSTextView()
        textView.string = text
        textView.font = .systemFont(ofSize: 15)
        textView.textContainerInset = NSSize(width: 8, height: 8)
        textView.isRichText = false
        textView.allowsUndo = true
        textView.delegate = self
        textView.setAccessibilityLabel("灵感文字")
        let scroll = TinglePageAwareVerticalScrollView()
        scroll.documentView = textView
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        scroll.drawsBackground = true
        return (scroll, textView)
    }

    private func readOnlyTextView(_ text: String) -> NSScrollView {
        let textView = NSTextView()
        textView.string = text
        textView.font = .systemFont(ofSize: 13)
        textView.textColor = .secondaryLabelColor
        textView.textContainerInset = NSSize(width: 8, height: 8)
        textView.isEditable = false
        textView.isSelectable = true
        textView.isRichText = false
        textView.drawsBackground = false
        let scroll = TinglePageAwareVerticalScrollView()
        scroll.documentView = textView
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        scroll.drawsBackground = false
        return scroll
    }

    @objc private func requestCapture() {
        delegate?.tingleHistoryViewDidRequestCapture(self)
    }

    @objc private func collectionChanged() {
        switchCollection(archived: collectionControl.selectedSegment == 1)
    }

    @objc private func saveFromButton() {
        saveChanges()
    }

    @objc private func playFromButton() {
        togglePlayback()
    }

    @objc private func retryFromButton() {
        retrySelected()
    }

    @objc private func archiveFromButton() {
        archiveSelected()
    }

    @objc private func restoreFromButton() {
        restoreSelected()
    }

    @objc private func deleteFromButton() {
        confirmAndDeleteSelected()
    }

    func numberOfRows(in tableView: NSTableView) -> Int {
        visibleItems.count
    }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        guard visibleItems.indices.contains(row) else { return nil }
        let identifier = NSUserInterfaceItemIdentifier("TingleInspirationCell")
        let cell: TingleInspirationCellView
        if let reusable = tableView.makeView(withIdentifier: identifier, owner: self) as? TingleInspirationCellView {
            cell = reusable
        } else {
            cell = TingleInspirationCellView()
            cell.identifier = identifier
        }
        cell.update(
            inspiration: visibleItems[row],
            source: sourceText(visibleItems[row].source),
            date: formatDate(visibleItems[row].createdAt)
        )
        return cell
    }

    func tableViewSelectionDidChange(_ notification: Notification) {
        guard !suppressSelectionChange else { return }
        selectVisibleRow(tableView.selectedRow)
    }

    func controlTextDidChange(_ obj: Notification) {
        if obj.object as? NSSearchField === searchField {
            applySearchFilter()
        }
        publishState()
    }

    func controlTextDidBeginEditing(_ obj: Notification) {
        if obj.object as? NSTextField === titleField {
            editorHasFocus = true
            publishState()
        }
    }

    func controlTextDidEndEditing(_ obj: Notification) {
        if obj.object as? NSTextField === titleField {
            editorHasFocus = false
            publishState()
        }
    }

    func textDidBeginEditing(_ notification: Notification) {
        if notification.object as? NSTextView === contentTextView {
            editorHasFocus = true
            publishState()
        }
    }

    func textDidChange(_ notification: Notification) {
        publishState()
    }

    func textDidEndEditing(_ notification: Notification) {
        if notification.object as? NSTextView === contentTextView {
            editorHasFocus = false
            publishState()
        }
    }
}


private final class TingleInspirationCellView: NSTableCellView {
    private let titleLabel = NSTextField(labelWithString: "")
    private let previewLabel = NSTextField(wrappingLabelWithString: "")
    private let metadataLabel = NSTextField(labelWithString: "")

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        titleLabel.font = .systemFont(ofSize: 14, weight: .semibold)
        titleLabel.lineBreakMode = .byTruncatingTail
        previewLabel.font = .systemFont(ofSize: 12)
        previewLabel.textColor = .secondaryLabelColor
        previewLabel.maximumNumberOfLines = 2
        previewLabel.lineBreakMode = .byTruncatingTail
        metadataLabel.font = .systemFont(ofSize: 10)
        metadataLabel.textColor = .tertiaryLabelColor

        let stack = NSStackView(views: [titleLabel, previewLabel, metadataLabel])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 4
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: topAnchor, constant: 8),
            stack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 12),
            stack.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -12),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: bottomAnchor, constant: -8),
            titleLabel.widthAnchor.constraint(equalTo: stack.widthAnchor),
            previewLabel.widthAnchor.constraint(equalTo: stack.widthAnchor),
        ])
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func update(inspiration: TingleInspiration, source: String, date: String) {
        titleLabel.stringValue = inspiration.title
        previewLabel.stringValue = inspiration.preview
        var metadata = [source, date]
        if inspiration.state == "processing" {
            metadata.append("整理中")
        } else if inspiration.state == "failed" {
            metadata.append("处理失败")
        }
        metadataLabel.stringValue = metadata.filter { !$0.isEmpty }.joined(separator: " · ")
    }
}
