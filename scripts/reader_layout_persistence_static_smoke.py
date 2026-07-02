#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "Probe" / "NativeSentenceReader" / "SentenceReaderNative.swift"
API = ROOT / "reader_api" / "app.py"


def fail(message: str) -> None:
    raise SystemExit(f"reader layout persistence static smoke FAIL: {message}")


def require(text: str, marker: str) -> None:
    if marker not in text:
        fail(f"missing `{marker}`")


def forbid(text: str, marker: str) -> None:
    if marker in text:
        fail(f"forbidden `{marker}`")


def main() -> int:
    native = NATIVE.read_text(encoding="utf-8")
    api = API.read_text(encoding="utf-8")

    for marker in [
        "body.sr-large-font #sr-page-surface",
        "document.body.classList.toggle('sr-large-font', fontSize >= 24)",
        "overflow-wrap: anywhere !important",
        "word-break: break-word !important",
        "table-layout: fixed !important",
        "div.right, #main1",
        "float: none !important",
        "text-align: left !important",
        "正在保存备注到 Reader API",
        "红标已保存到 Reader API",
        "红标保存失败，已恢复数据库状态",
        "红标取消已保存到 Reader API",
        "红标取消保存失败，已恢复数据库状态",
        "func deleteAnnotation(annotationID: String) -> Bool",
        "lineHeight: max(1.2, min(2.05, settings.lineHeight))",
        "marginX: max(2, min(40, settings.marginX))",
        "const lineHeight = Math.max(1.2, Math.min(2.05",
    ]:
        require(native, marker)

    for marker in [
        "body.reader-large-font #reader",
        "document.body.classList.toggle('reader-large-font', Number(settings.fontSize) >= 24)",
        "overflow-wrap:anywhere",
        "word-break:break-word",
        "table-layout:fixed",
        "#reader div.right, #reader #main1",
        "orphans:1; widows:1",
        "-webkit-column-break-inside:auto",
        "page-break-inside:auto",
        "#reader p { margin-bottom:.34em; }",
        "await json('/annotations'",
        "saved = await json(`/annotations/${existing.id}`",
        "await refreshAnnotations()",
        "#toolbarMode",
        "body.sentence-mode #toolbarActions { display:none; }",
        "body.sentence-mode #sentenceBar.show { display:grid; }",
        "body.note-editor-mode #sentenceBar.show { display:none; }",
        "document.body.classList.add('sentence-mode')",
        "document.body.classList.remove('sentence-mode')",
        "<div id=\"readingStats\"></div>",
        "<div id=\"voiceToast\" aria-live=\"polite\"></div>",
        "<section id=\"noteEditor\" aria-hidden=\"true\">",
        "id=\"noteEditorText\"",
        "id=\"noteEditorVoice\"",
        "id=\"noteEditorClean\"",
        "id=\"noteEditorSave\"",
        "function updateReadingStats()",
        "#readingStats { display:none; }",
        "#voiceToast { position:fixed;",
        "#noteEditor { position:fixed;",
        "function openNoteEditor(options = {})",
        "function closeNoteEditor()",
        "function appendNoteEditorText(text)",
        "function replacePendingNoteText(text)",
        "function addPendingNoteText()",
        "function saveNoteEditor()",
        "function cleanNoteEditorText()",
        "function noteEditorOpen()",
        "function pollAudioNote(audioNoteID)",
        "pendingAudioPolls: new Map()",
        "voiceNotePendingText",
        "语音转写中...",
        "语音已保存，后台转写中",
        "后台转写完成后会自动补到备注",
        "/audio-notes/${audioNoteID}",
        "/annotations/${annotationID}/clean",
        "top:calc(var(--lan-toolbar-height)",
        "bottom:auto; max-height:min(64vh, 460px)",
        "document.body.classList.add('note-editor-mode')",
        "document.body.classList.remove('note-editor-mode')",
        "state.noteEditorAudioNoteID",
        "nativeReaderAudioRecording",
        "function nativeReaderAudioAvailable()",
        "ClickNativeAudio.startReaderNote(state.book.id)",
        "__clickNativeReaderAudioDidUpload",
        "if (noteEditorOpen())",
        "appendNoteEditorText(transcript)",
        "replacePendingNoteText(transcript)",
        "已转写到备注框",
        "function showVoiceToast(title, body = '', mode = 'info', options = {})",
        "function hideVoiceToast(delay = 0)",
        "voiceToastTimer",
        "status(`${state.pageIndex + 1}/${state.totalPages}`);",
        "if (stats) stats.textContent = '';",
        "min=\"1.2\" max=\"2.05\"",
        "min=\"4\" max=\"40\"",
        "data-open-book=\"${esc(book.id)}\"",
        "bindOpenBookTargets($('continueHero'))",
        "['topImport'].forEach",
        "@media (max-width: 1024px)",
        "#toolbar { top:auto; bottom:0;",
        "#readerWrap { top:0; bottom:calc(var(--lan-toolbar-height) + env(safe-area-inset-bottom)); }",
        "#toolbarActions button { min-width:48px; min-height:42px; padding:8px 10px; border-radius:10px; font-size:16px; font-weight:750; }",
        "#toolbarActions button { min-width:42px; min-height:42px; padding:7px 8px; border-radius:10px; font-size:15px; font-weight:760; }",
        "#sentenceBar button { min-height:42px; border-radius:10px; padding:8px 9px; font-size:15px; font-weight:750; }",
        "#sentenceBar button { min-width:0; min-height:42px; padding:7px 6px; border-radius:10px; font-size:15px; font-weight:760; }",
        "--reader-bottom-pad:max(8px, env(safe-area-inset-bottom) + 4px)",
        "const prevButton = $('prev');",
        "if (prevButton) prevButton.disabled",
        "if (prevButton) prevButton.onclick = () => turnPage(-1);",
        "showVoiceToast('系统录音已打开'",
        "showVoiceToast('正在录音'",
        "showVoiceToast('正在处理语音'",
        "showVoiceToast('语音备注已保存'",
        "停止并保存",
    ]:
        require(api, marker)

    forbid(api, 'id="sideImport"')
    forbid(api, 'id="libraryImport"')
    forbid(api, "['topImport','sideImport','libraryImport']")
    forbid(api, "#sentenceBar { position:fixed;")
    forbid(api, "if (prefersSystemAudioCapture())")
    forbid(api, "if (!openAudioCaptureFallback()) startBrowserSpeechNote();")
    forbid(api, "#toolbarActions button { min-width:30px; min-height:28px; padding:4px 6px; font-size:11px; }")
    forbid(api, "#sentenceBar button { min-width:0; min-height:28px; padding:4px 4px; font-size:11px; }")
    forbid(api, "本章 ${englishWords} 词")
    forbid(api, "status(`${title} · ${state.pageIndex + 1}/${state.totalPages}`);")
    forbid(api, "--reader-bottom-pad:max(18px, env(safe-area-inset-bottom) + 10px)")
    forbid(api, '<button id="prev"')
    forbid(api, '<button id="next"')
    forbid(api, "#prev, #next")
    forbid(api, "status('正在上传语音到 Mac 转写...')")
    forbid(api, "status('正在录音，再点一次“语音”结束')")
    forbid(api, "prompt('备注'")

    print("reader layout persistence static smoke PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
