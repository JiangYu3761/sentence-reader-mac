#!/usr/bin/env python3
from __future__ import annotations

import argparse
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
CALIBRATION_REVIEWED = OUTPUT_DIR / "calibration_001_reviewed.csv"

PENDING_FORMAL_FILES = [
    (ROOT / "reports" / "lifestudy_vocab_pipeline" / "01_Genesis-120-pages-1-1255-importable.csv", "term", "suggested_meaning_zh_simp"),
    (CORPUS_DIR / "lifestudy_vocab_v1_importable.csv", "term", "candidate_meaning_zh_simp"),
    (CORPUS_DIR / "lifestudy_frontend_candidate_adjudication_v2_ready_for_dry_run.csv", "word", "final_meaning_zh_simp"),
    (CORPUS_DIR / "lifestudy_needs_review_frontend_ready_for_dry_run.csv", "word", "final_meaning_zh_simp"),
]

EXPECTED_SOURCE_ROWS = 4102
EXPECTED_CALIBRATION_ROWS = 100
DEFAULT_BATCH_SIZE = 250

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

GENERIC_LOW_VALUE = {
    "always",
    "anyone",
    "ask",
    "begin",
    "beginning",
    "best",
    "bring",
    "cause",
    "clear",
    "complete",
    "condition",
    "consider",
    "continues",
    "daily",
    "different",
    "easy",
    "end",
    "entire",
    "eventually",
    "example",
    "fact",
    "family",
    "find",
    "firstly",
    "follow",
    "four",
    "genuine",
    "happy",
    "hear",
    "hence",
    "history",
    "husband",
    "important",
    "immediately",
    "including",
    "instead",
    "job",
    "learn",
    "letters",
    "long",
    "members",
    "mind",
    "name",
    "nations",
    "necessary",
    "nevertheless",
    "never",
    "number",
    "often",
    "open",
    "parents",
    "part",
    "past",
    "period",
    "perhaps",
    "picture",
    "poor",
    "possible",
    "present",
    "problem",
    "question",
    "rather",
    "reason",
    "record",
    "regarding",
    "remember",
    "result",
    "right",
    "section",
    "seems",
    "seven",
    "simply",
    "since",
    "small",
    "someone",
    "stand",
    "step",
    "strong",
    "study",
    "suffer",
    "suppose",
    "table",
    "taking",
    "teachers",
    "term",
    "themselves",
    "third",
    "think",
    "thought",
    "three",
    "today",
    "together",
    "understand",
    "until",
    "unto",
    "use",
    "want",
    "whereas",
    "whether",
    "whenever",
    "whole",
    "wife",
    "woman",
    "wonderful",
    "working",
    "writing",
    "wrong",
    "yet",
    "yourself",
}

NEEDS_MORE_EVIDENCE = {
    "abide": "May be a biblical term, but this row alone is not enough to define front-end priority meaning.",
    "administration": "Could be a domain term, but requires more Life-study evidence before front-end use.",
    "angels": "Biblical term; keep for more evidence instead of showing from one row.",
    "ark": "Biblical typology term; needs more evidence to choose stable front-end meaning.",
    "blood": "Biblical term with strong potential, but it needs more evidence for front-end glossary treatment.",
    "bread": "Biblical/typological term; this row is not enough for a stable front-end entry.",
    "burden": "May be a ministry-context term; needs more evidence for stable meaning.",
    "city": "Could be biblical typology in some contexts, but this row is too broad.",
    "concept": "May matter in Life-study reading, but it is not a front-end glossary term from one row.",
    "conscience": "Spiritual term; needs more evidence before front-end priority use.",
    "contact": "Can be spiritually meaningful, but current evidence is not enough for front-end use.",
    "contrary": "Potentially useful learning word, not enough for front-end priority.",
    "covenant": "Biblical term; candidate meaning 盟约 needs more checks against Life-study usage.",
    "cross": "Biblical term; needs more evidence before final front-end treatment.",
    "culture": "May be specific in some Life-study contexts, but current row is insufficient.",
    "dead": "Biblical/spiritual term, but needs more evidence for stable front-end meaning.",
    "deep": "Potentially meaningful in Genesis context, but one row is not enough.",
    "devil": "Biblical term; keep for more evidence before front-end entry.",
    "drink": "Could be typological, but candidate row is insufficient.",
    "dwell": "Potential spiritual term; needs more evidence for stable front-end meaning.",
    "dwelling": "Potential typology term; needs more evidence.",
    "elders": "Church term; needs more evidence before front-end use.",
    "embodiment": "Likely domain term, but current candidate should be checked with more evidence.",
    "enemy": "Biblical/spiritual term; needs more evidence.",
    "epistle": "Bible-genre term; not enough evidence for front-end priority.",
    "essence": "Potentially theological; needs more evidence.",
    "eternity": "Theological term; needs more evidence for stable front-end meaning.",
    "fall": "Potential spiritual term; needs more evidence.",
    "fallen": "Spiritual term; needs more evidence for stable priority meaning.",
    "feast": "Candidate meaning is likely not stable enough; needs more evidence.",
    "fire": "Biblical symbol; needs more evidence.",
    "food": "Typological/spiritual term; needs more evidence.",
    "foundation": "Could be theological/building term, but current candidate is unstable.",
    "fruit": "Potential typology/life term; needs more evidence.",
    "fulfillment": "Potential biblical term; needs more evidence.",
    "fullness": "Likely domain term, but needs more evidence for stable meaning.",
    "gifts": "Church/spiritual term; needs more evidence.",
    "gold": "Biblical material/typology term; candidate meaning may be unstable.",
    "government": "Potential domain term; needs more evidence.",
    "growth": "Life term; needs more evidence for front-end treatment.",
    "hidden": "Potentially useful, but not front-end-stable from one row.",
    "holiness": "Spiritual term; needs more evidence before front-end use.",
    "humanity": "Theological term; needs more evidence.",
    "idols": "Biblical term; needs more evidence.",
    "incarnation": "Theological term; needs more evidence.",
    "incense": "Typology term; needs more evidence.",
    "intention": "May be meaningful but not front-end-stable from one row.",
    "judge": "Could be person/role/book ambiguity; needs more evidence.",
    "lamb": "Biblical typology term; needs more evidence for stable front-end meaning.",
    "lampstand": "Typology term; needs more evidence.",
    "line": "Could be domain-specific as line/route, but current row is not enough.",
    "lust": "Spiritual/moral term; needs more evidence.",
    "material": "May be typology/building material, but current row is insufficient.",
    "meaning": "Too broad but sometimes important; keep for more evidence, not front-end.",
    "mercy": "Spiritual term; needs more evidence.",
    "mystery": "Theological term; needs more evidence.",
    "ordinances": "Biblical term; needs more evidence.",
    "passover": "Biblical feast term; needs more evidence before front-end use.",
    "pillar": "Typology/building term; needs more evidence.",
    "possession": "Could be biblical inheritance/possession; needs more evidence.",
    "practical": "May be ministry-context term; needs more evidence.",
    "practice": "May be ministry-context term; needs more evidence.",
    "presence": "Spiritual term; needs more evidence.",
    "processed": "Likely Life-study domain term, but needs stable evidence before front-end use.",
    "prophets": "Biblical term; needs more evidence.",
    "pure": "Spiritual descriptor; needs more evidence.",
    "race": "Could be theological/humanity context; needs more evidence.",
    "religion": "Important contrast term in Life-study, but needs more evidence.",
    "rest": "Biblical/spiritual term; needs more evidence.",
    "sabbath": "Biblical term; needs more evidence.",
    "sacrifice": "Biblical term with multiple Chinese possibilities; needs more evidence.",
    "satisfaction": "Potential spiritual term; needs more evidence.",
    "seed": "Biblical typology term; needs more evidence.",
    "self": "Spiritual/experiential term; needs more evidence.",
    "sense": "May be common or conceptual; needs more evidence.",
    "serve": "Can mean serve/supply in ministry contexts; needs more evidence.",
    "servant": "Biblical/service term; needs more evidence.",
    "shortage": "Potential spiritual-culture context, but not front-end-stable from one row.",
    "significance": "Too broad but potentially useful; needs more evidence.",
    "sinful": "Spiritual term; needs more evidence.",
    "sinners": "Biblical/spiritual term; needs more evidence.",
    "soul": "Candidate meaning 人 is too broad; needs more evidence for soul-related meaning.",
    "source": "Potentially important, but broad; needs more evidence.",
    "standard": "Potential ministry-context term; needs more evidence.",
    "stone": "Biblical typology term; needs more evidence.",
    "strength": "Potential spiritual term; needs more evidence.",
    "suffering": "Spiritual/experiential term; needs more evidence.",
    "transformed": "Likely domain term related to transformation; needs more evidence.",
    "tribulation": "Biblical/spiritual term; needs more evidence.",
    "true": "Could be important but too broad from one row.",
    "virtues": "Spiritual/moral term; needs more evidence.",
    "water": "Biblical symbol; needs more evidence.",
    "wisdom": "Biblical/spiritual term; needs more evidence.",
    "witness": "Biblical/ministry term; needs more evidence.",
}

OBVIOUS_REJECTS = {
    "ephesians": "Candidate meaning 新约 is not the book name Ephesians; reject this candidate row.",
    "philippians": "Candidate meaning is truncated or misaligned; reject this candidate row.",
    "pharisees": "Candidate meaning 人 is too generic and does not preserve Pharisees.",
    "anointed": "Candidate meaning 基督 loses the term-level meaning anointed; reject this row rather than approving a wrong meaning.",
    "divinity": "Candidate meaning 神 is too broad for divinity and likely misaligned.",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def as_int(value: Any) -> int:
    try:
        return int(float(str(value or "0")))
    except ValueError:
        return 0


def bool_value(value: Any) -> bool:
    return str(value).strip().lower() == "true"


def word_supported(word: str, evidence_en: str) -> bool:
    return bool(word and re.search(rf"(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])", evidence_en or "", re.IGNORECASE))


def meaning_parts(meaning: str) -> list[str]:
    return [part.strip() for part in re.split(r"[；;/,，、]", meaning or "") if part.strip()]


def meaning_supported(meaning: str, evidence_zh: str) -> bool:
    parts = meaning_parts(meaning)
    return bool(parts) and all(part in (evidence_zh or "") for part in parts)


def load_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise SystemExit(f"missing CSV: {path}")
    return list(csv.DictReader(path.open(encoding="utf-8-sig")))


def load_source_rows() -> list[dict[str, str]]:
    rows = load_csv(SOURCE_CSV)
    if len(rows) != EXPECTED_SOURCE_ROWS:
        raise SystemExit(f"unexpected source row count: {len(rows)} != {EXPECTED_SOURCE_ROWS}")
    return rows


def load_calibration_words() -> set[str]:
    rows = load_csv(CALIBRATION_REVIEWED)
    if len(rows) != EXPECTED_CALIBRATION_ROWS:
        raise SystemExit(f"unexpected calibration row count: {len(rows)} != {EXPECTED_CALIBRATION_ROWS}")
    return {(row.get("word") or "").strip().lower() for row in rows}


def load_pending_formal_terms() -> dict[str, dict[str, str]]:
    terms: dict[str, dict[str, str]] = {}
    for path, term_field, meaning_field in PENDING_FORMAL_FILES:
        for row in load_csv(path):
            term = (row.get(term_field) or "").strip().lower()
            meaning = (row.get(meaning_field) or "").strip()
            if term and meaning:
                terms[term] = {"meaning": meaning, "source_file": str(path.relative_to(ROOT))}
    if len(terms) != 100:
        raise SystemExit(f"unexpected pending formal unique term count: {len(terms)} != 100")
    return terms


def batch_rows(batch_number: int, batch_size: int) -> list[tuple[int, dict[str, str]]]:
    if batch_number < 1:
        raise SystemExit("batch number must be >= 1")
    excluded = load_calibration_words()
    remaining = [
        (index + 1, row)
        for index, row in enumerate(load_source_rows())
        if (row.get("word") or "").strip().lower() not in excluded
    ]
    start = (batch_number - 1) * batch_size
    end = start + batch_size
    selected = remaining[start:end]
    if not selected:
        raise SystemExit(f"batch {batch_number:03d} is out of range")
    if len(selected) != batch_size and end < len(remaining):
        raise SystemExit(f"batch {batch_number:03d} expected {batch_size} rows, got {len(selected)}")
    return selected


def propose_category(row: dict[str, str], pending: dict[str, str]) -> tuple[str, str, str]:
    word = (row.get("word") or "").strip().lower()
    candidate = (row.get("reviewed_meaning_zh_simp") or "").strip()
    evidence_en = row.get("evidence_en") or ""
    evidence_zh = row.get("evidence_zh_simp") or ""
    known_meaning = pending.get("meaning", "")

    if word in OBVIOUS_REJECTS:
        return "reject", "", OBVIOUS_REJECTS[word]
    if known_meaning:
        if word_supported(word, evidence_en) and meaning_supported(known_meaning, evidence_zh):
            return "frontend_ready", known_meaning, "Existing pending formal term is supported by this aligned evidence; historical anchor wins."
        return "needs_more_evidence", "", "Existing pending formal term, but this batch row does not support the anchored meaning directly."
    if not word_supported(word, evidence_en) or not meaning_supported(candidate, evidence_zh):
        return "reject", "", "Same-record English or Chinese evidence does not support the candidate meaning."
    if word in NEEDS_MORE_EVIDENCE:
        return "needs_more_evidence", "", NEEDS_MORE_EVIDENCE[word]
    if word in PROPER_OR_REFERENCE:
        return "learning_only", candidate, "Proper name, Bible book, place, or reference term; keep out of front-end glossary."
    if word in GENERIC_LOW_VALUE:
        return "learning_only", candidate, "Generic high-frequency word; useful for learning but would annoy in front-end lookup."
    return "learning_only", candidate, "Evidence-supported learning word, but not strong enough for front-end Life-study glossary priority."


def review_row(batch_id: str, source_index: int, row: dict[str, str], pending_terms: dict[str, dict[str, str]]) -> dict[str, Any]:
    word = (row.get("word") or "").strip().lower()
    pending = pending_terms.get(word, {})
    final_category, final_meaning, final_reason = propose_category(row, pending)
    candidate = (row.get("reviewed_meaning_zh_simp") or "").strip()
    evidence_en = row.get("evidence_en") or ""
    evidence_zh = row.get("evidence_zh_simp") or ""
    return {
        "batch_id": batch_id,
        "source_index": source_index,
        "word": word,
        "lemma": row.get("lemma") or "",
        "reviewed_meaning_zh_simp": candidate,
        "known_pending_formal_meaning_zh_simp": pending.get("meaning", ""),
        "known_pending_formal_source": pending.get("source_file", ""),
        "suggested_final_meaning_zh_simp": pending.get("meaning", candidate if final_category == "learning_only" else ""),
        "proposed_category": final_category,
        "proposed_reason": final_reason,
        "final_category": final_category,
        "final_meaning_zh_simp": final_meaning,
        "final_reason": final_reason,
        "needs_second_pass": final_category == "frontend_ready",
        "pending_formal_anchor_hit": bool(pending),
        "pending_formal_meaning_mismatch": bool(pending and pending.get("meaning") != candidate),
        "same_record_english_hit": word_supported(word, evidence_en),
        "same_record_chinese_hit": meaning_supported(final_meaning or candidate, evidence_zh),
        "front_end_candidate_ready": final_category == "frontend_ready",
        "front_end_import_ready": False,
        "database_write_performed": False,
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
    fieldnames = list(rows[0]) if rows else []
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: csv_value(row.get(field)) for field in fieldnames})


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        f"# Life-study Final Review {payload['batch_id']} Reviewed",
        "",
        "This batch is reviewed without writing PostgreSQL and without marking rows as final import-ready.",
        "",
        f"- Reviewed rows: `{payload['quality']['reviewed_rows']}`",
        f"- Front-end ready: `{payload['quality']['decision_counts'].get('frontend_ready', 0)}`",
        f"- Learning only: `{payload['quality']['decision_counts'].get('learning_only', 0)}`",
        f"- Needs more evidence: `{payload['quality']['decision_counts'].get('needs_more_evidence', 0)}`",
        f"- Reject: `{payload['quality']['decision_counts'].get('reject', 0)}`",
        f"- Database write performed: `{payload['database_write_performed']}`",
        "",
        "## Front-end Ready",
        "",
    ]
    for row in payload["items"]:
        if row["final_category"] == "frontend_ready":
            lines.extend(
                [
                    f"### {row['word']} -> {row['final_meaning_zh_simp']}",
                    "",
                    f"- Reason: {row['final_reason']}",
                    f"- Source: `{row['source_volume']} p{row['source_page']}`",
                    f"- EN: {row['evidence_en']}",
                    f"- ZH: {row['evidence_zh_simp']}",
                    "",
                ]
            )
    lines.extend(["", "## Needs More Evidence", ""])
    for row in payload["items"]:
        if row["final_category"] == "needs_more_evidence":
            lines.extend(
                [
                    f"### {row['word']} -> {row['reviewed_meaning_zh_simp']}",
                    "",
                    f"- Reason: {row['final_reason']}",
                    f"- Source: `{row['source_volume']} p{row['source_page']}`",
                    "",
                ]
            )
    lines.extend(["", "## Reject", ""])
    for row in payload["items"]:
        if row["final_category"] == "reject":
            lines.extend(
                [
                    f"### {row['word']} -> {row['reviewed_meaning_zh_simp']}",
                    "",
                    f"- Reason: {row['final_reason']}",
                    f"- Source: `{row['source_volume']} p{row['source_page']}`",
                    f"- EN: {row['evidence_en']}",
                    f"- ZH: {row['evidence_zh_simp']}",
                    "",
                ]
            )
    path.write_text("\n".join(lines), encoding="utf-8")


def build_payload(batch_number: int, batch_size: int) -> dict[str, Any]:
    batch_id = f"batch_{batch_number:03d}"
    pending_terms = load_pending_formal_terms()
    selected = batch_rows(batch_number, batch_size)
    rows = [review_row(batch_id, source_index, row, pending_terms) for source_index, row in selected]
    decision_counts = dict(Counter(row["final_category"] for row in rows))
    return {
        "schema": "sentence_reader.lifestudy_final_review_batch.v1",
        "generated_at": now_iso(),
        "batch_id": batch_id,
        "batch_number": batch_number,
        "batch_size": len(rows),
        "requested_batch_size": batch_size,
        "source_csv": str(SOURCE_CSV),
        "excluded_calibration_csv": str(CALIBRATION_REVIEWED),
        "database_write_performed": False,
        "front_end_import_ready": False,
        "policy": "250_row_batch_review_no_db_write_no_import",
        "quality": {
            "reviewed_rows": len(rows),
            "blank_final_category_rows": sum(1 for row in rows if not row["final_category"]),
            "blank_final_reason_rows": sum(1 for row in rows if not row["final_reason"]),
            "front_end_import_ready_count": sum(1 for row in rows if row["front_end_import_ready"]),
            "database_write_count": sum(1 for row in rows if row["database_write_performed"]),
            "frontend_ready_rows": decision_counts.get("frontend_ready", 0),
            "needs_more_evidence_rows": decision_counts.get("needs_more_evidence", 0),
            "learning_only_rows": decision_counts.get("learning_only", 0),
            "reject_rows": decision_counts.get("reject", 0),
            "pending_formal_anchor_rows": sum(1 for row in rows if row["pending_formal_anchor_hit"]),
            "pending_formal_meaning_mismatch_rows": sum(1 for row in rows if row["pending_formal_meaning_mismatch"]),
            "decision_counts": decision_counts,
        },
        "items": rows,
    }


def write_outputs(payload: dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    batch_id = payload["batch_id"]
    rows = payload["items"]
    input_fields = [
        "word",
        "lemma",
        "reviewed_meaning_zh_simp",
        "known_pending_formal_meaning_zh_simp",
        "suggested_final_meaning_zh_simp",
        "evidence_en",
        "evidence_zh_simp",
        "source_volume",
        "source_page",
        "batch_id",
        "database_write_performed",
        "front_end_import_ready",
    ]
    write_csv(OUTPUT_DIR / f"{batch_id}_input.csv", [{field: row.get(field, "") for field in input_fields} for row in rows])
    write_csv(OUTPUT_DIR / f"{batch_id}_review_template.csv", rows)
    write_csv(OUTPUT_DIR / f"{batch_id}_reviewed.csv", rows)
    write_csv(OUTPUT_DIR / f"{batch_id}_frontend_ready.csv", [row for row in rows if row["final_category"] == "frontend_ready"])
    write_csv(OUTPUT_DIR / f"{batch_id}_learning_only.csv", [row for row in rows if row["final_category"] == "learning_only"])
    write_csv(OUTPUT_DIR / f"{batch_id}_needs_more_evidence.csv", [row for row in rows if row["final_category"] == "needs_more_evidence"])
    write_csv(OUTPUT_DIR / f"{batch_id}_reject.csv", [row for row in rows if row["final_category"] == "reject"])
    summary = {k: v for k, v in payload.items() if k != "items"}
    summary["outputs"] = {
        "input_csv": str(OUTPUT_DIR / f"{batch_id}_input.csv"),
        "review_template_csv": str(OUTPUT_DIR / f"{batch_id}_review_template.csv"),
        "reviewed_csv": str(OUTPUT_DIR / f"{batch_id}_reviewed.csv"),
        "frontend_ready_csv": str(OUTPUT_DIR / f"{batch_id}_frontend_ready.csv"),
        "learning_only_csv": str(OUTPUT_DIR / f"{batch_id}_learning_only.csv"),
        "needs_more_evidence_csv": str(OUTPUT_DIR / f"{batch_id}_needs_more_evidence.csv"),
        "reject_csv": str(OUTPUT_DIR / f"{batch_id}_reject.csv"),
        "summary_json": str(OUTPUT_DIR / f"{batch_id}_reviewed_summary.json"),
        "markdown": str(OUTPUT_DIR / f"{batch_id}_reviewed.md"),
    }
    (OUTPUT_DIR / f"{batch_id}_reviewed_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_markdown(OUTPUT_DIR / f"{batch_id}_reviewed.md", payload)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    args = parser.parse_args()
    payload = build_payload(args.batch, args.batch_size)
    write_outputs(payload)
    print(json.dumps({k: v for k, v in payload.items() if k != "items"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
