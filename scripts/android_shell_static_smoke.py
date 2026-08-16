#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ANDROID = ROOT / "apps" / "android" / "ClickShell"


def read(relative: str) -> str:
    path = ROOT / relative
    if not path.exists():
        raise AssertionError(f"missing required file: {relative}")
    return path.read_text(encoding="utf-8")


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise AssertionError(f"{label} must contain {needle!r}")


def forbid(text: str, pattern: str, label: str) -> None:
    if re.search(pattern, text, flags=re.IGNORECASE):
        raise AssertionError(f"{label} must not match {pattern!r}")


def main() -> int:
    readme = read("apps/android/ClickShell/README.md")
    settings = read("apps/android/ClickShell/settings.gradle")
    root_gradle = read("apps/android/ClickShell/build.gradle")
    app_gradle = read("apps/android/ClickShell/app/build.gradle")
    gradle_properties = read("apps/android/ClickShell/gradle.properties")
    build_script = read("scripts/build_android_click_shell.sh")
    manifest = read("apps/android/ClickShell/app/src/main/AndroidManifest.xml")
    activity = read("apps/android/ClickShell/app/src/main/java/com/click/shell/MainActivity.java")
    wav_recorder = read("apps/android/ClickShell/app/src/main/java/com/click/shell/ClickWavRecorder.java")
    native_activity = read("apps/android/ClickShell/app/src/main/java/com/click/shell/ClickNativeReaderActivity.java")
    readium_activity = read("apps/android/ClickShell/app/src/main/java/com/click/shell/ClickReadiumNavigatorActivity.kt")
    tts_session = read("apps/android/ClickShell/app/src/main/java/com/click/shell/ReaderTtsSession.kt")
    native_store = read("apps/android/ClickShell/app/src/main/java/com/click/shell/ClickNativeStore.java")
    sync_config = read("apps/android/ClickShell/app/src/main/java/com/click/shell/ClickSyncConfig.java")
    sync_engine = read("apps/android/ClickShell/app/src/main/java/com/click/shell/ClickSyncEngine.java")
    sync_scheduler = read("apps/android/ClickShell/app/src/main/java/com/click/shell/ClickSyncScheduler.java")
    strings = read("apps/android/ClickShell/app/src/main/res/values/strings.xml")
    security = read("apps/android/ClickShell/app/src/main/res/xml/network_security_config.xml")
    icon = read("apps/android/ClickShell/app/src/main/res/mipmap-anydpi-v26/ic_launcher.xml")
    icon_foreground = read("apps/android/ClickShell/app/src/main/res/drawable/ic_launcher_foreground.xml")
    icon_background = read("apps/android/ClickShell/app/src/main/res/drawable/ic_launcher_background.xml")
    icon_reading = read("apps/android/ClickShell/app/src/main/res/drawable/ic_entry_reading.xml")
    icon_recording = read("apps/android/ClickShell/app/src/main/res/drawable/ic_entry_recording_local.xml")
    icon_hermes = read("apps/android/ClickShell/app/src/main/res/drawable/ic_entry_hermes.xml")

    combined = "\n".join([
        readme,
        settings,
        root_gradle,
        app_gradle,
        gradle_properties,
        build_script,
        manifest,
        activity,
        wav_recorder,
        native_activity,
        readium_activity,
        tts_session,
        native_store,
        sync_config,
        sync_engine,
        sync_scheduler,
        strings,
        security,
        icon,
        icon_foreground,
        icon_background,
        icon_reading,
        icon_recording,
        icon_hermes,
    ])

    for needle in [
        "Click",
        "WebView",
        "/health",
        "/home",
        "/library",
        "/lan/reader",
        "/recordings",
        "/tingle",
        "/hermes",
        "18180",
        "8765",
        "SharedPreferences",
        "usesCleartextTraffic=\"true\"",
        "android.permission.INTERNET",
        "android.permission.RECORD_AUDIO",
        "networkSecurityConfig",
        "enableOnBackInvokedCallback=\"true\"",
        "windowSoftInputMode=\"adjustResize\"",
        "android:icon=\"@mipmap/ic_launcher\"",
        "<adaptive-icon",
        "Click Workspace",
        "ic_entry_recording_local",
        "Voice Memos-style",
        "JavaScriptEnabled",
        "setDomStorageEnabled",
        "BuildConfig.CLICK_DEFAULT_HOST",
        "buildConfigField \"String\", \"CLICK_DEFAULT_HOST\"",
        "app-debug.apk",
        "shasum -a 256",
        "KEY_DEVICE_ID",
        "KEY_ACCESS_TOKEN",
        "KEY_RECENT_HOSTS",
        "MAX_RECENT_HOSTS",
        "Spinner",
        "ArrayAdapter",
        "pendingAudioPermissionRequest",
        "NativeAudioBridge",
        "ClickNativeAudio",
        "usesNativeImeResize",
        "WindowInsetsCompat.Type.ime()",
        "addJavascriptInterface",
        "MediaRecorder",
        "AudioRecord",
        "audio/wav",
        "writeRecording",
        "writeWavHeader",
        "startReaderNote",
        "startHermesVoice",
        "reader_note",
        "hermes_voice",
        "startNativeAudioRecorder",
        "stopNativeAudioRecording",
        "uploadNativeAudio",
        "__clickNativeAudioDidUpload",
        'payload.put("source_feature", "Tingle")',
        'menuButton("Tingle", this::openLocalTingle)',
        "TingleLocalActivity.class",
        "__clickNativeReaderAudioDidUpload",
        "__clickNativeHermesVoiceDidUpload",
        "/v1/android/audio-notes/transcribe",
        "/v1/voice/message",
        "onRequestPermissionsResult",
        "pendingFilePathCallback",
        "onShowFileChooser",
        "REQUEST_FILE_CHOOSER",
        "FileChooserParams.parseResult",
        "rememberRecentHost",
        "recentHostOptions",
        "loadAuthenticatedWebUrl",
        "ClickNativeReaderActivity",
        "ClickReadiumNavigatorActivity",
        "ClickNativeStore",
        "EpubNavigatorFragment",
        "EpubNavigatorFactory",
        "PublicationOpener",
        "DefaultPublicationParser",
        "openReadiumNavigator",
        "Readium 已打开",
        "org.readium.kotlin-toolkit",
        "org.jetbrains.kotlin.android",
        "android.useAndroidX=true",
        "org.gradle.jvmargs=-Xmx4096m",
        "/v1/android/sync/full",
        "/v1/android/sync/changes",
        "/v1/android/sync/operations",
        "/v1/android/tts",
        "TextToSpeech",
        "operation_queue",
        "click-epub-cache",
        "click-cover-cache",
        "cover_local_path",
        "asset_refresh_queue",
        "ASSET_COVER",
    ]:
        require(combined, needle, "Android shell")

    for needle in [
        "首页",
        "阅读",
        "录音",
        "Hermes",
        "刷新",
        "连接与同步",
        "Hermes 端口",
        "本地服务端口",
        "设备 ID",
        "首次连接你的 Mac",
        "最近访问过的 Mac 地址",
        "选择最近访问过的地址",
        "192.168.x.x（首次配对）",
        "handleBackNavigation",
        "onBackPressed",
        "OnBackInvokedCallback",
        "registerOnBackInvokedCallback",
        "edgeSwipeCandidate",
        "setOnTouchListener",
        "高级连接设置",
    ]:
        require(activity, needle, "Android Activity")
    forbid(activity, r"root\.addView\(back,\s*backParams\)", "Android Activity")
    forbid(activity, r"root\.addView\(menu,\s*menuParams\)", "Android Activity")
    for sync_url in [
        "/v1/android/sync/full",
        "/v1/android/sync/changes",
        "/v1/android/sync/operations",
    ]:
        require(sync_engine, sync_url, "shared Android sync engine")
        forbid(activity, re.escape(sync_url), "Android Activity direct sync path")
        forbid(native_activity, re.escape(sync_url), "native reader direct sync path")
        forbid(readium_activity, re.escape(sync_url), "Readium direct sync path")
    require(sync_scheduler, "WorkManager.getInstance", "shared Android sync scheduler")
    require(strings, "<string name=\"app_name\">Click</string>", "Android app name")

    require(readme, "source-complete ARM64 candidate", "Android README")
    require(readme, "baseline is `0.1.7`", "Android README")
    require(readme, "No public APK is published", "Android README")
    require(readme, "physical-device acceptance remains required", "Android README")
    require(readme, "阅读 / 录音 / Hermes", "Android README")
    require(icon_background, "#141821", "local workspace icon background")
    require(icon_foreground, "Click Workspace", "local workspace icon foreground")
    require(icon_recording, "Voice Memos-style", "recording entry icon")
    forbid(readme, r"APK\s+(is\s+)?(complete|done|ready|finished)", "Android README")
    forbid(readme, r"(signed|release)\s+APK\s+(is\s+)?(ready|complete|done|finished)", "Android README")
    forbid(readme, r"已完成\s*APK|已上线|可安装", "Android README")
    forbid(combined, r"\b(?:192\.168|100\.64|10\.0)\.\d{1,3}\.\d{1,3}\b", "Android shell")
    private_home_pattern = "/" + "Users" + "/" + "jiang" + "yu"
    forbid(combined, re.escape(private_home_pattern), "Android shell")

    apk_files = list(ANDROID.rglob("*.apk"))
    for apk in apk_files:
        if "debug" not in apk.name.lower():
            raise AssertionError(f"only local debug APKs are allowed in this pass: {apk}")

    print("android shell static smoke passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"android shell static smoke failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
