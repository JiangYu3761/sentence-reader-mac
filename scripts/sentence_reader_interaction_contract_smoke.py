#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SWIFT = ROOT / "Probe" / "NativeSentenceReader" / "SentenceReaderNative.swift"
APP = ROOT / "reader_api" / "app.py"
DOC = ROOT / "docs" / "interaction_contract.md"
STATUS = ROOT / "docs" / "current_status.md"
ACCEPTANCE = ROOT / "docs" / "product_acceptance.md"


def require_contains(path: Path, markers: list[str], missing: dict[str, list[str]]) -> None:
    text = path.read_text(encoding="utf-8")
    absent = [marker for marker in markers if marker not in text]
    if absent:
        missing[str(path)] = absent


def swift_selection_context_does_not_route_to_secondary_red(text: str) -> bool:
    start = text.find("function shouldLetSystemHandleContext")
    if start < 0:
        return False
    window = text[start : start + 420]
    selection_index = window.find("if (hasReaderTextSelection() && selectionActionBarVisible()) { return true; }")
    sentence_index = window.find("const sentence = sentenceFromTarget")
    return selection_index >= 0 and sentence_index >= 0 and selection_index < sentence_index


def swift_without_pdf_view(text: str) -> str:
    start = text.find("private final class ClickPDFView: PDFView")
    end = text.find("private final class MacPDFReaderController", start)
    if start < 0 or end < 0:
        return text
    return text[:start] + text[end:]


def main() -> int:
    paths = [SWIFT, APP, DOC, STATUS, ACCEPTANCE]
    missing_files = [str(path) for path in paths if not path.exists()]
    missing_markers: dict[str, list[str]] = {}
    if missing_files:
        print(f"interaction contract smoke FAIL missing_files={missing_files}")
        return 1

    require_contains(
        SWIFT,
        [
            "contractVersion: 'sentence-reader-interaction-v1'",
            "priority: 'sentence-reader-first'",
            "sentenceContextWinsOnlyWithoutSelection: true",
            "selectedTextActionBar: 'sr-selection-action-bar'",
            "copyPath: 'selection-action-bar-copy-button-not-command-c'",
            "function readerSelectionPayload()",
            "function hasReaderTextSelection()",
            "function selectionActionBarNode()",
            "id = 'sr-selection-action-bar'",
            "data-sr-selection-action=\"copy\"",
            "data-sr-selection-action=\"red\"",
            "data-sr-selection-action=\"note\"",
            "type: 'selectionCopy'",
            "type: 'selectionRed'",
            "type: 'selectionNote'",
            "取消标红",
            "function selectionPayloadHasExactRed(payload)",
            "function removeSelectionRedFragments(fragments)",
            "type: 'selectionRedRemove'",
            "\"mode\": \"text_selection\"",
            "selectionMode: \"text_selection_note\"",
            "NSPasteboard.general.setString(rawText, forType: .string)",
            "document.addEventListener('dblclick', function (event)",
            "type: 'note'",
            "function toggleRedFromSecondaryEvent(event)",
            "function sentenceFromPoint(x, y)",
            "function sentenceFromEvent(event)",
            "function clearTextSelectionAfterSecondaryRed()",
            "lastSecondaryRedClaimedAt",
            "event.type === 'contextmenu'",
            "now - lastSecondaryRedClaimedAt < 650",
            "claimSentenceEvent(event);",
            "return toggleRed(sentence, event);",
            "type: 'lookup'",
            "isEditableTarget",
            "function beginSelectionActionDrag(event)",
            "function finishSelectionActionDrag(event)",
            "selectionDragAllowedUntil",
            "document.addEventListener('selectionchange', handleReaderSelectionChange, true);",
        ],
        missing_markers,
    )

    require_contains(
        APP,
        [
            "sentence-reader-interaction-v1",
            "english-tap-lookup",
            "double-tap-note",
            "context-click-red",
            "reader-keyboard-note-red-voice-v1",
            "key === 'n'",
            "key === 'r'",
            "key === 'v'",
        ],
        missing_markers,
    )

    require_contains(
        DOC,
        [
            "Sentence Reader Interaction Contract",
            "selected-text action bar",
            "Selection action bar `复制`",
            "Selection action bar `标红`",
            "Selection action bar `备注`",
            "active selection + two-finger tap is not",
            "Command+C",
            "not a Click-owned reading command",
            "single-click lookup",
            "double-click note",
            "two-finger whole-sentence red",
        ],
        missing_markers,
    )

    require_contains(
        ACCEPTANCE,
        [
            "Mac selected-text action bar `复制 / 标红 / 备注`",
            "single-click English lookup",
            "double-click sentence note",
            "two-finger whole-sentence red highlight",
            "must not make `Command+C` a reading-surface command",
        ],
        missing_markers,
    )

    swift_text = SWIFT.read_text(encoding="utf-8")
    forbidden_swift_markers = [
        "__sentenceReaderToggleRedAtPoint",
        "key == \"c\"",
        "key === 'c'",
        "document.addEventListener('selectionchange', scheduleSelectionActionBarUpdate, true);",
        "document.addEventListener('mousemove', observeReaderSelection, true);",
        "window.setInterval(observeReaderSelection",
        "lastObservedSelectionText",
    ]
    present_forbidden = [marker for marker in forbidden_swift_markers if marker in swift_text]
    if ".rightMouseDown" in swift_without_pdf_view(swift_text):
        present_forbidden.append(".rightMouseDown outside ClickPDFView")
    if present_forbidden:
        missing_markers.setdefault(str(SWIFT), []).extend(
            [f"forbidden reading command or native secondary route: {marker}" for marker in present_forbidden]
        )
    if not swift_selection_context_does_not_route_to_secondary_red(swift_text):
        missing_markers.setdefault(str(SWIFT), []).append(
            "active reader selection must return before sentence secondary-red routing"
        )

    status_text = STATUS.read_text(encoding="utf-8")
    obsolete_phrases = [
        "not implemented in the current app",
        "plan-only future additive feature",
        "active正文 selection + two-finger tap",
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
