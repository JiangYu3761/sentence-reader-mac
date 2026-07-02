# Local Workspace Android Shell

Status: local P1 scaffold implemented and extended through the Mobile Workspace P1.1-P6 local acceptance route. Debug APK is a local build target; no signed release APK is published yet.

This Android shell is the local mobile workspace, not the Click reader itself. It uses a native Android Activity plus WebView to connect to the Mac local service, check `/health`, open `/home`, and show the workspace with Click 阅读 / 录音 / Hermes.

The three entries route to:

- Click 阅读: `/library`, then the existing `/lan/reader` after a book is opened.
- 录音: `/recordings`, saving durable recording assets into the Mac `~/Documents/Recordings` total recording store.
- Hermes: `/hermes`, using the Mac-side Hermes runtime through the local workspace gateway.

It does not implement a second reader, Android-local EPUB import, Android-local PostgreSQL, offline reading, cloud sync, or a separate database.

Current user flow:

1. Open 本地工作台.
2. Enter the Mac LAN address, local service port, and Hermes port. The default local service port is `18180`; the default Hermes port shown for diagnostics is `8765`.
3. After a successful connection, the app stores the recent Mac address locally. The next connection screen shows a "最近访问过的 Mac 地址" dropdown so a changed Wi-Fi address can be selected without retyping from scratch.
4. The app keeps a stable local `device_id`. Local same-LAN use is allowed by default; strict device approval can be enabled later with `CLICK_MOBILE_REQUIRE_APPROVAL=1`.
5. The app checks `/health`.
6. On success it opens `/home` in a full-screen WebView.
7. A visible back button and Android Back handling return within the WebView before leaving the workspace home.
8. The floating menu can return to 首页, Click 阅读, 录音, Hermes, refresh, or change the Mac address.

Mobile Workspace boundary:

- Device access can use the local `/v1/mobile/access/*` routes when strict approval is enabled. This is local-only access control, not a public account system.
- `/recordings` lists durable recordings from `~/Documents/Recordings`, can edit title/category/tags, can hide without deleting files, and can trigger reprocess dry-run.
- `/hermes` supports text chat and temporary voice messages through VoiceInbox. These messages are not durable recording assets unless a later explicit save flow is added.
- edge-tts audio replies use the local Mac command when available; text replies still work without TTS.

Build boundary:

- This repo contains the Android project scaffold under `apps/android/ClickShell`.
- A debug APK can be produced after Java, Gradle, and Android SDK are available.
- The repo does not publish a signed release APK yet.
- Build with:

```bash
scripts/build_android_click_shell.sh
```

The build script copies the debug APK and its SHA256 file to the desktop Quark backup folder root for phone testing. It does not create nested backup folders.

P1.1 icon boundary:

- The Android launcher icon is a self-drawn dark local-workspace icon with a book, click point, and light audio cue.
- The workspace entry icons are separate local resources for Click reading, recording, and Hermes.
- The local recording entry can use a Voice Memos-style microphone/waveform cue for private builds, but the resource is replaceable before public distribution.

Or import `apps/android/ClickShell` into Android Studio and run the `app` configuration. Android Studio can also generate a Gradle wrapper later if we want this project to build with `./gradlew`.
