#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BATCH_SCRIPT = ROOT / "scripts" / "lifestudy_final_review_batch.py"
OUTPUT_DIR = ROOT / "reports" / "lifestudy_vocab_final_review"
SOURCE_CSV = ROOT / "reports" / "lifestudy_vocab_corpus" / "lifestudy_dictionary_guided_review_v2_possible_frontend_after_human_review.csv"
CALIBRATION_REVIEWED = OUTPUT_DIR / "calibration_001_reviewed.csv"

EXPECTED_SOURCE_ROWS = 4102
EXPECTED_CALIBRATION_ROWS = 100
DEFAULT_BATCH_SIZE = 250
VALID_CATEGORIES = {"frontend_ready", "learning_only", "needs_more_evidence", "reject"}


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


def word_supported(word: str, evidence_en: str) -> bool:
    return bool(re.search(rf"(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])", evidence_en or "", re.IGNORECASE))


def meaning_parts(meaning: str) -> list[str]:
    return [part.strip() for part in re.split(r"[；;/,，、]", meaning or "") if part.strip()]


def meaning_supported(meaning: str, evidence_zh: str) -> bool:
    parts = meaning_parts(meaning)
    return bool(parts) and all(part in (evidence_zh or "") for part in parts)


def production_counts(batch_size: int) -> tuple[int, int, int]:
    source_rows = read_csv(SOURCE_CSV)
    calibration_rows = read_csv(CALIBRATION_REVIEWED)
    if len(source_rows) != EXPECTED_SOURCE_ROWS:
        fail(f"source row count mismatch: {len(source_rows)} != {EXPECTED_SOURCE_ROWS}")
    if len(calibration_rows) != EXPECTED_CALIBRATION_ROWS:
        fail(f"calibration row count mismatch: {len(calibration_rows)} != {EXPECTED_CALIBRATION_ROWS}")
    calibration_words = {(row.get("word") or "").strip().lower() for row in calibration_rows}
    remaining = [row for row in source_rows if (row.get("word") or "").strip().lower() not in calibration_words]
    total_batches = math.ceil(len(remaining) / batch_size)
    return len(remaining), total_batches, len(remaining) % batch_size or batch_size


def expected_batch_rows(batch: int, batch_size: int) -> int:
    remaining_count, total_batches, last_size = production_counts(batch_size)
    if batch < 1 or batch > total_batches:
        fail(f"batch out of range: {batch}; total_batches={total_batches}")
    if batch == total_batches:
        return last_size
    return batch_size


def run_batch(batch: int, batch_size: int, validate_only: bool = False) -> tuple[dict[str, int] | None, dict[str, int] | None]:
    before = db_counts()
    if not validate_only:
        proc = subprocess.run(
            [sys.executable, str(BATCH_SCRIPT), "--batch", str(batch), "--batch-size", str(batch_size)],
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=int, required=True)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()

    expected_rows = expected_batch_rows(args.batch, args.batch_size)
    before_counts, after_counts = run_batch(args.batch, args.batch_size, args.validate_only)
    batch_id = f"batch_{args.batch:03d}"
    paths = {
        "summary": OUTPUT_DIR / f"{batch_id}_reviewed_summary.json",
        "reviewed": OUTPUT_DIR / f"{batch_id}_reviewed.csv",
        "input": OUTPUT_DIR / f"{batch_id}_input.csv",
        "template": OUTPUT_DIR / f"{batch_id}_review_template.csv",
        "frontend": OUTPUT_DIR / f"{batch_id}_frontend_ready.csv",
        "learning": OUTPUT_DIR / f"{batch_id}_learning_only.csv",
        "needs_more": OUTPUT_DIR / f"{batch_id}_needs_more_evidence.csv",
        "reject": OUTPUT_DIR / f"{batch_id}_reject.csv",
        "markdown": OUTPUT_DIR / f"{batch_id}_reviewed.md",
    }
    for path in paths.values():
        if not path.exists():
            fail(f"missing batch output: {path}")

    payload = json.loads(paths["summary"].read_text(encoding="utf-8"))
    quality = payload.get("quality") or {}
    if payload.get("schema") != "sentence_reader.lifestudy_final_review_batch.v1":
        fail("unexpected batch schema")
    if payload.get("batch_id") != batch_id:
        fail(f"unexpected batch id: {payload.get('batch_id')} != {batch_id}")
    if payload.get("database_write_performed") is not False:
        fail("batch summary must be no-write")
    if payload.get("front_end_import_ready") is not False:
        fail("batch summary must not be import-ready")
    if quality.get("reviewed_rows") != expected_rows:
        fail(f"reviewed row count mismatch: {quality.get('reviewed_rows')} != {expected_rows}")
    if quality.get("blank_final_category_rows") != 0:
        fail("all rows must have final_category")
    if quality.get("blank_final_reason_rows") != 0:
        fail("all rows must have final_reason")
    if quality.get("front_end_import_ready_count") != 0:
        fail("no row may be front_end_import_ready")
    if quality.get("database_write_count") != 0:
        fail("no row may mark database write")

    reviewed = read_csv(paths["reviewed"])
    if len(reviewed) != expected_rows:
        fail("reviewed CSV row count mismatch")
    words = [row["word"] for row in reviewed]
    if len(set(words)) != len(words):
        fail(f"{batch_id} contains duplicate words")
    calibration_words = {row["word"] for row in read_csv(CALIBRATION_REVIEWED)}
    overlap = sorted(set(words) & calibration_words)
    if overlap:
        fail(f"{batch_id} overlaps calibration rows: {overlap[:10]}")
    for row in reviewed:
        if row.get("final_category") not in VALID_CATEGORIES:
            fail(f"invalid final_category for {row.get('word')}: {row.get('final_category')}")
        if row.get("database_write_performed") != "false":
            fail(f"row marked database write: {row.get('word')}")
        if row.get("front_end_import_ready") != "false":
            fail(f"row marked import-ready: {row.get('word')}")
        for field in ("word", "reviewed_meaning_zh_simp", "evidence_en", "evidence_zh_simp", "source_volume", "source_page", "final_category", "final_reason"):
            if not row.get(field):
                fail(f"missing required field {field} for {row.get('word')}")

    frontend_rows = read_csv(paths["frontend"])
    for row in frontend_rows:
        if row.get("final_category") != "frontend_ready":
            fail(f"frontend split contains non-frontend row: {row.get('word')}")
        if row.get("pending_formal_anchor_hit") != "true":
            fail(f"front-end row is not historical anchor: {row.get('word')}")
        if row.get("needs_second_pass") != "true":
            fail(f"front-end row must require second pass: {row.get('word')}")
        if not word_supported(row["word"], row.get("evidence_en", "")):
            fail(f"front-end row missing English evidence: {row['word']}")
        if not meaning_supported(row["final_meaning_zh_simp"], row.get("evidence_zh_simp", "")):
            fail(f"front-end row missing Chinese evidence: {row['word']}")

    split_total = sum(len(read_csv(paths[name])) for name in ("frontend", "learning", "needs_more", "reject"))
    if split_total != expected_rows:
        fail(f"split totals mismatch: {split_total} != {expected_rows}")

    if before_counts is not None and after_counts is not None and before_counts != after_counts:
        fail(f"database counts changed: before={before_counts}, after={after_counts}")

    print(
        json.dumps(
            {
                "ok": True,
                "batch_id": batch_id,
                "validate_only": args.validate_only,
                "reviewed_rows": expected_rows,
                "decision_counts": quality.get("decision_counts"),
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
