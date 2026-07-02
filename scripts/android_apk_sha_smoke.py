#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path


def backup_dir() -> Path:
    configured = os.environ.get("CLICK_ANDROID_APK_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Desktop" / "夸克备份"


def main() -> int:
    directory = backup_dir()
    files = sorted(directory.glob("LocalWorkspace-Android-debug-*.apk"), key=lambda path: path.stat().st_mtime)
    if not files:
        raise AssertionError(f"missing LocalWorkspace debug APK in {directory}")
    apk = files[-1]
    sha_file = Path(str(apk) + ".sha256")
    if not sha_file.exists():
        raise AssertionError(f"missing sha256 sidecar: {sha_file}")
    expected = sha_file.read_text(encoding="utf-8").split()[0].strip()
    actual = hashlib.sha256(apk.read_bytes()).hexdigest()
    if expected != actual:
        raise AssertionError(f"sha256 mismatch for {apk.name}: expected {expected}, got {actual}")
    print(f"android apk sha smoke passed: {apk} {actual}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"android apk sha smoke failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
