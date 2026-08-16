#!/usr/bin/env python3
"""Static acceptance checks for the Click iPad native workspace."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    project = source("apps/ios/ClickShell/ClickShell.xcodeproj/project.pbxproj")
    app = source("apps/ios/ClickShell/ClickShell/ClickShellApp.swift")
    content = source("apps/ios/ClickShell/ClickShell/ContentView.swift")
    views = source("apps/ios/ClickShell/ClickShell/WorkspaceViews.swift")
    models = source("apps/ios/ClickShell/ClickShell/WorkspaceModels.swift")
    store = source("apps/ios/ClickShell/ClickShell/WorkspaceStore.swift")
    reader = source("apps/ios/ClickShell/ClickShell/NativeReaderViews.swift")
    readium = source("apps/ios/ClickShell/ClickShell/ReadiumReaderBridge.swift")
    listening = source("apps/ios/ClickShell/ClickShell/ListeningController.swift")
    recording = source("apps/ios/ClickShell/ClickShell/RecordingController.swift")
    connection = source("apps/ios/ClickShell/ClickShell/ConnectionStore.swift")
    discovery = source("apps/ios/ClickShell/ClickShell/ClickLANDiscovery.swift")
    sync = source("apps/ios/ClickShell/ClickShell/ClickSyncCoordinator.swift")
    keychain = source("apps/ios/ClickShell/ClickShell/KeychainStore.swift")
    info = source("apps/ios/ClickShell/ClickShell/Info.plist")
    bundled_font_license = source(
        "apps/ios/ClickShell/ClickShell/Fonts/SourceHanSans-LICENSE.txt"
    )
    swift = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "apps/ios/ClickShell/ClickShell").glob("*.swift")
    )

    require(
        "version = 3.11.0;" in project
        and "kind = exactVersion;" in project
        and "ReadiumReaderBridge.swift in Sources" in project,
        "Readium 3.11.0 must be an exact compiled dependency",
    )
    require(
        "CURRENT_PROJECT_VERSION = 23;" in project
        and "<string>0.3.19</string>" in info
        and "<string>23</string>" in info,
        "the automatic-highlight candidate must be version 0.3.19 (23)",
    )
    require(
        "NavigationSplitView" in views
        and "WorkspaceView(" in content
        and "connectedBaseURL" not in content,
        "offline workspace must not wait for the Mac",
    )
    detail_roots = [
        views.split("struct LibraryWorkspaceView:", 1)[1].split(
            "private struct FolderRenameTarget:", 1
        )[0],
        views.split("struct MyContentView:", 1)[1].split(
            "#if targetEnvironment(macCatalyst)", 1
        )[0],
        views.split("struct RecordingWorkspaceView:", 1)[1].split(
            "struct HermesWorkspaceView:", 1
        )[0],
        views.split("struct HermesWorkspaceView:", 1)[1].split(
            "struct SettingsWorkspaceView:", 1
        )[0],
        views.split("struct SettingsWorkspaceView:", 1)[1].split(
            ".sheet(item: $updateController.availableUpdate)", 1
        )[0],
    ]
    require(
        all("NavigationStack {" not in detail for detail in detail_roots),
        "split-view detail roots must not add a second navigation stack that can crash sidebar toggling",
    )
    require(
        'WindowGroup("Click")' in app
        and "CatalystWorkspaceWindowTitleKeeper" in views
        and 'windowScene?.title = workspaceTitle' in views,
        "the Catalyst acceptance window title must recover after reader sheets close",
    )
    require(
        "library-v1.json" in store
        and "PendingMobileOperation" in models
        and "updateEPUBLocation" in store
        and "lastLocatorJSON" in models
        and 'let sourceChanged =' in store
        and 'books[index].remoteDownloadState = "pending"' in store
        and "hasPendingReplacement" in store,
        "local library, queue, EPUB position, and changed-source redownload must persist",
    )
    require(
        all(
            marker in readium
            for marker in (
                ".copy",
                'title: "标红"',
                'title: "取消标红"',
                'title: "备注"',
                'title: "播放本句"',
                "matchingHighlightIDs",
                "removeHighlights",
                "applyAnnotations",
                "EPUBTableOfContentsMap",
            )
        ),
        "EPUB selection, highlights, and directory must be wired",
    )
    require(
        "scroll: false" in readium
        and "scroll: true" not in readium
        and "columnCount: .one" in readium
        and "let succeeded = await navigator.go(to: locator)" in readium
        and "publication.locate(link)" in readium
        and 'onError("无法跳转到这个目录位置，请重试")' in readium
        and 'id: "click-toc-current-target"' in readium
        and 'in: "click-toc-target"' in readium
        and "showTableOfContentsTarget(locator)" in readium
        and "hideReaderControls()" in reader
        and "navigateToEPUBContents(item)" in reader
        and '"已跳转到：\\(confirmation.title)"' in reader
        and "DirectionalNavigationAdapter(" not in readium
        and "directionalNavigationAdapter" not in readium,
        "EPUB must use one-column horizontal pagination with reliable TOC anchors and without edge-tap page turns",
    )
    require(
        "UISwipeGestureRecognizer" not in readium
        and "handlePageSwipe" not in readium
        and "isSwipeNavigationInFlight" not in readium
        and "navigator.goRight(options:" not in readium
        and "navigator.goLeft(options:" not in readium
        and "Readium already provides interactive, paginated horizontal scrolling" in readium,
        "EPUB swipes must use Readium's native interactive pagination without a competing recognizer",
    )
    require(
        "UITapGestureRecognizer" not in readium
        and "centralBand" not in readium
        and "navigator.addObserver(.activate" in readium
        and "inputObserverTokens" in readium
        and "navigator.currentSelection == nil" in readium
        and "selection-handle" in readium
        and "onToggleControls()" in readium,
        "EPUB page taps must use Readium input events without stealing selection or pagination gestures",
    )
    require(
        "@_spi(ExperimentalTargetElement) import ReadiumNavigator" in readium
        and "event.targetElement?.content as? ImageContentElement" in readium
        and "ClickImagePreviewViewController" in readium
        and "modalPresentationStyle = .fullScreen" in readium
        and 'statusLabel.text = "点击屏幕返回"' in readium
        and "publication.get(image.embeddedLink)" in readium
        and "case yahei" in reader
        and 'ReaderTypefaceChoice.yahei.rawValue' in reader
        and ".system(size: size, weight: .regular, design: .default)" in reader
        and 'rawValue: "PingFang SC"' in readium
        and "didApplyYaHeiDefault" in reader,
        "EPUB images must toggle a full-screen original preview and the comfortable system face must be the migrated default",
    )
    require(
        "case yahei" in reader
        and "case system" in reader
        and 'case .yahei: return "舒适黑体"' in reader
        and 'case .system: return "系统默认"' in reader
        and "case serif" not in reader
        and "case rounded" not in reader
        and "didRestrictTypefaceChoices" in reader,
        "reader typeface choices must be limited to YaHei and the system fallback",
    )
    require(
        "SourceHanSansCN-VF.otf in Resources" not in project
        and "SourceHanSans-LICENSE.txt in Resources" in project
        and "<key>UIAppFonts</key>" not in info
        and "<string>SourceHanSansCN-VF.otf</string>" not in info
        and "SIL OPEN FONT LICENSE Version 1.1" in bundled_font_license
        and "bundledFontFamilyDeclarations" in readium
        and 'forResource: "SourceHanSansCN-VF"' in readium
        and "CSSFontFace(" in readium
        and "preload: true" in readium
        and "weight: .variable(250 ... 900)" in readium
        and "Microsoft YaHei" not in reader
        and "Microsoft YaHei" not in readium,
        "public source must use a system/PingFang fallback, retain the OFL notice, and avoid vendoring the font binary",
    )
    require(
        "fullPageTextFlowScript" in readium
        and 'styleID = "click-full-page-text-flow-v1"' in readium
        and "-webkit-column-break-inside: auto !important" in readium
        and "page-break-inside: auto !important" in readium
        and "break-inside: auto !important" in readium
        and "orphans: 1 !important" in readium
        and "widows: 1 !important" in readium
        and "setupUserScripts userContentController" in readium
        and "injectionTime: .atDocumentEnd" in readium,
        "EPUB paragraphs must use every available page line instead of reserving widow/orphan rows",
    )
    require(
        "onAutomaticHighlight" in readium
        and "scheduleAutomaticHighlight(for: selection)" in readium
        and "shouldShowMenuForSelection selection: Selection" in readium
        and "try await Task.sleep(for: .milliseconds(320))" in readium
        and "commitAutomaticHighlight(for: currentSelection)" in readium
        and "automaticHighlightSessionWatcherTask" in readium
        and "try await Task.sleep(for: .milliseconds(120))" in readium
        and "Self.isSelectionAdjustment(session.locator, selection.locator)"
        in readium
        and "workspace.updateHighlight(" in readium
        and "guard payload.matchingHighlightIDs.isEmpty" in readium
        and "func updateHighlight(" in store
        and "hasMutablePendingUpsert" in store,
        "a settled EPUB drag selection must auto-highlight once, update the same pending annotation during handle adjustment, and keep the normal edit menu",
    )
    require(
        "firstVisibleElementLocator()" in readium
        and "preferredListeningStartLocator" in readium
        and "programmaticLocatorNavigationDepth" in readium
        and "preferredListeningStartLocator = locator" in readium
        and "startLocator?.text.highlight" in readium
        and "hasReachedExactStart" in readium
        and "fallbackSegments" in readium
        and "matchesStart(sentence:" in readium
        and "publication.content(from: startLocator)" in readium
        and "publication.content(from: nil)" in readium
        and "EPUBListeningQueue" in readium
        and "func start(" in listening
        and "queue: EPUBListeningQueue" in listening
        and "synchronizeCurrentSegment()" in listening
        and "epubCoordinator.listeningQueue()" in reader,
        "EPUB listening must preserve exact search/listening anchors and keep audio position aligned",
    )
    require(
        "@Published private(set) var currentBookID" in listening
        and "@Published private(set) var currentSegmentLocatorJSON" in listening
        and 'currentSegmentLocatorJSON = ""' in listening
        and "currentSegmentLocatorJSON = locatorJSON" in listening
        and "listeningLocatorJSON:" in readium
        and "listening.currentBookID == book.id.uuidString" in readium
        and "applyListeningIndicator(locatorJSON:" in readium
        and 'in: "click-tts-current"' in readium
        and 'id: "click-tts-current-segment"' in readium
        and "locator.copy(text:" in readium
        and "locatorText = locatorText[range]" in readium,
        "EPUB listening must expose one book-scoped, sentence-precise visual indicator and clear it on stop",
    )
    require(
        "containsSpeakableContent" in listening
        and "result.filter(containsSpeakableContent)" in listening
        and "preparedSegments = queue.segments.filter" in listening
        and "!Self.containsSpeakableContent(sentences[currentIndex])" in listening
        and "listening.currentBookID == bookID" in reader
        and "listening.currentBookID == book.id.uuidString" in reader
        and "listeningPreparationTask?.cancel()" in reader
        and "guard !Task.isCancelled else { return }" in reader,
        "iPad listening must skip punctuation-only segments and reject stale cross-book preparation",
    )
    require(
        "EPUBOfflineTextExtractor" not in readium
        and "EPUBOfflineTextExtractor" not in reader,
        "the obsolete whole-book EPUB text path must not bypass locator-based listening",
    )
    require(
        "epubCoordinator.navigate" in reader
        and "multilineTextAlignment(.leading)" in reader,
        "directory rows must wrap, align left, and navigate",
    )
    require(
        'ClickReader.topMargin' in reader
        and 'ClickReader.bottomMargin' in reader
        and "topMargin: $topMargin" in reader
        and "bottomMargin: $bottomMargin" in reader
        and ".padding(.top, topMargin)" in reader
        and ".padding(.bottom, bottomMargin)" in reader
        and "contentInset: [" in readium
        and ".compact: (top: 0, bottom: 0)" in readium,
        "reader top and bottom margins must be adjustable and update content live",
    )
    require(
        "ReaderTopBar" in reader
        and ".toolbar(.hidden, for: .navigationBar)" in reader
        and ".overlay(alignment: .top)" in reader
        and ".overlay(alignment: .bottom)" in reader
        and ".safeAreaInset(edge: .bottom" not in reader
        and ".safeAreaInset(edge: .bottom" not in views
        and "pageMargins: max(0.1" in readium
        and "navigator.view.clipsToBounds = true" in readium
        and "navigator.view.layer.masksToBounds = true" in readium,
        "reader chrome must not resize the paginated viewport or expose adjacent columns",
    )
    require(
        "struct ClickFolderPath" in models
        and "static let maxDepth = 3" in models
        and 'components.joined(separator: " › ")' in models
        and "func childFolderPaths(at parent: ClickFolderPath)" in store
        and "func books(directlyIn path: ClickFolderPath)" in store
        and "func books(inFolderPath path: ClickFolderPath)" in store
        and "func setFolderPath(_ path: ClickFolderPath, forBooks" in store
        and "func renameFolder(at path: ClickFolderPath" in store
        and "func moveFolderContentsToParent(" in store
        and "FolderCard" in views
        and "selectedFolderPath = ClickFolderPath.root" in views
        and 'Text("书架 › \\(selectedFolderPath.displayValue)")' in views
        and 'Label("返回上一级"' in views
        and "workspace.childFolderPaths(at: selectedFolderPath)" in views
        and "workspace.folderPath(for: $0) == selectedFolderPath" in views
        and "@State private var selectedFolder: String?" not in views
        and ".draggable(book.id.uuidString)" in views
        and ".dropDestination(for: String.self)" in views
        and "BulkFolderEditor" in views
        and "FolderRenameEditor" in views
        and "func addBookmark(" in store
        and "func bookmarks(" in store
        and "navigate(toLocatorJSON" in readium
        and "publication.search(query:" in readium
        and "EPUBReaderSearchView" in reader
        and "ReaderSearchView" in reader
        and "Task.sleep(for: .milliseconds(220))" in reader,
        "folders, positioned bookmarks, and demand-driven local search must be wired",
    )
    require(
        "ClickUpdateManifestURL" in info
        and "CLICK_UPDATE_MANIFEST_URL" in project
        and "AppUpdateController" in views
        and "checkForUpdates()" in views
        and 'url.scheme?.lowercased() == "https"' in views
        and 'scheme == "itms-services"' in views
        and 'Label("检查更新"' in views,
        "the iPad app must expose a bounded HTTPS update check without background polling",
    )
    require(
        '\"section\": currentTextSectionID' in reader
        and 'object["section"] as? Int' in reader
        and ".scrollPosition(id: $currentTextSectionID" in reader
        and "navigate: navigateToSearchSection" in reader
        and "sectionID: section.id" in reader
        and "@Binding var requestedPage: Int?" in reader
        and "requestedPageBinding.wrappedValue = nil" in reader,
        "text bookmarks and text/PDF search must jump to stable, consumable locations",
    )
    require(
        "ContentUnavailableView.search" not in views
        and "ContentUnavailableView.search" not in reader
        and '"没有找到结果"' in reader
        and '"还没有收藏"' in views,
        "empty and no-result states must remain fully localized in Chinese",
    )
    require(
        "showTransientStatus(" in store
        and "Task.sleep(nanoseconds: dismissAfterNanoseconds)" in store
        and "statusDismissTask?.cancel()" in store,
        "successful shelf actions must auto-dismiss without clearing newer messages",
    )
    require(
        "if !annotation.excerpt.isEmpty," in views
        and "!annotation.note.isEmpty," in views,
        "a note without an excerpt must not be rendered twice",
    )
    require(
        'searchable(text: $searchText, prompt: "搜索书名、标红或备注")' in views
        and "filteredAnnotations(for:" in views,
        "cross-book reading notes must remain locally searchable",
    )
    require(
        "AVSpeechSynthesizer" not in listening
        and "AVSpeechUtterance" not in listening
        and "playSystemFallback" not in listening
        and "AVAudioPlayer" in listening
        and 'static let microsoftVoice = "zh-CN-YunjianNeural"' in listening
        and 'let metadataPaths = ["/v1/mobile/tts", "/lookup/tts"]' in listening
        and "http.statusCode == 404, index == 0" in listening
        and 'payload["voice"] as? String == Self.microsoftVoice' in listening
        and "ClickTTSConfiguration" in listening
        and "normalizedCacheText(text)" in listening
        and "precomposedStringWithCanonicalMapping" in listening
        and "isSameOrigin(audioURL, configuration.baseURL)" in listening
        and "effectivePort(of:" in listening
        and "schedulePrefetch(" in listening
        and "prefetchTask?.cancel()" in listening
        and "audioFetchTasks" in listening
        and "existing.task.value" in listening
        and "shouldRetryMicrosoftFetch" in listening
        and "AVAudioPlayer(data: audioData)" in listening
        and "MPRemoteCommandCenter" in listening
        and "startCountdown(minutes:" in listening,
        "iPad reading must use Microsoft TTS only, with bounded cache, media controls, and countdown",
    )
    require(
        "initialCacheSeconds: TimeInterval = 60 * 60" in listening
        and "refillLowWaterSeconds: TimeInterval = 30 * 60" in listening
        and "refillChunkSeconds: TimeInterval = 30 * 60" in listening
        and "globalCacheSeconds: TimeInterval = 200 * 60" in listening
        and "globalCacheBytes = 512 * 1_024 * 1_024" in listening
        and "prefetchFailureCooldown: TimeInterval = 5 * 60" in listening
        and 'cacheIndexSchema = "click.ipad.microsoft_tts_cache.v2"' in listening
        and "cachedListeningSeconds(from:" in listening
        and "protectedCacheKeys()" in listening
        and "lastAccessedAt < $1.lastAccessedAt" in listening
        and "prefetchTask == nil" in listening
        and "Task(priority: .utility)" in listening
        and "pendingInitialPrefetch" in listening
        and "Self.initialCacheSeconds - cachedSeconds" in listening
        and "prefetchRemainingSeconds = Self.refillChunkSeconds" in listening
        and "prefetchRemainingSeconds\n                                - entry.duration" in listening
        and "prefetchGoalSeconds" not in listening,
        "Microsoft audio cache must use one event-driven 60/30/200 minute policy with LRU cleanup",
    )
    require(
        "mode: .spokenAudio" in listening
        and "mode: .default" in listening
        and "options: []" in listening
        and "func hidePlayer()" in listening
        and 'Image(systemName: "chevron.down")' not in views
        and 'Image(systemName: "xmark")' in views
        and 'Button(role: .destructive)' in views
        and "listening.stop()" in views
        and '.presentationDetents([.height(300), .medium])' not in views
        and '.presentationDetents([.large])' in views
        and '.frame(width: 96, height: 128)' in views
        and '.fixedSize(horizontal: false, vertical: true)' in views
        and 'Text(listening.currentSentence)\n                .font(.title3)\n                .lineLimit(3)' not in views
        and "minHeight: 120" in views
        and "maxHeight: 260" in views
        and ".scaledToFit()" in views,
        "the mini-player close button must stop reading and expanded listening must open full-height with complete artwork and sentence text",
    )
    require(
        "audioFetchTasks.values.forEach" in listening
        and "audioFetchTasks.removeAll()" in listening
        and "corruptCacheRecoveryKey" in listening,
        "explicit stop or manual seeking must cancel obsolete shared fetches, and bad cache must self-heal",
    )
    require(
        "localCoverRelativePath" in models
        and "remoteCoverPath" in models
        and "func coverURL(for book:" in store
        and "isDecodableCoverData" in store
        and "CGImageSourceGetCount(source) > 0" in store
        and "CGImageSourceCreateThumbnailAtIndex" in store
        and "func remoteCoverPath(for book:" in store
        and "installDownloadedCover" in store
        and "downloadMissingCovers" in sync
        and "WorkspaceStore.isDecodableCoverData(data)" in sync
        and "artworkURL: workspace.coverURL(for: book)" in reader
        and "MPMediaItemPropertyArtwork" in listening
        and "ListeningArtwork" in views
        and "BookCoverArtwork" in views
        and ".frame(maxWidth: .infinity, maxHeight: .infinity)" in views,
        "book artwork must reject undecodable files and remain clipped in every shelf surface",
    )
    require(
        "enum LocalBookCoverExtractor" in readium
        and "publication.coverFitting" in readium
        and "func requestLocalCover(for bookID:" in store
        and "localCoverWorkerTask" in store
        and "pendingLocalCoverBookIDs" in store
        and "queuedLocalCoverBookIDs" in store
        and "localCoverAttemptedBookIDs" in store
        and "Task(priority: .utility)" in store
        and "requestCover: workspace.requestLocalCover(for:)" in views
        and ".task(id: book.id)" in views,
        "downloaded EPUB/PDF books must extract local covers lazily without a whole-library scan",
    )
    require(
        "var displayTitle:" in models
        and "static func hasUsefulTitle" in models
        and "static func hasUsefulAuthor" in models
        and '"book"' in models
        and '"unknown"' in models
        and "normalizeStoredBookMetadata" in store
        and "repairLocalMetadata" in store
        and "LocalBookCoverExtractor.extract(from:" in store
        and "publication.metadata.title" in readium
        and "publication.metadata.authors" in readium
        and "Text(book.displayTitle)" in views,
        "placeholder titles and authors must be normalized and repaired lazily from local publications",
    )
    require(
        "ClickRemoteTTSOrigin" in info
        and "validatedRemoteTTSOrigin" in connection
        and "remoteTTSBaseURL ?? connectedBaseURL" in connection
        and 'components.scheme?.lowercased() == "https"' in connection,
        "Microsoft TTS must prefer a build-configured Click HTTPS origin over the optional Mac LAN path",
    )
    require(
        "ClickReader.microsoftTTSDisclosureAccepted.v1" in reader
        and '"使用 Click 微软语音"' in reader
        and "不会发送整本书、备注或标红" in reader,
        "the first online Microsoft TTS use must disclose the bounded text transfer",
    )
    require(
        "AVAudioRecorder" in recording
        and 'appendingPathComponent("Recordings"' in recording
        and "VoiceCaptureManifest" in recording
        and 'syncState: "pending"' in recording
        and "markSynced(" in recording
        and "iPad 原音仍保留" in recording,
        "recordings must stay in durable native storage with visible sync state",
    )
    require(
        "VoiceNoteDraftController" in readium
        and "mode: .measurement" in readium
        and (
            "requiresOnDeviceRecognition = recognizer.supportsOnDeviceRecognition"
            in readium
        )
        and "supportsOnDeviceRecognition" in readium
        and "audioRelativePath" in models
        and 'appendingPathComponent("VoiceNotes"' in store
        and "VoiceNotePlaybackController" in reader,
        "reading voice notes must keep playable original audio and prefer on-device transcription",
    )
    require(
        "ReaderThemeChoice.black.rawValue" in reader
        and "ReaderTypefaceChoice.allCases" in reader
        and ".presentationDetents([.large])" in reader
        and "controlsVisible = false" in reader
        and "onToggleControls: toggleReaderControls" in reader
        and "readiumBackgroundHex" in readium,
        "reader controls must open fully, support immersive mode, and default to a dark full-page theme",
    )
    require(
        '"audio_note_created"' in store
        and "voiceNoteOperationEnvelope" in store
        and "receiptHash == expectedHash.lowercased()" in store
        and "size <= 16 * 1_024 * 1_024" in sync,
        "reading voice notes must upload once with a bounded body and verified audio receipt",
    )
    require(
        'path: "/v1/recordings"' in sync
        and '"client_capture_id": capture.id' in sync
        and "audio_base64" in sync
        and "base64EncodedData()" in sync
        and "receiptHash.lowercased() == audioHash" in sync
        and "nextPendingCapture()" in sync
        and "removeItem(at: capture.url)" not in sync,
        "Tingle upload must be serial, idempotent, hash-verified, and retain local audio",
    )
    require(
        "KeychainStore" in connection
        and "kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly" in keychain
        and "access_token" not in connection,
        "credentials must stay in Keychain and out of URLs",
    )
    require(
        "_click-reader._tcp." in discovery
        and "click.reader_runtime.v1" in discovery
        and ".seconds(8)" in discovery
        and "numericPrivateIPv4" in discovery
        and "/v1/android/readium/health" in discovery,
        "Bonjour discovery must be bounded, private-LAN-only, and authenticated",
    )
    require(
        "maximumOperationBatches = 10" in sync
        and "maximumChangePages = 20" in sync
        and "queuedPreferredBookID" in sync
        and "/v1/android/sync/operations" in sync
        and "/v1/android/sync/changes" in sync
        and "/v1/android/sync/full?include_chapters=false" in sync
        and "actualIDs == expectedIDs" in sync,
        "sync must bound work and require exact idempotent receipts",
    )
    require(
        "serverRemoved" in models
        and "ClickSyncConflict" in models
        and "markRemoteAnnotationsRemoved" in store
        and "clientPayloadJSON" in store,
        "remote tombstones and annotation conflicts must preserve local content",
    )
    require(
        ".onChange(of: scenePhase)" in content
        and "phase == .active" in content
        and "NWPathMonitor" not in swift
        and "BGTaskScheduler" not in swift,
        "sync must be foreground-triggered without a resident monitor",
    )
    require(
        "<string>audio</string>" in info
        and "NSMicrophoneUsageDescription" in info,
        "background audio and microphone permission must be declared",
    )
    require(
        "NSSpeechRecognitionUsageDescription" in info,
        "on-device voice-note transcription permission must be declared",
    )
    require(
        "正在接入" not in reader and "正在接入" not in readium,
        "the app must not ship an EPUB placeholder",
    )

    require(
        swift.count("Timer.scheduledTimer") == 2,
        "only active countdown and recording may own timers",
    )
    require(
        "func deleteBook" not in store
        and "books.removeAll" not in store
        and "annotations.removeAll" not in store,
        "sync must not add user-library deletion operations",
    )

    print("click iPad native shell smoke: PASS")


if __name__ == "__main__":
    main()
