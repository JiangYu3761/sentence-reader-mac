import AppKit
import Carbon
import Foundation
import ServiceManagement
import UserNotifications


enum TingleSystemSettings {
    static let keyboardShortcuts = URL(
        string: "x-apple.systempreferences:com.apple.preference.keyboard?Shortcuts"
    )!
    static let loginItems = URL(
        string: "x-apple.systempreferences:com.apple.LoginItems-Settings.extension"
    )!
}


enum TingleGlobalShortcutState: Equatable {
    case notRegistered
    case active
    case conflict(OSStatus)
    case failed(OSStatus)

    var isActive: Bool {
        if case .active = self { return true }
        return false
    }

    var statusText: String {
        switch self {
        case .notRegistered:
            return "Option-Space 尚未注册"
        case .active:
            return "Option-Space 已启用"
        case .conflict:
            return "Option-Space 正被其他应用使用"
        case let .failed(status):
            return "Option-Space 注册失败（\(status)）"
        }
    }

    var diagnosticName: String {
        switch self {
        case .notRegistered: return "not_registered"
        case .active: return "active"
        case .conflict: return "conflict"
        case .failed: return "failed"
        }
    }

    var diagnosticStatus: Int? {
        switch self {
        case let .conflict(status), let .failed(status):
            return Int(status)
        case .notRegistered, .active:
            return nil
        }
    }

    static func classify(registrationStatus: OSStatus) -> TingleGlobalShortcutState {
        if registrationStatus == noErr {
            return .active
        }
        if registrationStatus == OSStatus(eventHotKeyExistsErr) {
            return .conflict(registrationStatus)
        }
        return .failed(registrationStatus)
    }
}


final class TingleGlobalHotKeyController {
    private static let signature: OSType = 0x54494E47 // TING
    private static let recordingID: UInt32 = 1
    private static let repeatGuardSeconds: TimeInterval = 0.35

    private let uptime: () -> TimeInterval
    private let onPress: () -> Void
    private var handler: EventHandlerRef?
    private var hotKey: EventHotKeyRef?
    private var lastDispatchTime: TimeInterval?

    private(set) var state: TingleGlobalShortcutState = .notRegistered

    init(
        uptime: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
        onPress: @escaping () -> Void
    ) {
        self.uptime = uptime
        self.onPress = onPress
    }

    deinit {
        unregister()
    }

    @discardableResult
    func register() -> TingleGlobalShortcutState {
        if hotKey != nil {
            state = .active
            return state
        }
        if handler == nil {
            let installStatus = installHandler()
            guard installStatus == noErr else {
                state = .failed(installStatus)
                return state
            }
        }
        let identifier = EventHotKeyID(signature: Self.signature, id: Self.recordingID)
        var reference: EventHotKeyRef?
        let registrationStatus = RegisterEventHotKey(
            UInt32(kVK_Space),
            UInt32(optionKey),
            identifier,
            GetApplicationEventTarget(),
            0,
            &reference
        )
        state = TingleGlobalShortcutState.classify(registrationStatus: registrationStatus)
        hotKey = state.isActive ? reference : nil
        return state
    }

    @discardableResult
    func retry() -> TingleGlobalShortcutState {
        if let hotKey {
            UnregisterEventHotKey(hotKey)
            self.hotKey = nil
        }
        state = .notRegistered
        return register()
    }

    func unregister() {
        if let hotKey {
            UnregisterEventHotKey(hotKey)
            self.hotKey = nil
        }
        if let handler {
            RemoveEventHandler(handler)
            self.handler = nil
        }
        state = .notRegistered
    }

    private func installHandler() -> OSStatus {
        var eventSpec = EventTypeSpec(
            eventClass: OSType(kEventClassKeyboard),
            eventKind: UInt32(kEventHotKeyPressed)
        )
        return InstallEventHandler(
            GetApplicationEventTarget(),
            { _, event, userData in
                guard let event, let userData else { return OSStatus(eventNotHandledErr) }
                var identifier = EventHotKeyID()
                let readStatus = GetEventParameter(
                    event,
                    EventParamName(kEventParamDirectObject),
                    EventParamType(typeEventHotKeyID),
                    nil,
                    MemoryLayout<EventHotKeyID>.size,
                    nil,
                    &identifier
                )
                guard readStatus == noErr,
                      identifier.signature == TingleGlobalHotKeyController.signature,
                      identifier.id == TingleGlobalHotKeyController.recordingID
                else {
                    return readStatus == noErr ? OSStatus(eventNotHandledErr) : readStatus
                }
                let controller = Unmanaged<TingleGlobalHotKeyController>
                    .fromOpaque(userData)
                    .takeUnretainedValue()
                DispatchQueue.main.async { controller.dispatchPressIfNeeded() }
                return noErr
            },
            1,
            &eventSpec,
            Unmanaged.passUnretained(self).toOpaque(),
            &handler
        )
    }

    func dispatchPressIfNeeded() {
        let now = uptime()
        if let lastDispatchTime,
           now - lastDispatchTime < Self.repeatGuardSeconds {
            return
        }
        lastDispatchTime = now
        onPress()
    }
}


final class TingleCuePlayer: NSObject, NSSoundDelegate {
    private var startSound: NSSound?
    private var stopSound: NSSound?
    private var startCompletion: (() -> Void)?

    func playStart(completion: @escaping () -> Void) {
        startCompletion = completion
        guard let sound = NSSound(
            contentsOfFile: "/System/Library/Sounds/Tink.aiff",
            byReference: true
        ) else {
            finishStartCue()
            return
        }
        startSound = sound
        sound.delegate = self
        if !sound.play() {
            finishStartCue()
        }
    }

    func playStop() {
        let sound = NSSound(
            contentsOfFile: "/System/Library/Sounds/Pop.aiff",
            byReference: true
        )
        stopSound = sound
        _ = sound?.play()
    }

    func sound(_ sound: NSSound, didFinishPlaying finishedPlaying: Bool) {
        guard sound === startSound else { return }
        finishStartCue()
    }

    private func finishStartCue() {
        startSound?.delegate = nil
        startSound = nil
        let completion = startCompletion
        startCompletion = nil
        completion?()
    }
}


final class TingleNotifier: NSObject, UNUserNotificationCenterDelegate {
    private let center = UNUserNotificationCenter.current()

    override init() {
        super.init()
        center.delegate = self
    }

    func notifySaved(duration: TimeInterval) {
        deliver(
            identifier: "tingle-saved-\(UUID().uuidString)",
            title: "Tingle 已保存",
            body: "录音 \(Self.durationText(duration)) 已安全保存在本机"
        )
    }

    func notifyTranscribed(text: String) {
        let snippet = TingleTranscriptPresentation.snippet(text, maxCharacters: 120)
        guard !snippet.isEmpty else { return }
        deliver(
            identifier: "tingle-transcribed-\(UUID().uuidString)",
            title: "Tingle 已转写",
            body: snippet
        )
    }

    func notifyFailure(_ message: String) {
        deliver(
            identifier: "tingle-failed-\(UUID().uuidString)",
            title: "Tingle 未能开始录音",
            body: message
        )
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner])
    }

    private func deliver(identifier: String, title: String, body: String) {
        center.requestAuthorization(options: [.alert]) { [weak self] granted, _ in
            guard granted, let self else { return }
            let content = UNMutableNotificationContent()
            content.title = title
            content.body = body
            self.center.add(UNNotificationRequest(identifier: identifier, content: content, trigger: nil))
        }
    }

    private static func durationText(_ duration: TimeInterval) -> String {
        let seconds = max(0, Int(duration.rounded()))
        return String(format: "%02d:%02d", seconds / 60, seconds % 60)
    }
}


final class TingleLoginItemController {
    private let service = SMAppService.mainApp

    var isEnabled: Bool { service.status == .enabled }

    var statusText: String {
        switch service.status {
        case .enabled:
            return "登录时启动已开启"
        case .requiresApproval:
            return "登录启动等待系统批准"
        case .notRegistered:
            return "登录时启动已关闭"
        case .notFound:
            return "登录时启动已关闭"
        @unknown default:
            return "登录启动状态未知"
        }
    }

    @discardableResult
    func toggle() -> Result<Void, Error> {
        do {
            if service.status == .enabled {
                try service.unregister()
            } else {
                try service.register()
            }
            return .success(())
        } catch {
            return .failure(error)
        }
    }
}
