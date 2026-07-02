#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BATCH_SCRIPT = ROOT / "scripts" / "lifestudy_final_review_batch.py"
OUTPUT_DIR = ROOT / "reports" / "lifestudy_vocab_final_review"
CALIBRATION_REVIEWED = OUTPUT_DIR / "calibration_001_reviewed.csv"
SUMMARY = OUTPUT_DIR / "batch_001_reviewed_summary.json"
REVIEWED = OUTPUT_DIR / "batch_001_reviewed.csv"
INPUT = OUTPUT_DIR / "batch_001_input.csv"
TEMPLATE = OUTPUT_DIR / "batch_001_review_template.csv"
FRONTEND_READY = OUTPUT_DIR / "batch_001_frontend_ready.csv"
LEARNING_ONLY = OUTPUT_DIR / "batch_001_learning_only.csv"
NEEDS_MORE = OUTPUT_DIR / "batch_001_needs_more_evidence.csv"
REJECT = OUTPUT_DIR / "batch_001_reject.csv"
MARKDOWN = OUTPUT_DIR / "batch_001_reviewed.md"
PLAN = ROOT / "docs" / "lifestudy_vocab_final_review_plan.md"

EXPECTED_BATCH_ROWS = 250
EXPECTED_COUNTS = {
    "frontend_ready": 4,
    "learning_only": 157,
    "needs_more_evidence": 86,
    "reject": 3,
}
EXPECTED_FRONTEND = {
    "principle": "原则",
    "knowledge": "知识",
    "recovery": "恢复",
    "praise": "赞美",
}
EXPECTED_REJECTS = {
    "ephesians",
    "philippians",
    "divinity",
}
EXPECTED_NEEDS_MORE_SUBSET = {
    "blood",
    "covenant",
    "seed",
    "soul",
    "sacrifice",
    "lampstand",
    "processed",
    "administration",
    "burden",
}


def fail(message: str) -> None:
    raise SystemExit(message)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        fail(f"missing CSV: {path}")
    return list(csv.DictReader(path.open(encoding="utf-8-sig")))


def db_counts() -> dict[str, int] | None:
    python = ROOT / ".venv-reader-api" / "bin" / "python"
    if not python.exists():
        return None
    code = """
from reader_api.db import connect
def value(row):
    try:
        return row[0]
    except Exception:
        return row['count']
with connect() as conn:
    with conn.cursor() as cur:
        cur.execute('select count(*) as count from reader.dictionary_entries')
        dictionary_count = value(cur.fetchone())
        cur.execute('select count(*) as count from reader.domain_glossary_entries')
        domain_count = value(cur.fetchone())
print(f'{dictionary_count},{domain_count}')
"""
    proc = subprocess.run([str(python), "-c", code], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        return None
    dictionary_count, domain_count = [int(part) for part in proc.stdout.strip().split(",")]
    return {
        "reader.dictionary_entries": dictionary_count,
        "reader.domain_glossary_entries": domain_count,
    }


def meaning_parts(meaning: str) -> list[str]:
    return [part.strip() for part in re.split(r"[；;/,，、]", meaning or "") if part.strip()]


def meaning_supported(meaning: str, evidence_zh: str) -> bool:
    parts = meaning_parts(meaning)
    return bool(parts) and all(part in (evidence_zh or "") for part in parts)


def word_supported(word: str, evidence_en: str) -> bool:
    return bool(re.search(rf"(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])", evidence_en or "", re.IGNORECASE))


def run_batch() -> tuple[dict[str, int] | None, dict[str, int] | None]:
    before = db_counts()
    proc = subprocess.run(
        [sys.executable, str(BATCH_SCRIPT), "--batch", "1"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        fail(proc.stderr.strip() or proc.stdout.strip())
    after = db_counts()
    return before, after


def main() -> int:
    before_counts, after_counts = run_batch()
    for path in (SUMMARY, REVIEWED, INPUT, TEMPLATE, FRONTEND_READY, LEARNING_ONLY, NEEDS_MORE, REJECT, MARKDOWN):
        if not path.exists():
            fail(f"missing batch output: {path}")

    payload = json.loads(SUMMARY.read_text(encoding="utf-8"))
    quality = payload.get("quality") or {}
    if payload.get("schema") != "sentence_reader.lifestudy_final_review_batch.v1":
        fail("unexpected batch schema")
    if payload.get("batch_id") != "batch_001":
        fail("unexpected batch id")
    if payload.get("database_write_performed") is not False:
        fail("batch summary must be no-write")
    if payload.get("front_end_import_ready") is not False:
        fail("batch summary must not be import-ready")
    if quality.get("reviewed_rows") != EXPECTED_BATCH_ROWS:
        fail(f"batch row count mismatch: {quality.get('reviewed_rows')} != {EXPECTED_BATCH_ROWS}")
    if quality.get("blank_final_category_rows") != 0:
        fail("all rows must have final_category")
    if quality.get("blank_final_reason_rows") != 0:
        fail("all rows must have final_reason")
    if quality.get("front_end_import_ready_count") != 0:
        fail("no row may be front_end_import_ready")
    if quality.get("database_write_count") != 0:
        fail("no row may mark database write")
    decision_counts = quality.get("decision_counts") or {}
    for category, expected in EXPECTED_COUNTS.items():
        if decision_counts.get(category) != expected:
            fail(f"{category} count mismatch: {decision_counts.get(category)} != {expected}")

    reviewed = read_csv(REVIEWED)
    if len(reviewed) != EXPECTED_BATCH_ROWS:
        fail("reviewed CSV row count mismatch")
    words = [row["word"] for row in reviewed]
    if len(set(words)) != len(words):
        fail("batch contains duplicate words")
    calibration_words = {row["word"] for row in read_csv(CALIBRATION_REVIEWED)}
    overlap = sorted(set(words) & calibration_words)
    if overlap:
        fail(f"batch overlaps calibration rows: {overlap[:10]}")
    for row in reviewed:
        if row.get("database_write_performed") != "false":
            fail(f"row marked database write: {row.get('word')}")
        if row.get("front_end_import_ready") != "false":
            fail(f"row marked import-ready: {row.get('word')}")
        for field in ("word", "reviewed_meaning_zh_simp", "evidence_en", "evidence_zh_simp", "source_volume", "source_page", "final_category", "final_reason"):
            if not row.get(field):
                fail(f"missing required field {field} for {row.get('word')}")

    frontend_rows = read_csv(FRONTEND_READY)
    if {row["word"]: row["final_meaning_zh_simp"] for row in frontend_rows} != EXPECTED_FRONTEND:
        fail("front-end ready rows do not match expected anchored terms")
    for row in frontend_rows:
        if row.get("pending_formal_anchor_hit") != "true":
            fail(f"front-end row is not historical anchor: {row.get('word')}")
        if row.get("needs_second_pass") != "true":
            fail(f"front-end row must require second pass: {row.get('word')}")
        if not word_supported(row["word"], row.get("evidence_en", "")):
            fail(f"front-end row missing English evidence: {row['word']}")
        if not meaning_supported(row["final_meaning_zh_simp"], row.get("evidence_zh_simp", "")):
            fail(f"front-end row missing Chinese evidence: {row['word']}")

    needs_more_words = {row["word"] for row in read_csv(NEEDS_MORE)}
    missing_needs_more = sorted(EXPECTED_NEEDS_MORE_SUBSET - needs_more_words)
    if missing_needs_more:
        fail(f"expected needs_more_evidence words missing: {missing_needs_more}")
    reject_words = {row["word"] for row in read_csv(REJECT)}
    if reject_words != EXPECTED_REJECTS:
        fail(f"reject set mismatch: {sorted(reject_words)} != {sorted(EXPECTED_REJECTS)}")

    if before_counts is not None and after_counts is not None and before_counts != after_counts:
        fail(f"database counts changed: before={before_counts}, after={after_counts}")

    plan_text = PLAN.read_text(encoding="utf-8")
    for phrase in ("batch_001_reviewed.csv", "batch_001_frontend_ready.csv", "batch_001_needs_more_evidence.csv"):
        if phrase not in plan_text:
            fail(f"plan missing batch output reference: {phrase}")

    print(
        json.dumps(
            {
                "ok": True,
                "batch_id": "batch_001",
                "reviewed_rows": quality.get("reviewed_rows"),
                "decision_counts": decision_counts,
                "frontend_ready": EXPECTED_FRONTEND,
                "needs_more_evidence_checked": sorted(EXPECTED_NEEDS_MORE_SUBSET),
                "rejects": sorted(EXPECTED_REJECTS),
                "database_counts_checked": before_counts is not None and after_counts is not None,
                "database_counts": after_counts,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
