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
TEMPLATE = OUTPUT_DIR / "calibration_001_review_template.csv"

EXPECTED_ROWS = 100

# These terms look potentially useful for front-end lookup, but this single
# calibration evidence row is not enough to lock the final front-end meaning.
NEEDS_MORE_EVIDENCE: dict[str, str] = {
    "age": "Life-study often uses age with a dispensational sense; this row supports 时代 but should be checked across more evidence before front-end use.",
    "cross": "十字架 is likely useful, but it should be checked against more occurrences before becoming a Life-study priority entry.",
    "flesh": "肉体 is a biblical/spiritual term with possible nuance; keep for more evidence instead of approving from one row.",
    "nourishment": "The evidence supports 营养 in a Christ-as-food context, but more rows should confirm front-end usefulness.",
    "power": "The candidate meaning 势力 is too narrow; power may correspond to 能力 or 权柄/权势 depending on context.",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_rows() -> list[dict[str, str]]:
    if not TEMPLATE.exists():
        raise SystemExit(f"missing calibration template: {TEMPLATE}")
    rows = list(csv.DictReader(TEMPLATE.open(encoding="utf-8-sig")))
    if len(rows) != EXPECTED_ROWS:
        raise SystemExit(f"unexpected calibration rows: {len(rows)} != {EXPECTED_ROWS}")
    return rows


def bool_value(value: Any) -> bool:
    return str(value).strip().lower() == "true"


def adjudicate(row: dict[str, str]) -> dict[str, Any]:
    word = (row.get("word") or "").strip().lower()
    pending_anchor = bool_value(row.get("pending_formal_anchor_hit"))
    known_meaning = (row.get("known_pending_formal_meaning_zh_simp") or "").strip()
    reviewed_meaning = (row.get("reviewed_meaning_zh_simp") or "").strip()

    if pending_anchor:
        final_category = "frontend_ready"
        final_meaning = known_meaning or reviewed_meaning
        reason = "Accepted from the existing 100 pending formal terms; historical correction anchor wins over older candidate meaning."
    elif word in NEEDS_MORE_EVIDENCE:
        final_category = "needs_more_evidence"
        final_meaning = ""
        reason = NEEDS_MORE_EVIDENCE[word]
    else:
        final_category = "learning_only"
        final_meaning = reviewed_meaning
        reason = "Useful as a learning/context word but not valuable enough to pop up as a front-end Life-study glossary entry."

    return {
        **row,
        "final_category": final_category,
        "final_meaning_zh_simp": final_meaning,
        "final_reason": reason,
        "front_end_candidate_ready": final_category == "frontend_ready",
        "front_end_import_ready": False,
        "database_write_performed": False,
        "review_status": "calibration_reviewed",
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
        "# Life-study Final Review Calibration 001 Reviewed",
        "",
        "This file is the completed review result for the first 100-row calibration batch. It does not write PostgreSQL and it does not import entries into the app.",
        "",
        f"- Reviewed rows: `{payload['quality']['reviewed_rows']}`",
        f"- Front-end ready: `{payload['quality']['decision_counts'].get('frontend_ready', 0)}`",
        f"- Learning only: `{payload['quality']['decision_counts'].get('learning_only', 0)}`",
        f"- Needs more evidence: `{payload['quality']['decision_counts'].get('needs_more_evidence', 0)}`",
        f"- Reject: `{payload['quality']['decision_counts'].get('reject', 0)}`",
        f"- Database write performed: `{payload['database_write_performed']}`",
        "",
        "## Decision Counts",
        "",
    ]
    for decision, count in sorted(payload["quality"]["decision_counts"].items()):
        lines.append(f"- `{decision}`: `{count}`")
    lines.extend(["", "## Front-end Ready", ""])
    for row in payload["items"]:
        if row["final_category"] == "frontend_ready":
            lines.extend(
                [
                    f"### {row['word']} -> {row['final_meaning_zh_simp']}",
                    "",
                    f"- Reason: {row['final_reason']}",
                    f"- Candidate meaning: `{row['reviewed_meaning_zh_simp']}`",
                    f"- Pending formal meaning: `{row['known_pending_formal_meaning_zh_simp']}`",
                    f"- Source: `{row['source_volume']} p{row['source_page']}`",
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
                    f"- EN: {row['evidence_en']}",
                    f"- ZH: {row['evidence_zh_simp']}",
                    "",
                ]
            )
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    rows = [adjudicate(row) for row in load_rows()]
    decision_counts = dict(Counter(row["final_category"] for row in rows))
    frontend_rows = [row for row in rows if row["final_category"] == "frontend_ready"]
    learning_rows = [row for row in rows if row["final_category"] == "learning_only"]
    more_evidence_rows = [row for row in rows if row["final_category"] == "needs_more_evidence"]
    reject_rows = [row for row in rows if row["final_category"] == "reject"]

    payload = {
        "schema": "sentence_reader.lifestudy_final_review_calibration_reviewed.v1",
        "generated_at": now_iso(),
        "source_template": str(TEMPLATE),
        "database_write_performed": False,
        "front_end_import_ready": False,
        "policy": "complete_100_row_calibration_review_no_db_write",
        "quality": {
            "reviewed_rows": len(rows),
            "blank_final_category_rows": sum(1 for row in rows if not row["final_category"]),
            "blank_final_reason_rows": sum(1 for row in rows if not row["final_reason"]),
            "front_end_import_ready_count": sum(1 for row in rows if row["front_end_import_ready"]),
            "database_write_count": sum(1 for row in rows if row["database_write_performed"]),
            "pending_formal_anchor_rows": sum(1 for row in rows if bool_value(row.get("pending_formal_anchor_hit"))),
            "pending_formal_meaning_mismatch_rows": sum(1 for row in rows if bool_value(row.get("pending_formal_meaning_mismatch"))),
            "decision_counts": decision_counts,
        },
        "outputs": {
            "reviewed_csv": str(OUTPUT_DIR / "calibration_001_reviewed.csv"),
            "frontend_ready_csv": str(OUTPUT_DIR / "calibration_001_frontend_ready.csv"),
            "learning_only_csv": str(OUTPUT_DIR / "calibration_001_learning_only.csv"),
            "needs_more_evidence_csv": str(OUTPUT_DIR / "calibration_001_needs_more_evidence.csv"),
            "reject_csv": str(OUTPUT_DIR / "calibration_001_reject.csv"),
            "summary_json": str(OUTPUT_DIR / "calibration_001_reviewed_summary.json"),
            "markdown": str(OUTPUT_DIR / "calibration_001_reviewed.md"),
        },
        "items": rows,
    }

    write_csv(OUTPUT_DIR / "calibration_001_reviewed.csv", rows)
    write_csv(OUTPUT_DIR / "calibration_001_frontend_ready.csv", frontend_rows)
    write_csv(OUTPUT_DIR / "calibration_001_learning_only.csv", learning_rows)
    write_csv(OUTPUT_DIR / "calibration_001_needs_more_evidence.csv", more_evidence_rows)
    write_csv(OUTPUT_DIR / "calibration_001_reject.csv", reject_rows)
    (OUTPUT_DIR / "calibration_001_reviewed_summary.json").write_text(
        json.dumps({k: v for k, v in payload.items() if k != "items"}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_markdown(OUTPUT_DIR / "calibration_001_reviewed.md", payload)
    print(json.dumps({k: v for k, v in payload.items() if k != "items"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
