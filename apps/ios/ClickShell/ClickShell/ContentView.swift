import SwiftUI

struct ContentView: View {
    @StateObject private var connection = ConnectionStore()
    @StateObject private var workspace = WorkspaceStore()
    @StateObject private var listening = ListeningController()
    @StateObject private var recording = RecordingController()
    @StateObject private var sync = ClickSyncCoordinator()
    @Environment(\.scenePhase) private var scenePhase
    @State private var didStart = false

    var body: some View {
        WorkspaceView(
            workspace: workspace,
            connection: connection,
            listening: listening,
            recording: recording,
            sync: sync
        )
        .tint(.blue)
        .task {
            guard !didStart else { return }
            didStart = true
#if DEBUG
            if isLocalCoverProbeRequested {
                await runLocalCoverProbeIfRequested()
                return
            }
            if isPhysicalTTSProbeRequested {
                await connection.restoreConnectionOrDiscover()
                runPhysicalTTSProbeIfRequested()
            }
#endif
            await sync.run(
                workspace: workspace,
                connection: connection,
                recording: recording
            )
        }
        .onChange(of: scenePhase) { _, phase in
            guard phase == .active, didStart else { return }
            Task {
                await sync.run(
                    workspace: workspace,
                    connection: connection,
                    recording: recording
                )
            }
        }
        .onChange(of: workspace.requestedDownloadBookID) { _, bookID in
            guard let bookID else { return }
            Task {
                await sync.run(
                    workspace: workspace,
                    connection: connection,
                    recording: recording,
                    preferredBookID: bookID
                )
            }
        }
        .onChange(of: workspace.selectedBookID) { _, bookID in
            guard let bookID else { return }
            Task {
                await sync.run(
                    workspace: workspace,
                    connection: connection,
                    recording: recording,
                    preferredBookID: bookID
                )
            }
        }
        .onChange(of: recording.syncRequestID) { _, _ in
            guard didStart else { return }
            Task {
                await sync.run(
                    workspace: workspace,
                    connection: connection,
                    recording: recording
                )
            }
        }
    }

#if DEBUG
    private var isLocalCoverProbeRequested: Bool {
        !(ProcessInfo.processInfo.environment["CLICK_IPAD_LOCAL_COVER_PROBE_PATH"] ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .isEmpty
    }

    private func runLocalCoverProbeIfRequested() async {
        let path = (
            ProcessInfo.processInfo.environment["CLICK_IPAD_LOCAL_COVER_PROBE_PATH"] ?? ""
        ).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !path.isEmpty else { return }
        let sourceURL = URL(fileURLWithPath: path)
        await workspace.importDocuments([sourceURL])
        let deadline = Date().addingTimeInterval(12)
        while Date() < deadline {
            if let book = workspace.books.first(where: {
                URL(fileURLWithPath: $0.localRelativePath).lastPathComponent
                    == sourceURL.lastPathComponent
            }), let coverURL = workspace.coverURL(for: book) {
                print("CLICK_LOCAL_COVER_PROBE PASS cover=\(coverURL.lastPathComponent)")
                workspace.showStatus("本地封面测试通过")
                return
            }
            try? await Task.sleep(for: .milliseconds(200))
        }
        print("CLICK_LOCAL_COVER_PROBE FAIL")
        workspace.showStatus("本地封面测试失败")
    }

    private var isPhysicalTTSProbeRequested: Bool {
        !(ProcessInfo.processInfo.environment["CLICK_IPAD_TTS_PROBE_TEXT"] ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .isEmpty
    }

    private func runPhysicalTTSProbeIfRequested() {
        let text = (
            ProcessInfo.processInfo.environment["CLICK_IPAD_TTS_PROBE_TEXT"] ?? ""
        ).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !listening.hasContent else { return }
        listening.start(
            text: text,
            title: "iPad 朗读测试",
            configuration: connection.clickTTSConfiguration(
                bookID: workspace.books.first?.id.uuidString
                    ?? "physical-tts-probe"
            )
        )
    }
#endif
}
