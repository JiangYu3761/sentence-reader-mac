#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "Probe/NativeSentenceReader/SentenceReaderNative.swift"
APP = ROOT / "reader_api/app.py"
MOBILE = ROOT / "reader_api/mobile_workspace.py"


def require_markers(path: Path, markers: list[str]) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [marker for marker in markers if marker not in text]


def main() -> int:
    missing: dict[str, list[str]] = {}
    checks = {
        NATIVE: [
            "private enum NoteTextNormalizer",
            "spokenPunctuation",
            "NoteTextNormalizer.normalized(text)",
            "NoteTextNormalizer.normalized(textView.string)",
            '"句号", "。"',
            '"问号", "？"',
        ],
        APP: [
            "def normalize_note_text(raw_text: str) -> str:",
            "NOTE_SPOKEN_PUNCTUATION",
            "transcript = normalize_note_text",
            "function normalizeNoteText(value)",
            "note = normalizeNoteText(note);",
        ],
        MOBILE: [
            "def normalize_note_text(raw_text: str) -> str:",
            "text = normalize_note_text(str(raw.get(\"text\") or \"\"))",
            "transcript = normalize_note_text(str(pipeline_result[\"transcript\"]))",
        ],
    }
    for path, markers in checks.items():
        misses = require_markers(path, markers)
        if misses:
            missing[str(path)] = misses

    if missing:
        print(f"note punctuation static smoke FAIL missing={missing}")
        return 1

    print("note punctuation static smoke PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
