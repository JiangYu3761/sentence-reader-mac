#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "Probe" / "NativeSentenceReader" / "SentenceReaderNative.swift"
PLAN = ROOT / "docs" / "annotation_core_v2_plan.md"


SWIFT_MARKERS = {
    "annotation core flag": "__sentenceReaderAnnotationCoreV2",
    "full annotation restore bridge": "__sentenceReaderApplyAnnotations",
    "legacy red restore wrapper": "__sentenceReaderApplyRedHighlights",
    "note marker class": ".sr-sentence.sr-note",
    "note marker payload": "applyNoteMarkers",
    "note preview event": "notePreview",
    "note preview sheet": "showNotePreview",
    "existing note editor": "showExistingNoteEditor",
    "multi index parser": "sentenceIndexList",
    "payload indexes bridge": "sentenceIndexPayload",
    "selection detection": "selectedSentences",
    "selection awareness": "hasSystemTextSelection",
    "interaction router": "__SentenceReaderInteractionRouter",
    "sentence reader priority": "sentence-reader-first",
    "editable system guard": "isEditableTarget",
    "system routing guard": "shouldLetSystemHandle",
    "context red routing guard": "shouldLetSystemHandleContext",
    "sentence event claim helper": "claimSentenceEvent",
    "secondary click point sentence route": "function sentenceFromPoint(x, y)",
    "secondary click event sentence route": "const sentence = sentenceFromEvent(event);",
    "secondary red clears WebKit residual selection": "function clearTextSelectionAfterSecondaryRed()",
    "single sentence before selection red": "if (sentence) {\n          return toggleRedSentences([sentence], event);\n        }",
    "double click note default": "double-click-note",
    "range intersection": "rangesIntersect",
    "batch red toggle": "toggleRedSentences",
    "batch undo": "redBatch",
    "non sentence boundary marker": "nonSentenceBoundaryCharacters = '：:；;'",
    "versioned reading sentence ranges": "function readingSentenceRanges(text)",
}

PLAN_MARKERS = [
    "Annotation Core V2",
    "three-finger or drag selection",
    "No database schema migration",
    "Single-click the note-marked sentence",
]


def main() -> int:
    missing_files = [str(path) for path in (SOURCE, PLAN) if not path.exists()]
    missing_markers: dict[str, list[str]] = {}

    if SOURCE.exists():
        text = SOURCE.read_text(encoding="utf-8")
        missing = [name for name, marker in SWIFT_MARKERS.items() if marker not in text]
        if missing:
            missing_markers[str(SOURCE)] = missing

        boundary_start = text.find("function readingSentenceRanges(text)")
        boundary_window = text[boundary_start : boundary_start + 1_600]
        if "；;" in boundary_window:
            missing_markers.setdefault(str(SOURCE), []).append("semicolon must not be a reading sentence boundary")
        if "boundary = /[。！？!?" not in boundary_window or "isReadingPeriodBoundary" not in boundary_window:
            missing_markers.setdefault(str(SOURCE), []).append("expected explicit reading sentence punctuation")
        pdf_start = text.find("private final class ClickPDFView: PDFView")
        pdf_end = text.find("private final class MacPDFReaderController", pdf_start)
        non_pdf_text = text
        if pdf_start >= 0 and pdf_end > pdf_start:
            non_pdf_text = text[:pdf_start] + text[pdf_end:]
        forbidden_markers = [
            ".rightMouseDown",
            "__sentenceReaderToggleRedAtPoint",
        ]
        present_forbidden = [marker for marker in forbidden_markers if marker in non_pdf_text]
        if present_forbidden:
            missing_markers.setdefault(str(SOURCE), []).extend(
                [f"forbidden secondary red route: {marker}" for marker in present_forbidden]
            )

    if PLAN.exists():
        plan_text = PLAN.read_text(encoding="utf-8")
        missing = [marker for marker in PLAN_MARKERS if marker not in plan_text]
        if missing:
            missing_markers[str(PLAN)] = missing

    if missing_files or missing_markers:
        print(f"annotation core v2 static FAIL missing_files={missing_files} missing_markers={missing_markers}")
        return 1

    print("annotation core v2 static PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
