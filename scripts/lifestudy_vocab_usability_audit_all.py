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

IMPORTABLE_JSON = OUTPUT_DIR / "final_database_import_prepare_all_4102.json"
AUDIT_ALL_CSV = OUTPUT_DIR / "final_usability_audit_all_4102.csv"
AUDIT_DEFAULT_POPUP_CSV = OUTPUT_DIR / "final_usability_audit_default_popup.csv"
AUDIT_HIGH_CONFIDENCE_POPUP_CSV = OUTPUT_DIR / "final_usability_audit_high_confidence_popup.csv"
AUDIT_CLICK_LOOKUP_CSV = OUTPUT_DIR / "final_usability_audit_click_lookup.csv"
AUDIT_LEARNING_SEARCH_CSV = OUTPUT_DIR / "final_usability_audit_learning_search.csv"
AUDIT_NEEDS_FIX_CSV = OUTPUT_DIR / "final_usability_audit_needs_fix.csv"
AUDIT_SUMMARY_JSON = OUTPUT_DIR / "final_usability_audit_summary.json"
AUDIT_SUMMARY_MD = OUTPUT_DIR / "final_usability_audit_summary.md"

EXPECTED_TOTAL = 4102


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fail(message: str) -> None:
    raise SystemExit(message)


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


def load_items() -> list[dict[str, Any]]:
    if not IMPORTABLE_JSON.exists():
        fail(f"missing importable JSON: {IMPORTABLE_JSON}")
    payload = json.loads(IMPORTABLE_JSON.read_text(encoding="utf-8"))
    if payload.get("schema") != "sentence_reader.lifestudy_vocab_importable.v1":
        fail(f"unexpected schema: {payload.get('schema')}")
    items = payload.get("items") or []
    if len(items) != EXPECTED_TOTAL:
        fail(f"item count mismatch: {len(items)} != {EXPECTED_TOTAL}")
    return items


def surface_decision(item: dict[str, Any]) -> dict[str, Any]:
    term = str(item.get("term") or "").strip()
    meaning = str(item.get("suggested_meaning_zh_simp") or "").strip()
    evidence_ok = item.get("meaning_supported_by_current_evidence") is True
    import_ok = item.get("import_allowed") is True and evidence_ok and bool(term) and bool(meaning)
    can_default_popup = bool(item.get("can_default_popup")) and import_ok
    high_confidence_popup = bool(item.get("high_confidence_popup")) and import_ok
    can_click_lookup = import_ok
    can_learning_search = import_ok
    needs_fix = not import_ok
    needs_followup_review = bool(item.get("needs_followup_review")) and import_ok

    if needs_fix:
        usability_status = "needs_fix_before_use"
        primary_surface = "none"
        reason = "Missing required import fields or evidence support."
    elif high_confidence_popup:
        usability_status = "usable_single_click_popup_high_confidence"
        primary_surface = "reader_single_click_popup"
        reason = "Single-click popup is enabled; this term is also high-confidence/high-priority."
    elif needs_followup_review:
        usability_status = "usable_single_click_popup_followup"
        primary_surface = "reader_single_click_popup"
        reason = "Single-click popup is enabled; keep follow-up review flag for future sense refinement."
    else:
        usability_status = "usable_single_click_popup"
        primary_surface = "reader_single_click_popup"
        reason = "Single-click popup/search/wordbook entry is enabled."

    return {
        "term": term,
        "lemma": item.get("lemma", ""),
        "meaning_zh_simp": meaning,
        "usable_in_database": import_ok,
        "can_default_popup": can_default_popup,
        "single_click_popup_enabled": can_default_popup,
        "high_confidence_popup": high_confidence_popup,
        "can_click_lookup": can_click_lookup,
        "can_learning_search": can_learning_search,
        "can_wordbook": can_learning_search,
        "needs_followup_review": needs_followup_review,
        "needs_fix_before_use": needs_fix,
        "usability_status": usability_status,
        "primary_surface": primary_surface,
        "quality_grade": item.get("quality_grade", ""),
        "reason": reason,
        "evidence_en": item.get("evidence_en", ""),
        "evidence_zh_simp": item.get("evidence_zh_simp", ""),
        "source_volume": item.get("source_volume", ""),
        "source_page": item.get("source_page", ""),
        "occurrence_count": item.get("occurrence_count", ""),
        "volume_count": item.get("volume_count", ""),
        "match_source": item.get("match_source", ""),
        "product_layer_after_prepare": item.get("product_layer_after_prepare", ""),
        "original_product_layer": item.get("original_product_layer", ""),
        "original_final_category": item.get("original_final_category", ""),
        "repaired_from_reject": item.get("repaired_from_reject") is True,
        "database_write_performed": False,
        "front_end_import_ready": False,
    }


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    q = payload["quality"]
    lines = [
        "# Life-study Full Usability Audit",
        "",
        "This audit answers the practical product question: after review, what can each of the 4,102 Life-study vocabulary rows do?",
        "",
        "Important distinction: popup means the user single-clicks an English word and sees the Life-study meaning. High-confidence priority is only an extra ranking flag, not the popup boundary.",
        "",
        "## Result",
        "",
        f"- Total rows audited one by one: `{q['total_rows']}`",
        f"- Usable in database: `{q['usable_in_database_count']}`",
        f"- Single-click popup enabled: `{q['can_default_popup_count']}`",
        f"- High-confidence popup priority: `{q['high_confidence_popup_count']}`",
        f"- Usable for explicit click lookup: `{q['can_click_lookup_count']}`",
        f"- Usable for learning/search/wordbook: `{q['can_learning_search_count']}`",
        f"- Needs follow-up review but still usable on click/search: `{q['needs_followup_review_count']}`",
        f"- Needs fix before any use: `{q['needs_fix_before_use_count']}`",
        f"- Database writes performed: `{q['database_write_count']}`",
        "",
        "## How To Read This",
        "",
        "- `can_default_popup=true`: the user single-clicks the English word and the popup can show the Life-study meaning.",
        "- `can_click_lookup=true`: same interaction boundary; kept for compatibility with existing reader code.",
        "- `can_learning_search=true`: the word can appear in search, review, and wordbook flows.",
        "- `high_confidence_popup=true`: high-priority/high-confidence subset, not the only popup set.",
        "- `needs_followup_review=true`: still pop on single click; flag it for later sense refinement.",
        "",
        "## Outputs",
        "",
    ]
    for key, value in payload["outputs"].items():
        lines.append(f"- `{key}`: `{value}`")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    items = load_items()
    rows = [surface_decision(item) for item in items]
    terms = [row["term"] for row in rows]
    if len(set(terms)) != EXPECTED_TOTAL:
        fail("duplicate terms found")

    default_rows = [row for row in rows if row["can_default_popup"] is True]
    high_confidence_rows = [row for row in rows if row["high_confidence_popup"] is True]
    click_rows = [row for row in rows if row["can_click_lookup"] is True]
    learning_rows = [row for row in rows if row["can_learning_search"] is True]
    needs_fix_rows = [row for row in rows if row["needs_fix_before_use"] is True]

    fields = [
        "term",
        "lemma",
        "meaning_zh_simp",
        "usable_in_database",
        "can_default_popup",
        "single_click_popup_enabled",
        "high_confidence_popup",
        "can_click_lookup",
        "can_learning_search",
        "can_wordbook",
        "needs_followup_review",
        "needs_fix_before_use",
        "usability_status",
        "primary_surface",
        "quality_grade",
        "reason",
        "evidence_en",
        "evidence_zh_simp",
        "source_volume",
        "source_page",
        "occurrence_count",
        "volume_count",
        "match_source",
        "product_layer_after_prepare",
        "original_product_layer",
        "original_final_category",
        "repaired_from_reject",
        "database_write_performed",
        "front_end_import_ready",
    ]
    write_csv(AUDIT_ALL_CSV, rows, fields)
    write_csv(AUDIT_DEFAULT_POPUP_CSV, default_rows, fields)
    write_csv(AUDIT_HIGH_CONFIDENCE_POPUP_CSV, high_confidence_rows, fields)
    write_csv(AUDIT_CLICK_LOOKUP_CSV, click_rows, fields)
    write_csv(AUDIT_LEARNING_SEARCH_CSV, learning_rows, fields)
    write_csv(AUDIT_NEEDS_FIX_CSV, needs_fix_rows, fields)

    quality = {
        "total_rows": len(rows),
        "usable_in_database_count": sum(1 for row in rows if row["usable_in_database"] is True),
        "can_default_popup_count": len(default_rows),
        "single_click_popup_count": len(default_rows),
        "high_confidence_popup_count": len(high_confidence_rows),
        "can_click_lookup_count": len(click_rows),
        "can_learning_search_count": len(learning_rows),
        "needs_followup_review_count": sum(1 for row in rows if row["needs_followup_review"] is True),
        "needs_fix_before_use_count": len(needs_fix_rows),
        "database_write_count": 0,
        "front_end_import_ready_count": 0,
        "usability_status_counts": dict(Counter(row["usability_status"] for row in rows)),
    }
    payload = {
        "schema": "sentence_reader.lifestudy_vocab_usability_audit.v1",
        "generated_at": now_iso(),
        "source_importable_json": str(IMPORTABLE_JSON),
        "database_write_performed": False,
        "front_end_import_ready": False,
        "quality": quality,
        "outputs": {
            "all_csv": str(AUDIT_ALL_CSV),
            "default_popup_csv": str(AUDIT_DEFAULT_POPUP_CSV),
            "high_confidence_popup_csv": str(AUDIT_HIGH_CONFIDENCE_POPUP_CSV),
            "click_lookup_csv": str(AUDIT_CLICK_LOOKUP_CSV),
            "learning_search_csv": str(AUDIT_LEARNING_SEARCH_CSV),
            "needs_fix_csv": str(AUDIT_NEEDS_FIX_CSV),
            "summary_json": str(AUDIT_SUMMARY_JSON),
            "summary_md": str(AUDIT_SUMMARY_MD),
        },
    }
    AUDIT_SUMMARY_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(AUDIT_SUMMARY_MD, payload)
    print(json.dumps({"ok": True, **quality, "outputs": payload["outputs"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
