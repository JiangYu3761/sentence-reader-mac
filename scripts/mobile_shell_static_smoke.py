#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    full = ROOT / path
    if not full.exists():
        raise AssertionError(f"missing required file: {path}")
    return full.read_text(encoding="utf-8")


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise AssertionError(f"{label} must contain {needle!r}")


def forbid(text: str, pattern: str, label: str) -> None:
    if re.search(pattern, text, flags=re.IGNORECASE):
        raise AssertionError(f"{label} must not match {pattern!r}")


def main() -> int:
    construction = read("docs/mobile_app_shell_construction.md")
    require(construction, "Click iPad Mobile App Shell", "construction doc")

    project = read("apps/ios/ClickShell/ClickShell.xcodeproj/project.pbxproj")
    scheme = read("apps/ios/ClickShell/ClickShell.xcodeproj/xcshareddata/xcschemes/ClickShell.xcscheme")
    require(project, "ClickShell.app", "xcode project")
    require(project, "TARGETED_DEVICE_FAMILY = 2", "xcode project")
    require(scheme, "ClickShell", "xcode scheme")

    app = read("apps/ios/ClickShell/ClickShell/ClickShellApp.swift")
    content = read("apps/ios/ClickShell/ClickShell/ContentView.swift")
    store = read("apps/ios/ClickShell/ClickShell/ConnectionStore.swift")
    connection = read("apps/ios/ClickShell/ClickShell/ConnectionView.swift")
    webview = read("apps/ios/ClickShell/ClickShell/ReaderWebView.swift")
    toolbar = read("apps/ios/ClickShell/ClickShell/ShellToolbar.swift")
    plist = read("apps/ios/ClickShell/ClickShell/Info.plist")
    ios_readme = read("apps/ios/ClickShell/README.md")
    android_readme = read("apps/android/ClickShell/README.md")

    combined_ios = "\n".join([app, content, store, connection, webview, toolbar, plist, ios_readme])
    for needle in [
        "SwiftUI",
        "WKWebView",
        "/home",
        "/library",
        "/recordings",
        "/hermes",
        "/health",
        "UserDefaults",
        "deviceID",
        "accessTokenInput",
        "18180",
        "NSLocalNetworkUsageDescription",
        "NSAllowsArbitraryLoadsInWebContent",
        "NSAllowsLocalNetworking",
        "NSMicrophoneUsageDescription",
        "Click",
    ]:
        require(combined_ios, needle, "iOS shell")

    require(toolbar, "ellipsis.circle.fill", "iOS light toolbar")
    require(toolbar, "首页", "iOS shell toolbar")
    require(toolbar, "阅读", "iOS shell toolbar")
    require(toolbar, "录音", "iOS shell toolbar")
    require(toolbar, "Hermes", "iOS shell toolbar")
    require(toolbar, "刷新", "iOS shell toolbar")
    require(toolbar, "更换地址", "iOS shell toolbar")
    require(webview, "allowsBackForwardNavigationGestures", "WKWebView")
    require(combined_ios, "ignoresSafeArea", "WKWebView shell")
    require(connection, "Mac 局域网地址", "connection placeholder")
    require(connection, "设备令牌", "connection token")
    require(connection, "设备 ID：", "connection device id")

    require(android_readme, "source-complete ARM64 candidate", "Android README")
    require(android_readme, "No public APK is published", "Android README")
    require(android_readme, "阅读 / 录音 / Hermes", "Android README")
    forbid(android_readme, r"Android\s+APK\s+(is\s+)?(complete|done|ready|finished)", "Android README")
    forbid(android_readme, r"(signed|release)\s+APK\s+(is\s+)?(ready|complete|done|finished)", "Android README")
    forbid(android_readme, r"已完成\s*APK|已上线|可安装", "Android README")

    checked_paths = [
        "docs/mobile_app_shell_construction.md",
        "apps/ios/ClickShell/README.md",
        "apps/android/ClickShell/README.md",
    ]
    for path in checked_paths:
        text = read(path)
        forbid(text, r"\b(?:192\.168|100\.64|10\.0)\.\d{1,3}\.\d{1,3}\b", path)
        private_home_pattern = "/" + "Users" + "/" + "jiang" + "yu"
        forbid(text, re.escape(private_home_pattern), path)

    apk_files = list((ROOT / "apps/android/ClickShell").rglob("*.apk"))
    for apk in apk_files:
        if "debug" not in apk.name.lower():
            raise AssertionError(f"only local debug APKs are allowed: {apk}")

    print("mobile shell static smoke passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"mobile shell static smoke failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
