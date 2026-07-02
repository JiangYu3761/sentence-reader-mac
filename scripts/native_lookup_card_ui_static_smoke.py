#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NATIVE_READER = ROOT / "Probe" / "NativeSentenceReader" / "SentenceReaderNative.swift"


def fail(message: str) -> None:
    raise SystemExit(f"native lookup card UI static smoke FAIL: {message}")


def require(text: str, needle: str) -> None:
    if needle not in text:
        fail(f"missing `{needle}`")


def extract_function(text: str, name: str) -> str:
    marker = f"private func {name}"
    start = text.find(marker)
    if start < 0:
        fail(f"missing function `{name}`")

    brace_start = text.find("{", start)
    if brace_start < 0:
        fail(f"function `{name}` has no body")

    depth = 0
    for index in range(brace_start, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]

    fail(f"function `{name}` body is not closed")
    return ""


def main() -> int:
    text = NATIVE_READER.read_text(encoding="utf-8")
    show_lookup = extract_function(text, "showLookupAlert")
    show_evidence = extract_function(text, "showLookupEvidenceAlert")

    for marker in [
        "func lookupTTS(text: String, voice: String? = nil) -> URL?",
        "func lookupTTSData(text: String, voice: String? = nil) -> Data?",
        "func lookupCachedTTSData(text: String, voice: String? = nil) -> Data?",
        "baseURL.appendingPathComponent(\"lookup/tts/status\")",
        "private func downloadLookupAudioData",
        "private var lookupAudioPlayer: AVPlayer?",
        "private var lookupAudioDataPlayer: AVAudioPlayer?",
        "private var lookupSoundPlayer: NSSound?",
        "private var lookupAudioProcess: Process?",
        "private var lookupActionTargets: [LookupActionTarget] = []",
        "private final class LookupWordButton",
        "acceptsFirstMouse",
        "private func playLookupAudioData",
        "/usr/bin/afplay",
        "process.run()",
        "NSSound(data: data)",
        "AVAudioPlayer(data: data)",
        "private func speakChineseMeaning",
        "private func speakChineseFallback",
        "private func speakEnglishFallback",
        "lookupCachedTTSData(text: trimmed, voice: \"en-US-BrianNeural\")",
        "lookupTTS(text: trimmed, voice: \"en-US-BrianNeural\")",
        "lookupCachedTTSData(text: trimmed, voice: \"zh-CN-YunjianNeural\")",
        "lookupTTS(text: trimmed, voice: \"zh-CN-YunjianNeural\")",
        "首次生成高质量读音，先用本机语音朗读",
        "首次生成高质量释义朗读，先用本机语音朗读",
        "URL(fileURLWithPath: \"/usr/bin/say\")",
        "\"Samantha\"",
        "request.timeoutInterval = 0.35",
        "semaphore.wait(timeout: .now() + 0.45)",
        "private func decorateLookupActionButtons",
        "private static func lookupPartOfSpeechTitle",
        "private static func lookupPartOfSpeechShortTitle",
        "读释义",
        "speaker.wave.2.fill",
        "en-US-BrianNeural",
        ".sr-word-focused",
        "function focusWordHit",
        "function lookupWordHitFromEvent",
    ]:
        require(text, marker)

    for marker in [
        "alert.informativeText = \"\"",
        "makeLookupActionButton(",
        "title: displayWord",
        "size: 34",
        "LookupWordButton(title: displayWord",
        "clickableWordButton.toolTip = \"点击朗读单词\"",
        "clickableWordButton.alignment = .center",
        "已点击单词，准备朗读",
        "LookupActionTarget { [weak self] in",
        "DispatchQueue.main.asyncAfter(deadline: .now() + 0.25)",
        "title: \"读释义\"",
        "title: \"证据\"",
        "textView.alignment = .center",
        "textView.textContainerInset = NSSize(width: 0, height: 17)",
        "speakChineseMeaning",
        "speakEnglish(displayWord)",
        "alert.addButton(withTitle: \"关闭\")",
    ]:
        require(show_lookup, marker)

    for forbidden in [
        "(\"读句\", \"speak_sentence\")",
        "(\"读词\", \"speak_word\")",
        "(\"读释义\", \"speak_meaning\")",
        "title: \"读词\"",
        "英文例句：",
        "对应中文：",
    ]:
        if forbidden in show_lookup:
            fail(f"default lookup card still contains primary evidence/noise marker `{forbidden}`")

    if "focusWordHit(hit);" not in text or "post({ type: 'lookup', word: hit.word" not in text:
        fail("single-click lookup does not use word-level focus before posting lookup")
    if "focus(sentence);\n          const hit" in text:
        fail("single-click lookup still focuses the whole sentence before word lookup")

    for marker in [
        "这些信息用于复核，不作为默认查词内容。",
        "英文上下文：",
        "中文证据：",
        "来源：",
        "对齐：",
        "本书出现：",
        "(\"复习\", \"reviewing\")",
        "(\"掌握\", \"known\")",
    ]:
        require(show_evidence, marker)

    print("native lookup card UI static smoke PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
