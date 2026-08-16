# Click iPad Native

`ClickShell` is the native iPad source candidate. The current baseline is `0.3.19` (`build 23`). It is not a public App Store or Ad Hoc release, and physical-device acceptance is still required before daily use.

## Current capability

- SwiftUI three-column workspace, shelf, personal content, recording, Hermes, and settings.
- File import, local library persistence, and offline cold start.
- Readium Swift Toolkit `3.11.0` EPUB reading, PDFKit reading, and plain-text reading.
- EPUB table of contents, position restore, search, bookmarks, typography, and horizontal pagination.
- Selection actions for copy, red highlight, note, and play sentence.
- Cross-book search for red highlights and notes, with navigation back to the source.
- Local M4A recording and audio-note preservation.
- Keychain credential storage; access tokens never appear in URLs.
- Bounded foreground Bonjour discovery that accepts private-network endpoints only.
- Full baseline plus monotonic incremental sync, idempotent receipts, tombstones, and conflict preservation.
- Cached cover extraction and bounded one-book-at-a-time background downloads.
- Listening controls with a bounded audio cache, next/previous sentence, sleep timer, and lock-screen controls.

The app keeps its data in the iOS application container. It does not bundle Python, PostgreSQL, or a local large-language model, and it does not start a permanent background sync service.

## Privacy and release boundary

- Real books, notes, recordings, device addresses, access tokens, signing identities, and release artifacts are not committed.
- Remote speech uses a Click-controlled HTTPS origin supplied through the `CLICK_REMOTE_TTS_ORIGIN` build setting.
- The remote origin must be a plain HTTPS origin with no credentials, path, query, or fragment.
- When the remote speech service is unavailable, Click plays only already-cached audio and does not silently switch engines.
- Hermes remains an explicit connected feature; when the paired service is unavailable, the app reports offline status.

## Code map

- `ClickShell/WorkspaceViews.swift`: workspace and shelf
- `ClickShell/NativeReaderViews.swift`: local PDF and text readers
- `ClickShell/ReadiumReaderBridge.swift`: EPUB navigation and selection actions
- `ClickShell/WorkspaceStore.swift`: local manifest, files, and pending operations
- `ClickShell/ClickLANDiscovery.swift`: local-service discovery and authenticated probing
- `ClickShell/ClickSyncCoordinator.swift`: upload, baseline, incremental sync, conflicts, and bounded downloads
- `ClickShell/ListeningController.swift`: listening queue, audio cache, and media controls
- `ClickShell/RecordingController.swift`: local recording
- `ClickShell/ConnectionStore.swift`: paired Reader API connection boundary

## Validation

From the repository root:

```bash
python3 scripts/click_ipad_native_shell_smoke.py
plutil -lint apps/ios/ClickShell/ClickShell/Info.plist
plutil -lint apps/ios/ClickShell/ClickShell.xcodeproj/project.pbxproj
```

With a complete iOS platform installed in Xcode:

```bash
xcodebuild \
  -project apps/ios/ClickShell/ClickShell.xcodeproj \
  -scheme ClickShell \
  -destination 'generic/platform=iOS' \
  CODE_SIGNING_ALLOWED=NO \
  build
```
