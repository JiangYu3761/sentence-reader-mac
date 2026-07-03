#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SWIFT = ROOT / "Probe" / "NativeSentenceReader" / "SentenceReaderNative.swift"
APP = ROOT / "reader_api" / "app.py"
DOC = ROOT / "docs" / "interaction_contract.md"
STATUS = ROOT / "docs" / "current_status.md"
ACCEPTANCE = ROOT / "docs" / "product_acceptance.md"


COMMON_MARKERS = [
    "contractVersion: 'sentence-reader-interaction-v1'",
    "priority: 'sentence-reader-first'",
    "sentenceContextWinsEvenWithSelection: true",
    "copyPath: 'command-c-or-non-sentence-context-menu'",
    "shouldLetSystemHandleContext",
    "claimSentenceEvent",
    "hasSystemTextSelection",
    "isEditableTarget",
    "context-click-red",
]


def require_contains(path: Path, markers: list[str], missing: dict[str, list[str]]) -> None:
    text = path.read_text(encoding="utf-8")
    absent = [marker for marker in markers if marker not in text]
    if absent:
        missing[str(path)] = absent


def context_guard_has_sentence_priority(text: str, sentence_marker: str) -> bool:
    start = text.find("function shouldLetSystemHandleContext")
    if start < 0:
        return False
    window = text[start : start + 700]
    sentence_index = window.find(sentence_marker)
    selection_index = window.find("return hasSystemTextSelection")
    return sentence_index >= 0 and selection_index >= 0 and sentence_index < selection_index


def main() -> int:
    missing_files = [str(path) for path in [SWIFT, APP, DOC, STATUS, ACCEPTANCE] if not path.exists()]
    missing_markers: dict[str, list[str]] = {}
    if missing_files:
        print(f"interaction contract smoke FAIL missing_files={missing_files}")
        return 1

    require_contains(
        SWIFT,
        COMMON_MARKERS
        + [
            "return toggleRedFromSecondaryEvent(event);",
            "function toggleRedFromSecondaryEvent(event)",
            "lastSecondaryRedAt",
            "lastSecondaryRedIndex",
            "now - lastSecondaryRedAt < 320",
            "if (sentence) {\n          return toggleRedSentences([sentence], event);\n        }",
            "document.addEventListener('mousedown', function (event) {\n        if (!event || event.button !== 2) { return; }\n        return toggleRedFromSecondaryEvent(event);",
            "document.addEventListener('auxclick', function (event) {\n        if (!event || event.button !== 2) { return; }\n        return toggleRedFromSecondaryEvent(event);",
            "english-click-lookup",
            "installApplicationMenu",
            "退出 Sentence Reader",
            "keyEquivalent: \"q\"",
            "key == \"q\"",
            "notePreviewTimer = window.setTimeout(function ()",
            "post({ type: 'lookup'",
        ],
        missing_markers,
    )
    require_contains(
        APP,
        COMMON_MARKERS
        + [
            "toggleRed().catch",
            "long-press-red",
            "english-tap-lookup",
            "reader-keyboard-note-red-voice-v1",
            "key === 'n'",
            "key === 'r'",
            "key === 'v'",
            "sentenceTapTimer",
            "clearTimeout(state.sentenceTapTimer)",
            "showLookup(word, node.textContent || '', node.dataset.srIndex || '')",
        ],
        missing_markers,
    )
    require_contains(
        DOC,
        [
            "Sentence Reader Interaction Contract",
            "sentence-reader-interaction-v1",
            "English word in sentence text",
            "pending lookup is cancelled",
            "Sentence Reader owns it and toggles red highlight",
            "Command+C",
            "Command+Q",
            "Inputs, textareas, buttons, controls",
            "Mac sentence text | Two-finger tap",
            "iPad sentence action bar | Red button",
            "Desktop Web / Windows route focused sentence",
            "聚焦句子后 `N`",
            "聚焦句子后 `R`",
            "聚焦句子后 `V`",
        ],
        missing_markers,
    )
    require_contains(
        ACCEPTANCE,
        [
            "Keep the interaction-router contract stable",
            "single-click/single-tap on an English word",
            "Mac two-finger tap for whole-sentence red highlight",
            "iPad bottom action bar for red highlight",
            "Command+C",
            "Command+Q",
        ],
        missing_markers,
    )

    swift_text = SWIFT.read_text(encoding="utf-8")
    app_text = APP.read_text(encoding="utf-8")
    forbidden_swift_markers = [
        ".rightMouseDown",
        "__sentenceReaderToggleRedAtPoint",
    ]
    present_forbidden = [marker for marker in forbidden_swift_markers if marker in swift_text]
    if present_forbidden:
        missing_markers.setdefault(str(SWIFT), []).extend(
            [f"forbidden secondary red route: {marker}" for marker in present_forbidden]
        )
    if not context_guard_has_sentence_priority(swift_text, "if (sentence) { return false; }"):
        missing_markers.setdefault(str(SWIFT), []).append("context guard must route sentence before selection")
    if not context_guard_has_sentence_priority(app_text, "if (node) return false;"):
        missing_markers.setdefault(str(APP), []).append("context guard must route sentence before selection")

    status_text = STATUS.read_text(encoding="utf-8")
    obsolete_phrases = [
        "trackpad two-finger context click now passes through to the system/WebKit context menu",
        "context click for red highlight when there is no active text selection",
    ]
    obsolete = [phrase for phrase in obsolete_phrases if phrase in status_text]
    if obsolete:
        missing_markers.setdefault(str(STATUS), []).extend([f"obsolete phrase: {phrase}" for phrase in obsolete])

    if missing_markers:
        print(f"interaction contract smoke FAIL missing_markers={missing_markers}")
        return 1

    print("interaction contract smoke PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
