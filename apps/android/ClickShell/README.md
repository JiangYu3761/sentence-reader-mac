# Click Android

Status: source-complete ARM64 candidate for controlled device testing. The current baseline is `0.1.7` (`versionCode 8`). No public APK is published from this repository, and physical-device acceptance remains required before daily use.

## Product surface

The first screen keeps three clear entries: 阅读 / 录音 / Hermes.

- 阅读 opens the native local shelf, then a Readium EPUB reader or the PDF reader.
- 录音 stores a local capture first and uploads it only when an approved Reader API is reachable.
- Hermes uses the bounded current-book evidence contract; no evidence means no model call.

The Android app keeps a local SQLite replica, verified EPUB and cover caches, reading positions, annotations, lookup results, and an offline operation queue. The Mac Reader API and PostgreSQL remain the source of truth. Android does not create a second cloud account or database.

## Offline and sync behavior

1. Cold start opens the cached native shelf without waiting for the Mac.
2. Verified EPUB and PDF files remain readable while the Mac is unavailable.
3. The connection screen accepts a Mac address and service port without embedding a real device address in source.
4. Pairing uses a stable device ID, a short comparison code, an approved token, and Android Keystore storage.
5. Incremental sync uses a monotonic sequence, durable operation receipts, tombstones, and conflict preservation.
6. Book imports are copied into app-controlled storage and indexed off the main thread.
7. Android Back closes the active reader layer before returning to the shelf.

## Reader interactions

- Tap a sentence to focus it and open sentence actions.
- Add a note or voice note against the sentence.
- Mark the whole sentence red without manually adjusting a text range.
- Tap the red action again to remove the mark.
- Use the table of contents, search, typography controls, and listening controls from the native reader chrome.

## Privacy and release boundary

- Real books, notes, recordings, device addresses, access tokens, signing keys, and release artifacts are not committed.
- Release signing values come only from `CLICK_ANDROID_RELEASE_*` environment variables.
- The repository may contain the approved public signing-certificate fingerprint, but never the private key or passwords.
- Automatic update checks run only when the app enters the foreground and are rate-limited.
- Downloaded updates must pass descriptor, SHA-256, package, version, and signing-certificate checks before Android's system installer is opened.
- Default hosts stay empty in public source. Users explicitly enter or discover their own Reader API endpoint.

## Build

Requirements:

- JDK 17
- Android SDK matching the Gradle configuration
- An ARM64 device or emulator when testing the optional local TTS runtime

Build the local debug candidate:

```bash
scripts/build_android_click_shell.sh
```

Or import `apps/android/ClickShell` into Android Studio and run the `app` configuration.

The public source includes Readium integration and the optional sherpa-onnx bridge, but it does not vendor native runtime binaries or voice-model weights. Supply those build-time assets separately and follow the licenses recorded in `THIRD_PARTY_NOTICES.md` and `app/src/main/assets/licenses/`. Without them, the core reader still builds and local TTS reports that the optional runtime is unavailable.
