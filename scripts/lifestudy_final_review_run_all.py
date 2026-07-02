#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import math
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "reports" / "lifestudy_vocab_final_review"
SOURCE_CSV = ROOT / "reports" / "lifestudy_vocab_corpus" / "lifestudy_dictionary_guided_review_v2_possible_frontend_after_human_review.csv"
CALIBRATION_REVIEWED = OUTPUT_DIR / "calibration_001_reviewed.csv"
BATCH_SMOKE = ROOT / "scripts" / "lifestudy_final_review_batch_smoke.py"
FINAL_SMOKE = ROOT / "scripts" / "lifestudy_final_review_final_smoke.py"

EXPECTED_SOURCE_ROWS = 4102
EXPECTED_CALIBRATION_ROWS = 100
BATCH_SIZE = 250


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fail(message: str) -> None:
    raise SystemExit(message)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        fail(f"missing CSV: {path}")
    return list(csv.DictReader(path.open(encoding="utf-8-sig")))


def run_command(args: list[str]) -> dict[str, Any]:
    proc = subprocess.run(args, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        fail(proc.stderr.strip() or proc.stdout.strip())
    text = proc.stdout.strip()
    try:
        return json.loads(text[text.find("{") :])
    except Exception:
        return {"stdout": text}


def total_batches() -> int:
    source_rows = read_csv(SOURCE_CSV)
    calibration_rows = read_csv(CALIBRATION_REVIEWED)
    if len(source_rows) != EXPECTED_SOURCE_ROWS:
        fail(f"source row count mismatch: {len(source_rows)} != {EXPECTED_SOURCE_ROWS}")
    if len(calibration_rows) != EXPECTED_CALIBRATION_ROWS:
        fail(f"calibration row count mismatch: {len(calibration_rows)} != {EXPECTED_CALIBRATION_ROWS}")
    calibration_words = {(row.get("word") or "").strip().lower() for row in calibration_rows}
    remaining = [row for row in source_rows if (row.get("word") or "").strip().lower() not in calibration_words]
    return math.ceil(len(remaining) / BATCH_SIZE)


def batch_complete(batch: int) -> bool:
    batch_id = f"batch_{batch:03d}"
    required = [
        OUTPUT_DIR / f"{batch_id}_input.csv",
        OUTPUT_DIR / f"{batch_id}_review_template.csv",
        OUTPUT_DIR / f"{batch_id}_reviewed.csv",
        OUTPUT_DIR / f"{batch_id}_frontend_ready.csv",
        OUTPUT_DIR / f"{batch_id}_learning_only.csv",
        OUTPUT_DIR / f"{batch_id}_needs_more_evidence.csv",
        OUTPUT_DIR / f"{batch_id}_reject.csv",
        OUTPUT_DIR / f"{batch_id}_reviewed_summary.json",
        OUTPUT_DIR / f"{batch_id}_reviewed.md",
    ]
    return all(path.exists() for path in required)


def main() -> int:
    batch_total = total_batches()
    generated: list[int] = []
    skipped: list[int] = []
    batch_results: list[dict[str, Any]] = []
    for batch in range(1, batch_total + 1):
        if batch_complete(batch):
            result = run_command([sys.executable, str(BATCH_SMOKE), "--batch", str(batch), "--validate-only"])
            skipped.append(batch)
        else:
            result = run_command([sys.executable, str(BATCH_SMOKE), "--batch", str(batch)])
            generated.append(batch)
        batch_results.append(result)

    final_result = run_command([sys.executable, str(FINAL_SMOKE)])
    payload = {
        "schema": "sentence_reader.lifestudy_final_review_run_all.v1",
        "generated_at": now_iso(),
        "batch_size": BATCH_SIZE,
        "production_batches": batch_total,
        "skipped_completed_batches": skipped,
        "generated_batches": generated,
        "batch_results": batch_results,
        "final_result": final_result,
        "database_write_performed": False,
        "front_end_import_ready": False,
    }
    path = OUTPUT_DIR / "final_review_run_all_summary.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
