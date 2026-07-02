#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "reader_api" / "app.py"
MOBILE_WORKSPACE = ROOT / "reader_api" / "mobile_workspace.py"
IMPORTER = ROOT / "scripts" / "lifestudy_context_vocab_import.py"
PREPARE = ROOT / "scripts" / "lifestudy_final_database_import_prepare.py"
IMPORTABLE_JSON = ROOT / "reports" / "lifestudy_vocab_final_review" / "final_database_import_prepare_all_4102.json"

EXPECTED_TOTAL = 4102


def fail(message: str) -> None:
    raise SystemExit(f"lifestudy lookup popup TTS static smoke FAIL: {message}")


def run_prepare() -> None:
    proc = subprocess.run([sys.executable, str(PREPARE)], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        fail(proc.stderr.strip() or proc.stdout.strip())


def require(text: str, needle: str, source: Path) -> None:
    if needle not in text:
        fail(f"missing `{needle}` in {source.relative_to(ROOT)}")


def main() -> int:
    run_prepare()
    app_text = APP.read_text(encoding="utf-8")
    mobile_text = MOBILE_WORKSPACE.read_text(encoding="utf-8")
    importer_text = IMPORTER.read_text(encoding="utf-8")

    for needle in [
        "FileResponse",
        "EDGE_TTS_VOICE",
        "edge_tts_path",
        "class LookupTTSCreate",
        "@app.post(\"/lookup/tts\")",
        "@app.post(\"/lookup/tts/status\")",
        "@app.get(\"/lookup/tts/{audio_id}.mp3\")",
        "\"cached\": cached",
        "lookupSpeakMeaning",
        "读释义",
        "part_of_speech_zh",
        "popup_speak_text_zh",
    ]:
        require(app_text, needle, APP)

    require(mobile_text, "zh-CN-YunjianNeural", MOBILE_WORKSPACE)

    for needle in [
        "part_of_speech",
        "part_of_speech_zh",
        "popup_speak_text_zh",
        "tts_engine_preferred",
        "tts_voice_preferred",
        "single_click_popup_enabled",
        "high_confidence_popup",
    ]:
        require(importer_text, needle, IMPORTER)

    payload = json.loads(IMPORTABLE_JSON.read_text(encoding="utf-8"))
    items = payload.get("items") or []
    if len(items) != EXPECTED_TOTAL:
        fail(f"wrong import package size: {len(items)}")

    bad_rows: list[str] = []
    for item in items:
        term = str(item.get("term") or "")
        meaning = str(item.get("suggested_meaning_zh_simp") or "")
        pos = str(item.get("part_of_speech") or "")
        pos_zh = str(item.get("part_of_speech_zh") or "")
        speak_text = str(item.get("popup_speak_text_zh") or "")
        if not term or not meaning or not pos or not pos_zh or not speak_text:
            bad_rows.append(term or "<empty>")
            continue
        if item.get("single_click_popup_enabled") is not True or item.get("can_default_popup") is not True:
            bad_rows.append(term)
            continue
        if item.get("tts_engine_preferred") != "edge-tts" or item.get("tts_voice_preferred") != "zh-CN-YunjianNeural":
            bad_rows.append(term)
            continue
        if pos_zh not in speak_text or meaning not in speak_text or term not in speak_text:
            bad_rows.append(term)
            continue
        if re.search(r"\b(n|v|adj|adv)\.", speak_text, flags=re.IGNORECASE):
            bad_rows.append(term)
    if bad_rows:
        fail(f"bad POS/TTS rows: {bad_rows[:10]}")

    print(
        json.dumps(
            {
                "ok": True,
                "lookup_popup_tts_static": True,
                "total_items_with_pos_and_chinese_tts_text": len(items),
                "tts_engine_preferred": "edge-tts",
                "tts_voice_preferred": "zh-CN-YunjianNeural",
                "database_write_performed": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
