import AppIntents
import Foundation


enum TingleAppIntentCommand: String, Equatable {
    case openApp = "open_app"
    case start
    case pauseOrResume = "pause_or_resume"
    case stopAndSave = "stop_and_save"
    case getState = "get_state"
}


protocol TingleAppIntentCommandHandling: AnyObject {
    func performTingleAppIntentCommand(_ command: TingleAppIntentCommand) -> TingleCommandDisposition
    func tingleAppIntentSnapshot() -> TingleRecordingSnapshot
}


@MainActor
final class TingleAppIntentActivationGuard {
    static let shared = TingleAppIntentActivationGuard()

    private var suppressReopenUntil = Date.distantPast

    private init() {}

    func begin(_ command: TingleAppIntentCommand) {
        guard command != .openApp else { return }
        suppressReopenUntil = Date().addingTimeInterval(2)
        UserDefaults.standard.set(command.rawValue, forKey: "TingleLastBackgroundIntentCommand")
        UserDefaults.standard.set(
            ISO8601DateFormatter().string(from: Date()),
            forKey: "TingleLastBackgroundIntentAt"
        )
    }

    func shouldSuppressReopen() -> Bool {
        Date() < suppressReopenUntil
    }
}


@MainActor
final class TingleAppIntentRouter {
    static let shared = TingleAppIntentRouter()

    private weak var handler: TingleAppIntentCommandHandling?

    private init() {}

    func register(_ handler: TingleAppIntentCommandHandling) {
        self.handler = handler
    }

    func unregister(_ handler: TingleAppIntentCommandHandling) {
        guard self.handler === handler else { return }
        self.handler = nil
    }

    func executeIfReady(
        _ command: TingleAppIntentCommand
    ) -> (TingleCommandDisposition, TingleRecordingSnapshot)? {
        guard let handler else { return nil }
        let disposition = command == .getState
            ? TingleCommandDisposition.noOp
            : handler.performTingleAppIntentCommand(command)
        return (disposition, handler.tingleAppIntentSnapshot())
    }

    func snapshotIfReady() -> TingleRecordingSnapshot? {
        handler?.tingleAppIntentSnapshot()
    }
}


enum TingleAppIntentRuntime {
    static func execute(_ command: TingleAppIntentCommand) async -> String {
        await TingleAppIntentActivationGuard.shared.begin(command)
        var execution: (TingleCommandDisposition, TingleRecordingSnapshot)?
        for _ in 0..<30 {
            execution = await TingleAppIntentRouter.shared.executeIfReady(command)
            if execution != nil { break }
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        guard var execution else {
            return "Tingle 尚未完成启动，命令没有执行。"
        }

        if command == .start, execution.0 == .accepted {
            for _ in 0..<60 {
                guard execution.1.state == .preparing else { break }
                try? await Task.sleep(nanoseconds: 100_000_000)
                if let snapshot = await TingleAppIntentRouter.shared.snapshotIfReady() {
                    execution.1 = snapshot
                }
            }
        }
        return describe(command: command, disposition: execution.0, snapshot: execution.1)
    }

    static func describe(
        command: TingleAppIntentCommand,
        disposition: TingleCommandDisposition,
        snapshot: TingleRecordingSnapshot
    ) -> String {
        switch (command, disposition) {
        case (.start, .noOp):
            return "录音已经在进行，没有重复开始。\(stateText(snapshot))"
        case (.stopAndSave, .noOp):
            return "当前没有录音需要保存。\(stateText(snapshot))"
        case (.pauseOrResume, .noOp):
            return "当前没有可暂停或继续的录音。\(stateText(snapshot))"
        case (_, .busy):
            return "Tingle 正在处理上一条命令，本次没有重复执行。\(stateText(snapshot))"
        case (_, .unavailable):
            return "Tingle 无法执行这条命令。\(stateText(snapshot))"
        default:
            return stateText(snapshot)
        }
    }

    static func stateText(_ snapshot: TingleRecordingSnapshot) -> String {
        let duration = durationText(snapshot.elapsedSeconds)
        switch snapshot.state {
        case .idle where snapshot.pendingSyncCount > 0:
            return "Tingle 当前空闲；\(snapshot.pendingSyncCount) 条录音已保存在本机，等待同步。"
        case .idle:
            return "Tingle 当前空闲。"
        case .preparing:
            return "Tingle 正在准备麦克风。"
        case .recording:
            return "Tingle 正在录音，已录制 \(duration)。"
        case .paused:
            return "Tingle 录音已暂停，当前时长 \(duration)。"
        case .finalizing:
            return "Tingle 正在停止并保存原始音频。"
        case .savedLocally where snapshot.syncState == .pending:
            return "录音已保存在本机，时长 \(duration)，等待同步。"
        case .savedLocally:
            return "录音已保存在本机，时长 \(duration)。"
        case .failed:
            let message = snapshot.errorMessage?.trimmingCharacters(in: .whitespacesAndNewlines)
            return message?.isEmpty == false
                ? "Tingle 录音失败：\(message!)"
                : "Tingle 录音失败，原始音频若已捕获会继续保留。"
        }
    }

    private static func durationText(_ seconds: Double) -> String {
        let total = max(0, Int(seconds.rounded()))
        return String(format: "%02d:%02d", total / 60, total % 60)
    }
}


struct OpenTingleIntent: AppIntent {
    static let title: LocalizedStringResource = "打开 Tingle"
    static let description = IntentDescription("打开 Tingle 的录音界面。")
    static let openAppWhenRun = true

    func perform() async throws -> some IntentResult & ReturnsValue<String> {
        .result(value: await TingleAppIntentRuntime.execute(.openApp))
    }
}


struct StartTingleRecordingIntent: AppIntent {
    static let title: LocalizedStringResource = "开始 Tingle 录音"
    static let description = IntentDescription("在后台开始一条 Tingle 录音，不显示主窗口。")
    static let openAppWhenRun = false

    func perform() async throws -> some IntentResult & ReturnsValue<String> {
        .result(value: await TingleAppIntentRuntime.execute(.start))
    }
}


struct PauseOrResumeTingleRecordingIntent: AppIntent {
    static let title: LocalizedStringResource = "暂停或继续 Tingle 录音"
    static let description = IntentDescription("暂停正在进行的 Tingle 录音，或继续已暂停的录音。")
    static let openAppWhenRun = false

    func perform() async throws -> some IntentResult & ReturnsValue<String> {
        .result(value: await TingleAppIntentRuntime.execute(.pauseOrResume))
    }
}


struct StopAndSaveTingleRecordingIntent: AppIntent {
    static let title: LocalizedStringResource = "停止并保存 Tingle 录音"
    static let description = IntentDescription("停止当前录音，并在原始音频落盘后返回真实状态。")
    static let openAppWhenRun = false

    func perform() async throws -> some IntentResult & ReturnsValue<String> {
        .result(value: await TingleAppIntentRuntime.execute(.stopAndSave))
    }
}


struct GetTingleRecordingStateIntent: AppIntent {
    static let title: LocalizedStringResource = "获取 Tingle 录音状态"
    static let description = IntentDescription("获取 Tingle 当前录音、保存和待同步状态。")
    static let openAppWhenRun = false

    func perform() async throws -> some IntentResult & ReturnsValue<String> {
        .result(value: await TingleAppIntentRuntime.execute(.getState))
    }
}
