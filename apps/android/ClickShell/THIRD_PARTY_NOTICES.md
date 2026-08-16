# Third-Party Notices

## Readium Kotlin Toolkit

- Project: <https://github.com/readium/kotlin-toolkit>
- License: BSD 3-Clause License
- Use: Android EPUB publication opening, navigation, locators, pagination, table of contents and fixed-layout rendering.
- Delivery: pinned Maven dependencies; Click does not fork or copy the Readium source tree.

## Coil

- Project: <https://github.com/coil-kt/coil>
- License: Apache License 2.0
- Use: Android book-cover decoding, memory caching and lifecycle-aware request cancellation.
- Delivery: pinned Maven dependency; Click keeps its downloaded cover files as the offline source of truth.

## OpenCC / opencc-python-reimplemented

- OpenCC project: <https://github.com/BYVoid/OpenCC>
- Python binding: <https://github.com/yichen0831/opencc-python>
- Licenses: OpenCC Apache License 2.0; `opencc-python-reimplemented` MIT License
- Use: local `tw2sp` and `s2twp` conversion when producing length-preserving display sidecars on the Mac.
- The source EPUB remains unchanged and no online conversion service is used.

## EPUBCheck

- Project: <https://github.com/w3c/epubcheck>
- License: BSD 3-Clause License
- Use: optional Mac-side EPUB conformance diagnostics during import.
- Delivery: official v5.3.0 release archive installed outside the repository after SHA-256 verification; it is not embedded in the Android APK.

## charset-normalizer

- Project: <https://github.com/jawah/charset_normalizer>
- License: MIT License
- Use: final, confidence-gated character-encoding detection after BOM, declared encoding, strict UTF-8 and strict GB18030 checks fail.

## sherpa-onnx

- Project: <https://github.com/k2-fsa/sherpa-onnx>
- License: Apache License 2.0
- Use: Android ARM64 local TTS runtime and the upstream Kotlin `Tts.kt` wrapper.
- The copied wrapper retains its Xiaomi Corporation copyright notice.

The base APK contains the local inference runtime but does not contain a voice model.

## Kokoro-82M-v1.1-zh

- Model source: <https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh>
- sherpa-onnx conversion: <https://k2-fsa.github.io/sherpa/onnx/tts/pretrained_models/kokoro.html>
- License: Apache License 2.0
- Use: optional, user-imported local voice package. It is not committed to Git and is not embedded in the base APK.

The Click debug acceptance package validates the supported model archive with SHA256 before installing it into the app-private directory.

## eSpeak NG data

- Project: <https://github.com/espeak-ng/espeak-ng>
- License: GNU General Public License v3.0 or later
- Use: language and pronunciation data inside the optional Kokoro voice package.
- Delivery: not embedded in the base APK and not committed to Git. The optional package carries this notice and the corresponding GPL text/source reference.

The optional voice archive is a local acceptance artifact. Public redistribution requires preserving all applicable model and eSpeak NG license obligations; P1.3 does not publish that archive.
