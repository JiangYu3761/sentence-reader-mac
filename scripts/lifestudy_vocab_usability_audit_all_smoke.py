#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "lifestudy_vocab_usability_audit_all.py"
OUTPUT_DIR = ROOT / "reports" / "lifestudy_vocab_final_review"

AUDIT_ALL_CSV = OUTPUT_DIR / "final_usability_audit_all_4102.csv"
AUDIT_DEFAULT_POPUP_CSV = OUTPUT_DIR / "final_usability_audit_default_popup.csv"
AUDIT_HIGH_CONFIDENCE_POPUP_CSV = OUTPUT_DIR / "final_usability_audit_high_confidence_popup.csv"
AUDIT_CLICK_LOOKUP_CSV = OUTPUT_DIR / "final_usability_audit_click_lookup.csv"
AUDIT_LEARNING_SEARCH_CSV = OUTPUT_DIR / "final_usability_audit_learning_search.csv"
AUDIT_NEEDS_FIX_CSV = OUTPUT_DIR / "final_usability_audit_needs_fix.csv"
AUDIT_SUMMARY_JSON = OUTPUT_DIR / "final_usability_audit_summary.json"

EXPECTED_TOTAL = 4102
EXPECTED_DEFAULT_POPUP = 4102
EXPECTED_HIGH_CONFIDENCE_POPUP = 49


def fail(message: str) -> None:
    raise SystemExit(f"lifestudy vocab usability audit smoke FAIL: {message}")


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        fail(f"missing CSV: {path}")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [{key.lstrip("\ufeff"): value for key, value in row.items()} for row in csv.DictReader(handle)]


def run_script() -> None:
    proc = subprocess.run([sys.executable, str(SCRIPT)], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        fail(proc.stderr.strip() or proc.stdout.strip())


def main() -> int:
    run_script()

    rows = read_csv(AUDIT_ALL_CSV)
    default_rows = read_csv(AUDIT_DEFAULT_POPUP_CSV)
    high_confidence_rows = read_csv(AUDIT_HIGH_CONFIDENCE_POPUP_CSV)
    click_rows = read_csv(AUDIT_CLICK_LOOKUP_CSV)
    learning_rows = read_csv(AUDIT_LEARNING_SEARCH_CSV)
    needs_fix_rows = read_csv(AUDIT_NEEDS_FIX_CSV)
    summary = json.loads(AUDIT_SUMMARY_JSON.read_text(encoding="utf-8"))

    if len(rows) != EXPECTED_TOTAL:
        fail(f"all audit row count mismatch: {len(rows)}")
    if len({row["term"] for row in rows}) != EXPECTED_TOTAL:
        fail("duplicate terms in audit")
    if len(default_rows) != EXPECTED_DEFAULT_POPUP:
        fail(f"default popup count mismatch: {len(default_rows)}")
    if len(high_confidence_rows) != EXPECTED_HIGH_CONFIDENCE_POPUP:
        fail(f"high-confidence popup count mismatch: {len(high_confidence_rows)}")
    if len(click_rows) != EXPECTED_TOTAL:
        fail(f"click lookup count mismatch: {len(click_rows)}")
    if len(learning_rows) != EXPECTED_TOTAL:
        fail(f"learning search count mismatch: {len(learning_rows)}")
    if needs_fix_rows:
        fail(f"needs-fix rows should be empty, got {len(needs_fix_rows)}")

    for row in rows:
        term = row.get("term")
        if row.get("usable_in_database") != "true":
            fail(f"not usable in database: {term}")
        if row.get("can_click_lookup") != "true":
            fail(f"not click-lookup usable: {term}")
        if row.get("can_default_popup") != "true":
            fail(f"not single-click popup usable: {term}")
        if row.get("single_click_popup_enabled") != "true":
            fail(f"single_click_popup_enabled not true: {term}")
        if row.get("can_learning_search") != "true":
            fail(f"not learning-search usable: {term}")
        if row.get("can_wordbook") != "true":
            fail(f"not wordbook usable: {term}")
        if row.get("needs_fix_before_use") != "false":
            fail(f"needs fix before use: {term}")
        if not row.get("meaning_zh_simp"):
            fail(f"missing meaning: {term}")
        if not row.get("evidence_en") or not row.get("evidence_zh_simp"):
            fail(f"missing evidence: {term}")
        if row.get("database_write_performed") != "false":
            fail(f"database write flag set: {term}")
        if row.get("front_end_import_ready") != "false":
            fail(f"front-end import-ready flag set: {term}")

    quality = summary.get("quality") or {}
    expected_quality = {
        "total_rows": EXPECTED_TOTAL,
        "usable_in_database_count": EXPECTED_TOTAL,
        "can_click_lookup_count": EXPECTED_TOTAL,
        "can_learning_search_count": EXPECTED_TOTAL,
        "needs_fix_before_use_count": 0,
        "can_default_popup_count": EXPECTED_DEFAULT_POPUP,
        "single_click_popup_count": EXPECTED_DEFAULT_POPUP,
        "high_confidence_popup_count": EXPECTED_HIGH_CONFIDENCE_POPUP,
        "database_write_count": 0,
        "front_end_import_ready_count": 0,
    }
    for key, expected in expected_quality.items():
        if quality.get(key) != expected:
            fail(f"summary {key} mismatch: {quality.get(key)} != {expected}")

    print(
        json.dumps(
            {
                "ok": True,
                "total_rows": EXPECTED_TOTAL,
                "usable_in_database": EXPECTED_TOTAL,
                "can_click_lookup": EXPECTED_TOTAL,
                "can_learning_search": EXPECTED_TOTAL,
                "can_default_popup": EXPECTED_DEFAULT_POPUP,
                "single_click_popup": EXPECTED_DEFAULT_POPUP,
                "high_confidence_popup": EXPECTED_HIGH_CONFIDENCE_POPUP,
                "needs_fix_before_use": 0,
                "database_write_performed": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
