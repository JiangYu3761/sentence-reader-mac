#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADJUDICATE = ROOT / "scripts" / "lifestudy_final_review_calibration_adjudicate.py"
OUTPUT_DIR = ROOT / "reports" / "lifestudy_vocab_final_review"
SUMMARY = OUTPUT_DIR / "calibration_001_reviewed_summary.json"
REVIEWED = OUTPUT_DIR / "calibration_001_reviewed.csv"
FRONTEND_READY = OUTPUT_DIR / "calibration_001_frontend_ready.csv"
LEARNING_ONLY = OUTPUT_DIR / "calibration_001_learning_only.csv"
NEEDS_MORE = OUTPUT_DIR / "calibration_001_needs_more_evidence.csv"
REJECT = OUTPUT_DIR / "calibration_001_reject.csv"
MARKDOWN = OUTPUT_DIR / "calibration_001_reviewed.md"
PLAN = ROOT / "docs" / "lifestudy_vocab_final_review_plan.md"

EXPECTED_COUNTS = {
    "frontend_ready": 21,
    "learning_only": 74,
    "needs_more_evidence": 5,
}
EXPECTED_CORRECTIONS = {
    "righteousness": "公义",
    "redemption": "救赎",
    "reality": "实际",
    "priesthood": "祭司职分",
    "anointing": "受膏；膏油的涂抹",
}
EXPECTED_NEEDS_MORE = {"age", "cross", "flesh", "nourishment", "power"}


def fail(message: str) -> None:
    raise SystemExit(message)


def run_adjudicator() -> None:
    proc = subprocess.run(
        [sys.executable, str(ADJUDICATE)],
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
    run_adjudicator()
    for path in (SUMMARY, REVIEWED, FRONTEND_READY, LEARNING_ONLY, NEEDS_MORE, REJECT, MARKDOWN):
        if not path.exists():
            fail(f"missing reviewed calibration output: {path}")

    payload = json.loads(SUMMARY.read_text(encoding="utf-8"))
    quality = payload.get("quality") or {}
    if payload.get("schema") != "sentence_reader.lifestudy_final_review_calibration_reviewed.v1":
        fail("unexpected reviewed calibration schema")
    if payload.get("database_write_performed") is not False:
        fail("reviewed calibration must not write database")
    if payload.get("front_end_import_ready") is not False:
        fail("reviewed calibration must not mark final import-ready")
    if quality.get("reviewed_rows") != 100:
        fail("reviewed calibration must contain 100 rows")
    if quality.get("blank_final_category_rows") != 0:
        fail("all rows must have final_category")
    if quality.get("blank_final_reason_rows") != 0:
        fail("all rows must have final_reason")
    if quality.get("front_end_import_ready_count") != 0:
        fail("no calibration row may be import-ready")
    if quality.get("database_write_count") != 0:
        fail("no calibration row may write database")
    decision_counts = quality.get("decision_counts") or {}
    for category, expected in EXPECTED_COUNTS.items():
        if decision_counts.get(category) != expected:
            fail(f"{category} count mismatch: {decision_counts.get(category)} != {expected}")
    if decision_counts.get("reject", 0) != 0:
        fail("calibration 001 should have zero rejects; unsupported terms are learning or needs_more_evidence")

    reviewed = read_csv(REVIEWED)
    by_word = {row["word"]: row for row in reviewed}
    if len(by_word) != 100:
        fail("reviewed calibration contains duplicate words")
    for word, expected_meaning in EXPECTED_CORRECTIONS.items():
        row = by_word.get(word)
        if not row:
            fail(f"missing correction row: {word}")
        if row.get("final_category") != "frontend_ready":
            fail(f"corrected anchor must be frontend_ready: {word}")
        if row.get("final_meaning_zh_simp") != expected_meaning:
            fail(f"corrected meaning mismatch for {word}: {row.get('final_meaning_zh_simp')} != {expected_meaning}")

    needs_more = {row["word"] for row in read_csv(NEEDS_MORE)}
    if needs_more != EXPECTED_NEEDS_MORE:
        fail(f"needs_more_evidence set mismatch: {sorted(needs_more)} != {sorted(EXPECTED_NEEDS_MORE)}")
    frontend_rows = read_csv(FRONTEND_READY)
    if len(frontend_rows) != EXPECTED_COUNTS["frontend_ready"]:
        fail("front-end ready split count mismatch")
    for row in frontend_rows:
        if row.get("pending_formal_anchor_hit") != "true":
            fail(f"front-end ready row must come from pending formal anchor in calibration 001: {row.get('word')}")
        if not row.get("final_meaning_zh_simp"):
            fail(f"front-end ready row missing final meaning: {row.get('word')}")

    plan_text = PLAN.read_text(encoding="utf-8")
    for phrase in ("calibration_001_reviewed.csv", "calibration_001_frontend_ready.csv", "calibration_001_needs_more_evidence.csv"):
        if phrase not in plan_text:
            fail(f"plan missing reviewed calibration output reference: {phrase}")

    print(
        json.dumps(
            {
                "ok": True,
                "reviewed_rows": quality.get("reviewed_rows"),
                "decision_counts": decision_counts,
                "corrected_anchors": EXPECTED_CORRECTIONS,
                "needs_more_evidence": sorted(EXPECTED_NEEDS_MORE),
                "database_write_performed": payload.get("database_write_performed"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
