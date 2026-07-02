#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "prewarm_lookup_tts_cache.py"


def fail(message: str) -> None:
    raise SystemExit(f"prewarm lookup TTS cache static smoke FAIL: {message}")


def require(text: str, needle: str) -> None:
    if needle not in text:
        fail(f"missing `{needle}`")


def main() -> int:
    text = SCRIPT.read_text(encoding="utf-8")
    for marker in [
        "final_database_import_prepare_all_4102.json",
        "DEFAULT_BASE_URL = \"http://127.0.0.1:18180\"",
        "DEFAULT_VOICE = \"en-US-BrianNeural\"",
        "/lookup/tts/status",
        "/lookup/tts",
        "ThreadPoolExecutor",
        "--limit",
        "--workers",
        "cached_before",
        "prewarm progress",
    ]:
        require(text, marker)
    print("prewarm lookup TTS cache static smoke PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
