from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from reader_api import db


ACTION_TARGET_RECEIPT_SCHEMA = "click.voice.action_target.receipt.v1"


class VoiceActionTargetError(RuntimeError):
    pass


class UnsupportedVoiceActionTarget(VoiceActionTargetError):
    pass


@dataclass(frozen=True)
class TargetWriteResult:
    adapter: str
    identity: dict[str, Any]
    receipt: dict[str, Any]


class VoiceActionTargetAdapter(Protocol):
    name: str

    def validate_request(self, action: dict[str, Any], record: dict[str, Any]) -> None: ...

    def apply(self, action: dict[str, Any], record: dict[str, Any], transcript: str) -> TargetWriteResult: ...

    def read_back(self, action: dict[str, Any], record: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any]: ...


def _stable_id(prefix: str, *parts: Any) -> str:
    raw = "\0".join(str(part or "") for part in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def _content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _target(action: dict[str, Any]) -> dict[str, Any]:
    value = action.get("target")
    return value if isinstance(value, dict) else {}


def _safe_slug(value: str, fallback: str = "voice-note") -> str:
    slug = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", str(value or ""), flags=re.UNICODE).strip("-._")
    return (slug[:64] or fallback).strip(".")


def _knowledge_base_root() -> Path:
    configured = os.getenv("CLICK_KNOWLEDGE_BASE_ROOT", "").strip()
    return Path(configured).expanduser().resolve() if configured else (Path.home() / "Documents" / "KnowledgeBase").resolve()


def _write_exclusive(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing = path.read_text(encoding="utf-8")
        if existing != content:
            raise VoiceActionTargetError("目标笔记已存在，但内容与本次动作不一致；未覆盖原文件")
        return
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


class InternalRecordTargetAdapter:
    name = "click_voice_record.v1"
    supported = {"archive", "ask_followup", "no_action"}

    def validate_request(self, action: dict[str, Any], record: dict[str, Any]) -> None:
        if action.get("action_type") not in self.supported:
            raise UnsupportedVoiceActionTarget("这类动作不能写入录音自身状态")

    def apply(self, action: dict[str, Any], record: dict[str, Any], transcript: str) -> TargetWriteResult:
        action_type = str(action["action_type"])
        expected_status = {"archive": "archived", "ask_followup": "needs_followup", "no_action": "settled"}[action_type]
        with db.connect() as conn:
            row = conn.execute(
                """
                UPDATE reader.voice_records
                SET status = %s,
                    archived_at = CASE WHEN %s = 'archived' THEN COALESCE(archived_at, now()) ELSE archived_at END,
                    failure_code = NULL, failure_message = NULL, updated_at = now()
                WHERE id = %s AND deleted_at IS NULL
                RETURNING id, status, updated_at
                """,
                (expected_status, expected_status, record["id"]),
            ).fetchone()
        if not row:
            raise VoiceActionTargetError("录音记录不存在或已经被软删除")
        identity = {"target_type": "voice_record", "target_id": record["id"], "expected_status": expected_status}
        return TargetWriteResult(self.name, identity, {"schema": ACTION_TARGET_RECEIPT_SCHEMA, "record": dict(row)})

    def read_back(self, action: dict[str, Any], record: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any]:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT id, status, updated_at FROM reader.voice_records WHERE id = %s AND deleted_at IS NULL",
                (identity.get("target_id"),),
            ).fetchone()
        if not row or row["status"] != identity.get("expected_status"):
            raise VoiceActionTargetError("录音状态读回与预期不一致")
        return {"schema": ACTION_TARGET_RECEIPT_SCHEMA, "validated": True, "record": dict(row)}


class ReaderAnnotationTargetAdapter:
    name = "click_reader_annotation.v1"

    def _source(self, action: dict[str, Any], record: dict[str, Any], transcript: str) -> dict[str, Any]:
        target = _target(action)
        linked_annotation = None
        annotation_id = str(target.get("annotation_id") or "").strip()
        book_id = str(target.get("book_id") or "").strip()
        with db.connect() as conn:
            if annotation_id:
                row = conn.execute("SELECT * FROM reader.annotations WHERE id = %s", (annotation_id,)).fetchone()
                if not row:
                    raise VoiceActionTargetError("目标 Click 批注不存在，请重新选择阅读证据")
                linked_annotation = dict(row)
                book_id = str(linked_annotation["book_id"])
            book = conn.execute("SELECT id, title FROM reader.books WHERE id = %s", (book_id,)).fetchone() if book_id else None
        if not book:
            raise VoiceActionTargetError("动作没有指向真实的 Click 书籍")
        source_text = str(
            target.get("source_text")
            or (linked_annotation or {}).get("source_text")
            or transcript
            or record.get("summary")
            or record.get("title")
            or "Click Voice 灵感"
        ).strip()[:20_000]
        chapter_locator = str(
            target.get("chapter_locator")
            or (linked_annotation or {}).get("chapter_locator")
            or "click-voice"
        ).strip()[:500]
        return {
            "book_id": book_id,
            "book_title": book["title"],
            "linked_annotation_id": annotation_id or None,
            "source_text": source_text,
            "chapter_title": target.get("chapter_title") or (linked_annotation or {}).get("chapter_title"),
            "chapter_locator": chapter_locator,
            "range_locator": target.get("range_locator") if isinstance(target.get("range_locator"), dict) else {},
        }

    def validate_request(self, action: dict[str, Any], record: dict[str, Any]) -> None:
        if action.get("action_type") != "append_reading_note":
            raise UnsupportedVoiceActionTarget("这类动作不能写入 Click 阅读批注")
        self._source(action, record, str(record.get("transcript") or ""))
        if not str(action.get("body") or "").strip():
            raise VoiceActionTargetError("阅读笔记正文为空，不能写入")

    def apply(self, action: dict[str, Any], record: dict[str, Any], transcript: str) -> TargetWriteResult:
        source = self._source(action, record, transcript)
        annotation_id = _stable_id("ann", "click_voice", action["id"])
        note_text = str(action.get("body") or action.get("title") or "").strip()
        metadata = {
            "created_by": "click_voice",
            "voice_record_id": record["id"],
            "voice_action_id": action["id"],
            "linked_annotation_id": source["linked_annotation_id"],
        }
        with db.connect() as conn:
            existing = conn.execute("SELECT * FROM reader.annotations WHERE id = %s", (annotation_id,)).fetchone()
            if existing:
                existing_dict = dict(existing)
                if existing_dict.get("note_text") != note_text or (existing_dict.get("metadata") or {}).get("voice_action_id") != action["id"]:
                    raise VoiceActionTargetError("目标阅读笔记已存在但内容不匹配；未覆盖原笔记")
                row = existing
            else:
                row = conn.execute(
                    """
                    INSERT INTO reader.annotations (
                        id, book_id, sentence_id, kind, source_text, note_text, color,
                        chapter_title, chapter_locator, range_locator, metadata, created_at, updated_at
                    ) VALUES (%s, %s, NULL, 'note', %s, %s, NULL, %s, %s, %s, %s, now(), now())
                    RETURNING *
                    """,
                    (
                        annotation_id,
                        source["book_id"],
                        source["source_text"],
                        note_text,
                        source["chapter_title"],
                        source["chapter_locator"],
                        db.jsonb(source["range_locator"]),
                        db.jsonb(metadata),
                    ),
                ).fetchone()
            conn.execute(
                """
                INSERT INTO reader.voice_record_links (id, voice_record_id, action_id, target_type, target_id, metadata)
                VALUES (%s, %s, %s, 'book', %s, %s)
                ON CONFLICT (voice_record_id, target_type, target_id) DO NOTHING
                """,
                (
                    _stable_id("vlink", record["id"], "book", source["book_id"]),
                    record["id"],
                    action["id"],
                    source["book_id"],
                    db.jsonb({"book_title": source["book_title"]}),
                ),
            )
            conn.execute(
                """
                INSERT INTO reader.voice_record_links (id, voice_record_id, action_id, target_type, target_id, metadata)
                VALUES (%s, %s, %s, 'annotation', %s, %s)
                ON CONFLICT (voice_record_id, target_type, target_id) DO NOTHING
                """,
                (
                    _stable_id("vlink", record["id"], "annotation", annotation_id),
                    record["id"],
                    action["id"],
                    annotation_id,
                    db.jsonb({"book_id": source["book_id"], "linked_annotation_id": source["linked_annotation_id"]}),
                ),
            )
        identity = {
            "target_type": "annotation",
            "target_id": annotation_id,
            "book_id": source["book_id"],
            "expected_note_hash": _content_hash(note_text),
        }
        return TargetWriteResult(
            self.name,
            identity,
            {"schema": ACTION_TARGET_RECEIPT_SCHEMA, "annotation_id": annotation_id, "book_id": source["book_id"]},
        )

    def read_back(self, action: dict[str, Any], record: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any]:
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM reader.annotations WHERE id = %s", (identity.get("target_id"),)).fetchone()
            link = conn.execute(
                "SELECT * FROM reader.voice_record_links WHERE voice_record_id = %s AND target_type = 'annotation' AND target_id = %s",
                (record["id"], identity.get("target_id")),
            ).fetchone()
        if not row:
            raise VoiceActionTargetError("写入后的 Click 阅读笔记无法读回")
        item = dict(row)
        note_hash = _content_hash(str(item.get("note_text") or ""))
        if note_hash != identity.get("expected_note_hash") or (item.get("metadata") or {}).get("voice_action_id") != action["id"]:
            raise VoiceActionTargetError("读回的 Click 阅读笔记与本次动作不一致")
        if not link:
            raise VoiceActionTargetError("录音与 Click 阅读笔记的关联未能读回")
        return {
            "schema": ACTION_TARGET_RECEIPT_SCHEMA,
            "validated": True,
            "annotation_id": item["id"],
            "book_id": item["book_id"],
            "note_hash": note_hash,
        }


class KnowledgeBaseNoteTargetAdapter:
    name = "click_knowledge_base_note.v1"
    supported = {"create_note", "create_review_item", "add_to_knowledge_base"}

    def validate_request(self, action: dict[str, Any], record: dict[str, Any]) -> None:
        if action.get("action_type") not in self.supported:
            raise UnsupportedVoiceActionTarget("这类动作不能写入 KnowledgeBase")
        root = _knowledge_base_root()
        if not root.exists() or not root.is_dir():
            raise VoiceActionTargetError(f"KnowledgeBase 目录不可用：{root}")
        if not str(action.get("body") or action.get("title") or "").strip():
            raise VoiceActionTargetError("KnowledgeBase 笔记内容为空，不能写入")

    def _render(self, action: dict[str, Any], record: dict[str, Any], transcript: str) -> str:
        title = str(action.get("title") or record.get("title") or "Click Voice 灵感").strip()
        body = str(action.get("body") or "").strip()
        source_title = str(record.get("title") or "未命名语音").strip()
        lines = [
            "---",
            "schema: click.voice.knowledge_base_note.v1",
            f"voice_record_id: {record['id']}",
            f"voice_action_id: {action['id']}",
            f"source: {record.get('source') or 'unknown'}",
            "---",
            "",
            f"# {title}",
            "",
            body,
            "",
            "## 来源录音",
            "",
            source_title,
        ]
        if transcript.strip():
            lines.extend(["", "## 忠实转写", "", transcript.strip()])
        return "\n".join(lines).rstrip() + "\n"

    def _path(self, action: dict[str, Any]) -> Path:
        root = _knowledge_base_root()
        category = "复盘" if action.get("action_type") == "create_review_item" else "灵感"
        filename = f"{_safe_slug(str(action.get('title') or 'voice-note'))}-{action['id']}.md"
        path = (root / "00_待分类" / "Click Voice" / category / filename).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise VoiceActionTargetError("KnowledgeBase 目标路径越界，已拒绝写入") from exc
        return path

    def apply(self, action: dict[str, Any], record: dict[str, Any], transcript: str) -> TargetWriteResult:
        content = self._render(action, record, transcript)
        path = self._path(action)
        _write_exclusive(path, content)
        content_hash = _content_hash(content)
        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO reader.voice_record_links (id, voice_record_id, action_id, target_type, target_id, metadata)
                VALUES (%s, %s, %s, 'knowledge_base_note', %s, %s)
                ON CONFLICT (voice_record_id, target_type, target_id) DO NOTHING
                """,
                (
                    _stable_id("vlink", record["id"], "knowledge_base_note", str(path)),
                    record["id"],
                    action["id"],
                    str(path),
                    db.jsonb({"content_hash": content_hash, "adapter": self.name}),
                ),
            )
        identity = {"target_type": "knowledge_base_note", "target_id": str(path), "expected_content_hash": content_hash}
        return TargetWriteResult(
            self.name,
            identity,
            {"schema": ACTION_TARGET_RECEIPT_SCHEMA, "path": str(path), "content_hash": content_hash},
        )

    def read_back(self, action: dict[str, Any], record: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any]:
        root = _knowledge_base_root()
        path = Path(str(identity.get("target_id") or "")).expanduser().resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise VoiceActionTargetError("KnowledgeBase 读回路径越界") from exc
        if not path.is_file():
            raise VoiceActionTargetError("写入后的 KnowledgeBase 笔记无法读回")
        content = path.read_text(encoding="utf-8")
        content_hash = _content_hash(content)
        if content_hash != identity.get("expected_content_hash"):
            raise VoiceActionTargetError("KnowledgeBase 笔记读回内容与本次动作不一致；未覆盖文件")
        if f"voice_action_id: {action['id']}" not in content or f"voice_record_id: {record['id']}" not in content:
            raise VoiceActionTargetError("KnowledgeBase 笔记缺少可追溯来源标识")
        with db.connect() as conn:
            link = conn.execute(
                "SELECT * FROM reader.voice_record_links WHERE voice_record_id = %s AND target_type = 'knowledge_base_note' AND target_id = %s",
                (record["id"], str(path)),
            ).fetchone()
        if not link:
            raise VoiceActionTargetError("录音与 KnowledgeBase 笔记的关联未能读回")
        return {
            "schema": ACTION_TARGET_RECEIPT_SCHEMA,
            "validated": True,
            "path": str(path),
            "content_hash": content_hash,
        }


_INTERNAL_ADAPTER = InternalRecordTargetAdapter()
_READER_ADAPTER = ReaderAnnotationTargetAdapter()
_KNOWLEDGE_BASE_ADAPTER = KnowledgeBaseNoteTargetAdapter()


def target_adapter_for(action: dict[str, Any], record: dict[str, Any]) -> VoiceActionTargetAdapter:
    action_type = str(action.get("action_type") or "")
    if action_type in _INTERNAL_ADAPTER.supported:
        adapter: VoiceActionTargetAdapter = _INTERNAL_ADAPTER
    elif action_type == "append_reading_note":
        adapter = _READER_ADAPTER
    elif action_type in _KNOWLEDGE_BASE_ADAPTER.supported:
        adapter = _KNOWLEDGE_BASE_ADAPTER
    elif action_type == "create_task":
        raise UnsupportedVoiceActionTarget("Click Voice 还没有真实任务目标适配器；这条任务建议会保留待处理，不会冒充已创建")
    else:
        raise UnsupportedVoiceActionTarget(f"当前没有可验证的动作出口：{action_type or 'unknown'}")
    adapter.validate_request(action, record)
    return adapter


def adapter_by_name(name: str) -> VoiceActionTargetAdapter:
    adapters: dict[str, VoiceActionTargetAdapter] = {
        _INTERNAL_ADAPTER.name: _INTERNAL_ADAPTER,
        _READER_ADAPTER.name: _READER_ADAPTER,
        _KNOWLEDGE_BASE_ADAPTER.name: _KNOWLEDGE_BASE_ADAPTER,
    }
    adapter = adapters.get(str(name or ""))
    if not adapter:
        raise VoiceActionTargetError("动作记录中的 target adapter 不存在，无法安全读回")
    return adapter


def receipt_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
