from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from reader_api import db
from reader_api.voice_hermes import VoiceHermesAdapter, get_voice_hermes_adapter
from reader_api.voice_inbox_service import (
    create_context_snapshot,
    effective_transcript,
    get_context_snapshot,
    get_voice_record_or_404,
    json_hash,
    stable_id,
)


VOICE_READING_CONTEXT_SCHEMA = "click.voice.reading_context.v1"
VOICE_READING_CONTEXT_ALGORITHM_VERSION = "click.voice.reading_context.algorithm.v2"
VOICE_CITATION_SCHEMA = "click.voice.citation.v1"
CURRENT_BOOK_EVIDENCE_SCHEMA = "click.voice.current_book_evidence.v1"
MAX_RECENT_BOOKS = 12
MAX_EVIDENCE_PER_BOOK = 18
MAX_CURRENT_BOOK_EVIDENCE_ITEMS = 8
MAX_CURRENT_BOOK_EVIDENCE_ITEM_CHARS = 1_500
MAX_CURRENT_BOOK_EVIDENCE_TOTAL_CHARS = 12_000
_SMOKE_TITLE = re.compile(r"(?:\bsmoke\b|\btest\b|测试|示例书)", flags=re.I)
_LIVING_ASSET_PATHS = (
    "6_Hermes调用/书籍调用卡.md",
    "5_书籍思想模型/思维协议.md",
    "5_书籍思想模型/这本书的核心主张.md",
    "5_书籍思想模型/这本书的判断标准.md",
    "5_书籍思想模型/这本书的盲区.md",
)


class CitationResolutionError(RuntimeError):
    pass


class CurrentBookNotFoundError(ValueError):
    pass


def knowledge_base_root() -> Path:
    return Path(os.getenv("CLICK_KNOWLEDGE_BASE_ROOT", str(Path.home() / "Documents" / "KnowledgeBase"))).expanduser().resolve()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _inside_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _bundle_indexes() -> dict[str, Path]:
    root = knowledge_base_root()
    output: dict[str, Path] = {}
    migration = _read_json(root / "_system" / "migrations" / "living_books_v1_map.json")
    for item in migration.get("entries") or []:
        if not isinstance(item, dict):
            continue
        book_id = str(item.get("book_id") or "")
        raw_path = item.get("write_owner_path") or item.get("legacy_bundle_path")
        if not book_id or not raw_path:
            continue
        path = Path(str(raw_path)).expanduser()
        if _inside_root(path, root) and (path / "book_manifest.json").is_file():
            output[book_id] = path.resolve()
    category = _read_json(root / "_system" / "indexes" / "book_category_index.json")
    for item in category.get("books") or []:
        if not isinstance(item, dict):
            continue
        book_id = str(item.get("book_id") or "")
        raw_path = item.get("bundle_dir")
        if book_id in output or not book_id or not raw_path:
            continue
        path = Path(str(raw_path)).expanduser()
        if _inside_root(path, root) and (path / "book_manifest.json").is_file():
            output[book_id] = path.resolve()
    return output


def _evidence_id(source_type: str, target_id: str, text: str) -> str:
    return stable_id("vev", source_type, target_id, json_hash(text))


def _evidence(
    *,
    source_type: str,
    evidence_class: str,
    target_id: str,
    book: dict[str, Any],
    text: str,
    resolver: dict[str, Any],
    chapter_title: Optional[str] = None,
    chapter_locator: Optional[str] = None,
    review_state: str = "source",
) -> dict[str, Any]:
    normalized = str(text or "").strip()
    return {
        "schema": VOICE_CITATION_SCHEMA,
        "evidence_id": _evidence_id(source_type, target_id, normalized),
        "source_type": source_type,
        "evidence_class": evidence_class,
        "target_id": target_id,
        "book_id": book.get("id"),
        "book_title": book.get("title"),
        "book_author": book.get("author"),
        "chapter_title": chapter_title,
        "chapter_locator": chapter_locator,
        "text": normalized,
        "content_hash": json_hash(normalized),
        "review_state": review_state,
        "resolver": resolver,
    }


def _recent_book_rows(selected_book_ids: list[str], scope: str) -> list[dict[str, Any]]:
    selected = list(dict.fromkeys(str(book_id).strip() for book_id in selected_book_ids if str(book_id).strip()))
    if len(selected) > 20:
        raise ValueError("一次最多选择 20 本书")
    with db.connect() as conn:
        if scope == "selected_books":
            if not selected:
                return []
            rows = conn.execute(
                """
                SELECT b.*, rp.chapter_id, rp.chapter_locator,
                       rp.updated_at AS position_updated_at,
                       COALESCE(rp.updated_at, b.last_opened_at, b.updated_at) AS recent_activity_at
                FROM reader.books b
                LEFT JOIN reader.reading_positions rp ON rp.book_id = b.id
                WHERE b.id = ANY(%s)
                ORDER BY COALESCE(rp.updated_at, b.last_opened_at, b.updated_at) DESC
                """,
                (selected,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT b.*, rp.chapter_id, rp.chapter_locator,
                       rp.updated_at AS position_updated_at,
                       COALESCE(rp.updated_at, b.last_opened_at, b.updated_at) AS recent_activity_at
                FROM reader.books b
                LEFT JOIN reader.reading_positions rp ON rp.book_id = b.id
                LEFT JOIN reader.library_state ls ON ls.book_id = b.id
                WHERE COALESCE(ls.hidden, false) = false
                  AND (rp.updated_at IS NOT NULL OR b.last_opened_at IS NOT NULL)
                ORDER BY COALESCE(rp.updated_at, b.last_opened_at) DESC NULLS LAST
                LIMIT %s
                """,
                (MAX_RECENT_BOOKS,),
            ).fetchall()
            by_id = {str(row["id"]): dict(row) for row in rows}
            if selected:
                explicit = conn.execute(
                    """
                    SELECT b.*, rp.chapter_id, rp.chapter_locator,
                           rp.updated_at AS position_updated_at,
                           COALESCE(rp.updated_at, b.last_opened_at, b.updated_at) AS recent_activity_at
                    FROM reader.books b
                    LEFT JOIN reader.reading_positions rp ON rp.book_id = b.id
                    WHERE b.id = ANY(%s)
                    """,
                    (selected,),
                ).fetchall()
                by_id.update({str(row["id"]): dict(row) for row in explicit})
                rows = list(by_id.values())
    output = []
    for row in rows:
        item = dict(row)
        if scope != "selected_books" and str(item.get("id")) not in selected and _SMOKE_TITLE.search(str(item.get("title") or "")):
            continue
        item["explicitly_selected"] = str(item.get("id")) in selected
        output.append(item)
    if scope == "selected_books":
        found = {str(item["id"]) for item in output}
        missing = [book_id for book_id in selected if book_id not in found]
        if missing:
            raise ValueError(f"选择的书不存在：{', '.join(missing)}")
    output.sort(key=lambda item: str(item.get("recent_activity_at") or ""), reverse=True)
    return output[: max(MAX_RECENT_BOOKS, len(selected))]


def _transcript_keywords(transcript: str) -> set[str]:
    output = {word.casefold() for word in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", transcript)}
    stop = {"我觉得", "这个", "一个", "就是", "然后", "应该", "没有", "可以", "怎么", "什么", "需要", "还是"}
    for segment in re.findall(r"[\u4e00-\u9fff]{2,}", transcript):
        if segment not in stop and len(segment) <= 12:
            output.add(segment)
        for size in (2, 3, 4):
            for index in range(max(0, len(segment) - size + 1)):
                token = segment[index : index + size]
                if token not in stop:
                    output.add(token)
    return output


def _database_evidence(book: dict[str, Any], transcript: str) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    with db.connect() as conn:
        annotations = conn.execute(
            """
            SELECT * FROM reader.annotations
            WHERE book_id = %s
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 200
            """,
            (book["id"],),
        ).fetchall()
        keywords = _transcript_keywords(transcript)

        def annotation_score(row: Any) -> int:
            combined = f"{row.get('source_text') or ''}\n{row.get('note_text') or ''}".casefold()
            return sum(len(keyword) * len(keyword) for keyword in keywords if keyword in combined)

        ranked_annotations = sorted(
            enumerate(annotations),
            key=lambda pair: (-annotation_score(pair[1]), pair[0]),
        )[:12]
        for _, row in ranked_annotations:
            item = dict(row)
            source_text = str(item.get("source_text") or "").strip()
            if source_text:
                evidence.append(
                    _evidence(
                        source_type="annotation_source",
                        evidence_class="book_source",
                        target_id=str(item["id"]),
                        book=book,
                        text=source_text,
                        chapter_title=item.get("chapter_title"),
                        chapter_locator=item.get("chapter_locator"),
                        resolver={"kind": "annotation", "id": item["id"], "field": "source_text"},
                    )
                )
            note_text = str(item.get("note_text") or "").strip()
            if note_text:
                evidence.append(
                    _evidence(
                        source_type="user_annotation",
                        evidence_class="user_annotation",
                        target_id=str(item["id"]),
                        book=book,
                        text=note_text,
                        chapter_title=item.get("chapter_title"),
                        chapter_locator=item.get("chapter_locator"),
                        resolver={"kind": "annotation", "id": item["id"], "field": "note_text"},
                    )
                )
        chapter_id = book.get("chapter_id")
        if chapter_id:
            sentences = conn.execute(
                """
                SELECT s.*, c.title AS chapter_title
                FROM reader.sentences s
                LEFT JOIN reader.chapters c ON c.id = s.chapter_id
                WHERE s.book_id = %s AND s.chapter_id = %s
                ORDER BY s.sentence_index ASC
                LIMIT 160
                """,
                (book["id"], chapter_id),
            ).fetchall()
            keywords = _transcript_keywords(transcript)
            ranked = []
            for row in sentences:
                text = str(row.get("text") or "")
                score = sum(1 for keyword in keywords if keyword.casefold() in text.casefold())
                if score:
                    ranked.append((score, dict(row)))
            for _, item in sorted(ranked, key=lambda pair: (-pair[0], int(pair[1].get("sentence_index") or 0)))[:5]:
                evidence.append(
                    _evidence(
                        source_type="reader_sentence",
                        evidence_class="book_source",
                        target_id=str(item["id"]),
                        book=book,
                        text=str(item["text"]),
                        chapter_title=item.get("chapter_title"),
                        chapter_locator=item.get("chapter_locator"),
                        resolver={"kind": "sentence", "id": item["id"]},
                    )
                )
    return evidence


def _allowed_living_path(target: Path, bundle: Path, hermes_manifest: dict[str, Any]) -> bool:
    authorization = hermes_manifest.get("authorization") if isinstance(hermes_manifest.get("authorization"), dict) else {}
    allowed = authorization.get("allowed_paths") if isinstance(authorization.get("allowed_paths"), list) else []
    if not allowed:
        return target.relative_to(bundle).as_posix() in _LIVING_ASSET_PATHS
    hermes_dir = bundle / "6_Hermes调用"
    for raw in allowed:
        candidate = (hermes_dir / str(raw)).resolve()
        if not _inside_root(candidate, bundle):
            continue
        try:
            target.resolve().relative_to(candidate)
            return True
        except ValueError:
            if target.resolve() == candidate:
                return True
    return False


def _living_book_evidence(book: dict[str, Any], bundles: dict[str, Path]) -> list[dict[str, Any]]:
    bundle = bundles.get(str(book["id"]))
    if not bundle:
        return []
    manifest = _read_json(bundle / "book_manifest.json")
    if str(manifest.get("book_id") or "") != str(book["id"]):
        return []
    hermes_manifest = _read_json(bundle / "6_Hermes调用" / "hermes_manifest.json")
    if hermes_manifest.get("schema") != "click.living_book.hermes_manifest.v1":
        return []
    evidence = []
    root = knowledge_base_root()
    for relative in _LIVING_ASSET_PATHS:
        path = (bundle / relative).resolve()
        if not _inside_root(path, root) or not path.is_file() or not _allowed_living_path(path, bundle, hermes_manifest):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        content = content.strip()
        if not content:
            continue
        header = "\n".join(content.splitlines()[:20]).casefold()
        review_state = "reviewed" if re.search(r"status:\s*(?:reviewed|final|confirmed)", header) and "needs_review: true" not in header else "draft"
        excerpt = content[:5000]
        evidence.append(
            _evidence(
                source_type="living_book_asset",
                evidence_class="living_book_model",
                target_id=relative,
                book=book,
                text=excerpt,
                review_state=review_state,
                resolver={
                    "kind": "living_book_asset",
                    "path": str(path),
                    "relative_path": path.relative_to(bundle).as_posix(),
                    "file_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                },
            )
        )
    return evidence


def _keyword_score(text: str, keywords: set[str]) -> int:
    normalized = str(text or "").casefold()
    return sum(len(keyword) * len(keyword) for keyword in keywords if keyword in normalized)


def _current_book_search_keywords(keywords: set[str]) -> list[str]:
    return sorted(
        (keyword for keyword in keywords if len(keyword) >= 2),
        key=lambda keyword: (-len(keyword), keyword),
    )[:12]


def _bounded_current_book_evidence(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bounded: list[dict[str, Any]] = []
    total_chars = 0
    seen: set[str] = set()
    for item in items:
        evidence_id = str(item.get("evidence_id") or "")
        if not evidence_id or evidence_id in seen:
            continue
        available = min(
            MAX_CURRENT_BOOK_EVIDENCE_ITEM_CHARS,
            MAX_CURRENT_BOOK_EVIDENCE_TOTAL_CHARS - total_chars,
        )
        if available <= 0 or len(bounded) >= MAX_CURRENT_BOOK_EVIDENCE_ITEMS:
            break
        original_text = str(item.get("text") or "").strip()
        if not original_text:
            continue
        text = original_text[:available]
        bounded_item = dict(item)
        bounded_item["text"] = text
        bounded_item["content_hash"] = json_hash(text)
        bounded_item["evidence_id"] = _evidence_id(
            str(item.get("source_type") or ""),
            str(item.get("target_id") or ""),
            text,
        )
        bounded_item["text_truncated"] = len(text) < len(original_text)
        bounded.append(bounded_item)
        seen.add(evidence_id)
        total_chars += len(text)
    return bounded


def build_current_book_evidence(
    book_id: str,
    question: str,
    chapter_locator: Optional[str] = None,
) -> dict[str, Any]:
    normalized_book_id = str(book_id or "").strip()
    normalized_question = str(question or "").strip()
    normalized_chapter = str(chapter_locator or "").strip()
    if not normalized_book_id:
        raise ValueError("book_id 不能为空")
    if not normalized_question:
        raise ValueError("question 不能为空")

    keywords = _transcript_keywords(normalized_question)
    search_keywords = _current_book_search_keywords(keywords)
    with db.connect() as conn:
        book_row = conn.execute(
            "SELECT id, title, author FROM reader.books WHERE id = %s",
            (normalized_book_id,),
        ).fetchone()
        if not book_row:
            raise CurrentBookNotFoundError(f"书不存在：{normalized_book_id}")
        book = dict(book_row)
        annotations = conn.execute(
            """
            SELECT * FROM reader.annotations
            WHERE book_id = %s
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 200
            """,
            (normalized_book_id,),
        ).fetchall()
        sentence_rows: list[Any] = []
        if normalized_chapter:
            sentence_rows.extend(
                conn.execute(
                    """
                    SELECT s.*, c.title AS chapter_title
                    FROM reader.sentences s
                    LEFT JOIN reader.chapters c ON c.id = s.chapter_id
                    WHERE s.book_id = %s AND s.chapter_locator = %s
                    ORDER BY s.sentence_index ASC
                    LIMIT 160
                    """,
                    (normalized_book_id, normalized_chapter),
                ).fetchall()
            )
        if search_keywords:
            sentence_rows.extend(
                conn.execute(
                    """
                    SELECT s.*, c.title AS chapter_title
                    FROM reader.sentences s
                    LEFT JOIN reader.chapters c ON c.id = s.chapter_id
                    WHERE s.book_id = %s
                      AND EXISTS (
                        SELECT 1
                        FROM unnest(%s::text[]) AS keyword
                        WHERE position(lower(keyword) in lower(s.text)) > 0
                      )
                    ORDER BY (
                      SELECT count(*)
                      FROM unnest(%s::text[]) AS keyword
                      WHERE position(lower(keyword) in lower(s.text)) > 0
                    ) DESC,
                    c.chapter_index ASC NULLS LAST,
                    s.sentence_index ASC
                    LIMIT 300
                    """,
                    (normalized_book_id, search_keywords, search_keywords),
                ).fetchall()
            )
        sentences_by_id: dict[str, dict[str, Any]] = {}
        for row in sentence_rows:
            item = dict(row)
            sentence_id = str(item.get("id") or "")
            if sentence_id and sentence_id not in sentences_by_id:
                sentences_by_id[sentence_id] = item
        sentences = list(sentences_by_id.values())

    ranked: list[tuple[tuple[int, int, int, int], dict[str, Any]]] = []
    ordinal = 0

    def add_candidate(item: dict[str, Any], *, source_priority: int) -> None:
        nonlocal ordinal
        text = str(item.get("text") or "").strip()
        keyword_score = _keyword_score(text, keywords)
        current_chapter = bool(
            normalized_chapter
            and str(item.get("chapter_locator") or "").strip() == normalized_chapter
        )
        if not current_chapter and keyword_score <= 0:
            return
        rank = (
            0 if current_chapter and keyword_score > 0 else 1 if current_chapter else 2,
            -keyword_score,
            source_priority,
            ordinal,
        )
        ranked.append((rank, item))
        ordinal += 1

    for row in annotations:
        annotation = dict(row)
        source_text = str(annotation.get("source_text") or "").strip()
        if source_text:
            add_candidate(
                _evidence(
                    source_type="annotation_source",
                    evidence_class="book_source",
                    target_id=str(annotation["id"]),
                    book=book,
                    text=source_text,
                    chapter_title=annotation.get("chapter_title"),
                    chapter_locator=annotation.get("chapter_locator"),
                    resolver={"kind": "annotation", "id": annotation["id"], "field": "source_text"},
                    review_state="source",
                ),
                source_priority=0,
            )
        note_text = str(annotation.get("note_text") or "").strip()
        if note_text:
            add_candidate(
                _evidence(
                    source_type="user_annotation",
                    evidence_class="user_annotation",
                    target_id=str(annotation["id"]),
                    book=book,
                    text=note_text,
                    chapter_title=annotation.get("chapter_title"),
                    chapter_locator=annotation.get("chapter_locator"),
                    resolver={"kind": "annotation", "id": annotation["id"], "field": "note_text"},
                    review_state="user_authored",
                ),
                source_priority=1,
            )

    for row in sentences:
        sentence = dict(row)
        text = str(sentence.get("text") or "").strip()
        if not text:
            continue
        add_candidate(
            _evidence(
                source_type="reader_sentence",
                evidence_class="book_source",
                target_id=str(sentence["id"]),
                book=book,
                text=text,
                chapter_title=sentence.get("chapter_title"),
                chapter_locator=sentence.get("chapter_locator"),
                resolver={"kind": "sentence", "id": sentence["id"]},
                review_state="source",
            ),
            source_priority=0,
        )

    for item in _living_book_evidence(book, _bundle_indexes()):
        add_candidate(item, source_priority=2)

    evidence_items = _bounded_current_book_evidence(
        [item for _, item in sorted(ranked, key=lambda candidate: candidate[0])]
    )
    total_chars = sum(len(str(item.get("text") or "")) for item in evidence_items)
    no_evidence_reason = None
    if not evidence_items:
        no_evidence_reason = "没有找到与问题关键词或当前章节直接匹配的可验证证据。"
    return {
        "schema": CURRENT_BOOK_EVIDENCE_SCHEMA,
        "book": {
            "id": str(book.get("id") or ""),
            "title": str(book.get("title") or ""),
            "author": str(book.get("author") or ""),
        },
        "chapter_locator": normalized_chapter or None,
        "evidence_items": evidence_items,
        "evidence_count": len(evidence_items),
        "total_chars": total_chars,
        "insufficient_evidence": not evidence_items,
        "no_evidence_reason": no_evidence_reason,
    }


def build_reading_candidates(
    voice_record_id: str,
    *,
    scope: str = "recent_reading",
    selected_book_ids: Optional[list[str]] = None,
) -> tuple[dict[str, Any], str, Optional[dict[str, Any]], list[dict[str, Any]]]:
    record = get_voice_record_or_404(voice_record_id)
    transcript, transcript_version = effective_transcript(voice_record_id)
    if not transcript.strip():
        raise ValueError("这条语音还没有可用于讨论的转写")
    if scope in {"record_only", "recent_voice"}:
        return record, transcript, transcript_version, []
    rows = _recent_book_rows(selected_book_ids or [], scope)
    bundles = _bundle_indexes()
    candidates = []
    for row in rows:
        evidence = _database_evidence(row, transcript)
        evidence.extend(_living_book_evidence(row, bundles))
        candidates.append(
            {
                "book_id": str(row["id"]),
                "title": row.get("title"),
                "author": row.get("author"),
                "recent_activity_at": row.get("recent_activity_at"),
                "explicitly_selected": bool(row.get("explicitly_selected")),
                "position": {
                    "chapter_id": row.get("chapter_id"),
                    "chapter_locator": row.get("chapter_locator"),
                    "updated_at": row.get("position_updated_at"),
                },
                "evidence": evidence[:MAX_EVIDENCE_PER_BOOK],
            }
        )
    return record, transcript, transcript_version, candidates


def context_request_hash(
    voice_record_id: str,
    *,
    scope: str,
    selected_book_ids: Optional[list[str]] = None,
) -> str:
    transcript, version = effective_transcript(voice_record_id)
    with db.connect() as conn:
        revisions = conn.execute(
            """
            SELECT
              (SELECT max(updated_at) FROM reader.books) AS books_at,
              (SELECT max(updated_at) FROM reader.reading_positions) AS positions_at,
              (SELECT max(updated_at) FROM reader.annotations) AS annotations_at
            """
        ).fetchone()
    return json_hash(
        {
            "schema": VOICE_READING_CONTEXT_SCHEMA,
            "algorithm_version": VOICE_READING_CONTEXT_ALGORITHM_VERSION,
            "voice_record_id": voice_record_id,
            "transcript_hash": str((version or {}).get("content_hash") or json_hash(transcript)),
            "scope": scope,
            "selected_book_ids": sorted(selected_book_ids or []),
            "reading_revisions": dict(revisions),
        }
    )


def build_context_material(
    voice_record_id: str,
    *,
    scope: str,
    selected_book_ids: Optional[list[str]] = None,
    adapter: Optional[VoiceHermesAdapter] = None,
) -> dict[str, Any]:
    record, transcript, transcript_version, candidates = build_reading_candidates(
        voice_record_id,
        scope=scope,
        selected_book_ids=selected_book_ids,
    )
    voice_evidence = _evidence(
        source_type="voice_transcript",
        evidence_class="user_voice",
        target_id=str((transcript_version or {}).get("id") or record["id"]),
        book={"id": None, "title": None, "author": None},
        text=transcript,
        resolver={
            "kind": "transcript_version" if transcript_version else "voice_record",
            "id": (transcript_version or {}).get("id") or record["id"],
        },
        review_state="user_edited" if (transcript_version or {}).get("version_type") == "user_edited" else "captured",
    )
    if not candidates:
        return {
            "record": record,
            "transcript_version": transcript_version,
            "requested_book_ids": sorted(selected_book_ids or []),
            "source_refs": [{"kind": "voice_record", "id": voice_record_id}],
            "evidence_items": [voice_evidence],
            "relevance_decisions": [],
            "selection_receipt": None,
            "no_relevant_reason": "当前范围没有候选书籍，因此没有加入阅读证据。",
        }
    receipt = (adapter or get_voice_hermes_adapter()).select_relevant_reading(
        voice_record_id=voice_record_id,
        transcript=transcript,
        candidates=candidates,
    )
    output = receipt["output"]
    selected_ids: list[str] = []
    for decision in output["decisions"]:
        if not decision["relevant"]:
            continue
        for evidence_id in decision["selected_evidence_ids"]:
            if evidence_id not in selected_ids:
                selected_ids.append(evidence_id)
    evidence_by_id = {
        item["evidence_id"]: item
        for candidate in candidates
        for item in candidate["evidence"]
    }
    selected_evidence = [evidence_by_id[evidence_id] for evidence_id in selected_ids]
    source_refs = [{"kind": "voice_record", "id": voice_record_id}]
    source_refs.extend(
        {
            "kind": "book_candidate",
            "id": candidate["book_id"],
            "title": candidate.get("title"),
            "recent_activity_at": candidate.get("recent_activity_at"),
            "explicitly_selected": candidate.get("explicitly_selected"),
        }
        for candidate in candidates
    )
    return {
        "record": record,
        "transcript_version": transcript_version,
        "requested_book_ids": sorted(selected_book_ids or []),
        "source_refs": source_refs,
        "evidence_items": [voice_evidence, *selected_evidence],
        "relevance_decisions": output["decisions"],
        "selection_receipt": receipt,
        "no_relevant_reason": output.get("no_relevant_reason"),
    }


def persist_context_snapshot(
    voice_record_id: str,
    *,
    scope: str,
    material: dict[str, Any],
    created_by_run_id: Optional[str],
) -> dict[str, Any]:
    receipt = material.get("selection_receipt") or {}
    return create_context_snapshot(
        voice_record_id=voice_record_id,
        scope=scope,
        source_refs=material["source_refs"],
        evidence_items=material["evidence_items"],
        relevance_decisions=material["relevance_decisions"],
        metadata={
            "schema": VOICE_READING_CONTEXT_SCHEMA,
            "algorithm_version": VOICE_READING_CONTEXT_ALGORITHM_VERSION,
            "selected_book_ids": material.get("requested_book_ids") or [],
            "candidate_count": max(0, len(material["source_refs"]) - 1),
            "reading_evidence_count": max(0, len(material["evidence_items"]) - 1),
            "no_relevant_reason": material.get("no_relevant_reason"),
            "adapter_version": receipt.get("adapter_version"),
            "provider": receipt.get("provider"),
            "model": receipt.get("model"),
            "session_id": receipt.get("session_id"),
        },
        created_by_run_id=created_by_run_id,
    )


def context_snapshot_matches(
    snapshot: dict[str, Any],
    *,
    scope: str,
    selected_book_ids: Optional[list[str]] = None,
) -> bool:
    if str(snapshot.get("scope") or "") != scope:
        return False
    if scope != "selected_books":
        return True
    metadata = snapshot.get("metadata") if isinstance(snapshot.get("metadata"), dict) else {}
    snapshot_book_ids = metadata.get("selected_book_ids")
    if not isinstance(snapshot_book_ids, list):
        snapshot_book_ids = [
            ref.get("id")
            for ref in snapshot.get("source_refs") or []
            if ref.get("kind") == "book_candidate" and ref.get("id")
        ]
    return sorted(str(book_id) for book_id in snapshot_book_ids) == sorted(
        str(book_id) for book_id in (selected_book_ids or [])
    )


def resolve_snapshot_citation(snapshot_id: str, evidence_id: str) -> dict[str, Any]:
    snapshot = get_context_snapshot(snapshot_id)
    evidence = next(
        (item for item in snapshot.get("evidence_items") or [] if str(item.get("evidence_id")) == evidence_id),
        None,
    )
    if not evidence:
        raise CitationResolutionError("这条引用不在指定的上下文快照中")
    resolver = evidence.get("resolver") if isinstance(evidence.get("resolver"), dict) else {}
    kind = resolver.get("kind")
    target: Optional[dict[str, Any]] = None
    current_text = ""
    if kind == "annotation":
        with db.connect() as conn:
            row = conn.execute(
                """
                SELECT a.*, b.title AS book_title, b.author AS book_author
                FROM reader.annotations a JOIN reader.books b ON b.id = a.book_id
                WHERE a.id = %s
                """,
                (resolver.get("id"),),
            ).fetchone()
        if row:
            target = dict(row)
            current_text = str(target.get(str(resolver.get("field") or "source_text")) or "").strip()
    elif kind == "sentence":
        with db.connect() as conn:
            row = conn.execute(
                """
                SELECT s.*, c.title AS chapter_title, b.title AS book_title, b.author AS book_author
                FROM reader.sentences s
                JOIN reader.books b ON b.id = s.book_id
                LEFT JOIN reader.chapters c ON c.id = s.chapter_id
                WHERE s.id = %s
                """,
                (resolver.get("id"),),
            ).fetchone()
        if row:
            target = dict(row)
            current_text = str(target.get("text") or "").strip()
    elif kind == "transcript_version":
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM reader.voice_transcript_versions WHERE id = %s", (resolver.get("id"),)).fetchone()
        if row:
            target = dict(row)
            current_text = str(target.get("content") or "").strip()
    elif kind == "voice_record":
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM reader.voice_records WHERE id = %s", (resolver.get("id"),)).fetchone()
        if row:
            target = dict(row)
            current_text = str(target.get("transcript") or "").strip()
    elif kind == "living_book_asset":
        path = Path(str(resolver.get("path") or "")).expanduser()
        if _inside_root(path, knowledge_base_root()) and path.is_file():
            try:
                current_text = path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                current_text = ""
            if current_text:
                target = {"path": str(path), "relative_path": resolver.get("relative_path")}
    if target is None:
        raise CitationResolutionError("引用目标已经不存在；快照中的原证据仍保留，但不能冒充可回溯引用")
    snapshot_text = str(evidence.get("text") or "")
    current_matches = (
        snapshot_text == current_text
        if kind != "living_book_asset"
        else str(resolver.get("file_hash") or "") == hashlib.sha256(current_text.encode("utf-8")).hexdigest()
    )
    return {
        "ok": True,
        "schema": VOICE_CITATION_SCHEMA,
        "snapshot_id": snapshot_id,
        "evidence": evidence,
        "target": target,
        "current_matches_snapshot": current_matches,
        "current_text": current_text if not current_matches else snapshot_text,
    }
