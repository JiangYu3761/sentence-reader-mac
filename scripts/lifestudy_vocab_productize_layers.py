#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "reports" / "lifestudy_vocab_final_review"
ALL_REVIEWED = OUTPUT_DIR / "final_review_all_reviewed.csv"
IMPORT_CANDIDATES = OUTPUT_DIR / "final_import_candidates_after_49_review.csv"

LAYER_ALL = OUTPUT_DIR / "productized_lifestudy_vocab_all_layers.csv"
LAYER_FRONT = OUTPUT_DIR / "productized_lifestudy_default_front_glossary.csv"
LAYER_LEARNING = OUTPUT_DIR / "productized_lifestudy_learning_vocab.csv"
LAYER_EVIDENCE = OUTPUT_DIR / "productized_lifestudy_evidence_queue.csv"
LAYER_REJECT = OUTPUT_DIR / "productized_lifestudy_reject.csv"
SUMMARY_JSON = OUTPUT_DIR / "productized_lifestudy_vocab_summary.json"
SUMMARY_MD = OUTPUT_DIR / "productized_lifestudy_vocab_summary.md"

EXPECTED_TOTAL = 4102
EXPECTED_FRONT = 33

LAYER_POLICIES = {
    "default_front_glossary": {
        "display_policy": "default_lookup_popup",
        "recommended_surface": "reader_default_lookup",
        "description": "High-confidence Life-study terms that can appear in the default reader lookup after dry-run and user confirmation.",
    },
    "learning_vocab": {
        "display_policy": "learning_search_and_review",
        "recommended_surface": "learning_vocab_search_review",
        "description": "Evidence-linked learning words. Productize in search, review, and wordbook surfaces, not default reader popups.",
    },
    "evidence_queue": {
        "display_policy": "evidence_queue",
        "recommended_surface": "curation_backlog",
        "description": "Potentially valuable terms that need stronger or additional aligned evidence before front-end promotion.",
    },
    "reject": {
        "display_policy": "do_not_surface",
        "recommended_surface": "none",
        "description": "Misaligned, noisy, or unsuitable rows. Keep only for audit traceability.",
    },
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fail(message: str) -> None:
    raise SystemExit(message)


def normalize_row(row: dict[str, str]) -> dict[str, str]:
    return {key.lstrip("\ufeff"): value for key, value in row.items()}


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        fail(f"missing CSV: {path}")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [normalize_row(row) for row in csv.DictReader(handle)]


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


def candidate_map() -> dict[str, dict[str, str]]:
    rows = read_csv(IMPORT_CANDIDATES)
    if len(rows) != EXPECTED_FRONT:
        fail(f"import candidate count mismatch: {len(rows)} != {EXPECTED_FRONT}")
    result: dict[str, dict[str, str]] = {}
    for row in rows:
        word = (row.get("word") or "").strip().lower()
        if not word:
            fail("candidate row missing word")
        if word in result:
            fail(f"duplicate import candidate word: {word}")
        result[word] = row
    return result


def classify_row(row: dict[str, str], candidates: dict[str, dict[str, str]]) -> dict[str, Any]:
    word = (row.get("word") or "").strip().lower()
    final_category = (row.get("final_category") or "").strip()
    if word in candidates:
        layer = "default_front_glossary"
        meaning = candidates[word].get("precheck_final_meaning_zh_simp") or row.get("final_meaning_zh_simp") or row.get("reviewed_meaning_zh_simp")
        product_reason = candidates[word].get("precheck_reason") or "Confirmed for default front glossary candidate."
        can_default_popup = True
        can_learning_search = True
        needs_more_evidence = False
    elif final_category == "learning_only":
        layer = "learning_vocab"
        meaning = row.get("final_meaning_zh_simp") or row.get("reviewed_meaning_zh_simp")
        product_reason = "Productize as learning/search/review vocabulary, not as default reader popup."
        can_default_popup = False
        can_learning_search = True
        needs_more_evidence = False
    elif final_category == "needs_more_evidence":
        layer = "evidence_queue"
        meaning = row.get("reviewed_meaning_zh_simp") or row.get("final_meaning_zh_simp")
        product_reason = "Keep in evidence queue until stronger aligned evidence supports promotion."
        can_default_popup = False
        can_learning_search = False
        needs_more_evidence = True
    elif final_category == "reject":
        layer = "reject"
        meaning = ""
        product_reason = "Rejected during final review; do not surface except audit traceability."
        can_default_popup = False
        can_learning_search = False
        needs_more_evidence = False
    else:
        fail(f"unknown final_category for {word}: {final_category}")

    policy = LAYER_POLICIES[layer]
    return {
        "product_layer": layer,
        "display_policy": policy["display_policy"],
        "recommended_surface": policy["recommended_surface"],
        "can_default_popup": can_default_popup,
        "can_learning_search": can_learning_search,
        "needs_more_evidence": needs_more_evidence,
        "product_meaning_zh_simp": meaning or "",
        "product_reason": product_reason,
        "database_write_performed": False,
        "reader_dictionary_import": False,
        "domain_glossary_import": False,
        "front_end_import_ready": False,
        "review_origin": row.get("review_origin", ""),
        "batch_id": row.get("batch_id", ""),
        "source_index": row.get("source_index", ""),
        "word": row.get("word", ""),
        "lemma": row.get("lemma", ""),
        "reviewed_meaning_zh_simp": row.get("reviewed_meaning_zh_simp", ""),
        "final_category": final_category,
        "final_meaning_zh_simp": row.get("final_meaning_zh_simp", ""),
        "final_reason": row.get("final_reason", ""),
        "total_content_frequency": row.get("total_content_frequency", ""),
        "volume_count": row.get("volume_count", ""),
        "source_volume": row.get("source_volume", ""),
        "source_page": row.get("source_page", ""),
        "evidence_en": row.get("evidence_en", ""),
        "evidence_zh_simp": row.get("evidence_zh_simp", ""),
    }


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    q = payload["quality"]
    lines = [
        "# Productized Life-study Vocabulary Layers",
        "",
        "This package turns all 4,102 reviewed Life-study vocabulary rows into product layers. It is a no-write packaging step, not a database import.",
        "",
        "## Layer Counts",
        "",
    ]
    for layer, count in q["layer_counts"].items():
        lines.append(f"- `{layer}`: `{count}`")
    lines.extend(
        [
            "",
            "## Product Meaning",
            "",
            "- `default_front_glossary`: can become default reader lookup after dry-run and user confirmation.",
            "- `learning_vocab`: should go online as search/review/wordbook assets, not default popups.",
            "- `evidence_queue`: preserved for future evidence expansion.",
            "- `reject`: preserved only for audit traceability.",
            "",
            "## Safety",
            "",
            f"- Database writes: `{q['database_write_count']}`",
            f"- Reader dictionary imports: `{q['reader_dictionary_import_count']}`",
            f"- Domain glossary imports: `{q['domain_glossary_import_count']}`",
            f"- Front-end import-ready flags: `{q['front_end_import_ready_count']}`",
            "",
            "## Outputs",
            "",
        ]
    )
    for key, value in payload["outputs"].items():
        lines.append(f"- `{key}`: `{value}`")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    rows = read_csv(ALL_REVIEWED)
    if len(rows) != EXPECTED_TOTAL:
        fail(f"all reviewed count mismatch: {len(rows)} != {EXPECTED_TOTAL}")
    candidates = candidate_map()
    product_rows = [classify_row(row, candidates) for row in rows]
    layer_counts = dict(Counter(row["product_layer"] for row in product_rows))
    if sum(layer_counts.values()) != EXPECTED_TOTAL:
        fail("product layer counts do not add up")
    if layer_counts.get("default_front_glossary") != EXPECTED_FRONT:
        fail(f"default front count mismatch: {layer_counts.get('default_front_glossary')} != {EXPECTED_FRONT}")
    if len({row["word"].strip().lower() for row in product_rows}) != EXPECTED_TOTAL:
        fail("duplicate productized words")

    fields = [
        "product_layer",
        "display_policy",
        "recommended_surface",
        "can_default_popup",
        "can_learning_search",
        "needs_more_evidence",
        "product_meaning_zh_simp",
        "product_reason",
        "database_write_performed",
        "reader_dictionary_import",
        "domain_glossary_import",
        "front_end_import_ready",
        "review_origin",
        "batch_id",
        "source_index",
        "word",
        "lemma",
        "reviewed_meaning_zh_simp",
        "final_category",
        "final_meaning_zh_simp",
        "final_reason",
        "total_content_frequency",
        "volume_count",
        "source_volume",
        "source_page",
        "evidence_en",
        "evidence_zh_simp",
    ]
    split = {layer: [row for row in product_rows if row["product_layer"] == layer] for layer in LAYER_POLICIES}
    write_csv(LAYER_ALL, product_rows, fields)
    write_csv(LAYER_FRONT, split["default_front_glossary"], fields)
    write_csv(LAYER_LEARNING, split["learning_vocab"], fields)
    write_csv(LAYER_EVIDENCE, split["evidence_queue"], fields)
    write_csv(LAYER_REJECT, split["reject"], fields)

    payload = {
        "schema": "sentence_reader.lifestudy_vocab_product_layers.v1",
        "generated_at": now_iso(),
        "quality": {
            "total_rows": len(product_rows),
            "layer_counts": {
                "default_front_glossary": layer_counts.get("default_front_glossary", 0),
                "learning_vocab": layer_counts.get("learning_vocab", 0),
                "evidence_queue": layer_counts.get("evidence_queue", 0),
                "reject": layer_counts.get("reject", 0),
            },
            "database_write_count": sum(1 for row in product_rows if row["database_write_performed"] is not False),
            "reader_dictionary_import_count": sum(1 for row in product_rows if row["reader_dictionary_import"] is not False),
            "domain_glossary_import_count": sum(1 for row in product_rows if row["domain_glossary_import"] is not False),
            "front_end_import_ready_count": sum(1 for row in product_rows if row["front_end_import_ready"] is not False),
        },
        "policies": LAYER_POLICIES,
        "outputs": {
            "all_layers_csv": str(LAYER_ALL),
            "default_front_glossary_csv": str(LAYER_FRONT),
            "learning_vocab_csv": str(LAYER_LEARNING),
            "evidence_queue_csv": str(LAYER_EVIDENCE),
            "reject_csv": str(LAYER_REJECT),
            "summary_json": str(SUMMARY_JSON),
            "summary_md": str(SUMMARY_MD),
        },
        "database_write_performed": False,
        "front_end_import_ready": False,
    }
    SUMMARY_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(SUMMARY_MD, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
