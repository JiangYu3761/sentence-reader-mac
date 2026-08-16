from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any, Optional

from reader_api import db
from reader_api.voice_inbox_service import (
    VOICE_RECORD_SCHEMA,
    ensure_raw_transcript_version,
    file_hash,
    insert_processing_run,
    json_hash,
    json_safe,
    seconds_to_ms,
    stable_id,
    upsert_voice_record,
    voice_record_by_id,
)


READER_AUDIO_NOTE_SOURCE = "platform_forward"
READER_AUDIO_NOTE_ORIGIN_PREFIX = "reader_audio_note:"
READER_AUDIO_NOTE_PLATFORM = "Click Reader 语音备注"

_PRESERVED_STATUSES = {
    "transcribing",
    "cleanup_pending",
    "cleaning",
    "cleaned",
    "understand_pending",
    "understanding",
    "needs_user_confirmation",
    "needs_followup",
    "action_pending",
    "action_applied",
    "settled",
    "archived",
    "failed_cleanup",
    "failed_understand",
    "failed_action",
}


def reader_audio_note_origin_ref(audio_note_id: str) -> str:
    return f"{READER_AUDIO_NOTE_ORIGIN_PREFIX}{audio_note_id}"


def _audio_note_row(conn: Any, audio_note_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        """
        SELECT
          an.*,
          b.title AS book_title,
          b.author AS book_author,
          a.source_text AS annotation_source_text,
          a.note_text AS annotation_note_text,
          a.chapter_title AS annotation_chapter_title,
          a.chapter_locator AS annotation_chapter_locator
        FROM reader.audio_notes an
        JOIN reader.books b ON b.id = an.book_id
        LEFT JOIN reader.annotations a ON a.id = an.annotation_id
        WHERE an.id = %s
        """,
        (audio_note_id,),
    ).fetchone()
    return dict(row) if row else None


def _existing_projection(conn: Any, origin_ref: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        """
        SELECT vr.*, vtv.version_type AS active_version_type
        FROM reader.voice_records vr
        LEFT JOIN reader.voice_transcript_versions vtv
          ON vtv.id = vr.active_transcript_version_id
        WHERE vr.source = %s AND vr.origin_ref = %s
        LIMIT 1
        """,
        (READER_AUDIO_NOTE_SOURCE, origin_ref),
    ).fetchone()
    return dict(row) if row else None


def _mime_type(audio_path: Path) -> str:
    return {
        ".m4a": "audio/mp4",
        ".mp4": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".webm": "audio/webm",
        ".ogg": "audio/ogg",
        ".aac": "audio/aac",
    }.get(audio_path.suffix.lower(), mimetypes.guess_type(str(audio_path))[0] or "application/octet-stream")


def _projection_status(
    audio_note: dict[str, Any],
    *,
    file_exists: bool,
    existing: Optional[dict[str, Any]],
) -> tuple[str, Optional[str], Optional[str]]:
    existing_status = str((existing or {}).get("status") or "")
    if existing_status in _PRESERVED_STATUSES:
        return existing_status, existing.get("failure_code"), existing.get("failure_message")
    if not file_exists:
        return (
            "failed_upload",
            "reader_audio_missing",
            "Click 阅读语音的原始音频文件不在当前路径。记录和已有文字仍保留。",
        )
    transcript = str(audio_note.get("transcript") or "").strip()
    if transcript:
        return "transcribed", None, None
    if str(audio_note.get("status") or "") == "failed":
        detail = str(audio_note.get("error_message") or "").strip()
        message = "Click 阅读语音转写失败。原始音频仍保留，可以在这里重试。"
        if detail:
            message = f"Click 阅读语音转写失败：{detail}。原始音频仍保留，可以在这里重试。"
        return "failed_transcribe", "reader_audio_transcribe_failed", message
    return "transcribe_pending", None, None


def _projection_title(audio_note: dict[str, Any]) -> str:
    book_title = str(audio_note.get("book_title") or "").strip()
    return f"《{book_title}》阅读语音" if book_title else "Click 阅读语音"


def _projection_metadata(audio_note: dict[str, Any], *, file_exists: bool, title: str) -> dict[str, Any]:
    book_id = str(audio_note.get("book_id") or "")
    annotation_id = str(audio_note.get("annotation_id") or "")
    return {
        "schema": VOICE_RECORD_SCHEMA,
        "primary_store": "reader.voice_records",
        "projection_source_store": "reader.audio_notes",
        "origin_kind": "reader_audio_note",
        "reader_audio_note_id": str(audio_note.get("id") or ""),
        "reader_audio_note_status": str(audio_note.get("status") or ""),
        "reader_audio_note_provider": str(audio_note.get("provider") or ""),
        "audio_file_exists": file_exists,
        "projection_title": title,
        "book_id": book_id,
        "book_title": str(audio_note.get("book_title") or ""),
        "book_author": str(audio_note.get("book_author") or ""),
        "annotation_id": annotation_id,
        "chapter_title": str(audio_note.get("annotation_chapter_title") or ""),
        "chapter_locator": str(audio_note.get("annotation_chapter_locator") or ""),
        "annotation_source_text": str(audio_note.get("annotation_source_text") or "")[:2_000],
        "annotation_note_text": str(audio_note.get("annotation_note_text") or "")[:2_000],
        "click_reader_url": f"sentence-reader://open-native?book_id={book_id}" if book_id else "",
    }


def _upsert_link(
    conn: Any,
    *,
    voice_record_id: str,
    target_type: str,
    target_id: str,
    metadata: dict[str, Any],
) -> None:
    if not target_id:
        return
    conn.execute(
        """
        INSERT INTO reader.voice_record_links (
          id, voice_record_id, target_type, target_id, metadata, created_at
        )
        VALUES (%s, %s, %s, %s, %s, now())
        ON CONFLICT (voice_record_id, target_type, target_id) DO UPDATE SET
          metadata = EXCLUDED.metadata
        """,
        (
            stable_id("vlink", voice_record_id, target_type, target_id),
            voice_record_id,
            target_type,
            target_id,
            db.jsonb(json_safe(metadata)),
        ),
    )


def _project_audio_note(conn: Any, audio_note: dict[str, Any]) -> dict[str, Any]:
    audio_note_id = str(audio_note["id"])
    origin_ref = reader_audio_note_origin_ref(audio_note_id)
    existing = _existing_projection(conn, origin_ref)
    if existing and existing.get("deleted_at") is not None:
        return existing

    audio_path = Path(str(audio_note.get("audio_path") or "")).expanduser()
    file_exists = audio_path.is_file()
    transcript = str(audio_note.get("transcript") or "").strip()
    generated_title = _projection_title(audio_note)
    previous_metadata = (existing or {}).get("metadata") or {}
    if not isinstance(previous_metadata, dict):
        previous_metadata = {}
    title = generated_title
    if existing and str(existing.get("title") or "") != str(previous_metadata.get("projection_title") or ""):
        title = str(existing.get("title") or generated_title)

    status, failure_code, failure_message = _projection_status(
        audio_note,
        file_exists=file_exists,
        existing=existing,
    )
    stored_hash = str(audio_note.get("audio_hash") or "").strip()
    if stored_hash:
        audio_hash = stored_hash
    elif file_exists:
        audio_hash = file_hash(audio_path)
    else:
        audio_hash = json_hash({"audio_note_id": audio_note_id, "audio_path": str(audio_path), "missing": True})

    record_transcript = transcript or None
    if existing and str(existing.get("active_version_type") or "") == "user_edited":
        record_transcript = str(existing.get("transcript") or "") or transcript or None

    row = upsert_voice_record(
        conn,
        voice_record_id=stable_id("vr", READER_AUDIO_NOTE_SOURCE, origin_ref),
        title=title,
        source=READER_AUDIO_NOTE_SOURCE,
        source_platform=READER_AUDIO_NOTE_PLATFORM,
        device_id=None,
        origin_ref=origin_ref,
        audio_uri=str(audio_path),
        audio_hash=audio_hash,
        duration_ms=seconds_to_ms(audio_note.get("duration_seconds")),
        mime_type=_mime_type(audio_path),
        status=status,
        transcript=record_transcript,
        summary=(existing or {}).get("summary"),
        intent_type=str((existing or {}).get("intent_type") or "reading_note"),
        evidence_spans=list((existing or {}).get("evidence_spans") or []),
        failure_code=failure_code,
        failure_message=failure_message,
        metadata=_projection_metadata(audio_note, file_exists=file_exists, title=generated_title),
    )
    conn.execute(
        """
        UPDATE reader.voice_records
        SET created_at = LEAST(created_at, %s),
            recorded_at = COALESCE(recorded_at, %s),
            updated_at = GREATEST(updated_at, %s)
        WHERE id = %s
        """,
        (audio_note.get("created_at"), audio_note.get("created_at"), audio_note.get("updated_at"), row["id"]),
    )

    if transcript:
        ensure_raw_transcript_version(
            conn,
            voice_record_id=str(row["id"]),
            content=transcript,
            provider=str(audio_note.get("provider") or "reader_audio_note"),
        )

    _upsert_link(
        conn,
        voice_record_id=str(row["id"]),
        target_type="book",
        target_id=str(audio_note.get("book_id") or ""),
        metadata={"book_title": audio_note.get("book_title"), "origin_kind": "reader_audio_note"},
    )
    _upsert_link(
        conn,
        voice_record_id=str(row["id"]),
        target_type="annotation",
        target_id=str(audio_note.get("annotation_id") or ""),
        metadata={
            "book_id": audio_note.get("book_id"),
            "chapter_title": audio_note.get("annotation_chapter_title"),
            "chapter_locator": audio_note.get("annotation_chapter_locator"),
            "origin_kind": "reader_audio_note",
        },
    )

    projection_run = conn.execute(
        """
        SELECT id
        FROM reader.voice_processing_runs
        WHERE voice_record_id = %s
          AND provider = 'click_reader_audio_note_projection'
          AND request_payload->>'audio_note_id' = %s
        LIMIT 1
        """,
        (row["id"], audio_note_id),
    ).fetchone()
    if not projection_run:
        insert_processing_run(
            conn,
            voice_record_id=str(row["id"]),
            run_type="upload",
            provider="click_reader_audio_note_projection",
            status="success" if file_exists else "failed",
            request_payload={
                "audio_note_id": audio_note_id,
                "audio_uri": str(audio_path),
                "source_store": "reader.audio_notes",
            },
            response_payload={
                "voice_record_id": row["id"],
                "same_audio_path": True,
                "audio_file_exists": file_exists,
            },
            failure_reason=None if file_exists else failure_message,
        )
    return voice_record_by_id(conn, str(row["id"])) or row


def project_reader_audio_note_to_voice_inbox(audio_note_id: str) -> dict[str, Any]:
    with db.connect() as conn:
        audio_note = _audio_note_row(conn, audio_note_id)
        if not audio_note:
            raise LookupError(f"reader audio note not found: {audio_note_id}")
        return _project_audio_note(conn, audio_note)


def reconcile_reader_audio_notes_to_voice_inbox() -> dict[str, Any]:
    with db.connect() as conn:
        rows = conn.execute("SELECT id FROM reader.audio_notes ORDER BY created_at ASC, id ASC").fetchall()
    projected = 0
    skipped_deleted = 0
    failures: list[dict[str, str]] = []
    for row in rows:
        audio_note_id = str(row["id"])
        try:
            record = project_reader_audio_note_to_voice_inbox(audio_note_id)
            if record.get("deleted_at") is not None:
                skipped_deleted += 1
            else:
                projected += 1
        except Exception as exc:  # noqa: BLE001 - one damaged legacy note must not block the remaining inbox.
            failures.append({"audio_note_id": audio_note_id, "error": str(exc)})
    return {
        "ok": not failures,
        "source_store": "reader.audio_notes",
        "projected": projected,
        "skipped_deleted": skipped_deleted,
        "failures": failures,
    }
