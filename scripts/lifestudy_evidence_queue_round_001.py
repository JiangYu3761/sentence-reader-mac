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
CACHE_DIR = CORPUS_DIR / "all_word_volume_caches"
OUTPUT_DIR = ROOT / "reports" / "lifestudy_vocab_final_review"
EVIDENCE_QUEUE = OUTPUT_DIR / "productized_lifestudy_evidence_queue.csv"

ROUND_ID = "round_001"
ROUND_SIZE = 50
INPUT_CSV = OUTPUT_DIR / "evidence_queue_round_001_input.csv"
REVIEWED_CSV = OUTPUT_DIR / "evidence_queue_round_001_reviewed.csv"
PROMOTE_CSV = OUTPUT_DIR / "evidence_queue_round_001_promote_default_front_glossary.csv"
MOVE_LEARNING_CSV = OUTPUT_DIR / "evidence_queue_round_001_move_to_learning_vocab.csv"
KEEP_CSV = OUTPUT_DIR / "evidence_queue_round_001_keep_evidence_queue.csv"
REJECT_CSV = OUTPUT_DIR / "evidence_queue_round_001_reject.csv"
SUMMARY_JSON = OUTPUT_DIR / "evidence_queue_round_001_summary.json"
SUMMARY_MD = OUTPUT_DIR / "evidence_queue_round_001_summary.md"

PROMOTE_MEANINGS: dict[str, tuple[str, str]] = {
    "flesh": ("肉体", "Stable Life-study and biblical term with repeated aligned evidence."),
    "cross": ("十字架", "Stable biblical term with repeated aligned evidence and high reader value."),
    "blood": ("血", "Stable biblical term with repeated aligned evidence."),
    "covenant": ("约", "Corrects the narrower candidate 盟约 to the stable Life-study/Bible term 约."),
    "soul": ("魂", "Corrects the old broad candidate 人 to the stable tripartite-man term 魂."),
    "humanity": ("人性", "Stable theological/humanity term with repeated aligned evidence."),
    "enemy": ("仇敌", "Stable biblical/spiritual term with repeated aligned evidence."),
    "rest": ("安息", "Stable biblical/spiritual term with repeated aligned evidence."),
    "lamb": ("羔羊", "Stable biblical typology term with repeated aligned evidence."),
    "fallen": ("堕落", "Stable spiritual condition term; use base meaning rather than adjective-only wording."),
    "presence": ("同在", "Corrects the old broad candidate 面前 to the spiritually useful meaning 同在."),
    "feast": ("节期", "Corrects the old candidate 享受 to the stable biblical term 节期."),
    "ark": ("方舟", "Stable biblical typology term with repeated aligned evidence."),
    "mercy": ("怜悯", "Stable spiritual term with repeated aligned evidence."),
    "elders": ("长老", "Stable church term with repeated aligned evidence."),
    "sabbath": ("安息日", "Stable biblical term with repeated aligned evidence."),
}

MOVE_LEARNING_MEANINGS: dict[str, tuple[str, str]] = {
    "age": ("时代", "Useful learning word, but too polysemous for default popup."),
    "city": ("城", "Evidence supports city/城, but the word is broad and should not default-pop."),
    "seed": ("种子", "Useful biblical learning term, but current round keeps it out of default popup."),
    "food": ("食物", "Useful as learning/context word; too ordinary for default popup."),
    "concept": ("观念", "Useful learning word, not a default Life-study glossary term."),
    "stone": ("石头", "Useful biblical/material word, but too broad for default popup."),
    "dead": ("死的", "Useful learning adjective, not a stable front-end glossary entry."),
    "sense": ("感觉", "Useful learning/context word, not default-popup material."),
    "suffering": ("受苦", "Useful learning/spiritual word, but not front-end-stable in this round."),
    "epistle": ("书信", "Useful Bible-genre learning word, not default popup in this round."),
    "religion": ("宗教", "Useful Life-study contrast term, but broad enough to keep as learning first."),
    "angels": ("天使", "Useful biblical learning word, not default popup in this round."),
    "wisdom": ("智慧", "Useful learning/spiritual word, not default popup in this round."),
    "fire": ("火", "Useful biblical symbol word, but too broad for default popup."),
    "dwelling": ("住处", "Useful learning word; meaning can vary with 居所/住处 contexts."),
    "practice": ("实行", "Useful learning word, not default popup."),
    "prophets": ("先知", "Useful biblical learning word, not default popup in this round."),
    "virtues": ("美德", "Useful learning word, not default popup."),
    "gold": ("金子", "Corrects old unstable candidate 宝贵 to the literal material 金子 for learning use."),
    "sinners": ("罪人", "Useful biblical learning word; default already has sin/罪."),
    "bread": ("饼", "Corrects old broad candidate 食物; keep as learning before popup use."),
    "dwell": ("居住", "Useful learning verb, not default popup."),
    "source": ("来源", "Useful learning word, too broad for default popup."),
    "intention": ("目的", "Useful learning/context word, too broad for default popup."),
    "eternity": ("永远", "Useful learning/spiritual word, but not default-popup material in this round."),
    "pillar": ("柱子", "Useful typology/material learning word, not default popup."),
    "material": ("材料", "Useful learning word, too broad for default popup."),
    "idols": ("偶像", "Useful biblical learning word, not default popup in this round."),
}

KEEP_MEANINGS: dict[str, tuple[str, str]] = {
    "power": ("能力", "Power maps to 能力/权柄/势力 by context; keep for sense separation instead of hard-promoting."),
    "gifts": ("恩赐", "Gifts can map to 恩赐 or 礼物; keep for stronger sense evidence."),
    "drink": ("饮料", "Drink can be noun/verb and typological/common; keep until sense is split."),
    "true": ("真实的", "Too broad and semantically variable for promotion."),
    "sacrifice": ("祭物", "Sacrifice maps across 祭物/献祭/牺牲; keep for stronger sense separation."),
    "serve": ("服事", "Serve can map to 服事/事奉/供应; keep for stronger sense separation."),
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


def meaning_supported(meaning: str, evidence_zh: str) -> bool:
    return bool(meaning) and meaning in (evidence_zh or "")


def to_int(value: str) -> int:
    try:
        return int(float(value or 0))
    except ValueError:
        return 0


def priority_score(row: dict[str, str]) -> tuple[int, int, str]:
    return (-to_int(row.get("total_content_frequency", "")), -to_int(row.get("volume_count", "")), row.get("word", ""))


def volume_label(volume: Any, fallback: str) -> str:
    if isinstance(volume, dict):
        idx = str(volume.get("volume_index") or "").strip()
        title = str(volume.get("title_en") or "").strip()
        if idx and title:
            return f"{idx} {title}"
        return title or idx or fallback
    return str(volume or fallback)


def evidence_index(target_words: set[str]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {word: [] for word in target_words}
    for path in sorted(CACHE_DIR.glob("*-all-words-cache.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        volume = volume_label(payload.get("volume"), path.name)
        for item in payload.get("items", []):
            word = str(item.get("word") or "").strip().lower()
            if word not in target_words:
                continue
            for evidence in item.get("sample_evidence", []):
                en = str(evidence.get("en") or "").strip()
                zh = str(evidence.get("zh_simp") or "").strip()
                if not en or not zh or not word_supported(word, en):
                    continue
                result[word].append(
                    {
                        "source_volume": volume,
                        "source_page": evidence.get("page") or "",
                        "confidence": evidence.get("confidence") or "",
                        "alignment_score": evidence.get("alignment_score") or "",
                        "evidence_en": en,
                        "evidence_zh": zh,
                    }
                )
    for word, items in result.items():
        seen: set[tuple[str, str, str]] = set()
        deduped: list[dict[str, Any]] = []
        for item in items:
            key = (str(item["source_volume"]), str(item["source_page"]), item["evidence_en"][:120])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        result[word] = deduped
    return result


def pick_evidence(items: list[dict[str, Any]], meaning: str, min_supported: int) -> list[dict[str, Any]]:
    supported = [item for item in items if meaning_supported(meaning, str(item.get("evidence_zh") or ""))]
    preferred = supported if len(supported) >= min_supported else supported + [item for item in items if item not in supported]
    return preferred[:3]


def evidence_fields(items: list[dict[str, Any]]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for index in range(3):
        item = items[index] if index < len(items) else {}
        number = index + 1
        fields[f"evidence_en_{number}"] = item.get("evidence_en", "")
        fields[f"evidence_zh_{number}"] = item.get("evidence_zh", "")
        fields[f"source_volume_{number}"] = item.get("source_volume", "")
        fields[f"source_page_{number}"] = item.get("source_page", "")
    source_volumes = []
    for item in items:
        value = str(item.get("source_volume") or "")
        if value and value not in source_volumes:
            source_volumes.append(value)
    fields["source_volumes"] = "; ".join(source_volumes)
    fields["evidence_count"] = len([item for item in items if item.get("evidence_en") and item.get("evidence_zh")])
    return fields


def review_row(row: dict[str, str], evidence_by_word: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    word = row["word"].strip().lower()
    current_meaning = row.get("product_meaning_zh_simp") or row.get("reviewed_meaning_zh_simp") or ""
    if word in PROMOTE_MEANINGS:
        proposed, base_reason = PROMOTE_MEANINGS[word]
        picked = pick_evidence(evidence_by_word.get(word, []), proposed, 3)
        if len([item for item in picked if meaning_supported(proposed, str(item.get("evidence_zh") or ""))]) >= 3:
            decision = "promote_default_front_glossary"
            layer = "default_front_glossary"
            can_default = True
            can_learning = True
            needs_more = False
            reason = base_reason
        else:
            decision = "keep_evidence_queue"
            layer = "evidence_queue"
            can_default = False
            can_learning = False
            needs_more = True
            reason = f"Requested promotion to {proposed}, but fewer than 3 aligned Chinese evidence rows support it."
    elif word in MOVE_LEARNING_MEANINGS:
        proposed, base_reason = MOVE_LEARNING_MEANINGS[word]
        picked = pick_evidence(evidence_by_word.get(word, []), proposed, 1)
        decision = "move_to_learning_vocab"
        layer = "learning_vocab"
        can_default = False
        can_learning = True
        needs_more = False
        reason = base_reason
    elif word in KEEP_MEANINGS:
        proposed, base_reason = KEEP_MEANINGS[word]
        picked = pick_evidence(evidence_by_word.get(word, []), proposed, 1)
        decision = "keep_evidence_queue"
        layer = "evidence_queue"
        can_default = False
        can_learning = False
        needs_more = True
        reason = base_reason
    else:
        proposed = current_meaning
        picked = pick_evidence(evidence_by_word.get(word, []), proposed, 1)
        decision = "keep_evidence_queue"
        layer = "evidence_queue"
        can_default = False
        can_learning = False
        needs_more = True
        reason = "No stable round_001 rule promoted this word; keep for later evidence review."

    output: dict[str, Any] = {
        "round_id": ROUND_ID,
        "source_index": row.get("source_index", ""),
        "word": row.get("word", ""),
        "lemma": row.get("lemma", ""),
        "current_meaning_zh_simp": current_meaning,
        "proposed_final_meaning_zh_simp": proposed,
        "decision": decision,
        "decision_reason": reason,
        "product_layer_after_review": layer,
        "can_default_popup": can_default,
        "can_learning_search": can_learning,
        "needs_more_evidence": needs_more,
        "database_write_performed": False,
        "front_end_import_ready": False,
        "total_content_frequency": row.get("total_content_frequency", ""),
        "volume_count": row.get("volume_count", ""),
        "original_source_volume": row.get("source_volume", ""),
        "original_source_page": row.get("source_page", ""),
        "original_evidence_en": row.get("evidence_en", ""),
        "original_evidence_zh_simp": row.get("evidence_zh_simp", ""),
    }
    output.update(evidence_fields(picked))
    return output


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    q = payload["quality"]
    lines = [
        "# Life-study Evidence Queue Round 001",
        "",
        "This is a no-write second-pass review of 50 high-priority evidence queue terms.",
        "",
        "## Counts",
        "",
    ]
    for decision, count in q["decision_counts"].items():
        lines.append(f"- `{decision}`: `{count}`")
    lines.extend(
        [
            "",
            "## Corrected Meanings",
            "",
        ]
    )
    for item in payload["corrected_meanings"]:
        lines.append(f"- `{item['word']}`: `{item['from']}` -> `{item['to']}`")
    lines.extend(["", "## Outputs", ""])
    for key, value in payload["outputs"].items():
        lines.append(f"- `{key}`: `{value}`")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    queue_rows = read_csv(EVIDENCE_QUEUE)
    if len(queue_rows) != 96:
        fail(f"evidence queue count mismatch: {len(queue_rows)} != 96")
    round_rows = sorted(queue_rows, key=priority_score)[:ROUND_SIZE]
    target_words = {row["word"].strip().lower() for row in round_rows}
    evidence_by_word = evidence_index(target_words)
    reviewed = [review_row(row, evidence_by_word) for row in round_rows]

    fields = [
        "round_id",
        "source_index",
        "word",
        "lemma",
        "current_meaning_zh_simp",
        "proposed_final_meaning_zh_simp",
        "evidence_en_1",
        "evidence_zh_1",
        "evidence_en_2",
        "evidence_zh_2",
        "evidence_en_3",
        "evidence_zh_3",
        "evidence_count",
        "source_volumes",
        "decision",
        "decision_reason",
        "product_layer_after_review",
        "can_default_popup",
        "can_learning_search",
        "needs_more_evidence",
        "database_write_performed",
        "front_end_import_ready",
        "total_content_frequency",
        "volume_count",
        "source_volume_1",
        "source_page_1",
        "source_volume_2",
        "source_page_2",
        "source_volume_3",
        "source_page_3",
        "original_source_volume",
        "original_source_page",
        "original_evidence_en",
        "original_evidence_zh_simp",
    ]
    write_csv(INPUT_CSV, round_rows, list(round_rows[0].keys()))
    write_csv(REVIEWED_CSV, reviewed, fields)
    split = {
        "promote_default_front_glossary": [row for row in reviewed if row["decision"] == "promote_default_front_glossary"],
        "move_to_learning_vocab": [row for row in reviewed if row["decision"] == "move_to_learning_vocab"],
        "keep_evidence_queue": [row for row in reviewed if row["decision"] == "keep_evidence_queue"],
        "reject": [row for row in reviewed if row["decision"] == "reject"],
    }
    write_csv(PROMOTE_CSV, split["promote_default_front_glossary"], fields)
    write_csv(MOVE_LEARNING_CSV, split["move_to_learning_vocab"], fields)
    write_csv(KEEP_CSV, split["keep_evidence_queue"], fields)
    write_csv(REJECT_CSV, split["reject"], fields)
    corrected = [
        {
            "word": row["word"],
            "from": row["current_meaning_zh_simp"],
            "to": row["proposed_final_meaning_zh_simp"],
        }
        for row in reviewed
        if row["current_meaning_zh_simp"] != row["proposed_final_meaning_zh_simp"]
    ]
    payload = {
        "schema": "sentence_reader.lifestudy_evidence_queue_round_001.v1",
        "generated_at": now_iso(),
        "round_id": ROUND_ID,
        "database_write_performed": False,
        "front_end_import_ready": False,
        "quality": {
            "input_rows": len(round_rows),
            "reviewed_rows": len(reviewed),
            "decision_counts": dict(Counter(row["decision"] for row in reviewed)),
            "database_write_count": sum(1 for row in reviewed if row["database_write_performed"] is not False),
            "front_end_import_ready_count": sum(1 for row in reviewed if row["front_end_import_ready"] is not False),
            "corrected_meaning_count": len(corrected),
        },
        "corrected_meanings": corrected,
        "outputs": {
            "input_csv": str(INPUT_CSV),
            "reviewed_csv": str(REVIEWED_CSV),
            "promote_default_front_glossary_csv": str(PROMOTE_CSV),
            "move_to_learning_vocab_csv": str(MOVE_LEARNING_CSV),
            "keep_evidence_queue_csv": str(KEEP_CSV),
            "reject_csv": str(REJECT_CSV),
            "summary_json": str(SUMMARY_JSON),
            "summary_md": str(SUMMARY_MD),
        },
    }
    SUMMARY_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(SUMMARY_MD, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
