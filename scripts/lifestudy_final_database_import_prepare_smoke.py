#!/usr/bin/env python3
from __future__ import annotations

import csv
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PREPARE = ROOT / "scripts" / "lifestudy_final_database_import_prepare.py"
IMPORTER = ROOT / "scripts" / "lifestudy_context_vocab_import.py"
OUTPUT_DIR = ROOT / "reports" / "lifestudy_vocab_final_review"

IMPORTABLE_JSON = OUTPUT_DIR / "final_database_import_prepare_all_4102.json"
IMPORTABLE_CSV = OUTPUT_DIR / "final_database_import_prepare_all_4102.csv"
DEFAULT_POPUP_CSV = OUTPUT_DIR / "final_database_import_prepare_default_popup.csv"
HIGH_CONFIDENCE_POPUP_CSV = OUTPUT_DIR / "final_database_import_prepare_high_confidence_popup.csv"
LEARNING_SEARCH_CSV = OUTPUT_DIR / "final_database_import_prepare_learning_search.csv"
REPAIRED_CSV = OUTPUT_DIR / "final_database_import_prepare_repaired_from_reject.csv"
SUMMARY_JSON = OUTPUT_DIR / "final_database_import_prepare_summary.json"

EXPECTED_TOTAL = 4102
EXPECTED_DEFAULT_POPUP = 4102
EXPECTED_HIGH_CONFIDENCE_POPUP = 49
EXPECTED_REPAIRED = {
    "ephesians": "以弗所书",
    "philippians": "腓立比书",
    "divinity": "神性",
    "pharisees": "法利赛人",
    "anointed": "膏",
}


def fail(message: str) -> None:
    raise SystemExit(f"lifestudy final database import prepare smoke FAIL: {message}")


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        fail(f"missing CSV: {path}")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [{key.lstrip("\ufeff"): value for key, value in row.items()} for row in csv.DictReader(handle)]


def run_prepare() -> None:
    proc = subprocess.run([sys.executable, str(PREPARE)], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        fail(proc.stderr.strip() or proc.stdout.strip())


def load_importer_validate_items() -> Any:
    spec = importlib.util.spec_from_file_location("lifestudy_context_vocab_import", IMPORTER)
    if spec is None or spec.loader is None:
        fail("cannot load import script module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validate_items


def main() -> int:
    run_prepare()

    if not IMPORTABLE_JSON.exists():
        fail(f"missing importable JSON: {IMPORTABLE_JSON}")
    payload = json.loads(IMPORTABLE_JSON.read_text(encoding="utf-8"))
    if payload.get("schema") != "sentence_reader.lifestudy_vocab_importable.v1":
        fail("unexpected importable schema")
    if payload.get("database_write_performed") is not False:
        fail("prepare payload reports database write")
    if payload.get("front_end_import_ready") is not False:
        fail("prepare payload marks front-end import-ready")

    quality = payload.get("quality") or {}
    if quality.get("total_import_candidates") != EXPECTED_TOTAL:
        fail(f"wrong total import candidates: {quality.get('total_import_candidates')}")
    if quality.get("default_popup_count") != EXPECTED_DEFAULT_POPUP:
        fail(f"wrong default popup count: {quality.get('default_popup_count')}")
    if quality.get("single_click_popup_count") != EXPECTED_DEFAULT_POPUP:
        fail(f"wrong single-click popup count: {quality.get('single_click_popup_count')}")
    if quality.get("high_confidence_popup_count") != EXPECTED_HIGH_CONFIDENCE_POPUP:
        fail(f"wrong high-confidence popup count: {quality.get('high_confidence_popup_count')}")
    if quality.get("learning_search_count") != EXPECTED_TOTAL:
        fail("every row must remain searchable learning vocabulary")
    if quality.get("repaired_from_reject_count") != len(EXPECTED_REPAIRED):
        fail("repaired reject count mismatch")
    if quality.get("database_write_count") != 0:
        fail("database_write_count must be zero")
    if quality.get("front_end_import_ready_count") != 0:
        fail("front_end_import_ready_count must be zero")

    items = payload.get("items") or []
    if len(items) != EXPECTED_TOTAL:
        fail(f"item count mismatch: {len(items)}")
    terms = [str(item.get("term") or "") for item in items]
    if len(set(terms)) != EXPECTED_TOTAL:
        fail("duplicate terms in import package")
    if any(not term for term in terms):
        fail("empty term found")

    for item in items:
        term = item.get("term")
        if item.get("import_allowed") is not True:
            fail(f"import_allowed is not true: {term}")
        if item.get("domain_glossary_import_candidate") is not True:
            fail(f"domain glossary candidate is not true: {term}")
        if item.get("reader_dictionary_import_candidate") is not False:
            fail(f"reader dictionary candidate should be false: {term}")
        if item.get("database_write_performed") is not False:
            fail(f"database write flag set: {term}")
        if item.get("front_end_import_ready") is not False:
            fail(f"front-end import-ready flag set: {term}")
        if item.get("can_default_popup") is not True:
            fail(f"single-click popup should be enabled: {term}")
        if item.get("single_click_popup_enabled") is not True:
            fail(f"single_click_popup_enabled should be true: {term}")
        if item.get("quality_grade") not in {"A", "B"}:
            fail(f"invalid quality grade: {term}")
        if not item.get("suggested_meaning_zh_simp"):
            fail(f"empty meaning: {term}")
        part_of_speech = str(item.get("part_of_speech") or "")
        part_of_speech_zh = str(item.get("part_of_speech_zh") or "")
        speak_text = str(item.get("popup_speak_text_zh") or "")
        if not part_of_speech or not part_of_speech_zh:
            fail(f"missing part of speech fields: {term}")
        if not speak_text:
            fail(f"missing popup speech text: {term}")
        if item.get("tts_engine_preferred") != "edge-tts":
            fail(f"unexpected TTS engine: {term}")
        if item.get("tts_voice_preferred") != "zh-CN-YunjianNeural":
            fail(f"unexpected TTS voice: {term}")
        if part_of_speech_zh not in speak_text:
            fail(f"speech text does not include Chinese POS: {term}")
        if str(item.get("suggested_meaning_zh_simp") or "") not in speak_text:
            fail(f"speech text does not include Chinese meaning: {term}")
        if re.search(r"\b(n|v|adj|adv)\.", speak_text, flags=re.IGNORECASE):
            fail(f"speech text contains English POS abbreviation: {term}")
        if not item.get("evidence_en") or not item.get("evidence_zh_simp") or not item.get("source_page"):
            fail(f"missing evidence fields: {term}")
        if item.get("meaning_supported_by_current_evidence") is not True:
            fail(f"meaning not supported by current evidence: {term}")

    rows = read_csv(IMPORTABLE_CSV)
    default_rows = read_csv(DEFAULT_POPUP_CSV)
    high_confidence_rows = read_csv(HIGH_CONFIDENCE_POPUP_CSV)
    learning_rows = read_csv(LEARNING_SEARCH_CSV)
    repaired_rows = read_csv(REPAIRED_CSV)
    if len(rows) != EXPECTED_TOTAL:
        fail("importable CSV count mismatch")
    if len(default_rows) != EXPECTED_DEFAULT_POPUP:
        fail("default popup CSV count mismatch")
    if len(high_confidence_rows) != EXPECTED_HIGH_CONFIDENCE_POPUP:
        fail("high-confidence popup CSV count mismatch")
    if len(learning_rows) != EXPECTED_TOTAL:
        fail("learning search CSV count mismatch")
    repaired_map = {row["term"]: row["suggested_meaning_zh_simp"] for row in repaired_rows}
    if repaired_map != EXPECTED_REPAIRED:
        fail(f"repaired rows mismatch: {repaired_map}")

    validate_items = load_importer_validate_items()
    accepted = validate_items(payload)
    if len(accepted) != EXPECTED_TOTAL:
        fail(f"existing importer validates {len(accepted)} rows, expected {EXPECTED_TOTAL}")

    summary = json.loads(SUMMARY_JSON.read_text(encoding="utf-8"))
    if summary.get("quality", {}).get("total_import_candidates") != EXPECTED_TOTAL:
        fail("summary total mismatch")

    print(
        json.dumps(
            {
                "ok": True,
                "total_import_candidates": EXPECTED_TOTAL,
                "default_popup_count": EXPECTED_DEFAULT_POPUP,
                "single_click_popup_count": EXPECTED_DEFAULT_POPUP,
                "high_confidence_popup_count": EXPECTED_HIGH_CONFIDENCE_POPUP,
                "learning_search_count": EXPECTED_TOTAL,
                "repaired_from_reject": repaired_map,
                "existing_importer_validation_count": len(accepted),
                "database_write_performed": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
