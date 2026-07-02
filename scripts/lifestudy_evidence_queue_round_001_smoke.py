#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUND_SCRIPT = ROOT / "scripts" / "lifestudy_evidence_queue_round_001.py"
PRODUCTIZED_SMOKE = ROOT / "scripts" / "lifestudy_vocab_productize_layers_smoke.py"
OUTPUT_DIR = ROOT / "reports" / "lifestudy_vocab_final_review"
INPUT_CSV = OUTPUT_DIR / "evidence_queue_round_001_input.csv"
REVIEWED_CSV = OUTPUT_DIR / "evidence_queue_round_001_reviewed.csv"
PROMOTE_CSV = OUTPUT_DIR / "evidence_queue_round_001_promote_default_front_glossary.csv"
MOVE_LEARNING_CSV = OUTPUT_DIR / "evidence_queue_round_001_move_to_learning_vocab.csv"
KEEP_CSV = OUTPUT_DIR / "evidence_queue_round_001_keep_evidence_queue.csv"
REJECT_CSV = OUTPUT_DIR / "evidence_queue_round_001_reject.csv"
SUMMARY_JSON = OUTPUT_DIR / "evidence_queue_round_001_summary.json"

EXPECTED_ROWS = 50
VALID_DECISIONS = {
    "promote_default_front_glossary",
    "move_to_learning_vocab",
    "keep_evidence_queue",
    "reject",
}


def fail(message: str) -> None:
    raise SystemExit(message)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        fail(f"missing CSV: {path}")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [{key.lstrip("\ufeff"): value for key, value in row.items()} for row in csv.DictReader(handle)]


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


def run_round() -> tuple[dict[str, int] | None, dict[str, int] | None]:
    before = db_counts()
    proc = subprocess.run([sys.executable, str(ROUND_SCRIPT)], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        fail(proc.stderr.strip() or proc.stdout.strip())
    after = db_counts()
    return before, after


def run_productized_smoke() -> None:
    proc = subprocess.run([sys.executable, str(PRODUCTIZED_SMOKE)], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        fail(proc.stderr.strip() or proc.stdout.strip())


def is_false(value: str) -> bool:
    return str(value).strip().lower() == "false"


def meaning_in_all_evidence(row: dict[str, str]) -> bool:
    meaning = row.get("proposed_final_meaning_zh_simp", "")
    if not meaning:
        return False
    for index in range(1, 4):
        if meaning not in (row.get(f"evidence_zh_{index}") or ""):
            return False
    return True


def main() -> int:
    before, after = run_round()
    payload = json.loads(SUMMARY_JSON.read_text(encoding="utf-8"))
    if payload.get("schema") != "sentence_reader.lifestudy_evidence_queue_round_001.v1":
        fail("unexpected summary schema")
    if payload.get("database_write_performed") is not False:
        fail("round summary must be no-write")
    if payload.get("front_end_import_ready") is not False:
        fail("round summary must not mark import-ready")
    quality = payload.get("quality") or {}
    if quality.get("input_rows") != EXPECTED_ROWS:
        fail("input row count mismatch")
    if quality.get("reviewed_rows") != EXPECTED_ROWS:
        fail("reviewed row count mismatch")
    if quality.get("database_write_count") != 0:
        fail("database write count must be zero")
    if quality.get("front_end_import_ready_count") != 0:
        fail("front-end import-ready count must be zero")

    input_rows = read_csv(INPUT_CSV)
    reviewed = read_csv(REVIEWED_CSV)
    promote = read_csv(PROMOTE_CSV)
    move_learning = read_csv(MOVE_LEARNING_CSV)
    keep = read_csv(KEEP_CSV)
    reject = read_csv(REJECT_CSV)
    if len(input_rows) != EXPECTED_ROWS:
        fail("input CSV row count mismatch")
    if len(reviewed) != EXPECTED_ROWS:
        fail("reviewed CSV row count mismatch")
    if len(promote) + len(move_learning) + len(keep) + len(reject) != EXPECTED_ROWS:
        fail("split CSV totals mismatch")
    if len({row["word"].strip().lower() for row in reviewed}) != EXPECTED_ROWS:
        fail("duplicate reviewed words")
    for row in reviewed:
        word = row.get("word", "")
        if row.get("decision") not in VALID_DECISIONS:
            fail(f"invalid decision for {word}: {row.get('decision')}")
        if not row.get("decision_reason"):
            fail(f"missing decision reason for {word}")
        if not is_false(row.get("database_write_performed", "")):
            fail(f"database write flag set for {word}")
        if not is_false(row.get("front_end_import_ready", "")):
            fail(f"front-end import-ready flag set for {word}")
    for row in promote:
        word = row.get("word", "")
        if int(row.get("evidence_count") or 0) < 3:
            fail(f"promoted row has fewer than 3 evidence rows: {word}")
        if not meaning_in_all_evidence(row):
            fail(f"promoted meaning not supported by all evidence rows: {word}")
        if row.get("product_layer_after_review") != "default_front_glossary":
            fail(f"promoted row has wrong layer: {word}")
        if row.get("can_default_popup") != "true":
            fail(f"promoted row cannot default popup: {word}")
    for row in move_learning:
        word = row.get("word", "")
        if int(row.get("evidence_count") or 0) < 1:
            fail(f"learning row has no evidence: {word}")
        if row.get("product_layer_after_review") != "learning_vocab":
            fail(f"learning row has wrong layer: {word}")
        if row.get("can_learning_search") != "true":
            fail(f"learning row cannot learning search: {word}")
    for row in reject:
        if not row.get("decision_reason"):
            fail(f"reject row missing reason: {row.get('word')}")
    if before is not None and after is not None and before != after:
        fail(f"database counts changed: before={before}, after={after}")
    run_productized_smoke()
    print(
        json.dumps(
            {
                "ok": True,
                "reviewed_rows": len(reviewed),
                "decision_counts": quality.get("decision_counts"),
                "corrected_meaning_count": quality.get("corrected_meaning_count"),
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
