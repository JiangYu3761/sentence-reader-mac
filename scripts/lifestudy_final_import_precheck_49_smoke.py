#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRECHECK = ROOT / "scripts" / "lifestudy_final_import_precheck_49.py"
OUTPUT_DIR = ROOT / "reports" / "lifestudy_vocab_final_review"
REVIEWED = OUTPUT_DIR / "final_import_precheck_49_reviewed.csv"
CANDIDATES = OUTPUT_DIR / "final_import_candidates_after_49_review.csv"
NOT_IMPORTED = OUTPUT_DIR / "final_import_precheck_49_not_imported.csv"
SUMMARY = OUTPUT_DIR / "final_import_precheck_49_summary.json"

EXPECTED_REVIEWED_ROWS = 49
EXPECTED_CANDIDATES = 33
EXPECTED_NOT_IMPORTED = 16


def fail(message: str) -> None:
    raise SystemExit(message)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        fail(f"missing CSV: {path}")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [{key.lstrip("\ufeff"): value for key, value in row.items()} for row in csv.DictReader(handle)]


def word_supported(word: str, evidence_en: str) -> bool:
    return bool(re.search(rf"(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])", evidence_en or "", re.IGNORECASE))


def meaning_parts(meaning: str) -> list[str]:
    return [part.strip() for part in re.split(r"[；;/,，、]", meaning or "") if part.strip()]


def meaning_supported(meaning: str, evidence_zh: str) -> bool:
    parts = meaning_parts(meaning)
    return bool(parts) and all(part in (evidence_zh or "") for part in parts)


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


def run_precheck() -> tuple[dict[str, int] | None, dict[str, int] | None]:
    before = db_counts()
    proc = subprocess.run([sys.executable, str(PRECHECK)], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        fail(proc.stderr.strip() or proc.stdout.strip())
    after = db_counts()
    return before, after


def main() -> int:
    before, after = run_precheck()
    payload = json.loads(SUMMARY.read_text(encoding="utf-8"))
    quality = payload.get("quality") or {}
    if payload.get("schema") != "sentence_reader.lifestudy_final_import_precheck_49.v1":
        fail("unexpected precheck schema")
    if payload.get("database_write_performed") is not False:
        fail("precheck must not write database")
    if payload.get("front_end_import_ready") is not False:
        fail("precheck must not mark rows import-ready")
    if quality.get("reviewed_rows") != EXPECTED_REVIEWED_ROWS:
        fail("reviewed count mismatch")
    if quality.get("first_import_candidate_count") != EXPECTED_CANDIDATES:
        fail("candidate count mismatch")
    if quality.get("not_imported_count") != EXPECTED_NOT_IMPORTED:
        fail("not-imported count mismatch")
    if quality.get("database_write_count") != 0:
        fail("row marked database write")
    if quality.get("front_end_import_ready_count") != 0:
        fail("row marked front-end import-ready")

    reviewed = read_csv(REVIEWED)
    candidates = read_csv(CANDIDATES)
    not_imported = read_csv(NOT_IMPORTED)
    if len(reviewed) != EXPECTED_REVIEWED_ROWS:
        fail("reviewed CSV row count mismatch")
    if len(candidates) != EXPECTED_CANDIDATES:
        fail("candidate CSV row count mismatch")
    if len(not_imported) != EXPECTED_NOT_IMPORTED:
        fail("not-imported CSV row count mismatch")
    if len({row["word"].strip().lower() for row in candidates}) != len(candidates):
        fail("duplicate candidate words")
    for row in reviewed:
        if row.get("database_write_performed") != "false":
            fail(f"row marked database write: {row.get('word')}")
        if row.get("front_end_import_ready") != "false":
            fail(f"row marked import-ready: {row.get('word')}")
    for row in candidates:
        word = row.get("word", "")
        meaning = row.get("precheck_final_meaning_zh_simp", "")
        if not word_supported(word, row.get("evidence_en", "")):
            fail(f"candidate missing English evidence: {word}")
        if not meaning_supported(meaning, row.get("evidence_zh_simp", "")):
            fail(f"candidate missing Chinese evidence: {word} -> {meaning}")
    if before is not None and after is not None and before != after:
        fail(f"database counts changed: before={before}, after={after}")

    print(
        json.dumps(
            {
                "ok": True,
                "reviewed_rows": len(reviewed),
                "first_import_candidate_count": len(candidates),
                "not_imported_count": len(not_imported),
                "database_counts_checked": before is not None and after is not None,
                "database_counts": after,
                "outputs": payload.get("outputs"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
