#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRODUCTIZE = ROOT / "scripts" / "lifestudy_vocab_productize_layers.py"
OUTPUT_DIR = ROOT / "reports" / "lifestudy_vocab_final_review"
LAYER_ALL = OUTPUT_DIR / "productized_lifestudy_vocab_all_layers.csv"
LAYER_FRONT = OUTPUT_DIR / "productized_lifestudy_default_front_glossary.csv"
LAYER_LEARNING = OUTPUT_DIR / "productized_lifestudy_learning_vocab.csv"
LAYER_EVIDENCE = OUTPUT_DIR / "productized_lifestudy_evidence_queue.csv"
LAYER_REJECT = OUTPUT_DIR / "productized_lifestudy_reject.csv"
SUMMARY_JSON = OUTPUT_DIR / "productized_lifestudy_vocab_summary.json"

EXPECTED_COUNTS = {
    "default_front_glossary": 33,
    "learning_vocab": 3968,
    "evidence_queue": 96,
    "reject": 5,
}
EXPECTED_TOTAL = sum(EXPECTED_COUNTS.values())


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


def run_productize() -> tuple[dict[str, int] | None, dict[str, int] | None]:
    before = db_counts()
    proc = subprocess.run([sys.executable, str(PRODUCTIZE)], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        fail(proc.stderr.strip() or proc.stdout.strip())
    after = db_counts()
    return before, after


def assert_no_write_flags(rows: list[dict[str, str]]) -> None:
    for row in rows:
        word = row.get("word")
        if row.get("database_write_performed") != "false":
            fail(f"database write flag set: {word}")
        if row.get("reader_dictionary_import") != "false":
            fail(f"reader dictionary import flag set: {word}")
        if row.get("domain_glossary_import") != "false":
            fail(f"domain glossary import flag set: {word}")
        if row.get("front_end_import_ready") != "false":
            fail(f"front-end import-ready flag set: {word}")


def main() -> int:
    before, after = run_productize()
    payload = json.loads(SUMMARY_JSON.read_text(encoding="utf-8"))
    if payload.get("schema") != "sentence_reader.lifestudy_vocab_product_layers.v1":
        fail("unexpected product layer schema")
    if payload.get("database_write_performed") is not False:
        fail("productize must not write database")
    if payload.get("front_end_import_ready") is not False:
        fail("productize must not mark import-ready")
    quality = payload.get("quality") or {}
    if quality.get("total_rows") != EXPECTED_TOTAL:
        fail("total row count mismatch")
    if quality.get("layer_counts") != EXPECTED_COUNTS:
        fail(f"layer counts mismatch: {quality.get('layer_counts')} != {EXPECTED_COUNTS}")
    for key in ("database_write_count", "reader_dictionary_import_count", "domain_glossary_import_count", "front_end_import_ready_count"):
        if quality.get(key) != 0:
            fail(f"{key} must be zero")

    all_rows = read_csv(LAYER_ALL)
    front_rows = read_csv(LAYER_FRONT)
    learning_rows = read_csv(LAYER_LEARNING)
    evidence_rows = read_csv(LAYER_EVIDENCE)
    reject_rows = read_csv(LAYER_REJECT)
    if len(all_rows) != EXPECTED_TOTAL:
        fail("all layer CSV row count mismatch")
    if len(front_rows) != EXPECTED_COUNTS["default_front_glossary"]:
        fail("front layer row count mismatch")
    if len(learning_rows) != EXPECTED_COUNTS["learning_vocab"]:
        fail("learning layer row count mismatch")
    if len(evidence_rows) != EXPECTED_COUNTS["evidence_queue"]:
        fail("evidence layer row count mismatch")
    if len(reject_rows) != EXPECTED_COUNTS["reject"]:
        fail("reject layer row count mismatch")
    if len({row["word"].strip().lower() for row in all_rows}) != EXPECTED_TOTAL:
        fail("duplicate words in product layers")
    assert_no_write_flags(all_rows)
    for row in front_rows:
        if row.get("can_default_popup") != "true":
            fail(f"front row cannot default popup: {row.get('word')}")
        if row.get("can_learning_search") != "true":
            fail(f"front row cannot learning search: {row.get('word')}")
    for row in learning_rows:
        if row.get("can_default_popup") != "false":
            fail(f"learning row can default popup: {row.get('word')}")
        if row.get("can_learning_search") != "true":
            fail(f"learning row cannot learning search: {row.get('word')}")
    for row in evidence_rows:
        if row.get("needs_more_evidence") != "true":
            fail(f"evidence row not marked needs_more_evidence: {row.get('word')}")
    if before is not None and after is not None and before != after:
        fail(f"database counts changed: before={before}, after={after}")

    print(
        json.dumps(
            {
                "ok": True,
                "layer_counts": quality.get("layer_counts"),
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
