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
CORPUS_DIR = ROOT / "reports" / "lifestudy_vocab_corpus"
OUTPUT_DIR = ROOT / "reports" / "lifestudy_vocab_final_review"
SOURCE_CSV = CORPUS_DIR / "lifestudy_dictionary_guided_review_v2_possible_frontend_after_human_review.csv"
PENDING_FORMAL_FILES = [
    (ROOT / "reports" / "lifestudy_vocab_pipeline" / "01_Genesis-120-pages-1-1255-importable.csv", "term", "suggested_meaning_zh_simp"),
    (CORPUS_DIR / "lifestudy_vocab_v1_importable.csv", "term", "candidate_meaning_zh_simp"),
    (CORPUS_DIR / "lifestudy_frontend_candidate_adjudication_v2_ready_for_dry_run.csv", "word", "final_meaning_zh_simp"),
    (CORPUS_DIR / "lifestudy_needs_review_frontend_ready_for_dry_run.csv", "word", "final_meaning_zh_simp"),
]

CALIBRATION_SIZE = 100
EXPECTED_SOURCE_ROWS = 4102

DOMAIN_HINTS = {
    "altar",
    "anointing",
    "apostle",
    "apostles",
    "ascension",
    "authority",
    "blessing",
    "consecration",
    "dispensation",
    "dispensing",
    "divine",
    "economy",
    "element",
    "enjoyment",
    "experience",
    "faith",
    "fellowship",
    "glory",
    "gospel",
    "grace",
    "heavenly",
    "holy",
    "justification",
    "life",
    "mingled",
    "ministry",
    "priesthood",
    "reality",
    "redemption",
    "regeneration",
    "revelation",
    "righteousness",
    "sanctification",
    "transformation",
    "truth",
    "vision",
}

GENERIC_LOW_VALUE = {
    "able",
    "according",
    "actually",
    "also",
    "another",
    "anything",
    "became",
    "become",
    "case",
    "come",
    "day",
    "everything",
    "father",
    "first",
    "get",
    "give",
    "go",
    "good",
    "great",
    "hand",
    "have",
    "know",
    "living",
    "make",
    "many",
    "matter",
    "may",
    "mother",
    "much",
    "need",
    "one",
    "people",
    "person",
    "place",
    "receive",
    "see",
    "son",
    "take",
    "thing",
    "things",
    "time",
    "way",
    "work",
}

PROPER_OR_REFERENCE = {
    "abraham",
    "adam",
    "corinthians",
    "david",
    "genesis",
    "israel",
    "jacob",
    "jehovah",
    "jesus",
    "john",
    "joseph",
    "moses",
    "paul",
    "peter",
}

SUSPICIOUS_MEANINGS = {
    "事情",
    "东西",
    "方面",
    "方向",
    "情形",
    "局面",
    "中心",
    "问题",
    "事实",
    "部分",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def as_int(value: Any) -> int:
    try:
        return int(float(str(value or "0")))
    except ValueError:
        return 0


def load_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise SystemExit(f"missing source CSV: {path}")
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    if len(rows) != EXPECTED_SOURCE_ROWS:
        raise SystemExit(f"unexpected source row count: {len(rows)} != {EXPECTED_SOURCE_ROWS}")
    return rows


def word_supported(word: str, evidence_en: str) -> bool:
    if not word:
        return False
    return bool(re.search(rf"(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])", evidence_en or "", re.IGNORECASE))


def meaning_supported(meaning: str, evidence_zh: str) -> bool:
    if not meaning:
        return False
    parts = [part.strip() for part in re.split(r"[；;/,，、]", meaning) if part.strip()]
    return bool(parts) and all(part in (evidence_zh or "") for part in parts)


def load_pending_formal_terms() -> dict[str, dict[str, str]]:
    terms: dict[str, dict[str, str]] = {}
    for path, term_field, meaning_field in PENDING_FORMAL_FILES:
        if not path.exists():
            raise SystemExit(f"missing pending formal file: {path}")
        for row in csv.DictReader(path.open(encoding="utf-8-sig")):
            term = (row.get(term_field) or "").strip().lower()
            meaning = (row.get(meaning_field) or "").strip()
            if not term or not meaning:
                continue
            terms[term] = {
                "meaning": meaning,
                "source_file": str(path.relative_to(ROOT)),
            }
    if len(terms) != 100:
        raise SystemExit(f"unexpected pending formal unique term count: {len(terms)} != 100")
    return terms


def classify_prefilter(row: dict[str, str], pending_formal: dict[str, dict[str, str]]) -> tuple[str, list[str]]:
    word = (row.get("word") or "").strip().lower()
    meaning = (row.get("reviewed_meaning_zh_simp") or "").strip()
    evidence_en = row.get("evidence_en") or ""
    evidence_zh = row.get("evidence_zh_simp") or ""
    reasons: list[str] = []

    en_hit = word_supported(word, evidence_en)
    zh_hit = meaning_supported(meaning, evidence_zh)
    pending = pending_formal.get(word)

    if pending:
        reasons.append("already_in_pending_formal_terms_requires_anchor_check")
        if pending["meaning"] != meaning:
            reasons.append("candidate_meaning_differs_from_pending_formal_meaning")
        return "frontend_ready_candidate", reasons
    if not en_hit or not zh_hit:
        reasons.append("same_record_evidence_incomplete")
        return "needs_more_evidence", reasons
    if word in PROPER_OR_REFERENCE:
        reasons.append("proper_name_or_biblical_reference")
        return "learning_only", reasons
    if word in GENERIC_LOW_VALUE:
        reasons.append("generic_common_word")
        return "learning_only", reasons
    if meaning in SUSPICIOUS_MEANINGS:
        reasons.append("meaning_too_generic_or_structural")
        return "learning_only", reasons
    if word in DOMAIN_HINTS:
        reasons.append("domain_hint_requires_second_pass")
        return "frontend_ready_candidate", reasons
    if as_int(row.get("volume_count")) >= 20 and as_int(row.get("total_content_frequency")) >= 200:
        reasons.append("high_frequency_learning_candidate")
        return "learning_only", reasons
    reasons.append("default_learning_candidate")
    return "learning_only", reasons


def priority_score(row: dict[str, str]) -> float:
    word = (row.get("word") or "").strip().lower()
    freq = as_int(row.get("total_content_frequency"))
    volumes = as_int(row.get("volume_count"))
    score = min(freq, 5000) / 100 + volumes
    if word in DOMAIN_HINTS:
        score += 80
    if word in GENERIC_LOW_VALUE or word in PROPER_OR_REFERENCE:
        score += 25
    if not meaning_supported(row.get("reviewed_meaning_zh_simp") or "", row.get("evidence_zh_simp") or ""):
        score += 15
    return score


def pick_calibration_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    by_word = {(row.get("word") or "").strip().lower(): row for row in rows}
    selected: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(row: dict[str, str]) -> None:
        word = (row.get("word") or "").strip().lower()
        if not word or word in seen:
            return
        selected.append(row)
        seen.add(word)

    for word in sorted((DOMAIN_HINTS & set(by_word)), key=lambda item: -priority_score(by_word[item])):
        add(by_word[word])
        if len(selected) >= 30:
            break

    for word in sorted(((GENERIC_LOW_VALUE | PROPER_OR_REFERENCE) & set(by_word)), key=lambda item: -priority_score(by_word[item])):
        add(by_word[word])
        if len(selected) >= 55:
            break

    for row in sorted(rows, key=priority_score, reverse=True):
        add(row)
        if len(selected) >= 80:
            break

    stride = max(1, len(rows) // 80)
    for index in range(0, len(rows), stride):
        add(rows[index])
        if len(selected) >= CALIBRATION_SIZE:
            break

    for row in rows:
        add(row)
        if len(selected) >= CALIBRATION_SIZE:
            break

    if len(selected) != CALIBRATION_SIZE:
        raise SystemExit(f"failed to build calibration batch: {len(selected)} != {CALIBRATION_SIZE}")
    return selected


def build_review_row(row: dict[str, str], batch_id: str, source_index: int, pending_formal: dict[str, dict[str, str]]) -> dict[str, Any]:
    proposed_category, reasons = classify_prefilter(row, pending_formal)
    word = (row.get("word") or "").strip().lower()
    meaning = (row.get("reviewed_meaning_zh_simp") or "").strip()
    pending = pending_formal.get(word, {})
    evidence_en = row.get("evidence_en") or ""
    evidence_zh = row.get("evidence_zh_simp") or ""
    return {
        "batch_id": batch_id,
        "source_index": source_index,
        "word": word,
        "lemma": row.get("lemma") or "",
        "reviewed_meaning_zh_simp": meaning,
        "known_pending_formal_meaning_zh_simp": pending.get("meaning", ""),
        "known_pending_formal_source": pending.get("source_file", ""),
        "suggested_final_meaning_zh_simp": pending.get("meaning", meaning if proposed_category == "frontend_ready_candidate" else ""),
        "proposed_category": proposed_category,
        "proposed_reason": "; ".join(reasons),
        "final_category": "",
        "final_meaning_zh_simp": "",
        "final_reason": "",
        "needs_second_pass": proposed_category == "frontend_ready_candidate",
        "pending_formal_anchor_hit": bool(pending),
        "pending_formal_meaning_mismatch": bool(pending and pending.get("meaning") != meaning),
        "same_record_english_hit": word_supported(word, evidence_en),
        "same_record_chinese_hit": meaning_supported(meaning, evidence_zh),
        "database_write_performed": False,
        "front_end_import_ready": False,
        "candidate_confidence": row.get("candidate_confidence") or "",
        "candidate_source": row.get("candidate_source") or "",
        "total_content_frequency": as_int(row.get("total_content_frequency")),
        "volume_count": as_int(row.get("volume_count")),
        "source_volume": row.get("source_volume") or "",
        "source_page": row.get("source_page") or "",
        "evidence_en": evidence_en,
        "evidence_zh_simp": evidence_zh,
    }


def csv_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(rows[0]) if rows else []
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: csv_value(row.get(field)) for field in fields})


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# Life-study Final Review Calibration Batch 001",
        "",
        "This is a calibration package for the 4,102-row final review pool. It does not write PostgreSQL and does not mark any row as final import-ready.",
        "",
        f"- Source rows: `{payload['quality']['source_rows']}`",
        f"- Calibration rows: `{payload['quality']['calibration_rows']}`",
        f"- Database write performed: `{payload['database_write_performed']}`",
        "",
        "## Proposed Category Counts",
        "",
    ]
    for category, count in sorted(payload["quality"]["proposed_category_counts"].items()):
        lines.append(f"- `{category}`: `{count}`")
    lines.extend(["", "## Rows", ""])
    for row in payload["items"]:
        lines.extend(
            [
                f"### {row['word']} -> {row['reviewed_meaning_zh_simp']}",
                "",
                f"- Proposed category: `{row['proposed_category']}`",
                f"- Reason: {row['proposed_reason']}",
                f"- Known pending formal meaning: `{row['known_pending_formal_meaning_zh_simp']}`",
                f"- Suggested final meaning: `{row['suggested_final_meaning_zh_simp']}`",
                f"- Needs second pass: `{row['needs_second_pass']}`",
                f"- Evidence hits: EN `{row['same_record_english_hit']}`, ZH `{row['same_record_chinese_hit']}`",
                f"- Source: `{row['source_volume']} p{row['source_page']}`",
                f"- EN: {row['evidence_en']}",
                f"- ZH: {row['evidence_zh_simp']}",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    rows = load_rows(SOURCE_CSV)
    pending_formal = load_pending_formal_terms()
    indexed = [(index + 1, row) for index, row in enumerate(rows)]
    row_to_index = {id(row): index for index, row in indexed}
    selected = pick_calibration_rows(rows)
    batch_id = "calibration_001"
    review_rows = [build_review_row(row, batch_id, row_to_index[id(row)], pending_formal) for row in selected]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    input_csv = OUTPUT_DIR / "calibration_001_input.csv"
    review_csv = OUTPUT_DIR / "calibration_001_review_template.csv"
    summary_json = OUTPUT_DIR / "calibration_001_summary.json"
    markdown = OUTPUT_DIR / "calibration_001_review.md"

    input_rows = [
        {field: row.get(field, "") for field in [
            "word",
            "lemma",
            "reviewed_meaning_zh_simp",
            "candidate_confidence",
            "total_content_frequency",
            "volume_count",
            "source_volume",
            "source_page",
            "evidence_en",
            "evidence_zh_simp",
        ]}
        for row in selected
    ]
    write_csv(input_csv, input_rows)
    write_csv(review_csv, review_rows)
    payload = {
        "schema": "sentence_reader.lifestudy_final_review_calibration.v1",
        "generated_at": now_iso(),
        "source_csv": str(SOURCE_CSV),
        "batch_id": batch_id,
        "database_write_performed": False,
        "front_end_import_ready": False,
        "policy": "calibration_prefilter_no_db_write_no_final_import",
        "quality": {
            "source_rows": len(rows),
            "calibration_rows": len(review_rows),
            "final_category_filled_rows": sum(1 for row in review_rows if row["final_category"]),
            "second_pass_required_rows": sum(1 for row in review_rows if row["needs_second_pass"]),
            "english_hit_rows": sum(1 for row in review_rows if row["same_record_english_hit"]),
            "chinese_hit_rows": sum(1 for row in review_rows if row["same_record_chinese_hit"]),
            "pending_formal_anchor_rows": sum(1 for row in review_rows if row["pending_formal_anchor_hit"]),
            "pending_formal_meaning_mismatch_rows": sum(1 for row in review_rows if row["pending_formal_meaning_mismatch"]),
            "proposed_category_counts": dict(Counter(row["proposed_category"] for row in review_rows)),
        },
        "outputs": {
            "input_csv": str(input_csv),
            "review_template_csv": str(review_csv),
            "summary_json": str(summary_json),
            "markdown": str(markdown),
        },
        "items": review_rows,
    }
    summary_json.write_text(json.dumps({k: v for k, v in payload.items() if k != "items"}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_markdown(markdown, payload)
    print(json.dumps({k: v for k, v in payload.items() if k != "items"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
