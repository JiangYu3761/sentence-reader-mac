#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "lifestudy_final_review_calibration.py"
OUTPUT_DIR = ROOT / "reports" / "lifestudy_vocab_final_review"
SUMMARY = OUTPUT_DIR / "calibration_001_summary.json"
REVIEW_TEMPLATE = OUTPUT_DIR / "calibration_001_review_template.csv"
INPUT = OUTPUT_DIR / "calibration_001_input.csv"
MARKDOWN = OUTPUT_DIR / "calibration_001_review.md"
PLAN = ROOT / "docs" / "lifestudy_vocab_final_review_plan.md"

EXPECTED_SOURCE_ROWS = 4102
EXPECTED_CALIBRATION_ROWS = 100
ALLOWED_PROPOSED = {
    "frontend_ready_candidate",
    "learning_only",
    "needs_more_evidence",
    "reject",
}


def fail(message: str) -> None:
    raise SystemExit(message)


def run_generator() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        fail(proc.stderr.strip() or proc.stdout.strip())


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        fail(f"missing CSV: {path}")
    return list(csv.DictReader(path.open(encoding="utf-8-sig")))


def main() -> int:
    run_generator()
    for path in (SUMMARY, REVIEW_TEMPLATE, INPUT, MARKDOWN):
        if not path.exists():
            fail(f"missing calibration output: {path}")

    payload = json.loads(SUMMARY.read_text(encoding="utf-8"))
    quality = payload.get("quality") or {}
    if payload.get("schema") != "sentence_reader.lifestudy_final_review_calibration.v1":
        fail("unexpected calibration schema")
    if payload.get("database_write_performed") is not False:
        fail("calibration must not write database")
    if payload.get("front_end_import_ready") is not False:
        fail("calibration must not mark import-ready")
    if quality.get("source_rows") != EXPECTED_SOURCE_ROWS:
        fail(f"source row count mismatch: {quality.get('source_rows')} != {EXPECTED_SOURCE_ROWS}")
    if quality.get("calibration_rows") != EXPECTED_CALIBRATION_ROWS:
        fail(f"calibration row count mismatch: {quality.get('calibration_rows')} != {EXPECTED_CALIBRATION_ROWS}")
    if quality.get("final_category_filled_rows") != 0:
        fail("calibration template must not fill final decisions")
    if quality.get("second_pass_required_rows", 0) <= 0:
        fail("calibration batch should include rows requiring second pass")
    if quality.get("pending_formal_anchor_rows", 0) <= 0:
        fail("calibration batch should include existing pending formal anchors")
    if quality.get("pending_formal_meaning_mismatch_rows", 0) <= 0:
        fail("calibration batch should expose pending formal meaning corrections")

    rows = read_csv(REVIEW_TEMPLATE)
    if len(rows) != EXPECTED_CALIBRATION_ROWS:
        fail(f"review template rows mismatch: {len(rows)}")
    words = [row.get("word", "").strip().lower() for row in rows]
    if len(set(words)) != len(words):
        fail("calibration template contains duplicate words")
    for row in rows:
        if row.get("database_write_performed") != "false":
            fail("row must be no-write")
        if row.get("front_end_import_ready") != "false":
            fail("row must not be import-ready")
        if row.get("final_category"):
            fail("final_category must remain blank in calibration template")
        if row.get("proposed_category") not in ALLOWED_PROPOSED:
            fail(f"invalid proposed category: {row.get('proposed_category')}")
        if row.get("pending_formal_meaning_mismatch") == "true" and row.get("needs_second_pass") != "true":
            fail("pending formal meaning mismatches must require second pass")
        if row.get("pending_formal_anchor_hit") == "true" and not row.get("known_pending_formal_meaning_zh_simp"):
            fail("pending formal anchor rows must include known pending meaning")
        for field in ("word", "reviewed_meaning_zh_simp", "evidence_en", "evidence_zh_simp", "source_volume", "source_page"):
            if not row.get(field):
                fail(f"missing required field {field} for {row.get('word')}")

    plan_text = PLAN.read_text(encoding="utf-8")
    required_plan_phrases = [
        "Calibration Gate",
        "250 rows",
        "500 to 1,000 reviewed rows",
        "no row writes to `reader.dictionary_entries`",
        "no row writes to PostgreSQL during review",
    ]
    missing = [phrase for phrase in required_plan_phrases if phrase not in plan_text]
    if missing:
        fail(f"plan missing required phrases: {missing}")

    print(
        json.dumps(
            {
                "ok": True,
                "source_rows": quality.get("source_rows"),
                "calibration_rows": quality.get("calibration_rows"),
                "second_pass_required_rows": quality.get("second_pass_required_rows"),
                "pending_formal_anchor_rows": quality.get("pending_formal_anchor_rows"),
                "pending_formal_meaning_mismatch_rows": quality.get("pending_formal_meaning_mismatch_rows"),
                "proposed_category_counts": quality.get("proposed_category_counts"),
                "database_write_performed": payload.get("database_write_performed"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
