#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ANDROID_DIR="$ROOT_DIR/apps/android/ClickShell"
TOOLCHAIN_DIR="$HOME/Library/Application Support/ClickAndroidToolchain"
DEFAULT_SDK_DIR="$HOME/Library/Android/sdk"
REQUIRED_GRADLE_VERSION="8.11.1"

if [ -d "$TOOLCHAIN_DIR/jdk/jdk-17/Contents/Home" ]; then
  export JAVA_HOME="$TOOLCHAIN_DIR/jdk/jdk-17/Contents/Home"
  export PATH="$JAVA_HOME/bin:$PATH"
fi

if [ -x "$TOOLCHAIN_DIR/gradle/gradle-$REQUIRED_GRADLE_VERSION/bin/gradle" ]; then
  export PATH="$TOOLCHAIN_DIR/gradle/gradle-$REQUIRED_GRADLE_VERSION/bin:$PATH"
fi

if [ -d "$DEFAULT_SDK_DIR" ]; then
  export ANDROID_HOME="${ANDROID_HOME:-$DEFAULT_SDK_DIR}"
  export ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-$ANDROID_HOME}"
  export PATH="$ANDROID_HOME/cmdline-tools/latest/bin:$ANDROID_HOME/platform-tools:$PATH"
fi

export CLICK_DEFAULT_HOST="${CLICK_DEFAULT_HOST:-}"

if ! java -version >/dev/null 2>&1; then
  echo "Java Runtime is required before building the Android Click shell." >&2
  exit 70
fi

ACTUAL_JAVA_VERSION="$(java -version 2>&1 | awk -F '"' '/version/ { print $2; exit }')"
ACTUAL_JAVA_MAJOR="${ACTUAL_JAVA_VERSION%%.*}"
if [ "$ACTUAL_JAVA_MAJOR" = "1" ]; then
  ACTUAL_JAVA_MAJOR="$(printf '%s' "$ACTUAL_JAVA_VERSION" | cut -d. -f2)"
fi
if [ "$ACTUAL_JAVA_MAJOR" != "17" ]; then
  echo "JDK 17 is required; found ${ACTUAL_JAVA_VERSION:-unknown}. No toolchain is downloaded automatically." >&2
  exit 70
fi

if ! command -v gradle >/dev/null 2>&1; then
  echo "Gradle $REQUIRED_GRADLE_VERSION is required for Android Gradle Plugin 8.10.x." >&2
  exit 70
fi

ACTUAL_GRADLE_VERSION="$(gradle --version | awk '/^Gradle / { print $2; exit }')"
if [ "$ACTUAL_GRADLE_VERSION" != "$REQUIRED_GRADLE_VERSION" ]; then
  echo "Gradle $REQUIRED_GRADLE_VERSION is required; found ${ACTUAL_GRADLE_VERSION:-unknown}. No toolchain is downloaded automatically." >&2
  exit 70
fi

if [ -z "${ANDROID_HOME:-}" ] || [ ! -d "$ANDROID_HOME/platforms/android-36-ext19" ]; then
  echo "Android SDK platform 36-ext19 is required by the Click PDF reader contract. Install it explicitly before building." >&2
  exit 70
fi

cd "$ANDROID_DIR"
gradle --no-daemon --max-workers=1 assembleDebug

APK_SOURCE="$ANDROID_DIR/app/build/outputs/apk/debug/app-debug.apk"
SHA_TARGET="$APK_SOURCE.sha256"

if [ ! -f "$APK_SOURCE" ]; then
  echo "Expected debug APK was not produced: $APK_SOURCE" >&2
  exit 74
fi

shasum -a 256 "$APK_SOURCE" > "$SHA_TARGET"

echo "Android debug APK: $APK_SOURCE"
echo "SHA256: $(awk '{print $1}' "$SHA_TARGET")"

if [ -n "${CLICK_ANDROID_APK_DIR:-}" ]; then
  mkdir -p "$CLICK_ANDROID_APK_DIR"
  cp "$APK_SOURCE" "$CLICK_ANDROID_APK_DIR/Click-Android-debug.apk"
  cp "$SHA_TARGET" "$CLICK_ANDROID_APK_DIR/Click-Android-debug.apk.sha256"
  echo "Optional copy: $CLICK_ANDROID_APK_DIR/Click-Android-debug.apk"
fi
