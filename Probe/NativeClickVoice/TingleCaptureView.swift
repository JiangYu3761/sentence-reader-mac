import AppKit


enum TingleLatestInspirationState {
    case saved
    case transcribed
    case transcriptionFailed
}


struct TingleLatestInspirationPresentation {
    let text: String
    let state: TingleLatestInspirationState
    let actionable: Bool
}


enum TingleTranscriptPresentation {
    static func normalized(_ text: String) -> String {
        text.split(whereSeparator: \.isWhitespace).joined(separator: " ")
    }

    static func snippet(_ text: String, maxCharacters: Int) -> String {
        let content = normalized(text)
        guard content.count > maxCharacters else { return content }
        return String(content.prefix(maxCharacters)) + "…"
    }
}


final class TingleWindow: NSWindow {
    var shouldHandleSpace: (() -> Bool)?
    var handleSpace: (() -> Void)?
    var shouldHandleEscape: (() -> Bool)?
    var handleEscape: (() -> Void)?
    var shouldHandleReturn: (() -> Bool)?
    var handleReturn: (() -> Void)?

    override func sendEvent(_ event: NSEvent) {
        let modifiers = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        if event.type == .keyDown,
           event.keyCode == 53,
           modifiers.isEmpty,
           !event.isARepeat,
           shouldHandleEscape?() == true {
            handleEscape?()
            return
        }
        if event.type == .keyDown,
           event.keyCode == 36 || event.keyCode == 76,
           modifiers.isEmpty,
           !event.isARepeat,
           shouldHandleReturn?() == true {
            handleReturn?()
            return
        }
        super.sendEvent(event)
    }

    override func keyDown(with event: NSEvent) {
        let modifiers = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        if event.keyCode == 49,
           modifiers.isEmpty,
           !event.isARepeat,
           shouldHandleSpace?() == true {
            handleSpace?()
            return
        }
        super.keyDown(with: event)
    }
}


final class TinglePrimaryRecordButton: NSButton {
    private var pointerInside = false
    private var trackingAreaReference: NSTrackingArea?

    var isRecordingAppearance = false {
        didSet {
            guard oldValue != isRecordingAppearance else { return }
            updateAppearance()
        }
    }

    override var isEnabled: Bool {
        didSet {
            guard oldValue != isEnabled else { return }
            updateAppearance()
        }
    }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        isBordered = false
        bezelStyle = .regularSquare
        imagePosition = .imageOnly
        focusRingType = .exterior
        setButtonType(.momentaryChange)
        wantsLayer = true
        layer?.masksToBounds = true
        layer?.backgroundColor = NSColor.clear.cgColor
        translatesAutoresizingMaskIntoConstraints = false
        updateAppearance()
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let trackingAreaReference {
            removeTrackingArea(trackingAreaReference)
        }
        let tracking = NSTrackingArea(
            rect: bounds,
            options: [.activeInKeyWindow, .mouseEnteredAndExited, .inVisibleRect],
            owner: self,
            userInfo: nil
        )
        addTrackingArea(tracking)
        trackingAreaReference = tracking
    }

    override func mouseEntered(with event: NSEvent) {
        pointerInside = true
        updateAppearance()
    }

    override func mouseExited(with event: NSEvent) {
        pointerInside = false
        updateAppearance()
    }

    override func draw(_ dirtyRect: NSRect) {
        let diameter = min(bounds.width, bounds.height)
        let circle = NSRect(
            x: bounds.midX - diameter / 2,
            y: bounds.midY - diameter / 2,
            width: diameter,
            height: diameter
        )
        let base = NSColor.systemRed
        let color = pointerInside && isEnabled ? base.blended(withFraction: 0.10, of: .white) ?? base : base
        color.withAlphaComponent(isEnabled ? 1 : 0.52).setFill()
        NSBezierPath(ovalIn: circle).fill()
        super.draw(dirtyRect)
    }

    private func updateAppearance() {
        needsDisplay = true
        let symbol = isRecordingAppearance ? "stop.fill" : "mic.fill"
        let description = isRecordingAppearance ? "停止并保存录音" : "开始录音"
        let configuration = NSImage.SymbolConfiguration(pointSize: isRecordingAppearance ? 36 : 42, weight: .medium)
        image = NSImage(systemSymbolName: symbol, accessibilityDescription: description)?.withSymbolConfiguration(configuration)
        contentTintColor = .white
        toolTip = description
        setAccessibilityLabel(description)
        setAccessibilityRole(.button)
    }
}


final class TingleCaptureView: NSView {
    let recordButton = TinglePrimaryRecordButton(frame: .zero)
    let pauseButton = NSButton()
    let retryButton = NSButton()
    let latestStatusButton = NSButton()

    private let timerLabel = NSTextField(labelWithString: "00:00")
    private var latestInspiration: TingleLatestInspirationPresentation?
    private var lastSnapshot: TingleRecordingSnapshot?

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        translatesAutoresizingMaskIntoConstraints = false
        wantsLayer = true
        layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
        build()
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func update(snapshot: TingleRecordingSnapshot) {
        lastSnapshot = snapshot
        let active = snapshot.state == .recording || snapshot.state == .paused
        let busy = snapshot.state == .preparing || snapshot.state == .finalizing || snapshot.state == .savedLocally
        recordButton.isRecordingAppearance = active || snapshot.state == .finalizing
        recordButton.isEnabled = !busy

        pauseButton.alphaValue = active ? 1 : 0
        pauseButton.isEnabled = active
        pauseButton.setAccessibilityHidden(!active)
        let pauseSymbol = snapshot.state == .paused ? "play.fill" : "pause.fill"
        let pauseDescription = snapshot.state == .paused ? "继续录音" : "暂停录音"
        pauseButton.image = NSImage(systemSymbolName: pauseSymbol, accessibilityDescription: pauseDescription)?
            .withSymbolConfiguration(NSImage.SymbolConfiguration(pointSize: 17, weight: .medium))
        pauseButton.toolTip = pauseDescription
        pauseButton.setAccessibilityLabel(pauseDescription)

        let seconds = max(0, Int(snapshot.elapsedSeconds.rounded(.down)))
        timerLabel.stringValue = String(format: "%02d:%02d", seconds / 60, seconds % 60)

        renderStatus(snapshot)
        retryButton.alphaValue = snapshot.syncState == .failed || snapshot.pendingSyncCount > 0 ? 1 : 0
        retryButton.isEnabled = retryButton.alphaValue == 1
    }

    func updateLatestInspiration(_ presentation: TingleLatestInspirationPresentation?) {
        latestInspiration = presentation
        if let lastSnapshot {
            renderStatus(lastSnapshot)
        }
    }

    private func renderStatus(_ snapshot: TingleRecordingSnapshot) {
        var text = ""
        var color = NSColor.secondaryLabelColor
        var actionable = false
        switch snapshot.state {
        case .idle:
            if let latestInspiration {
                text = latestInspiration.text
                actionable = latestInspiration.actionable
                switch latestInspiration.state {
                case .saved:
                    color = .systemGreen
                case .transcribed:
                    color = .labelColor
                case .transcriptionFailed:
                    color = .systemOrange
                }
            } else if snapshot.pendingSyncCount > 0 {
                text = "\(snapshot.pendingSyncCount) 条录音已安全保存在本机，等待同步"
            } else {
                text = ""
            }
        case .preparing:
            text = "正在准备麦克风..."
        case .recording:
            text = "正在录音"
        case .paused:
            text = "已暂停"
            color = .systemYellow
        case .finalizing:
            text = "正在保存原始音频..."
        case .savedLocally:
            text = snapshot.syncState == .pending
                ? "已保存 \(timerLabel.stringValue)，等待同步"
                : "已保存 \(timerLabel.stringValue)"
            color = .systemGreen
        case .failed:
            text = snapshot.errorMessage ?? "录音失败"
            color = .systemRed
        }
        latestStatusButton.attributedTitle = NSAttributedString(
            string: text,
            attributes: [
                .font: NSFont.systemFont(ofSize: 13, weight: .regular),
                .foregroundColor: color,
            ]
        )
        latestStatusButton.toolTip = actionable ? "打开刚录下的灵感" : (text.isEmpty ? nil : text)
        latestStatusButton.setAccessibilityLabel(text)
        latestStatusButton.setAccessibilityHelp(actionable ? "打开刚录下的灵感详情" : "")
        latestStatusButton.isEnabled = actionable
    }

    private func build() {
        let brand = NSTextField(labelWithString: "Tingle")
        brand.font = .systemFont(ofSize: 18, weight: .semibold)
        brand.textColor = .labelColor
        brand.translatesAutoresizingMaskIntoConstraints = false

        configureIconButton(retryButton, symbol: "arrow.clockwise", description: "重试同步")

        timerLabel.font = .monospacedDigitSystemFont(ofSize: 34, weight: .regular)
        timerLabel.textColor = .labelColor
        timerLabel.alignment = .center
        timerLabel.translatesAutoresizingMaskIntoConstraints = false
        timerLabel.setContentCompressionResistancePriority(.required, for: .horizontal)

        pauseButton.isBordered = true
        pauseButton.bezelStyle = .circular
        pauseButton.imagePosition = .imageOnly
        pauseButton.focusRingType = .exterior
        pauseButton.translatesAutoresizingMaskIntoConstraints = false
        pauseButton.alphaValue = 0
        pauseButton.isEnabled = false
        pauseButton.setAccessibilityHidden(true)

        latestStatusButton.isBordered = false
        latestStatusButton.bezelStyle = .regularSquare
        latestStatusButton.alignment = .center
        latestStatusButton.lineBreakMode = .byTruncatingTail
        latestStatusButton.focusRingType = .exterior
        latestStatusButton.translatesAutoresizingMaskIntoConstraints = false
        latestStatusButton.setAccessibilityRole(.button)

        let center = NSStackView(views: [timerLabel, recordButton, pauseButton, latestStatusButton, retryButton])
        center.orientation = .vertical
        center.alignment = .centerX
        center.spacing = 18
        center.setCustomSpacing(40, after: timerLabel)
        center.setCustomSpacing(14, after: recordButton)
        center.setCustomSpacing(16, after: pauseButton)
        center.setCustomSpacing(8, after: latestStatusButton)
        center.translatesAutoresizingMaskIntoConstraints = false

        addSubview(brand)
        addSubview(center)

        NSLayoutConstraint.activate([
            brand.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 28),
            brand.topAnchor.constraint(equalTo: safeAreaLayoutGuide.topAnchor, constant: 18),

            center.centerXAnchor.constraint(equalTo: centerXAnchor),
            center.centerYAnchor.constraint(equalTo: centerYAnchor, constant: 14),
            center.leadingAnchor.constraint(greaterThanOrEqualTo: leadingAnchor, constant: 36),
            center.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor, constant: -36),

            timerLabel.widthAnchor.constraint(equalToConstant: 180),
            timerLabel.heightAnchor.constraint(equalToConstant: 44),
            recordButton.widthAnchor.constraint(equalToConstant: 136),
            recordButton.heightAnchor.constraint(equalToConstant: 136),
            pauseButton.widthAnchor.constraint(equalToConstant: 48),
            pauseButton.heightAnchor.constraint(equalToConstant: 48),
            latestStatusButton.widthAnchor.constraint(equalToConstant: 440),
            latestStatusButton.heightAnchor.constraint(equalToConstant: 24),
            retryButton.widthAnchor.constraint(equalToConstant: 32),
            retryButton.heightAnchor.constraint(equalToConstant: 32),
        ])
    }

    private func configureIconButton(_ button: NSButton, symbol: String, description: String) {
        button.image = NSImage(systemSymbolName: symbol, accessibilityDescription: description)?
            .withSymbolConfiguration(NSImage.SymbolConfiguration(pointSize: 17, weight: .regular))
        button.imagePosition = .imageOnly
        button.isBordered = false
        button.bezelStyle = .regularSquare
        button.focusRingType = .exterior
        button.toolTip = description
        button.translatesAutoresizingMaskIntoConstraints = false
        button.setAccessibilityLabel(description)
        button.setAccessibilityRole(.button)
    }
}
