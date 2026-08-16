from __future__ import annotations

import hashlib
import json
import re
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from fastapi import HTTPException

from reader_api import db


VOICE_RECORD_SCHEMA = "click.voice_inbox.record.v1"
VOICE_PROCESSING_RUN_SCHEMA = "click.voice_inbox.processing_run.v1"
VOICE_ACTION_SCHEMA = "click.voice_inbox.action.v1"
VOICE_TRANSCRIPT_VERSION_SCHEMA = "click.voice_inbox.transcript_version.v1"
VOICE_PROCESSING_JOB_SCHEMA = "click.voice_inbox.processing_job.v1"
VOICE_CONTEXT_SNAPSHOT_SCHEMA = "click.voice_inbox.context_snapshot.v1"
VOICE_DISCUSSION_SCHEMA = "click.voice_inbox.discussion.v1"
VOICE_DISCUSSION_MESSAGE_SCHEMA = "click.voice_inbox.discussion_message.v1"

VOICE_SOURCES = {"mac", "click_mobile", "import", "platform_forward", "browser", "future"}
VOICE_INTENT_TYPES = {
    "task",
    "idea",
    "question",
    "review",
    "reading_note",
    "meeting_note",
    "material",
    "memo",
    "unknown",
}
VOICE_RECORD_STATUSES = {
    "recording",
    "saved_local",
    "upload_pending",
    "uploading",
    "uploaded",
    "transcribe_pending",
    "transcribing",
    "transcribed",
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
    "failed_upload",
    "failed_transcribe",
    "failed_cleanup",
    "failed_understand",
    "failed_action",
    "deleted",
}
VOICE_ACTION_TYPES = {
    "create_note",
    "create_task",
    "append_reading_note",
    "create_review_item",
    "add_to_knowledge_base",
    "ask_followup",
    "archive",
    "no_action",
}
VOICE_ACTION_STATUSES = {
    "proposed",
    "pending_user_confirmation",
    "applying",
    "auto_applied",
    "applied",
    "validating",
    "rejected",
    "superseded",
    "failed_apply",
    "validated",
}
VOICE_RUN_TYPES = {
    "upload",
    "transcribe",
    "clean_transcript",
    "understand",
    "build_context",
    "discuss",
    "title_summary",
    "action_extract",
    "apply_action",
    "validate",
}
VOICE_TRANSCRIPT_VERSION_TYPES = {"asr_raw", "hermes_cleaned", "user_edited"}
VOICE_PROCESSING_JOB_TYPES = {
    "transcribe",
    "clean_transcript",
    "understand",
    "build_context",
    "prepare_discussion",
    "apply_action",
    "validate_action",
}
VOICE_CONTEXT_SCOPES = {"record_only", "recent_reading", "selected_books", "recent_voice"}
VOICE_DISCUSSION_MESSAGE_ROLES = {"user", "assistant", "system"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def stable_id(prefix: str, *parts: Any) -> str:
    raw = "\0".join(str(part or "") for part in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def json_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    return str(value)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_source(value: Optional[str], fallback: str = "mac") -> str:
    source = re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")
    if source in {"mobile", "mobile_app", "mobile_click", "click_app", "click_mobile_app", "android", "ios", "ipad", "iphone"}:
        return "click_mobile"
    if source in VOICE_SOURCES:
        return source
    return fallback


def safe_intent_type(value: Optional[str]) -> str:
    intent = re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")
    return intent if intent in VOICE_INTENT_TYPES else "unknown"


def safe_record_status(value: str) -> str:
    if value not in VOICE_RECORD_STATUSES:
        raise HTTPException(status_code=422, detail="unsupported voice record status")
    return value


def safe_action_type(value: Optional[str]) -> str:
    action_type = re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")
    return action_type if action_type in VOICE_ACTION_TYPES else "no_action"


def safe_action_status(value: str) -> str:
    if value not in VOICE_ACTION_STATUSES:
        raise HTTPException(status_code=422, detail="unsupported voice action status")
    return value


def safe_run_type(value: str) -> str:
    if value not in VOICE_RUN_TYPES:
        raise HTTPException(status_code=422, detail="unsupported voice processing run type")
    return value


def safe_transcript_version_type(value: str) -> str:
    if value not in VOICE_TRANSCRIPT_VERSION_TYPES:
        raise HTTPException(status_code=422, detail="unsupported voice transcript version type")
    return value


def safe_processing_job_type(value: str) -> str:
    if value not in VOICE_PROCESSING_JOB_TYPES:
        raise HTTPException(status_code=422, detail="unsupported voice processing job type")
    return value


def safe_context_scope(value: Optional[str]) -> str:
    scope = str(value or "recent_reading").strip().lower()
    if scope not in VOICE_CONTEXT_SCOPES:
        raise HTTPException(status_code=422, detail="unsupported voice context scope")
    return scope


def clean_title(value: Optional[str], fallback: str = "未命名语音") -> str:
    title = re.sub(r"\s+", " ", str(value or "").strip()).strip('"“”')
    return title[:80] if title else fallback


def seconds_to_ms(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    return int(seconds * 1000)


def audio_duration_ms(audio_path: Path, fallback: Optional[int] = None) -> Optional[int]:
    if audio_path.suffix.lower() != ".wav":
        return fallback
    try:
        with wave.open(str(audio_path), "rb") as audio:
            sample_rate = audio.getframerate()
            if sample_rate <= 0:
                return fallback
            return max(0, int(round(audio.getnframes() * 1000 / sample_rate)))
    except (OSError, EOFError, wave.Error):
        return fallback


def category_to_intent(category: Optional[str]) -> str:
    value = str(category or "").strip()
    mapping = {
        "任务": "task",
        "想法": "idea",
        "灵感": "idea",
        "项目": "idea",
        "读书": "reading_note",
        "待整理": "unknown",
    }
    return mapping.get(value, "unknown")


def sqlite_status_to_voice_status(status: Optional[str], transcript: str = "") -> str:
    value = str(status or "").strip()
    if value == "named":
        return "needs_user_confirmation"
    if value == "transcribed":
        return "understand_pending"
    if value == "transcribed_needs_naming":
        return "failed_understand"
    if value == "needs_processing":
        return "failed_transcribe" if not transcript else "failed_understand"
    if value == "saved":
        return "transcribe_pending"
    return "saved_local"


def voice_record_by_id(conn: Any, voice_record_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM reader.voice_records WHERE id = %s", (voice_record_id,)).fetchone()
    return dict(row) if row else None


def list_voice_records(
    *,
    status: Optional[str] = None,
    source: Optional[str] = None,
    include_deleted: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses = []
    params: list[Any] = []
    if not include_deleted:
        clauses.append("deleted_at IS NULL")
    if status:
        clauses.append("status = %s")
        params.append(status)
    if source:
        clauses.append("source = %s")
        params.append(safe_source(source))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(limit, 500)))
    with db.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM reader.voice_records
            {where}
            ORDER BY COALESCE(recorded_at, created_at) DESC, created_at DESC, id DESC
            LIMIT %s
            """,
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


def insert_processing_run(
    conn: Any,
    *,
    voice_record_id: str,
    run_type: str,
    provider: str,
    status: str,
    called_hermes: bool = False,
    model: Optional[str] = None,
    request_payload: Optional[dict[str, Any]] = None,
    response_payload: Optional[dict[str, Any]] = None,
    fallback_used: bool = False,
    failure_reason: Optional[str] = None,
    created_actions: Optional[list[dict[str, Any]]] = None,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
) -> dict[str, Any]:
    request_payload = json_safe(request_payload or {})
    response_payload = json_safe(response_payload or {})
    created_actions = json_safe(created_actions or [])
    run_id = new_id("vpr")
    started_value = started_at or now_iso()
    finished_value = finished_at or now_iso()
    try:
        started_dt = datetime.fromisoformat(started_value.replace("Z", "+00:00"))
        finished_dt = datetime.fromisoformat(finished_value.replace("Z", "+00:00"))
        duration_seconds: Optional[float] = max(0.0, (finished_dt - started_dt).total_seconds())
    except Exception:
        duration_seconds = None
    row = conn.execute(
        """
        INSERT INTO reader.voice_processing_runs (
            id, voice_record_id, run_type, provider, called_hermes, model,
            input_hash, prompt_hash, output_hash, status, started_at, finished_at,
            duration_seconds, fallback_used, failure_reason, request_payload,
            response_payload, created_actions, created_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        RETURNING *
        """,
        (
            run_id,
            voice_record_id,
            safe_run_type(run_type),
            provider,
            bool(called_hermes),
            model,
            json_hash(request_payload),
            json_hash(request_payload.get("prompt", request_payload)),
            json_hash(response_payload),
            status,
            started_value,
            finished_value,
            duration_seconds,
            bool(fallback_used),
            failure_reason,
            db.jsonb(request_payload),
            db.jsonb(response_payload),
            db.jsonb(created_actions),
        ),
    ).fetchone()
    conn.execute(
        "UPDATE reader.voice_records SET latest_processing_run_id = %s, updated_at = now() WHERE id = %s",
        (run_id, voice_record_id),
    )
    return dict(row)


def upsert_voice_record(
    conn: Any,
    *,
    voice_record_id: str,
    title: str,
    source: str,
    source_platform: Optional[str],
    device_id: Optional[str],
    origin_ref: Optional[str],
    audio_uri: str,
    audio_hash: str,
    duration_ms: Optional[int],
    mime_type: Optional[str],
    status: str,
    transcript: Optional[str] = None,
    summary: Optional[str] = None,
    intent_type: Optional[str] = None,
    evidence_spans: Optional[list[dict[str, Any]]] = None,
    failure_code: Optional[str] = None,
    failure_message: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    evidence_spans = evidence_spans or []
    metadata = metadata or {}
    resolved_source = safe_source(source)
    assert_voice_record_not_tombstoned(
        conn,
        source=resolved_source,
        origin_ref=origin_ref,
        audio_hash=audio_hash,
        client_capture_id=str(metadata.get("client_capture_id") or ""),
    )
    row = conn.execute(
        """
        INSERT INTO reader.voice_records (
            id, title, source, source_platform, device_id, origin_ref, audio_uri,
            audio_hash, duration_ms, mime_type, language, transcript, summary,
            intent_type, evidence_spans, status, failure_code, failure_message,
            metadata, created_at, updated_at, recorded_at, uploaded_at,
            transcribed_at, processed_at
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'zh', %s, %s, %s, %s, %s,
            %s, %s, %s, now(), now(), now(), now(),
            CASE WHEN %s::text IS NOT NULL THEN now() ELSE NULL END,
            CASE WHEN %s::text IS NOT NULL THEN now() ELSE NULL END
        )
        ON CONFLICT (source, origin_ref) WHERE origin_ref IS NOT NULL DO UPDATE SET
            title = EXCLUDED.title,
            source_platform = EXCLUDED.source_platform,
            device_id = COALESCE(EXCLUDED.device_id, reader.voice_records.device_id),
            audio_uri = EXCLUDED.audio_uri,
            audio_hash = EXCLUDED.audio_hash,
            duration_ms = EXCLUDED.duration_ms,
            mime_type = EXCLUDED.mime_type,
            transcript = COALESCE(EXCLUDED.transcript, reader.voice_records.transcript),
            summary = COALESCE(EXCLUDED.summary, reader.voice_records.summary),
            intent_type = EXCLUDED.intent_type,
            evidence_spans = EXCLUDED.evidence_spans,
            status = EXCLUDED.status,
            failure_code = EXCLUDED.failure_code,
            failure_message = EXCLUDED.failure_message,
            metadata = reader.voice_records.metadata || EXCLUDED.metadata,
            transcribed_at = COALESCE(reader.voice_records.transcribed_at, EXCLUDED.transcribed_at),
            processed_at = COALESCE(reader.voice_records.processed_at, EXCLUDED.processed_at),
            updated_at = now()
        RETURNING *
        """,
        (
            voice_record_id,
            clean_title(title),
            resolved_source,
            source_platform,
            device_id,
            origin_ref,
            audio_uri,
            audio_hash,
            duration_ms,
            mime_type,
            transcript,
            summary,
            safe_intent_type(intent_type),
            db.jsonb(json_safe(evidence_spans)),
            safe_record_status(status),
            failure_code,
            failure_message,
            db.jsonb(json_safe(metadata)),
            transcript,
            summary,
        ),
    ).fetchone()
    return dict(row)


def assert_voice_record_not_tombstoned(
    conn: Any,
    *,
    source: str,
    origin_ref: Optional[str],
    audio_hash: Optional[str],
    client_capture_id: Optional[str],
) -> None:
    resolved_source = safe_source(source)
    if resolved_source not in {"mac", "click_mobile"}:
        return
    resolved_origin = str(origin_ref or "")
    resolved_audio_hash = str(audio_hash or "")
    resolved_capture_id = str(client_capture_id or "")
    tombstone = conn.execute(
        """
        SELECT id
        FROM reader.tingle_tombstones
        WHERE kind = 'voice'
          AND (
            (%s <> '' AND voice_source = %s AND origin_ref = %s)
            OR (%s <> '' AND audio_hash = %s)
            OR (%s <> '' AND client_capture_id = %s)
          )
        LIMIT 1
        """,
        (
            resolved_origin,
            resolved_source,
            resolved_origin,
            resolved_audio_hash,
            resolved_audio_hash,
            resolved_capture_id,
            resolved_capture_id,
        ),
    ).fetchone()
    if tombstone:
        raise HTTPException(
            status_code=409,
            detail="这条录音已被永久删除，不能从本地缓存重新同步",
        )


def create_voice_record_from_audio(
    *,
    audio_path: Path,
    audio_hash: Optional[str],
    duration_ms: Optional[int],
    mime_type: str,
    source: str,
    source_platform: Optional[str] = None,
    device_id: Optional[str] = None,
    origin_ref: Optional[str] = None,
    title: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if not audio_path.exists() or not audio_path.is_file():
        raise HTTPException(status_code=422, detail="audio file is missing")
    resolved_hash = audio_hash or file_hash(audio_path)
    resolved_duration_ms = audio_duration_ms(audio_path, duration_ms)
    voice_record_id = stable_id("vr", source, origin_ref or resolved_hash)
    with db.connect() as conn:
        row = upsert_voice_record(
            conn,
            voice_record_id=voice_record_id,
            title=title or "新语音",
            source=safe_source(source),
            source_platform=source_platform,
            device_id=device_id,
            origin_ref=origin_ref,
            audio_uri=str(audio_path),
            audio_hash=resolved_hash,
            duration_ms=resolved_duration_ms,
            mime_type=mime_type,
            status="transcribe_pending",
            metadata={**(metadata or {}), "schema": VOICE_RECORD_SCHEMA},
        )
        insert_processing_run(
            conn,
            voice_record_id=row["id"],
            run_type="upload",
            provider="click_voice_inbox",
            status="success",
            request_payload={"audio_uri": str(audio_path), "source": safe_source(source), "origin_ref": origin_ref},
            response_payload={"voice_record_id": row["id"], "audio_hash": resolved_hash},
        )
    return row


def upsert_voice_record_from_recording(
    record: dict[str, Any],
    *,
    source_hint: Optional[str] = None,
    device_id: Optional[str] = None,
) -> dict[str, Any]:
    transcript_path = Path(str(record.get("transcript_path") or ""))
    transcript = transcript_path.read_text(encoding="utf-8") if transcript_path.exists() else ""
    source = safe_source(source_hint or record.get("source") or "click_mobile", fallback="click_mobile")
    if "reader" in str(record.get("source_feature") or "").lower():
        source = "click_mobile"
    voice_status = sqlite_status_to_voice_status(str(record.get("status") or ""), transcript)
    title = str(record.get("title") or record.get("provisional_title") or "录音")
    metadata = {
        "schema": VOICE_RECORD_SCHEMA,
        "primary_store": "reader.voice_records",
        "capture_projection": "local_recordings_sqlite",
        "legacy_schema": record.get("schema"),
        "recording_id": record.get("recording_id"),
        "recording_status": record.get("status"),
        "category": record.get("category"),
        "tags": record.get("tags") or [],
        "organized_status": record.get("organized_status"),
        "storage_mode": record.get("storage_mode", "canonical"),
    }
    with db.connect() as conn:
        existing = conn.execute(
            """
            SELECT *
            FROM reader.voice_records
            WHERE source = %s AND origin_ref = %s AND deleted_at IS NULL
            LIMIT 1
            """,
            (source, str(record.get("recording_id") or "")),
        ).fetchone()
        existing_row = dict(existing) if existing else None
        user_title_override = bool(record.get("user_title_override"))
        user_category_override = bool(record.get("user_category_override"))
        row = upsert_voice_record(
            conn,
            voice_record_id=stable_id("vr", source, record.get("recording_id")),
            title=title if user_title_override or not existing_row else str(existing_row["title"]),
            source=source,
            source_platform=f"{record.get('source_app') or 'Click'} / {record.get('source_feature') or 'Recording'}",
            device_id=device_id,
            origin_ref=str(record.get("recording_id") or ""),
            audio_uri=str(record.get("audio_path") or ""),
            audio_hash=str(record.get("audio_hash") or ""),
            duration_ms=audio_duration_ms(
                Path(str(record.get("audio_path") or "")),
                seconds_to_ms(record.get("duration_seconds")),
            ),
            mime_type=str(record.get("mime_type") or ""),
            status=str(existing_row["status"]) if existing_row else voice_status,
            transcript=transcript or None,
            summary=(
                str(record.get("summary") or "") or None
                if not existing_row
                else existing_row.get("summary")
            ),
            intent_type=(
                category_to_intent(record.get("category"))
                if user_category_override or not existing_row
                else str(existing_row.get("intent_type") or "unknown")
            ),
            evidence_spans=list(existing_row.get("evidence_spans") or []) if existing_row else None,
            failure_code=(
                existing_row.get("failure_code")
                if existing_row
                else "recording_processor_pending" if "failed" in voice_status else None
            ),
            failure_message=(
                existing_row.get("failure_message")
                if existing_row
                else str(record.get("error_message") or "") or None
            ),
            metadata=metadata,
        )
        upload_run = conn.execute(
            """
            SELECT id
            FROM reader.voice_processing_runs
            WHERE voice_record_id = %s AND run_type = 'upload' AND status = 'success'
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (row["id"],),
        ).fetchone()
        if not upload_run:
            insert_processing_run(
                conn,
                voice_record_id=str(row["id"]),
                run_type="upload",
                provider="click_mobile_capture",
                status="success",
                request_payload={
                    "recording_id": record.get("recording_id"),
                    "source": source,
                    "device_id": device_id,
                    "audio_uri": record.get("audio_path"),
                    "audio_hash": record.get("audio_hash"),
                },
                response_payload={
                    "voice_record_id": row["id"],
                    "primary_store": "reader.voice_records",
                    "capture_projection": "local_recordings_sqlite",
                },
            )
        if transcript:
            ensure_raw_transcript_version(
                conn,
                voice_record_id=row["id"],
                content=transcript,
                provider=str((record.get("metadata") or {}).get("voice_pipeline", {}).get("asr_engine") or "funasr-local"),
            )
            row = voice_record_by_id(conn, row["id"]) or row
        return row


def get_voice_record_or_404(voice_record_id: str) -> dict[str, Any]:
    with db.connect() as conn:
        row = voice_record_by_id(conn, voice_record_id)
    if not row:
        raise HTTPException(status_code=404, detail="voice record not found")
    return row


def list_actions_for_record(voice_record_id: str) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM reader.voice_actions
            WHERE voice_record_id = %s
            ORDER BY created_at DESC
            """,
            (voice_record_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_action_receipts(voice_record_id: str) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM reader.voice_action_receipts
            WHERE voice_record_id = %s
            ORDER BY created_at DESC
            """,
            (voice_record_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_voice_record_links(voice_record_id: str) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM reader.voice_record_links
            WHERE voice_record_id = %s
            ORDER BY created_at DESC
            """,
            (voice_record_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_runs_for_record(voice_record_id: str) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM reader.voice_processing_runs
            WHERE voice_record_id = %s
            ORDER BY created_at DESC
            """,
            (voice_record_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def transcript_version_by_id(conn: Any, version_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM reader.voice_transcript_versions WHERE id = %s",
        (version_id,),
    ).fetchone()
    return dict(row) if row else None


def active_transcript_version(conn: Any, voice_record_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        """
        SELECT vtv.*
        FROM reader.voice_records vr
        JOIN reader.voice_transcript_versions vtv
          ON vtv.id = vr.active_transcript_version_id
        WHERE vr.id = %s
        """,
        (voice_record_id,),
    ).fetchone()
    return dict(row) if row else None


def create_transcript_version(
    conn: Any,
    *,
    voice_record_id: str,
    version_type: str,
    content: str,
    provider: str,
    parent_version_id: Optional[str] = None,
    processing_run_id: Optional[str] = None,
    changes: Optional[list[dict[str, Any]]] = None,
    confidence: Optional[float] = None,
    meaning_changed: bool = False,
    needs_review: bool = False,
    metadata: Optional[dict[str, Any]] = None,
    activate: bool = True,
) -> dict[str, Any]:
    normalized_type = safe_transcript_version_type(version_type)
    normalized_content = str(content or "").strip()
    if not normalized_content:
        raise HTTPException(status_code=422, detail="voice transcript version content is empty")
    record = voice_record_by_id(conn, voice_record_id)
    if not record:
        raise HTTPException(status_code=404, detail="voice record not found")
    current = active_transcript_version(conn, voice_record_id)
    should_activate = bool(activate)
    if current and current.get("version_type") == "user_edited" and normalized_type != "user_edited":
        should_activate = False
    if parent_version_id and not transcript_version_by_id(conn, parent_version_id):
        raise HTTPException(status_code=422, detail="parent transcript version not found")
    if should_activate:
        conn.execute(
            "UPDATE reader.voice_transcript_versions SET is_active = false WHERE voice_record_id = %s AND is_active",
            (voice_record_id,),
        )
    row = conn.execute(
        """
        INSERT INTO reader.voice_transcript_versions (
            id, voice_record_id, version_type, parent_version_id, content,
            content_hash, provider, processing_run_id, changes, confidence,
            meaning_changed, needs_review, is_active, metadata, created_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        RETURNING *
        """,
        (
            new_id("vtv"),
            voice_record_id,
            normalized_type,
            parent_version_id,
            normalized_content,
            json_hash(normalized_content),
            str(provider or "unknown")[:120],
            processing_run_id,
            db.jsonb(json_safe(changes or [])),
            confidence,
            bool(meaning_changed),
            bool(needs_review),
            should_activate,
            db.jsonb(json_safe(metadata or {})),
        ),
    ).fetchone()
    if should_activate:
        conn.execute(
            """
            UPDATE reader.voice_records
            SET active_transcript_version_id = %s, updated_at = now()
            WHERE id = %s
            """,
            (row["id"], voice_record_id),
        )
    return dict(row)


def ensure_raw_transcript_version(
    conn: Any,
    *,
    voice_record_id: str,
    content: str,
    provider: str,
    processing_run_id: Optional[str] = None,
    confidence: Optional[float] = None,
) -> dict[str, Any]:
    normalized_content = str(content or "").strip()
    content_hash = json_hash(normalized_content)
    existing = conn.execute(
        """
        SELECT *
        FROM reader.voice_transcript_versions
        WHERE voice_record_id = %s AND version_type = 'asr_raw' AND content_hash = %s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (voice_record_id, content_hash),
    ).fetchone()
    if existing:
        return dict(existing)
    current = active_transcript_version(conn, voice_record_id)
    return create_transcript_version(
        conn,
        voice_record_id=voice_record_id,
        version_type="asr_raw",
        content=normalized_content,
        provider=provider,
        processing_run_id=processing_run_id,
        confidence=confidence,
        activate=current is None or current.get("version_type") == "asr_raw",
    )


def list_transcript_versions(voice_record_id: str) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM reader.voice_transcript_versions
            WHERE voice_record_id = %s
            ORDER BY created_at DESC
            """,
            (voice_record_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def latest_transcript_version(voice_record_id: str, version_type: str) -> Optional[dict[str, Any]]:
    normalized_type = safe_transcript_version_type(version_type)
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM reader.voice_transcript_versions
            WHERE voice_record_id = %s AND version_type = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (voice_record_id, normalized_type),
        ).fetchone()
    return dict(row) if row else None


def preferred_raw_transcript_version(voice_record_id: str) -> Optional[dict[str, Any]]:
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM reader.voice_transcript_versions
            WHERE voice_record_id = %s AND version_type = 'asr_raw'
            ORDER BY
              is_active DESC,
              (processing_run_id IS NOT NULL) DESC,
              (provider <> 'legacy_voice_record') DESC,
              created_at DESC
            LIMIT 1
            """,
            (voice_record_id,),
        ).fetchone()
    return dict(row) if row else None


def effective_transcript(voice_record_id: str) -> tuple[str, Optional[dict[str, Any]]]:
    with db.connect() as conn:
        version = active_transcript_version(conn, voice_record_id)
        record = voice_record_by_id(conn, voice_record_id)
    if not record:
        raise HTTPException(status_code=404, detail="voice record not found")
    if version:
        return str(version.get("content") or ""), version
    return str(record.get("transcript") or ""), None


def enqueue_processing_job(
    voice_record_id: str,
    job_type: str,
    *,
    payload: Optional[dict[str, Any]] = None,
    input_hash: Optional[str] = None,
    priority: int = 0,
    max_attempts: int = 3,
    retry_failed: bool = False,
) -> dict[str, Any]:
    normalized_type = safe_processing_job_type(job_type)
    normalized_payload = json_safe(payload or {})
    resolved_input_hash = input_hash or json_hash(normalized_payload)
    idempotency_key = f"{voice_record_id}:{normalized_type}:{resolved_input_hash}"
    with db.connect() as conn:
        if not voice_record_by_id(conn, voice_record_id):
            raise HTTPException(status_code=404, detail="voice record not found")
        if retry_failed:
            retried = conn.execute(
                """
                UPDATE reader.voice_processing_jobs
                SET status = 'pending', available_at = now(), lease_owner = NULL,
                    lease_expires_at = NULL, last_error = NULL, finished_at = NULL,
                    attempt_count = 0, processing_run_id = NULL,
                    updated_at = now()
                WHERE idempotency_key = %s AND status IN ('failed', 'cancelled')
                RETURNING *
                """,
                (idempotency_key,),
            ).fetchone()
            if retried:
                return dict(retried)
        row = conn.execute(
            """
            INSERT INTO reader.voice_processing_jobs (
                id, voice_record_id, job_type, status, priority, attempt_count,
                max_attempts, input_hash, idempotency_key, payload,
                available_at, created_at, updated_at
            )
            VALUES (%s, %s, %s, 'pending', %s, 0, %s, %s, %s, %s, now(), now(), now())
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING *
            """,
            (
                new_id("vpj"),
                voice_record_id,
                normalized_type,
                int(priority),
                max(1, min(int(max_attempts), 20)),
                resolved_input_hash,
                idempotency_key,
                db.jsonb(normalized_payload),
            ),
        ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT * FROM reader.voice_processing_jobs WHERE idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
    return dict(row)


def claim_next_processing_job(
    worker_id: str,
    *,
    lease_seconds: int = 240,
    job_types: Optional[list[str]] = None,
) -> Optional[dict[str, Any]]:
    lease = max(30, min(int(lease_seconds), 3600))
    normalized_types = [safe_processing_job_type(job_type) for job_type in (job_types or [])]
    type_filter = "AND job_type = ANY(%s)" if normalized_types else ""
    params: list[Any] = []
    if normalized_types:
        params.append(normalized_types)
    params.extend([str(worker_id)[:160], lease])
    with db.connect() as conn:
        row = conn.execute(
            f"""
            WITH candidate AS (
              SELECT id
              FROM reader.voice_processing_jobs
              WHERE attempt_count < max_attempts
                {type_filter}
                AND (
                  (status = 'pending' AND available_at <= now())
                  OR (status = 'running' AND lease_expires_at < now())
                )
              ORDER BY priority DESC, available_at ASC, created_at ASC
              FOR UPDATE SKIP LOCKED
              LIMIT 1
            )
            UPDATE reader.voice_processing_jobs jobs
            SET status = 'running',
                attempt_count = jobs.attempt_count + 1,
                lease_owner = %s,
                lease_expires_at = now() + make_interval(secs => %s),
                updated_at = now()
            FROM candidate
            WHERE jobs.id = candidate.id
            RETURNING jobs.*
            """,
            tuple(params),
        ).fetchone()
    return dict(row) if row else None


def complete_processing_job(job_id: str, *, processing_run_id: Optional[str] = None) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute(
            """
            UPDATE reader.voice_processing_jobs
            SET status = 'succeeded', processing_run_id = %s, lease_owner = NULL,
                lease_expires_at = NULL, last_error = NULL, finished_at = now(),
                updated_at = now()
            WHERE id = %s AND status = 'running'
            RETURNING *
            """,
            (processing_run_id, job_id),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=409, detail="voice processing job is not running")
    return dict(row)


def fail_processing_job(
    job_id: str,
    error: str,
    *,
    retry_delay_seconds: int = 5,
    processing_run_id: Optional[str] = None,
    minimum_max_attempts: Optional[int] = None,
) -> dict[str, Any]:
    delay = max(0, min(int(retry_delay_seconds), 86400))
    attempt_floor = max(1, min(int(minimum_max_attempts or 1), 100))
    with db.connect() as conn:
        row = conn.execute(
            """
            UPDATE reader.voice_processing_jobs
            SET max_attempts = GREATEST(max_attempts, %s),
                status = CASE
                  WHEN attempt_count < GREATEST(max_attempts, %s) THEN 'pending'
                  ELSE 'failed'
                END,
                processing_run_id = COALESCE(%s, processing_run_id),
                available_at = CASE
                  WHEN attempt_count < GREATEST(max_attempts, %s)
                    THEN now() + make_interval(secs => %s)
                  ELSE available_at
                END,
                lease_owner = NULL,
                lease_expires_at = NULL,
                last_error = %s,
                finished_at = CASE
                  WHEN attempt_count < GREATEST(max_attempts, %s) THEN NULL
                  ELSE now()
                END,
                updated_at = now()
            WHERE id = %s AND status = 'running'
            RETURNING *
            """,
            (
                attempt_floor,
                attempt_floor,
                processing_run_id,
                attempt_floor,
                delay,
                str(error)[:4000],
                attempt_floor,
                job_id,
            ),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=409, detail="voice processing job is not running")
    return dict(row)


def list_processing_jobs(voice_record_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM reader.voice_processing_jobs
            WHERE voice_record_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (voice_record_id, max(1, min(int(limit), 500))),
        ).fetchall()
    return [dict(row) for row in rows]


def create_context_snapshot(
    *,
    voice_record_id: str,
    scope: str,
    source_refs: list[dict[str, Any]],
    evidence_items: list[dict[str, Any]],
    relevance_decisions: list[dict[str, Any]],
    metadata: Optional[dict[str, Any]] = None,
    created_by_run_id: Optional[str] = None,
) -> dict[str, Any]:
    normalized_scope = safe_context_scope(scope)
    payload = {
        "source_refs": json_safe(source_refs),
        "evidence_items": json_safe(evidence_items),
        "relevance_decisions": json_safe(relevance_decisions),
        "metadata": json_safe(metadata or {}),
    }
    with db.connect() as conn:
        if not voice_record_by_id(conn, voice_record_id):
            raise HTTPException(status_code=404, detail="voice record not found")
        row = conn.execute(
            """
            INSERT INTO reader.voice_context_snapshots (
                id, voice_record_id, scope, source_refs, evidence_items,
                relevance_decisions, input_hash, metadata, created_by_run_id,
                created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            RETURNING *
            """,
            (
                new_id("vctx"),
                voice_record_id,
                normalized_scope,
                db.jsonb(payload["source_refs"]),
                db.jsonb(payload["evidence_items"]),
                db.jsonb(payload["relevance_decisions"]),
                json_hash(payload),
                db.jsonb(payload["metadata"]),
                created_by_run_id,
            ),
        ).fetchone()
    return dict(row)


def get_context_snapshot(snapshot_id: str) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM reader.voice_context_snapshots WHERE id = %s",
            (snapshot_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="voice context snapshot not found")
    return dict(row)


def list_context_snapshots(voice_record_id: str) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM reader.voice_context_snapshots
            WHERE voice_record_id = %s
            ORDER BY created_at DESC
            """,
            (voice_record_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def create_discussion(
    *,
    voice_record_id: str,
    title: Optional[str] = None,
    context_scope: str = "recent_reading",
    context_options: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    scope = safe_context_scope(context_scope)
    with db.connect() as conn:
        record = voice_record_by_id(conn, voice_record_id)
        if not record:
            raise HTTPException(status_code=404, detail="voice record not found")
        row = conn.execute(
            """
            INSERT INTO reader.voice_discussions (
                id, voice_record_id, title, context_scope, context_options,
                status, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, 'active', now(), now())
            RETURNING *
            """,
            (
                new_id("vdisc"),
                voice_record_id,
                clean_title(title, fallback=str(record.get("title") or "灵感讨论")),
                scope,
                db.jsonb(json_safe(context_options or {})),
            ),
        ).fetchone()
    return dict(row)


def get_discussion_or_404(discussion_id: str) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM reader.voice_discussions WHERE id = %s",
            (discussion_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="voice discussion not found")
    return dict(row)


def list_discussions(voice_record_id: str) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM reader.voice_discussions
            WHERE voice_record_id = %s
            ORDER BY updated_at DESC
            """,
            (voice_record_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def update_discussion_runtime(
    discussion_id: str,
    *,
    context_snapshot_id: Optional[str],
    hermes_session_id: Optional[str],
) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute(
            """
            UPDATE reader.voice_discussions
            SET current_context_snapshot_id = COALESCE(%s, current_context_snapshot_id),
                hermes_session_id = COALESCE(%s, hermes_session_id),
                updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (context_snapshot_id, hermes_session_id, discussion_id),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="voice discussion not found")
    return dict(row)


def append_discussion_message(
    *,
    discussion_id: str,
    role: str,
    content: str,
    citations: Optional[list[dict[str, Any]]] = None,
    context_snapshot_id: Optional[str] = None,
    processing_run_id: Optional[str] = None,
    status: str = "succeeded",
    failure_reason: Optional[str] = None,
    structured_content: Optional[dict[str, Any]] = None,
    adapter_version: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    client_message_id: Optional[str] = None,
) -> dict[str, Any]:
    normalized_role = str(role or "").strip().lower()
    if normalized_role not in VOICE_DISCUSSION_MESSAGE_ROLES:
        raise HTTPException(status_code=422, detail="unsupported voice discussion message role")
    if status not in {"pending", "succeeded", "failed"}:
        raise HTTPException(status_code=422, detail="unsupported voice discussion message status")
    normalized_content = str(content or "").strip()
    if not normalized_content and status not in {"pending", "failed"}:
        raise HTTPException(status_code=422, detail="voice discussion message content is empty")
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM reader.voice_discussions WHERE id = %s", (discussion_id,)).fetchone():
            raise HTTPException(status_code=404, detail="voice discussion not found")
        row = conn.execute(
            """
            INSERT INTO reader.voice_discussion_messages (
                id, discussion_id, role, content, citations, context_snapshot_id,
                processing_run_id, status, failure_reason, structured_content,
                adapter_version, provider, model, client_message_id, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            RETURNING *
            """,
            (
                new_id("vmsg"),
                discussion_id,
                normalized_role,
                normalized_content,
                db.jsonb(json_safe(citations or [])),
                context_snapshot_id,
                processing_run_id,
                status,
                failure_reason,
                db.jsonb(json_safe(structured_content or {})),
                adapter_version,
                provider,
                model,
                str(client_message_id)[:160] if client_message_id else None,
            ),
        ).fetchone()
        conn.execute("UPDATE reader.voice_discussions SET updated_at = now() WHERE id = %s", (discussion_id,))
    return dict(row)


def discussion_message_by_client_id(discussion_id: str, client_message_id: str) -> Optional[dict[str, Any]]:
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM reader.voice_discussion_messages
            WHERE discussion_id = %s AND client_message_id = %s
            """,
            (discussion_id, str(client_message_id)[:160]),
        ).fetchone()
    return dict(row) if row else None


def update_discussion_message(
    message_id: str,
    *,
    content: str,
    citations: Optional[list[dict[str, Any]]] = None,
    context_snapshot_id: Optional[str] = None,
    processing_run_id: Optional[str] = None,
    status: str,
    failure_reason: Optional[str] = None,
    structured_content: Optional[dict[str, Any]] = None,
    adapter_version: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> dict[str, Any]:
    if status not in {"pending", "succeeded", "failed"}:
        raise HTTPException(status_code=422, detail="unsupported voice discussion message status")
    normalized_content = str(content or "").strip()
    if status == "succeeded" and not normalized_content:
        raise HTTPException(status_code=422, detail="voice discussion message content is empty")
    with db.connect() as conn:
        row = conn.execute(
            """
            UPDATE reader.voice_discussion_messages
            SET content = %s, citations = %s,
                context_snapshot_id = COALESCE(%s, context_snapshot_id),
                processing_run_id = COALESCE(%s, processing_run_id),
                status = %s, failure_reason = %s,
                structured_content = %s,
                adapter_version = COALESCE(%s, adapter_version),
                provider = COALESCE(%s, provider),
                model = COALESCE(%s, model)
            WHERE id = %s
            RETURNING *
            """,
            (
                normalized_content,
                db.jsonb(json_safe(citations or [])),
                context_snapshot_id,
                processing_run_id,
                status,
                failure_reason,
                db.jsonb(json_safe(structured_content or {})),
                adapter_version,
                provider,
                model,
                message_id,
            ),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE reader.voice_discussions SET updated_at = now() WHERE id = %s",
                (row["discussion_id"],),
            )
    if not row:
        raise HTTPException(status_code=404, detail="voice discussion message not found")
    return dict(row)


def list_discussion_messages(discussion_id: str) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM reader.voice_discussion_messages
            WHERE discussion_id = %s
            ORDER BY created_at ASC
            """,
            (discussion_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def insert_voice_actions(
    conn: Any,
    voice_record_id: str,
    actions: list[dict[str, Any]],
    *,
    proposed_by_run_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    inserted: list[dict[str, Any]] = []
    for action in actions:
        action_type = safe_action_type(action.get("action_type") or action.get("type"))
        risk = str(action.get("risk") or "medium").strip().lower()
        if risk not in {"low", "medium", "high"}:
            risk = "medium"
        requires_confirmation = bool(action.get("requires_confirmation", risk != "low" or action_type not in {"archive", "no_action"}))
        status = "pending_user_confirmation" if requires_confirmation else "proposed"
        row = conn.execute(
            """
            INSERT INTO reader.voice_actions (
                id, voice_record_id, action_type, title, body, target, risk, status,
                evidence_spans, requires_confirmation, applied_result,
                proposed_by_run_id, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '{}'::jsonb, %s, now())
            RETURNING *
            """,
            (
                new_id("vact"),
                voice_record_id,
                action_type,
                clean_title(action.get("title"), fallback=action_type),
                str(action.get("body") or ""),
                db.jsonb(json_safe(action.get("target") if isinstance(action.get("target"), dict) else {})),
                risk,
                status,
                db.jsonb(json_safe(action.get("evidence_spans") if isinstance(action.get("evidence_spans"), list) else [])),
                requires_confirmation,
                proposed_by_run_id,
            ),
        ).fetchone()
        inserted.append(dict(row))
    return inserted


def action_by_id(conn: Any, voice_record_id: str, action_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM reader.voice_actions WHERE id = %s AND voice_record_id = %s",
        (action_id, voice_record_id),
    ).fetchone()
    return dict(row) if row else None


def request_voice_action_apply(
    voice_record_id: str,
    action_id: str,
    *,
    confirmed: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from reader_api.voice_action_targets import target_adapter_for

    with db.connect() as conn:
        action = action_by_id(conn, voice_record_id, action_id)
        if not action:
            raise HTTPException(status_code=404, detail="voice action not found")
        if action.get("status") in {"superseded", "rejected"}:
            raise HTTPException(status_code=409, detail="这条动作已经失效，不能再执行")
        if action.get("status") == "validated":
            jobs = conn.execute(
                "SELECT * FROM reader.voice_processing_jobs WHERE id = %s",
                (action.get("validate_job_id") or action.get("apply_job_id"),),
            ).fetchone()
            return action, dict(jobs) if jobs else {}
        if action.get("requires_confirmation") and not confirmed:
            raise HTTPException(status_code=409, detail="这条动作需要你明确确认后才能写入")
        record = voice_record_by_id(conn, voice_record_id)
        if not record:
            raise HTTPException(status_code=404, detail="voice record not found")
    try:
        adapter = target_adapter_for(action, record)
    except Exception as exc:
        from reader_api.voice_action_targets import UnsupportedVoiceActionTarget, VoiceActionTargetError

        if isinstance(exc, UnsupportedVoiceActionTarget):
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if isinstance(exc, VoiceActionTargetError):
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        raise

    retry_validation = bool(
        action.get("status") == "failed_apply"
        and action.get("target_adapter")
        and isinstance(action.get("target_identity"), dict)
        and action.get("target_identity")
    )
    action_hash = json_hash(
        {
            "action_id": action_id,
            "target_adapter": adapter.name,
            "target_identity": action.get("target_identity") or {},
        }
        if retry_validation
        else {
            "action_id": action_id,
            "action_type": action.get("action_type"),
            "title": action.get("title"),
            "body": action.get("body"),
            "target": action.get("target") or {},
            "adapter": adapter.name,
        }
    )
    job = enqueue_processing_job(
        voice_record_id,
        "validate_action" if retry_validation else "apply_action",
        payload={"action_id": action_id, "target_adapter": adapter.name},
        input_hash=action_hash,
        max_attempts=3,
        retry_failed=True,
    )
    with db.connect() as conn:
        updated = conn.execute(
            """
            UPDATE reader.voice_actions
            SET status = CASE
                  WHEN status = 'validated' THEN status
                  WHEN %s THEN 'validating'
                  ELSE 'applying'
                END,
                confirmed_at = CASE WHEN %s THEN COALESCE(confirmed_at, now()) ELSE confirmed_at END,
                confirmed_by = CASE WHEN %s THEN COALESCE(confirmed_by, 'local_user') ELSE confirmed_by END,
                target_adapter = %s,
                apply_job_id = CASE WHEN %s THEN apply_job_id ELSE %s END,
                validate_job_id = CASE WHEN %s THEN %s ELSE validate_job_id END,
                failure_reason = NULL
            WHERE id = %s AND voice_record_id = %s
            RETURNING *
            """,
            (
                retry_validation,
                confirmed,
                confirmed,
                adapter.name,
                retry_validation,
                job.get("id"),
                retry_validation,
                job.get("id"),
                action_id,
                voice_record_id,
            ),
        ).fetchone()
        conn.execute(
            """
            UPDATE reader.voice_records
            SET status = CASE WHEN status IN ('archived', 'deleted') THEN status ELSE 'action_pending' END,
                failure_code = NULL, failure_message = NULL, updated_at = now()
            WHERE id = %s AND deleted_at IS NULL
            """,
            (voice_record_id,),
        )
    return dict(updated), job


def archive_voice_record(voice_record_id: str) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute(
            """
            UPDATE reader.voice_records
            SET status = 'archived',
                archived_at = now(),
                updated_at = now()
            WHERE id = %s AND deleted_at IS NULL
            RETURNING *
            """,
            (voice_record_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="voice record not found")
    return dict(row)


def soft_delete_voice_record(voice_record_id: str) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute(
            """
            UPDATE reader.voice_records
            SET status = 'deleted',
                deleted_at = now(),
                updated_at = now()
            WHERE id = %s AND deleted_at IS NULL
            RETURNING *
            """,
            (voice_record_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="voice record not found")
    return dict(row)
