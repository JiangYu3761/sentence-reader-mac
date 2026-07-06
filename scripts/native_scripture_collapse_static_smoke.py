#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SWIFT = ROOT / "Probe" / "NativeSentenceReader" / "SentenceReaderNative.swift"
CURRENT_STATUS = ROOT / "docs" / "current_status.md"
PRODUCT_ACCEPTANCE = ROOT / "docs" / "product_acceptance.md"


def main() -> int:
    missing: dict[str, list[str]] = {}
    for path in [SWIFT, CURRENT_STATUS, PRODUCT_ACCEPTANCE]:
        if not path.exists():
            missing[str(path)] = ["missing file"]

    if missing:
        print(f"native scripture collapse static smoke FAIL {missing}")
        return 1

    swift = SWIFT.read_text(encoding="utf-8")
    current = CURRENT_STATUS.read_text(encoding="utf-8")
    acceptance = PRODUCT_ACCEPTANCE.read_text(encoding="utf-8")

    swift_markers = [
        "sr-scripture-footnote",
        "sr-reader-details",
        "function normalizeCollapsibleScriptureBlocks()",
        "function collapseExistingDetails(surface)",
        "function convertScriptureFootnotes(surface)",
        "function installScriptureFootnoteToggles(surface)",
        "isScriptureFootnoteNode(node)",
        "epub:type",
        "duokan-footnote",
        "calibre_verse",
        "details.removeAttribute('open')",
        "target.matches('details.sr-scripture-footnote')",
        "target.open = !target.open",
        "invalidatePagination();",
        "applyPage(false, 0);",
        "normalizeCollapsibleScriptureBlocks();",
    ]
    absent = [marker for marker in swift_markers if marker not in swift]
    if absent:
        missing[str(SWIFT)] = absent

    required_docs = [
        "scripture",
        "footnote",
        "collapsed",
    ]
    doc_text = f"{current}\n{acceptance}".lower()
    absent_docs = [marker for marker in required_docs if marker not in doc_text]
    if absent_docs:
        missing[f"{CURRENT_STATUS},{PRODUCT_ACCEPTANCE}"] = absent_docs

    if missing:
        print(f"native scripture collapse static smoke FAIL {missing}")
        return 1

    print("native scripture collapse static smoke PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
