#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "reports" / "lifestudy_vocab_final_review"

PRODUCTIZED_ALL = OUTPUT_DIR / "productized_lifestudy_vocab_all_layers.csv"
ROUND_001 = OUTPUT_DIR / "evidence_queue_round_001_reviewed.csv"

IMPORTABLE_JSON = OUTPUT_DIR / "final_database_import_prepare_all_4102.json"
IMPORTABLE_CSV = OUTPUT_DIR / "final_database_import_prepare_all_4102.csv"
DEFAULT_POPUP_CSV = OUTPUT_DIR / "final_database_import_prepare_default_popup.csv"
HIGH_CONFIDENCE_POPUP_CSV = OUTPUT_DIR / "final_database_import_prepare_high_confidence_popup.csv"
LEARNING_SEARCH_CSV = OUTPUT_DIR / "final_database_import_prepare_learning_search.csv"
REPAIRED_CSV = OUTPUT_DIR / "final_database_import_prepare_repaired_from_reject.csv"
SUMMARY_JSON = OUTPUT_DIR / "final_database_import_prepare_summary.json"
SUMMARY_MD = OUTPUT_DIR / "final_database_import_prepare_summary.md"

EXPECTED_TOTAL = 4102

POS_ZH = {
    "noun": "名词",
    "verb": "动词",
    "adjective": "形容词",
    "adverb": "副词",
    "proper_noun": "专有名词",
    "noun_or_verb": "名词或动词",
    "adjective_or_noun": "形容词或名词",
}

PROPER_NOUNS = {
    "abraham",
    "adam",
    "christ",
    "david",
    "ephesians",
    "god",
    "isaac",
    "israel",
    "jacob",
    "jesus",
    "john",
    "joseph",
    "moses",
    "paul",
    "peter",
    "pharisees",
    "philippians",
    "satan",
}

REPAIRS_FROM_REJECT: dict[str, dict[str, str]] = {
    "ephesians": {
        "meaning": "以弗所书",
        "reason": "Repair rejected row for database import: English evidence names Ephesians, and Chinese evidence has 以弗所/以弗所书 context.",
    },
    "philippians": {
        "meaning": "腓立比书",
        "reason": "Repair rejected row for database import: English evidence names Philippians, and Chinese evidence uses 腓立比二章 as the book reference.",
    },
    "divinity": {
        "meaning": "神性",
        "reason": "Repair rejected row for database import: aligned Chinese evidence directly contains 神性.",
    },
    "pharisees": {
        "meaning": "法利赛人",
        "reason": "Repair rejected row for database import: aligned Chinese evidence directly contains 法利赛人.",
    },
    "anointed": {
        "meaning": "膏",
        "reason": "Repair rejected row for database import: aligned Chinese evidence contains 所膏; use the supported root meaning 膏 for learning/search.",
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


def word_key(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def compact_word(value: str) -> str:
    return re.sub(r"[^a-z']", "", word_key(value).replace("’", "'"))


def infer_part_of_speech(term: str, lemma: str, meaning: str) -> str:
    word = compact_word(term)
    lemma_key = compact_word(lemma)
    if word in PROPER_NOUNS or "书" in meaning and word in {"ephesians", "philippians"}:
        return "proper_noun"
    if word.endswith("ly"):
        return "adverb"
    if word.endswith(("tion", "sion", "ment", "ness", "ity", "ship", "hood", "ance", "ence", "ism", "ist", "er", "or")):
        return "noun"
    if word.endswith(("ous", "ful", "less", "ive", "al", "ial", "ic", "ical", "able", "ible", "ary")):
        return "adjective"
    if word.endswith("ing"):
        if lemma_key and lemma_key != word:
            return "noun_or_verb"
        return "noun_or_verb"
    if word.endswith("ed"):
        return "verb"
    if "/" in meaning or "；" in meaning:
        return "noun_or_verb"
    return "noun"


def pos_zh(pos: str) -> str:
    return POS_ZH.get(pos, "词")


def popup_speak_text(term: str, pos: str, meaning: str) -> str:
    return f"{pos_zh(pos)}，{meaning}。英文，{term}。"


def to_int(value: Any) -> int:
    try:
        return int(float(str(value or 0)))
    except ValueError:
        return 0


def to_float(value: Any) -> float:
    try:
        return float(str(value or 0))
    except ValueError:
        return 0.0


def round_001_by_word() -> dict[str, dict[str, str]]:
    if not ROUND_001.exists():
        return {}
    return {word_key(row.get("word", "")): row for row in read_csv(ROUND_001)}


def meaning_supported(meaning: str, evidence_zh: str, *, repaired_word: str = "") -> bool:
    if not meaning:
        return False
    parts = [part.strip() for part in re.split(r"[；;/,，、\s]+", meaning) if part.strip()]
    if parts and all(part in evidence_zh for part in parts):
        return True
    if repaired_word == "philippians" and "腓立比" in evidence_zh:
        return True
    return False


def apply_round_evidence(item: dict[str, Any], review: dict[str, str], meaning: str) -> None:
    chosen = 1
    for index in range(1, 4):
        if meaning_supported(meaning, review.get(f"evidence_zh_{index}", "")):
            chosen = index
            break
    item["evidence_en"] = review.get(f"evidence_en_{chosen}", "") or item["evidence_en"]
    item["evidence_zh_simp"] = review.get(f"evidence_zh_{chosen}", "") or item["evidence_zh_simp"]
    item["source_volume"] = review.get(f"source_volume_{chosen}", "") or item["source_volume"]
    item["source_page"] = review.get(f"source_page_{chosen}", "") or item["source_page"]


def base_item(row: dict[str, str]) -> dict[str, Any]:
    term = word_key(row.get("word", ""))
    lemma = word_key(row.get("lemma", "")) or compact_word(term) or term
    return {
        "term": term,
        "lemma": lemma,
        "source_index": row.get("source_index", ""),
        "source_volume": row.get("source_volume", ""),
        "source_page": row.get("source_page", ""),
        "evidence_en": row.get("evidence_en", ""),
        "evidence_zh_simp": row.get("evidence_zh_simp", ""),
        "occurrence_count": to_int(row.get("total_content_frequency", "")),
        "volume_count": to_int(row.get("volume_count", "")),
        "score": to_float(row.get("total_content_frequency", "")) + to_float(row.get("volume_count", "")) * 10.0,
        "original_product_layer": row.get("product_layer", ""),
        "original_final_category": row.get("final_category", ""),
        "original_meaning_zh_simp": row.get("product_meaning_zh_simp", ""),
        "database_write_performed": False,
        "front_end_import_ready": False,
        "reader_dictionary_import_candidate": False,
        "domain_glossary_import_candidate": True,
        "import_allowed": True,
    }


def import_item(row: dict[str, str], round_rows: dict[str, dict[str, str]]) -> dict[str, Any]:
    item = base_item(row)
    term = item["term"]
    layer = row.get("product_layer", "")
    meaning = row.get("product_meaning_zh_simp", "").strip()
    quality_grade = "B"
    can_default_popup = True
    single_click_popup_enabled = True
    high_confidence_popup = False
    can_learning_search = True
    needs_followup = False
    product_layer_after_prepare = "learning_vocab"
    reason = row.get("product_reason", "") or row.get("final_reason", "")
    decision_source = "productized_final_review"
    repaired_from_reject = False

    if layer == "default_front_glossary":
        quality_grade = "A"
        high_confidence_popup = True
        product_layer_after_prepare = "default_front_glossary"
        decision_source = "final_import_precheck_49"
    elif term in round_rows:
        review = round_rows[term]
        meaning = review.get("proposed_final_meaning_zh_simp", "").strip() or meaning
        apply_round_evidence(item, review, meaning)
        decision = review.get("decision", "")
        reason = review.get("decision_reason", "") or reason
        decision_source = "evidence_queue_round_001"
        if decision == "promote_default_front_glossary":
            quality_grade = "A"
            high_confidence_popup = True
            product_layer_after_prepare = "default_front_glossary"
        elif decision == "keep_evidence_queue":
            needs_followup = True
            product_layer_after_prepare = "learning_vocab_needs_followup"
        else:
            product_layer_after_prepare = "learning_vocab"
    elif layer == "evidence_queue":
        needs_followup = True
        product_layer_after_prepare = "learning_vocab_needs_followup"
        reason = "Import as single-click popup/search vocabulary. Keep high-confidence priority disabled until stronger sense evidence is reviewed."
    elif layer == "reject":
        repair = REPAIRS_FROM_REJECT.get(term)
        if not repair:
            fail(f"reject row has no repair rule: {term}")
        meaning = repair["meaning"]
        reason = repair["reason"]
        decision_source = "reject_repair_for_database_import"
        repaired_from_reject = True
        product_layer_after_prepare = "learning_vocab_repaired"
    elif layer != "learning_vocab":
        fail(f"unexpected product layer for {term}: {layer}")

    if not term:
        fail("row missing term")
    if not meaning:
        fail(f"row missing repaired/import meaning: {term}")
    if not item["evidence_en"] or not item["evidence_zh_simp"]:
        fail(f"row missing evidence: {term}")

    evidence_support = meaning_supported(meaning, item["evidence_zh_simp"], repaired_word=term)
    part_of_speech = infer_part_of_speech(term, str(item.get("lemma") or ""), meaning)
    part_of_speech_zh = pos_zh(part_of_speech)
    speak_text = popup_speak_text(term, part_of_speech, meaning)
    item.update(
        {
            "suggested_meaning_zh_simp": meaning,
            "part_of_speech": part_of_speech,
            "part_of_speech_zh": part_of_speech_zh,
            "popup_speak_text_zh": speak_text,
            "tts_engine_preferred": "edge-tts",
            "tts_voice_preferred": "zh-CN-YunjianNeural",
            "quality_grade": quality_grade,
            "can_default_popup": can_default_popup,
            "single_click_popup_enabled": single_click_popup_enabled,
            "high_confidence_popup": high_confidence_popup,
            "can_learning_search": can_learning_search,
            "needs_followup_review": needs_followup,
            "product_layer_after_prepare": product_layer_after_prepare,
            "reason": reason,
            "match_source": decision_source,
            "meaning_supported_by_current_evidence": evidence_support,
            "repaired_from_reject": repaired_from_reject,
            "metadata": {
                "schema": "sentence_reader.lifestudy_full_database_import_prepare.item.v1",
                "original_product_layer": layer,
                "original_final_category": row.get("final_category", ""),
                "source_volume": row.get("source_volume", ""),
                "source_page": row.get("source_page", ""),
                "can_default_popup": can_default_popup,
                "single_click_popup_enabled": single_click_popup_enabled,
                "high_confidence_popup": high_confidence_popup,
                "can_learning_search": can_learning_search,
                "needs_followup_review": needs_followup,
                "repaired_from_reject": repaired_from_reject,
                "part_of_speech": part_of_speech,
                "part_of_speech_zh": part_of_speech_zh,
                "popup_speak_text_zh": speak_text,
                "tts_engine_preferred": "edge-tts",
                "tts_voice_preferred": "zh-CN-YunjianNeural",
            },
        }
    )
    return item


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    q = payload["quality"]
    lines = [
        "# Life-study Full Database Import Preparation",
        "",
        "This package corrects the product stance: all 4,102 reviewed Life-study vocabulary rows are treated as database vocabulary assets and single-click popup terms.",
        "",
        "No PostgreSQL write is performed here. This package is ready for dry-run import and still requires explicit user confirmation before apply.",
        "",
        "## Counts",
        "",
        f"- Total import candidates: `{q['total_import_candidates']}`",
        f"- Single-click popup enabled: `{q['default_popup_count']}`",
        f"- High-confidence popup priority: `{q['high_confidence_popup_count']}`",
        f"- Learning/search enabled: `{q['learning_search_count']}`",
        f"- Needs follow-up but still import candidate: `{q['needs_followup_review_count']}`",
        f"- Repaired from previous reject: `{q['repaired_from_reject_count']}`",
        f"- Database writes performed: `{q['database_write_count']}`",
        "",
        "## Corrected Interpretation",
        "",
        "- `can_default_popup=true` means single-click word popup is enabled in the reader.",
        "- `high_confidence_popup=true` means the popup is high-priority/high-confidence.",
        "- `learning_vocab` means the word still belongs in the Life-study popup/search/review/wordbook surfaces.",
        "- Previous `evidence_queue` rows are imported as single-click popup/search assets with a follow-up-review flag.",
        "- Previous `reject` rows were repaired when the aligned evidence supported a clear term meaning.",
        "",
        "## Repaired Rows",
        "",
    ]
    for row in payload["repaired_from_reject"]:
        lines.append(f"- `{row['term']}` -> `{row['suggested_meaning_zh_simp']}`")
    lines.extend(["", "## Outputs", ""])
    for key, value in payload["outputs"].items():
        lines.append(f"- `{key}`: `{value}`")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    rows = read_csv(PRODUCTIZED_ALL)
    if len(rows) != EXPECTED_TOTAL:
        fail(f"productized row count mismatch: {len(rows)} != {EXPECTED_TOTAL}")

    round_rows = round_001_by_word()
    items = [import_item(row, round_rows) for row in rows]

    terms = [item["term"] for item in items]
    if len(set(terms)) != EXPECTED_TOTAL:
        fail("duplicate import terms found")
    if any(item["database_write_performed"] for item in items):
        fail("prepare step must not write database")
    if any(item["front_end_import_ready"] for item in items):
        fail("prepare step must not mark front-end import-ready")
    unsupported = [item["term"] for item in items if not item["meaning_supported_by_current_evidence"]]
    if unsupported:
        fail(f"meaning not supported by current evidence: {unsupported[:10]}")

    default_rows = [item for item in items if item["can_default_popup"] is True]
    high_confidence_rows = [item for item in items if item["high_confidence_popup"] is True]
    learning_rows = [item for item in items if item["can_learning_search"] is True]
    repaired_rows = [item for item in items if item["repaired_from_reject"] is True]
    counts = Counter(item["product_layer_after_prepare"] for item in items)
    grade_counts = Counter(item["quality_grade"] for item in items)
    match_counts = Counter(item["match_source"] for item in items)

    fieldnames = [
        "term",
        "lemma",
        "suggested_meaning_zh_simp",
        "part_of_speech",
        "part_of_speech_zh",
        "popup_speak_text_zh",
        "tts_engine_preferred",
        "tts_voice_preferred",
        "quality_grade",
        "import_allowed",
        "can_default_popup",
        "single_click_popup_enabled",
        "high_confidence_popup",
        "can_learning_search",
        "needs_followup_review",
        "product_layer_after_prepare",
        "reason",
        "match_source",
        "meaning_supported_by_current_evidence",
        "repaired_from_reject",
        "source_index",
        "source_volume",
        "source_page",
        "evidence_en",
        "evidence_zh_simp",
        "occurrence_count",
        "volume_count",
        "score",
        "original_product_layer",
        "original_final_category",
        "original_meaning_zh_simp",
        "database_write_performed",
        "front_end_import_ready",
        "reader_dictionary_import_candidate",
        "domain_glossary_import_candidate",
        "metadata",
    ]
    write_csv(IMPORTABLE_CSV, items, fieldnames)
    write_csv(DEFAULT_POPUP_CSV, default_rows, fieldnames)
    write_csv(HIGH_CONFIDENCE_POPUP_CSV, high_confidence_rows, fieldnames)
    write_csv(LEARNING_SEARCH_CSV, learning_rows, fieldnames)
    write_csv(REPAIRED_CSV, repaired_rows, fieldnames)

    payload = {
        "schema": "sentence_reader.lifestudy_vocab_importable.v1",
        "generated_at": now_iso(),
        "source_report": str(PRODUCTIZED_ALL),
        "database_write_performed": False,
        "front_end_import_ready": False,
        "import_policy": {
            "target_table": "reader.domain_glossary_entries",
            "all_reviewed_rows_are_database_candidates": True,
            "default_popup_means_single_click_word_popup": True,
            "all_reviewed_rows_enable_single_click_popup": True,
            "high_confidence_popup_is_priority_flag_only": True,
            "reader_dictionary_entries_import": False,
            "requires_user_confirmation_before_apply": True,
        },
        "quality": {
            "total_import_candidates": len(items),
            "default_popup_count": len(default_rows),
            "single_click_popup_count": len(default_rows),
            "high_confidence_popup_count": len(high_confidence_rows),
            "learning_search_count": len(learning_rows),
            "needs_followup_review_count": sum(1 for item in items if item["needs_followup_review"] is True),
            "repaired_from_reject_count": len(repaired_rows),
            "quality_grade_counts": dict(grade_counts),
            "product_layer_after_prepare_counts": dict(counts),
            "match_source_counts": dict(match_counts),
            "database_write_count": 0,
            "front_end_import_ready_count": 0,
        },
        "repaired_from_reject": [
            {
                "term": item["term"],
                "suggested_meaning_zh_simp": item["suggested_meaning_zh_simp"],
                "reason": item["reason"],
            }
            for item in repaired_rows
        ],
        "outputs": {
            "importable_json": str(IMPORTABLE_JSON),
            "importable_csv": str(IMPORTABLE_CSV),
            "default_popup_csv": str(DEFAULT_POPUP_CSV),
            "high_confidence_popup_csv": str(HIGH_CONFIDENCE_POPUP_CSV),
            "learning_search_csv": str(LEARNING_SEARCH_CSV),
            "repaired_from_reject_csv": str(REPAIRED_CSV),
            "summary_json": str(SUMMARY_JSON),
            "summary_md": str(SUMMARY_MD),
        },
        "items": items,
    }
    IMPORTABLE_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    SUMMARY_JSON.write_text(
        json.dumps({key: value for key, value in payload.items() if key != "items"}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_markdown(SUMMARY_MD, payload)
    print(
        json.dumps(
            {
                "ok": True,
                "total_import_candidates": len(items),
                "default_popup_count": len(default_rows),
                "high_confidence_popup_count": len(high_confidence_rows),
                "needs_followup_review_count": payload["quality"]["needs_followup_review_count"],
                "repaired_from_reject_count": len(repaired_rows),
                "outputs": payload["outputs"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
