#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import math
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "reports" / "lifestudy_vocab_final_review"
SOURCE_CSV = ROOT / "reports" / "lifestudy_vocab_corpus" / "lifestudy_dictionary_guided_review_v2_possible_frontend_after_human_review.csv"
CALIBRATION_REVIEWED = OUTPUT_DIR / "calibration_001_reviewed.csv"

EXPECTED_SOURCE_ROWS = 4102
EXPECTED_CALIBRATION_ROWS = 100
DEFAULT_BATCH_SIZE = 250
VALID_CATEGORIES = {"frontend_ready", "learning_only", "needs_more_evidence", "reject"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fail(message: str) -> None:
    raise SystemExit(message)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        fail(f"missing CSV: {path}")
    return list(csv.DictReader(path.open(encoding="utf-8-sig")))


def csv_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: csv_value(row.get(field)) for field in fieldnames})


def word_supported(word: str, evidence_en: str) -> bool:
    return bool(re.search(rf"(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])", evidence_en or "", re.IGNORECASE))


def meaning_parts(meaning: str) -> list[str]:
    return [part.strip() for part in re.split(r"[；;/,，、]", meaning or "") if part.strip()]


def meaning_supported(meaning: str, evidence_zh: str) -> bool:
    parts = meaning_parts(meaning)
    return bool(parts) and all(part in (evidence_zh or "") for part in parts)


def production_batch_count(batch_size: int = DEFAULT_BATCH_SIZE) -> int:
    source_rows = read_csv(SOURCE_CSV)
    calibration_rows = read_csv(CALIBRATION_REVIEWED)
    if len(source_rows) != EXPECTED_SOURCE_ROWS:
        fail(f"source row count mismatch: {len(source_rows)} != {EXPECTED_SOURCE_ROWS}")
    if len(calibration_rows) != EXPECTED_CALIBRATION_ROWS:
        fail(f"calibration row count mismatch: {len(calibration_rows)} != {EXPECTED_CALIBRATION_ROWS}")
    calibration_words = {(row.get("word") or "").strip().lower() for row in calibration_rows}
    remaining = [row for row in source_rows if (row.get("word") or "").strip().lower() not in calibration_words]
    return math.ceil(len(remaining) / batch_size)


def load_reviewed_batches() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in read_csv(CALIBRATION_REVIEWED):
        item: dict[str, Any] = dict(row)
        item["review_origin"] = "calibration_001"
        rows.append(item)
    total_batches = production_batch_count()
    for batch in range(1, total_batches + 1):
        batch_id = f"batch_{batch:03d}"
        path = OUTPUT_DIR / f"{batch_id}_reviewed.csv"
        for row in read_csv(path):
            item = dict(row)
            item["review_origin"] = batch_id
            rows.append(item)
    return rows


def validate_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if len(rows) != EXPECTED_SOURCE_ROWS:
        fail(f"all reviewed row count mismatch: {len(rows)} != {EXPECTED_SOURCE_ROWS}")
    source_indices = [int(str(row.get("source_index") or "0")) for row in rows]
    expected_indices = set(range(1, EXPECTED_SOURCE_ROWS + 1))
    actual_indices = set(source_indices)
    missing = sorted(expected_indices - actual_indices)
    duplicate_indices = sorted(index for index, count in Counter(source_indices).items() if count > 1)
    if missing:
        fail(f"missing source indices: {missing[:20]}")
    if duplicate_indices:
        fail(f"duplicate source indices: {duplicate_indices[:20]}")
    words = [str(row.get("word") or "").strip().lower() for row in rows]
    duplicate_words = sorted(word for word, count in Counter(words).items() if count > 1)
    if duplicate_words:
        fail(f"duplicate reviewed words: {duplicate_words[:20]}")
    for row in rows:
        word = str(row.get("word") or "").strip()
        final_category = str(row.get("final_category") or "").strip()
        if final_category not in VALID_CATEGORIES:
            fail(f"invalid final_category for {word}: {final_category}")
        if not str(row.get("final_reason") or "").strip():
            fail(f"missing final_reason for {word}")
        if str(row.get("database_write_performed") or "").lower() != "false":
            fail(f"row marked database write: {word}")
        if str(row.get("front_end_import_ready") or "").lower() != "false":
            fail(f"row marked import-ready: {word}")
        if final_category == "frontend_ready":
            if not word_supported(word, str(row.get("evidence_en") or "")):
                fail(f"front-end row missing English evidence: {word}")
            if not meaning_supported(str(row.get("final_meaning_zh_simp") or ""), str(row.get("evidence_zh_simp") or "")):
                fail(f"front-end row missing Chinese evidence: {word}")
    return {
        "missing_source_indices": len(missing),
        "duplicate_source_indices": len(duplicate_indices),
        "duplicate_words": len(duplicate_words),
    }


def ordered_fields(rows: list[dict[str, Any]]) -> list[str]:
    preferred = [
        "review_origin",
        "batch_id",
        "source_index",
        "word",
        "lemma",
        "reviewed_meaning_zh_simp",
        "known_pending_formal_meaning_zh_simp",
        "known_pending_formal_source",
        "suggested_final_meaning_zh_simp",
        "final_category",
        "final_meaning_zh_simp",
        "final_reason",
        "needs_second_pass",
        "pending_formal_anchor_hit",
        "pending_formal_meaning_mismatch",
        "same_record_english_hit",
        "same_record_chinese_hit",
        "front_end_candidate_ready",
        "front_end_import_ready",
        "database_write_performed",
        "candidate_confidence",
        "candidate_source",
        "total_content_frequency",
        "volume_count",
        "source_volume",
        "source_page",
        "evidence_en",
        "evidence_zh_simp",
    ]
    all_fields = []
    for row in rows:
        for field in row:
            if field not in all_fields:
                all_fields.append(field)
    return preferred + [field for field in all_fields if field not in preferred]


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# Life-study Final Review Summary",
        "",
        "This is the no-write review summary for all 4,102 candidates. It is not an import package.",
        "",
        f"- Total reviewed rows: `{payload['quality']['total_reviewed_rows']}`",
        f"- Calibration rows: `{payload['quality']['calibration_rows']}`",
        f"- Production batches: `{payload['quality']['production_batches']}`",
        f"- Database write performed: `{payload['database_write_performed']}`",
        f"- Front-end import ready rows: `{payload['quality']['front_end_import_ready_count']}`",
        "",
        "## Decision Counts",
        "",
    ]
    for category, count in sorted(payload["quality"]["decision_counts"].items()):
        lines.append(f"- `{category}`: `{count}`")
    lines.extend(["", "## Outputs", ""])
    for key, value in payload["outputs"].items():
        lines.append(f"- `{key}`: `{value}`")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    rows = load_reviewed_batches()
    coverage = validate_rows(rows)
    fields = ordered_fields(rows)
    split = {category: [row for row in rows if row.get("final_category") == category] for category in VALID_CATEGORIES}
    decision_counts = dict(Counter(str(row.get("final_category") or "") for row in rows))
    outputs = {
        "all_reviewed_csv": str(OUTPUT_DIR / "final_review_all_reviewed.csv"),
        "frontend_ready_csv": str(OUTPUT_DIR / "final_review_frontend_ready.csv"),
        "learning_only_csv": str(OUTPUT_DIR / "final_review_learning_only.csv"),
        "needs_more_evidence_csv": str(OUTPUT_DIR / "final_review_needs_more_evidence.csv"),
        "reject_csv": str(OUTPUT_DIR / "final_review_reject.csv"),
        "summary_json": str(OUTPUT_DIR / "final_review_summary.json"),
        "summary_md": str(OUTPUT_DIR / "final_review_summary.md"),
    }
    write_csv(Path(outputs["all_reviewed_csv"]), rows, fields)
    write_csv(Path(outputs["frontend_ready_csv"]), split["frontend_ready"], fields)
    write_csv(Path(outputs["learning_only_csv"]), split["learning_only"], fields)
    write_csv(Path(outputs["needs_more_evidence_csv"]), split["needs_more_evidence"], fields)
    write_csv(Path(outputs["reject_csv"]), split["reject"], fields)
    payload = {
        "schema": "sentence_reader.lifestudy_final_review_summary.v1",
        "generated_at": now_iso(),
        "source_csv": str(SOURCE_CSV),
        "database_write_performed": False,
        "front_end_import_ready": False,
        "quality": {
            "total_source_rows": EXPECTED_SOURCE_ROWS,
            "total_reviewed_rows": len(rows),
            "calibration_rows": EXPECTED_CALIBRATION_ROWS,
            "production_batches": production_batch_count(),
            "front_end_import_ready_count": sum(1 for row in rows if str(row.get("front_end_import_ready") or "").lower() == "true"),
            "database_write_count": sum(1 for row in rows if str(row.get("database_write_performed") or "").lower() == "true"),
            "decision_counts": decision_counts,
            "coverage": coverage,
        },
        "outputs": outputs,
    }
    Path(outputs["summary_json"]).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_markdown(Path(outputs["summary_md"]), payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
