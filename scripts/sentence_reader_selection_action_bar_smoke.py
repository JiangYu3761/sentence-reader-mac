#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SWIFT = ROOT / "Probe" / "NativeSentenceReader" / "SentenceReaderNative.swift"
PLAN = ROOT / "docs" / "mac_native_reader_selection_action_bar_plan.md"
DOC = ROOT / "docs" / "interaction_contract.md"


def window_after(text: str, marker: str, length: int = 900) -> str:
    start = text.find(marker)
    if start < 0:
        return ""
    return text[start : start + length]


def main() -> int:
    missing: dict[str, list[str]] = {}
    for path in [SWIFT, PLAN, DOC]:
        if not path.exists():
            missing[str(path)] = ["missing file"]

    if missing:
        print(f"selection action bar smoke FAIL {missing}")
        return 1

    swift = SWIFT.read_text(encoding="utf-8")
    plan = PLAN.read_text(encoding="utf-8")
    doc = DOC.read_text(encoding="utf-8")

    swift_markers = [
        "#sr-selection-action-bar",
        "-webkit-user-select: text",
        "function readerSelectionPayload()",
        "function rangeIntersectsSentence(selectionRange, sentence)",
        "selectionRange.intersectsNode(sentence)",
        "function sentenceFromPoint(x, y)",
        "function sentenceFromEvent(event)",
        "document.elementFromPoint",
        "getClientRects",
        "function clearTextSelectionAfterSecondaryRed()",
        "function selectionActionBarNode()",
        "function selectionPayloadHasExactRed(payload)",
        "function updateSelectionActionBarState(payload)",
        "function removeSelectionRedFragments(fragments)",
        "function handleSelectionAction(action)",
        "取消标红",
        "srSelectionRedMode",
        "suppressSelectionActionBarUntil",
        "selectionDragAllowedUntil",
        "selectionDragStart",
        "function selectionActionBarSuppressed()",
        "function suppressSelectionActionBar(durationMs)",
        "function selectionActionBarCanOpen()",
        "function beginSelectionActionDrag(event)",
        "function updateSelectionActionDrag(event)",
        "function finishSelectionActionDrag(event)",
        "function hideSelectionActionBarWhenSelectionGone()",
        "selectionActionBarVisible()",
        "data-sr-selection-action=\"copy\"",
        "data-sr-selection-action=\"red\"",
        "data-sr-selection-action=\"note\"",
        "event.preventDefault();",
        "type: 'selectionCopy'",
        "type: 'selectionRed'",
        "type: 'selectionRedUndo'",
        "type: 'selectionNote'",
        "NSPasteboard.general.setString(rawText, forType: .string)",
        "persistSelectionRed(",
        "deleteSelectionRed(",
        "\"mode\": \"text_selection\"",
        "selectionMode: \"text_selection_note\"",
        "undoStack.push({",
        "type: 'selectionRed'",
        "type: 'selectionRedRemove'",
        "previousFragments",
        "selectionFragmentKeys(",
        "selectionRedFragments",
        "renderSelectionRedFragments()",
        "document.addEventListener('mousedown', beginSelectionActionDrag, true);",
        "document.addEventListener('mousemove', updateSelectionActionDrag, true);",
        "document.addEventListener('mouseup', finishSelectionActionDrag, true);",
        "document.addEventListener('dragend', finishSelectionActionDrag, true);",
        "document.addEventListener('selectionchange', hideSelectionActionBarWhenSelectionGone, true);",
        "document.addEventListener('dblclick', function (event)",
        "suppressSelectionActionBar(520)",
        "post({ type: 'note'",
        "function toggleRedFromSecondaryEvent(event)",
        "post({ type: 'lookup'",
    ]
    absent = [marker for marker in swift_markers if marker not in swift]
    if absent:
        missing[str(SWIFT)] = absent

    context_window = window_after(swift, "function shouldLetSystemHandleContext")
    selection_gate = context_window.find("hasReaderTextSelection")
    sentence_gate = context_window.find("sentenceFromTarget")
    if selection_gate < 0 or sentence_gate < 0 or selection_gate > sentence_gate:
        missing.setdefault(str(SWIFT), []).append(
            "selection must gate secondary click before sentence red routing"
        )

    keydown_window = window_after(swift, "document.addEventListener('keydown'")
    forbidden = [
        marker
        for marker in [
            "key == \"c\"",
            "key === 'c'",
            "__sentenceReaderToggleRedAtPoint",
            ".rightMouseDown",
            "document.addEventListener('selectionchange', scheduleSelectionActionBarUpdate, true);",
            "document.addEventListener('keyup', scheduleSelectionActionBarUpdate, true);",
            "document.addEventListener('mousemove', observeReaderSelection, true);",
            "document.addEventListener('pointerup', observeReaderSelection, true);",
            "document.addEventListener('touchend', delayedSelectionActionBarUpdate, true);",
            "window.setInterval(observeReaderSelection",
            "function observeReaderSelection()",
            "function delayedSelectionActionBarUpdate()",
            "lastObservedSelectionText",
        ]
        if marker in swift
    ]
    if forbidden:
        missing.setdefault(str(SWIFT), []).extend([f"forbidden marker: {marker}" for marker in forbidden])
    if "Command+C" in keydown_window:
        missing.setdefault(str(SWIFT), []).append("keydown handler must not document or own Command+C")

    action_bar_html = window_after(swift, "selectionActionBar.innerHTML", 500)
    for label in ["复制", "标红", "备注"]:
        if label not in action_bar_html:
            missing.setdefault(str(SWIFT), []).append(f"missing action label: {label}")

    for path, text in [(PLAN, plan), (DOC, doc)]:
        required = [
            "复制",
            "标红",
            "备注",
            "Command+C",
            "two-finger",
            "schema",
        ]
        absent_doc = [marker for marker in required if marker not in text]
        if absent_doc:
            missing.setdefault(str(path), []).extend(absent_doc)

    if missing:
        print(f"selection action bar smoke FAIL {missing}")
        return 1

    print("selection action bar smoke PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
