#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AGGREGATE = ROOT / "scripts" / "lifestudy_final_review_aggregate.py"
OUTPUT_DIR = ROOT / "reports" / "lifestudy_vocab_final_review"
SUMMARY = OUTPUT_DIR / "final_review_summary.json"
ALL_REVIEWED = OUTPUT_DIR / "final_review_all_reviewed.csv"
FRONTEND_READY = OUTPUT_DIR / "final_review_frontend_ready.csv"
LEARNING_ONLY = OUTPUT_DIR / "final_review_learning_only.csv"
NEEDS_MORE = OUTPUT_DIR / "final_review_needs_more_evidence.csv"
REJECT = OUTPUT_DIR / "final_review_reject.csv"
SUMMARY_MD = OUTPUT_DIR / "final_review_summary.md"

EXPECTED_SOURCE_ROWS = 4102
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


def run_aggregate() -> tuple[dict[str, int] | None, dict[str, int] | None]:
    before = db_counts()
    proc = subprocess.run([sys.executable, str(AGGREGATE)], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        fail(proc.stderr.strip() or proc.stdout.strip())
    after = db_counts()
    return before, after


def main() -> int:
    before_counts, after_counts = run_aggregate()
    for path in (SUMMARY, ALL_REVIEWED, FRONTEND_READY, LEARNING_ONLY, NEEDS_MORE, REJECT, SUMMARY_MD):
        if not path.exists():
            fail(f"missing final review output: {path}")

    payload = json.loads(SUMMARY.read_text(encoding="utf-8"))
    quality = payload.get("quality") or {}
    if payload.get("schema") != "sentence_reader.lifestudy_final_review_summary.v1":
        fail("unexpected final summary schema")
    if payload.get("database_write_performed") is not False:
        fail("final summary must be no-write")
    if payload.get("front_end_import_ready") is not False:
        fail("final summary must not be import-ready")
    if quality.get("total_reviewed_rows") != EXPECTED_SOURCE_ROWS:
        fail(f"reviewed row count mismatch: {quality.get('total_reviewed_rows')} != {EXPECTED_SOURCE_ROWS}")
    if quality.get("front_end_import_ready_count") != 0:
        fail("final review must not mark import-ready rows")
    if quality.get("database_write_count") != 0:
        fail("final review must not mark database writes")
    coverage = quality.get("coverage") or {}
    if coverage.get("missing_source_indices") != 0:
        fail("final review has missing source indices")
    if coverage.get("duplicate_source_indices") != 0:
        fail("final review has duplicate source indices")
    if coverage.get("duplicate_words") != 0:
        fail("final review has duplicate words")

    all_rows = read_csv(ALL_REVIEWED)
    if len(all_rows) != EXPECTED_SOURCE_ROWS:
        fail("final all_reviewed CSV row count mismatch")
    source_indices = [int(row["source_index"]) for row in all_rows]
    if sorted(source_indices) != list(range(1, EXPECTED_SOURCE_ROWS + 1)):
        fail("final source_index coverage mismatch")
    for row in all_rows:
        if row.get("final_category") not in VALID_CATEGORIES:
            fail(f"invalid final category: {row.get('word')} -> {row.get('final_category')}")
        if not row.get("final_reason"):
            fail(f"missing final reason: {row.get('word')}")
        if row.get("database_write_performed") != "false":
            fail(f"row marked database write: {row.get('word')}")
        if row.get("front_end_import_ready") != "false":
            fail(f"row marked import-ready: {row.get('word')}")

    split_total = sum(len(read_csv(path)) for path in (FRONTEND_READY, LEARNING_ONLY, NEEDS_MORE, REJECT))
    if split_total != EXPECTED_SOURCE_ROWS:
        fail(f"final split totals mismatch: {split_total} != {EXPECTED_SOURCE_ROWS}")

    for row in read_csv(FRONTEND_READY):
        if not word_supported(row["word"], row.get("evidence_en", "")):
            fail(f"front-end row missing English evidence: {row['word']}")
        if not meaning_supported(row["final_meaning_zh_simp"], row.get("evidence_zh_simp", "")):
            fail(f"front-end row missing Chinese evidence: {row['word']}")

    if before_counts is not None and after_counts is not None and before_counts != after_counts:
        fail(f"database counts changed: before={before_counts}, after={after_counts}")

    print(
        json.dumps(
            {
                "ok": True,
                "total_reviewed_rows": quality.get("total_reviewed_rows"),
                "production_batches": quality.get("production_batches"),
                "decision_counts": quality.get("decision_counts"),
                "database_counts_checked": before_counts is not None and after_counts is not None,
                "database_counts": after_counts,
                "outputs": payload.get("outputs"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
