#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ANDROID_DIR="$ROOT_DIR/apps/android/ClickShell"
TOOLCHAIN_DIR="$HOME/Library/Application Support/ClickAndroidToolchain"
DEFAULT_SDK_DIR="$HOME/Library/Android/sdk"

if [ -d "$TOOLCHAIN_DIR/jdk/jdk-17/Contents/Home" ]; then
  export JAVA_HOME="$TOOLCHAIN_DIR/jdk/jdk-17/Contents/Home"
  export PATH="$JAVA_HOME/bin:$PATH"
fi

if [ -x "$TOOLCHAIN_DIR/gradle/gradle-8.10.2/bin/gradle" ]; then
  export PATH="$TOOLCHAIN_DIR/gradle/gradle-8.10.2/bin:$PATH"
fi

if [ -d "$DEFAULT_SDK_DIR" ]; then
  export ANDROID_HOME="${ANDROID_HOME:-$DEFAULT_SDK_DIR}"
  export ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-$ANDROID_HOME}"
  export PATH="$ANDROID_HOME/cmdline-tools/latest/bin:$ANDROID_HOME/platform-tools:$PATH"
fi

if [ -z "${CLICK_DEFAULT_HOST:-}" ]; then
  CLICK_DEFAULT_HOST="$(ipconfig getifaddr en0 2>/dev/null || true)"
  if [ -z "$CLICK_DEFAULT_HOST" ]; then
    CLICK_DEFAULT_HOST="$(ipconfig getifaddr en1 2>/dev/null || true)"
  fi
  export CLICK_DEFAULT_HOST
fi

if ! java -version >/dev/null 2>&1; then
  echo "Java Runtime is required before building the Android Click shell." >&2
  exit 70
fi

if ! command -v gradle >/dev/null 2>&1; then
  echo "Gradle is required before building the Android Click shell." >&2
  exit 70
fi

cd "$ANDROID_DIR"
gradle assembleDebug

APK_SOURCE="$ANDROID_DIR/app/build/outputs/apk/debug/app-debug.apk"
DESKTOP_DIR="${DESKTOP_DIR:-$HOME/Desktop}"
BACKUP_DIR="${CLICK_ANDROID_APK_DIR:-$DESKTOP_DIR/夸克备份}"
STAMP="$(date +%Y%m%d)"
APK_TARGET="$BACKUP_DIR/LocalWorkspace-Android-debug-$STAMP.apk"
SHA_TARGET="$APK_TARGET.sha256"

if [ ! -f "$APK_SOURCE" ]; then
  echo "Expected debug APK was not produced: $APK_SOURCE" >&2
  exit 74
fi

mkdir -p "$BACKUP_DIR"
cp "$APK_SOURCE" "$APK_TARGET"
shasum -a 256 "$APK_TARGET" > "$SHA_TARGET"

echo "Android debug APK: $APK_TARGET"
echo "SHA256: $(awk '{print $1}' "$SHA_TARGET")"
