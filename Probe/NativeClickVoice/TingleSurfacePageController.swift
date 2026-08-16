import AppKit
import Foundation


enum TingleSurfacePage: Int, CaseIterable {
    case capture = 0
    case history = 1

    var accessibilityLabel: String {
        switch self {
        case .capture:
            return "Tingle 录音"
        case .history:
            return "Tingle 灵感历史"
        }
    }
}


enum TingleSurfaceNavigationSource: String {
    case trackpad
    case showCaptureCommand = "show_capture_command"
    case showHistoryCommand = "show_history_command"
    case escape
    case search
    case latestInspiration = "latest_inspiration"
    case historyBack = "history_back"
    case collectionCommand = "collection_command"
    case userActivation = "user_activation"
    case windowClose = "window_close"
}


enum TingleSurfaceNavigationBlockReason: String {
    case recording
    case unsavedEdit = "unsaved_edit"
    case destructiveConfirmation = "destructive_confirmation"
}


struct TingleSurfaceNavigationDecision {
    let isAllowed: Bool
    let blockReason: TingleSurfaceNavigationBlockReason?

    static let allowed = TingleSurfaceNavigationDecision(
        isAllowed: true,
        blockReason: nil
    )

    static func blocked(
        _ reason: TingleSurfaceNavigationBlockReason
    ) -> TingleSurfaceNavigationDecision {
        TingleSurfaceNavigationDecision(isAllowed: false, blockReason: reason)
    }
}


protocol TingleSurfacePageControllerRoutingDelegate: AnyObject {
    func tingleSurfacePageController(
        _ pageController: TingleSurfacePageController,
        decisionFrom sourcePage: TingleSurfacePage,
        to targetPage: TingleSurfacePage,
        source: TingleSurfaceNavigationSource
    ) -> TingleSurfaceNavigationDecision

    func tingleSurfacePageController(
        _ pageController: TingleSurfacePageController,
        didCommit page: TingleSurfacePage,
        source: TingleSurfaceNavigationSource
    )

    func tingleSurfacePageController(
        _ pageController: TingleSurfacePageController,
        didSettleAt page: TingleSurfacePage,
        source: TingleSurfaceNavigationSource,
        committed: Bool
    )
}


enum TinglePageScrollRoute: Equatable {
    case pageNavigation
    case verticalContent
}


struct TinglePageScrollRouteLatch {
    private(set) var route: TinglePageScrollRoute?

    mutating func resolve(
        deltaX: CGFloat,
        deltaY: CGFloat,
        startsNewGesture: Bool
    ) -> TinglePageScrollRoute? {
        if startsNewGesture {
            route = nil
        } else if let route {
            return route
        }
        return begin(deltaX: deltaX, deltaY: deltaY)
    }

    mutating func begin(
        deltaX: CGFloat,
        deltaY: CGFloat
    ) -> TinglePageScrollRoute? {
        guard abs(deltaX) > 0.001 || abs(deltaY) > 0.001 else {
            return nil
        }
        let resolved: TinglePageScrollRoute = abs(deltaX) > abs(deltaY)
            ? .pageNavigation
            : .verticalContent
        route = resolved
        return resolved
    }

    mutating func reset() {
        route = nil
    }
}


final class TinglePageAwareVerticalScrollView: NSScrollView {
    private var routeLatch = TinglePageScrollRouteLatch()
    private var streamIsActive = false

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        configurePageArbitration()
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        configurePageArbitration()
    }

    override func scrollWheel(with event: NSEvent) {
        let modifiers = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        guard event.hasPreciseScrollingDeltas, modifiers.isEmpty else {
            routeLatch.reset()
            streamIsActive = false
            super.scrollWheel(with: event)
            return
        }

        let startsNewGesture = event.phase.contains(.began)
            || (!streamIsActive && event.momentumPhase.isEmpty)
        streamIsActive = true
        let route = routeLatch.resolve(
            deltaX: event.scrollingDeltaX,
            deltaY: event.scrollingDeltaY,
            startsNewGesture: startsNewGesture
        )

        switch route {
        case .pageNavigation?:
            if let nextResponder {
                nextResponder.scrollWheel(with: event)
            } else {
                super.scrollWheel(with: event)
            }
        case .verticalContent?:
            super.scrollWheel(with: event)
        case nil:
            super.scrollWheel(with: event)
        }

        if event.phase.contains(.ended) {
            streamIsActive = false
        }
        if event.phase.contains(.cancelled)
            || event.momentumPhase.contains(.ended)
            || event.momentumPhase.contains(.cancelled) {
            streamIsActive = false
            routeLatch.reset()
        }
    }

    private func configurePageArbitration() {
        hasHorizontalScroller = false
        horizontalScrollElasticity = .none
        usesPredominantAxisScrolling = true
    }
}


final class TingleHorizontalPagingScrollView: NSScrollView {
    var beginHorizontalInteraction: (() -> Bool)?
    var finishHorizontalInteraction: (() -> Void)?

    private var routeLatch = TinglePageScrollRouteLatch()
    private var streamIsActive = false
    private var streamIsAllowed = false
    private var momentumIsActive = false

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        configurePaging()
    }

    deinit {
        NotificationCenter.default.removeObserver(self)
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        configurePaging()
    }

    var isHorizontalInteractionActive: Bool {
        streamIsActive || momentumIsActive
    }

    override func scrollWheel(with event: NSEvent) {
        let modifiers = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        guard event.hasPreciseScrollingDeltas, modifiers.isEmpty else {
            super.scrollWheel(with: event)
            return
        }

        let startsNewGesture = event.phase.contains(.began)
            || (
                !streamIsActive
                    && event.momentumPhase.isEmpty
                    && abs(event.scrollingDeltaX) > abs(event.scrollingDeltaY)
            )
        let route = routeLatch.resolve(
            deltaX: event.scrollingDeltaX,
            deltaY: event.scrollingDeltaY,
            startsNewGesture: startsNewGesture
        )
        guard let route else {
            return
        }
        guard route == .pageNavigation else {
            super.scrollWheel(with: event)
            return
        }

        if startsNewGesture {
            momentumIsActive = false
            streamIsActive = true
            streamIsAllowed = beginHorizontalInteraction?() ?? true
        }

        if event.momentumPhase.contains(.began)
            || event.momentumPhase.contains(.changed) {
            momentumIsActive = true
        }

        if streamIsAllowed {
            super.scrollWheel(with: event)
        }

        if event.momentumPhase.contains(.ended)
            || event.momentumPhase.contains(.cancelled) {
            momentumIsActive = false
            if streamIsAllowed {
                finishInteraction()
            } else {
                resetInteraction()
            }
        } else if !streamIsAllowed,
                  event.phase.contains(.ended)
                    || event.phase.contains(.cancelled) {
            resetInteraction()
        }
    }

    @objc private func didEndLiveScroll(_ notification: Notification) {
        guard streamIsActive else { return }
        finishInteraction()
    }

    private func finishInteraction() {
        let wasAllowed = streamIsAllowed
        resetInteraction()
        if wasAllowed {
            finishHorizontalInteraction?()
        }
    }

    private func resetInteraction() {
        streamIsActive = false
        streamIsAllowed = false
        momentumIsActive = false
        routeLatch.reset()
    }

    private func configurePaging() {
        drawsBackground = false
        borderType = .noBorder
        hasHorizontalScroller = false
        hasVerticalScroller = false
        autohidesScrollers = true
        horizontalScrollElasticity = .none
        verticalScrollElasticity = .none
        usesPredominantAxisScrolling = true
        automaticallyAdjustsContentInsets = false
        contentInsets = NSEdgeInsets()
        scrollerInsets = NSEdgeInsets()
        contentView.wantsLayer = true
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(didEndLiveScroll(_:)),
            name: NSScrollView.didEndLiveScrollNotification,
            object: self
        )
    }
}


private final class TingleHistoryLoadingShellView: NSView {
    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        translatesAutoresizingMaskIntoConstraints = false
        wantsLayer = true
        layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
        setAccessibilityLabel("正在载入灵感")

        let header = NSView()
        header.translatesAutoresizingMaskIntoConstraints = false

        let backImage = NSImageView()
        backImage.image = NSImage(
            systemSymbolName: "chevron.left",
            accessibilityDescription: nil
        )
        backImage.contentTintColor = .secondaryLabelColor
        backImage.translatesAutoresizingMaskIntoConstraints = false

        let title = NSTextField(labelWithString: "灵感")
        title.font = .systemFont(ofSize: 18, weight: .semibold)
        title.translatesAutoresizingMaskIntoConstraints = false

        let status = NSTextField(labelWithString: "正在载入…")
        status.font = .systemFont(ofSize: 12)
        status.textColor = .secondaryLabelColor
        status.alignment = .right
        status.translatesAutoresizingMaskIntoConstraints = false

        header.addSubview(backImage)
        header.addSubview(title)
        header.addSubview(status)

        let headerSeparator = NSBox()
        headerSeparator.boxType = .separator
        headerSeparator.translatesAutoresizingMaskIntoConstraints = false

        let listPane = NSView()
        listPane.translatesAutoresizingMaskIntoConstraints = false

        let search = NSSearchField()
        search.placeholderString = "搜索灵感"
        search.isEnabled = false
        search.translatesAutoresizingMaskIntoConstraints = false

        let collection = NSSegmentedControl(
            labels: ["灵感", "回收站"],
            trackingMode: .selectOne,
            target: nil,
            action: nil
        )
        collection.selectedSegment = 0
        collection.isEnabled = false
        collection.translatesAutoresizingMaskIntoConstraints = false

        let listStatus = NSTextField(labelWithString: "正在载入灵感…")
        listStatus.font = .systemFont(ofSize: 13)
        listStatus.textColor = .secondaryLabelColor
        listStatus.alignment = .center
        listStatus.translatesAutoresizingMaskIntoConstraints = false

        listPane.addSubview(search)
        listPane.addSubview(collection)
        listPane.addSubview(listStatus)

        let bodySeparator = NSBox()
        bodySeparator.boxType = .separator
        bodySeparator.translatesAutoresizingMaskIntoConstraints = false

        let detailStatus = NSTextField(labelWithString: "正在载入…")
        detailStatus.font = .systemFont(ofSize: 13)
        detailStatus.textColor = .secondaryLabelColor
        detailStatus.translatesAutoresizingMaskIntoConstraints = false

        addSubview(header)
        addSubview(headerSeparator)
        addSubview(listPane)
        addSubview(bodySeparator)
        addSubview(detailStatus)

        NSLayoutConstraint.activate([
            header.topAnchor.constraint(equalTo: topAnchor),
            header.leadingAnchor.constraint(equalTo: leadingAnchor),
            header.trailingAnchor.constraint(equalTo: trailingAnchor),
            header.heightAnchor.constraint(equalToConstant: 64),
            backImage.leadingAnchor.constraint(equalTo: header.leadingAnchor, constant: 28),
            backImage.centerYAnchor.constraint(equalTo: header.centerYAnchor, constant: 7),
            backImage.widthAnchor.constraint(equalToConstant: 16),
            backImage.heightAnchor.constraint(equalToConstant: 16),
            title.leadingAnchor.constraint(equalTo: header.leadingAnchor, constant: 62),
            title.centerYAnchor.constraint(equalTo: backImage.centerYAnchor),
            status.leadingAnchor.constraint(greaterThanOrEqualTo: title.trailingAnchor, constant: 16),
            status.trailingAnchor.constraint(equalTo: header.trailingAnchor, constant: -18),
            status.centerYAnchor.constraint(equalTo: backImage.centerYAnchor),

            headerSeparator.topAnchor.constraint(equalTo: header.bottomAnchor),
            headerSeparator.leadingAnchor.constraint(equalTo: leadingAnchor),
            headerSeparator.trailingAnchor.constraint(equalTo: trailingAnchor),

            listPane.topAnchor.constraint(equalTo: headerSeparator.bottomAnchor),
            listPane.leadingAnchor.constraint(equalTo: leadingAnchor),
            listPane.bottomAnchor.constraint(equalTo: bottomAnchor),
            listPane.widthAnchor.constraint(equalToConstant: 300),
            search.topAnchor.constraint(equalTo: listPane.topAnchor, constant: 12),
            search.leadingAnchor.constraint(equalTo: listPane.leadingAnchor, constant: 12),
            search.trailingAnchor.constraint(equalTo: listPane.trailingAnchor, constant: -12),
            collection.topAnchor.constraint(equalTo: search.bottomAnchor, constant: 8),
            collection.leadingAnchor.constraint(equalTo: search.leadingAnchor),
            collection.trailingAnchor.constraint(equalTo: search.trailingAnchor),
            listStatus.topAnchor.constraint(equalTo: collection.bottomAnchor, constant: 28),
            listStatus.leadingAnchor.constraint(equalTo: listPane.leadingAnchor, constant: 12),
            listStatus.trailingAnchor.constraint(equalTo: listPane.trailingAnchor, constant: -12),

            bodySeparator.topAnchor.constraint(equalTo: headerSeparator.bottomAnchor),
            bodySeparator.leadingAnchor.constraint(equalTo: listPane.trailingAnchor),
            bodySeparator.bottomAnchor.constraint(equalTo: bottomAnchor),

            detailStatus.topAnchor.constraint(equalTo: headerSeparator.bottomAnchor, constant: 28),
            detailStatus.leadingAnchor.constraint(equalTo: bodySeparator.trailingAnchor, constant: 28),
        ])
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }
}


final class TingleSurfaceHostViewController: NSViewController {
    let page: TingleSurfacePage

    private let contentContainer = NSView()
    private var loadingShell: NSView?
    private(set) weak var installedContent: NSView?

    init(page: TingleSurfacePage, initialContent: NSView? = nil) {
        self.page = page
        super.init(nibName: nil, bundle: nil)

        let root = NSView(frame: NSRect(x: 0, y: 0, width: 760, height: 640))
        root.autoresizingMask = [.width, .height]
        root.wantsLayer = true
        root.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
        root.setAccessibilityLabel(page.accessibilityLabel)
        view = root

        contentContainer.translatesAutoresizingMaskIntoConstraints = false
        root.addSubview(contentContainer)
        NSLayoutConstraint.activate([
            contentContainer.topAnchor.constraint(equalTo: root.topAnchor),
            contentContainer.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            contentContainer.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            contentContainer.bottomAnchor.constraint(equalTo: root.bottomAnchor),
        ])

        if page == .history {
            let shell = TingleHistoryLoadingShellView()
            loadingShell = shell
            contentContainer.addSubview(shell)
            NSLayoutConstraint.activate([
                shell.topAnchor.constraint(equalTo: contentContainer.topAnchor),
                shell.leadingAnchor.constraint(equalTo: contentContainer.leadingAnchor),
                shell.trailingAnchor.constraint(equalTo: contentContainer.trailingAnchor),
                shell.bottomAnchor.constraint(equalTo: contentContainer.bottomAnchor),
            ])
        }

        if let initialContent {
            install(initialContent)
        }
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func install(_ content: NSView) {
        if installedContent === content {
            loadingShell?.isHidden = true
            return
        }
        installedContent?.removeFromSuperview()
        installedContent = content
        content.translatesAutoresizingMaskIntoConstraints = false
        contentContainer.addSubview(content)
        NSLayoutConstraint.activate([
            content.topAnchor.constraint(equalTo: contentContainer.topAnchor),
            content.leadingAnchor.constraint(equalTo: contentContainer.leadingAnchor),
            content.trailingAnchor.constraint(equalTo: contentContainer.trailingAnchor),
            content.bottomAnchor.constraint(equalTo: contentContainer.bottomAnchor),
        ])
        loadingShell?.isHidden = true
    }

    func removeInstalledContent() {
        installedContent?.removeFromSuperview()
        installedContent = nil
        loadingShell?.isHidden = false
    }
}


final class TingleSurfacePageController: NSViewController {
    weak var routingDelegate: TingleSurfacePageControllerRoutingDelegate?

    let captureHostController: TingleSurfaceHostViewController
    let historyHostController: TingleSurfaceHostViewController

    private struct NavigationMotion {
        let id: String
        let sourcePage: TingleSurfacePage
        var requestedTarget: TingleSurfacePage
        let source: TingleSurfaceNavigationSource
        let startedAt: TimeInterval
        let completion: ((Bool) -> Void)?
    }

    private let pagingScrollView = TingleHorizontalPagingScrollView()
    private let documentStrip = NSView()
    private var settledPage = TingleSurfacePage.capture
    private var pageSize = NSSize.zero
    private var navigationMotion: NavigationMotion?
    private var snapGeneration = 0
    private var snapTargetPage: TingleSurfacePage?

    init(captureView: NSView) {
        captureHostController = TingleSurfaceHostViewController(
            page: .capture,
            initialContent: captureView
        )
        historyHostController = TingleSurfaceHostViewController(page: .history)
        super.init(nibName: nil, bundle: nil)

        let container = NSView(frame: NSRect(x: 0, y: 0, width: 760, height: 640))
        container.autoresizingMask = [.width, .height]
        container.wantsLayer = true
        container.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
        view = container

        pagingScrollView.translatesAutoresizingMaskIntoConstraints = false
        documentStrip.wantsLayer = true
        documentStrip.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
        pagingScrollView.documentView = documentStrip
        container.addSubview(pagingScrollView)
        NSLayoutConstraint.activate([
            pagingScrollView.topAnchor.constraint(equalTo: container.topAnchor),
            pagingScrollView.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            pagingScrollView.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            pagingScrollView.bottomAnchor.constraint(equalTo: container.bottomAnchor),
        ])

        addChild(captureHostController)
        addChild(historyHostController)
        documentStrip.addSubview(captureHostController.view)
        documentStrip.addSubview(historyHostController.view)
        captureHostController.view.autoresizingMask = [.width, .height]
        historyHostController.view.autoresizingMask = [.width, .height]

        pagingScrollView.beginHorizontalInteraction = { [weak self] in
            self?.beginTrackpadInteraction() ?? false
        }
        pagingScrollView.finishHorizontalInteraction = { [weak self] in
            self?.settleTrackpadInteraction()
        }
        view.setAccessibilityLabel(TingleSurfacePage.capture.accessibilityLabel)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func viewDidLayout() {
        super.viewDidLayout()
        updatePageGeometry()
    }

    let pageCount = 2

    var selectedPage: TingleSurfacePage {
        settledPage
    }

    var isSettled: Bool {
        guard snapTargetPage == nil,
              !pagingScrollView.isHorizontalInteractionActive
        else { return false }
        return abs(currentModelOffset() - offset(for: settledPage)) < 0.5
    }

    @discardableResult
    func navigate(
        to targetPage: TingleSurfacePage,
        source: TingleSurfaceNavigationSource,
        animated: Bool = true,
        completion: ((Bool) -> Void)? = nil
    ) -> Bool {
        let sourcePage = selectedPage
        if sourcePage != targetPage {
            let decision = routingDelegate?.tingleSurfacePageController(
                self,
                decisionFrom: sourcePage,
                to: targetPage,
                source: source
            ) ?? .allowed
            guard decision.isAllowed else {
                persistBlockedSummary(
                    from: sourcePage,
                    to: targetPage,
                    source: source,
                    reason: decision.blockReason
                )
                completion?(false)
                return false
            }
        }

        cancelCurrentMotion(result: "interrupted")
        navigationMotion = NavigationMotion(
            id: UUID().uuidString.lowercased(),
            sourcePage: sourcePage,
            requestedTarget: targetPage,
            source: source,
            startedAt: ProcessInfo.processInfo.systemUptime,
            completion: completion
        )
        startSnap(
            to: targetPage,
            animated: animated && !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        )
        return true
    }

    private func beginTrackpadInteraction() -> Bool {
        cancelCurrentMotion(result: "interrupted")

        let sourcePage = settledPage
        let targetPage = oppositePage(from: sourcePage)
        let decision = routingDelegate?.tingleSurfacePageController(
            self,
            decisionFrom: sourcePage,
            to: targetPage,
            source: .trackpad
        ) ?? .allowed
        guard decision.isAllowed else {
            setModelOffset(offset(for: sourcePage))
            persistBlockedSummary(
                from: sourcePage,
                to: targetPage,
                source: .trackpad,
                reason: decision.blockReason
            )
            return false
        }

        navigationMotion = NavigationMotion(
            id: UUID().uuidString.lowercased(),
            sourcePage: sourcePage,
            requestedTarget: targetPage,
            source: .trackpad,
            startedAt: ProcessInfo.processInfo.systemUptime,
            completion: nil
        )
        return true
    }

    private func settleTrackpadInteraction() {
        guard var motion = navigationMotion else { return }
        let targetPage = nearestPage(for: currentModelOffset())
        motion.requestedTarget = targetPage
        navigationMotion = motion
        startSnap(
            to: targetPage,
            animated: !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        )
    }

    private func oppositePage(from page: TingleSurfacePage) -> TingleSurfacePage {
        page == .capture ? .history : .capture
    }

    private func updatePageGeometry() {
        let nextSize = pagingScrollView.contentSize
        guard nextSize.width > 0, nextSize.height > 0 else { return }
        guard abs(nextSize.width - pageSize.width) > 0.5
                || abs(nextSize.height - pageSize.height) > 0.5
        else { return }

        let previousWidth = pageSize.width
        let previousOffset = currentVisibleOffset()
        let progress = previousWidth > 0
            ? min(max(previousOffset / previousWidth, 0), 1)
            : CGFloat(settledPage.rawValue)
        let interruptedTarget = snapTargetPage
        if interruptedTarget != nil {
            invalidateSnapAnimation(at: previousOffset)
        }

        pageSize = nextSize
        documentStrip.frame = NSRect(
            x: 0,
            y: 0,
            width: nextSize.width * 2,
            height: nextSize.height
        )
        captureHostController.view.frame = NSRect(
            x: 0,
            y: 0,
            width: nextSize.width,
            height: nextSize.height
        )
        historyHostController.view.frame = NSRect(
            x: nextSize.width,
            y: 0,
            width: nextSize.width,
            height: nextSize.height
        )
        setModelOffset(progress * nextSize.width)

        if let interruptedTarget, navigationMotion != nil {
            startSnap(
                to: interruptedTarget,
                animated: !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
            )
        } else if navigationMotion == nil,
                  !pagingScrollView.isHorizontalInteractionActive {
            setModelOffset(offset(for: settledPage))
        }
    }

    private func startSnap(to targetPage: TingleSurfacePage, animated: Bool) {
        view.layoutSubtreeIfNeeded()
        let targetOffset = offset(for: targetPage)
        snapGeneration += 1
        let generation = snapGeneration
        snapTargetPage = targetPage

        guard animated, abs(currentModelOffset() - targetOffset) >= 0.5 else {
            setModelOffset(targetOffset)
            finishSnap(generation: generation, targetPage: targetPage)
            return
        }

        NSAnimationContext.runAnimationGroup { _ in
            self.pagingScrollView.contentView.animator().setBoundsOrigin(
                NSPoint(x: targetOffset, y: 0)
            )
        } completionHandler: { [weak self] in
            DispatchQueue.main.async {
                self?.finishSnap(
                    generation: generation,
                    targetPage: targetPage
                )
            }
        }
    }

    private func finishSnap(
        generation: Int,
        targetPage: TingleSurfacePage
    ) {
        guard snapGeneration == generation,
              snapTargetPage == targetPage,
              let motion = navigationMotion
        else { return }

        setModelOffset(offset(for: targetPage))
        snapTargetPage = nil
        navigationMotion = nil

        let committed = targetPage != motion.sourcePage
        settledPage = targetPage
        view.setAccessibilityLabel(targetPage.accessibilityLabel)
        if committed {
            routingDelegate?.tingleSurfacePageController(
                self,
                didCommit: targetPage,
                source: motion.source
            )
        }
        persistSummary(
            motion: motion,
            finalPage: targetPage,
            result: committed ? "accepted" : "cancelled",
            blockReason: nil
        )
        routingDelegate?.tingleSurfacePageController(
            self,
            didSettleAt: targetPage,
            source: motion.source,
            committed: committed
        )
        motion.completion?(targetPage == motion.requestedTarget)
    }

    private func cancelCurrentMotion(result: String) {
        let motion = navigationMotion
        navigationMotion = nil
        invalidateSnapAnimation(at: currentVisibleOffset())
        guard let motion else { return }
        persistSummary(
            motion: motion,
            finalPage: settledPage,
            result: result,
            blockReason: nil
        )
        motion.completion?(false)
    }

    private func invalidateSnapAnimation(at visibleOffset: CGFloat) {
        snapGeneration += 1
        snapTargetPage = nil
        pagingScrollView.contentView.layer?.removeAllAnimations()
        setModelOffset(visibleOffset)
    }

    private func currentVisibleOffset() -> CGFloat {
        if snapTargetPage != nil,
           let presentationOffset = pagingScrollView.contentView.layer?
            .presentation()?.bounds.origin.x {
            return clampedOffset(presentationOffset)
        }
        return currentModelOffset()
    }

    private func currentModelOffset() -> CGFloat {
        clampedOffset(pagingScrollView.contentView.bounds.origin.x)
    }

    private func clampedOffset(_ value: CGFloat) -> CGFloat {
        min(max(value, 0), max(pageSize.width, 0))
    }

    private func offset(for page: TingleSurfacePage) -> CGFloat {
        CGFloat(page.rawValue) * pageSize.width
    }

    private func nearestPage(for horizontalOffset: CGFloat) -> TingleSurfacePage {
        guard pageSize.width > 0 else { return settledPage }
        return clampedOffset(horizontalOffset) >= pageSize.width / 2
            ? .history
            : .capture
    }

    private func setModelOffset(_ horizontalOffset: CGFloat) {
        let clipView = pagingScrollView.contentView
        clipView.setBoundsOrigin(
            NSPoint(x: clampedOffset(horizontalOffset), y: 0)
        )
        pagingScrollView.reflectScrolledClipView(clipView)
    }

    private func persistBlockedSummary(
        from sourcePage: TingleSurfacePage,
        to targetPage: TingleSurfacePage,
        source: TingleSurfaceNavigationSource,
        reason: TingleSurfaceNavigationBlockReason?
    ) {
        let motion = NavigationMotion(
            id: UUID().uuidString.lowercased(),
            sourcePage: sourcePage,
            requestedTarget: targetPage,
            source: source,
            startedAt: ProcessInfo.processInfo.systemUptime,
            completion: nil
        )
        persistSummary(
            motion: motion,
            finalPage: sourcePage,
            result: "blocked",
            blockReason: reason
        )
    }

    private func persistSummary(
        motion: NavigationMotion,
        finalPage: TingleSurfacePage,
        result: String,
        blockReason: TingleSurfaceNavigationBlockReason?
    ) {
        let completedAt = ProcessInfo.processInfo.systemUptime
        let summary: [String: Any] = [
            "transition_id": motion.id,
            "app_version": Bundle.main.object(
                forInfoDictionaryKey: "CFBundleShortVersionString"
            ) as? String ?? "",
            "app_build": Bundle.main.object(
                forInfoDictionaryKey: "CFBundleVersion"
            ) as? String ?? "",
            "command_source": motion.source.rawValue,
            "source_index": motion.sourcePage.rawValue,
            "requested_target_index": motion.requestedTarget.rawValue,
            "final_index": finalPage.rawValue,
            "result": result,
            "started_at_monotonic": motion.startedAt,
            "completed_at_monotonic": completedAt,
            "duration_seconds": max(0, completedAt - motion.startedAt),
            "unsaved_edit_block": blockReason == .unsavedEdit,
            "recording_state_block": blockReason == .recording,
            "block_reason": blockReason?.rawValue ?? "",
        ]
        UserDefaults.standard.set(summary, forKey: "TingleLastSurfaceTransition")
    }
}
