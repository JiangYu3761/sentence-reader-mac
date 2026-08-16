#!/usr/bin/env python3
from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

SKIP_DIRS = {
    ".git",
    ".venv",
    ".venv-reader-api",
    ".build",
    "build",
    "DerivedData",
    "output",
    "reports",
    "__pycache__",
}
SKIP_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".icns",
    ".ico",
    ".pyc",
    ".epub",
    ".mobi",
    ".pdf",
    ".sqlite",
    ".db",
    ".dump",
}

_MAC_HOME_PREFIX = "/" + "".join(chr(code) for code in (85, 115, 101, 114, 115))
_PRIVATE_DB_PREFIX = "".join(chr(code) for code in (106, 105, 97, 110, 103, 121, 117))
_DOC_IMAGE_PREFIX = "docs" + "/" + "images" + "/"
_PUBLIC_DOC_IMAGE_PREFIX = _DOC_IMAGE_PREFIX + "public/"
PUBLIC_DOC_IMAGES = {
    "docs/images/public/click-library-sanitized.jpg",
    "docs/images/public/click-note-double-click-sanitized.jpg",
    "docs/images/public/click-red-mark-two-finger-sanitized.jpg",
}

DISALLOWED_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("absolute macOS user path", re.compile(re.escape(_MAC_HOME_PREFIX) + r"/[A-Za-z0-9._-]+")),
    ("private LAN IP", re.compile(r"\b(?:192\.168|10\.|172\.(?:1[6-9]|2\d|3[01])|100\.64)\.\d{1,3}\.\d{1,3}\b")),
    ("personal database name", re.compile(_PRIVATE_DB_PREFIX + r"_os", re.IGNORECASE)),
    ("personal GitHub handle", re.compile("Jiang" + "Yu3761", re.IGNORECASE)),
    ("personal archive path", re.compile("资料" + "归档|" + "夸克" + "网盘")),
    ("personal FunASR project folder", re.compile("New" + r"\s+project")),
    ("desktop source fixture", re.compile("Desktop" + r"/")),
    ("raw SSH key fingerprint", re.compile("SHA" + r"256:[A-Za-z0-9+/=]{20,}")),
    (
        "non-public documentation image reference",
        re.compile(re.escape(_DOC_IMAGE_PREFIX) + r"(?!public/)"),
    ),
]


def iter_tracked_files() -> list[Path]:
    try:
        proc = subprocess.run(
            ["git", "ls-files"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    return [ROOT / line for line in proc.stdout.splitlines() if line]


def iter_text_files() -> list[Path]:
    files: list[Path] = []
    tracked = iter_tracked_files()
    candidates = tracked if tracked else list(ROOT.rglob("*"))
    for path in candidates:
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        files.append(path)
    return files


def main() -> int:
    failures: list[str] = []
    for path in iter_tracked_files():
        rel = path.relative_to(ROOT)
        rel_text = rel.as_posix()
        if len(rel.parts) >= 2 and rel.parts[0] == "docs" and rel.parts[1] == "images":
            if rel_text not in PUBLIC_DOC_IMAGES:
                failures.append(f"{rel}: documentation image is not on the audited public allowlist")
    for path in iter_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in DISALLOWED_PATTERNS:
            if label == "non-public documentation image reference" and path.name in {".gitignore", "public_repo_privacy_smoke.py"}:
                continue
            for match in pattern.finditer(text):
                line_no = text.count("\n", 0, match.start()) + 1
                rel = path.relative_to(ROOT)
                failures.append(f"{rel}:{line_no}: {label}: {match.group(0)}")
    if failures:
        print("public repo privacy smoke FAIL")
        for item in failures[:200]:
            print(item)
        if len(failures) > 200:
            print(f"... {len(failures) - 200} more")
        return 1
    print("public repo privacy smoke PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
