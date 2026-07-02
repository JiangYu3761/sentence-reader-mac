#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE_URL = "postgresql://localhost/sentence_reader"


def stable_id(prefix: str, *parts: Any) -> str:
    raw = "\0".join(str(part or "") for part in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def normalize_term(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def clean_word_key(value: str) -> str:
    return re.sub(r"[^a-z']", "", normalize_term(value).replace("’", "'")).replace("'", "")


def load_importable(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "sentence_reader.lifestudy_vocab_importable.v1":
        raise SystemExit(f"unexpected importable schema: {payload.get('schema')}")
    if payload.get("database_write_performed") is not False:
        raise SystemExit("refusing import: source report says database write already happened")
    return payload


def source_pdf_from_report(payload: dict[str, Any]) -> str:
    source_report = payload.get("source_report")
    if not source_report:
        return ""
    try:
        report = json.loads(Path(str(source_report)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(report.get("source_pdf") or "")


def validate_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in payload.get("items") or []:
        term = normalize_term(str(item.get("term") or ""))
        meaning = str(item.get("suggested_meaning_zh_simp") or "").strip()
        grade = str(item.get("quality_grade") or "")
        if term in seen:
            rejected.append({"term": term, "reason": "duplicate term"})
            continue
        seen.add(term)
        if grade not in {"A", "B"}:
            rejected.append({"term": term, "reason": f"grade {grade} is not importable"})
            continue
        if item.get("import_allowed") is not True:
            rejected.append({"term": term, "reason": "import_allowed is not true"})
            continue
        if not term or not meaning:
            rejected.append({"term": term, "reason": "missing term or meaning"})
            continue
        if not item.get("evidence_en") or not item.get("evidence_zh_simp") or not item.get("source_page"):
            rejected.append({"term": term, "reason": "missing evidence"})
            continue
        normalized = dict(item)
        normalized["term"] = term
        normalized["lemma"] = clean_word_key(term) if " " not in term else term
        normalized["source_report"] = payload.get("source_report") or ""
        accepted.append(normalized)
    if rejected:
        raise SystemExit(f"refusing import: invalid importable items found: {rejected[:8]}")
    return accepted


def _load_psycopg():
    try:
        import psycopg
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb
    except ImportError as exc:
        raise SystemExit("psycopg is required for database import") from exc
    return psycopg, dict_row, Jsonb


def db_counts(database_url: str, book_id: str, terms: list[str]) -> dict[str, Any]:
    psycopg, dict_row, _ = _load_psycopg()
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        book = conn.execute("SELECT id, title FROM reader.books WHERE id = %s", (book_id,)).fetchone()
        if not book:
            return {"book_exists": False, "book": None, "existing_glossary_terms": 0, "existing_vocab_terms": 0}
        existing_glossary = conn.execute(
            "SELECT count(*) AS n FROM reader.book_glossary WHERE book_id = %s AND lower(term) = ANY(%s::text[])",
            (book_id, terms),
        ).fetchone()["n"]
        existing_vocab = conn.execute(
            "SELECT count(*) AS n FROM reader.book_vocab_items WHERE book_id = %s AND lower(surface) = ANY(%s::text[])",
            (book_id, terms),
        ).fetchone()["n"]
    return {
        "book_exists": True,
        "book": dict(book),
        "existing_glossary_terms": int(existing_glossary),
        "existing_vocab_terms": int(existing_vocab),
    }


def domain_counts(database_url: str, *, domain: str, volume: str, terms: list[str]) -> dict[str, Any]:
    psycopg, dict_row, _ = _load_psycopg()
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        existing = conn.execute(
            """
            SELECT count(*) AS n
            FROM reader.domain_glossary_entries
            WHERE domain = %s AND COALESCE(volume, '') = COALESCE(%s, '') AND lower(term) = ANY(%s::text[])
            """,
            (domain, volume, terms),
        ).fetchone()["n"]
    return {"domain": domain, "volume": volume, "existing_domain_terms": int(existing)}


def apply_import(database_url: str, book_id: str, items: list[dict[str, Any]]) -> dict[str, int]:
    psycopg, dict_row, Jsonb = _load_psycopg()
    inserted = {"book_glossary": 0, "lexemes": 0, "book_vocab_items": 0}
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        book = conn.execute("SELECT id FROM reader.books WHERE id = %s", (book_id,)).fetchone()
        if not book:
            raise SystemExit(f"book_id not found: {book_id}")
        for item in items:
            term = str(item["term"])
            lemma = str(item.get("lemma") or term)
            meaning = str(item["suggested_meaning_zh_simp"])
            confidence = 0.95 if item.get("quality_grade") == "A" else 0.88
            metadata = {
                "source": "lifestudy_context_vocab_pipeline",
                "source_report": item.get("source_report") or "",
                "quality_grade": item.get("quality_grade"),
                "source_page": item.get("source_page"),
                "evidence_en": item.get("evidence_en"),
                "evidence_zh_simp": item.get("evidence_zh_simp"),
                "reason": item.get("reason"),
                "match_source": item.get("match_source"),
                "occurrence_count": item.get("occurrence_count"),
                "part_of_speech": item.get("part_of_speech") or "",
                "part_of_speech_zh": item.get("part_of_speech_zh") or "",
                "popup_speak_text_zh": item.get("popup_speak_text_zh") or "",
                "tts_engine_preferred": item.get("tts_engine_preferred") or "",
                "tts_voice_preferred": item.get("tts_voice_preferred") or "",
                "single_click_popup_enabled": item.get("single_click_popup_enabled"),
                "high_confidence_popup": item.get("high_confidence_popup"),
            }

            conn.execute(
                """
                INSERT INTO reader.book_glossary (
                    id, book_id, term, meaning_zh, source, confidence, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, 'lifestudy_context', %s, now(), now())
                ON CONFLICT (book_id, term) DO UPDATE
                SET meaning_zh = CASE
                        WHEN reader.book_glossary.source = 'user' THEN reader.book_glossary.meaning_zh
                        ELSE EXCLUDED.meaning_zh
                    END,
                    source = CASE
                        WHEN reader.book_glossary.source = 'user' THEN reader.book_glossary.source
                        ELSE EXCLUDED.source
                    END,
                    confidence = CASE
                        WHEN reader.book_glossary.source = 'user' THEN reader.book_glossary.confidence
                        ELSE GREATEST(reader.book_glossary.confidence, EXCLUDED.confidence)
                    END,
                    updated_at = CASE
                        WHEN reader.book_glossary.source = 'user' THEN reader.book_glossary.updated_at
                        ELSE now()
                    END
                """,
                (stable_id("gloss", book_id, term), book_id, term, meaning, confidence),
            )
            inserted["book_glossary"] += 1

            lexeme_id = stable_id("lex", "en", lemma, term)
            conn.execute(
                """
                INSERT INTO reader.lexemes (
                    id, lemma, surface, language, part_of_speech, short_definition, source, created_at, updated_at
                )
                VALUES (%s, %s, %s, 'en', %s, %s, 'lifestudy_context', now(), now())
                ON CONFLICT (language, lemma, surface) DO UPDATE
                SET part_of_speech = COALESCE(NULLIF(EXCLUDED.part_of_speech, ''), reader.lexemes.part_of_speech),
                    short_definition = COALESCE(NULLIF(EXCLUDED.short_definition, ''), reader.lexemes.short_definition),
                    source = COALESCE(NULLIF(EXCLUDED.source, ''), reader.lexemes.source),
                    updated_at = now()
                """,
                (lexeme_id, lemma, term, item.get("part_of_speech") or "", meaning),
            )
            inserted["lexemes"] += 1

            conn.execute(
                """
                INSERT INTO reader.book_vocab_items (
                    id, book_id, lexeme_id, surface, lemma, context_meaning, meaning_source,
                    alignment_status, alignment_reason, representative_sentence_en, representative_sentence_zh,
                    occurrence_count, chapter_count, score, status, metadata, created_at, updated_at
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, 'lifestudy_context',
                    %s, %s, %s, %s,
                    %s, 0, %s, 'candidate', %s, now(), now()
                )
                ON CONFLICT (book_id, lemma, surface) DO UPDATE
                SET lexeme_id = EXCLUDED.lexeme_id,
                    context_meaning = EXCLUDED.context_meaning,
                    meaning_source = EXCLUDED.meaning_source,
                    alignment_status = EXCLUDED.alignment_status,
                    alignment_reason = EXCLUDED.alignment_reason,
                    representative_sentence_en = EXCLUDED.representative_sentence_en,
                    representative_sentence_zh = EXCLUDED.representative_sentence_zh,
                    occurrence_count = EXCLUDED.occurrence_count,
                    score = EXCLUDED.score,
                    metadata = reader.book_vocab_items.metadata || EXCLUDED.metadata,
                    updated_at = now()
                WHERE COALESCE(reader.book_vocab_items.meaning_source, '') <> 'user_glossary'
                """,
                (
                    stable_id("vocab", book_id, lemma, term),
                    book_id,
                    lexeme_id,
                    term,
                    lemma,
                    meaning,
                    "confirmed_context_meaning" if item.get("quality_grade") == "A" else "paraphrased_context_meaning",
                    item.get("reason") or "",
                    item.get("evidence_en") or "",
                    item.get("evidence_zh_simp") or "",
                    int(item.get("occurrence_count") or 0),
                    float(item.get("score") or 0),
                    Jsonb(metadata),
                ),
            )
            inserted["book_vocab_items"] += 1
        conn.commit()
    return inserted


def apply_domain_import(
    database_url: str,
    *,
    domain: str,
    volume: str,
    source_title: str,
    source_pdf: str,
    items: list[dict[str, Any]],
) -> dict[str, int]:
    psycopg, dict_row, Jsonb = _load_psycopg()
    inserted = {"domain_glossary_entries": 0}
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        for item in items:
            term = str(item["term"])
            confidence = 0.95 if item.get("quality_grade") == "A" else 0.88
            metadata = {
                "source": "lifestudy_context_vocab_pipeline",
                "source_report": item.get("source_report") or "",
                "reason": item.get("reason"),
                "match_source": item.get("match_source"),
                "alignment_confidence": item.get("alignment_confidence"),
                "alignment_score": item.get("alignment_score"),
                "part_of_speech": item.get("part_of_speech") or "",
                "part_of_speech_zh": item.get("part_of_speech_zh") or "",
                "popup_speak_text_zh": item.get("popup_speak_text_zh") or "",
                "tts_engine_preferred": item.get("tts_engine_preferred") or "",
                "tts_voice_preferred": item.get("tts_voice_preferred") or "",
                "single_click_popup_enabled": item.get("single_click_popup_enabled"),
                "high_confidence_popup": item.get("high_confidence_popup"),
            }
            conn.execute(
                """
                INSERT INTO reader.domain_glossary_entries (
                    id, domain, volume, language, term, lemma, meaning_zh, quality_grade, confidence,
                    source_title, source_pdf, source_page, evidence_en, evidence_zh,
                    occurrence_count, score, status, metadata, created_at, updated_at
                )
                VALUES (
                    %s, %s, %s, 'en', %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, 'active', %s, now(), now()
                )
                ON CONFLICT (domain, volume, language, term) DO UPDATE
                SET lemma = EXCLUDED.lemma,
                    meaning_zh = EXCLUDED.meaning_zh,
                    quality_grade = EXCLUDED.quality_grade,
                    confidence = GREATEST(reader.domain_glossary_entries.confidence, EXCLUDED.confidence),
                    source_title = EXCLUDED.source_title,
                    source_pdf = EXCLUDED.source_pdf,
                    source_page = EXCLUDED.source_page,
                    evidence_en = EXCLUDED.evidence_en,
                    evidence_zh = EXCLUDED.evidence_zh,
                    occurrence_count = EXCLUDED.occurrence_count,
                    score = EXCLUDED.score,
                    status = 'active',
                    metadata = reader.domain_glossary_entries.metadata || EXCLUDED.metadata,
                    updated_at = now()
                """,
                (
                    stable_id("dgloss", domain, volume, term),
                    domain,
                    volume,
                    term,
                    item.get("lemma") or term,
                    item.get("suggested_meaning_zh_simp"),
                    item.get("quality_grade"),
                    confidence,
                    source_title,
                    source_pdf,
                    int(item.get("source_page") or 0),
                    item.get("evidence_en") or "",
                    item.get("evidence_zh_simp") or "",
                    int(item.get("occurrence_count") or 0),
                    float(item.get("score") or 0),
                    Jsonb(metadata),
                ),
            )
            inserted["domain_glossary_entries"] += 1
        conn.commit()
    return inserted


def main() -> int:
    parser = argparse.ArgumentParser(description="Controlled import for Life-study context vocabulary.")
    parser.add_argument("importable_json", type=Path)
    parser.add_argument("--book-id")
    parser.add_argument("--domain-staging", action="store_true", help="Import into reader.domain_glossary_entries instead of a book.")
    parser.add_argument("--domain", default="lifestudy")
    parser.add_argument("--volume", default="Genesis")
    parser.add_argument("--source-title", default="Life-study of Genesis")
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument("--apply", action="store_true", help="Actually write A/B importable items. Default is dry-run.")
    args = parser.parse_args()

    payload = load_importable(args.importable_json)
    items = validate_items(payload)
    terms = [str(item["term"]) for item in items]
    source_pdf = source_pdf_from_report(payload)

    if not args.domain_staging and not args.book_id:
        raise SystemExit("either --book-id or --domain-staging is required")
    if args.domain_staging:
        counts = domain_counts(args.database_url, domain=args.domain, volume=args.volume, terms=terms)
    else:
        counts = db_counts(args.database_url, str(args.book_id), terms)
        if not counts["book_exists"]:
            raise SystemExit(f"book_id not found: {args.book_id}")

    result = {
        "ok": True,
        "schema": "sentence_reader.lifestudy_vocab_import_run.v1",
        "mode": "apply" if args.apply else "dry_run",
        "target": "domain_staging" if args.domain_staging else "book",
        "database_url": args.database_url,
        "book_id": args.book_id or "",
        "book": counts.get("book"),
        "domain": args.domain if args.domain_staging else "",
        "volume": args.volume if args.domain_staging else "",
        "source_title": args.source_title if args.domain_staging else "",
        "source_pdf": source_pdf,
        "candidate_count": len(items),
        "existing_glossary_terms": counts.get("existing_glossary_terms", 0),
        "existing_vocab_terms": counts.get("existing_vocab_terms", 0),
        "existing_domain_terms": counts.get("existing_domain_terms", 0),
        "accepted_terms": terms,
        "database_write_performed": False,
    }
    if args.apply:
        if args.domain_staging:
            result["inserted"] = apply_domain_import(
                args.database_url,
                domain=args.domain,
                volume=args.volume,
                source_title=args.source_title,
                source_pdf=source_pdf,
                items=items,
            )
        else:
            result["inserted"] = apply_import(args.database_url, str(args.book_id), items)
        result["database_write_performed"] = True
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
