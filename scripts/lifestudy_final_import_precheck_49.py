#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "reports" / "lifestudy_vocab_final_review"
FRONTEND_AUDIT = OUTPUT_DIR / "quality_audit_frontend_ready.csv"
WATCHLIST = OUTPUT_DIR / "quality_audit_learning_only_watchlist.csv"

PRECHECK_REVIEWED = OUTPUT_DIR / "final_import_precheck_49_reviewed.csv"
PRECHECK_CANDIDATES = OUTPUT_DIR / "final_import_candidates_after_49_review.csv"
PRECHECK_REJECTED = OUTPUT_DIR / "final_import_precheck_49_not_imported.csv"
PRECHECK_SUMMARY_JSON = OUTPUT_DIR / "final_import_precheck_49_summary.json"
PRECHECK_SUMMARY_MD = OUTPUT_DIR / "final_import_precheck_49_summary.md"

EXPECTED_FRONTEND_ROWS = 26
EXPECTED_WATCHLIST_ROWS = 23

PROMOTE_FROM_WATCHLIST = {
    "propitiation": {
        "meaning": "遮罪",
        "reason": "Promote with corrected aligned meaning. The evidence maps propitiation to 遮罪, not the broader candidate 赎罪.",
    },
    "priestly": {
        "meaning": "祭司的",
        "reason": "Promote as a stable ministry/Bible adjective; evidence directly supports 祭司的 and it complements priesthood.",
    },
    "fleshly": {
        "meaning": "属肉体的",
        "reason": "Promote as a stable spiritual adjective; evidence directly supports 属肉体的 and it is frequent across volumes.",
    },
    "comforter": {
        "meaning": "保惠师",
        "reason": "Promote with corrected aligned meaning. The evidence identifies Comforter as 保惠师; 圣灵 is context, not the term meaning.",
    },
    "atonement": {
        "meaning": "赎罪",
        "reason": "Promote as a stable biblical term; evidence directly supports 赎罪 in 赎罪节.",
    },
    "sinless": {
        "meaning": "无罪的",
        "reason": "Promote as a stable theological adjective; evidence directly supports 无罪的 in Christ/life context.",
    },
    "zoe": {
        "meaning": "生命",
        "reason": "Promote as a high-value Life-study term; evidence explains zoe as the eternal divine life and directly contains 生命.",
    },
}

KEEP_OUT_REASONS = {
    "christianity": "Keep out of first import. Correct and frequent, but broad and not a priority front-end Life-study glossary term.",
    "sinned": "Keep out of first import. Inflected common verb; sin is already covered.",
    "traffic": "Keep out of first import. Evidence is traffic signs, not the ministry sense of fellowship/交通.",
    "sinning": "Keep out of first import. Gerund/common verb; sin is already covered.",
    "honorable": "Keep out of first import. Broad adjective and the candidate meaning is not stable enough for front-end use.",
    "mediator": "Keep out of first import. Current candidate meaning 基督 is wrong for this evidence; needs separate evidence for 中保/中间人.",
    "bodily": "Keep out of first import. Correct but broad adjective; not a priority Life-study front-end term.",
    "communication": "Keep out of first import. Correct as 交通 in this row, but communication is not stable enough as the fellowship term.",
    "lifeless": "Keep out of first import. Correct adjective but low front-end glossary value.",
    "innocent": "Keep out of first import. Correct adjective but broad and not a priority glossary term.",
    "resurrect": "Keep out of first import. Correct verb, but first import should prefer stable noun/domain entries.",
    "crime": "Keep out of first import. Common legal word, not a front-end Life-study glossary term.",
    "innocence": "Keep out of first import. Correct noun but broad and not a priority glossary term.",
    "risen": "Keep out of first import. Correct participle, but resurrection-related base terms should be handled separately.",
    "fellowshipped": "Keep out of first import. Inflected verb; fellowship/交通 needs its own stable entry, not this form.",
    "easter": "Keep out of first import. Historical/cultural reference, not a Life-study glossary priority.",
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


def base_output(row: dict[str, str]) -> dict[str, Any]:
    keys = [
        "review_origin",
        "batch_id",
        "source_index",
        "word",
        "lemma",
        "reviewed_meaning_zh_simp",
        "final_category",
        "final_meaning_zh_simp",
        "total_content_frequency",
        "volume_count",
        "source_volume",
        "source_page",
        "evidence_en",
        "evidence_zh_simp",
    ]
    return {key: row.get(key, "") for key in keys}


def confirm_frontend_rows(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    reviewed: list[dict[str, Any]] = []
    for row in rows:
        word = row["word"].strip().lower()
        meaning = row["final_meaning_zh_simp"].strip()
        en_ok = word_supported(word, row.get("evidence_en", ""))
        zh_ok = meaning_supported(meaning, row.get("evidence_zh_simp", ""))
        if not en_ok or not zh_ok:
            decision = "downgrade_needs_manual_fix"
            candidate = False
            reason = "Downgraded because aligned English/Chinese evidence did not support the import meaning."
        else:
            decision = "confirmed_import_candidate"
            candidate = True
            reason = "Confirmed. Existing front-end candidate has aligned English and Chinese evidence."
        item = base_output(row)
        item.update(
            {
                "precheck_group": "existing_frontend_ready",
                "precheck_decision": decision,
                "precheck_import_candidate": candidate,
                "precheck_final_meaning_zh_simp": meaning if candidate else "",
                "precheck_reason": reason,
                "database_write_performed": False,
                "front_end_import_ready": False,
            }
        )
        reviewed.append(item)
    return reviewed


def review_watchlist(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    reviewed: list[dict[str, Any]] = []
    for row in rows:
        word = row["word"].strip().lower()
        item = base_output(row)
        if word in PROMOTE_FROM_WATCHLIST:
            meaning = PROMOTE_FROM_WATCHLIST[word]["meaning"]
            en_ok = word_supported(word, row.get("evidence_en", ""))
            zh_ok = meaning_supported(meaning, row.get("evidence_zh_simp", ""))
            if not en_ok or not zh_ok:
                decision = "hold_for_evidence_fix"
                candidate = False
                reason = f"Promotion blocked because evidence does not support corrected meaning {meaning}."
                meaning = ""
            else:
                decision = "promoted_import_candidate"
                candidate = True
                reason = PROMOTE_FROM_WATCHLIST[word]["reason"]
        else:
            meaning = ""
            decision = "keep_out_first_import"
            candidate = False
            reason = KEEP_OUT_REASONS.get(word, "Keep out of first import. Not selected as a stable front-end candidate.")
        item.update(
            {
                "precheck_group": "learning_only_watchlist",
                "precheck_decision": decision,
                "precheck_import_candidate": candidate,
                "precheck_final_meaning_zh_simp": meaning,
                "precheck_reason": reason,
                "database_write_performed": False,
                "front_end_import_ready": False,
            }
        )
        reviewed.append(item)
    return reviewed


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    q = payload["quality"]
    lines = [
        "# Life-study Final Import Precheck 49",
        "",
        "This is a no-write import precheck for the 26 existing front-end candidates plus the 23 learning-only watchlist rows.",
        "",
        "## Result",
        "",
        f"- Reviewed rows: `{q['reviewed_rows']}`",
        f"- Existing front-end confirmed: `{q['existing_frontend_confirmed']}`",
        f"- Watchlist promoted: `{q['watchlist_promoted']}`",
        f"- First-import candidates after precheck: `{q['first_import_candidate_count']}`",
        f"- Not imported in first pass: `{q['not_imported_count']}`",
        f"- Database writes: `{q['database_write_count']}`",
        f"- Front-end import-ready flags: `{q['front_end_import_ready_count']}`",
        "",
        "## Watchlist Promotions",
        "",
    ]
    for row in payload["watchlist_promotions"]:
        lines.append(f"- `{row['word']}` -> `{row['precheck_final_meaning_zh_simp']}`")
    lines.extend(["", "## Outputs", ""])
    for key, value in payload["outputs"].items():
        lines.append(f"- `{key}`: `{value}`")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    frontend_rows = read_csv(FRONTEND_AUDIT)
    watchlist_rows = read_csv(WATCHLIST)
    if len(frontend_rows) != EXPECTED_FRONTEND_ROWS:
        fail(f"frontend row count mismatch: {len(frontend_rows)} != {EXPECTED_FRONTEND_ROWS}")
    if len(watchlist_rows) != EXPECTED_WATCHLIST_ROWS:
        fail(f"watchlist row count mismatch: {len(watchlist_rows)} != {EXPECTED_WATCHLIST_ROWS}")

    reviewed = confirm_frontend_rows(frontend_rows) + review_watchlist(watchlist_rows)
    candidates = [row for row in reviewed if row["precheck_import_candidate"] is True]
    not_imported = [row for row in reviewed if row["precheck_import_candidate"] is not True]
    promoted = [row for row in reviewed if row["precheck_decision"] == "promoted_import_candidate"]

    fields = [
        "precheck_group",
        "precheck_decision",
        "precheck_import_candidate",
        "precheck_final_meaning_zh_simp",
        "precheck_reason",
        "review_origin",
        "batch_id",
        "source_index",
        "word",
        "lemma",
        "reviewed_meaning_zh_simp",
        "final_category",
        "final_meaning_zh_simp",
        "total_content_frequency",
        "volume_count",
        "source_volume",
        "source_page",
        "evidence_en",
        "evidence_zh_simp",
        "database_write_performed",
        "front_end_import_ready",
    ]
    write_csv(PRECHECK_REVIEWED, reviewed, fields)
    write_csv(PRECHECK_CANDIDATES, candidates, fields)
    write_csv(PRECHECK_REJECTED, not_imported, fields)

    payload = {
        "schema": "sentence_reader.lifestudy_final_import_precheck_49.v1",
        "generated_at": now_iso(),
        "quality": {
            "reviewed_rows": len(reviewed),
            "existing_frontend_confirmed": sum(1 for row in reviewed if row["precheck_decision"] == "confirmed_import_candidate"),
            "watchlist_promoted": len(promoted),
            "first_import_candidate_count": len(candidates),
            "not_imported_count": len(not_imported),
            "database_write_count": sum(1 for row in reviewed if row["database_write_performed"] is not False),
            "front_end_import_ready_count": sum(1 for row in reviewed if row["front_end_import_ready"] is not False),
        },
        "watchlist_promotions": [
            {
                "word": row["word"],
                "precheck_final_meaning_zh_simp": row["precheck_final_meaning_zh_simp"],
                "precheck_reason": row["precheck_reason"],
            }
            for row in promoted
        ],
        "outputs": {
            "reviewed_csv": str(PRECHECK_REVIEWED),
            "first_import_candidates_csv": str(PRECHECK_CANDIDATES),
            "not_imported_csv": str(PRECHECK_REJECTED),
            "summary_json": str(PRECHECK_SUMMARY_JSON),
            "summary_md": str(PRECHECK_SUMMARY_MD),
        },
        "database_write_performed": False,
        "front_end_import_ready": False,
    }
    PRECHECK_SUMMARY_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(PRECHECK_SUMMARY_MD, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
