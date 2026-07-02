#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import random
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "reports" / "lifestudy_vocab_final_review"
ALL_REVIEWED = OUTPUT_DIR / "final_review_all_reviewed.csv"
FRONTEND_READY = OUTPUT_DIR / "final_review_frontend_ready.csv"
LEARNING_ONLY = OUTPUT_DIR / "final_review_learning_only.csv"
NEEDS_MORE = OUTPUT_DIR / "final_review_needs_more_evidence.csv"
REJECT = OUTPUT_DIR / "final_review_reject.csv"

AUDIT_SUMMARY_JSON = OUTPUT_DIR / "quality_audit_summary.json"
AUDIT_SUMMARY_MD = OUTPUT_DIR / "quality_audit_summary.md"
AUDIT_FRONTEND_CSV = OUTPUT_DIR / "quality_audit_frontend_ready.csv"
AUDIT_LEARNING_WATCHLIST_CSV = OUTPUT_DIR / "quality_audit_learning_only_watchlist.csv"
AUDIT_LEARNING_RANDOM_CSV = OUTPUT_DIR / "quality_audit_learning_only_random_sample.csv"
AUDIT_NEEDS_MORE_CSV = OUTPUT_DIR / "quality_audit_needs_more_evidence.csv"
AUDIT_REJECT_CSV = OUTPUT_DIR / "quality_audit_reject.csv"

EXPECTED_SOURCE_ROWS = 4102
VALID_CATEGORIES = {"frontend_ready", "learning_only", "needs_more_evidence", "reject"}
RANDOM_SEED = 20260701

PROPER_OR_REFERENCE = {
    "aaron",
    "abraham",
    "adam",
    "babylon",
    "colossians",
    "corinthians",
    "david",
    "egypt",
    "ephesians",
    "ezekiel",
    "genesis",
    "hebrews",
    "isaac",
    "isaiah",
    "israel",
    "jacob",
    "james",
    "jeremiah",
    "jesus",
    "jewish",
    "jews",
    "john",
    "joseph",
    "joshua",
    "luke",
    "matthew",
    "moses",
    "paul",
    "peter",
    "pharaoh",
    "philippians",
    "psalm",
    "romans",
    "samuel",
}

HIGH_VALUE_WORDS = {
    "abide",
    "administration",
    "anointing",
    "authority",
    "baptism",
    "blood",
    "body",
    "bread",
    "calling",
    "church",
    "conscience",
    "consecration",
    "covenant",
    "cross",
    "dispensing",
    "economy",
    "elders",
    "element",
    "embodiment",
    "eternal",
    "fellowship",
    "flesh",
    "glory",
    "grace",
    "incarnation",
    "justification",
    "kingdom",
    "life",
    "mingled",
    "mystery",
    "organic",
    "priesthood",
    "redemption",
    "regeneration",
    "resurrection",
    "righteousness",
    "sanctification",
    "sanctuary",
    "sin",
    "spirit",
    "transformation",
    "vision",
}

HIGH_VALUE_ZH = {
    "经纶",
    "分赐",
    "调和",
    "生命",
    "那灵",
    "圣灵",
    "基督",
    "召会",
    "身体",
    "权柄",
    "膏油",
    "受膏",
    "救赎",
    "称义",
    "圣别",
    "复活",
    "重生",
    "变化",
    "国度",
    "异象",
    "十字架",
    "肉体",
    "祭司",
    "祭司职分",
    "圣所",
    "交通",
    "恩典",
    "荣耀",
    "公义",
    "赎罪",
    "犯罪",
    "无罪",
}

WEAK_NEEDS_MORE_REASON_MARKERS = {
    "evidence",
    "not enough",
    "needs more",
    "requires more",
    "insufficient",
    "one row",
    "this row",
    "too broad",
    "too narrow",
    "not stable",
    "unstable",
    "checked with more",
    "checked against more",
    "more occurrences",
    "depending on context",
    "keep for more",
    "candidate row",
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


def word_supported(word: str, evidence_en: str) -> bool:
    return bool(re.search(rf"(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])", evidence_en or "", re.IGNORECASE))


def meaning_parts(meaning: str) -> list[str]:
    return [part.strip() for part in re.split(r"[；;/,，、]", meaning or "") if part.strip()]


def meaning_supported(meaning: str, evidence_zh: str) -> bool:
    parts = meaning_parts(meaning)
    return bool(parts) and all(part in (evidence_zh or "") for part in parts)


def to_int(value: str) -> int:
    try:
        return int(float(value or 0))
    except ValueError:
        return 0


def row_base(row: dict[str, str]) -> dict[str, Any]:
    keys = [
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
    return {key: row.get(key, "") for key in keys}


def learning_risk(row: dict[str, str]) -> tuple[int, list[str]]:
    word = (row.get("word") or "").strip().lower()
    meaning = row.get("reviewed_meaning_zh_simp") or row.get("final_meaning_zh_simp") or ""
    reason: list[str] = []
    score = 0
    if word in HIGH_VALUE_WORDS:
        score += 4
        reason.append("high_value_word")
    if any(token in meaning for token in HIGH_VALUE_ZH):
        score += 3
        reason.append("high_value_chinese_meaning")
    if to_int(row.get("total_content_frequency", "")) >= 100 and (word in HIGH_VALUE_WORDS or any(token in meaning for token in HIGH_VALUE_ZH)):
        score += 2
        reason.append("high_frequency")
    if to_int(row.get("volume_count", "")) >= 5 and (word in HIGH_VALUE_WORDS or any(token in meaning for token in HIGH_VALUE_ZH)):
        score += 1
        reason.append("multi_volume")
    if word in PROPER_OR_REFERENCE:
        score -= 2
        reason.append("proper_or_reference")
    if not (row.get("final_reason") or "").strip():
        score += 3
        reason.append("missing_reason")
    return score, reason


def audit_frontend(rows: list[dict[str, str]]) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    records: list[dict[str, Any]] = []
    blockers: list[str] = []
    warnings: list[str] = []
    for row in rows:
        word = row.get("word", "")
        final_meaning = row.get("final_meaning_zh_simp", "")
        en_ok = word_supported(word, row.get("evidence_en", ""))
        zh_ok = meaning_supported(final_meaning, row.get("evidence_zh_simp", ""))
        anchor_hit = (row.get("pending_formal_anchor_hit") or "").lower() == "true"
        non_anchor = not anchor_hit
        issue_level = "pass"
        notes: list[str] = []
        if not final_meaning:
            blockers.append(f"{word}: missing final_meaning_zh_simp")
            issue_level = "blocker"
            notes.append("missing_final_meaning")
        if not en_ok:
            blockers.append(f"{word}: English evidence does not contain the word")
            issue_level = "blocker"
            notes.append("english_evidence_missing")
        if not zh_ok:
            blockers.append(f"{word}: Chinese evidence does not contain the final meaning")
            issue_level = "blocker"
            notes.append("chinese_evidence_missing")
        if non_anchor:
            warnings.append(f"{word}: frontend_ready is not from the historical anchor set; keep in final human spot check")
            if issue_level == "pass":
                issue_level = "manual_check"
            notes.append("non_anchor_frontend_ready")
        item = row_base(row)
        item.update(
            {
                "audit_level": issue_level,
                "english_evidence_ok": en_ok,
                "chinese_evidence_ok": zh_ok,
                "historical_anchor_hit": anchor_hit,
                "audit_notes": ";".join(notes),
            }
        )
        records.append(item)
    return records, blockers, warnings


def audit_needs_more(rows: list[dict[str, str]]) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    warnings: list[str] = []
    for row in rows:
        reason = (row.get("final_reason") or "").lower()
        notes: list[str] = []
        if not reason:
            warnings.append(f"{row.get('word')}: needs_more_evidence missing reason")
            notes.append("missing_reason")
        if not any(marker in reason for marker in WEAK_NEEDS_MORE_REASON_MARKERS):
            warnings.append(f"{row.get('word')}: needs_more_evidence reason may not explain missing evidence")
            notes.append("weak_missing_evidence_reason")
        item = row_base(row)
        item["audit_notes"] = ";".join(notes) if notes else "ok"
        records.append(item)
    return records, warnings


def audit_reject(rows: list[dict[str, str]]) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    warnings: list[str] = []
    for row in rows:
        notes: list[str] = []
        if not (row.get("final_reason") or "").strip():
            warnings.append(f"{row.get('word')}: reject missing reason")
            notes.append("missing_reason")
        if (row.get("final_meaning_zh_simp") or "").strip():
            warnings.append(f"{row.get('word')}: reject has final meaning; check whether it should be learning_only")
            notes.append("reject_has_final_meaning")
        item = row_base(row)
        item["audit_notes"] = ";".join(notes) if notes else "ok"
        records.append(item)
    return records, warnings


def select_learning_watchlist(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    scored: list[dict[str, Any]] = []
    for row in rows:
        score, reasons = learning_risk(row)
        if score >= 4:
            item = row_base(row)
            item["risk_score"] = score
            item["risk_reasons"] = ";".join(reasons)
            scored.append(item)
    scored.sort(key=lambda row: (-int(row["risk_score"]), -to_int(row.get("total_content_frequency", "")), row.get("word", "")))
    return scored


def select_random_learning_sample(rows: list[dict[str, str]], count: int = 100) -> list[dict[str, Any]]:
    rng = random.Random(RANDOM_SEED)
    sample = rows[:] if len(rows) <= count else rng.sample(rows, count)
    sample.sort(key=lambda row: int(row.get("source_index") or 0))
    return [row_base(row) for row in sample]


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    q = payload["quality"]
    lines = [
        "# Life-study Final Review Quality Audit",
        "",
        "This audit checks the completed no-write review package. It is not an import step.",
        "",
        "## Verdict",
        "",
        f"- Quality grade: `{q['quality_grade']}`",
        f"- Import decision: `{q['import_decision']}`",
        f"- Blocking issues: `{q['blocking_issue_count']}`",
        f"- Warnings: `{q['warning_count']}`",
        f"- Front-end rows audited: `{q['frontend_ready_rows']}`",
        f"- Learning-only watchlist rows: `{q['learning_only_watchlist_rows']}`",
        f"- Random learning-only sample rows: `{q['learning_only_random_sample_rows']}`",
        "",
        "## Counts",
        "",
    ]
    for category, count in q["decision_counts"].items():
        lines.append(f"- `{category}`: `{count}`")
    lines.extend(
        [
            "",
            "## Main Finding",
            "",
            "The completed review is structurally sound and conservative. It is safe to move into final human spot check, but not directly into import.",
            "",
            "The biggest quality risk is false negatives: some `learning_only` or `needs_more_evidence` rows may be useful enough for the front-end after more evidence is checked. This is a better failure mode than false positives in the reader.",
            "",
            "## Output Files",
            "",
        ]
    )
    for key, value in payload["outputs"].items():
        lines.append(f"- `{key}`: `{value}`")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    all_rows = read_csv(ALL_REVIEWED)
    if len(all_rows) != EXPECTED_SOURCE_ROWS:
        fail(f"expected {EXPECTED_SOURCE_ROWS} reviewed rows, got {len(all_rows)}")
    categories = Counter(row.get("final_category") for row in all_rows)
    invalid = sorted(category for category in categories if category not in VALID_CATEGORIES)
    if invalid:
        fail(f"invalid categories: {invalid}")

    frontend_rows = read_csv(FRONTEND_READY)
    learning_rows = read_csv(LEARNING_ONLY)
    needs_rows = read_csv(NEEDS_MORE)
    reject_rows = read_csv(REJECT)

    frontend_audit, frontend_blockers, frontend_warnings = audit_frontend(frontend_rows)
    needs_audit, needs_warnings = audit_needs_more(needs_rows)
    reject_audit, reject_warnings = audit_reject(reject_rows)
    learning_watchlist = select_learning_watchlist(learning_rows)
    learning_random = select_random_learning_sample(learning_rows)

    warnings = frontend_warnings + needs_warnings + reject_warnings
    blocking_count = len(frontend_blockers)
    if blocking_count:
        quality_grade = "blocked"
        import_decision = "do_not_import"
    elif len(learning_watchlist) > 80:
        quality_grade = "B+ conservative_pass"
        import_decision = "human_spot_check_required_before_import"
    else:
        quality_grade = "A- conservative_pass"
        import_decision = "human_spot_check_required_before_import"

    common_fields = [
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
    write_csv(
        AUDIT_FRONTEND_CSV,
        frontend_audit,
        common_fields + ["audit_level", "english_evidence_ok", "chinese_evidence_ok", "historical_anchor_hit", "audit_notes"],
    )
    write_csv(AUDIT_LEARNING_WATCHLIST_CSV, learning_watchlist, common_fields + ["risk_score", "risk_reasons"])
    write_csv(AUDIT_LEARNING_RANDOM_CSV, learning_random, common_fields)
    write_csv(AUDIT_NEEDS_MORE_CSV, needs_audit, common_fields + ["audit_notes"])
    write_csv(AUDIT_REJECT_CSV, reject_audit, common_fields + ["audit_notes"])

    payload = {
        "schema": "sentence_reader.lifestudy_final_review_quality_audit.v1",
        "generated_at": now_iso(),
        "quality": {
            "quality_grade": quality_grade,
            "import_decision": import_decision,
            "blocking_issue_count": blocking_count,
            "blocking_issues": frontend_blockers,
            "warning_count": len(warnings),
            "warnings": warnings[:100],
            "decision_counts": dict(categories),
            "frontend_ready_rows": len(frontend_rows),
            "frontend_non_anchor_rows": sum(1 for row in frontend_audit if row["historical_anchor_hit"] is False),
            "learning_only_rows": len(learning_rows),
            "learning_only_watchlist_rows": len(learning_watchlist),
            "learning_only_random_sample_rows": len(learning_random),
            "needs_more_evidence_rows": len(needs_rows),
            "reject_rows": len(reject_rows),
            "random_seed": RANDOM_SEED,
        },
        "outputs": {
            "frontend_audit_csv": str(AUDIT_FRONTEND_CSV),
            "learning_only_watchlist_csv": str(AUDIT_LEARNING_WATCHLIST_CSV),
            "learning_only_random_sample_csv": str(AUDIT_LEARNING_RANDOM_CSV),
            "needs_more_evidence_audit_csv": str(AUDIT_NEEDS_MORE_CSV),
            "reject_audit_csv": str(AUDIT_REJECT_CSV),
            "summary_json": str(AUDIT_SUMMARY_JSON),
            "summary_md": str(AUDIT_SUMMARY_MD),
        },
    }
    AUDIT_SUMMARY_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(AUDIT_SUMMARY_MD, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
