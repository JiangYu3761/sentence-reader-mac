import AppKit
import Foundation

final class ClickVoiceAppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, NSMenuItemValidation, TingleRecordingCoordinatorDelegate, TingleAppIntentCommandHandling, TingleHistoryViewDelegate, TingleSurfacePageControllerRoutingDelegate {
    private let recorder = ClickVoiceRecorder()
    private let api = ClickVoiceAPIClient()
    private let cuePlayer = TingleCuePlayer()
    private let notifier = TingleNotifier()
    private let loginItemController = TingleLoginItemController()
    private lazy var coordinator = TingleRecordingCoordinator(
        captureDriver: recorder,
        uploader: api,
        playStartCue: { [weak self] completion in
            guard let self else {
                completion()
                return
            }
            self.cuePlayer.playStart(completion: completion)
        },
        playStopCue: { [weak self] in self?.cuePlayer.playStop() }
    )
    private lazy var globalShortcut = TingleGlobalHotKeyController { [weak self] in
        // A global recording command must never activate or reveal the Tingle window.
        self?.handleGlobalRecordingCommand()
    }

    private var window: TingleWindow!
    private var captureView: TingleCaptureView!
    private var surfacePageController: TingleSurfacePageController!
    private var historyView: TingleHistoryView?
    private var timer: Timer?
    private var runtimeRecoveryWorkItem: DispatchWorkItem?
    private var historyReleaseWorkItem: DispatchWorkItem?
    private var memoryPressureSource: DispatchSourceMemoryPressure?
    private var runtimeRecoveryAttempt = 0
    private var historyLifecycleGeneration = 0
    private let historyWarmGraceSeconds: TimeInterval = 5
    private var webState = TingleWebState()

    private var surface: TingleSurfacePage {
        surfacePageController?.selectedPage ?? .capture
    }

    private var runtimeReady = false
    private var runtimeStarting = false
    private var microphoneAlertVisible = false
    private var unsavedNavigationAlertVisible = false
    private var latestCaptureID: String?
    private var latestVoiceRecordID: String?
    private var latestInspirationID: String?
    private var latestInspirationIsProcessing = false
    private var latestRawTranscriptText: String?
    private var latestTranscriptionNotificationArmed = false
    private var latestRefreshGeneration = 0
    private var latestRefreshAttempt = 0
    private var latestRefreshStartedAt: Date?
    private var latestRefreshWorkItem: DispatchWorkItem?

    private var statusItem: NSStatusItem?
    private var shortcutState = TingleGlobalShortcutState.notRegistered
    private var loginItemError: String?
    private var lastFailureNotification: String?
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        coordinator.delegate = self
        TingleAppIntentRouter.shared.register(self)
        installApplicationMenu()
        buildWindow()
        installMemoryPressureObserver()
        installStatusItem()
        installGlobalShortcut()
        _ = coordinator.recoverPendingCaptures()
        captureView.update(snapshot: coordinator.snapshot)
        if NSApp.isActive {
            _ = requestShowCaptureSurface(
                activate: true,
                source: .userActivation,
                animated: false
            )
        }
        ensureRuntime()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    func applicationDockMenu(_ sender: NSApplication) -> NSMenu? {
        let menu = NSMenu(title: "Tingle")
        menu.addItem(menuItem("录音", action: #selector(showCapture(_:))))
        menu.addItem(menuItem("历史", action: #selector(showHistory(_:))))
        return menu
    }

    func applicationDidBecomeActive(_ notification: Notification) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { [weak self] in
            guard let self, self.window != nil, !self.window.isVisible else { return }
            if TingleAppIntentActivationGuard.shared.shouldSuppressReopen() {
                UserDefaults.standard.set(
                    "suppressed_background_intent",
                    forKey: "TingleLastReopenDisposition"
                )
                return
            }
            UserDefaults.standard.set(
                "shown_for_user_activation",
                forKey: "TingleLastReopenDisposition"
            )
            self.showWindowForUserActivation()
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.30) { [weak self] in
            if TingleAppIntentActivationGuard.shared.shouldSuppressReopen() {
                self?.window?.orderOut(nil)
                sender.hide(nil)
                UserDefaults.standard.set(
                    "suppressed_background_intent",
                    forKey: "TingleLastReopenDisposition"
                )
                return
            }
            UserDefaults.standard.set(
                "shown_for_user_activation",
                forKey: "TingleLastReopenDisposition"
            )
            self?.showWindowForUserActivation()
        }
        return false
    }

    func applicationWillTerminate(_ notification: Notification) {
        TingleAppIntentRouter.shared.unregister(self)
        coordinator.preserveForTermination()
        timer?.invalidate()
        runtimeRecoveryWorkItem?.cancel()
        historyReleaseWorkItem?.cancel()
        memoryPressureSource?.cancel()
        memoryPressureSource = nil
        latestRefreshWorkItem?.cancel()
        globalShortcut.unregister()
        historyView?.suspend()
        if let statusItem {
            NSStatusBar.system.removeStatusItem(statusItem)
            self.statusItem = nil
        }
    }

    func windowWillClose(_ notification: Notification) {
        guard surface == .history,
              !webState.hasUnsavedChanges,
              webState.destructiveConfirmationActive != true
        else { return }
        _ = requestShowCaptureSurface(
            activate: false,
            source: .windowClose,
            animated: false
        )
    }

    private func installApplicationMenu() {
        let mainMenu = NSMenu()

        let appMenuItem = NSMenuItem()
        mainMenu.addItem(appMenuItem)
        let appMenu = NSMenu(title: "Tingle")
        let about = NSMenuItem(title: "关于 Tingle", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        about.target = NSApp
        appMenu.addItem(about)
        appMenu.addItem(NSMenuItem.separator())
        let hide = NSMenuItem(title: "隐藏 Tingle", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        hide.keyEquivalentModifierMask = [.command]
        hide.target = NSApp
        appMenu.addItem(hide)
        let hideOthers = NSMenuItem(title: "隐藏其他", action: #selector(NSApplication.hideOtherApplications(_:)), keyEquivalent: "h")
        hideOthers.keyEquivalentModifierMask = [.command, .option]
        hideOthers.target = NSApp
        appMenu.addItem(hideOthers)
        let showAll = NSMenuItem(title: "全部显示", action: #selector(NSApplication.unhideAllApplications(_:)), keyEquivalent: "")
        showAll.target = NSApp
        appMenu.addItem(showAll)
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(menuItem("退出 Tingle", action: #selector(quit(_:)), key: "q", modifiers: [.command]))
        appMenuItem.submenu = appMenu

        let fileMenuItem = NSMenuItem()
        let fileMenu = NSMenu(title: "文件")
        fileMenu.addItem(menuItem("开始录音", action: #selector(startRecordingFromApplicationMenu(_:)), key: " "))
        fileMenu.addItem(menuItem("暂停录音", action: #selector(togglePauseFromApplicationMenu(_:))))
        fileMenu.addItem(menuItem("停止并保存录音", action: #selector(stopRecordingFromApplicationMenu(_:)), key: " "))
        fileMenu.addItem(NSMenuItem.separator())
        fileMenu.addItem(responderMenuItem("关闭窗口", action: NSSelectorFromString("performClose:"), key: "w", modifiers: [.command]))
        fileMenuItem.submenu = fileMenu
        mainMenu.addItem(fileMenuItem)

        let editMenuItem = NSMenuItem()
        let editMenu = NSMenu(title: "编辑")
        editMenu.addItem(responderMenuItem("撤销", action: NSSelectorFromString("undo:"), key: "z", modifiers: [.command]))
        editMenu.addItem(responderMenuItem("重做", action: NSSelectorFromString("redo:"), key: "z", modifiers: [.command, .shift]))
        editMenu.addItem(NSMenuItem.separator())
        editMenu.addItem(responderMenuItem("剪切", action: NSSelectorFromString("cut:"), key: "x", modifiers: [.command]))
        editMenu.addItem(responderMenuItem("拷贝", action: NSSelectorFromString("copy:"), key: "c", modifiers: [.command]))
        editMenu.addItem(responderMenuItem("粘贴", action: NSSelectorFromString("paste:"), key: "v", modifiers: [.command]))
        editMenu.addItem(responderMenuItem("全选", action: NSSelectorFromString("selectAll:"), key: "a", modifiers: [.command]))
        editMenu.addItem(NSMenuItem.separator())
        editMenu.addItem(menuItem("查找灵感", action: #selector(findInspirations(_:)), key: "f", modifiers: [.command]))
        editMenu.addItem(menuItem("保存更改", action: #selector(saveInspirationChanges(_:)), key: "s", modifiers: [.command]))
        editMenuItem.submenu = editMenu
        mainMenu.addItem(editMenuItem)

        let inspirationMenuItem = NSMenuItem()
        let inspirationMenu = NSMenu(title: "灵感")
        inspirationMenu.addItem(menuItem("播放原音", action: #selector(toggleOriginalAudio(_:))))
        inspirationMenu.addItem(NSMenuItem.separator())
        inspirationMenu.addItem(menuItem("移到回收站", action: #selector(archiveInspiration(_:)), key: "\u{8}", modifiers: [.command]))
        inspirationMenu.addItem(menuItem("从回收站恢复", action: #selector(restoreInspiration(_:))))
        inspirationMenu.addItem(menuItem("立即删除…", action: #selector(deleteInspirationImmediately(_:)), key: "\u{8}", modifiers: [.command, .option]))
        inspirationMenuItem.submenu = inspirationMenu
        mainMenu.addItem(inspirationMenuItem)

        let viewMenuItem = NSMenuItem()
        let viewMenu = NSMenu(title: "显示")
        viewMenu.addItem(menuItem("录音", action: #selector(showCapture(_:)), key: "1", modifiers: [.command]))
        viewMenu.addItem(menuItem("历史", action: #selector(showHistory(_:)), key: "2", modifiers: [.command]))
        viewMenu.addItem(NSMenuItem.separator())
        viewMenu.addItem(menuItem("显示全部灵感", action: #selector(showInspirations(_:))))
        viewMenu.addItem(menuItem("显示回收站", action: #selector(showRecycleBin(_:))))
        viewMenu.addItem(NSMenuItem.separator())
        viewMenu.addItem(menuItem("重新载入 Tingle", action: #selector(reloadTingle(_:)), key: "r", modifiers: [.command]))
        viewMenuItem.submenu = viewMenu
        mainMenu.addItem(viewMenuItem)

        let windowMenuItem = NSMenuItem()
        let windowMenu = NSMenu(title: "窗口")
        windowMenu.addItem(responderMenuItem("最小化", action: NSSelectorFromString("performMiniaturize:"), key: "m", modifiers: [.command]))
        windowMenu.addItem(responderMenuItem("缩放", action: NSSelectorFromString("performZoom:")))
        windowMenu.addItem(NSMenuItem.separator())
        windowMenu.addItem(responderMenuItem("前置全部窗口", action: NSSelectorFromString("arrangeInFront:")))
        windowMenuItem.submenu = windowMenu
        mainMenu.addItem(windowMenuItem)

        let helpMenuItem = NSMenuItem()
        let helpMenu = NSMenu(title: "帮助")
        helpMenu.addItem(menuItem("Tingle 帮助", action: #selector(showTingleHelp(_:))))
        helpMenuItem.submenu = helpMenu
        mainMenu.addItem(helpMenuItem)

        NSApp.mainMenu = mainMenu
        NSApp.windowsMenu = windowMenu
        NSApp.helpMenu = helpMenu
    }

    private func menuItem(
        _ title: String,
        action: Selector,
        key: String = "",
        modifiers: NSEvent.ModifierFlags = []
    ) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.keyEquivalentModifierMask = modifiers
        item.target = self
        return item
    }

    private func responderMenuItem(
        _ title: String,
        action: Selector,
        key: String = "",
        modifiers: NSEvent.ModifierFlags = []
    ) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.keyEquivalentModifierMask = modifiers
        item.target = nil
        return item
    }

    private func buildWindow() {
        window = TingleWindow(
            contentRect: NSRect(x: 0, y: 0, width: 760, height: 640),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "Tingle"
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.minSize = NSSize(width: 600, height: 520)
        window.center()
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.shouldHandleSpace = { [weak self] in self?.shouldHandleForegroundSpace() == true }
        window.handleSpace = { [weak self] in
            _ = self?.coordinator.toggle(source: .foregroundSpace)
        }
        window.shouldHandleEscape = { [weak self] in
            self?.surface == .history
        }
        window.handleEscape = { [weak self] in
            _ = self?.requestShowCaptureSurface(
                activate: true,
                source: .escape
            )
        }
        window.shouldHandleReturn = { [weak self] in
            guard let self else { return false }
            return self.surface == .capture
                && self.coordinator.snapshot.state == .idle
                && self.latestInspirationID != nil
        }
        window.handleReturn = { [weak self] in
            self?.openLatestInspiration(nil)
        }

        captureView = TingleCaptureView(frame: .zero)
        captureView.recordButton.target = self
        captureView.recordButton.action = #selector(toggleRecording(_:))
        captureView.pauseButton.target = self
        captureView.pauseButton.action = #selector(togglePause(_:))
        captureView.retryButton.target = self
        captureView.retryButton.action = #selector(retryRuntime(_:))
        captureView.latestStatusButton.target = self
        captureView.latestStatusButton.action = #selector(openLatestInspiration(_:))

        surfacePageController = TingleSurfacePageController(captureView: captureView)
        surfacePageController.routingDelegate = self
        window.contentViewController = surfacePageController
    }

    private func ensureHistorySurface() {
        guard historyView == nil else { return }
        historyLifecycleGeneration += 1
        let view = buildHistoryView()
        surfacePageController.historyHostController.install(view)
    }

    private func installMemoryPressureObserver() {
        guard memoryPressureSource == nil else { return }
        let source = DispatchSource.makeMemoryPressureSource(
            eventMask: [.warning, .critical],
            queue: .main
        )
        source.setEventHandler { [weak self] in
            self?.releaseHistoryForMemoryPressureIfSafe()
        }
        source.resume()
        memoryPressureSource = source
    }

    private func buildHistoryView() -> TingleHistoryView {
        let view = TingleHistoryView(api: api)
        view.delegate = self
        historyView = view
        return view
    }

    private func shouldHandleForegroundSpace() -> Bool {
        guard surface == .capture,
              surfacePageController.isSettled,
              !webState.editorFocused
        else { return false }
        if let responder = window.firstResponder as? NSTextView, responder.isEditable {
            return false
        }
        return true
    }

    @discardableResult
    private func requestShowHistorySurface(
        source: TingleSurfaceNavigationSource = .showHistoryCommand,
        activate: Bool = true,
        animated: Bool = true,
        completion: ((Bool) -> Void)? = nil
    ) -> Bool {
        if activate {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        }
        return surfacePageController.navigate(
            to: .history,
            source: source,
            animated: animated,
            completion: completion
        )
    }

    @discardableResult
    private func requestShowCaptureSurface(
        activate: Bool,
        source: TingleSurfaceNavigationSource = .showCaptureCommand,
        animated: Bool = true,
        completion: ((Bool) -> Void)? = nil
    ) -> Bool {
        if activate {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        }
        return surfacePageController.navigate(
            to: .capture,
            source: source,
            animated: animated,
            completion: completion
        )
    }

    private func presentUnsavedNavigationAlert() {
        guard !unsavedNavigationAlertVisible else { return }
        unsavedNavigationAlertVisible = true
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "这条灵感还有未保存的修改"
        alert.informativeText = "请先保存或撤销修改，再返回录音页。当前标题和文字会继续保留在编辑器中。"
        alert.addButton(withTitle: "继续编辑")
        alert.beginSheetModal(for: window) { [weak self] _ in
            self?.unsavedNavigationAlertVisible = false
        }
    }

    private func showWindowForUserActivation() {
        if surface == .history, webState.hasUnsavedChanges {
            historyReleaseWorkItem?.cancel()
            historyReleaseWorkItem = nil
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        _ = requestShowCaptureSurface(
            activate: true,
            source: .userActivation,
            animated: false
        )
    }

    private func resumeHistorySurface() {
        historyView?.resume()
    }

    private func suspendHistorySurface() {
        guard let historyView else { return }
        historyView.suspend()
        webState.playbackState = .idle
        scheduleHistoryRelease()
    }

    private func scheduleHistoryRelease() {
        historyReleaseWorkItem?.cancel()
        let generation = historyLifecycleGeneration
        let workItem = DispatchWorkItem { [weak self] in
            self?.releaseHistorySurface(generation: generation)
        }
        historyReleaseWorkItem = workItem
        DispatchQueue.main.asyncAfter(
            deadline: .now() + historyWarmGraceSeconds,
            execute: workItem
        )
    }

    private func releaseHistorySurface(generation: Int) {
        let activeRequestCount = historyView?.activeRequestCount ?? 0
        let context = TingleHistoryReleaseContext(
            scheduledGeneration: generation,
            currentGeneration: historyLifecycleGeneration,
            captureSurfaceVisible: surface == .capture,
            hasUnsavedChanges: webState.hasUnsavedChanges,
            destructiveConfirmationActive: webState.destructiveConfirmationActive == true,
            queuedCommandCount: 0,
            inFlightCommandCount: activeRequestCount
        )
        guard context.canRelease, historyView?.canRelease != false else {
            if context.shouldRetry {
                scheduleHistoryRelease()
            }
            return
        }
        historyReleaseWorkItem = nil
        historyLifecycleGeneration += 1
        historyView?.suspend()
        window.makeFirstResponder(captureView)
        surfacePageController.historyHostController.removeInstalledContent()
        historyView = nil
        webState = TingleWebState(schema: "tingle.command_bridge.unavailable")
        UserDefaults.standard.set(
            ISO8601DateFormatter().string(from: Date()),
            forKey: "TingleHistoryReleasedAt"
        )
    }

    private func releaseHistoryForMemoryPressureIfSafe() {
        guard historyView != nil else { return }
        let generation = historyLifecycleGeneration
        let context = TingleHistoryReleaseContext(
            scheduledGeneration: generation,
            currentGeneration: historyLifecycleGeneration,
            captureSurfaceVisible: surface == .capture,
            hasUnsavedChanges: webState.hasUnsavedChanges,
            destructiveConfirmationActive: webState.destructiveConfirmationActive == true,
            queuedCommandCount: 0,
            inFlightCommandCount: historyView?.activeRequestCount ?? 0
        )
        guard context.canRelease, historyView?.canRelease != false else { return }
        historyReleaseWorkItem?.cancel()
        historyReleaseWorkItem = nil
        releaseHistorySurface(generation: generation)
        UserDefaults.standard.set(
            ISO8601DateFormatter().string(from: Date()),
            forKey: "TingleHistoryReleasedForMemoryPressureAt"
        )
    }

    private func ensureRuntime() {
        guard !runtimeStarting else { return }
        runtimeStarting = true
        captureView.retryButton.isEnabled = false
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let result = self.ensureSharedRuntime()
            DispatchQueue.main.async {
                self.runtimeStarting = false
                self.captureView.retryButton.isEnabled = true
                switch result {
                case .success:
                    if self.runtimeRecoveryAttempt > 0 {
                        UserDefaults.standard.set(
                            ISO8601DateFormatter().string(from: Date()),
                            forKey: "TingleRuntimeRecoveredAt"
                        )
                    }
                    self.runtimeRecoveryWorkItem?.cancel()
                    self.runtimeRecoveryWorkItem = nil
                    self.runtimeRecoveryAttempt = 0
                    self.runtimeReady = true
                    self.coordinator.setRuntimeAvailable(true)
                    if self.latestVoiceRecordID != nil,
                       self.latestInspirationID == nil || self.latestInspirationIsProcessing {
                        self.latestRefreshWorkItem?.cancel()
                        self.latestRefreshWorkItem = nil
                        self.refreshLatestInspiration(generation: self.latestRefreshGeneration)
                    }
                    if self.surface == .history {
                        self.historyView?.resume()
                    }
                case let .failure(message):
                    self.runtimeReady = false
                    self.coordinator.setRuntimeAvailable(false)
                    self.historyView?.showRuntimeUnavailable(message)
                    if self.coordinator.snapshot.pendingSyncCount > 0 || self.latestVoiceRecordID != nil {
                        self.scheduleRuntimeRecovery()
                    }
                }
                self.captureView.update(snapshot: self.coordinator.snapshot)
                self.rebuildStatusMenu()
            }
        }
    }

    private enum RuntimeResult {
        case success
        case failure(String)
    }

    private func ensureSharedRuntime() -> RuntimeResult {
        let fileManager = FileManager.default
        let resources = Bundle.main.resourceURL
        let current = URL(fileURLWithPath: fileManager.currentDirectoryPath, isDirectory: true)
        let managers = [
            resources?.appendingPathComponent("ReaderRuntime/scripts/click_runtime_manager.py"),
            current.appendingPathComponent("scripts/click_runtime_manager.py"),
        ].compactMap { $0 }
        let pythons = [
            resources?.appendingPathComponent("ReaderRuntime/Python3.framework/Versions/3.9/bin/python3.9"),
            current.appendingPathComponent(".venv-reader-api/bin/python"),
            URL(fileURLWithPath: "/usr/bin/python3"),
        ].compactMap { $0 }
        guard let manager = managers.first(where: { fileManager.fileExists(atPath: $0.path) }),
              let python = pythons.first(where: { fileManager.isExecutableFile(atPath: $0.path) })
        else {
            return .failure("Click Runtime 文件不完整，本地录音仍会保留")
        }
        let runtime = manager.deletingLastPathComponent().deletingLastPathComponent()
        let appSupport = fileManager.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/SentenceReader", isDirectory: true)
        let process = Process()
        process.executableURL = python
        process.arguments = [
            manager.path,
            "ensure",
            "--runtime", runtime.path,
            "--app-support", appSupport.path,
            "--host", "0.0.0.0",
            "--port", "18180",
            "--expected-contract", "click.reader_runtime.v1",
            "--minimum-revision", "3",
            "--capability", "voice.inbox.v2",
            "--capability", "voice.processing_jobs.v1",
            "--capability", "voice.hermes_adapter.v1",
            "--capability", "tingle.inspirations.v1",
            "--capability", "tingle.permanent_delete.v1",
            "--capability", "tingle.tombstone_replay_guard.v1",
        ]
        let output = Pipe()
        process.standardOutput = output
        process.standardError = output
        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            return .failure("Click Runtime 启动失败，本地录音仍会保留")
        }
        let data = output.fileHandleForReading.readDataToEndOfFile()
        let detail = String(data: data, encoding: .utf8) ?? ""
        guard process.terminationStatus == 0 else {
            return .failure(detail.contains("blocked_incompatible_service")
                ? "发现不兼容的旧版 Click Runtime"
                : "Click Runtime 暂时不可用，本地录音仍会保留")
        }
        let semaphore = DispatchSemaphore(value: 0)
        var healthy = false
        api.health {
            healthy = $0
            semaphore.signal()
        }
        _ = semaphore.wait(timeout: .now() + 3)
        return healthy ? .success : .failure("Click Runtime 未通过兼容性检查")
    }

    @objc private func toggleRecording(_ sender: Any?) {
        _ = coordinator.toggle(source: .captureButton)
    }

    @objc private func togglePause(_ sender: Any?) {
        _ = coordinator.pauseOrResume(source: .captureButton)
    }

    @objc private func startRecordingFromApplicationMenu(_ sender: Any?) {
        _ = coordinator.start(source: .applicationMenu)
    }

    @objc private func togglePauseFromApplicationMenu(_ sender: Any?) {
        _ = coordinator.pauseOrResume(source: .applicationMenu)
    }

    @objc private func stopRecordingFromApplicationMenu(_ sender: Any?) {
        _ = coordinator.stopAndSave(source: .applicationMenu)
    }

    @objc private func toggleRecordingFromMenuBar(_ sender: Any?) {
        _ = coordinator.toggle(source: .menuBar)
    }

    @objc private func togglePauseFromMenuBar(_ sender: Any?) {
        _ = coordinator.pauseOrResume(source: .menuBar)
    }

    @objc private func showHistory(_ sender: Any?) {
        _ = requestShowHistorySurface(source: .showHistoryCommand)
    }

    @objc private func showCapture(_ sender: Any?) {
        _ = requestShowCaptureSurface(
            activate: true,
            source: .showCaptureCommand
        )
    }

    @objc private func openLatestInspiration(_ sender: Any?) {
        guard surface == .capture,
              coordinator.snapshot.state == .idle,
              let inspirationID = latestInspirationID
        else {
            NSSound.beep()
            return
        }
        _ = requestShowHistorySurface(
            source: .latestInspiration
        ) { [weak self] accepted in
            guard accepted else { return }
            self?.historyView?.openInspiration(inspirationID)
        }
    }

    @objc private func findInspirations(_ sender: Any?) {
        guard surface == .history || coordinator.snapshot.state == .idle else {
            NSSound.beep()
            return
        }
        _ = requestShowHistorySurface(source: .search) { [weak self] accepted in
            guard accepted else { return }
            self?.historyView?.focusSearchField()
        }
    }

    @objc private func saveInspirationChanges(_ sender: Any?) {
        historyView?.saveChanges()
    }

    @objc private func toggleOriginalAudio(_ sender: Any?) {
        historyView?.togglePlayback()
    }

    @objc private func archiveInspiration(_ sender: Any?) {
        historyView?.archiveSelected()
    }

    @objc private func restoreInspiration(_ sender: Any?) {
        historyView?.restoreSelected()
    }

    @objc private func deleteInspirationImmediately(_ sender: Any?) {
        historyView?.confirmAndDeleteSelected()
    }

    @objc private func showInspirations(_ sender: Any?) {
        _ = requestShowHistorySurface(
            source: .collectionCommand
        ) { [weak self] accepted in
            guard accepted else { return }
            self?.historyView?.showInspirations()
        }
    }

    @objc private func showRecycleBin(_ sender: Any?) {
        _ = requestShowHistorySurface(
            source: .collectionCommand
        ) { [weak self] accepted in
            guard accepted else { return }
            self?.historyView?.showRecycleBin()
        }
    }

    @objc private func reloadTingle(_ sender: Any?) {
        _ = requestShowHistorySurface(
            source: .collectionCommand
        ) { [weak self] accepted in
            guard accepted else { return }
            self?.historyView?.reload()
        }
    }

    @objc private func showTingleHelp(_ sender: Any?) {
        let alert = NSAlert()
        alert.messageText = "Tingle 帮助"
        alert.informativeText = "打开 Tingle 即可录音。向左轻扫或按 Command-2 打开历史，向右轻扫、按 Esc 或 Command-1 返回录音。归档内容可以在回收站恢复。"
        alert.addButton(withTitle: "好")
        alert.runModal()
    }

    private func beginLatestInspirationResolution(
        capture: ClickVoiceCaptureManifest,
        voiceRecordID: String
    ) {
        latestRefreshGeneration += 1
        latestRefreshWorkItem?.cancel()
        latestRefreshWorkItem = nil
        latestCaptureID = capture.captureID
        latestVoiceRecordID = voiceRecordID
        latestInspirationID = nil
        latestInspirationIsProcessing = true
        latestRawTranscriptText = nil
        latestRefreshAttempt = 0
        latestRefreshStartedAt = Date()
        captureView.updateLatestInspiration(
            TingleLatestInspirationPresentation(
                text: "录音已保存",
                state: .saved,
                actionable: false
            )
        )
        refreshLatestInspiration(generation: latestRefreshGeneration)
    }

    private func refreshLatestInspiration(generation: Int) {
        guard generation == latestRefreshGeneration,
              let voiceRecordID = latestVoiceRecordID
        else { return }
        latestRefreshWorkItem = nil
        guard runtimeReady else {
            scheduleLatestInspirationRefresh(generation: generation)
            return
        }
        api.getTingleInspiration(voiceRecordID: voiceRecordID) { [weak self] result in
            DispatchQueue.main.async {
                guard let self, generation == self.latestRefreshGeneration else { return }
                switch result {
                case let .success(inspiration):
                    self.applyLatestInspiration(inspiration, generation: generation)
                case .failure:
                    self.scheduleLatestInspirationRefresh(generation: generation)
                }
            }
        }
    }

    private func applyLatestInspiration(_ inspiration: [String: Any], generation: Int) {
        guard let inspirationID = inspiration["id"] as? String, !inspirationID.isEmpty else {
            scheduleLatestInspirationRefresh(generation: generation)
            return
        }
        latestInspirationID = inspirationID
        let state = String(describing: inspiration["state"] ?? "processing")
        let rawTranscript = rawTranscriptText(from: inspiration)
        if latestRawTranscriptText == nil, !rawTranscript.isEmpty {
            latestRawTranscriptText = rawTranscript
        }
        if let transcript = latestRawTranscriptText, !transcript.isEmpty {
            latestInspirationIsProcessing = false
            captureView.updateLatestInspiration(
                TingleLatestInspirationPresentation(
                    text: "查看刚才录音",
                    state: .transcribed,
                    actionable: true
                )
            )
            if latestTranscriptionNotificationArmed {
                latestTranscriptionNotificationArmed = false
                notifier.notifyTranscribed(text: transcript)
            }
            return
        }
        switch state {
        case "failed":
            latestInspirationIsProcessing = false
            captureView.updateLatestInspiration(
                TingleLatestInspirationPresentation(
                    text: "未识别到清晰语音，原音已保存",
                    state: .transcriptionFailed,
                    actionable: true
                )
            )
        case "processing":
            latestInspirationIsProcessing = true
            captureView.updateLatestInspiration(
                TingleLatestInspirationPresentation(
                    text: "录音已保存",
                    state: .saved,
                    actionable: true
                )
            )
            scheduleLatestInspirationRefresh(generation: generation)
        default:
            latestInspirationIsProcessing = false
            captureView.updateLatestInspiration(
                TingleLatestInspirationPresentation(
                    text: "未识别到清晰语音，原音已保存",
                    state: .transcriptionFailed,
                    actionable: true
                )
            )
        }
    }

    private func rawTranscriptText(from inspiration: [String: Any]) -> String {
        guard let versions = inspiration["transcript_versions"] as? [[String: Any]] else {
            return ""
        }
        for version in versions where String(describing: version["version_type"] ?? "") == "asr_raw" {
            let content = String(describing: version["content"] ?? "")
            return TingleTranscriptPresentation.normalized(content)
        }
        return ""
    }

    private func scheduleLatestInspirationRefresh(generation: Int) {
        guard generation == latestRefreshGeneration,
              latestRefreshWorkItem == nil,
              let startedAt = latestRefreshStartedAt,
              Date().timeIntervalSince(startedAt) < 600
        else { return }
        let delays: [TimeInterval] = [1, 2, 4, 8, 15, 30]
        let delay = delays[min(latestRefreshAttempt, delays.count - 1)]
        latestRefreshAttempt += 1
        let workItem = DispatchWorkItem { [weak self] in
            self?.refreshLatestInspiration(generation: generation)
        }
        latestRefreshWorkItem = workItem
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: workItem)
    }

    private func clearLatestInspirationContext() {
        latestRefreshGeneration += 1
        latestRefreshWorkItem?.cancel()
        latestRefreshWorkItem = nil
        latestCaptureID = nil
        latestVoiceRecordID = nil
        latestInspirationID = nil
        latestInspirationIsProcessing = false
        latestRawTranscriptText = nil
        latestTranscriptionNotificationArmed = false
        latestRefreshAttempt = 0
        latestRefreshStartedAt = nil
        captureView.updateLatestInspiration(nil)
    }

    func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        let action = menuItem.action
        let snapshot = coordinator.snapshot
        let webCommands = TingleCommandAvailability.evaluate(webState: webState)
        let acceptsSpace = shouldHandleForegroundSpace()

        switch action {
        case #selector(startRecordingFromApplicationMenu(_:)):
            return snapshot.canStart && acceptsSpace
        case #selector(togglePauseFromApplicationMenu(_:)):
            menuItem.title = snapshot.state == .paused ? "继续录音" : "暂停录音"
            return snapshot.canPause || snapshot.canResume
        case #selector(stopRecordingFromApplicationMenu(_:)):
            return snapshot.canStopAndSave && acceptsSpace
        case #selector(showCapture(_:)):
            return surface == .history
        case #selector(showHistory(_:)):
            return surface == .capture && snapshot.state == .idle
        case #selector(findInspirations(_:)):
            return surface == .history || snapshot.state == .idle
        case #selector(saveInspirationChanges(_:)):
            return surface == .history && historyView != nil && webCommands.canSaveChanges
        case #selector(toggleOriginalAudio(_:)):
            menuItem.title = webState.playbackState == .playing ? "暂停原音" : "播放原音"
            return surface == .history && historyView != nil && webCommands.canTogglePlayback
        case #selector(archiveInspiration(_:)):
            return surface == .history && historyView != nil && webCommands.canArchive
        case #selector(restoreInspiration(_:)):
            return surface == .history && historyView != nil && webCommands.canRestore
        case #selector(deleteInspirationImmediately(_:)):
            return surface == .history && historyView != nil && webCommands.canDeletePermanently
        case #selector(showInspirations(_:)), #selector(showRecycleBin(_:)):
            return surface == .history || snapshot.state == .idle
        case #selector(reloadTingle(_:)):
            return runtimeReady
        default:
            return true
        }
    }

    @objc private func retryRuntime(_ sender: Any?) {
        runtimeRecoveryWorkItem?.cancel()
        runtimeRecoveryWorkItem = nil
        runtimeRecoveryAttempt = 0
        ensureRuntime()
    }

    private func scheduleRuntimeRecovery() {
        guard runtimeRecoveryWorkItem == nil else { return }
        let delays: [TimeInterval] = [5, 10, 30, 60]
        let delay = delays[min(runtimeRecoveryAttempt, delays.count - 1)]
        runtimeRecoveryAttempt += 1
        let workItem = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.runtimeRecoveryWorkItem = nil
            self.ensureRuntime()
        }
        runtimeRecoveryWorkItem = workItem
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: workItem)
    }

    private func startTimerIfNeeded(for state: TingleRecordingState) {
        if state == .recording || state == .paused {
            guard timer == nil else { return }
            timer = Timer.scheduledTimer(withTimeInterval: 0.10, repeats: true) { [weak self] _ in
                guard let self else { return }
                self.coordinator.refreshElapsedTime()
                self.captureView.update(snapshot: self.coordinator.snapshot)
            }
        } else {
            timer?.invalidate()
            timer = nil
        }
    }

    private func showMicrophonePermissionAlert() {
        guard !microphoneAlertVisible, NSApp.isActive else { return }
        microphoneAlertVisible = true
        let alert = NSAlert()
        alert.messageText = "Tingle 需要麦克风权限"
        alert.informativeText = "录音尚未开始，也没有创建空记录。请在系统设置的隐私与安全性中允许 Tingle 使用麦克风。"
        alert.addButton(withTitle: "打开系统设置")
        alert.addButton(withTitle: "稍后")
        if alert.runModal() == .alertFirstButtonReturn,
           let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone") {
            NSWorkspace.shared.open(url)
        }
        microphoneAlertVisible = false
    }

    private func installStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        item.button?.image = NSImage(systemSymbolName: "mic.circle", accessibilityDescription: "Tingle 空闲")
        item.button?.toolTip = "Tingle"
        statusItem = item
        rebuildStatusMenu()
    }

    private func rebuildStatusMenu() {
        guard let statusItem else { return }
        let snapshot = coordinator.snapshot
        let state = snapshot.state
        let presentation = statusPresentation(for: snapshot)
        statusItem.button?.image = NSImage(
            systemSymbolName: presentation.symbol,
            accessibilityDescription: "Tingle \(presentation.label)"
        )
        statusItem.button?.contentTintColor = presentation.tint
        statusItem.button?.toolTip = "Tingle · \(presentation.label)"
        let menu = NSMenu(title: "Tingle")
        menu.autoenablesItems = false
        let stateItem = NSMenuItem(title: presentation.label, action: nil, keyEquivalent: "")
        stateItem.isEnabled = false
        menu.addItem(stateItem)
        menu.addItem(NSMenuItem.separator())
        menu.addItem(menuItem("打开 Tingle", action: #selector(openFromMenu(_:))))
        let recordingTitle = state == .recording || state == .paused ? "停止并保存录音" : "开始录音"
        let recordItem = menuItem(recordingTitle, action: #selector(toggleRecordingFromMenuBar(_:)))
        recordItem.isEnabled = state != .preparing && state != .finalizing && state != .savedLocally
        menu.addItem(recordItem)
        if state == .recording || state == .paused {
            menu.addItem(menuItem(
                state == .paused ? "继续录音" : "暂停录音",
                action: #selector(togglePauseFromMenuBar(_:))
            ))
        }
        menu.addItem(NSMenuItem.separator())
        let runtime = NSMenuItem(title: runtimeReady ? "Click Runtime 已连接" : "Click Runtime 未连接", action: nil, keyEquivalent: "")
        runtime.isEnabled = false
        menu.addItem(runtime)
        let shortcut = NSMenuItem(title: shortcutState.statusText, action: nil, keyEquivalent: "")
        shortcut.isEnabled = false
        menu.addItem(shortcut)
        if case .conflict = shortcutState {
            menu.addItem(menuItem("解决 Option-Space 冲突…", action: #selector(openKeyboardShortcutSettings(_:))))
            menu.addItem(menuItem("重新检测快捷键", action: #selector(retryGlobalShortcut(_:))))
        } else if !shortcutState.isActive {
            menu.addItem(menuItem("重新检测快捷键", action: #selector(retryGlobalShortcut(_:))))
        }
        if snapshot.pendingSyncCount > 0 {
            let pending = NSMenuItem(title: "\(snapshot.pendingSyncCount) 条本地录音等待同步", action: nil, keyEquivalent: "")
            pending.isEnabled = false
            menu.addItem(pending)
        }
        menu.addItem(NSMenuItem.separator())
        let login = menuItem("登录时启动", action: #selector(toggleLoginItem(_:)))
        login.state = loginItemController.isEnabled ? .on : .off
        menu.addItem(login)
        if loginItemController.statusText.contains("等待系统批准") {
            menu.addItem(menuItem("打开登录项设置…", action: #selector(openLoginItemSettings(_:))))
        }
        if let loginItemError {
            let error = NSMenuItem(title: loginItemError, action: nil, keyEquivalent: "")
            error.isEnabled = false
            menu.addItem(error)
        }
        menu.addItem(NSMenuItem.separator())
        menu.addItem(menuItem("退出 Tingle", action: #selector(quit(_:)), key: "q", modifiers: [.command]))
        statusItem.menu = menu
    }

    private func statusPresentation(
        for snapshot: TingleRecordingSnapshot
    ) -> (symbol: String, label: String, tint: NSColor?) {
        switch snapshot.state {
        case .preparing:
            return ("ellipsis.circle", "正在准备麦克风", .secondaryLabelColor)
        case .recording:
            return ("stop.circle.fill", "正在录音", .systemRed)
        case .paused:
            return ("pause.circle.fill", "录音已暂停", .systemOrange)
        case .finalizing:
            return ("hourglass.circle", "正在停止并保存", .secondaryLabelColor)
        case .savedLocally:
            return ("checkmark.circle.fill", "录音已保存在本机", .systemGreen)
        case .failed:
            return ("exclamationmark.circle.fill", "录音需要处理", .systemRed)
        case .idle where snapshot.pendingSyncCount > 0:
            return ("arrow.triangle.2.circlepath.circle", "录音已保存在本机，等待同步", .systemOrange)
        case .idle:
            return ("mic.circle", "空闲", nil)
        }
    }

    private func installGlobalShortcut() {
        shortcutState = globalShortcut.register()
        persistSystemDiagnostics()
        rebuildStatusMenu()
    }

    @objc private func retryGlobalShortcut(_ sender: Any?) {
        shortcutState = globalShortcut.retry()
        persistSystemDiagnostics()
        rebuildStatusMenu()
    }

    private func handleGlobalRecordingCommand() {
        let defaults = UserDefaults.standard
        let frontmostBefore = NSWorkspace.shared.frontmostApplication?.bundleIdentifier ?? "unknown"
        defaults.set(ISO8601DateFormatter().string(from: Date()), forKey: "TingleLastGlobalCommandAt")
        defaults.set(frontmostBefore, forKey: "TingleLastGlobalCommandFrontmostBefore")
        defaults.set(window?.isVisible == true, forKey: "TingleLastGlobalCommandWindowWasVisible")
        let disposition = coordinator.toggle(source: .globalHotKey)
        defaults.set(disposition.rawValue, forKey: "TingleLastGlobalCommandDisposition")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.20) {
            defaults.set(
                NSWorkspace.shared.frontmostApplication?.bundleIdentifier ?? "unknown",
                forKey: "TingleLastGlobalCommandFrontmostAfter"
            )
        }
    }

    private func persistSystemDiagnostics() {
        let defaults = UserDefaults.standard
        defaults.set(shortcutState.diagnosticName, forKey: "TingleGlobalShortcutState")
        defaults.set(shortcutState.diagnosticStatus, forKey: "TingleGlobalShortcutStatus")
        defaults.set("option_space", forKey: "TingleGlobalShortcut")
        defaults.removeObject(forKey: "TingleSpotlightCommandSpaceEnabled")
        defaults.set(loginItemController.statusText, forKey: "TingleLoginItemState")
        defaults.set(ISO8601DateFormatter().string(from: Date()), forKey: "TingleSystemDiagnosticsUpdatedAt")
    }

    @objc private func openKeyboardShortcutSettings(_ sender: Any?) {
        NSWorkspace.shared.open(TingleSystemSettings.keyboardShortcuts)
    }

    @objc private func toggleLoginItem(_ sender: Any?) {
        switch loginItemController.toggle() {
        case .success:
            loginItemError = nil
        case let .failure(error):
            loginItemError = "登录启动未更改：\(error.localizedDescription)"
        }
        persistSystemDiagnostics()
        rebuildStatusMenu()
    }

    @objc private func openLoginItemSettings(_ sender: Any?) {
        NSWorkspace.shared.open(TingleSystemSettings.loginItems)
    }

    @objc private func openFromMenu(_ sender: Any?) {
        showWindowForUserActivation()
    }

    func performTingleAppIntentCommand(_ command: TingleAppIntentCommand) -> TingleCommandDisposition {
        let disposition: TingleCommandDisposition
        switch command {
        case .openApp:
            showWindowForUserActivation()
            disposition = .accepted
        case .start:
            disposition = coordinator.start(source: .appIntent)
        case .pauseOrResume:
            disposition = coordinator.pauseOrResume(source: .appIntent)
        case .stopAndSave:
            disposition = coordinator.stopAndSave(source: .appIntent)
        case .getState:
            disposition = .noOp
        }
        let defaults = UserDefaults.standard
        defaults.set(command.rawValue, forKey: "TingleLastAppIntentCommand")
        defaults.set(disposition.rawValue, forKey: "TingleLastAppIntentDisposition")
        defaults.set(ISO8601DateFormatter().string(from: Date()), forKey: "TingleLastAppIntentAt")
        return disposition
    }

    func tingleAppIntentSnapshot() -> TingleRecordingSnapshot {
        coordinator.snapshot
    }

    @objc private func quit(_ sender: Any?) {
        NSApp.terminate(sender)
    }

    func tingleRecordingCoordinator(_ coordinator: TingleRecordingCoordinator, didChange snapshot: TingleRecordingSnapshot) {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            let defaults = UserDefaults.standard
            defaults.set(snapshot.state.rawValue, forKey: "TingleRecordingState")
            defaults.set(snapshot.syncState.rawValue, forKey: "TingleRecordingSyncState")
            defaults.set(snapshot.pendingSyncCount, forKey: "TinglePendingSyncCount")
            defaults.set(ISO8601DateFormatter().string(from: Date()), forKey: "TingleRecordingDiagnosticsUpdatedAt")
            if snapshot.syncState == .pending, snapshot.pendingSyncCount > 0 {
                defaults.set(
                    ISO8601DateFormatter().string(from: Date()),
                    forKey: "TingleLastPendingSyncAt"
                )
            }
            if snapshot.state == .preparing, self.latestCaptureID != nil {
                self.clearLatestInspirationContext()
            }
            self.captureView.update(snapshot: snapshot)
            self.startTimerIfNeeded(for: snapshot.state)
            self.rebuildStatusMenu()
            if snapshot.state == .failed,
               snapshot.errorMessage == ClickVoiceRecorderError.microphoneDenied.localizedDescription {
                if NSApp.isActive {
                    self.showMicrophonePermissionAlert()
                } else if self.lastFailureNotification != snapshot.errorMessage {
                    self.lastFailureNotification = snapshot.errorMessage
                    self.notifier.notifyFailure("请在系统设置中允许 Tingle 使用麦克风；没有创建空录音。")
                }
            } else if snapshot.state != .failed {
                self.lastFailureNotification = nil
            }
        }
    }

    func tingleRecordingCoordinator(_ coordinator: TingleRecordingCoordinator, didSaveLocally capture: ClickVoiceCaptureManifest) {
        latestCaptureID = capture.captureID
        latestVoiceRecordID = nil
        latestInspirationID = nil
        latestInspirationIsProcessing = false
        latestRawTranscriptText = nil
        latestTranscriptionNotificationArmed = true
        latestRefreshWorkItem?.cancel()
        latestRefreshWorkItem = nil
        captureView.updateLatestInspiration(
            TingleLatestInspirationPresentation(
                text: "录音已保存",
                state: .saved,
                actionable: false
            )
        )
        notifier.notifySaved(duration: capture.durationSeconds ?? 0)
    }

    func tingleRecordingCoordinator(
        _ coordinator: TingleRecordingCoordinator,
        didUpload capture: ClickVoiceCaptureManifest,
        voiceRecordID: String
    ) {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.beginLatestInspirationResolution(
                capture: capture,
                voiceRecordID: voiceRecordID
            )
            if self.surface == .history {
                self.historyView?.reload()
            }
        }
    }

    func tingleRecordingCoordinatorNeedsRuntimeRecovery(_ coordinator: TingleRecordingCoordinator) {
        DispatchQueue.main.async { [weak self] in
            UserDefaults.standard.set(
                ISO8601DateFormatter().string(from: Date()),
                forKey: "TingleRuntimeRecoveryRequestedAt"
            )
            self?.runtimeReady = false
            self?.scheduleRuntimeRecovery()
            self?.rebuildStatusMenu()
        }
    }

    func tingleHistoryViewDidRequestCapture(_ historyView: TingleHistoryView) {
        _ = requestShowCaptureSurface(
            activate: true,
            source: .historyBack
        )
    }

    func tingleHistoryView(
        _ historyView: TingleHistoryView,
        didChange state: TingleWebState
    ) {
        guard historyView === self.historyView else { return }
        webState = state
        NSApp.mainMenu?.update()
    }

    func tingleSurfacePageController(
        _ pageController: TingleSurfacePageController,
        decisionFrom sourcePage: TingleSurfacePage,
        to targetPage: TingleSurfacePage,
        source: TingleSurfaceNavigationSource
    ) -> TingleSurfaceNavigationDecision {
        guard coordinator.snapshot.state == .idle else {
            if source != .trackpad {
                NSSound.beep()
            }
            return .blocked(.recording)
        }
        if targetPage == .capture, webState.hasUnsavedChanges {
            presentUnsavedNavigationAlert()
            return .blocked(.unsavedEdit)
        }
        if targetPage == .capture,
           webState.destructiveConfirmationActive == true {
            NSSound.beep()
            return .blocked(.destructiveConfirmation)
        }
        return .allowed
    }

    func tingleSurfacePageController(
        _ pageController: TingleSurfacePageController,
        didCommit page: TingleSurfacePage,
        source: TingleSurfaceNavigationSource
    ) {
        switch page {
        case .capture:
            suspendHistorySurface()
        case .history:
            historyReleaseWorkItem?.cancel()
            historyReleaseWorkItem = nil
        }
        NSApp.mainMenu?.update()
    }

    func tingleSurfacePageController(
        _ pageController: TingleSurfacePageController,
        didSettleAt page: TingleSurfacePage,
        source: TingleSurfaceNavigationSource,
        committed: Bool
    ) {
        if page == .history, committed {
            ensureHistorySurface()
            if runtimeReady {
                resumeHistorySurface()
            } else {
                ensureRuntime()
            }
        }
        if page == .capture, committed {
            window.makeFirstResponder(captureView)
        }
        NSApp.mainMenu?.update()
    }
}


@main
struct ClickVoiceApplicationMain {
    static func main() {
        let application = NSApplication.shared
        let delegate = ClickVoiceAppDelegate()
        application.setActivationPolicy(.regular)
        application.delegate = delegate
        application.run()
    }
}
