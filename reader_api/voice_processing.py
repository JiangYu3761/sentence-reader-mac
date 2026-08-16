from __future__ import annotations

import os
import socket
import threading
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import uuid4

from reader_api import db
from reader_api.mobile_workspace import mac_voice_pipeline_transcribe
from reader_api.voice_hermes import (
    VOICE_HERMES_CLEANUP_INPUT_SCHEMA,
    VOICE_HERMES_UNDERSTAND_INPUT_SCHEMA,
    VoiceHermesAdapter,
    get_voice_hermes_adapter,
)
from reader_api.voice_inbox_service import (
    active_transcript_version,
    action_by_id,
    claim_next_processing_job,
    complete_processing_job,
    create_transcript_version,
    get_context_snapshot,
    get_discussion_or_404,
    effective_transcript,
    enqueue_processing_job,
    ensure_raw_transcript_version,
    fail_processing_job,
    get_voice_record_or_404,
    insert_processing_run,
    insert_voice_actions,
    json_hash,
    json_safe,
    list_discussion_messages,
    list_context_snapshots,
    now_iso,
    preferred_raw_transcript_version,
    update_discussion_message,
    update_discussion_runtime,
)
from reader_api.voice_action_targets import (
    VoiceActionTargetError,
    adapter_by_name,
    receipt_hash,
)
from reader_api.voice_reading_context import (
    CitationResolutionError,
    build_context_material,
    context_snapshot_matches,
    persist_context_snapshot,
    resolve_snapshot_citation,
)


JobHandler = Callable[[dict[str, Any]], Optional[str]]


class BoundedAdaptiveWait:
    def __init__(self, initial_seconds: float, maximum_seconds: float, multiplier: float = 2.0) -> None:
        self.initial_seconds = max(0.02, float(initial_seconds))
        self.maximum_seconds = max(self.initial_seconds, float(maximum_seconds))
        self.multiplier = max(1.0, float(multiplier))
        self.current_seconds = self.initial_seconds

    def reset(self) -> None:
        self.current_seconds = self.initial_seconds

    def advance(self) -> None:
        self.current_seconds = min(
            self.maximum_seconds,
            max(self.initial_seconds, self.current_seconds * self.multiplier),
        )


class VoiceProcessingExecutionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        processing_run_id: Optional[str] = None,
        retry_delay_seconds: Optional[int] = None,
        minimum_max_attempts: Optional[int] = None,
        retry_record_status: Optional[str] = None,
        terminal_record_status: Optional[str] = None,
        discussion_message_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.processing_run_id = processing_run_id
        self.retry_delay_seconds = retry_delay_seconds
        self.minimum_max_attempts = minimum_max_attempts
        self.retry_record_status = retry_record_status
        self.terminal_record_status = terminal_record_status
        self.discussion_message_id = discussion_message_id


def _session_busy(message: str) -> bool:
    return "session_busy" in message.lower()


def _hermes_execution_error(
    message: str,
    *,
    processing_run_id: str,
    retry_record_status: Optional[str] = None,
    terminal_record_status: Optional[str] = None,
    discussion_message_id: Optional[str] = None,
) -> VoiceProcessingExecutionError:
    if _session_busy(message):
        return VoiceProcessingExecutionError(
            message,
            processing_run_id=processing_run_id,
            retry_delay_seconds=20,
            minimum_max_attempts=100,
            retry_record_status=retry_record_status,
            terminal_record_status=terminal_record_status,
            discussion_message_id=discussion_message_id,
        )
    return VoiceProcessingExecutionError(message, processing_run_id=processing_run_id)


def _confidence_from_result(result: dict[str, Any]) -> Optional[float]:
    raw = result.get("raw_result") if isinstance(result.get("raw_result"), dict) else {}
    candidate = raw.get("confidence")
    try:
        return max(0.0, min(1.0, float(candidate))) if candidate is not None else None
    except (TypeError, ValueError):
        return None


def _raw_asr_text(result: dict[str, Any]) -> str:
    raw = result.get("raw_result") if isinstance(result.get("raw_result"), dict) else {}
    # mac_voice_pipeline_transcribe keeps the provider response here. The legacy
    # transcript field may already contain punctuation normalization.
    return str(raw.get("text") or result.get("transcript") or "").strip()


def transcribe_failure_message(detail: str) -> tuple[str, str]:
    normalized = detail.lower()
    if "did not return text" in normalized or "empty transcript" in normalized:
        return (
            "no_speech_detected",
            "录音中没有识别到清晰语音。原始音频已保留，请确认麦克风音量后重试。",
        )
    failure_code = "funasr_unavailable" if "health" in normalized else "transcribe_failed"
    return failure_code, f"FunASR 暂时无法完成转写：{detail}。原始音频已保留，可稍后重试。"


def _record_transcribe_failure(
    voice_record_id: str,
    *,
    record: dict[str, Any],
    result: dict[str, Any],
    started_at: str,
    failure_code: str,
    failure_message: str,
) -> str:
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE reader.voice_records
            SET status = 'failed_transcribe', failure_code = %s,
                failure_message = %s, updated_at = now()
            WHERE id = %s
            """,
            (failure_code, failure_message, voice_record_id),
        )
        run = insert_processing_run(
            conn,
            voice_record_id=voice_record_id,
            run_type="transcribe",
            provider="funasr-local",
            status="failed",
            request_payload={
                "audio_uri": record.get("audio_uri"),
                "audio_hash": record.get("audio_hash"),
            },
            response_payload=result,
            failure_reason=failure_message,
            started_at=started_at,
        )
    return str(run["id"])


def process_transcribe_job(job: dict[str, Any]) -> str:
    voice_record_id = str(job["voice_record_id"])
    started_at = now_iso()
    record = get_voice_record_or_404(voice_record_id)
    audio_path = Path(str(record.get("audio_uri") or "")).expanduser()
    if not audio_path.is_file():
        message = "原始音频文件不存在。录音记录仍保留，请恢复文件后重试。"
        run_id = _record_transcribe_failure(
            voice_record_id,
            record=record,
            result={"ok": False, "error": "audio file missing", "audio_uri": str(audio_path)},
            started_at=started_at,
            failure_code="audio_missing",
            failure_message=message,
        )
        raise VoiceProcessingExecutionError(message, processing_run_id=run_id)

    with db.connect() as conn:
        conn.execute(
            """
            UPDATE reader.voice_records
            SET status = 'transcribing', failure_code = NULL,
                failure_message = NULL, updated_at = now()
            WHERE id = %s
            """,
            (voice_record_id,),
        )

    result = mac_voice_pipeline_transcribe(
        audio_path,
        purpose="click_voice_durable_transcription",
        timeout=120.0,
    )
    transcript = _raw_asr_text(result)
    if not result.get("ok") or not transcript:
        detail = str(result.get("error") or "FunASR returned an empty transcript")
        failure_code, message = transcribe_failure_message(detail)
        run_id = _record_transcribe_failure(
            voice_record_id,
            record=record,
            result=result,
            started_at=started_at,
            failure_code=failure_code,
            failure_message=message,
        )
        raise VoiceProcessingExecutionError(message, processing_run_id=run_id)

    with db.connect() as conn:
        run = insert_processing_run(
            conn,
            voice_record_id=voice_record_id,
            run_type="transcribe",
            provider="funasr-local",
            status="success",
            request_payload={
                "audio_uri": str(audio_path),
                "audio_hash": record.get("audio_hash"),
            },
            response_payload=result,
            started_at=started_at,
        )
        raw_version = ensure_raw_transcript_version(
            conn,
            voice_record_id=voice_record_id,
            content=transcript,
            provider="funasr-local",
            processing_run_id=run["id"],
            confidence=_confidence_from_result(result),
        )
        active = active_transcript_version(conn, voice_record_id)
        effective_text = str((active or {}).get("content") or transcript)
        conn.execute(
            """
            UPDATE reader.voice_records
            SET transcript = %s, status = 'cleanup_pending', transcribed_at = now(),
                failure_code = NULL, failure_message = NULL, updated_at = now()
            WHERE id = %s
            """,
            (effective_text, voice_record_id),
        )
    enqueue_processing_job(
        voice_record_id,
        "clean_transcript",
        payload={"transcript_version_id": raw_version["id"]},
        input_hash=str(raw_version["content_hash"]),
    )
    return str(run["id"])


def _prior_successful_run(voice_record_id: str, run_type: str, operation_input_hash: str) -> Optional[dict[str, Any]]:
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM reader.voice_processing_runs
            WHERE voice_record_id = %s
              AND run_type = %s
              AND status = 'success'
              AND request_payload->>'operation_input_hash' = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (voice_record_id, run_type, operation_input_hash),
        ).fetchone()
    return dict(row) if row else None


def _hermes_failure_run(
    *,
    voice_record_id: str,
    run_type: str,
    failed_status: str,
    failure_code: str,
    human_prefix: str,
    operation_input_hash: str,
    input_schema: str,
    started_at: str,
    error: Exception,
) -> str:
    detail = str(error)
    message = f"{human_prefix}：{detail}。原始音频和已有转写均已保留，可稍后重试。"
    called_hermes = bool(getattr(error, "called_hermes", True))
    raw_response = getattr(error, "response", {})
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE reader.voice_records
            SET status = %s, failure_code = %s, failure_message = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (failed_status, failure_code, message, voice_record_id),
        )
        run = insert_processing_run(
            conn,
            voice_record_id=voice_record_id,
            run_type=run_type,
            provider="hermes-runtime",
            called_hermes=called_hermes,
            status="failed",
            request_payload={
                "schema": input_schema,
                "operation_input_hash": operation_input_hash,
            },
            response_payload={"error": detail, "raw_response": raw_response},
            failure_reason=message,
            started_at=started_at,
        )
    return str(run["id"])


def _enqueue_understanding(voice_record_id: str, transcript: str, version: Optional[dict[str, Any]]) -> dict[str, Any]:
    content_hash = str((version or {}).get("content_hash") or json_hash(transcript))
    return enqueue_processing_job(
        voice_record_id,
        "understand",
        payload={"transcript_version_id": (version or {}).get("id")},
        input_hash=content_hash,
    )


def process_clean_transcript_job(
    job: dict[str, Any],
    *,
    adapter: Optional[VoiceHermesAdapter] = None,
) -> str:
    voice_record_id = str(job["voice_record_id"])
    operation_input_hash = str(job["input_hash"])
    prior = _prior_successful_run(voice_record_id, "clean_transcript", operation_input_hash)
    if prior:
        transcript, version = effective_transcript(voice_record_id)
        _enqueue_understanding(voice_record_id, transcript, version)
        return str(prior["id"])

    raw_version = preferred_raw_transcript_version(voice_record_id)
    if not raw_version:
        message = "找不到 FunASR 原始转写，无法进行忠实整理。原始音频仍保留。"
        raise VoiceProcessingExecutionError(message)
    raw_text = str(raw_version.get("content") or "").strip()
    if not raw_text:
        raise VoiceProcessingExecutionError("FunASR 原始转写为空，无法进行忠实整理。")
    if operation_input_hash != str(raw_version.get("content_hash") or json_hash(raw_text)):
        enqueue_processing_job(
            voice_record_id,
            "clean_transcript",
            payload={"transcript_version_id": raw_version["id"]},
            input_hash=str(raw_version.get("content_hash") or json_hash(raw_text)),
            retry_failed=True,
        )
        started_at = now_iso()
        with db.connect() as conn:
            run = insert_processing_run(
                conn,
                voice_record_id=voice_record_id,
                run_type="clean_transcript",
                provider="click-voice-worker",
                called_hermes=False,
                status="success",
                request_payload={
                    "schema": VOICE_HERMES_CLEANUP_INPUT_SCHEMA,
                    "operation_input_hash": operation_input_hash,
                },
                response_payload={"skipped": "newer_raw_transcript_available"},
                started_at=started_at,
            )
        return str(run["id"])

    started_at = now_iso()
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE reader.voice_records
            SET status = 'cleaning', failure_code = NULL,
                failure_message = NULL, updated_at = now()
            WHERE id = %s
            """,
            (voice_record_id,),
        )
    voice_adapter = adapter or get_voice_hermes_adapter()
    try:
        receipt = voice_adapter.clean_transcript(
            voice_record_id=voice_record_id,
            transcript_version_id=str(raw_version["id"]),
            raw_text=raw_text,
            vocabulary=[],
        )
    except Exception as exc:  # noqa: BLE001 - invalid JSON and transport errors share durable failure handling.
        retrying = _session_busy(str(exc))
        run_id = _hermes_failure_run(
            voice_record_id=voice_record_id,
            run_type="clean_transcript",
            failed_status="cleanup_pending" if retrying else "failed_cleanup",
            failure_code="hermes_cleanup_failed",
            human_prefix="Hermes 暂时无法完成忠实整理",
            operation_input_hash=operation_input_hash,
            input_schema=VOICE_HERMES_CLEANUP_INPUT_SCHEMA,
            started_at=started_at,
            error=exc,
        )
        raise _hermes_execution_error(
            str(exc),
            processing_run_id=run_id,
            retry_record_status="cleanup_pending",
            terminal_record_status="failed_cleanup",
        ) from exc

    output = receipt["output"]
    with db.connect() as conn:
        run = insert_processing_run(
            conn,
            voice_record_id=voice_record_id,
            run_type="clean_transcript",
            provider=str(receipt.get("provider") or "hermes-runtime"),
            model=str(receipt.get("model") or ""),
            called_hermes=True,
            status="success",
            request_payload={
                "schema": VOICE_HERMES_CLEANUP_INPUT_SCHEMA,
                "adapter_version": receipt["adapter_version"],
                "operation_input_hash": operation_input_hash,
                "prompt": receipt["prompt"],
            },
            response_payload={
                "output": output,
                "raw_response": receipt["raw_response"],
                "session_id": receipt["session_id"],
            },
            started_at=started_at,
        )
        create_transcript_version(
            conn,
            voice_record_id=voice_record_id,
            version_type="hermes_cleaned",
            parent_version_id=str(raw_version["id"]),
            content=str(output["cleaned_text"]),
            provider=str(receipt.get("provider") or "hermes-runtime"),
            processing_run_id=str(run["id"]),
            changes=output["changes"],
            meaning_changed=False,
            needs_review=bool(output["needs_review"]),
            metadata={
                "adapter_version": receipt["adapter_version"],
                "adapter_input_hash": operation_input_hash,
                "session_id": receipt["session_id"],
            },
            activate=not bool(output["needs_review"]),
        )
        active = active_transcript_version(conn, voice_record_id)
        effective_text = str((active or {}).get("content") or raw_text)
        conn.execute(
            """
            UPDATE reader.voice_records
            SET transcript = %s, status = 'understand_pending', cleaned_at = now(),
                failure_code = NULL, failure_message = NULL, updated_at = now()
            WHERE id = %s
            """,
            (effective_text, voice_record_id),
        )
    _enqueue_understanding(voice_record_id, effective_text, active)
    return str(run["id"])


def process_understand_job(
    job: dict[str, Any],
    *,
    adapter: Optional[VoiceHermesAdapter] = None,
) -> str:
    voice_record_id = str(job["voice_record_id"])
    operation_input_hash = str(job["input_hash"])
    prior = _prior_successful_run(voice_record_id, "understand", operation_input_hash)
    if prior:
        return str(prior["id"])

    record = get_voice_record_or_404(voice_record_id)
    transcript, version = effective_transcript(voice_record_id)
    transcript = transcript.strip()
    if not transcript:
        raise VoiceProcessingExecutionError("当前转写为空，Hermes 无法理解这条语音。")
    current_hash = str((version or {}).get("content_hash") or json_hash(transcript))
    if current_hash != operation_input_hash:
        _enqueue_understanding(voice_record_id, transcript, version)
        started_at = now_iso()
        with db.connect() as conn:
            run = insert_processing_run(
                conn,
                voice_record_id=voice_record_id,
                run_type="understand",
                provider="click-voice-worker",
                called_hermes=False,
                status="success",
                request_payload={
                    "schema": VOICE_HERMES_UNDERSTAND_INPUT_SCHEMA,
                    "operation_input_hash": operation_input_hash,
                },
                response_payload={"skipped": "active_transcript_changed"},
                started_at=started_at,
            )
        return str(run["id"])

    started_at = now_iso()
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE reader.voice_records
            SET status = 'understanding', failure_code = NULL,
                failure_message = NULL, updated_at = now()
            WHERE id = %s
            """,
            (voice_record_id,),
        )
    voice_adapter = adapter or get_voice_hermes_adapter()
    try:
        receipt = voice_adapter.understand(
            voice_record=record,
            transcript_version=version,
            transcript=transcript,
        )
    except Exception as exc:  # noqa: BLE001 - invalid JSON and transport errors share durable failure handling.
        retrying = _session_busy(str(exc))
        run_id = _hermes_failure_run(
            voice_record_id=voice_record_id,
            run_type="understand",
            failed_status="understand_pending" if retrying else "failed_understand",
            failure_code="hermes_understand_failed",
            human_prefix="Hermes 暂时无法理解这条语音",
            operation_input_hash=operation_input_hash,
            input_schema=VOICE_HERMES_UNDERSTAND_INPUT_SCHEMA,
            started_at=started_at,
            error=exc,
        )
        raise _hermes_execution_error(
            str(exc),
            processing_run_id=run_id,
            retry_record_status="understand_pending",
            terminal_record_status="failed_understand",
        ) from exc

    understood = receipt["output"]
    actions = understood["actions"]
    record_metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    resolved_title = (
        str(record.get("title") or understood["title"])
        if record_metadata.get("mobile_user_title_override") or record_metadata.get("tingle_user_title_override")
        else understood["title"]
    )
    resolved_intent = (
        str(record.get("intent_type") or understood["intent_type"])
        if record_metadata.get("mobile_user_category_override")
        else understood["intent_type"]
    )
    needs_followup = bool(understood.get("followup_question")) or any(
        action["action_type"] == "ask_followup" for action in actions
    )
    next_status = (
        "needs_followup"
        if needs_followup
        else "needs_user_confirmation"
        if understood["requires_confirmation"]
        else "action_pending"
    )
    with db.connect() as conn:
        run = insert_processing_run(
            conn,
            voice_record_id=voice_record_id,
            run_type="understand",
            provider=str(receipt.get("provider") or "hermes-runtime"),
            model=str(receipt.get("model") or ""),
            called_hermes=True,
            status="success",
            request_payload={
                "schema": VOICE_HERMES_UNDERSTAND_INPUT_SCHEMA,
                "adapter_version": receipt["adapter_version"],
                "operation_input_hash": operation_input_hash,
                "prompt": receipt["prompt"],
            },
            response_payload={
                "output": understood,
                "raw_response": receipt["raw_response"],
                "session_id": receipt["session_id"],
            },
            started_at=started_at,
        )
        conn.execute(
            """
            UPDATE reader.voice_actions
            SET status = 'superseded',
                applied_result = applied_result || %s,
                failure_reason = NULL
            WHERE voice_record_id = %s
              AND status IN ('proposed', 'pending_user_confirmation')
            """,
            (
                db.jsonb({"superseded_by_run_id": run["id"], "reason": "new_transcript_understanding"}),
                voice_record_id,
            ),
        )
        inserted_actions = insert_voice_actions(
            conn,
            voice_record_id,
            actions,
            proposed_by_run_id=str(run["id"]),
        )
        conn.execute(
            "UPDATE reader.voice_processing_runs SET created_actions = %s WHERE id = %s",
            (db.jsonb(json_safe(inserted_actions)), run["id"]),
        )
        conn.execute(
            """
            UPDATE reader.voice_records
            SET title = %s, summary = %s, intent_type = %s,
                evidence_spans = %s, status = %s, hermes_result_id = %s,
                failure_code = NULL, failure_message = NULL,
                processed_at = now(), updated_at = now()
            WHERE id = %s
            """,
            (
                resolved_title,
                understood["summary"],
                resolved_intent,
                db.jsonb(understood["evidence_spans"]),
                next_status,
                receipt["session_id"],
                voice_record_id,
            ),
        )
    return str(run["id"])


def _prior_context_run(voice_record_id: str, operation_input_hash: str) -> Optional[dict[str, Any]]:
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT vpr.*
            FROM reader.voice_processing_runs vpr
            JOIN reader.voice_context_snapshots vcs ON vcs.created_by_run_id = vpr.id
            WHERE vpr.voice_record_id = %s
              AND vpr.run_type = 'build_context'
              AND vpr.status = 'success'
              AND vpr.request_payload->>'operation_input_hash' = %s
            ORDER BY vpr.created_at DESC
            LIMIT 1
            """,
            (voice_record_id, operation_input_hash),
        ).fetchone()
    return dict(row) if row else None


def process_build_context_job(
    job: dict[str, Any],
    *,
    adapter: Optional[VoiceHermesAdapter] = None,
) -> str:
    voice_record_id = str(job["voice_record_id"])
    operation_input_hash = str(job["input_hash"])
    prior = _prior_context_run(voice_record_id, operation_input_hash)
    if prior:
        return str(prior["id"])
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    scope = str(payload.get("scope") or "recent_reading")
    selected_book_ids = payload.get("selected_book_ids") if isinstance(payload.get("selected_book_ids"), list) else []
    started_at = now_iso()
    try:
        material = build_context_material(
            voice_record_id,
            scope=scope,
            selected_book_ids=selected_book_ids,
            adapter=adapter,
        )
    except Exception as exc:  # noqa: BLE001 - provider and Hermes failures must remain durable.
        detail = str(exc)
        message = f"暂时无法准备阅读证据：{detail}。录音和已有转写均未改变，可稍后重试。"
        called_hermes = bool(getattr(exc, "called_hermes", False))
        raw_response = getattr(exc, "response", {})
        with db.connect() as conn:
            run = insert_processing_run(
                conn,
                voice_record_id=voice_record_id,
                run_type="build_context",
                provider="hermes-runtime" if called_hermes else "click-reading-context",
                called_hermes=called_hermes,
                status="failed",
                request_payload={
                    "schema": "click.voice.reading_context.request.v1",
                    "operation_input_hash": operation_input_hash,
                    "scope": scope,
                    "selected_book_ids": selected_book_ids,
                },
                response_payload={"error": detail, "raw_response": raw_response},
                failure_reason=message,
                started_at=started_at,
            )
        raise _hermes_execution_error(message, processing_run_id=str(run["id"])) from exc

    receipt = material.get("selection_receipt") or {}
    snapshot = persist_context_snapshot(
        voice_record_id,
        scope=scope,
        material=material,
        created_by_run_id=None,
    )
    with db.connect() as conn:
        run = insert_processing_run(
            conn,
            voice_record_id=voice_record_id,
            run_type="build_context",
            provider=str(receipt.get("provider") or "click-reading-context"),
            model=str(receipt.get("model") or ""),
            called_hermes=bool(receipt.get("called_hermes")),
            status="success",
            request_payload={
                "schema": "click.voice.reading_context.request.v1",
                "adapter_version": receipt.get("adapter_version"),
                "operation_input_hash": operation_input_hash,
                "scope": scope,
                "selected_book_ids": selected_book_ids,
                "prompt": receipt.get("prompt"),
            },
            response_payload={
                "snapshot_id": snapshot["id"],
                "relevance_decisions": material["relevance_decisions"],
                "no_relevant_reason": material.get("no_relevant_reason"),
                "raw_response": receipt.get("raw_response"),
            },
            started_at=started_at,
        )
        conn.execute(
            "UPDATE reader.voice_context_snapshots SET created_by_run_id = %s WHERE id = %s",
            (run["id"], snapshot["id"]),
        )
    return str(run["id"])


def _discussion_message(message_id: str) -> Optional[dict[str, Any]]:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM reader.voice_discussion_messages WHERE id = %s",
            (message_id,),
        ).fetchone()
    return dict(row) if row else None


def _discussion_snapshot(discussion: dict[str, Any], requested_snapshot_id: Optional[str]) -> Optional[dict[str, Any]]:
    snapshot_id = requested_snapshot_id or discussion.get("current_context_snapshot_id")
    if snapshot_id:
        snapshot = get_context_snapshot(str(snapshot_id))
        if str(snapshot.get("voice_record_id")) == str(discussion["voice_record_id"]):
            return snapshot
    snapshots = list_context_snapshots(str(discussion["voice_record_id"]))
    scope = str(discussion.get("context_scope") or "recent_reading")
    options = discussion.get("context_options") if isinstance(discussion.get("context_options"), dict) else {}
    selected_book_ids = options.get("selected_book_ids") if isinstance(options.get("selected_book_ids"), list) else []
    return next(
        (
            snapshot
            for snapshot in snapshots
            if context_snapshot_matches(
                snapshot,
                scope=scope,
                selected_book_ids=selected_book_ids,
            )
        ),
        None,
    )


def process_prepare_discussion_job(
    job: dict[str, Any],
    *,
    adapter: Optional[VoiceHermesAdapter] = None,
) -> str:
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    discussion_id = str(payload.get("discussion_id") or "")
    user_message_id = str(payload.get("user_message_id") or "")
    assistant_message_id = str(payload.get("assistant_message_id") or "")
    discussion = get_discussion_or_404(discussion_id)
    assistant_message = _discussion_message(assistant_message_id)
    if assistant_message and assistant_message.get("status") == "succeeded" and assistant_message.get("processing_run_id"):
        return str(assistant_message["processing_run_id"])
    user_message = _discussion_message(user_message_id)
    if not user_message or user_message.get("role") != "user":
        raise VoiceProcessingExecutionError("找不到这轮讨论的用户消息，无法继续。")
    snapshot = _discussion_snapshot(discussion, payload.get("context_snapshot_id"))
    if not snapshot:
        message = "阅读证据快照尚未准备完成，讨论会在快照可用后重试。"
        if int(job.get("attempt_count") or 0) >= int(job.get("max_attempts") or 3):
            update_discussion_message(
                assistant_message_id,
                content="",
                status="failed",
                failure_reason=message,
            )
            with db.connect() as conn:
                run = insert_processing_run(
                    conn,
                    voice_record_id=str(discussion["voice_record_id"]),
                    run_type="discuss",
                    provider="click-reading-context",
                    called_hermes=False,
                    status="failed",
                    request_payload={"discussion_id": discussion_id, "user_message_id": user_message_id},
                    response_payload={"error": message},
                    failure_reason=message,
                )
            raise VoiceProcessingExecutionError(message, processing_run_id=str(run["id"]))
        raise VoiceProcessingExecutionError(message)

    try:
        for evidence in snapshot.get("evidence_items") or []:
            if evidence.get("evidence_class") == "user_voice":
                continue
            resolved = resolve_snapshot_citation(str(snapshot["id"]), str(evidence["evidence_id"]))
            if not resolved.get("current_matches_snapshot"):
                raise CitationResolutionError("阅读证据来源后来发生变化，请刷新证据快照后再讨论")
    except Exception as exc:  # noqa: BLE001 - a stale/missing citation must stop the answer.
        message = f"无法验证讨论所需的阅读证据：{exc}"
        update_discussion_message(
            assistant_message_id,
            content="",
            context_snapshot_id=str(snapshot["id"]),
            status="failed",
            failure_reason=message,
        )
        with db.connect() as conn:
            run = insert_processing_run(
                conn,
                voice_record_id=str(discussion["voice_record_id"]),
                run_type="discuss",
                provider="click-reading-context",
                called_hermes=False,
                status="failed",
                request_payload={
                    "discussion_id": discussion_id,
                    "user_message_id": user_message_id,
                    "context_snapshot_id": snapshot["id"],
                },
                response_payload={"error": message},
                failure_reason=message,
            )
        raise _hermes_execution_error(message, processing_run_id=str(run["id"])) from exc

    voice_record_id = str(discussion["voice_record_id"])
    prior = _prior_successful_run(voice_record_id, "discuss", str(job["input_hash"]))
    if prior:
        prior_response = prior.get("response_payload") if isinstance(prior.get("response_payload"), dict) else {}
        prior_output = prior_response.get("output") if isinstance(prior_response.get("output"), dict) else {}
        if prior_output.get("schema") == "click.voice.hermes.discuss.output.v1":
            evidence_by_id = {
                str(item["evidence_id"]): item
                for item in snapshot.get("evidence_items") or []
                if item.get("evidence_id")
            }
            citations = [
                evidence_by_id[citation_id]
                for citation_id in prior_output.get("citation_ids") or []
                if citation_id in evidence_by_id
            ]
            from reader_api.voice_hermes import format_discussion_content

            update_discussion_message(
                assistant_message_id,
                content=format_discussion_content(prior_output),
                citations=citations,
                context_snapshot_id=str(snapshot["id"]),
                processing_run_id=str(prior["id"]),
                status="succeeded",
                structured_content=prior_output,
                adapter_version=str(prior.get("request_payload", {}).get("adapter_version") or ""),
                provider=str(prior.get("provider") or ""),
                model=str(prior.get("model") or ""),
            )
            return str(prior["id"])
    record = get_voice_record_or_404(voice_record_id)
    transcript, _ = effective_transcript(voice_record_id)
    history = [
        {"role": item["role"], "content": item["content"]}
        for item in list_discussion_messages(discussion_id)
        if item.get("status") == "succeeded"
        and str(item.get("id")) not in {assistant_message_id, user_message_id}
    ]
    started_at = now_iso()
    voice_adapter = adapter or get_voice_hermes_adapter()
    try:
        receipt = voice_adapter.discuss(
            discussion_id=discussion_id,
            voice_record=record,
            transcript=transcript,
            user_message=str(user_message.get("content") or ""),
            history=history,
            context_snapshot=snapshot,
        )
    except Exception as exc:  # noqa: BLE001 - preserve actual Hermes/schema failure.
        detail = str(exc)
        message = f"Hermes 暂时无法完成这轮讨论：{detail}。你的问题和上下文快照都已保留，可重试。"
        called_hermes = bool(getattr(exc, "called_hermes", True))
        raw_response = getattr(exc, "response", {})
        with db.connect() as conn:
            run = insert_processing_run(
                conn,
                voice_record_id=voice_record_id,
                run_type="discuss",
                provider="hermes-runtime",
                called_hermes=called_hermes,
                status="failed",
                request_payload={
                    "discussion_id": discussion_id,
                    "user_message_id": user_message_id,
                    "context_snapshot_id": snapshot["id"],
                    "operation_input_hash": job["input_hash"],
                },
                response_payload={"error": detail, "raw_response": raw_response},
                failure_reason=message,
                started_at=started_at,
            )
        execution_error = _hermes_execution_error(
            message,
            processing_run_id=str(run["id"]),
            discussion_message_id=assistant_message_id,
        )
        retrying = execution_error.minimum_max_attempts is not None
        update_discussion_message(
            assistant_message_id,
            content="",
            context_snapshot_id=str(snapshot["id"]),
            processing_run_id=str(run["id"]),
            status="pending" if retrying else "failed",
            failure_reason=message,
        )
        raise execution_error from exc

    output = receipt["output"]
    evidence_by_id = {
        str(item["evidence_id"]): item
        for item in snapshot.get("evidence_items") or []
        if item.get("evidence_id")
    }
    citations = [evidence_by_id[citation_id] for citation_id in output["citation_ids"]]
    with db.connect() as conn:
        run = insert_processing_run(
            conn,
            voice_record_id=voice_record_id,
            run_type="discuss",
            provider=str(receipt.get("provider") or "hermes-runtime"),
            model=str(receipt.get("model") or ""),
            called_hermes=True,
            status="success",
            request_payload={
                "schema": "click.voice.hermes.discuss.input.v1",
                "adapter_version": receipt["adapter_version"],
                "discussion_id": discussion_id,
                "user_message_id": user_message_id,
                "context_snapshot_id": snapshot["id"],
                "operation_input_hash": job["input_hash"],
                "prompt": receipt["prompt"],
            },
            response_payload={
                "output": output,
                "raw_response": receipt["raw_response"],
                "session_id": receipt["session_id"],
            },
            started_at=started_at,
        )
    update_discussion_message(
        assistant_message_id,
        content=str(receipt["content"]),
        citations=citations,
        context_snapshot_id=str(snapshot["id"]),
        processing_run_id=str(run["id"]),
        status="succeeded",
        structured_content=output,
        adapter_version=str(receipt["adapter_version"]),
        provider=str(receipt.get("provider") or "hermes-runtime"),
        model=str(receipt.get("model") or ""),
    )
    update_discussion_runtime(
        discussion_id,
        context_snapshot_id=str(snapshot["id"]),
        hermes_session_id=str(receipt["session_id"]),
    )
    return str(run["id"])


def _job_action(job: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    action_id = str(payload.get("action_id") or "")
    voice_record_id = str(job["voice_record_id"])
    with db.connect() as conn:
        action = action_by_id(conn, voice_record_id, action_id)
        record = conn.execute(
            "SELECT * FROM reader.voice_records WHERE id = %s AND deleted_at IS NULL",
            (voice_record_id,),
        ).fetchone()
    if not action:
        raise VoiceProcessingExecutionError("找不到要执行的动作，未写入任何目标。")
    if not record:
        raise VoiceProcessingExecutionError("录音记录不存在或已经软删除，未写入任何目标。")
    return action, dict(record)


def _insert_action_receipt(
    conn: Any,
    *,
    action: dict[str, Any],
    phase: str,
    target_adapter: str,
    target_identity: dict[str, Any],
    request_hash: str,
    status: str,
    receipt: Optional[dict[str, Any]] = None,
    failure_reason: Optional[str] = None,
    processing_run_id: Optional[str] = None,
) -> dict[str, Any]:
    row = conn.execute(
        """
        INSERT INTO reader.voice_action_receipts (
            id, action_id, voice_record_id, processing_run_id, phase,
            target_adapter, target_identity, request_hash, status,
            receipt, failure_reason, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        RETURNING *
        """,
        (
            f"varec_{uuid4().hex}",
            action["id"],
            action["voice_record_id"],
            processing_run_id,
            phase,
            target_adapter,
            db.jsonb(json_safe(target_identity)),
            request_hash,
            status,
            db.jsonb(json_safe(receipt or {})),
            failure_reason,
        ),
    ).fetchone()
    return dict(row)


def _record_action_failure(
    job: dict[str, Any],
    *,
    action: dict[str, Any],
    phase: str,
    target_adapter: str,
    target_identity: Optional[dict[str, Any]],
    error: Exception,
) -> str:
    voice_record_id = str(action["voice_record_id"])
    detail = str(error)
    prefix = "目标写入失败" if phase == "apply" else "目标读回验证失败"
    message = f"{prefix}：{detail}。录音和已有内容均已保留，可安全重试。"
    terminal = int(job.get("attempt_count") or 0) >= int(job.get("max_attempts") or 3)
    started_at = now_iso()
    with db.connect() as conn:
        run = insert_processing_run(
            conn,
            voice_record_id=voice_record_id,
            run_type="apply_action" if phase == "apply" else "validate",
            provider=target_adapter or "click-voice-action-target",
            called_hermes=False,
            status="failed",
            request_payload={
                "action_id": action["id"],
                "phase": phase,
                "operation_input_hash": job.get("input_hash"),
                "target_identity": target_identity or {},
            },
            response_payload={"error": detail},
            failure_reason=message,
            started_at=started_at,
        )
        _insert_action_receipt(
            conn,
            action=action,
            phase=phase,
            target_adapter=target_adapter or "unknown",
            target_identity=target_identity or {},
            request_hash=str(job.get("input_hash") or receipt_hash(job.get("payload") or {})),
            status="failed",
            failure_reason=message,
            processing_run_id=str(run["id"]),
        )
        conn.execute(
            """
            UPDATE reader.voice_actions
            SET status = CASE WHEN %s THEN 'failed_apply' ELSE status END,
                failure_reason = %s
            WHERE id = %s
            """,
            (terminal, message, action["id"]),
        )
        if terminal:
            conn.execute(
                """
                UPDATE reader.voice_records
                SET status = 'failed_action', failure_code = %s,
                    failure_message = %s, updated_at = now()
                WHERE id = %s AND deleted_at IS NULL
                """,
                ("action_write_failed" if phase == "apply" else "action_validation_failed", message, voice_record_id),
            )
    return str(run["id"])


def _enqueue_action_validation(action: dict[str, Any], identity: dict[str, Any], adapter_name: str) -> dict[str, Any]:
    validation_hash = receipt_hash(
        {"action_id": action["id"], "target_adapter": adapter_name, "target_identity": identity}
    )
    job = enqueue_processing_job(
        str(action["voice_record_id"]),
        "validate_action",
        payload={"action_id": action["id"], "target_adapter": adapter_name},
        input_hash=validation_hash,
        priority=5,
        max_attempts=3,
        retry_failed=True,
    )
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE reader.voice_actions
            SET status = CASE WHEN status = 'validated' THEN status ELSE 'validating' END,
                validate_job_id = %s, failure_reason = NULL
            WHERE id = %s
            """,
            (job.get("id"), action["id"]),
        )
    return job


def process_apply_action_job(job: dict[str, Any]) -> str:
    action, record = _job_action(job)
    if action.get("status") == "validated" and action.get("validation_receipt"):
        return str((action.get("validation_receipt") or {}).get("processing_run_id") or "") or None
    adapter_name = str(action.get("target_adapter") or (job.get("payload") or {}).get("target_adapter") or "")
    identity = action.get("target_identity") if isinstance(action.get("target_identity"), dict) else {}
    if identity and adapter_name and action.get("status") in {"applied", "validating"}:
        _enqueue_action_validation(action, identity, adapter_name)
        with db.connect() as conn:
            prior = conn.execute(
                """
                SELECT id FROM reader.voice_processing_runs
                WHERE voice_record_id = %s AND run_type = 'apply_action' AND status = 'success'
                  AND request_payload->>'action_id' = %s
                ORDER BY created_at DESC LIMIT 1
                """,
                (record["id"], action["id"]),
            ).fetchone()
        return str(prior["id"]) if prior else None
    started_at = now_iso()
    try:
        adapter = adapter_by_name(adapter_name)
        adapter.validate_request(action, record)
        transcript, _ = effective_transcript(str(record["id"]))
        write_result = adapter.apply(action, record, transcript)
    except Exception as exc:  # noqa: BLE001 - target failures must become durable and retryable.
        run_id = _record_action_failure(
            job,
            action=action,
            phase="apply",
            target_adapter=adapter_name,
            target_identity=identity,
            error=exc,
        )
        raise VoiceProcessingExecutionError(str(exc), processing_run_id=run_id) from exc
    identity = write_result.identity
    with db.connect() as conn:
        run = insert_processing_run(
            conn,
            voice_record_id=str(record["id"]),
            run_type="apply_action",
            provider=write_result.adapter,
            called_hermes=False,
            status="success",
            request_payload={
                "action_id": action["id"],
                "operation_input_hash": job.get("input_hash"),
                "target_adapter": write_result.adapter,
            },
            response_payload={"target_identity": identity, "write_receipt": write_result.receipt},
            started_at=started_at,
        )
        receipt = _insert_action_receipt(
            conn,
            action=action,
            phase="apply",
            target_adapter=write_result.adapter,
            target_identity=identity,
            request_hash=str(job.get("input_hash") or receipt_hash(action)),
            status="success",
            receipt=write_result.receipt,
            processing_run_id=str(run["id"]),
        )
        conn.execute(
            """
            UPDATE reader.voice_actions
            SET status = 'applied', target_adapter = %s, target_identity = %s,
                applied_result = %s, applied_at = COALESCE(applied_at, now()),
                failure_reason = NULL
            WHERE id = %s
            """,
            (
                write_result.adapter,
                db.jsonb(json_safe(identity)),
                db.jsonb(json_safe({"schema": "click.voice.action.apply.v2", "receipt_id": receipt["id"], "target_identity": identity})),
                action["id"],
            ),
        )
    action = {**action, "target_adapter": write_result.adapter, "target_identity": identity, "status": "applied"}
    _enqueue_action_validation(action, identity, write_result.adapter)
    return str(run["id"])


def _settle_record_after_validation(conn: Any, action: dict[str, Any]) -> None:
    action_type = str(action.get("action_type") or "")
    if action_type in {"archive", "ask_followup", "no_action"}:
        return
    outstanding = conn.execute(
        """
        SELECT count(*) AS count
        FROM reader.voice_actions
        WHERE voice_record_id = %s
          AND id <> %s
          AND status IN ('proposed', 'pending_user_confirmation', 'applying', 'applied', 'validating', 'failed_apply')
        """,
        (action["voice_record_id"], action["id"]),
    ).fetchone()
    next_status = "settled" if int(outstanding["count"] or 0) == 0 else "needs_user_confirmation"
    conn.execute(
        """
        UPDATE reader.voice_records
        SET status = %s, failure_code = NULL, failure_message = NULL,
            processed_at = COALESCE(processed_at, now()), updated_at = now()
        WHERE id = %s AND deleted_at IS NULL
        """,
        (next_status, action["voice_record_id"]),
    )


def process_validate_action_job(job: dict[str, Any]) -> str:
    action, record = _job_action(job)
    if action.get("status") == "validated" and action.get("validation_receipt"):
        return str((action.get("validation_receipt") or {}).get("processing_run_id") or "") or None
    adapter_name = str(action.get("target_adapter") or (job.get("payload") or {}).get("target_adapter") or "")
    identity = action.get("target_identity") if isinstance(action.get("target_identity"), dict) else {}
    if not identity:
        exc = VoiceActionTargetError("动作没有保存目标身份，不能进行读回验证")
        run_id = _record_action_failure(
            job,
            action=action,
            phase="validate",
            target_adapter=adapter_name,
            target_identity=identity,
            error=exc,
        )
        raise VoiceProcessingExecutionError(str(exc), processing_run_id=run_id)
    started_at = now_iso()
    try:
        adapter = adapter_by_name(adapter_name)
        validation = adapter.read_back(action, record, identity)
        if validation.get("validated") is not True:
            raise VoiceActionTargetError("目标读回没有返回明确的验证成功")
    except Exception as exc:  # noqa: BLE001 - read-back mismatch must never be treated as success.
        run_id = _record_action_failure(
            job,
            action=action,
            phase="validate",
            target_adapter=adapter_name,
            target_identity=identity,
            error=exc,
        )
        raise VoiceProcessingExecutionError(str(exc), processing_run_id=run_id) from exc
    with db.connect() as conn:
        run = insert_processing_run(
            conn,
            voice_record_id=str(record["id"]),
            run_type="validate",
            provider=adapter_name,
            called_hermes=False,
            status="success",
            request_payload={
                "action_id": action["id"],
                "operation_input_hash": job.get("input_hash"),
                "target_identity": identity,
            },
            response_payload=validation,
            started_at=started_at,
        )
        receipt = _insert_action_receipt(
            conn,
            action=action,
            phase="validate",
            target_adapter=adapter_name,
            target_identity=identity,
            request_hash=str(job.get("input_hash") or receipt_hash(identity)),
            status="success",
            receipt=validation,
            processing_run_id=str(run["id"]),
        )
        validation_receipt = {
            "schema": "click.voice.action.validation.v2",
            "validated": True,
            "receipt_id": receipt["id"],
            "processing_run_id": run["id"],
            "target_identity": identity,
            "read_back": validation,
        }
        conn.execute(
            """
            UPDATE reader.voice_actions
            SET status = 'validated', validation_receipt = %s,
                validated_at = now(), failure_reason = NULL
            WHERE id = %s
            """,
            (db.jsonb(json_safe(validation_receipt)), action["id"]),
        )
        _settle_record_after_validation(conn, action)
    return str(run["id"])


class VoiceProcessingWorker:
    def __init__(
        self,
        *,
        handlers: Optional[dict[str, JobHandler]] = None,
        poll_interval: float = 0.75,
        max_idle_interval: float = 5.0,
        database_failure_initial_interval: float = 5.0,
        database_failure_max_interval: float = 30.0,
        lease_seconds: int = 240,
        retry_delay_seconds: int = 5,
        worker_id: Optional[str] = None,
    ) -> None:
        self.handlers = handlers or {
            "transcribe": process_transcribe_job,
            "clean_transcript": process_clean_transcript_job,
            "understand": process_understand_job,
            "build_context": process_build_context_job,
            "prepare_discussion": process_prepare_discussion_job,
            "apply_action": process_apply_action_job,
            "validate_action": process_validate_action_job,
        }
        self.poll_interval = max(0.02, float(poll_interval))
        self.max_idle_interval = max(self.poll_interval, float(max_idle_interval))
        self.database_failure_initial_interval = max(
            self.poll_interval,
            float(database_failure_initial_interval),
        )
        self.database_failure_max_interval = max(
            self.database_failure_initial_interval,
            float(database_failure_max_interval),
        )
        self.lease_seconds = max(30, int(lease_seconds))
        self.retry_delay_seconds = max(0, int(retry_delay_seconds))
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:10]}"
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._metrics_lock = threading.Lock()
        self._metrics_started_at = now_iso()
        self._metrics: dict[str, int] = {
            "claim_attempts": 0,
            "empty_claims": 0,
            "claimed_jobs": 0,
            "succeeded_jobs": 0,
            "failed_jobs": 0,
            "claim_errors": 0,
            "loop_errors": 0,
            "wake_signals": 0,
            "wake_consumed": 0,
            "wait_timeouts": 0,
        }
        self._current_wait_seconds = 0.0
        self._current_wait_reason = "starting"
        self._last_claim_at: Optional[str] = None
        self._last_wake_at: Optional[str] = None
        self.last_error: Optional[str] = None

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> bool:
        if self.is_running:
            return False
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="click-voice-processing")
        self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=max(0.0, timeout))

    def wake(self) -> None:
        self._increment_metric("wake_signals")
        with self._metrics_lock:
            self._last_wake_at = now_iso()
        self._wake_event.set()

    def metrics_snapshot(self) -> dict[str, Any]:
        with self._metrics_lock:
            counters = dict(self._metrics)
            return {
                "schema": "click.voice.processing_worker.metrics.v1",
                "running": self.is_running,
                "started_at": self._metrics_started_at,
                "poll_interval_seconds": self.poll_interval,
                "max_idle_interval_seconds": self.max_idle_interval,
                "database_failure_initial_interval_seconds": self.database_failure_initial_interval,
                "database_failure_max_interval_seconds": self.database_failure_max_interval,
                "current_wait_seconds": self._current_wait_seconds,
                "current_wait_reason": self._current_wait_reason,
                "last_claim_at": self._last_claim_at,
                "last_wake_at": self._last_wake_at,
                "last_error": self.last_error,
                "counters": counters,
            }

    def _increment_metric(self, name: str) -> None:
        with self._metrics_lock:
            self._metrics[name] = self._metrics.get(name, 0) + 1

    def _wait_for_wake(self, timeout: float, *, reason: str) -> bool:
        with self._metrics_lock:
            self._current_wait_seconds = float(timeout)
            self._current_wait_reason = reason
        signaled = self._wake_event.wait(timeout)
        self._wake_event.clear()
        with self._metrics_lock:
            self._current_wait_seconds = 0.0
            self._current_wait_reason = "running"
            key = "wake_consumed" if signaled else "wait_timeouts"
            self._metrics[key] = self._metrics.get(key, 0) + 1
        return signaled

    def process_once(self) -> bool:
        self._increment_metric("claim_attempts")
        with self._metrics_lock:
            self._last_claim_at = now_iso()
        try:
            job = claim_next_processing_job(
                self.worker_id,
                lease_seconds=self.lease_seconds,
                job_types=list(self.handlers),
            )
        except Exception:
            self._increment_metric("claim_errors")
            raise
        if not job:
            self._increment_metric("empty_claims")
            return False
        self._increment_metric("claimed_jobs")
        handler = self.handlers[str(job["job_type"])]
        try:
            run_id = handler(job)
            complete_processing_job(str(job["id"]), processing_run_id=run_id)
            self.last_error = None
            self._increment_metric("succeeded_jobs")
        except VoiceProcessingExecutionError as exc:
            self.last_error = str(exc)
            self._increment_metric("failed_jobs")
            failed_job = fail_processing_job(
                str(job["id"]),
                str(exc),
                retry_delay_seconds=(
                    exc.retry_delay_seconds
                    if exc.retry_delay_seconds is not None
                    else self.retry_delay_seconds
                ),
                processing_run_id=exc.processing_run_id,
                minimum_max_attempts=exc.minimum_max_attempts,
            )
            retrying = failed_job["status"] == "pending"
            record_status = exc.retry_record_status if retrying else exc.terminal_record_status
            if record_status:
                with db.connect() as conn:
                    conn.execute(
                        "UPDATE reader.voice_records SET status = %s, updated_at = now() WHERE id = %s",
                        (record_status, job["voice_record_id"]),
                    )
            if exc.discussion_message_id and not retrying:
                update_discussion_message(
                    exc.discussion_message_id,
                    content="",
                    processing_run_id=exc.processing_run_id,
                    status="failed",
                    failure_reason=str(exc),
                )
        except Exception as exc:  # noqa: BLE001 - the lease is the crash recovery boundary.
            self.last_error = str(exc)
            self._increment_metric("failed_jobs")
            fail_processing_job(
                str(job["id"]),
                str(exc),
                retry_delay_seconds=self.retry_delay_seconds,
            )
        return True

    def _run(self) -> None:
        idle_wait = BoundedAdaptiveWait(self.poll_interval, self.max_idle_interval)
        database_wait = BoundedAdaptiveWait(
            self.database_failure_initial_interval,
            self.database_failure_max_interval,
        )
        while not self._stop_event.is_set():
            try:
                processed = self.process_once()
            except Exception as exc:  # noqa: BLE001 - database outages must not stop Runtime startup.
                self.last_error = str(exc)
                self._increment_metric("loop_errors")
                signaled = self._wait_for_wake(
                    database_wait.current_seconds,
                    reason="database_failure",
                )
                if signaled:
                    idle_wait.reset()
                    database_wait.reset()
                else:
                    database_wait.advance()
                continue
            database_wait.reset()
            if processed:
                idle_wait.reset()
                continue
            signaled = self._wait_for_wake(idle_wait.current_seconds, reason="idle")
            if signaled:
                idle_wait.reset()
            else:
                idle_wait.advance()


_WORKER_LOCK = threading.Lock()
_WORKER: Optional[VoiceProcessingWorker] = None


def start_voice_processing_worker() -> VoiceProcessingWorker:
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is None:
            _WORKER = VoiceProcessingWorker()
        _WORKER.start()
        return _WORKER


def wake_voice_processing_worker() -> None:
    with _WORKER_LOCK:
        worker = _WORKER
    if worker:
        worker.wake()


def voice_processing_worker_metrics() -> dict[str, Any]:
    with _WORKER_LOCK:
        worker = _WORKER
    if worker is None:
        return {
            "schema": "click.voice.processing_worker.metrics.v1",
            "running": False,
            "counters": {},
        }
    return worker.metrics_snapshot()


def stop_voice_processing_worker() -> None:
    global _WORKER
    with _WORKER_LOCK:
        worker = _WORKER
        _WORKER = None
    if worker:
        worker.stop()
