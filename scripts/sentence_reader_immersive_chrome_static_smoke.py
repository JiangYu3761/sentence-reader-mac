#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SWIFT = ROOT / "Probe" / "NativeSentenceReader" / "SentenceReaderNative.swift"

MARKERS = [
    "readerHeaderView",
    "readerFooterView",
    "readerChromeEventMonitor",
    "installReaderChromeMonitor",
    "revealReaderChromeTemporarily",
    "scheduleReaderChromeAutoHide",
    "setReaderChromeVisible(false)",
    "let header = WindowDragView()",
    "WindowDragView(),\n                readingTTSButton,\n                contentsButton,",
    "event.clickCount == 2",
    "window?.performZoom(nil)",
    ".mouseMoved",
    "mouseLocationOutsideOfEventStream",
    "titlebarAppearsTransparent = true",
    ".fullSizeContentView",
    "window.acceptsMouseMovedEvents = true",
    "webView.topAnchor.constraint(equalTo: root.topAnchor)",
    "webView.bottomAnchor\n            .constraint(\n                equalTo: root.bottomAnchor",
    "comicReader.webView.topAnchor.constraint(equalTo: root.topAnchor)",
    "comicReader.webView.bottomAnchor.constraint(equalTo: root.bottomAnchor)",
    "pdfReader.view.topAnchor.constraint(equalTo: root.topAnchor)",
    "pdfReader.view.bottomAnchor.constraint(equalTo: root.bottomAnchor)",
    'symbolName: "books.vertical.fill"',
    'symbolName: "list.bullet"',
    'symbolName: "textformat.size"',
    "notesRailWidthConstraint = notesRail.widthAnchor.constraint(equalToConstant: 0)",
    "notesRail.isHidden = true",
    "notesRail.topAnchor.constraint(equalTo: header.bottomAnchor",
    "notesRail.bottomAnchor.constraint(equalTo: footer.topAnchor",
]


def main() -> int:
    if not SWIFT.exists():
        print(f"immersive chrome static FAIL missing={SWIFT}")
        return 1
    text = SWIFT.read_text(encoding="utf-8")
    missing = [marker for marker in MARKERS if marker not in text]
    if missing:
        print(f"immersive chrome static FAIL missing_markers={missing}")
        return 1
    forbidden = [
        "webView.topAnchor.constraint(equalTo: header.bottomAnchor)",
        "readerWebViewBottomConstraint?.constant = visible ? -80 : -4",
    ]
    present = [marker for marker in forbidden if marker in text]
    if present:
        print(f"immersive chrome static FAIL layout_reflow_markers={present}")
        return 1
    print("immersive chrome static PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
