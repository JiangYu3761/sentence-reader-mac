from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException

from reader_api import db
from reader_api.voice_inbox_service import (
    VOICE_RECORD_STATUSES,
    create_transcript_version,
    file_hash,
    json_safe,
    list_processing_jobs,
    list_transcript_versions,
    new_id,
)


TINGLE_SCHEMA = "tingle.inspiration.v1"
TINGLE_DELETION_RECEIPT_SCHEMA = "tingle.deletion_receipt.v1"
TINGLE_TOMBSTONE_SCHEMA = "tingle.tombstone.v2"
TINGLE_RETENTION_DAYS = 30
TINGLE_VOICE_SOURCES = {"mac", "click_mobile"}
LEGACY_HERMES_INSPIRATION_PATH = Path(
    os.getenv(
        "TINGLE_LEGACY_HERMES_INSPIRATION_PATH",
        str(Path.home() / "Documents" / "Codex" / "2026-07-04" / "hermes-灵感记录.md"),
    )
).expanduser()


def _content_hash(title: str, content: str) -> str:
    return hashlib.sha256(f"{title}\0{content}".encode("utf-8")).hexdigest()


def _deletion_confirmation_hash(row: dict[str, Any]) -> str:
    record = row.get("voice_record") if isinstance(row.get("voice_record"), dict) else None
    payload = {
        "schema": TINGLE_SCHEMA,
        "inspiration_id": row.get("id"),
        "kind": row.get("kind"),
        "voice_record_id": record.get("id") if record else None,
        "source": record.get("source") if record else "hermes",
        "origin_ref": record.get("origin_ref") if record else row.get("external_ref"),
        "title": record.get("title") if record else row.get("title"),
        "content": record.get("transcript") if record else row.get("content"),
        "audio_uri": record.get("audio_uri") if record else None,
        "audio_hash": record.get("audio_hash") if record else None,
        "archived_at": row.get("archived_at"),
        "purge_after": row.get("purge_after"),
        "linked_external_target_count": int(row.get("linked_external_target_count") or 0),
    }
    encoded = json.dumps(json_safe(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _clean_title(value: Any, *, fallback: str = "未命名灵感") -> str:
    title = re.sub(r"\s+", " ", str(value or "").strip()).strip('"“”')
    return title[:80] if title else fallback


def _clean_content(value: Any) -> str:
    return str(value or "").strip()[:200_000]


def _source_date(title: str, fallback: datetime) -> datetime:
    match = re.search(r"(20\d{2}-\d{2}-\d{2})", title)
    if not match:
        return fallback
    try:
        return datetime.fromisoformat(match.group(1)).replace(tzinfo=timezone.utc)
    except ValueError:
        return fallback


def _display_legacy_title(title: str) -> str:
    cleaned = re.sub(r"\s*[（(]20\d{2}-\d{2}-\d{2}[^）)]*[）)]\s*$", "", title).strip()
    return _clean_title(cleaned or title)


def parse_legacy_hermes_markdown(text: str, *, source_path: Path) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    heading: Optional[str] = None
    body: list[str] = []
    occurrence: dict[str, int] = {}
    fallback = datetime.fromtimestamp(source_path.stat().st_mtime, tz=timezone.utc) if source_path.exists() else datetime.now(timezone.utc)

    def flush() -> None:
        nonlocal heading, body
        if not heading:
            return
        content = "\n".join(body).strip()
        normalized = heading.strip()
        index = occurrence.get(normalized, 0)
        occurrence[normalized] = index + 1
        external_ref = f"legacy-hermes:{hashlib.sha256(f'{source_path}:{normalized}:{index}'.encode('utf-8')).hexdigest()[:24]}"
        sections.append(
            {
                "external_ref": external_ref,
                "title": _display_legacy_title(normalized),
                "content": content,
                "source_created_at": _source_date(normalized, fallback),
                "metadata": {
                    "schema": TINGLE_SCHEMA,
                    "legacy_heading": normalized,
                    "legacy_path": str(source_path),
                    "legacy_occurrence": index,
                },
            }
        )
        heading = None
        body = []

    for line in text.splitlines():
        if line.startswith("## "):
            candidate = line[3:].strip()
            # This legacy file used dated headings for entries and undated H2s
            # for structure inside an entry. Preserve those inner headings.
            starts_entry = heading is None or bool(re.search(r"20\d{2}-\d{2}-\d{2}", candidate))
            if starts_entry:
                flush()
                heading = candidate
                continue
            body.append(line)
            continue
        if heading is not None:
            body.append(line)
    flush()
    return sections


def import_legacy_hermes_inspirations(path: Path = LEGACY_HERMES_INSPIRATION_PATH) -> dict[str, int]:
    if not path.is_file():
        return {"found": 0, "inserted": 0}
    sections = parse_legacy_hermes_markdown(path.read_text(encoding="utf-8"), source_path=path)
    inserted = 0
    with db.connect() as conn:
        for item in sections:
            tombstone = conn.execute(
                "SELECT 1 FROM reader.tingle_tombstones WHERE external_ref = %s",
                (item["external_ref"],),
            ).fetchone()
            if tombstone:
                continue
            row = conn.execute(
                """
                INSERT INTO reader.tingle_inspirations (
                    id, kind, external_ref, title, content, content_hash,
                    source_created_at, metadata, created_at, updated_at
                )
                VALUES (%s, 'hermes_text', %s, %s, %s, %s, %s, %s, now(), now())
                ON CONFLICT (external_ref) WHERE external_ref IS NOT NULL DO NOTHING
                RETURNING id
                """,
                (
                    new_id("tin"),
                    item["external_ref"],
                    item["title"],
                    item["content"],
                    _content_hash(item["title"], item["content"]),
                    item["source_created_at"],
                    db.jsonb(item["metadata"]),
                ),
            ).fetchone()
            inserted += int(bool(row))
    return {"found": len(sections), "inserted": inserted}


def sync_voice_inspirations() -> int:
    with db.connect() as conn:
        rows = conn.execute(
            """
            INSERT INTO reader.tingle_inspirations (
                id, kind, voice_record_id, content_hash, source_created_at,
                archived_at, metadata, created_at, updated_at
            )
            SELECT
                'tin_' || md5(vr.id),
                'voice',
                vr.id,
                md5(vr.id || ':' || vr.audio_hash),
                COALESCE(vr.recorded_at, vr.created_at),
                CASE WHEN vr.status = 'archived' THEN COALESCE(vr.archived_at, vr.updated_at) ELSE NULL END,
                jsonb_build_object('schema', %s::text, 'created_by', 'tingle_sync'),
                vr.created_at,
                vr.updated_at
            FROM reader.voice_records vr
            WHERE vr.source IN ('mac', 'click_mobile')
              AND COALESCE(vr.metadata->>'origin_kind', '') <> 'reader_audio_note'
              AND vr.deleted_at IS NULL
              AND NOT EXISTS (
                SELECT 1
                FROM reader.tingle_tombstones tombstone
                WHERE tombstone.kind = 'voice'
                  AND (
                    (tombstone.voice_source = vr.source AND tombstone.origin_ref = vr.origin_ref)
                    OR tombstone.audio_hash = vr.audio_hash
                  )
              )
            ON CONFLICT (voice_record_id) DO NOTHING
            RETURNING id
            """,
            (TINGLE_SCHEMA,),
        ).fetchall()
    return len(rows)


def reconcile_tingle_inspirations() -> dict[str, int]:
    legacy = import_legacy_hermes_inspirations()
    return {
        "voice_inserted": sync_voice_inspirations(),
        "legacy_found": legacy["found"],
        "legacy_inserted": legacy["inserted"],
    }


def _row_for_inspiration(conn: Any, inspiration_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        """
        SELECT ti.*, to_jsonb(vr) AS voice_record,
               COALESCE((
                   SELECT count(*)::int
                   FROM reader.voice_record_links link
                   WHERE link.voice_record_id = vr.id
               ), 0) AS linked_external_target_count
        FROM reader.tingle_inspirations ti
        LEFT JOIN reader.voice_records vr ON vr.id = ti.voice_record_id
        WHERE ti.id = %s
        """,
        (inspiration_id,),
    ).fetchone()
    return dict(row) if row else None


def _locked_row_for_inspiration(conn: Any, inspiration_id: str) -> Optional[dict[str, Any]]:
    inspiration = conn.execute(
        "SELECT * FROM reader.tingle_inspirations WHERE id = %s FOR UPDATE",
        (inspiration_id,),
    ).fetchone()
    if not inspiration:
        return None
    row = dict(inspiration)
    record = None
    linked_count = 0
    if row.get("voice_record_id"):
        locked_record = conn.execute(
            "SELECT * FROM reader.voice_records WHERE id = %s FOR UPDATE",
            (row["voice_record_id"],),
        ).fetchone()
        record = dict(locked_record) if locked_record else None
        linked = conn.execute(
            "SELECT count(*)::int AS count FROM reader.voice_record_links WHERE voice_record_id = %s",
            (row["voice_record_id"],),
        ).fetchone()
        linked_count = int(linked["count"] if linked else 0)
    row["voice_record"] = record
    row["linked_external_target_count"] = linked_count
    return row


def _processing_state(record: dict[str, Any]) -> str:
    status = str(record.get("status") or "")
    if status.startswith("failed"):
        return "failed"
    if status in {
        "recording", "saved_local", "upload_pending", "uploading", "uploaded",
        "transcribe_pending", "transcribing", "transcribed", "cleanup_pending",
        "cleaning", "cleaned", "understand_pending", "understanding",
    }:
        return "processing"
    return "ready"


def _public_failure_message(record: dict[str, Any]) -> Optional[str]:
    message = str(record.get("failure_message") or "").strip()
    if not message:
        return None
    normalized = message.lower()
    if "did not return text" in normalized or "empty transcript" in normalized:
        return "录音中没有识别到清晰语音。原始音频已保留，请确认麦克风音量后重试。"
    return message


def _public_inspiration(row: dict[str, Any]) -> dict[str, Any]:
    record = row.get("voice_record") if isinstance(row.get("voice_record"), dict) else None
    archived_at = row.get("archived_at")
    confirmation_hash = _deletion_confirmation_hash(row)
    linked_count = int(row.get("linked_external_target_count") or 0)
    if record:
        title = str(record.get("title") or "").strip()
        state = _processing_state(record)
        if title in {"", "新语音", "录音", "未命名语音"}:
            title = "正在生成标题" if state == "processing" else "未命名灵感"
        content = str(record.get("transcript") or "").strip()
        return {
            "schema": TINGLE_SCHEMA,
            "id": row["id"],
            "kind": "voice",
            "title": title,
            "content": content,
            "preview": content or ("正在转写..." if state == "processing" else "还没有文字内容"),
            "source": str(record.get("source") or ""),
            "voice_record_id": record.get("id"),
            "has_audio": True,
            "audio_url": f"/voice-records/{record['id']}/audio",
            "duration_ms": record.get("duration_ms"),
            "state": "archived" if archived_at else state,
            "failure_message": _public_failure_message(record),
            "created_at": row.get("source_created_at") or record.get("recorded_at") or row.get("created_at"),
            "updated_at": record.get("updated_at") or row.get("updated_at"),
            "archived_at": archived_at,
            "purge_after": row.get("purge_after"),
            "content_hash": confirmation_hash,
            "linked_external_target_count": linked_count,
            "permanent_delete_available": True,
        }
    return {
        "schema": TINGLE_SCHEMA,
        "id": row["id"],
        "kind": "hermes_text",
        "title": row.get("title") or "未命名灵感",
        "content": row.get("content") or "",
        "preview": row.get("content") or "",
        "source": "hermes",
        "voice_record_id": None,
        "has_audio": False,
        "audio_url": None,
        "duration_ms": None,
        "state": "archived" if archived_at else "ready",
        "failure_message": None,
        "created_at": row.get("source_created_at") or row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "archived_at": archived_at,
        "purge_after": row.get("purge_after"),
        "content_hash": confirmation_hash,
        "linked_external_target_count": 0,
        "permanent_delete_available": True,
    }


def list_tingle_inspirations(*, archived: bool = False, query: str = "", limit: int = 200) -> list[dict[str, Any]]:
    reconcile_tingle_inspirations()
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT ti.*, to_jsonb(vr) AS voice_record
            FROM reader.tingle_inspirations ti
            LEFT JOIN reader.voice_records vr ON vr.id = ti.voice_record_id
            WHERE (
                (%s AND ti.archived_at IS NOT NULL)
                OR (NOT %s AND ti.archived_at IS NULL)
            )
              AND (ti.kind = 'hermes_text' OR vr.deleted_at IS NULL)
            ORDER BY COALESCE(ti.source_created_at, ti.created_at) DESC, ti.id DESC
            LIMIT %s
            """,
            (archived, archived, max(1, min(int(limit), 500))),
        ).fetchall()
    items = [_public_inspiration(dict(row)) for row in rows]
    needle = query.strip().casefold()
    if needle:
        items = [item for item in items if needle in f"{item['title']} {item['content']}".casefold()]
    return items


def _detail_inspiration(row: dict[str, Any]) -> dict[str, Any]:
    item = _public_inspiration(row)
    if item["kind"] == "voice":
        voice_record_id = str(item["voice_record_id"])
        item["transcript_versions"] = list_transcript_versions(voice_record_id)
        item["jobs"] = list_processing_jobs(voice_record_id)
    else:
        item["transcript_versions"] = []
        item["jobs"] = []
    return item


def get_tingle_inspiration(inspiration_id: str) -> dict[str, Any]:
    reconcile_tingle_inspirations()
    with db.connect() as conn:
        row = _row_for_inspiration(conn, inspiration_id)
    if not row:
        raise HTTPException(status_code=404, detail="Tingle inspiration not found")
    return _detail_inspiration(row)


def get_tingle_inspiration_for_voice_record(voice_record_id: str) -> dict[str, Any]:
    reconcile_tingle_inspirations()
    with db.connect() as conn:
        identity = conn.execute(
            "SELECT id FROM reader.tingle_inspirations WHERE voice_record_id = %s",
            (voice_record_id,),
        ).fetchone()
        row = _row_for_inspiration(conn, str(identity["id"])) if identity else None
    if not row:
        raise HTTPException(status_code=404, detail="Tingle inspiration not found for VoiceRecord")
    return _detail_inspiration(row)


def create_hermes_inspiration(
    *,
    title: str,
    content: str,
    external_ref: Optional[str] = None,
    source_created_at: Optional[datetime] = None,
) -> dict[str, Any]:
    resolved_content = _clean_content(content)
    if not resolved_content:
        raise HTTPException(status_code=422, detail="inspiration content is empty")
    resolved_title = _clean_title(title, fallback=resolved_content.splitlines()[0][:80])
    resolved_external_ref = external_ref.strip() if external_ref else None
    with db.connect() as conn:
        if resolved_external_ref and conn.execute(
            "SELECT 1 FROM reader.tingle_tombstones WHERE external_ref = %s",
            (resolved_external_ref,),
        ).fetchone():
            raise HTTPException(status_code=409, detail="this inspiration was permanently deleted")
        row = conn.execute(
            """
            INSERT INTO reader.tingle_inspirations (
                id, kind, external_ref, title, content, content_hash,
                source_created_at, metadata, created_at, updated_at
            )
            VALUES (%s, 'hermes_text', %s, %s, %s, %s, %s, %s, now(), now())
            ON CONFLICT (external_ref) WHERE external_ref IS NOT NULL DO UPDATE SET
              title = EXCLUDED.title,
              content = EXCLUDED.content,
              content_hash = EXCLUDED.content_hash,
              updated_at = now()
            RETURNING *
            """,
            (
                new_id("tin"),
                resolved_external_ref,
                resolved_title,
                resolved_content,
                _content_hash(resolved_title, resolved_content),
                source_created_at,
                db.jsonb({"schema": TINGLE_SCHEMA, "created_by": "hermes"}),
            ),
        ).fetchone()
    return _public_inspiration({**dict(row), "voice_record": None})


def update_tingle_inspiration(
    inspiration_id: str,
    *,
    title: Optional[str] = None,
    content: Optional[str] = None,
) -> dict[str, Any]:
    if title is None and content is None:
        return get_tingle_inspiration(inspiration_id)
    with db.connect() as conn:
        row = _row_for_inspiration(conn, inspiration_id)
        if not row:
            raise HTTPException(status_code=404, detail="Tingle inspiration not found")
        record = row.get("voice_record") if isinstance(row.get("voice_record"), dict) else None
        if record:
            updates: list[str] = []
            params: list[Any] = []
            if title is not None:
                updates.extend(["title = %s", "metadata = metadata || %s"])
                params.extend([
                    _clean_title(title),
                    db.jsonb({"tingle_user_title_override": True}),
                ])
            if content is not None:
                resolved_content = _clean_content(content)
                if not resolved_content:
                    raise HTTPException(status_code=422, detail="inspiration content is empty")
                version = create_transcript_version(
                    conn,
                    voice_record_id=str(record["id"]),
                    version_type="user_edited",
                    parent_version_id=str(record.get("active_transcript_version_id")) if record.get("active_transcript_version_id") else None,
                    content=resolved_content,
                    provider="tingle_user",
                    changes=[{"type": "user_edit", "confirmed": True}],
                    metadata={"edited_in": "tingle"},
                )
                updates.extend(["transcript = %s", "active_transcript_version_id = %s"])
                params.extend([resolved_content, version["id"]])
            if updates:
                params.append(record["id"])
                conn.execute(
                    f"UPDATE reader.voice_records SET {', '.join(updates)}, updated_at = now() WHERE id = %s",
                    tuple(params),
                )
        else:
            resolved_title = _clean_title(title if title is not None else row.get("title"))
            resolved_content = _clean_content(content if content is not None else row.get("content"))
            if not resolved_content:
                raise HTTPException(status_code=422, detail="inspiration content is empty")
            conn.execute(
                """
                UPDATE reader.tingle_inspirations
                SET title = %s, content = %s, content_hash = %s, updated_at = now()
                WHERE id = %s
                """,
                (resolved_title, resolved_content, _content_hash(resolved_title, resolved_content), inspiration_id),
            )
        conn.execute(
            "UPDATE reader.tingle_inspirations SET updated_at = now() WHERE id = %s",
            (inspiration_id,),
        )
    return get_tingle_inspiration(inspiration_id)


def archive_tingle_inspiration(inspiration_id: str) -> dict[str, Any]:
    with db.connect() as conn:
        row = _row_for_inspiration(conn, inspiration_id)
        if not row:
            raise HTTPException(status_code=404, detail="Tingle inspiration not found")
        if row.get("archived_at") is not None:
            return _public_inspiration(row)
        record = row.get("voice_record") if isinstance(row.get("voice_record"), dict) else None
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        if record:
            metadata = {**metadata, "status_before_archive": record.get("status")}
            conn.execute(
                """
                UPDATE reader.voice_records
                SET status = 'archived', archived_at = now(), updated_at = now()
                WHERE id = %s
                """,
                (record["id"],),
            )
        conn.execute(
            """
            UPDATE reader.tingle_inspirations
            SET archived_at = now(),
                purge_after = now() + make_interval(days => %s),
                metadata = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (TINGLE_RETENTION_DAYS, db.jsonb(json_safe(metadata)), inspiration_id),
        )
    return get_tingle_inspiration(inspiration_id)


def restore_tingle_inspiration(inspiration_id: str) -> dict[str, Any]:
    with db.connect() as conn:
        row = _row_for_inspiration(conn, inspiration_id)
        if not row:
            raise HTTPException(status_code=404, detail="Tingle inspiration not found")
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        record = row.get("voice_record") if isinstance(row.get("voice_record"), dict) else None
        if record and record.get("status") == "archived":
            previous = str(metadata.get("status_before_archive") or "")
            if previous not in VOICE_RECORD_STATUSES or previous in {"archived", "deleted"}:
                previous = "action_pending" if record.get("transcript") and record.get("hermes_result_id") else "understand_pending" if record.get("transcript") else "transcribe_pending"
            conn.execute(
                """
                UPDATE reader.voice_records
                SET status = %s, archived_at = NULL, updated_at = now()
                WHERE id = %s
                """,
                (previous, record["id"]),
            )
        conn.execute(
            """
            UPDATE reader.tingle_inspirations
            SET archived_at = NULL, purge_after = NULL, updated_at = now()
            WHERE id = %s
            """,
            (inspiration_id,),
        )
    return get_tingle_inspiration(inspiration_id)


def _trusted_audio_path(record: dict[str, Any]) -> Optional[Path]:
    if str(record.get("source") or "") not in TINGLE_VOICE_SOURCES:
        return None
    path = Path(str(record.get("audio_uri") or "")).expanduser()
    roots = [
        Path.home() / "Library" / "Application Support" / "Click" / "VoiceInbox",
        Path.home() / "Documents" / "Recordings" / "Click" / "Standalone",
    ]
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        return None
    for root in roots:
        try:
            resolved.relative_to(root.resolve(strict=False))
            return resolved
        except ValueError:
            continue
    return None


def _owned_audio_derivatives(original_path: Path) -> list[Path]:
    if original_path.name.endswith(".funasr.wav"):
        return []
    candidate = original_path.with_suffix(".funasr.wav")
    if candidate.is_symlink():
        raise HTTPException(status_code=409, detail="发现异常的转写音频副本，已阻止永久删除")
    if not candidate.exists():
        return []
    if not candidate.is_file():
        raise HTTPException(status_code=409, detail="发现异常的转写音频副本，已阻止永久删除")
    if candidate.resolve(strict=True).parent != original_path.resolve(strict=False).parent:
        raise HTTPException(status_code=409, detail="转写音频副本不在可信目录，已阻止永久删除")
    return [candidate.resolve(strict=True)]


def _existing_deletion_receipt(
    conn: Any,
    inspiration_id: str,
    expected_content_hash: str,
) -> Optional[dict[str, Any]]:
    tombstone = conn.execute(
        "SELECT * FROM reader.tingle_tombstones WHERE inspiration_id = %s",
        (inspiration_id,),
    ).fetchone()
    if not tombstone:
        return None
    tombstone_row = dict(tombstone)
    if str(tombstone_row.get("content_hash") or "") != expected_content_hash:
        raise HTTPException(status_code=409, detail="灵感已发生变化，请重新确认永久删除")
    metadata = tombstone_row.get("metadata") if isinstance(tombstone_row.get("metadata"), dict) else {}
    receipt = metadata.get("receipt") if isinstance(metadata.get("receipt"), dict) else None
    if receipt:
        return receipt
    return {
        "schema": TINGLE_DELETION_RECEIPT_SCHEMA,
        "receipt_id": tombstone_row["id"],
        "tombstone_id": tombstone_row["id"],
        "inspiration_id": inspiration_id,
        "kind": tombstone_row.get("kind"),
        "voice_record_id": None,
        "audio_hash": tombstone_row.get("audio_hash"),
        "content_hash": tombstone_row.get("content_hash"),
        "linked_external_target_count": 0,
        "external_targets_preserved": True,
        "deleted_at": tombstone_row.get("deleted_at"),
    }


def _remove_native_capture_projection(record: dict[str, Any], original_path: Path) -> bool:
    if str(record.get("source") or "") != "mac":
        return False
    origin_ref = str(record.get("origin_ref") or "")
    if not origin_ref.startswith("mac-native:"):
        return False
    capture_id = origin_ref.split(":", 1)[1]
    native_root = (
        Path.home() / "Library" / "Application Support" / "Click" / "VoiceInbox" / "NativeRecords"
    ).resolve(strict=False)
    capture_root = original_path.parent.resolve(strict=False)
    if capture_root != native_root / capture_id:
        return False
    manifest_path = capture_root / "capture.json"
    if not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if str(manifest.get("capture_id") or "") != capture_id:
        return False
    if str(manifest.get("audio_hash") or "") != str(record.get("audio_hash") or ""):
        return False
    manifest_path.unlink()
    try:
        capture_root.rmdir()
    except OSError:
        pass
    return True


def permanent_delete_tingle_inspiration(
    inspiration_id: str,
    *,
    confirmation_intent: str,
    expected_content_hash: str,
    acknowledge_linked_external_targets: bool,
    deletion_reason: str = "user_immediate",
) -> dict[str, Any]:
    if confirmation_intent != "delete_permanently":
        raise HTTPException(status_code=422, detail="永久删除需要明确确认")
    expected_hash = str(expected_content_hash or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise HTTPException(status_code=422, detail="永久删除确认已失效，请重新打开这条灵感")

    with db.connect() as conn:
        row = _row_for_inspiration(conn, inspiration_id)
        if not row:
            receipt = _existing_deletion_receipt(conn, inspiration_id, expected_hash)
            if receipt:
                return receipt
            raise HTTPException(status_code=404, detail="Tingle inspiration not found")

    if _deletion_confirmation_hash(row) != expected_hash:
        raise HTTPException(status_code=409, detail="灵感已发生变化，请重新确认永久删除")
    linked_count = int(row.get("linked_external_target_count") or 0)
    if linked_count and not acknowledge_linked_external_targets:
        raise HTTPException(status_code=409, detail="这条灵感已有外部沉淀，请确认外部内容将被保留")

    record = row.get("voice_record") if isinstance(row.get("voice_record"), dict) else None
    original_path: Optional[Path] = None
    staged_audio_paths: list[tuple[Path, Path]] = []
    derived_audio_hashes: list[str] = []
    tombstone_id = new_id("tomb")
    if record:
        original_path = _trusted_audio_path(record)
        if original_path is None:
            raise HTTPException(status_code=409, detail="原始音频不在 Tingle 可信目录，已阻止永久删除")
        if not original_path.is_file():
            raise HTTPException(status_code=409, detail="找不到原始音频，已阻止永久删除")
        if file_hash(original_path) != str(record.get("audio_hash") or ""):
            raise HTTPException(status_code=409, detail="原始音频校验失败，已阻止永久删除")
        audio_paths = [original_path, *_owned_audio_derivatives(original_path)]
        derived_audio_hashes = [file_hash(path) for path in audio_paths[1:]]
        try:
            for audio_path in audio_paths:
                staged_path = audio_path.with_name(
                    f".{audio_path.name}.tingle-delete-{tombstone_id}"
                )
                if staged_path.exists():
                    raise HTTPException(
                        status_code=409,
                        detail="发现未完成的音频清理，请先恢复后重试",
                    )
                audio_path.replace(staged_path)
                staged_audio_paths.append((audio_path, staged_path))
        except Exception:
            for audio_path, staged_path in reversed(staged_audio_paths):
                if staged_path.exists() and not audio_path.exists():
                    staged_path.replace(audio_path)
            raise

    deleted_at = datetime.now(timezone.utc).isoformat()
    receipt = {
        "schema": TINGLE_DELETION_RECEIPT_SCHEMA,
        "receipt_id": tombstone_id,
        "tombstone_id": tombstone_id,
        "inspiration_id": inspiration_id,
        "kind": row["kind"],
        "voice_record_id": record.get("id") if record else None,
        "audio_hash": record.get("audio_hash") if record else None,
        "derived_audio_hashes": derived_audio_hashes,
        "content_hash": expected_hash,
        "linked_external_target_count": linked_count,
        "external_targets_preserved": True,
        "deleted_at": deleted_at,
    }

    try:
        with db.connect() as conn:
            locked = _locked_row_for_inspiration(conn, inspiration_id)
            if not locked or _deletion_confirmation_hash(locked) != expected_hash:
                raise HTTPException(status_code=409, detail="灵感已发生变化，请重新确认永久删除")
            current_links = int(locked.get("linked_external_target_count") or 0)
            if current_links and not acknowledge_linked_external_targets:
                raise HTTPException(status_code=409, detail="这条灵感已有外部沉淀，请确认外部内容将被保留")
            locked_record = locked.get("voice_record") if isinstance(locked.get("voice_record"), dict) else None
            if bool(locked_record) != bool(record):
                raise HTTPException(status_code=409, detail="灵感已发生变化，请重新确认永久删除")
            if record and locked_record:
                if (
                    str(locked_record.get("audio_uri") or "") != str(record.get("audio_uri") or "")
                    or str(locked_record.get("audio_hash") or "") != str(record.get("audio_hash") or "")
                ):
                    raise HTTPException(status_code=409, detail="原始音频已发生变化，请重新确认永久删除")
            metadata = locked.get("metadata") if isinstance(locked.get("metadata"), dict) else {}
            inserted = conn.execute(
                """
                INSERT INTO reader.tingle_tombstones (
                    id, inspiration_id, kind, voice_source, origin_ref,
                    external_ref, device_id, client_capture_id, audio_hash,
                    content_hash, deleted_at, metadata
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    tombstone_id,
                    inspiration_id,
                    locked["kind"],
                    locked_record.get("source") if locked_record else None,
                    locked_record.get("origin_ref") if locked_record else None,
                    locked.get("external_ref"),
                    locked_record.get("device_id") if locked_record else None,
                    ((locked_record.get("metadata") or {}).get("client_capture_id") if locked_record and isinstance(locked_record.get("metadata"), dict) else None),
                    locked_record.get("audio_hash") if locked_record else None,
                    expected_hash,
                    deleted_at,
                    db.jsonb({
                        "schema": TINGLE_TOMBSTONE_SCHEMA,
                        "deletion_reason": deletion_reason,
                        "prior_metadata_schema": metadata.get("schema"),
                        "receipt": receipt,
                    }),
                ),
            ).fetchone()
            if not inserted:
                raise RuntimeError("Tingle deletion tombstone was not persisted")
            if locked_record:
                deleted = conn.execute(
                    "DELETE FROM reader.voice_records WHERE id = %s RETURNING id",
                    (locked_record["id"],),
                ).fetchone()
            else:
                deleted = conn.execute(
                    "DELETE FROM reader.tingle_inspirations WHERE id = %s RETURNING id",
                    (inspiration_id,),
                ).fetchone()
            if not deleted:
                raise RuntimeError("Tingle inspiration was not deleted")
    except Exception:
        for audio_path, staged_path in reversed(staged_audio_paths):
            if staged_path.exists() and not audio_path.exists():
                staged_path.replace(audio_path)
        raise

    for _, staged_path in staged_audio_paths:
        if staged_path.exists():
            staged_path.unlink()
    if record and original_path:
        _remove_native_capture_projection(record, original_path)
    return receipt


def _purge_one(inspiration_id: str, *, dry_run: bool = False) -> dict[str, Any]:
    with db.connect() as conn:
        row = _row_for_inspiration(conn, inspiration_id)
    if not row or row.get("purge_after") is None:
        return {"id": inspiration_id, "status": "skipped"}
    record = row.get("voice_record") if isinstance(row.get("voice_record"), dict) else None
    if record:
        original_path = _trusted_audio_path(record)
        if original_path is None:
            return {"id": inspiration_id, "status": "blocked", "reason": "untrusted_audio_path"}
        if not original_path.is_file():
            return {"id": inspiration_id, "status": "blocked", "reason": "audio_missing"}
        if file_hash(original_path) != str(record.get("audio_hash") or ""):
            return {"id": inspiration_id, "status": "blocked", "reason": "audio_hash_mismatch"}
    if dry_run:
        return {"id": inspiration_id, "status": "eligible"}
    receipt = permanent_delete_tingle_inspiration(
        inspiration_id,
        confirmation_intent="delete_permanently",
        expected_content_hash=_deletion_confirmation_hash(row),
        acknowledge_linked_external_targets=True,
        deletion_reason="retention_expired",
    )
    return {"id": inspiration_id, "status": "purged", "receipt": receipt}


def purge_due_inspirations(*, limit: int = 50, dry_run: bool = False, now: Optional[datetime] = None) -> dict[str, Any]:
    cutoff = now or datetime.now(timezone.utc)
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id
            FROM reader.tingle_inspirations
            WHERE purge_after IS NOT NULL AND purge_after <= %s
            ORDER BY purge_after ASC
            LIMIT %s
            """,
            (cutoff, max(1, min(int(limit), 200))),
        ).fetchall()
    results = [_purge_one(str(row["id"]), dry_run=dry_run) for row in rows]
    return {
        "checked": len(results),
        "purged": sum(item["status"] == "purged" for item in results),
        "eligible": sum(item["status"] == "eligible" for item in results),
        "blocked": [item for item in results if item["status"] == "blocked"],
        "results": results,
    }
