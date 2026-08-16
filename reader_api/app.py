from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import mimetypes
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from html import escape as html_escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional
from urllib.error import URLError
from urllib.parse import parse_qs, quote, unquote
from urllib.request import Request as URLRequest, urlopen
from uuid import uuid4
from xml.etree import ElementTree as ET

from fastapi import Body, FastAPI, HTTPException, Request as FastAPIRequest
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from pypdf import PdfReader

from reader_api import db
from reader_api.android_sync_protocol import (
    ANDROID_SYNC_SCHEMA,
    current_sequence as android_current_sequence,
    event_page as android_event_page,
    operation_receipt as android_operation_receipt,
    operation_request_hash as android_operation_request_hash,
    resource_version as android_resource_version,
    store_operation_receipt as store_android_operation_receipt,
)
from reader_api.epub_compatibility import (
    DISPLAY_VARIANT_SCHEMA,
    REPORT_SCHEMA as EPUB_COMPATIBILITY_REPORT_SCHEMA,
    decode_epub_bytes,
    ensure_epub_assets,
)
from reader_api.mobile_workspace import (
    EDGE_TTS_VOICE,
    MAC_VOICE_PIPELINE_ID,
    MAC_VOICE_PIPELINE_SCHEMA,
    call_hermes_runtime,
    edge_tts_path,
    edge_tts_subprocess_environment,
    extract_json_object,
    mac_voice_pipeline_transcribe,
    reconcile_recordings_to_voice_inbox,
    require_android_access,
    require_mobile_admin_access,
    router as mobile_workspace_router,
    should_use_lite_ui,
    apply_lite_ui_cookie,
)
from reader_api.pdf_support import PDF_PREFLIGHT_SCHEMA, inspect_pdf
from reader_api.runtime_contract import RUNTIME_HEALTH_SCHEMA, runtime_payload
from reader_api.tingle import purge_due_inspirations, reconcile_tingle_inspirations
from reader_api.tingle_api import router as tingle_router
from reader_api.voice_inbox import router as voice_inbox_router
from reader_api.voice_processing import (
    start_voice_processing_worker,
    stop_voice_processing_worker,
    voice_processing_worker_metrics,
)
from reader_api.voice_reader_notes import (
    project_reader_audio_note_to_voice_inbox,
    reconcile_reader_audio_notes_to_voice_inbox,
)


app = FastAPI(title="Sentence Reader API", version="2.0.0")
app.include_router(mobile_workspace_router)
app.include_router(voice_inbox_router)
app.include_router(tingle_router)

_tingle_maintenance_stop = threading.Event()


@app.on_event("startup")
def start_click_voice_processing() -> None:
    start_voice_processing_worker()
    _tingle_maintenance_stop.clear()

    def reconcile_mobile_recordings() -> None:
        try:
            reconcile_recordings_to_voice_inbox()
        except Exception:
            # Each recording keeps its local pending-sync state for a later retry.
            pass

    # Opening ~/Documents can wait on a first-launch macOS privacy decision.
    # That must never hold the shared Runtime in FastAPI's startup phase.
    threading.Thread(
        target=reconcile_mobile_recordings,
        daemon=True,
        name="click-mobile-recording-reconcile",
    ).start()

    def reconcile_reader_audio_notes() -> None:
        try:
            reconcile_reader_audio_notes_to_voice_inbox()
        except Exception:
            # Reader audio notes remain authoritative and are retried on the next Runtime start.
            pass

    threading.Thread(
        target=reconcile_reader_audio_notes,
        daemon=True,
        name="click-reader-audio-note-reconcile",
    ).start()

    def maintain_tingle() -> None:
        while not _tingle_maintenance_stop.is_set():
            try:
                reconcile_tingle_inspirations()
                purge_due_inspirations()
            except Exception:
                # A maintenance failure must not block recording or Runtime startup.
                pass
            _tingle_maintenance_stop.wait(3600)

    threading.Thread(
        target=maintain_tingle,
        daemon=True,
        name="tingle-inspiration-maintenance",
    ).start()


@app.on_event("shutdown")
def stop_click_voice_processing() -> None:
    _tingle_maintenance_stop.set()
    stop_voice_processing_worker()


DEFAULT_HERMES_COGNITIVE_OS_DIR = Path(
    os.getenv(
        "SENTENCE_READER_COGNITIVE_OS_DIR",
        str(Path.home() / "Library" / "Application Support" / "SentenceReader" / "CognitiveOS"),
    )
)

LOOKUP_TTS_DIR = Path(
    os.getenv(
        "SENTENCE_READER_LOOKUP_TTS_DIR",
        str(Path.home() / "Library" / "Application Support" / "Click" / "ReaderTTS"),
    )
)
ANDROID_UPDATE_DIR = Path(
    os.getenv(
        "CLICK_ANDROID_UPDATE_DIR",
        str(Path.home() / "Library" / "Application Support" / "Click" / "AndroidUpdates"),
    )
)
ANDROID_UPDATE_SCHEMA = "click.android.app_update.v1"
ANDROID_UPDATE_MANIFEST_NAME = "latest.json"
ANDROID_UPDATE_ARTIFACT_PATTERN = re.compile(r"^click-android-[0-9]+-[0-9a-f]{12}$")
ANDROID_UPDATE_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ANDROID_UPDATE_CERTIFICATE_PATTERN = re.compile(r"^[0-9a-f]{64}$")
EDGE_TTS_GENERATION_LOCK = threading.Lock()
EDGE_TTS_NICE_COMMAND = Path("/usr/bin/nice")
EDGE_TTS_NICE_LEVEL = 10
ENABLE_HERMES_ONLINE_LOOKUP = os.getenv(
    "SENTENCE_READER_ENABLE_HERMES_ONLINE_LOOKUP",
    "0",
).strip().lower() in {"1", "true", "yes", "on"}
HERMES_MODEL_GATEWAY_BASE_URL = os.getenv("CLICK_HERMES_MODEL_GATEWAY_BASE_URL", "http://127.0.0.1:8093").rstrip("/")
LIVING_BOOK_QWEN_MODEL = os.getenv("CLICK_LIVING_BOOK_QWEN_MODEL", "local-auto").strip() or "local-auto"
VOICE_NOTE_PENDING_TEXT = "语音转写中..."
VOICE_NOTE_FAILED_TEXT = "语音已保存，转写失败，可稍后重试。"
BOOK_IMPORT_MAX_BYTES = 120 * 1024 * 1024
ANDROID_BOOK_UPLOAD_CHUNK_BYTES = 1024 * 1024
ANDROID_EPUB_MAX_ENTRIES = 10_000
ANDROID_EPUB_MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
ANDROID_EPUB_MAX_MEMBER_BYTES = 64 * 1024 * 1024


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def stable_id(prefix: str, *parts: Any) -> str:
    raw = "\0".join(str(part or "") for part in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


NOTE_SPOKEN_PUNCTUATION = [
    ("新的一行", "\n"),
    ("另起一行", "\n"),
    ("换行", "\n"),
    ("句号", "。"),
    ("逗号", "，"),
    ("顿号", "、"),
    ("问号", "？"),
    ("感叹号", "！"),
    ("叹号", "！"),
    ("冒号", "："),
    ("分号", "；"),
    ("省略号", "……"),
]
NOTE_CLOSING_PUNCTUATION = set("。！？!?.…；;：:，,、）)]】》」』”’\"'")


def normalize_note_text(raw_text: str) -> str:
    text = str(raw_text or "").strip()
    if not text:
        return ""
    for spoken, mark in NOTE_SPOKEN_PUNCTUATION:
        text = text.replace(spoken, mark)
    text = re.sub(r"\s+([，。！？；：、,.!?;:])", r"\1", text)
    text = re.sub(r"([，。！？；：、,.!?;:])\s+", r"\1", text)
    text = re.sub(r"([。！？!?]){2,}", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text or text[-1] in NOTE_CLOSING_PUNCTUATION:
        return text
    return text + ("." if re.search(r"[A-Za-z]", text) and not re.search(r"[\u4e00-\u9fff]", text) else "。")


class BookCreate(BaseModel):
    title: str
    author: Optional[str] = None
    source_kind: str = "epub"
    book_hash: str
    file_path: Optional[str] = None
    file_hash: Optional[str] = None
    byte_size: Optional[int] = None


class PositionUpsert(BaseModel):
    chapter_locator: str
    chapter_id: Optional[str] = None
    page_index: int = 0
    total_pages: int = 1
    page_ratio: float = 0
    locator: dict[str, Any] = Field(default_factory=dict)


class SentenceUpsert(BaseModel):
    book_id: str
    chapter_id: Optional[str] = None
    chapter_locator: str
    sentence_index: int
    sentence_text_hash: str
    text: str
    range_locator: dict[str, Any] = Field(default_factory=dict)


class AnnotationCreate(BaseModel):
    book_id: str
    sentence_id: Optional[str] = None
    kind: str
    source_text: str
    note_text: Optional[str] = None
    color: Optional[str] = None
    chapter_title: Optional[str] = None
    chapter_locator: str
    range_locator: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AnnotationPatch(BaseModel):
    note_text: Optional[str] = None
    color: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class AnnotationCleanRequest(BaseModel):
    apply: bool = True


class ExportCreate(BaseModel):
    book_id: str
    export_kind: str
    output_path: str
    annotation_count: int = 0


class ExportGenerate(BaseModel):
    output_dir: Optional[str] = None
    include_json: bool = True


class LivingBookSyncRequest(BaseModel):
    knowledge_base_root: Optional[str] = None


class LivingBookWriteOwnerReconcileRequest(BaseModel):
    target_bundle: str
    expected_sha256: str
    title: Optional[str] = None
    author: Optional[str] = None
    metadata_source: str = "verified_book_evidence"
    metadata_evidence: Optional[str] = None


class LivingBookClassifyRequest(BaseModel):
    knowledge_base_root: Optional[str] = None


class LivingBookClassificationConfirmRequest(BaseModel):
    knowledge_base_root: Optional[str] = None
    primary_category_id: str
    secondary_category_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    processing_policy: str = "normal"
    evidence: Optional[str] = None


class LivingBookTaxonomyApplyDefaultRequest(BaseModel):
    knowledge_base_root: Optional[str] = None
    confirmation_text: str
    evidence: Optional[str] = None


class LivingBookGenerateDraftsRequest(BaseModel):
    knowledge_base_root: Optional[str] = None
    use_hermes_runtime: bool = False


class LivingBookJobsRunRequest(BaseModel):
    knowledge_base_root: Optional[str] = None
    limit: int = 20
    force: bool = False


class LivingBookAnalyzeRequest(BaseModel):
    knowledge_base_root: Optional[str] = None
    requested_by: str = "click_user"
    force: bool = False


class HermesSyncGenerate(BaseModel):
    output_dir: Optional[str] = None
    annotation_ids: list[str] = Field(default_factory=list)
    include_red_highlights: bool = True


class HermesIngestRun(BaseModel):
    cognitive_os_dir: Optional[str] = None
    limit: int = 20
    dry_run: bool = False
    sync_event_ids: list[str] = Field(default_factory=list)


class CognitiveReviewQueueRun(BaseModel):
    cognitive_os_dir: Optional[str] = None
    limit: int = 100


class CognitiveDashboardRun(BaseModel):
    cognitive_os_dir: Optional[str] = None
    limit: int = 100
    history_limit: int = 20


class CognitiveOperatorDryRun(BaseModel):
    cognitive_os_dir: Optional[str] = None
    all_ready: bool = True
    allow_empty: bool = True
    allow_needs_review: bool = False


class CognitiveReviewItemRun(BaseModel):
    cognitive_os_dir: Optional[str] = None
    draft_id: Optional[str] = None
    candidate_intake_id: Optional[str] = None
    draft_path: Optional[str] = None
    prefer_statuses: list[str] = Field(default_factory=lambda: ["ready_to_approve", "needs_review", "blocked"])


class CognitiveOperatorPreflight(BaseModel):
    cognitive_os_dir: Optional[str] = None
    draft_ids: list[str] = Field(default_factory=list)
    candidate_intake_ids: list[str] = Field(default_factory=list)
    draft_paths: list[str] = Field(default_factory=list)
    allow_needs_review: bool = False


class CognitiveOperatorApprove(BaseModel):
    cognitive_os_dir: Optional[str] = None
    candidate_intake_id: str
    confirmation_text: str
    allow_needs_review: bool = False
    overwrite: bool = False
    skip_quality_gate: bool = False
    skip_quality_gate_reason: Optional[str] = None


class AudioNoteCreate(BaseModel):
    book_id: str
    annotation_id: Optional[str] = None
    audio_path: str
    audio_hash: Optional[str] = None
    duration_seconds: Optional[float] = None
    provider: str = "funasr"
    transcript: Optional[str] = None
    raw_result: dict[str, Any] = Field(default_factory=dict)
    status: str = "pending"
    error_message: Optional[str] = None


class AudioNotePatch(BaseModel):
    annotation_id: Optional[str] = None
    audio_hash: Optional[str] = None
    duration_seconds: Optional[float] = None
    provider: Optional[str] = None
    transcript: Optional[str] = None
    raw_result: Optional[dict[str, Any]] = None
    status: Optional[str] = None
    error_message: Optional[str] = None


class LANAudioTranscribe(BaseModel):
    book_id: str
    annotation_id: Optional[str] = None
    audio_base64: str
    mime_type: str = "audio/webm"
    duration_seconds: Optional[float] = None


class LibraryImport(BaseModel):
    filename: str
    content_base64: str
    title: Optional[str] = None
    author: Optional[str] = None


class LibraryMetadataPatch(BaseModel):
    title: Optional[str] = None
    author: Optional[str] = None
    source: str = "user_confirmed"
    evidence: Optional[str] = None


class LibraryBatchHide(BaseModel):
    book_ids: list[str] = Field(default_factory=list)


class LibraryOrganizationPatch(BaseModel):
    favorite: Optional[bool] = None
    custom_category: Optional[str] = None
    tags: Optional[list[str]] = None


class LibraryBatchOrganizationPatch(LibraryOrganizationPatch):
    book_ids: list[str] = Field(default_factory=list)


class VocabBuildRequest(BaseModel):
    limit: int = 500
    min_count: int = 1


class VocabPatch(BaseModel):
    status: Optional[str] = None
    context_meaning_zh: Optional[str] = None
    alignment_status: Optional[str] = None
    alignment_reason: Optional[str] = None
    user_note: Optional[str] = None


class VocabReviewCreate(BaseModel):
    rating: str


class LifeStudyVocabReviewDecision(BaseModel):
    term: str
    decision: str
    corrected_meaning_zh: Optional[str] = None
    note: Optional[str] = None


class LookupEventCreate(BaseModel):
    surface: str
    lemma: Optional[str] = None
    sentence_id: Optional[str] = None
    event_kind: str = "lookup"
    context: dict[str, Any] = Field(default_factory=dict)


class LookupCorrectionCreate(BaseModel):
    word: str
    meaning_zh: str
    sentence_id: Optional[str] = None
    sentence: Optional[str] = None


class LookupTTSCreate(BaseModel):
    text: str
    voice: Optional[str] = None


class AndroidSyncOperation(BaseModel):
    operation_id: str
    device_id: Optional[str] = None
    operation_type: str
    book_id: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    base_server_version: Optional[str] = None


class AndroidSyncOperations(BaseModel):
    device_id: str
    operations: list[AndroidSyncOperation] = Field(default_factory=list)


class AndroidTTSCreate(BaseModel):
    text: str
    voice: Optional[str] = None
    kind: str = "sentence"
    book_id: Optional[str] = None
    locator: dict[str, Any] = Field(default_factory=dict)


def jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    return value


def safe_slug(text: str) -> str:
    slug = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", text, flags=re.UNICODE).strip("-._")
    return slug[:80] or "book"


def default_export_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "SentenceReader" / "Exports"


def default_knowledge_base_root() -> Path:
    configured = os.getenv("CLICK_KNOWLEDGE_BASE_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Documents" / "KnowledgeBase"


def knowledge_base_root_path(knowledge_base_root: Optional[str] = None) -> Path:
    return Path(knowledge_base_root).expanduser() if knowledge_base_root else default_knowledge_base_root()


def living_books_root(knowledge_base_root: Optional[str] = None) -> Path:
    root = knowledge_base_root_path(knowledge_base_root)
    return root / "_system"


def legacy_living_books_root(knowledge_base_root: Optional[str] = None) -> Path:
    return knowledge_base_root_path(knowledge_base_root) / "LivingBooks"


def default_hermes_sync_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "SentenceReader" / "HermesSync"


def default_cognitive_ops_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "SentenceReader" / "CognitiveOps"


def lookup_tts_dir() -> Path:
    LOOKUP_TTS_DIR.mkdir(parents=True, exist_ok=True)
    return LOOKUP_TTS_DIR


def clean_lookup_tts_text(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value[:300]


def android_update_dir() -> Path:
    ANDROID_UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    return ANDROID_UPDATE_DIR


def load_android_update_manifest() -> Optional[dict[str, Any]]:
    root = android_update_dir().resolve()
    manifest_path = root / ANDROID_UPDATE_MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise HTTPException(status_code=503, detail=f"Android update manifest is invalid: {exc.__class__.__name__}")
    if not isinstance(payload, dict) or payload.get("schema") != ANDROID_UPDATE_SCHEMA:
        raise HTTPException(status_code=503, detail="Android update manifest schema is invalid")
    artifact_id = str(payload.get("artifact_id") or "")
    apk_sha256 = str(payload.get("apk_sha256") or "").lower()
    certificate_sha256 = str(payload.get("certificate_sha256") or "").lower()
    try:
        version_code = int(payload.get("version_code"))
        apk_bytes = int(payload.get("apk_bytes"))
        min_sdk = int(payload.get("min_sdk", 28))
    except (TypeError, ValueError):
        raise HTTPException(status_code=503, detail="Android update manifest numeric fields are invalid")
    if (
        ANDROID_UPDATE_ARTIFACT_PATTERN.fullmatch(artifact_id) is None
        or ANDROID_UPDATE_SHA256_PATTERN.fullmatch(apk_sha256) is None
        or ANDROID_UPDATE_CERTIFICATE_PATTERN.fullmatch(certificate_sha256) is None
        or version_code <= 0
        or apk_bytes <= 0
        or min_sdk < 28
    ):
        raise HTTPException(status_code=503, detail="Android update manifest identity is invalid")
    apk_path = root / f"{artifact_id}.apk"
    if (
        not apk_path.is_file()
        or apk_path.is_symlink()
        or apk_path.resolve().parent != root
        or apk_path.stat().st_size != apk_bytes
    ):
        raise HTTPException(status_code=503, detail="Android update APK is missing or has the wrong size")
    return {
        **payload,
        "artifact_id": artifact_id,
        "version_code": version_code,
        "version_name": str(payload.get("version_name") or "").strip(),
        "apk_bytes": apk_bytes,
        "apk_sha256": apk_sha256,
        "certificate_sha256": certificate_sha256,
        "min_sdk": min_sdk,
        "apk_path": apk_path,
    }


def synthesize_lookup_tts_low_priority(
    command: str,
    text: str,
    voice: str,
    audio_path: Path,
) -> tuple[bool, str]:
    """Generate one cache entry without duplicate jobs, argv text, or normal-priority CPU."""
    with EDGE_TTS_GENERATION_LOCK:
        if audio_path.is_file() and audio_path.stat().st_size > 0:
            return True, ""
        audio_path.unlink(missing_ok=True)
        cache_root = lookup_tts_dir()
        input_fd, input_name = tempfile.mkstemp(
            prefix=".edge-tts-input-",
            suffix=".txt",
            dir=cache_root,
        )
        input_path = Path(input_name)
        partial_path = cache_root / f".{audio_path.name}.{uuid4().hex}.partial"
        try:
            with os.fdopen(input_fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            arguments = [
                command,
                "--voice",
                voice,
                "--file",
                str(input_path),
                "--write-media",
                str(partial_path),
            ]
            if EDGE_TTS_NICE_COMMAND.is_file():
                arguments = [
                    str(EDGE_TTS_NICE_COMMAND),
                    "-n",
                    str(EDGE_TTS_NICE_LEVEL),
                    *arguments,
                ]
            proc = subprocess.run(
                arguments,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=45,
                env=edge_tts_subprocess_environment(),
            )
            if proc.returncode != 0 or not partial_path.is_file() or partial_path.stat().st_size <= 0:
                detail = (proc.stderr or proc.stdout or "edge-tts failed").strip()
                return False, detail[:240]
            with partial_path.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(partial_path, audio_path)
            return True, ""
        except subprocess.TimeoutExpired:
            return False, "edge-tts timed out"
        except OSError as error:
            return False, str(error)[:240]
        finally:
            input_path.unlink(missing_ok=True)
            partial_path.unlink(missing_ok=True)


POS_ZH_MAP = {
    "n": "名词",
    "noun": "名词",
    "v": "动词",
    "verb": "动词",
    "adj": "形容词",
    "a": "形容词",
    "adjective": "形容词",
    "adv": "副词",
    "adverb": "副词",
    "proper noun": "专有名词",
    "proper_noun": "专有名词",
    "noun or verb": "名词或动词",
    "noun_or_verb": "名词或动词",
    "adjective or noun": "形容词或名词",
    "adjective_or_noun": "形容词或名词",
    "prep": "介词",
    "preposition": "介词",
    "conj": "连词",
    "conjunction": "连词",
    "pron": "代词",
    "pronoun": "代词",
    "num": "数词",
    "number": "数词",
    "numeral": "数词",
    "interj": "感叹词",
    "interjection": "感叹词",
    "article": "冠词",
    "art": "冠词",
}


def lookup_part_of_speech_zh(part_of_speech: str) -> str:
    key = re.sub(r"[_\s]+", " ", str(part_of_speech or "").replace(".", " ").strip().lower())
    key = re.sub(r"\s+", " ", key).strip()
    if not key:
        return ""
    return POS_ZH_MAP.get(key, part_of_speech)


def lookup_popup_speak_text_zh(term: str, part_of_speech: str, meaning_zh: str) -> str:
    meaning = str(meaning_zh or "").strip()
    if not meaning:
        return ""
    pos_zh = lookup_part_of_speech_zh(part_of_speech)
    pieces = [piece for piece in (pos_zh, meaning) if piece]
    body = "，".join(pieces) if pieces else meaning
    word = clean_vocab_word(term)
    return f"{body}。英文，{word}。" if word else f"{body}。"


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def xml_children(node: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(node) if local_name(child.tag) == name]


def xml_first(node: ET.Element, name: str) -> Optional[ET.Element]:
    for child in node.iter():
        if local_name(child.tag) == name:
            return child
    return None


def xml_text(node: Optional[ET.Element]) -> str:
    if node is None:
        return ""
    return "".join(node.itertext()).strip()


def xml_attr(node: ET.Element, name: str) -> str:
    for key, value in node.attrib.items():
        if local_name(key) == name:
            return value
    return ""


def zip_text(epub: zipfile.ZipFile, name: str) -> str:
    raw = epub.read(name)
    text, _ = decode_epub_bytes(raw)
    return text


def safe_epub_member(path: str) -> str:
    normalized = posixpath.normpath(str(path or "").replace("\\", "/")).lstrip("/")
    if not normalized or normalized == "." or normalized.startswith("../") or "/../" in f"/{normalized}/":
        raise HTTPException(status_code=400, detail="unsafe EPUB asset path")
    return normalized


def resolve_epub_path(base: str, href: str) -> str:
    if not href:
        return ""
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", href) or href.startswith("#"):
        return href
    base_dir = posixpath.dirname(base)
    return safe_epub_member(posixpath.normpath(posixpath.join(base_dir, href.split("#", 1)[0])))


def epub_rootfile_path(epub: zipfile.ZipFile) -> str:
    try:
        container = ET.fromstring(epub.read("META-INF/container.xml"))
    except Exception as exc:  # noqa: BLE001 - invalid EPUB must become a clean API error.
        raise HTTPException(status_code=422, detail="EPUB container.xml is missing or invalid") from exc
    for node in container.iter():
        if local_name(node.tag) == "rootfile" and node.attrib.get("full-path"):
            return safe_epub_member(node.attrib["full-path"])
    raise HTTPException(status_code=422, detail="EPUB rootfile not found")


def chapter_title_from_html(html_text: str, fallback: str) -> str:
    for pattern in (
        r"<h1\b[^>]*>(.*?)</h1>",
        r"<h2\b[^>]*>(.*?)</h2>",
        r"<h3\b[^>]*>(.*?)</h3>",
        r"<title\b[^>]*>(.*?)</title>",
    ):
        match = re.search(pattern, html_text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            text = re.sub(r"<[^>]+>", "", match.group(1))
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                return text[:120]
    return fallback


def clean_toc_title(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:140]


def chapter_index_for_toc_href(href: str, chapters: list[dict[str, Any]]) -> Optional[int]:
    target = safe_epub_member((href or "").split("#", 1)[0])
    for chapter in chapters:
        if chapter.get("href") == target or chapter.get("locator") == target:
            return int(chapter["index"])
    return None


def append_epub3_toc_items(
    node: ET.Element,
    *,
    base_path: str,
    chapters: list[dict[str, Any]],
    level: int,
    into: list[dict[str, Any]],
) -> None:
    for li in [child for child in list(node) if local_name(child.tag) == "li"]:
        title = ""
        href = ""
        nested_lists: list[ET.Element] = []
        for child in list(li):
            child_name = local_name(child.tag)
            if child_name in {"a", "span"} and not title:
                title = clean_toc_title(xml_text(child))
                href = xml_attr(child, "href") if child_name == "a" else ""
            elif child_name in {"ol", "ul"}:
                nested_lists.append(child)
        if title and href:
            try:
                chapter_index = chapter_index_for_toc_href(resolve_epub_path(base_path, href), chapters)
            except HTTPException:
                chapter_index = None
            if chapter_index is not None:
                into.append(
                    {
                        "title": title,
                        "chapter_index": chapter_index,
                        "level": max(0, level),
                    }
                )
        for nested in nested_lists:
            append_epub3_toc_items(nested, base_path=base_path, chapters=chapters, level=level + 1, into=into)


def epub3_toc_entries(epub: zipfile.ZipFile, manifest: dict[str, dict[str, str]], chapters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    nav_items = [
        item
        for item in manifest.values()
        if "nav" in {part.strip().lower() for part in (item.get("properties") or "").split()}
    ]
    for item in nav_items:
        href = item.get("href") or ""
        try:
            root = ET.fromstring(zip_text(epub, href))
        except Exception:
            continue
        navs = [node for node in root.iter() if local_name(node.tag) == "nav"]
        toc_nav = next(
            (
                node
                for node in navs
                if "toc" in (xml_attr(node, "type") or "").split()
                or xml_attr(node, "role").lower() == "doc-toc"
            ),
            navs[0] if navs else None,
        )
        if toc_nav is None:
            continue
        top_list = next((child for child in list(toc_nav) if local_name(child.tag) in {"ol", "ul"}), None)
        if top_list is None:
            continue
        entries: list[dict[str, Any]] = []
        append_epub3_toc_items(top_list, base_path=href, chapters=chapters, level=0, into=entries)
        if entries:
            return entries
    return []


def append_ncx_toc_items(
    nav_point: ET.Element,
    *,
    base_path: str,
    chapters: list[dict[str, Any]],
    level: int,
    into: list[dict[str, Any]],
) -> None:
    label = clean_toc_title(xml_text(xml_first(nav_point, "text")))
    content = xml_first(nav_point, "content")
    href = xml_attr(content, "src") if content is not None else ""
    if label and href:
        try:
            chapter_index = chapter_index_for_toc_href(resolve_epub_path(base_path, href), chapters)
        except HTTPException:
            chapter_index = None
        if chapter_index is not None:
            into.append({"title": label, "chapter_index": chapter_index, "level": max(0, level)})
    for child in xml_children(nav_point, "navPoint"):
        append_ncx_toc_items(child, base_path=base_path, chapters=chapters, level=level + 1, into=into)


def ncx_toc_entries(epub: zipfile.ZipFile, manifest: dict[str, dict[str, str]], chapters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ncx_items = [
        item
        for item in manifest.values()
        if item.get("media_type") == "application/x-dtbncx+xml" or (item.get("href") or "").lower().endswith(".ncx")
    ]
    for item in ncx_items:
        href = item.get("href") or ""
        try:
            root = ET.fromstring(zip_text(epub, href))
        except Exception:
            continue
        nav_map = xml_first(root, "navMap")
        if nav_map is None:
            continue
        entries: list[dict[str, Any]] = []
        for nav_point in xml_children(nav_map, "navPoint"):
            append_ncx_toc_items(nav_point, base_path=href, chapters=chapters, level=0, into=entries)
        if entries:
            return entries
    return []


def toc_entries(epub: zipfile.ZipFile, manifest: dict[str, dict[str, str]], chapters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return epub3_toc_entries(epub, manifest, chapters) or ncx_toc_entries(epub, manifest, chapters)


def epub_publication(epub_path: Path, *, book: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    if not epub_path.exists():
        raise HTTPException(status_code=404, detail=f"EPUB file missing: {epub_path}")
    try:
        with zipfile.ZipFile(epub_path) as epub:
            opf_path = epub_rootfile_path(epub)
            opf_root = ET.fromstring(epub.read(opf_path))
            opf_dir = posixpath.dirname(opf_path)
            manifest: dict[str, dict[str, str]] = {}
            spine: list[str] = []
            for node in opf_root.iter():
                name = local_name(node.tag)
                if name == "item" and node.attrib.get("id") and node.attrib.get("href"):
                    href = safe_epub_member(posixpath.normpath(posixpath.join(opf_dir, node.attrib["href"])))
                    manifest[node.attrib["id"]] = {
                        "href": href,
                        "media_type": node.attrib.get("media-type", ""),
                        "properties": node.attrib.get("properties", ""),
                    }
                elif name == "itemref" and node.attrib.get("idref"):
                    spine.append(node.attrib["idref"])

            title = xml_text(xml_first(opf_root, "title")) or str((book or {}).get("title") or "")
            author = xml_text(xml_first(opf_root, "creator")) or str((book or {}).get("author") or "")
            chapters: list[dict[str, Any]] = []
            for idref in spine:
                item = manifest.get(idref)
                if not item:
                    continue
                href = item["href"]
                media_type = item.get("media_type") or ""
                if not (media_type in {"application/xhtml+xml", "text/html"} or href.lower().endswith((".xhtml", ".html", ".htm"))):
                    continue
                try:
                    chapter_html = zip_text(epub, href)
                except KeyError:
                    continue
                chapters.append(
                    {
                        "index": len(chapters),
                        "idref": idref,
                        "href": href,
                        "locator": href,
                        "title": chapter_title_from_html(chapter_html, f"第 {len(chapters) + 1} 章"),
                    }
                )
            toc = toc_entries(epub, manifest, chapters)
            return {
                "schema": "sentence_reader.epub_publication.v1",
                "title": title,
                "author": author,
                "opf_path": opf_path,
                "chapter_count": len(chapters),
                "chapters": chapters,
                "toc": toc,
            }
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=422, detail="invalid EPUB zip file") from exc


def epub_cover_asset(epub_path: Path) -> Optional[dict[str, str]]:
    if not epub_path.exists():
        return None
    try:
        with zipfile.ZipFile(epub_path) as epub:
            opf_path = epub_rootfile_path(epub)
            opf_root = ET.fromstring(epub.read(opf_path))
            opf_dir = posixpath.dirname(opf_path)
            manifest: dict[str, dict[str, str]] = {}
            cover_id = ""
            for node in opf_root.iter():
                name = local_name(node.tag)
                if name == "item" and node.attrib.get("id") and node.attrib.get("href"):
                    href = safe_epub_member(posixpath.normpath(posixpath.join(opf_dir, node.attrib["href"])))
                    manifest[node.attrib["id"]] = {
                        "href": href,
                        "media_type": node.attrib.get("media-type", ""),
                        "properties": node.attrib.get("properties", ""),
                    }
                elif name == "meta":
                    if node.attrib.get("name") == "cover" and node.attrib.get("content"):
                        cover_id = node.attrib["content"]

            candidates: list[dict[str, str]] = []
            candidates.extend(
                item for item in manifest.values() if "cover-image" in (item.get("properties") or "").split()
            )
            if cover_id and cover_id in manifest:
                candidates.append(manifest[cover_id])
            candidates.extend(
                item
                for item_id, item in manifest.items()
                if "cover" in item_id.lower() and (item.get("media_type") or "").startswith("image/")
            )
            candidates.extend(item for item in manifest.values() if (item.get("media_type") or "").startswith("image/"))
            for item in candidates:
                href = item.get("href") or ""
                if href and href in epub.namelist():
                    return item
    except Exception:
        return None
    return None


def cover_palette(book_id: str) -> tuple[str, str]:
    digest = hashlib.sha256(book_id.encode("utf-8")).hexdigest()
    palettes = [
        ("#243B53", "#C8A96A"),
        ("#2B2D42", "#EF8354"),
        ("#14342B", "#B7E4C7"),
        ("#3A2E39", "#F2D492"),
        ("#1D3557", "#A8DADC"),
        ("#32292F", "#C9ADA7"),
        ("#233D4D", "#FE7F2D"),
        ("#2D3142", "#BFC0C0"),
    ]
    return palettes[int(digest[:2], 16) % len(palettes)]


def xml_escape(value: Any) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def generated_cover_svg(book: dict[str, Any]) -> bytes:
    title = str(book.get("title") or "Untitled")
    author = str(book.get("author") or "Click")
    book_id = str(book.get("id") or book.get("book_hash") or title)
    primary, accent = cover_palette(book_id)
    title_lines = [title[i : i + 12] for i in range(0, min(len(title), 36), 12)] or ["Untitled"]
    title_svg = "".join(
        f'<text x="34" y="{126 + index * 38}" font-size="28" font-weight="700" fill="#F8FAFC">{xml_escape(line)}</text>'
        for index, line in enumerate(title_lines[:3])
    )
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="360" height="520" viewBox="0 0 360 520">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="{primary}"/>
      <stop offset="1" stop-color="#0B1020"/>
    </linearGradient>
    <linearGradient id="veil" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#000000" stop-opacity=".05"/>
      <stop offset=".58" stop-color="#000000" stop-opacity=".08"/>
      <stop offset="1" stop-color="#000000" stop-opacity=".55"/>
    </linearGradient>
  </defs>
  <rect width="360" height="520" rx="22" fill="url(#g)"/>
  <rect width="360" height="520" rx="22" fill="url(#veil)"/>
  <rect x="22" y="24" width="316" height="472" rx="16" fill="none" stroke="{accent}" stroke-width="3" opacity=".72"/>
  <rect x="34" y="56" width="92" height="7" rx="3.5" fill="{accent}"/>
  {title_svg}
  <text x="34" y="426" font-size="18" fill="#CBD5E1">{xml_escape(author[:28])}</text>
  <g font-size="15" font-weight="700">
    <text x="34" y="462" fill="{accent}">逐句读懂</text>
    <text x="132" y="462" fill="#F8FAFC">语境查词</text>
    <text x="230" y="462" fill="#C7F0D8">复习沉淀</text>
  </g>
</svg>"""
    return svg.encode("utf-8")


def pdf_cover_cache_path(book: dict[str, Any]) -> Path:
    stable = safe_slug(str(book.get("book_hash") or book.get("id") or "pdf"))
    return app_support_books_dir() / stable / "derived" / "cover.png"


def ensure_pdf_cover_cache(book: dict[str, Any], source_path: Path) -> Optional[Path]:
    target = pdf_cover_cache_path(book)
    if target.exists() and target.stat().st_size > 0:
        return target
    if not source_path.exists() or source_path.suffix.lower() != ".pdf":
        return None
    qlmanage = Path("/usr/bin/qlmanage")
    if not qlmanage.exists():
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="click-pdf-cover-") as temp_dir:
        try:
            completed = subprocess.run(
                [str(qlmanage), "-t", "-s", "720", "-o", temp_dir, str(source_path)],
                check=False,
                capture_output=True,
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        candidates = sorted(Path(temp_dir).glob("*.png"))
        if not candidates:
            return None
        shutil.copy2(candidates[0], target)
    return target if target.exists() else None


def library_cover_info(book: dict[str, Any], file_status: dict[str, Any]) -> dict[str, Any]:
    has_epub_cover = False
    if file_status.get("exists") and file_status.get("extension") == "epub":
        has_epub_cover = epub_cover_asset(Path(str(file_status.get("file_path") or "")).expanduser()) is not None
    is_pdf = file_status.get("exists") and file_status.get("extension") == "pdf"
    has_pdf_cover = False
    if is_pdf:
        cached = ensure_pdf_cover_cache(book, Path(str(file_status.get("file_path") or "")).expanduser())
        has_pdf_cover = cached is not None
    return {
        "url": f"/api/library/books/{book.get('id')}/cover",
        "kind": "epub" if has_epub_cover else ("pdf_first_page" if has_pdf_cover else "generated"),
        "has_image": has_epub_cover or has_pdf_cover,
    }


def library_reading_state(progress: dict[str, Any], row: dict[str, Any]) -> str:
    if bool(row.get("hidden") or False):
        return "搁置"
    percent = int(progress.get("percent") or 0)
    if percent >= 98:
        return "已读"
    if progress.get("has_position") or row.get("last_opened_at"):
        return "在读"
    return "未开始"


def strip_unsafe_html(html_text: str) -> str:
    html_text = re.sub(r"<script\b[^>]*>.*?</script>", "", html_text, flags=re.IGNORECASE | re.DOTALL)
    html_text = re.sub(r"<iframe\b[^>]*>.*?</iframe>", "", html_text, flags=re.IGNORECASE | re.DOTALL)
    html_text = re.sub(r"\s+on[a-zA-Z0-9_-]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", html_text)
    html_text = re.sub(r"\s+href\s*=\s*(['\"])\s*javascript:[^'\"]*\1", "", html_text, flags=re.IGNORECASE)
    return html_text


def transform_epub_html_assets(book_id: str, chapter_href: str, html_text: str) -> str:
    def replace_attr(match: re.Match[str]) -> str:
        attr = match.group("attr")
        quote_char = match.group("quote")
        value = match.group("value").strip()
        if not value or value.startswith(("#", "data:", "mailto:", "tel:")) or re.match(r"^https?://", value, flags=re.IGNORECASE):
            return match.group(0)
        try:
            resolved = resolve_epub_path(chapter_href, value)
        except HTTPException:
            return match.group(0)
        if not resolved or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", resolved):
            return match.group(0)
        return f'{attr}={quote_char}/lan/books/{book_id}/asset/{quote(resolved)}{quote_char}'

    html_text = strip_unsafe_html(html_text)
    body_match = re.search(r"<body\b[^>]*>(.*?)</body>", html_text, flags=re.IGNORECASE | re.DOTALL)
    if body_match:
        html_text = body_match.group(1)
    return re.sub(
        r"(?P<attr>\b(?:src|href))\s*=\s*(?P<quote>['\"])(?P<value>.*?)(?P=quote)",
        replace_attr,
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )


def book_with_latest_file(book_id: str) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT b.*,
                   bf.file_path,
                   bf.file_kind,
                   bf.file_hash,
                   bf.byte_size,
                   (
                       SELECT jsonb_agg(
                           jsonb_build_object(
                               'file_path', candidate.file_path,
                               'file_kind', candidate.file_kind,
                               'file_hash', candidate.file_hash,
                               'byte_size', candidate.byte_size
                           )
                           ORDER BY candidate.created_at DESC, candidate.id DESC
                       )
                       FROM reader.book_files candidate
                       WHERE candidate.book_id = b.id
                   ) AS file_candidates
            FROM reader.books b
            LEFT JOIN LATERAL (
                SELECT file_path, file_kind, file_hash, byte_size
                FROM reader.book_files
                WHERE book_id = b.id
                ORDER BY created_at DESC, id DESC
                LIMIT 1
            ) bf ON true
            WHERE b.id = %s
            """,
            (book_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="book not found")
    return preferred_existing_book_file(dict(row))


def preferred_existing_book_file(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    raw_candidates = result.pop("file_candidates", None)
    candidates = [
        dict(candidate)
        for candidate in (raw_candidates or [])
        if isinstance(candidate, dict)
        and str(candidate.get("file_path") or "").strip()
    ]
    if not candidates:
        return result
    source_kind = str(result.get("source_kind") or "").strip().lower()
    expected_extension = source_kind if source_kind in {"epub", "pdf"} else ""
    readable: list[tuple[dict[str, Any], bool]] = []
    for candidate in candidates:
        path = Path(str(candidate["file_path"])).expanduser()
        candidate_kind = str(candidate.get("file_kind") or "").strip().lower()
        if not path.is_file():
            continue
        if expected_extension and path.suffix.lower() != f".{expected_extension}":
            continue
        if (
            expected_extension
            and candidate_kind in {"epub", "pdf"}
            and candidate_kind != expected_extension
        ):
            continue
        readable.append(
            (
                candidate,
                bool(library_file_status(str(path))["owned_internal_copy"]),
            )
        )
    mapped_bundle = living_book_mapped_bundle_dir(result)
    mapped_original_dir = (mapped_bundle / "1_原书").resolve() if mapped_bundle else None

    def is_mapped_write_owner(candidate: dict[str, Any]) -> bool:
        if mapped_original_dir is None:
            return False
        try:
            Path(str(candidate.get("file_path") or "")).expanduser().resolve().relative_to(mapped_original_dir)
            return True
        except (FileNotFoundError, OSError, ValueError):
            return False

    selected = next(
        (candidate for candidate, _owned in readable if is_mapped_write_owner(candidate)),
        next(
            (candidate for candidate, owned in readable if owned),
            readable[0][0] if readable else candidates[0],
        ),
    )
    for key in ("file_path", "file_kind", "file_hash", "byte_size"):
        result[key] = selected.get(key)
    return result


def epub_path_for_book(book: dict[str, Any]) -> Path:
    file_path = str(book.get("file_path") or "").strip()
    if not file_path:
        raise HTTPException(status_code=404, detail="book has no EPUB file path")
    return Path(file_path).expanduser()


def epub_compatibility_root() -> Path:
    return Path(
        os.getenv(
            "CLICK_EPUB_COMPATIBILITY_ROOT",
            str(Path.home() / "Library" / "Application Support" / "Click" / "EpubCompatibility"),
        )
    ).expanduser()


def epub_compatibility_dir(book_id: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", str(book_id or "")).strip("-")
    if not safe_id:
        raise HTTPException(status_code=422, detail="book id is required for EPUB compatibility assets")
    return epub_compatibility_root() / safe_id


def ensure_book_epub_assets(book: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    book_id = str(book.get("id") or "").strip()
    epub_path = epub_path_for_book(book)
    try:
        return ensure_epub_assets(
            epub_path,
            book_id=book_id,
            output_dir=epub_compatibility_dir(book_id),
            force=force,
        )
    except Exception as exc:  # noqa: BLE001 - a damaged sidecar must never discard the imported EPUB.
        return {
            "schema": EPUB_COMPATIBILITY_REPORT_SCHEMA,
            "book_id": book_id,
            "source_file": str(epub_path),
            "checked_at": now_iso(),
            "status": "error",
            "reading_profile": "UNKNOWN",
            "recommended_spread": "never",
            "display_variants": {"available": False},
            "original_epub_modified": False,
            "error": f"{exc.__class__.__name__}: {exc}",
        }


def read_book_epub_assets(book_id: str) -> dict[str, Any]:
    path = epub_compatibility_dir(book_id) / "compatibility_report.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if payload.get("schema") == EPUB_COMPATIBILITY_REPORT_SCHEMA else {}


def android_epub_path_for_book(book: dict[str, Any], report: Optional[dict[str, Any]] = None) -> Path:
    report = report or read_book_epub_assets(str(book.get("id") or ""))
    runtime = Path(str(report.get("runtime_epub_path") or "")).expanduser()
    if report.get("reading_profile") == "IMAGE_COMIC" and runtime.exists():
        return runtime
    return epub_path_for_book(book)


def android_book_contract_fields(book: dict[str, Any], report: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    report = report or read_book_epub_assets(str(book.get("id") or ""))
    analysis = living_book_analysis_state_for_book(str(book.get("id") or ""), create=False)
    return {
        "reading_profile": report.get("reading_profile") or "UNKNOWN",
        "compatibility_status": "error" if report.get("status") == "error" else "ready" if report else "pending",
        "toc_depth": report.get("toc_depth_counts") or {},
        "display_variants_available": bool((report.get("display_variants") or {}).get("available")),
        "analysis_state": analysis.get("state") or "not_requested",
        "analysis_updated_at": analysis.get("updated_at") or "",
    }


def lan_reader_html() -> str:
    return r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>Sentence Reader LAN</title>
  <style>
    :root { color-scheme: dark; --bg:#050505; --panel:#141414; --text:#f4f4f4; --muted:#aaa; --line:#2b2b2b; --blue:#62a8ff; --red:rgba(255,59,48,.62); --lan-page-width:100vw; --lan-toolbar-height:42px; --lan-page-gap:36px; --reader-font-size:20px; --reader-line-height:1.62; --reader-side-pad:14px; --reader-bottom-pad:28px; }
    * { box-sizing: border-box; }
    html, body { margin:0; width:100%; height:100%; background:var(--bg); color:var(--text); font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif; overflow:hidden; }
    body { position:fixed; inset:0; min-height:100dvh; }
    button { background:#222; color:var(--text); border:1px solid #3a3a3a; border-radius:7px; padding:6px 8px; font-size:13px; cursor:pointer; white-space:nowrap; }
    button:disabled { opacity:.45; cursor:default; }
    #toolbar { position:fixed; z-index:20; top:0; left:0; right:0; min-height:var(--lan-toolbar-height); display:flex; gap:6px; align-items:center; justify-content:space-between; padding:4px max(8px, env(safe-area-inset-right)) 4px max(8px, env(safe-area-inset-left)); border-bottom:1px solid rgba(255,255,255,.08); background:rgba(5,5,5,.78); backdrop-filter:blur(16px); }
    #toolbarMode { flex:1 1 auto; min-width:0; display:flex; align-items:center; }
    #toolbarActions { display:flex; gap:4px; align-items:center; min-width:0; }
    #toolbarActions button { min-width:34px; min-height:30px; }
    #status { color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; padding:0 2px; min-width:52px; text-align:right; font-size:12px; }
    #readerWrap { position:fixed; left:0; right:0; top:var(--lan-toolbar-height); bottom:0; overflow:hidden; touch-action:pan-y; background:var(--bg); }
    #reader { height:100%; max-width:none; margin:0; padding:8px max(var(--reader-side-pad), env(safe-area-inset-right)) var(--reader-bottom-pad) max(var(--reader-side-pad), env(safe-area-inset-left)); font-size:var(--reader-font-size); line-height:var(--reader-line-height); column-width:calc(var(--lan-page-width) - max(calc(var(--reader-side-pad) * 2), env(safe-area-inset-left) + env(safe-area-inset-right) + calc(var(--reader-side-pad) * 2))); column-gap:var(--lan-page-gap); column-fill:auto; orphans:1; widows:1; transform:translate3d(0,0,0); will-change:transform; transition:transform 220ms ease; overflow:visible; }
    #reader, #reader * { box-sizing:border-box; max-width:100%; overflow-wrap:anywhere; word-break:break-word; }
    #reader p, #reader li, #reader blockquote, #reader div, #reader section, #reader article { min-width:0; }
    #reader table { width:100%; max-width:100%; table-layout:fixed; border-collapse:collapse; }
    #reader td, #reader th { width:auto; max-width:100%; min-width:0; overflow-wrap:anywhere; word-break:break-word; }
    #reader pre, #reader code { white-space:pre-wrap; overflow-wrap:anywhere; }
    #reader div.right, #reader #main1 { display:block; float:none; width:auto; min-width:0; max-width:100%; text-align:left; break-inside:auto; }
    #reader img, #reader svg { max-width:100%; max-height:calc(100dvh - var(--lan-toolbar-height) - 40px); height:auto; display:block; margin:14px auto; object-fit:contain; break-inside:avoid; }
    #reader a { color:#9fc8ff; }
    #reader p, #reader li, #reader blockquote { break-inside:auto; -webkit-column-break-inside:auto; page-break-inside:auto; orphans:1; widows:1; }
    #reader p { margin:0 0 .48em; }
    #reader h1, #reader h2, #reader h3, #reader h4, #reader h5, #reader h6 { margin:0 0 .5em; line-height:1.22; }
    #reader ul, #reader ol { margin:0 0 .48em; padding-left:1.28em; }
    .sr-sentence { border-radius:4px; }
    .sr-sentence.sr-focused { background:rgba(64,156,255,.30); box-shadow:0 0 0 1px rgba(124,190,255,.45) inset; }
    .sr-sentence.sr-red { background:var(--red); color:#fff; }
    .sr-sentence.sr-note { text-decoration-line:underline; text-decoration-style:dotted; text-decoration-color:rgba(96,165,250,.95); text-underline-offset:.18em; }
    #drawer { position:fixed; z-index:40; top:0; bottom:0; left:0; width:min(84vw, 330px); border-right:1px solid var(--line); background:var(--panel); display:flex; flex-direction:column; min-width:0; transform:translate3d(-102%,0,0); transition:transform 180ms ease; box-shadow:18px 0 42px rgba(0,0,0,.38); }
    body.drawer-open #drawer { transform:translate3d(0,0,0); }
    #scrim { position:fixed; z-index:35; inset:0; background:rgba(0,0,0,.48); opacity:0; pointer-events:none; transition:opacity 180ms ease; }
    body.drawer-open #scrim { opacity:1; pointer-events:auto; }
    #drawerHeader { flex:0 0 auto; display:flex; gap:8px; align-items:center; justify-content:space-between; padding:10px 12px; border-bottom:1px solid var(--line); }
    #drawerHeader strong { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    #chapters { flex:1 1 auto; padding:8px; overflow:auto; }
    #chapters .toc-row { white-space:normal; line-height:1.35; padding-left:var(--toc-indent, 8px); }
    #chapters .toc-row[data-level="0"] { font-weight:650; }
    #chapters .toc-row[data-level]:not([data-level="0"]) { color:#ddd; }
    #sentenceBar { display:none; width:min(560px, 100%); grid-template-columns:repeat(5,minmax(0,1fr)); gap:4px; }
    body.sentence-mode #toolbarActions { display:none; }
    body.sentence-mode #sentenceBar.show { display:grid; }
    body.note-editor-mode #sentenceBar.show { display:none; }
    #sentenceBar button { min-height:30px; border-radius:7px; font-size:13px; padding:4px 6px; }
    #readingStats { display:none; }
    #noteToast { position:fixed; z-index:31; left:max(12px, env(safe-area-inset-left)); right:max(12px, env(safe-area-inset-right)); bottom:calc(max(20px, env(safe-area-inset-bottom) + 20px)); max-height:30vh; overflow:auto; padding:10px 12px; border:1px solid rgba(96,165,250,.62); border-radius:11px; background:rgba(18,28,42,.94); color:#fff; box-shadow:0 16px 40px rgba(0,0,0,.42); opacity:0; transform:translate3d(0,14px,0); pointer-events:none; transition:opacity 160ms ease, transform 160ms ease; font-size:15px; line-height:1.58; }
    #noteToast.show { opacity:1; transform:translate3d(0,0,0); pointer-events:auto; }
    #noteToast strong { display:block; margin-bottom:4px; color:#9fc8ff; font-size:13px; }
    #voiceToast { position:fixed; z-index:34; left:max(14px, env(safe-area-inset-left)); right:max(14px, env(safe-area-inset-right)); bottom:calc(var(--lan-toolbar-height) + env(safe-area-inset-bottom) + 20px); max-height:30vh; overflow:auto; padding:12px 14px; border:1px solid rgba(240,211,107,.48); border-radius:14px; background:rgba(13,13,11,.96); color:#f6f0e8; box-shadow:0 16px 44px rgba(0,0,0,.48); opacity:0; transform:translate3d(0,14px,0); pointer-events:none; transition:opacity 160ms ease, transform 160ms ease; font-size:15px; line-height:1.48; }
    #voiceToast.show { opacity:1; transform:translate3d(0,0,0); pointer-events:auto; }
    #voiceToast.success { border-color:rgba(76,175,80,.62); }
    #voiceToast.error { border-color:rgba(255,95,86,.72); }
    #voiceToast.recording { border-color:rgba(255,183,77,.72); }
    .voice-toast-head { display:flex; align-items:center; gap:8px; font-weight:850; font-size:16px; }
    .voice-dot { width:10px; height:10px; border-radius:999px; background:#f0d36b; flex:0 0 auto; }
    #voiceToast.recording .voice-dot { background:#ff5f56; animation:voicePulse 1s ease-in-out infinite; }
    .voice-toast-body { margin-top:6px; color:#d9d0bd; overflow-wrap:anywhere; word-break:break-word; }
    .voice-toast-actions { display:flex; gap:8px; margin-top:10px; }
    .voice-toast-actions button { min-height:38px; border-radius:10px; padding:8px 14px; font-size:15px; font-weight:800; }
    @keyframes voicePulse { 0%,100% { transform:scale(.86); opacity:.62; } 50% { transform:scale(1.18); opacity:1; } }
    #noteEditor { position:fixed; z-index:46; left:max(12px, env(safe-area-inset-left)); right:max(12px, env(safe-area-inset-right)); top:calc(var(--lan-toolbar-height) + max(12px, env(safe-area-inset-top))); bottom:auto; max-height:min(64vh, 460px); display:none; grid-template-rows:auto auto minmax(128px, 1fr) auto auto; gap:9px; padding:13px; border:1px solid rgba(96,165,250,.58); border-radius:14px; background:rgba(15,18,23,.97); color:#f4f1e8; box-shadow:0 18px 56px rgba(0,0,0,.50); }
    #noteEditor.show { display:grid; }
    #noteEditorSource { max-height:76px; overflow:auto; padding:8px 10px; border-radius:10px; background:rgba(255,255,255,.055); color:#d8d3c6; font-size:14px; line-height:1.45; }
    #noteEditorText { width:100%; min-height:128px; max-height:24vh; resize:vertical; border:1px solid rgba(255,255,255,.16); border-radius:11px; background:rgba(0,0,0,.22); color:#fff; padding:10px 11px; font-size:16px; line-height:1.55; outline:none; }
    #noteEditorText:focus { border-color:rgba(96,165,250,.82); box-shadow:0 0 0 2px rgba(96,165,250,.18); }
    #noteEditorStatus { min-height:18px; color:#aaa18f; font-size:13px; line-height:1.35; }
    .note-editor-actions { display:grid; grid-template-columns:1fr 1fr 1fr 1fr; gap:8px; }
    .note-editor-actions button { min-height:40px; border-radius:10px; font-size:15px; font-weight:820; }
    #lookupCard { position:fixed; z-index:50; left:max(12px, env(safe-area-inset-left)); right:max(12px, env(safe-area-inset-right)); bottom:calc(max(24px, env(safe-area-inset-bottom) + 24px)); max-height:42vh; overflow:auto; padding:12px; border:1px solid rgba(215,168,79,.55); border-radius:12px; background:rgba(22,22,17,.96); color:#fff; box-shadow:0 18px 48px rgba(0,0,0,.48); opacity:0; transform:translate3d(0,14px,0); pointer-events:none; transition:opacity 160ms ease, transform 160ms ease; }
    #lookupCard.show { opacity:1; transform:translate3d(0,0,0); pointer-events:auto; }
    #lookupCard h3 { margin:0 0 4px; font-size:22px; line-height:1.2; }
    #lookupCard .meaning { color:#f2c36d; font-weight:800; margin-bottom:7px; }
    #lookupCard .lookup-text { color:#e9e2cf; line-height:1.55; margin:5px 0; font-size:14px; }
    #lookupCard .lookup-zh { color:#cfc7ad; }
    #lookupCard .lookup-actions { display:flex; flex-wrap:wrap; gap:6px; margin-top:10px; }
    #lookupCard .lookup-correction { margin-top:10px; padding-top:10px; border-top:1px solid rgba(255,255,255,.12); }
    #lookupCard textarea { width:100%; box-sizing:border-box; min-height:78px; resize:vertical; border:1px solid rgba(215,168,79,.46); border-radius:10px; background:rgba(255,255,255,.08); color:#fff; padding:10px; font:inherit; line-height:1.45; }
    #lookupCard textarea::placeholder { color:rgba(255,255,255,.42); }
    #lookupCard .lookup-correction-actions { display:flex; gap:8px; margin-top:8px; }
    #settingsSheet { position:fixed; z-index:45; left:max(12px, env(safe-area-inset-left)); right:max(12px, env(safe-area-inset-right)); bottom:max(12px, env(safe-area-inset-bottom)); border:1px solid var(--line); border-radius:14px; background:rgba(20,20,20,.96); box-shadow:0 18px 54px rgba(0,0,0,.50); padding:14px; display:none; }
    #settingsSheet.show { display:block; }
    #settingsSheet label { display:grid; gap:6px; margin:10px 0; color:var(--muted); font-size:13px; }
    #settingsSheet input[type=range] { width:100%; }
    .setting-row { display:flex; justify-content:space-between; gap:10px; align-items:center; }
    .row { width:100%; text-align:left; margin:0 0 6px; display:block; }
    .row.active { border-color:var(--blue); color:#fff; }
    @media (min-width: 900px) {
      #reader { padding-left:calc((100vw - 820px) / 2); padding-right:calc((100vw - 820px) / 2); column-width:min(820px, calc(var(--lan-page-width) - 48px)); }
    }
    body.reader-large-font #reader { padding-left:max(var(--reader-side-pad), env(safe-area-inset-left)); padding-right:max(var(--reader-side-pad), env(safe-area-inset-right)); column-width:calc(var(--lan-page-width) - max(calc(var(--reader-side-pad) * 2), env(safe-area-inset-left) + env(safe-area-inset-right) + calc(var(--reader-side-pad) * 2))); }
    @media (max-width: 1024px) {
      :root { --lan-toolbar-height:52px; --reader-bottom-pad:max(8px, env(safe-area-inset-bottom) + 4px); }
      #toolbar { top:auto; bottom:0; min-height:calc(var(--lan-toolbar-height) + env(safe-area-inset-bottom)); padding:5px max(10px, env(safe-area-inset-right)) max(5px, env(safe-area-inset-bottom)) max(10px, env(safe-area-inset-left)); border-top:1px solid rgba(255,255,255,.10); border-bottom:0; }
      #readerWrap { top:0; bottom:calc(var(--lan-toolbar-height) + env(safe-area-inset-bottom)); }
      #reader { padding-top:8px; }
      #reader img, #reader svg { max-height:calc(100dvh - var(--lan-toolbar-height) - env(safe-area-inset-bottom) - 24px); }
      #toolbarActions { gap:6px; }
      #toolbarActions button { min-width:48px; min-height:42px; padding:8px 10px; border-radius:10px; font-size:16px; font-weight:750; }
      #sentenceBar { gap:6px; }
      #sentenceBar button { min-height:42px; border-radius:10px; padding:8px 9px; font-size:15px; font-weight:750; }
      #status { font-size:16px; font-weight:800; text-align:right; min-width:46px; color:#f4f4f4; }
      #noteToast { bottom:calc(var(--lan-toolbar-height) + env(safe-area-inset-bottom) + 24px); }
      #voiceToast { bottom:calc(var(--lan-toolbar-height) + env(safe-area-inset-bottom) + 24px); }
      #noteEditor { top:calc(max(12px, env(safe-area-inset-top)) + 12px); bottom:auto; max-height:62vh; }
      #noteEditorText { min-height:150px; font-size:17px; }
      #lookupCard { bottom:calc(var(--lan-toolbar-height) + env(safe-area-inset-bottom) + 28px); }
      #reader p { margin-bottom:.34em; }
    }
    @media (max-width: 760px) {
      :root { --lan-toolbar-height:52px; --lan-page-gap:34px; --reader-bottom-pad:max(8px, env(safe-area-inset-bottom) + 4px); }
      :root { --reader-font-size:19px; --reader-line-height:1.56; }
      #reader { padding-top:6px; }
      #toolbarActions { gap:4px; }
      #toolbarActions button { min-width:42px; min-height:42px; padding:7px 8px; border-radius:10px; font-size:15px; font-weight:760; }
      #sentenceBar { gap:4px; }
      #sentenceBar button { min-width:0; min-height:42px; padding:7px 6px; border-radius:10px; font-size:15px; font-weight:760; }
      #status { font-size:16px; min-width:44px; }
    }
  </style>
</head>
<body>
  <div id="scrim"></div>
  <aside id="drawer">
    <div id="drawerHeader"><strong id="drawerTitle">目录</strong><button id="closeDrawer">收起</button></div>
    <div id="chapters"></div>
  </aside>
  <header id="toolbar">
    <div id="toolbarMode">
      <div id="toolbarActions">
        <button id="libraryHome">书库</button>
        <button id="tocToggle">目录</button>
        <button id="vocabHome">单词</button>
        <button id="fontSettings">Aa</button>
      </div>
      <div id="sentenceBar" aria-hidden="true">
        <button id="barRed">红标</button>
        <button id="barNote">笔记</button>
        <button id="barVoice">语音</button>
        <button id="barCopy">复制</button>
        <button id="barCancel">取消</button>
      </div>
    </div>
    <div id="status">正在加载...</div>
  </header>
  <input id="audioFile" type="file" accept="audio/*" capture="microphone" style="display:none">
  <main id="readerWrap"><article id="reader"></article></main>
  <div id="readingStats"></div>
  <div id="noteToast"></div>
  <div id="voiceToast" aria-live="polite"></div>
  <section id="noteEditor" aria-hidden="true">
    <div class="setting-row"><strong id="noteEditorTitle">句子备注</strong><button id="noteEditorClose" type="button">关闭</button></div>
    <div id="noteEditorSource"></div>
    <textarea id="noteEditorText" placeholder="输入文字备注，或点语音把转写插入这里。"></textarea>
    <div id="noteEditorStatus"></div>
    <div class="note-editor-actions">
      <button id="noteEditorVoice" type="button">语音</button>
      <button id="noteEditorClean" type="button">整理</button>
      <button id="noteEditorSave" type="button">保存</button>
      <button id="noteEditorCancel" type="button">取消</button>
    </div>
  </section>
  <div id="lookupCard"></div>
  <section id="settingsSheet" aria-hidden="true">
    <div class="setting-row"><strong>阅读设置</strong><button id="closeSettings">关闭</button></div>
    <label>字体大小 <input id="fontSize" type="range" min="16" max="30" step="1"></label>
    <label>行距 <input id="lineHeight" type="range" min="1.2" max="2.05" step="0.05"></label>
    <label>页边距 <input id="sidePadding" type="range" min="4" max="40" step="2"></label>
  </section>
  <script>
    const initialBookID = new URLSearchParams(window.location.search).get('book_id');
    const state = { books: [], book: null, manifest: null, chapterIndex: 0, annotations: [], focused: null, redIDs: new Map(), noteByIndex: new Map(), saveTimer: 0, noteTimer: 0, sentenceTapTimer: 0, voiceToastTimer: 0, noteEditorAnnotationID: '', noteEditorAudioNoteID: '', pendingAudioPolls: new Map(), pageIndex: 0, totalPages: 1, pageTurnLockUntil: 0, pendingPageTurnDirection: 0, pendingPageTurnTimer: 0, wheelGestureDirection: 0, wheelGestureDistance: 0, wheelGestureConsumed: false, lastWheelEventAt: 0, wheelInertiaLockUntil: 0, wheelGestureReleaseUntil: 0, touchStartX: 0, touchStartY: 0, touchStartTime: 0, touchSentence: null, longPressTimer: 0, longPressTriggered: false, recognition: null, mediaRecorder: null, nativeReaderAudioRecording: false, voiceChunks: [], voiceStartedAt: 0, voiceStream: null, lookup: null };
    // Keep the physical wheel gesture boundary separate from page animation:
    // one swipe triggers one page, while the next clear swipe may arrive before the animation ends.
    const pageTurnCooldownMs = 120;
    const wheelInertiaLockMs = 520;
    const wheelGestureIdleMs = 420;
    const wheelPageTurnThreshold = 120;
    const wheelDominanceRatio = 1.25;
    const $ = (id) => document.getElementById(id);
    const voiceNotePendingText = '语音转写中...';
    const voiceNoteFailedText = '语音已保存，转写失败，可稍后重试。';
    function status(text) { $('status').textContent = text; }
    function openDrawer() { document.body.classList.add('drawer-open'); }
    function closeDrawer() { document.body.classList.remove('drawer-open'); }
    const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
    const noteSpokenPunctuation = [['新的一行','\n'],['另起一行','\n'],['换行','\n'],['句号','。'],['逗号','，'],['顿号','、'],['问号','？'],['感叹号','！'],['叹号','！'],['冒号','：'],['分号','；'],['省略号','……']];
    const noteClosingPunctuation = `。！？!?.…；;：:，,、）)]】》」』”’"'`;
    function normalizeNoteText(value) {
      let text = String(value ?? '').trim();
      if (!text) return '';
      noteSpokenPunctuation.forEach(([spoken, mark]) => { text = text.split(spoken).join(mark); });
      text = text
        .replace(/\s+([，。！？；：、,.!?;:])/g, '$1')
        .replace(/([，。！？；：、,.!?;:])\s+/g, '$1')
        .replace(/([。！？!?]){2,}/g, '$1')
        .replace(/\n{3,}/g, '\n\n')
        .trim();
      if (!text || noteClosingPunctuation.includes(text[text.length - 1])) return text;
      return text + (/[A-Za-z]/.test(text) && !/[\u4e00-\u9fff]/.test(text) ? '.' : '。');
    }
    async function json(url, options) {
      const response = await fetch(url, options);
      if (!response.ok) throw new Error(await response.text());
      return response.json();
    }
    function pageWidth() { return Math.max(320, Math.floor($('readerWrap').clientWidth || window.innerWidth || 0)); }
    function pageRatio() {
      const denominator = Math.max(1, state.totalPages - 1);
      return Math.max(0, Math.min(1, state.pageIndex / denominator));
    }
    function updatePaginationStatus() {
      status(`${state.pageIndex + 1}/${state.totalPages}`);
      updateReadingStats();
      const prevButton = $('prev');
      const nextButton = $('next');
      if (prevButton) prevButton.disabled = state.chapterIndex <= 0 && state.pageIndex <= 0;
      if (nextButton) nextButton.disabled = state.manifest ? (state.chapterIndex >= state.manifest.chapters.length - 1 && state.pageIndex >= state.totalPages - 1) : true;
    }
    function updateReadingStats() {
      const stats = $('readingStats');
      if (stats) stats.textContent = '';
    }
    function applyPage(animated = true) {
      const reader = $('reader');
      reader.style.transition = animated ? 'transform 220ms ease' : 'none';
      reader.style.transform = `translate3d(${-state.pageIndex * pageWidth()}px,0,0)`;
      updatePaginationStatus();
      savePositionSoon();
    }
    function measuredContentWidth() {
      const width = pageWidth();
      const reader = $('reader');
      const scrollWidth = Math.max(width, Math.ceil(reader.scrollWidth || width));
      const baseLeft = reader.getBoundingClientRect().left;
      let rightEdge = 0;
      reader.querySelectorAll('.sr-sentence, img, svg, image, figure, h1, h2, h3, h4, h5, h6, p, li, blockquote').forEach((node) => {
        if (!node.getClientRects) return;
        Array.from(node.getClientRects()).forEach((rect) => {
          if (rect.width <= 0 || rect.height <= 0) return;
          rightEdge = Math.max(rightEdge, rect.right - baseLeft);
        });
      });
      if (rightEdge > 0) return Math.max(width, Math.ceil(rightEdge));
      return scrollWidth;
    }
    function layoutPages(savedRatio = null) {
      const width = pageWidth();
      const reader = $('reader');
      document.documentElement.style.setProperty('--lan-page-width', `${width}px`);
      const computed = window.getComputedStyle(reader);
      const horizontalPadding = Math.ceil(parseFloat(computed.paddingLeft || '0') + parseFloat(computed.paddingRight || '0'));
      document.documentElement.style.setProperty('--lan-page-gap', `${Math.max(0, horizontalPadding)}px`);
      reader.style.transform = 'translate3d(0,0,0)';
      const contentWidth = measuredContentWidth();
      state.totalPages = Math.max(1, Math.ceil((contentWidth - 2) / width));
      if (savedRatio !== null && Number.isFinite(savedRatio)) {
        state.pageIndex = Math.max(0, Math.min(state.totalPages - 1, Math.round(savedRatio * Math.max(0, state.totalPages - 1))));
      } else {
        state.pageIndex = Math.max(0, Math.min(state.pageIndex, state.totalPages - 1));
      }
      applyPage(false);
    }
    function schedulePendingPageTurn(direction) {
      state.pendingPageTurnDirection = direction < 0 ? -1 : 1;
      if (state.pendingPageTurnTimer) return;
      const delay = Math.max(0, state.pageTurnLockUntil - Date.now()) + 8;
      state.pendingPageTurnTimer = window.setTimeout(() => {
        state.pendingPageTurnTimer = 0;
        const queuedDirection = state.pendingPageTurnDirection;
        state.pendingPageTurnDirection = 0;
        if (queuedDirection) void turnPage(queuedDirection, { fromPending: true });
      }, delay);
    }
    async function turnPage(direction, options = {}) {
      if (!state.manifest) return false;
      const now = Date.now();
      if (now < state.pageTurnLockUntil) {
        if (options.allowQueue === false) return false;
        schedulePendingPageTurn(direction);
        return false;
      }
      if (options.fromPending) state.pendingPageTurnDirection = 0;
      state.pageTurnLockUntil = now + pageTurnCooldownMs;
      const before = state.pageIndex;
      state.pageIndex = Math.max(0, Math.min(state.pageIndex + direction, state.totalPages - 1));
      if (state.pageIndex !== before) {
        applyPage(true);
        return true;
      }
      const nextChapter = state.chapterIndex + direction;
      if (nextChapter >= 0 && nextChapter < state.manifest.chapters.length) {
        await loadChapter(nextChapter, direction < 0 ? 1 : 0);
        return true;
      }
      return false;
    }
    function resetWheelGesture() {
      state.wheelGestureDirection = 0;
      state.wheelGestureDistance = 0;
      state.wheelGestureConsumed = false;
      state.wheelGestureReleaseUntil = 0;
    }
    function handleHorizontalWheel(event) {
      const now = Date.now();
      const absX = Math.abs(event.deltaX || 0);
      const absY = Math.abs(event.deltaY || 0);
      const startsNewGesture = state.lastWheelEventAt === 0 || (now - state.lastWheelEventAt > wheelGestureIdleMs && now > state.wheelGestureReleaseUntil);
      if (startsNewGesture) resetWheelGesture();
      state.lastWheelEventAt = now;
      if (absX < 10 || absX < absY * wheelDominanceRatio) {
        if (!state.wheelGestureConsumed) {
          resetWheelGesture();
        } else {
          state.wheelGestureReleaseUntil = now + wheelGestureIdleMs;
        }
        return false;
      }
      if (state.wheelGestureConsumed) {
        state.wheelGestureReleaseUntil = now + wheelGestureIdleMs;
        return true;
      }
      if (now < state.wheelInertiaLockUntil && !startsNewGesture) {
        state.wheelGestureConsumed = true;
        state.wheelGestureReleaseUntil = now + wheelGestureIdleMs;
        return true;
      }
      const direction = event.deltaX > 0 ? 1 : -1;
      if (state.wheelGestureDirection !== 0 && direction !== state.wheelGestureDirection) {
        if (absX < 18) return true;
        resetWheelGesture();
      }
      if (state.wheelGestureDirection === 0) state.wheelGestureDirection = direction;
      if (state.wheelGestureConsumed) return true;
      state.wheelGestureDistance += absX;
      if (state.wheelGestureDistance >= wheelPageTurnThreshold) {
        const turnDirection = state.wheelGestureDirection;
        state.wheelGestureConsumed = true;
        state.wheelInertiaLockUntil = now + wheelInertiaLockMs;
        state.wheelGestureReleaseUntil = now + wheelGestureIdleMs;
        void turnPage(turnDirection, { fromWheel: true, allowQueue: false });
      }
      return true;
    }
    function sentenceParts(text) {
      const out = [];
      const sentenceBoundaryRegex = /([^。！？!?\n]+[。！？!?]+[”’」』）】》〕〉]*|[^。！？!?\n]+$|\n+)/g;
      let match;
      while ((match = sentenceBoundaryRegex.exec(text)) !== null) out.push(match[0]);
      return out.length ? out : [text];
    }
    function wrapSentences(root) {
      let nextIndex = 0;
      root.querySelectorAll('p, li, blockquote, h1, h2, h3, h4, h5, h6').forEach((block) => {
        const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT);
        const nodes = [];
        while (walker.nextNode()) nodes.push(walker.currentNode);
        nodes.forEach((node) => {
          if (!node.nodeValue.trim() || node.parentElement.closest('.sr-sentence,script,style,code,pre')) return;
          const parts = sentenceParts(node.nodeValue);
          if (parts.length <= 1 && parts[0].trim().length < 8) return;
          const fragment = document.createDocumentFragment();
          parts.forEach((part) => {
            if (!part.trim()) { fragment.appendChild(document.createTextNode(part)); return; }
            const span = document.createElement('span');
            span.className = 'sr-sentence';
            span.dataset.srIndex = String(nextIndex++);
            span.textContent = part;
            fragment.appendChild(span);
          });
          node.parentNode.replaceChild(fragment, node);
        });
      });
    }
    function annotationIndex(row) {
      const range = row.range_locator || {};
      const metadata = row.metadata || {};
      return String(range.sentenceIndex ?? metadata.sentenceIndex ?? '');
    }
    function indexList(value) { return String(value || '').split(',').map((x) => x.trim()).filter(Boolean); }
    function applyAnnotations() {
      state.redIDs.clear();
      state.noteByIndex.clear();
      document.querySelectorAll('.sr-sentence').forEach((node) => {
        node.classList.remove('sr-red', 'sr-note');
        delete node.dataset.noteText;
        delete node.dataset.noteID;
      });
      state.annotations.filter((row) => row.chapter_locator === state.manifest.chapters[state.chapterIndex].locator).forEach((row) => {
        indexList(annotationIndex(row)).forEach((index) => {
          const node = document.querySelector(`.sr-sentence[data-sr-index="${index}"]`);
          if (!node) return;
          if (row.kind === 'red_highlight') { node.classList.add('sr-red'); state.redIDs.set(index, row.id); }
          if (row.kind === 'note') { node.classList.add('sr-note'); node.dataset.noteID = row.id; node.dataset.noteText = row.note_text || ''; state.noteByIndex.set(index, row); }
        });
      });
    }
    function focusSentence(node) {
      if (!node) return;
      document.querySelectorAll('.sr-focused').forEach((item) => item.classList.remove('sr-focused'));
      node.classList.add('sr-focused');
      state.focused = node;
      const note = state.noteByIndex.get(node.dataset.srIndex || '');
      if (note) {
        showNoteToast(note.note_text || '空');
        status('已选中有注释的句子');
      } else {
        hideNoteToast();
        status('已选中一句话');
      }
      showSentenceBar();
    }
    function showSentenceBar() {
      const bar = $('sentenceBar');
      document.body.classList.add('sentence-mode');
      bar.classList.add('show');
      bar.setAttribute('aria-hidden', 'false');
    }
    function hideSentenceBar() {
      const bar = $('sentenceBar');
      document.body.classList.remove('sentence-mode');
      bar.classList.remove('show');
      bar.setAttribute('aria-hidden', 'true');
    }
    function clearSentenceFocus() {
      document.querySelectorAll('.sr-focused').forEach((item) => item.classList.remove('sr-focused'));
      state.focused = null;
      hideSentenceBar();
      hideNoteToast();
      status('继续阅读');
    }
    function hideNoteToast() {
      clearTimeout(state.noteTimer);
      const toast = $('noteToast');
      toast.classList.remove('show');
      toast.innerHTML = '';
    }
    function noteToastVisible() {
      return $('noteToast').classList.contains('show');
    }
    function showNoteToast(noteText) {
      clearTimeout(state.noteTimer);
      const toast = $('noteToast');
      toast.innerHTML = `<strong>注释</strong>${String(noteText || '空').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]))}`;
      toast.classList.add('show');
      state.noteTimer = setTimeout(() => toast.classList.remove('show'), 9000);
    }
    function noteEditorOpen() {
      return $('noteEditor').classList.contains('show');
    }
    function setNoteEditorStatus(text) {
      $('noteEditorStatus').textContent = text || '';
    }
    function closeNoteEditor() {
      const editor = $('noteEditor');
      editor.classList.remove('show');
      editor.setAttribute('aria-hidden', 'true');
      document.body.classList.remove('note-editor-mode');
      setNoteEditorStatus('');
      state.noteEditorAnnotationID = '';
      state.noteEditorAudioNoteID = '';
    }
    function appendNoteEditorText(text) {
      const value = normalizeNoteText(text);
      if (!value) return;
      const textarea = $('noteEditorText');
      const current = String(textarea.value || '').trim();
      textarea.value = current ? `${current}\n${value}` : value;
      textarea.focus();
      textarea.selectionStart = textarea.selectionEnd = textarea.value.length;
      setNoteEditorStatus('语音已转写到备注框，检查后点保存。');
    }
    function replacePendingNoteText(text) {
      const value = normalizeNoteText(text);
      if (!value) return;
      const textarea = $('noteEditorText');
      const current = String(textarea.value || '').trim();
      if (!current || current === voiceNotePendingText) {
        textarea.value = value;
      } else if (current.includes(voiceNotePendingText)) {
        textarea.value = normalizeNoteText(current.split(voiceNotePendingText).join(value));
      } else if (!current.includes(value)) {
        textarea.value = normalizeNoteText(`${current}\n${value}`);
      }
      textarea.focus();
      textarea.selectionStart = textarea.selectionEnd = textarea.value.length;
      setNoteEditorStatus('后台转写已完成，已补到备注框。');
    }
    function addPendingNoteText() {
      const textarea = $('noteEditorText');
      const current = String(textarea.value || '').trim();
      if (!current) textarea.value = voiceNotePendingText;
      else if (!current.includes(voiceNotePendingText)) textarea.value = normalizeNoteText(`${current}\n${voiceNotePendingText}`);
      setNoteEditorStatus('语音已保存，后台正在转写。你可以先退出。');
    }
    function openNoteEditor(options = {}) {
      const node = state.focused;
      if (!node || !state.book || !state.manifest) {
        status('先点一句话');
        return false;
      }
      const existing = state.noteByIndex.get(node.dataset.srIndex || '');
      $('noteEditorSource').textContent = String(node.textContent || '').trim();
      $('noteEditorText').value = existing ? (existing.note_text || '') : '';
      state.noteEditorAnnotationID = existing ? (existing.id || '') : '';
      state.noteEditorAudioNoteID = existing && existing.metadata && existing.metadata.voice_note ? (existing.metadata.voice_note.audio_note_id || '') : '';
      setNoteEditorStatus(existing ? '已载入原备注，可继续修改。' : '输入文字备注，或点语音插入转写。');
      hideNoteToast();
      hideLookupCard();
      closeSettingsSheet();
      const editor = $('noteEditor');
      document.body.classList.add('note-editor-mode');
      editor.classList.add('show');
      editor.setAttribute('aria-hidden', 'false');
      if (options.focusText !== false) $('noteEditorText').focus();
      status('正在编辑备注');
      return true;
    }
    async function linkAudioNoteToAnnotation(audioNoteID, annotation) {
      if (!audioNoteID || !annotation || !annotation.id) return;
      try {
        await json(`/audio-notes/${audioNoteID}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ annotation_id: annotation.id })
        });
      } catch (error) {}
    }
    async function ensureNoteEditorAnnotation() {
      const note = normalizeNoteText($('noteEditorText').value);
      if (!note) return null;
      const annotation = await saveNoteText(note);
      state.noteEditorAnnotationID = annotation && annotation.id ? annotation.id : state.noteEditorAnnotationID;
      return annotation;
    }
    async function saveNoteEditor() {
      const note = normalizeNoteText($('noteEditorText').value);
      if (!note) {
        setNoteEditorStatus('备注为空，未保存。');
        status('备注为空');
        return;
      }
      setNoteEditorStatus('正在保存...');
      const annotation = await saveNoteText(note);
      state.noteEditorAnnotationID = annotation && annotation.id ? annotation.id : state.noteEditorAnnotationID;
      await linkAudioNoteToAnnotation(state.noteEditorAudioNoteID, annotation);
      setNoteEditorStatus('已保存。');
      closeNoteEditor();
    }
    async function cleanNoteEditorText() {
      const note = normalizeNoteText($('noteEditorText').value);
      if (!note) {
        setNoteEditorStatus('备注为空，没法整理。');
        return;
      }
      setNoteEditorStatus('正在让 Hermes / Qwen 整理...');
      let annotationID = state.noteEditorAnnotationID;
      if (!annotationID) {
        const annotation = await ensureNoteEditorAnnotation();
        annotationID = annotation && annotation.id;
      }
      if (!annotationID) {
        setNoteEditorStatus('保存备注失败，暂时不能整理。');
        return;
      }
      const payload = await json(`/annotations/${annotationID}/clean`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ apply: true })
      });
      $('noteEditorText').value = payload.cleaned_text || note;
      state.noteEditorAnnotationID = annotationID;
      await refreshAnnotations();
      setNoteEditorStatus('已整理并保存。');
      status('备注已整理');
    }
    function hideLookupCard() {
      const card = $('lookupCard');
      card.classList.remove('show');
      card.innerHTML = '';
      state.lookup = null;
    }
    function speak(text, lang = 'en-US') {
      const value = String(text || '').trim();
      if (!value || !window.speechSynthesis) return;
      window.speechSynthesis.cancel();
      const utterance = new SpeechSynthesisUtterance(value);
      utterance.lang = lang;
      utterance.rate = .92;
      window.speechSynthesis.speak(utterance);
    }
    async function speakLookupMeaning(text) {
      const value = String(text || '').trim();
      if (!value) return;
      try {
        const response = await fetch('/lookup/tts', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: value })
        });
        const payload = await response.json();
        if (payload.ok && payload.audio_url) {
          window.speechSynthesis?.cancel();
          const audio = new Audio(payload.audio_url);
          await audio.play();
          return;
        }
      } catch (error) {
        // Browser speech synthesis is the local fallback when edge-tts is unavailable.
      }
      speak(value, 'zh-CN');
    }
    function wordFromSelection() {
      const selection = window.getSelection && window.getSelection();
      const text = selection ? String(selection.toString() || '').trim() : '';
      const match = text.match(/[A-Za-z][A-Za-z’'-]*/);
      return match ? match[0] : '';
    }
    function wordFromPoint(event) {
      let range = null;
      if (document.caretRangeFromPoint) {
        range = document.caretRangeFromPoint(event.clientX, event.clientY);
      } else if (document.caretPositionFromPoint) {
        const pos = document.caretPositionFromPoint(event.clientX, event.clientY);
        if (pos) {
          range = document.createRange();
          range.setStart(pos.offsetNode, pos.offset);
        }
      }
      const node = range && range.startContainer && range.startContainer.nodeType === Node.TEXT_NODE ? range.startContainer : null;
      if (!node) return '';
      const text = node.nodeValue || '';
      let start = Math.max(0, range.startOffset || 0);
      let end = start;
      while (start > 0 && /[A-Za-z’'-]/.test(text[start - 1])) start -= 1;
      while (end < text.length && /[A-Za-z’'-]/.test(text[end])) end += 1;
      const word = text.slice(start, end).trim();
      return /^[A-Za-z][A-Za-z’'-]{1,}$/.test(word) ? word : '';
    }
    function lookupWordFromEvent(event) {
      return wordFromSelection() || wordFromPoint(event);
    }
    window.__SentenceReaderInteractionRouter = {
      contractVersion: 'sentence-reader-interaction-v1',
      priority: 'sentence-reader-first',
      systemWhen: ['editable-target'],
      sentenceWhen: ['tap-focus-actions', 'english-tap-lookup', 'double-tap-note', 'context-click-red', 'long-press-red'],
      sentenceContextWinsEvenWithSelection: true,
      copyPath: 'command-c'
    };
    function isEditableTarget(target) {
      const node = target && target.nodeType === Node.ELEMENT_NODE ? target : target && target.parentElement;
      if (!node || !node.closest) return false;
      return !!node.closest('input, textarea, select, button, [contenteditable="true"], [contenteditable=""]');
    }
    function shouldLetSystemHandle(event, options = {}) {
      if (isEditableTarget(event && event.target)) return true;
      return false;
    }
    function claimSentenceEvent(event) {
      if (!event) return;
      event.preventDefault();
      event.stopPropagation();
      if (event.stopImmediatePropagation) event.stopImmediatePropagation();
    }
    function shouldLetSystemHandleContext(event) {
      if (isEditableTarget(event && event.target)) return true;
      const node = event && event.target && event.target.closest && event.target.closest('.sr-sentence');
      if (node) return false;
      return false;
    }
    async function updateVocabStatus(statusValue) {
      const item = state.lookup && state.lookup.item;
      if (!item || !state.book) return;
      await json(`/books/${state.book.id}/vocab/${item.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: statusValue })
      });
      status(statusValue === 'known' ? '已标记掌握' : '已加入复习');
      hideLookupCard();
    }
    async function updateVocabMeaning() {
      const current = state.lookup;
      const item = current && current.item;
      if (!current || !state.book) return;
      const input = $('lookupMeaningInput');
      const next = input ? input.value : '';
      const value = next.trim();
      if (!value) return;
      const updated = item && item.id
        ? await json(`/books/${state.book.id}/vocab/${item.id}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ context_meaning_zh: value })
          })
        : await json(`/books/${state.book.id}/lookup-corrections`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              word: current.word,
              meaning_zh: value,
              sentence: current.sentence || '',
              sentence_id: current.sentenceIndex || ''
            })
          });
      renderLookupCard({ ...current.payload, found: true, item: updated }, current.word, current.sentence, current.sentenceIndex);
      status('已保存本书释义');
    }
    function renderLookupCard(payload, word, sentence, sentenceIndex = '') {
      const item = payload.item || {};
      const meaning = item.context_meaning_zh || '看本句中文';
      const en = item.representative_sentence_en || sentence || '';
      const zh = item.representative_sentence_zh || '';
      const metadata = item.metadata || {};
      const partOfSpeechZh = metadata.part_of_speech_zh || item.part_of_speech_zh || '';
      const partOfSpeech = metadata.part_of_speech || item.part_of_speech || '';
      const popupSpeakText = metadata.popup_speak_text_zh || item.popup_speak_text_zh || [partOfSpeechZh, meaning].filter(Boolean).join('，') || meaning;
      const reviewable = item.reviewable !== false && !!item.id;
      state.lookup = { payload, item, word, sentence, sentenceIndex };
      const sourceTitle = item.meaning_source === 'lifestudy_domain_glossary'
        ? '生命读经词库'
        : (item.meaning_source === 'user_glossary' ? '用户修正' : (item.meaning_source === 'dictionary_fallback' ? '词典短释' : (item.meaning_source === 'online_lookup' ? '在线查询' : '')));
      const source = sourceTitle ? `<span class="pill">${esc(sourceTitle)}</span>` : '';
      const posLine = partOfSpeechZh ? `<span class="pill">${esc(partOfSpeechZh)}</span>` : (partOfSpeech ? `<span class="pill">${esc(partOfSpeech)}</span>` : '');
      const sourcePage = metadata.source_page ? `第 ${esc(metadata.source_page)} 页` : '';
      const sourceVolume = metadata.volume ? esc(metadata.volume) : '';
      const sourceMeta = [sourceVolume, sourcePage].filter(Boolean).join(' · ');
      const sourceLine = sourceMeta ? `<div class="lookup-text lookup-zh">出处：${sourceMeta}</div>` : '';
      const reviewActions = reviewable
        ? '<button id="lookupReview">复习</button><button id="lookupKnown">掌握</button>'
        : '';
      const correction = `<div id="lookupCorrection" class="lookup-correction" hidden><textarea id="lookupMeaningInput" placeholder="填写或修正这个词在当前书里的中文意思">${esc(item.context_meaning_zh || '')}</textarea><div class="lookup-correction-actions"><button id="lookupSaveMeaning">保存释义</button><button id="lookupCancelMeaning">取消</button></div></div>`;
      $('lookupCard').innerHTML = `<h3>${esc(word)}</h3><div class="meaning">${esc(meaning)}</div><div>${source}${posLine}</div>${sourceLine}<div class="lookup-text">${esc(en)}</div><div class="lookup-text lookup-zh">${esc(zh)}</div><div class="lookup-actions"><button id="lookupSpeakWord">读词</button><button id="lookupSpeakMeaning">读释义</button><button id="lookupSpeakSentence">读句</button><button id="lookupCopy">复制</button><button id="lookupEditMeaning">纠错</button>${reviewActions}<button id="lookupClose">关闭</button></div>${correction}`;
      $('lookupCard').classList.add('show');
      $('lookupSpeakWord').onclick = () => speak(word, 'en-US');
      $('lookupSpeakMeaning').onclick = () => speakLookupMeaning(popupSpeakText).catch(() => speak(popupSpeakText, 'zh-CN'));
      $('lookupSpeakSentence').onclick = () => speak(en, 'en-US');
      $('lookupCopy').onclick = () => navigator.clipboard?.writeText(`${word}\\n${meaning}\\n${en}\\n${zh}`).then(() => status('已复制查词卡片')).catch(() => status('复制失败'));
      $('lookupEditMeaning').onclick = () => {
        const panel = $('lookupCorrection');
        panel.hidden = false;
        $('lookupMeaningInput')?.focus();
      };
      $('lookupSaveMeaning').onclick = () => updateVocabMeaning().catch((error) => status(`保存失败：${error.message}`));
      $('lookupCancelMeaning').onclick = () => { $('lookupCorrection').hidden = true; };
      if (reviewable) {
        $('lookupReview').onclick = () => updateVocabStatus('reviewing').catch((error) => status(`保存失败：${error.message}`));
        $('lookupKnown').onclick = () => updateVocabStatus('known').catch((error) => status(`保存失败：${error.message}`));
      }
      $('lookupClose').onclick = hideLookupCard;
    }
    async function showLookup(word, sentence, sentenceIndex) {
      if (!state.book || !word) return false;
      status(`正在查词：${word}`);
      const params = new URLSearchParams({ word, sentence_id: sentenceIndex || '', sentence: sentence || '' });
      const payload = await json(`/books/${state.book.id}/lookup?${params.toString()}`);
      await json(`/books/${state.book.id}/lookup-events`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ surface: word, lemma: payload.item?.lemma || '', event_kind: 'lookup', context: { sentence, sentenceIndex } })
      }).catch(() => null);
      renderLookupCard(payload, word, sentence, sentenceIndex);
      status(payload.found ? `已查词：${word}` : `未收入单词本：${word}`);
      return true;
    }
    function setVoiceLabel(text) {
      $('barVoice').textContent = text;
    }
    function hideVoiceToast(delay = 0) {
      clearTimeout(state.voiceToastTimer);
      const run = () => {
        const toast = $('voiceToast');
        if (!toast) return;
        toast.className = '';
        toast.innerHTML = '';
        updatePaginationStatus();
      };
      if (delay > 0) state.voiceToastTimer = window.setTimeout(run, delay);
      else run();
    }
    function showVoiceToast(title, body = '', mode = 'info', options = {}) {
      const toast = $('voiceToast');
      if (!toast) return;
      clearTimeout(state.voiceToastTimer);
      const stop = options.stop ? '<div class="voice-toast-actions"><button id="voiceToastStop" type="button">停止并保存</button></div>' : '';
      toast.className = `show ${mode}`;
      toast.innerHTML = `<div class="voice-toast-head"><span class="voice-dot"></span><strong>${esc(title)}</strong></div>${body ? `<div class="voice-toast-body">${esc(body)}</div>` : ''}${stop}`;
      if (options.stop && $('voiceToastStop')) {
        $('voiceToastStop').onclick = () => {
          if (state.nativeReaderAudioRecording && nativeReaderAudioAvailable()) {
            try { ClickNativeAudio.stopRecording(); } catch (error) {}
            return;
          }
          if (state.mediaRecorder) {
            try { state.mediaRecorder.stop(); } catch (error) {}
            return;
          }
          if (state.recognition) {
            try { state.recognition.stop(); } catch (error) {}
          }
        };
      }
      if (options.autoHide) hideVoiceToast(options.autoHide);
    }
    async function copyFocusedSentence() {
      const node = state.focused;
      const text = node ? String(node.textContent || '').trim() : '';
      if (!text) return status('先点一句话');
      try {
        await navigator.clipboard.writeText(text);
        status('已复制句子');
      } catch (error) {
        status(text);
      }
    }
    function loadReaderSettings() {
      let settings = {};
      try { settings = JSON.parse(localStorage.getItem('sentenceReaderLanSettings') || '{}'); } catch (error) { settings = {}; }
      const fontSize = Number(settings.fontSize || 20);
      const lineHeight = Number(settings.lineHeight || 1.62);
      const sidePadding = Number(settings.sidePadding || 14);
      $('fontSize').value = String(fontSize);
      $('lineHeight').value = String(lineHeight);
      $('sidePadding').value = String(sidePadding);
      applyReaderSettings({ fontSize, lineHeight, sidePadding }, false);
    }
    function currentReaderSettings() {
      return {
        fontSize: Number($('fontSize').value || 20),
        lineHeight: Number($('lineHeight').value || 1.62),
        sidePadding: Number($('sidePadding').value || 14)
      };
    }
    function applyReaderSettings(settings = currentReaderSettings(), persist = true) {
      document.documentElement.style.setProperty('--reader-font-size', `${settings.fontSize}px`);
      document.documentElement.style.setProperty('--reader-line-height', String(settings.lineHeight));
      document.documentElement.style.setProperty('--reader-side-pad', `${settings.sidePadding}px`);
      document.body.classList.toggle('reader-large-font', Number(settings.fontSize) >= 24);
      if (persist) localStorage.setItem('sentenceReaderLanSettings', JSON.stringify(settings));
      requestAnimationFrame(() => layoutPages(pageRatio()));
    }
    function openSettingsSheet() {
      $('settingsSheet').classList.add('show');
      $('settingsSheet').setAttribute('aria-hidden', 'false');
      hideSentenceBar();
    }
    function closeSettingsSheet() {
      $('settingsSheet').classList.remove('show');
      $('settingsSheet').setAttribute('aria-hidden', 'true');
    }
    function goLibraryHome() {
      const bookParam = state.book && state.book.id ? `?book_id=${encodeURIComponent(state.book.id)}` : '';
      window.location.href = `/library${bookParam}`;
    }
    function goVocabHome() {
      const bookParam = state.book && state.book.id ? `?book_id=${encodeURIComponent(state.book.id)}` : '';
      window.location.href = `/vocab${bookParam}`;
    }
    function clearLongPressTimer() {
      if (state.longPressTimer) {
        clearTimeout(state.longPressTimer);
        state.longPressTimer = 0;
      }
    }
    function showReaderLoadError(message) {
      $('drawerTitle').textContent = '目录';
      $('chapters').innerHTML = '';
      $('reader').innerHTML = `<div style="padding:22px;line-height:1.65;color:#ddd"><strong>${esc(message)}</strong><br><button onclick="location.href='/library'">回到书库</button></div>`;
      status(message);
    }
    async function loadBooks() {
      if (!initialBookID) {
        showReaderLoadError('请从书库打开一本书');
        return;
      }
      await loadBook(initialBookID);
    }
    async function loadBook(bookID) {
      state.book = state.books.find((book) => book.id === bookID) || { id: bookID };
      state.manifest = await json(`/lan/books/${bookID}/manifest`);
      state.book = state.manifest.book || state.book;
      $('drawerTitle').textContent = state.book.title || '目录';
      state.annotations = await json(`/books/${bookID}/annotations`);
      const saved = state.manifest.position;
      const savedIndex = saved && saved.locator && Number.isInteger(saved.locator.chapterIndex) ? saved.locator.chapterIndex : state.manifest.chapters.findIndex((c) => c.locator === (saved || {}).chapter_locator);
      renderChapters();
      await loadChapter(savedIndex >= 0 ? savedIndex : 0, saved ? Number(saved.page_ratio || 0) : 0);
    }
    function renderChapters() {
      const chapterCount = Array.isArray(state.manifest.chapters) ? state.manifest.chapters.length : 0;
      const tocItems = Array.isArray(state.manifest.toc) && state.manifest.toc.length
        ? state.manifest.toc
        : state.manifest.chapters.map((chapter) => ({ title: chapter.title || chapter.locator, chapter_index: chapter.index, level: 0 }));
      const currentBookTocItems = tocItems.filter((entry) => {
        const chapterIndex = Number(entry.chapter_index);
        return Number.isInteger(chapterIndex) && chapterIndex >= 0 && chapterIndex < chapterCount;
      });
      $('chapters').innerHTML = currentBookTocItems.map((entry) => {
        const level = Math.max(0, Math.min(6, Number(entry.level || 0)));
        const chapterIndex = Number(entry.chapter_index);
        const title = entry.title || (state.manifest.chapters[chapterIndex] || {}).title || (state.manifest.chapters[chapterIndex] || {}).locator || '';
        return `<button class="row toc-row" data-level="${level}" style="--toc-indent:${8 + level * 18}px" data-chapter="${chapterIndex}">${esc(title)}</button>`;
      }).join('');
      $('chapters').querySelectorAll('button').forEach((button) => button.onclick = () => {
        closeDrawer();
        loadChapter(Number(button.dataset.chapter), 0);
      });
    }
    async function loadChapter(index, restoreRatio = null) {
      closeNoteEditor();
      state.chapterIndex = Math.max(0, Math.min(index, state.manifest.chapters.length - 1));
      const chapter = await json(`/lan/books/${state.book.id}/chapters/${state.chapterIndex}`);
      $('reader').innerHTML = chapter.html;
      wrapSentences($('reader'));
      applyAnnotations();
      document.querySelectorAll('#chapters .row').forEach((button) => button.classList.toggle('active', Number(button.dataset.chapter) === state.chapterIndex));
      state.focused = null;
      state.pageIndex = 0;
      requestAnimationFrame(() => {
        layoutPages(restoreRatio);
        document.querySelectorAll('#reader img, #reader svg').forEach((node) => {
          node.addEventListener('load', () => layoutPages(pageRatio()), { once: true });
        });
      });
    }
    async function refreshAnnotations() {
      state.annotations = await json(`/books/${state.book.id}/annotations`);
      applyAnnotations();
    }
    async function toggleRed() {
      const node = state.focused;
      if (!node || !state.book || !state.manifest) return status('先点一句话');
      const index = node.dataset.srIndex || '';
      const existing = state.redIDs.get(index);
      if (existing) {
        await fetch(`/annotations/${existing}`, { method: 'DELETE' });
      } else {
        const chapter = state.manifest.chapters[state.chapterIndex];
        await json('/annotations', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            book_id: state.book.id,
            kind: 'red_highlight',
            source_text: node.textContent || '',
            color: 'red',
            chapter_title: chapter.title,
            chapter_locator: chapter.locator,
            range_locator: { chapterLocator: chapter.locator, sentenceIndex: index },
            metadata: { source: 'SentenceReaderLAN', sentenceIndex: index }
          })
        });
      }
      await refreshAnnotations();
      status(existing ? '已取消红标' : '已标红');
      savePositionSoon();
    }
    async function saveNoteText(note) {
      const node = state.focused;
      if (!node || !state.book || !state.manifest) { status('先点一句话'); return null; }
      const index = node.dataset.srIndex || '';
      const existing = state.noteByIndex.get(index);
      if (note === null) return null;
      note = normalizeNoteText(note);
      const chapter = state.manifest.chapters[state.chapterIndex];
      let saved = null;
      if (existing) {
        saved = await json(`/annotations/${existing.id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ note_text: note }) });
      } else {
        saved = await json('/annotations', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            book_id: state.book.id,
            kind: 'note',
            source_text: node.textContent || '',
            note_text: note,
            chapter_title: chapter.title,
            chapter_locator: chapter.locator,
            range_locator: { chapterLocator: chapter.locator, sentenceIndex: index },
            metadata: { source: 'SentenceReaderLAN', sentenceIndex: index }
          })
        });
      }
      await refreshAnnotations();
      status('备注已保存');
      savePositionSoon();
      return saved;
    }
    async function addNote() {
      const node = state.focused;
      if (!node || !state.book || !state.manifest) return status('先点一句话');
      openNoteEditor();
    }
    function preferredAudioMimeType() {
      if (!window.MediaRecorder) return '';
      const candidates = ['audio/mp4', 'audio/webm;codecs=opus', 'audio/webm', 'audio/wav'];
      return candidates.find((type) => MediaRecorder.isTypeSupported(type)) || '';
    }
    function blobToBase64(blob) {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || '').split(',').pop() || '');
        reader.onerror = () => reject(reader.error || new Error('音频读取失败'));
        reader.readAsDataURL(blob);
      });
    }
    async function transcribeVoiceBlob(blob, durationSeconds = null) {
      if (!state.book) throw new Error('没有当前书籍');
      status('语音处理中');
      showVoiceToast('正在保存语音', noteEditorOpen() ? '录音会先保存，后台转写完成后自动补到备注框。' : '录音会先保存到当前句子，后台再补文字。', 'processing');
      const audioBase64 = await blobToBase64(blob);
      return json('/lan/audio-notes/transcribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          book_id: state.book.id,
          audio_base64: audioBase64,
          mime_type: blob.type || 'application/octet-stream',
          duration_seconds: durationSeconds
        })
      });
    }
    function stopAudioPoll(audioNoteID) {
      const timer = state.pendingAudioPolls.get(audioNoteID);
      if (timer) window.clearInterval(timer);
      state.pendingAudioPolls.delete(audioNoteID);
    }
    function updatePendingEditorFailure() {
      const textarea = $('noteEditorText');
      const current = String(textarea.value || '').trim();
      if (!current || current === voiceNotePendingText) textarea.value = voiceNoteFailedText;
      else if (current.includes(voiceNotePendingText)) textarea.value = normalizeNoteText(current.split(voiceNotePendingText).join(voiceNoteFailedText));
      setNoteEditorStatus('录音已保存，但后台转写失败，可稍后重试。');
    }
    async function pollAudioNote(audioNoteID) {
      if (!audioNoteID || state.pendingAudioPolls.has(audioNoteID)) return;
      let attempts = 0;
      const tick = async () => {
        attempts += 1;
        try {
          const note = await json(`/audio-notes/${audioNoteID}`);
          if (note.status === 'pending') {
            if (attempts >= 90) {
              stopAudioPoll(audioNoteID);
              setNoteEditorStatus('语音仍在后台处理，稍后打开备注会看到结果。');
            }
            return;
          }
          stopAudioPoll(audioNoteID);
          if (note.status === 'transcribed' && note.transcript) {
            if (noteEditorOpen() && state.noteEditorAudioNoteID === audioNoteID) replacePendingNoteText(note.transcript);
            await refreshAnnotations();
            status('语音转写完成');
            showVoiceToast('语音转写完成', '文字已补到备注里。', 'success', { autoHide: 2600 });
            return;
          }
          if (note.status === 'failed') {
            if (noteEditorOpen() && state.noteEditorAudioNoteID === audioNoteID) updatePendingEditorFailure();
            await refreshAnnotations();
            status('语音转写失败');
            showVoiceToast('录音已保存，转写失败', note.error_message || '可以稍后重试。', 'error', { autoHide: 3600 });
          }
        } catch (error) {
          if (attempts >= 5) {
            stopAudioPoll(audioNoteID);
            setNoteEditorStatus('暂时查不到语音状态，录音已在 Mac 端保存。');
          }
        }
      };
      const timer = window.setInterval(tick, 2000);
      state.pendingAudioPolls.set(audioNoteID, timer);
      tick();
    }
    async function applyVoiceTranscript(payload) {
      const audioNoteID = payload.audio_note_id || payload.id;
      const isPending = payload.accepted || payload.async_processing || payload.status === 'pending';
      if (isPending) {
        const pendingText = payload.pending_text || voiceNotePendingText;
        if (noteEditorOpen()) {
          addPendingNoteText();
          state.noteEditorAudioNoteID = audioNoteID || state.noteEditorAudioNoteID;
          const annotation = await ensureNoteEditorAnnotation();
          await linkAudioNoteToAnnotation(audioNoteID, annotation);
        } else {
          const annotation = await saveNoteText(pendingText);
          await linkAudioNoteToAnnotation(audioNoteID, annotation);
        }
        pollAudioNote(audioNoteID);
        status('语音已保存，后台转写中');
        showVoiceToast('语音已保存', '后台转写完成后会自动补到备注。', 'processing', { autoHide: 2600 });
        return;
      }
      const transcript = String(payload.transcript || payload.text || '').trim();
      if (!transcript) {
        const message = payload.error_message || payload.error || '没有识别出文字。';
        status('语音失败');
        showVoiceToast('语音没有保存', message, 'error', { autoHide: 3600 });
        return;
      }
      if (noteEditorOpen()) {
        if (String($('noteEditorText').value || '').includes(voiceNotePendingText)) replacePendingNoteText(transcript);
        else appendNoteEditorText(transcript);
        state.noteEditorAudioNoteID = audioNoteID || state.noteEditorAudioNoteID;
        status('语音已转写');
        showVoiceToast('已转写到备注框', transcript, 'success', { autoHide: 2800 });
        return;
      }
      const annotation = await saveNoteText(transcript);
      await linkAudioNoteToAnnotation(audioNoteID, annotation);
      status('语音已保存');
      showVoiceToast('语音备注已保存', transcript, 'success', { autoHide: 2800 });
    }
    function openAudioCaptureFallback() {
      const input = $('audioFile');
      if (!input) return false;
      try {
        input.value = '';
        status('系统录音');
        showVoiceToast('系统录音已打开', noteEditorOpen() ? '录完后点确认，Mac 会转写并插入备注框。' : '录完后点确认，Mac 会转写并保存到当前句子。', 'info', { autoHide: 4200 });
        input.click();
        return true;
      } catch (error) {
        return false;
      }
    }
    function nativeReaderAudioAvailable() {
      try {
        return Boolean(window.ClickNativeAudio && ClickNativeAudio.isAvailable && ClickNativeAudio.isAvailable() && ClickNativeAudio.startReaderNote && ClickNativeAudio.stopRecording);
      } catch (error) {
        return false;
      }
    }
    function nativeReaderAudioPayload(rawPayload) {
      if (!rawPayload) return {};
      if (rawPayload.response_json) return rawPayload.response_json;
      if (rawPayload.response_text) {
        try { return JSON.parse(rawPayload.response_text); } catch (error) {}
      }
      return rawPayload;
    }
    window.__clickNativeReaderAudioDidStart = () => {
      state.nativeReaderAudioRecording = true;
      setVoiceLabel('停止');
      status('录音中');
      showVoiceToast('正在录音', noteEditorOpen() ? '再次点“语音”或点“停止并保存”，录音会先保存。' : '再次点“语音”或点“停止并保存”结束录音。', 'recording', { stop: true });
    };
    window.__clickNativeReaderAudioDidStop = () => {
      state.nativeReaderAudioRecording = false;
      setVoiceLabel('语音');
      status('语音处理中');
      showVoiceToast('正在保存语音', noteEditorOpen() ? '录音先保存，后台转写完成后自动补到备注。' : '录音先保存到当前句子，后台再补文字。', 'processing');
    };
    window.__clickNativeReaderAudioDidUpload = async (rawPayload) => {
      state.nativeReaderAudioRecording = false;
      setVoiceLabel('语音');
      try {
        await applyVoiceTranscript(nativeReaderAudioPayload(rawPayload));
      } catch (error) {
        status('语音失败');
        showVoiceToast('语音转写失败', String(error.message || error), 'error', { autoHide: 3600 });
      }
    };
    window.__clickNativeReaderAudioDidError = (payload) => {
      state.nativeReaderAudioRecording = false;
      setVoiceLabel('语音');
      status('语音失败');
      showVoiceToast('语音失败', String((payload && payload.error) || '安卓原生录音不可用'), 'error', { autoHide: 3600 });
    };
    function prefersSystemAudioCapture() {
      const ua = navigator.userAgent || '';
      return /Android|iPhone|iPad|Mobile/i.test(ua) || window.innerWidth <= 1024;
    }
    async function startMediaVoiceNote() {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mimeType = preferredAudioMimeType();
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
      state.voiceChunks = [];
      state.voiceStartedAt = Date.now();
      state.voiceStream = stream;
      recorder.ondataavailable = (event) => {
        if (event.data && event.data.size > 0) state.voiceChunks.push(event.data);
      };
      recorder.onstop = async () => {
        const chunks = state.voiceChunks.slice();
        const activeStream = state.voiceStream;
        state.mediaRecorder = null;
        state.voiceStream = null;
        state.voiceChunks = [];
        if (activeStream) activeStream.getTracks().forEach((track) => track.stop());
        setVoiceLabel('语音');
        if (!chunks.length) {
          status('没有录音');
          showVoiceToast('没有录到音频', '请重新点语音录一次。', 'error', { autoHide: 2600 });
          return;
        }
        const type = recorder.mimeType || mimeType || chunks[0].type || 'audio/webm';
        const duration = Math.max(0.1, (Date.now() - state.voiceStartedAt) / 1000);
        try {
          const payload = await transcribeVoiceBlob(new Blob(chunks, { type }), duration);
          await applyVoiceTranscript(payload);
        } catch (error) {
          status('语音失败');
          showVoiceToast('语音转写失败', String(error.message || error), 'error', { autoHide: 3600 });
        }
      };
      recorder.onerror = () => {
        status('录音失败');
        showVoiceToast('录音失败', '可以改用系统录音或手动备注。', 'error', { autoHide: 3000 });
      };
      state.mediaRecorder = recorder;
      recorder.start();
      setVoiceLabel('停止');
      status('录音中');
      showVoiceToast('正在录音', noteEditorOpen() ? '点“停止并保存”，录音会先保存。' : '点“停止并保存”结束录音。', 'recording', { stop: true });
    }
    function startBrowserSpeechNote() {
      const node = state.focused;
      if (!node) {
        status('先点一句话');
        showVoiceToast('先点一句话', '选中句子后再点语音。', 'error', { autoHide: 2600 });
        return;
      }
      const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!Recognition) {
        status('语音不可用');
        showVoiceToast('语音不可用', '当前浏览器不支持语音识别，已切到手动备注。', 'error', { autoHide: 3200 });
        addNote();
        return;
      }
      if (state.recognition) {
        try { state.recognition.stop(); } catch (error) {}
        state.recognition = null;
        status('语音已停止');
        showVoiceToast('语音已停止', '正在保存识别结果。', 'processing', { autoHide: 1800 });
        return;
      }
      const recognition = new Recognition();
      recognition.lang = 'zh-CN';
      recognition.interimResults = false;
      recognition.continuous = false;
      state.recognition = recognition;
      setVoiceLabel('停止');
      status('听写中');
      showVoiceToast('正在听写', noteEditorOpen() ? '说完后会先写入备注框。' : '说完后会保存到当前句子。', 'recording', { stop: true });
      recognition.onresult = (event) => {
        const transcript = Array.from(event.results || []).map((result) => result[0] && result[0].transcript ? result[0].transcript : '').join('').trim();
        if (transcript) {
          applyVoiceTranscript({ transcript }).catch((error) => {
            status('语音失败');
            showVoiceToast('语音保存失败', String(error.message || error), 'error', { autoHide: 3200 });
          });
        } else {
          status('没有识别');
          showVoiceToast('没有识别到文字', '请重新录一次。', 'error', { autoHide: 2600 });
        }
      };
      recognition.onerror = () => {
        status('语音失败');
        showVoiceToast('语音识别失败', '可以改用系统录音或手动备注。', 'error', { autoHide: 3200 });
      };
      recognition.onend = () => {
        state.recognition = null;
        setVoiceLabel('语音');
        hideVoiceToast(2200);
      };
      try {
        recognition.start();
      } catch (error) {
        state.recognition = null;
        setVoiceLabel('语音');
        status('语音无法启动');
        showVoiceToast('语音无法启动', '可以改用系统录音或手动备注。', 'error', { autoHide: 3200 });
      }
    }
    function startVoiceNote() {
      const node = state.focused;
      if (!node) {
        status('先点一句话');
        showVoiceToast('先点一句话', '选中句子后再点语音。', 'error', { autoHide: 2600 });
        return;
      }
      if (state.nativeReaderAudioRecording) {
        if (nativeReaderAudioAvailable()) {
          try { ClickNativeAudio.stopRecording(); } catch (error) { window.__clickNativeReaderAudioDidError({ error: '原生录音停止失败' }); }
        }
        return;
      }
      if (state.mediaRecorder) {
        try { state.mediaRecorder.stop(); } catch (error) {}
        return;
      }
      if (state.recognition) {
        try { state.recognition.stop(); } catch (error) {}
        return;
      }
      if (nativeReaderAudioAvailable()) {
        try {
          ClickNativeAudio.startReaderNote(state.book.id);
        } catch (error) {
          window.__clickNativeReaderAudioDidError({ error: '原生录音启动失败' });
        }
        return;
      }
      if (navigator.mediaDevices && window.MediaRecorder) {
        startMediaVoiceNote().catch((error) => {
          status(`浏览器录音不可用：${error.message || error}`);
          startBrowserSpeechNote();
        });
        return;
      }
      const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (Recognition) {
        startBrowserSpeechNote();
        return;
      }
      status('语音不可用');
      showVoiceToast('当前环境不能直接录音', '请用新版安卓 App，或在 Mac 原生阅读器里使用语音备注。不会再自动打开文件夹。', 'error', { autoHide: 5200 });
    }
    function savePositionSoon() {
      clearTimeout(state.saveTimer);
      state.saveTimer = setTimeout(savePosition, 500);
    }
    async function savePosition() {
      if (!state.book || !state.manifest) return;
      const chapter = state.manifest.chapters[state.chapterIndex];
      try {
        await json(`/books/${state.book.id}/position`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ chapter_locator: chapter.locator, page_index: state.pageIndex, total_pages: state.totalPages, page_ratio: pageRatio(), locator: { source: 'lan_reader_paginated', chapterIndex: state.chapterIndex, chapterLocator: chapter.locator, pageIndex: state.pageIndex, totalPages: state.totalPages } })
        });
      } catch (error) {}
    }
    $('reader').addEventListener('click', (event) => {
      if (noteEditorOpen()) return;
      if (shouldLetSystemHandle(event)) return;
      const node = event.target.closest && event.target.closest('.sr-sentence');
      if (node) {
        focusSentence(node);
        clearTimeout(state.sentenceTapTimer);
        const word = lookupWordFromEvent(event);
        if (word) {
          state.sentenceTapTimer = setTimeout(() => {
            hideNoteToast();
            showLookup(word, node.textContent || '', node.dataset.srIndex || '').catch((error) => status(`查词失败：${error.message}`));
          }, 180);
        }
        return;
      }
      clearTimeout(state.sentenceTapTimer);
      clearSentenceFocus();
    });
    $('reader').addEventListener('dblclick', (event) => {
      if (noteEditorOpen()) return;
      if (shouldLetSystemHandle(event, { respectSelection: false })) return;
      const node = event.target.closest && event.target.closest('.sr-sentence');
      if (!node) return;
      clearTimeout(state.sentenceTapTimer);
      claimSentenceEvent(event);
      focusSentence(node);
      const word = event.altKey ? lookupWordFromEvent(event) : '';
      if (word) {
        showLookup(word, node.textContent || '', node.dataset.srIndex || '').catch((error) => status(`查词失败：${error.message}`));
        return;
      }
      addNote();
    });
    $('reader').addEventListener('contextmenu', (event) => {
      if (noteEditorOpen()) return;
      if (shouldLetSystemHandleContext(event)) return;
      const node = event.target.closest && event.target.closest('.sr-sentence');
      if (!node) return;
      claimSentenceEvent(event);
      focusSentence(node);
      toggleRed().catch((error) => status(`红标失败：${error.message}`));
    });
    $('libraryHome').onclick = goLibraryHome;
    $('vocabHome').onclick = goVocabHome;
    $('tocToggle').onclick = openDrawer;
    $('fontSettings').onclick = openSettingsSheet;
    $('closeSettings').onclick = closeSettingsSheet;
    $('closeDrawer').onclick = closeDrawer;
    $('scrim').onclick = closeDrawer;
    $('barRed').onclick = toggleRed;
    $('barNote').onclick = addNote;
    $('barVoice').onclick = () => {
      if (!state.focused) {
        startVoiceNote();
        return;
      }
      if (!noteEditorOpen()) openNoteEditor({ focusText: false });
      startVoiceNote();
    };
    $('barCopy').onclick = copyFocusedSentence;
    $('barCancel').onclick = clearSentenceFocus;
    $('noteEditorClose').onclick = closeNoteEditor;
    $('noteEditorCancel').onclick = closeNoteEditor;
    $('noteEditorVoice').onclick = startVoiceNote;
    $('noteEditorClean').onclick = () => cleanNoteEditorText().catch((error) => {
      setNoteEditorStatus(`整理失败：${error.message || error}`);
      status('备注整理失败');
    });
    $('noteEditorSave').onclick = () => saveNoteEditor().catch((error) => {
      setNoteEditorStatus(`保存失败：${error.message || error}`);
      status('备注保存失败');
    });
    $('noteEditorText').addEventListener('keydown', (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
        event.preventDefault();
        saveNoteEditor().catch((error) => {
          setNoteEditorStatus(`保存失败：${error.message || error}`);
          status('备注保存失败');
        });
      }
    });
    const prevButton = $('prev');
    const nextButton = $('next');
    if (prevButton) prevButton.onclick = () => turnPage(-1);
    if (nextButton) nextButton.onclick = () => turnPage(1);
    ['fontSize', 'lineHeight', 'sidePadding'].forEach((id) => {
      $(id).addEventListener('input', () => applyReaderSettings());
    });
    $('readerWrap').addEventListener('touchstart', (event) => {
      if (noteEditorOpen()) return;
      if (shouldLetSystemHandle(event)) return;
      const touch = event.changedTouches && event.changedTouches[0];
      if (!touch) return;
      state.touchStartX = touch.clientX;
      state.touchStartY = touch.clientY;
      state.touchStartTime = Date.now();
      state.touchSentence = event.target.closest && event.target.closest('.sr-sentence');
      state.longPressTriggered = false;
      clearLongPressTimer();
      if (state.touchSentence) {
        state.longPressTimer = window.setTimeout(async () => {
          state.longPressTriggered = true;
          focusSentence(state.touchSentence);
          await toggleRed();
        }, 560);
      }
    }, { passive: true });
    $('readerWrap').addEventListener('touchmove', (event) => {
      if (noteEditorOpen()) return;
      const touch = event.changedTouches && event.changedTouches[0];
      if (!touch) return;
      const deltaX = touch.clientX - state.touchStartX;
      const deltaY = touch.clientY - state.touchStartY;
      if (Math.abs(deltaX) > 12 || Math.abs(deltaY) > 12) {
        clearLongPressTimer();
      }
    }, { passive: true });
    $('readerWrap').addEventListener('touchend', (event) => {
      if (noteEditorOpen()) return;
      const touch = event.changedTouches && event.changedTouches[0];
      if (!touch) return;
      clearLongPressTimer();
      if (state.longPressTriggered) {
        event.preventDefault();
        state.longPressTriggered = false;
        state.touchSentence = null;
        return;
      }
      const deltaX = touch.clientX - state.touchStartX;
      const deltaY = touch.clientY - state.touchStartY;
      const elapsed = Date.now() - state.touchStartTime;
      if (Math.abs(deltaX) > 42 && Math.abs(deltaX) > Math.abs(deltaY) * 1.2 && elapsed < 1200) {
        event.preventDefault();
        turnPage(deltaX < 0 ? 1 : -1);
      }
      state.touchSentence = null;
    }, { passive: false });
    $('readerWrap').addEventListener('wheel', (event) => {
      if (noteEditorOpen()) return;
      if (shouldLetSystemHandle(event)) return;
      const handled = handleHorizontalWheel(event);
      if (handled) {
        event.preventDefault();
        event.stopPropagation();
      }
    }, { passive: false });
    $('audioFile').addEventListener('change', async (event) => {
      const file = event.target.files && event.target.files[0];
      if (!file) return;
      try {
        showVoiceToast('正在处理语音', noteEditorOpen() ? 'Mac 正在转写并插入备注框。' : 'Mac 正在转写并保存到当前句子。', 'processing');
        const payload = await transcribeVoiceBlob(file, null);
        await applyVoiceTranscript(payload);
      } catch (error) {
        status('语音失败');
        showVoiceToast('语音转写失败', String(error.message || error), 'error', { autoHide: 3600 });
      } finally {
        event.target.value = '';
      }
    });
    window.addEventListener('keydown', (event) => {
      if (event.defaultPrevented) return;
      if (event.key === 'Escape' && noteEditorOpen()) {
        event.preventDefault();
        closeNoteEditor();
        status('已关闭备注编辑');
        return;
      }
      if (shouldLetSystemHandle(event)) return;
      const key = String(event.key || '').toLowerCase();
      if (event.key === 'ArrowLeft' || event.key === 'PageUp') {
        event.preventDefault();
        turnPage(-1);
        return;
      }
      if (event.key === 'ArrowRight' || event.key === 'PageDown' || event.key === ' ') {
        event.preventDefault();
        turnPage(1);
        return;
      }
      if (!event.metaKey && !event.ctrlKey && !event.altKey && state.focused) {
        const readerKeyboardContract = 'reader-keyboard-note-red-voice-v1';
        if (key === 'n') {
          event.preventDefault();
          addNote().catch((error) => status(`备注失败：${error.message || error}`));
          return;
        }
        if (key === 'r') {
          event.preventDefault();
          toggleRed().catch((error) => status(`红标失败：${error.message || error}`));
          return;
        }
        if (key === 'v') {
          event.preventDefault();
          startVoiceNote();
          return;
        }
        void readerKeyboardContract;
      }
      if (event.key === 'Escape') {
        event.preventDefault();
        if (noteEditorOpen()) {
          closeNoteEditor();
          status('已关闭备注编辑');
          return;
        }
        if ($('settingsSheet').classList.contains('show')) {
          closeSettingsSheet();
          return;
        }
        if (noteToastVisible()) {
          hideNoteToast();
          status('已关闭注释');
          return;
        }
        if ($('lookupCard').classList.contains('show')) {
          hideLookupCard();
          return;
        }
        if (state.focused) {
          clearSentenceFocus();
          return;
        }
        closeDrawer();
      }
    });
    window.addEventListener('resize', () => layoutPages(pageRatio()));
    window.addEventListener('beforeunload', savePosition);
    loadReaderSettings();
    loadBooks().catch((error) => status(`加载失败：${error.message}`));
  </script>
</body>
</html>"""


def reader_runtime_root() -> Path:
    return Path(__file__).resolve().parents[1]


def reader_script_path(script_name: str) -> Path:
    return reader_runtime_root() / "scripts" / script_name


def export_output_dir(payload: ExportGenerate) -> Path:
    if payload.output_dir:
        return Path(payload.output_dir).expanduser()
    return default_export_dir()


def hermes_sync_output_dir(payload: HermesSyncGenerate) -> Path:
    if payload.output_dir:
        return Path(payload.output_dir).expanduser()
    return default_hermes_sync_dir()


def cognitive_os_root(payload: HermesIngestRun) -> Path:
    if payload.cognitive_os_dir:
        return Path(payload.cognitive_os_dir).expanduser()
    return DEFAULT_HERMES_COGNITIVE_OS_DIR


def cognitive_os_root_from_value(value: Optional[str]) -> Path:
    if value:
        return Path(value).expanduser()
    return DEFAULT_HERMES_COGNITIVE_OS_DIR


def run_reader_json_script(command: list[str], *, cwd: Optional[Path] = None, timeout: int = 90) -> dict[str, Any]:
    result = subprocess.run(
        command,
        cwd=cwd or reader_runtime_root(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "reader_script_failed",
                "returncode": result.returncode,
                "command": command,
                "output": result.stdout[-4000:],
            },
        )
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        parsed = {}
    return {
        "ok": True,
        "returncode": result.returncode,
        "command": command,
        "output": result.stdout[-4000:],
        "json": parsed,
    }


def require_reader_script(script_name: str) -> Path:
    script = reader_script_path(script_name)
    if not script.exists():
        raise HTTPException(status_code=500, detail=f"missing reader runtime script: {script}")
    return script


def vocabulary_output_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "SentenceReader" / "Vocabulary"


def lifestudy_vocab_review_dir() -> Path:
    return reader_runtime_root() / "reports" / "lifestudy_vocab_review"


def lifestudy_vocab_review_pack_path() -> Path:
    return lifestudy_vocab_review_dir() / "Genesis-review-pack.json"


def lifestudy_vocab_review_template_path() -> Path:
    return lifestudy_vocab_review_dir() / "Genesis-review-overrides.template.json"


def lifestudy_vocab_review_override_path() -> Path:
    return lifestudy_vocab_review_dir() / "Genesis-review-overrides.reviewed.json"


def clean_vocab_word(value: str) -> str:
    word = str(value or "").lower().replace("’", "'").strip("'")
    if word.endswith("'s"):
        word = word[:-2]
    return re.sub(r"[^a-z']", "", word).replace("'", "")


def normalize_vocab_lookup_text(value: str) -> str:
    text = str(value or "").lower().replace("’", "'")
    text = re.sub(r"[^a-z'\-\s]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" '-")
    return text


def vocab_lookup_terms(value: str) -> list[str]:
    raw = normalize_vocab_lookup_text(value)
    compact = clean_vocab_word(value)
    terms: list[str] = []

    def add(term: str) -> None:
        term = normalize_vocab_lookup_text(term)
        if len(term) >= 2 and term not in terms:
            terms.append(term)

    add(raw)
    add(raw.replace("-", " "))
    if compact and compact not in terms:
        terms.append(compact)
    return terms


def vocab_lookup_candidates(clean_word: str) -> list[str]:
    word = clean_vocab_word(clean_word)
    candidates: list[str] = []

    def add(value: str) -> None:
        value = clean_vocab_word(value)
        if len(value) >= 2 and value not in candidates:
            candidates.append(value)

    add(word)
    if len(word) > 4 and word.endswith("ies"):
        add(word[:-3] + "y")
    if len(word) > 4 and word.endswith("ied"):
        add(word[:-3] + "y")
    if len(word) > 5 and word.endswith("ing"):
        stem = word[:-3]
        add(stem)
        add(stem + "e")
    if len(word) > 4 and word.endswith("ed"):
        stem = word[:-2]
        add(stem)
        add(stem + "e")
    if len(word) > 4 and word.endswith("es"):
        add(word[:-2])
    if len(word) > 3 and word.endswith("s"):
        add(word[:-1])
    return candidates


def vocab_limit(value: int, default: int = 300) -> int:
    try:
        raw = int(value)
    except (TypeError, ValueError):
        raw = default
    return max(1, min(raw, 1000))


def normalize_lifestudy_review_term(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def load_lifestudy_review_pack() -> dict[str, Any]:
    path = lifestudy_vocab_review_pack_path()
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Life-study review pack not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "sentence_reader.lifestudy_vocab_review_pack.v1":
        raise HTTPException(status_code=500, detail=f"unexpected review pack schema: {payload.get('schema')}")
    if payload.get("database_write_performed") is not False:
        raise HTTPException(status_code=500, detail="review pack must be a no-write report")
    return payload


def lifestudy_review_base_override_payload(review_pack: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "sentence_reader.lifestudy_vocab_review_overrides.v1",
        "source_review_pack": str(lifestudy_vocab_review_pack_path()),
        "instructions": [
            "Set decision to approve, correct, or reject.",
            "For correct, fill corrected_meaning_zh.",
            "For reject, fill note.",
            "This reviewed file is UI-managed and still requires command-line --apply before database writes.",
        ],
        "items": [
            {
                "term": item.get("term") or "",
                "current_meaning_zh": item.get("current_meaning_zh") or "",
                "decision": "pending",
                "corrected_meaning_zh": "",
                "note": "",
            }
            for item in review_pack.get("items") or []
        ],
    }


def load_lifestudy_review_overrides(review_pack: dict[str, Any], *, create_if_missing: bool = False) -> dict[str, Any]:
    path = lifestudy_vocab_review_override_path()
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
    else:
        template = lifestudy_vocab_review_template_path()
        if template.exists():
            payload = json.loads(template.read_text(encoding="utf-8"))
        else:
            payload = lifestudy_review_base_override_payload(review_pack)
        if create_if_missing:
            write_lifestudy_review_overrides(payload)
    if payload.get("schema") != "sentence_reader.lifestudy_vocab_review_overrides.v1":
        raise HTTPException(status_code=500, detail=f"unexpected override schema: {payload.get('schema')}")
    return merge_lifestudy_review_overrides(review_pack, payload)


def write_lifestudy_review_overrides(payload: dict[str, Any]) -> None:
    path = lifestudy_vocab_review_override_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def merge_lifestudy_review_overrides(review_pack: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    existing = {
        normalize_lifestudy_review_term(str(item.get("term") or "")): dict(item)
        for item in payload.get("items") or []
    }
    merged = lifestudy_review_base_override_payload(review_pack)
    for item in merged["items"]:
        term = normalize_lifestudy_review_term(str(item.get("term") or ""))
        if term in existing:
            item.update(
                {
                    "decision": str(existing[term].get("decision") or "pending"),
                    "corrected_meaning_zh": str(existing[term].get("corrected_meaning_zh") or ""),
                    "note": str(existing[term].get("note") or ""),
                }
            )
    return merged


def lifestudy_review_decision_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"pending": 0, "approve": 0, "correct": 0, "reject": 0}
    for item in items:
        decision = str(item.get("decision") or "pending").strip().lower()
        counts[decision] = counts.get(decision, 0) + 1
    return counts


def lifestudy_review_api_payload() -> dict[str, Any]:
    review_pack = load_lifestudy_review_pack()
    overrides = load_lifestudy_review_overrides(review_pack)
    override_map = {
        normalize_lifestudy_review_term(str(item.get("term") or "")): item
        for item in overrides.get("items") or []
    }
    items: list[dict[str, Any]] = []
    for item in review_pack.get("items") or []:
        term = normalize_lifestudy_review_term(str(item.get("term") or ""))
        override = override_map.get(term) or {}
        decision = str(override.get("decision") or "pending")
        corrected = str(override.get("corrected_meaning_zh") or "")
        items.append(
            {
                **item,
                "decision": decision,
                "corrected_meaning_zh": corrected,
                "review_note": str(override.get("note") or ""),
                "final_meaning_zh": corrected if decision == "correct" else item.get("current_meaning_zh"),
            }
        )
    decision_counts = lifestudy_review_decision_counts(items)
    accepted_count = decision_counts.get("approve", 0) + decision_counts.get("correct", 0)
    rejected_count = decision_counts.get("reject", 0)
    pending_count = decision_counts.get("pending", 0)
    human_reviewed_precision = accepted_count / len(items) if pending_count == 0 and items else None
    quality = review_pack.get("quality") or {}
    return {
        "schema": "sentence_reader.lifestudy_vocab_review_api.v1",
        "review_pack": str(lifestudy_vocab_review_pack_path()),
        "override_file": str(lifestudy_vocab_review_override_path()),
        "database_write_performed": False,
        "quality": quality,
        "decision_counts": decision_counts,
        "accepted_count": accepted_count,
        "rejected_count": rejected_count,
        "human_reviewed_precision": human_reviewed_precision,
        "reviewed_precision_target": 0.85,
        "can_dry_run_apply": pending_count == 0,
        "can_expand_next_volume": pending_count == 0
        and human_reviewed_precision is not None
        and human_reviewed_precision >= 0.85
        and int(quality.get("missing_book_row_count") or 0) == 0
        and int(quality.get("dictionary_pollution_count") or 0) == 0,
        "items": items,
    }


def update_lifestudy_review_decision(payload: LifeStudyVocabReviewDecision) -> dict[str, Any]:
    review_pack = load_lifestudy_review_pack()
    overrides = load_lifestudy_review_overrides(review_pack, create_if_missing=True)
    term = normalize_lifestudy_review_term(payload.term)
    decision = str(payload.decision or "").strip().lower()
    if decision not in {"pending", "approve", "correct", "reject"}:
        raise HTTPException(status_code=400, detail="decision must be pending/approve/correct/reject")
    corrected = str(payload.corrected_meaning_zh or "").strip()
    note = str(payload.note or "").strip()
    if decision == "correct" and not corrected:
        raise HTTPException(status_code=400, detail="correct decision requires corrected_meaning_zh")
    if decision == "reject" and not note:
        raise HTTPException(status_code=400, detail="reject decision requires note")
    found = False
    for item in overrides.get("items") or []:
        if normalize_lifestudy_review_term(str(item.get("term") or "")) == term:
            item["decision"] = decision
            item["corrected_meaning_zh"] = corrected if decision == "correct" else ""
            item["note"] = note
            found = True
            break
    if not found:
        raise HTTPException(status_code=404, detail=f"review term not found: {term}")
    write_lifestudy_review_overrides(overrides)
    return lifestudy_review_api_payload()


def dry_run_lifestudy_review_apply() -> dict[str, Any]:
    review_pack = load_lifestudy_review_pack()
    overrides = load_lifestudy_review_overrides(review_pack, create_if_missing=True)
    write_lifestudy_review_overrides(overrides)
    script = reader_script_path("lifestudy_context_vocab_apply_review.py")
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--review-pack",
            str(lifestudy_vocab_review_pack_path()),
            "--overrides",
            str(lifestudy_vocab_review_override_path()),
        ],
        cwd=reader_runtime_root(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    parsed: dict[str, Any] | None = None
    if proc.stdout.strip():
        try:
            parsed = json.loads(proc.stdout)
        except json.JSONDecodeError:
            parsed = None
    return {
        "schema": "sentence_reader.lifestudy_vocab_review_dry_run.v1",
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "database_write_performed": False,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "result": parsed,
    }


def normalize_glossary_term(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def glossary_lateral_sql() -> str:
    return """
    LEFT JOIN LATERAL (
      SELECT term, meaning_zh, source, confidence
      FROM reader.book_glossary g
      WHERE g.book_id = bvi.book_id
        AND lower(g.term) IN (lower(bvi.surface), lower(coalesce(bvi.lemma, '')))
        AND g.source <> 'lifestudy_rejected'
      ORDER BY
        CASE WHEN lower(g.term) = lower(bvi.surface) THEN 0 ELSE 1 END,
        CASE WHEN g.source = 'user' THEN 0 ELSE 1 END,
        g.confidence DESC,
        g.updated_at DESC
      LIMIT 1
    ) g ON true
    """


def dictionary_lateral_sql() -> str:
    return """
    LEFT JOIN LATERAL (
      SELECT term, lemma, phonetic, part_of_speech, definition_zh, source, priority
      FROM reader.dictionary_entries d
      WHERE d.language = 'en'
        AND (
          lower(d.term) = lower(bvi.surface)
          OR lower(d.term) = lower(coalesce(bvi.lemma, ''))
          OR lower(coalesce(d.lemma, '')) = lower(coalesce(bvi.lemma, ''))
        )
      ORDER BY
        CASE
          WHEN lower(d.term) = lower(bvi.surface) THEN 0
          WHEN lower(d.term) = lower(coalesce(bvi.lemma, '')) THEN 1
          ELSE 2
        END,
        d.priority ASC,
        lower(d.term) ASC
      LIMIT 1
    ) d ON true
    """


def book_lifestudy_domain_enabled(conn: Any, book_id: str) -> bool:
    row = conn.execute(
        """
        SELECT b.title, b.book_hash, string_agg(COALESCE(bf.file_path, ''), ' ') AS file_paths
        FROM reader.books b
        LEFT JOIN reader.book_files bf ON bf.book_id = b.id
        WHERE b.id = %s
        GROUP BY b.id
        """,
        (book_id,),
    ).fetchone()
    if not row:
        return False
    text = " ".join(str(row.get(key) or "") for key in ("title", "book_hash", "file_paths")).lower()
    return any(marker in text for marker in ("life-study", "life study", "lifestudy", "生命读经", "生命讀經"))


def find_domain_glossary_entry(conn: Any, book_id: str, lookup_terms: list[str], compact_word: str) -> Optional[dict[str, Any]]:
    if not lookup_terms and not compact_word:
        return None
    row = conn.execute(
        """
        SELECT *
        FROM reader.domain_glossary_entries d
        WHERE d.domain = 'lifestudy'
          AND d.language = 'en'
          AND d.status = 'active'
          AND d.quality_grade IN ('A', 'B')
          AND (
            lower(d.term) = ANY(%s::text[])
            OR lower(coalesce(d.lemma, '')) = ANY(%s::text[])
            OR regexp_replace(lower(d.term), '[^a-z]', '', 'g') = %s
            OR regexp_replace(lower(coalesce(d.lemma, '')), '[^a-z]', '', 'g') = %s
          )
        ORDER BY
          CASE WHEN lower(d.term) = ANY(%s::text[]) THEN 0 ELSE 1 END,
          CASE d.quality_grade WHEN 'A' THEN 0 ELSE 1 END,
          d.confidence DESC,
          d.occurrence_count DESC,
          d.score DESC
        LIMIT 1
        """,
        (lookup_terms, lookup_terms, compact_word, compact_word, lookup_terms),
    ).fetchone()
    if not row:
        return None
    entry = dict(row)
    raw_metadata = jsonable(entry.get("metadata") or {})
    if not isinstance(raw_metadata, dict):
        raw_metadata = {}
    part_of_speech = str(raw_metadata.get("part_of_speech") or "")
    meaning_zh = entry.get("meaning_zh") or ""
    part_of_speech_zh = str(raw_metadata.get("part_of_speech_zh") or lookup_part_of_speech_zh(part_of_speech))
    popup_speak_text_zh = str(
        raw_metadata.get("popup_speak_text_zh")
        or lookup_popup_speak_text_zh(entry.get("term") or "", part_of_speech, meaning_zh)
    )
    metadata = {
        **raw_metadata,
        "source": "reader.domain_glossary_entries",
        "domain": entry.get("domain") or "",
        "volume": entry.get("volume") or "",
        "source_title": entry.get("source_title") or "",
        "source_page": entry.get("source_page"),
        "quality_grade": entry.get("quality_grade") or "",
        "reviewable": False,
    }
    return {
        "id": "",
        "book_id": book_id,
        "surface": entry.get("term") or "",
        "lemma": entry.get("lemma") or entry.get("term") or "",
        "context_meaning_zh": meaning_zh,
        "part_of_speech": part_of_speech,
        "part_of_speech_zh": part_of_speech_zh,
        "popup_speak_text_zh": popup_speak_text_zh,
        "meaning_source": "lifestudy_domain_glossary",
        "alignment_status": "confirmed_context_meaning" if entry.get("quality_grade") == "A" else "paraphrased_context_meaning",
        "alignment_reason": f"Life-study domain glossary {entry.get('quality_grade')} grade; confidence={entry.get('confidence')}",
        "representative_sentence_en": entry.get("evidence_en") or "",
        "representative_sentence_zh": entry.get("evidence_zh") or "",
        "occurrence_count": entry.get("occurrence_count") or 0,
        "chapter_count": 0,
        "score": entry.get("score") or 0,
        "status": "candidate",
        "user_note": "",
        "metadata": metadata,
        "reviewable": False,
        "glossary": {
            "term": entry.get("term") or "",
            "meaning_zh": meaning_zh,
            "source": "lifestudy_domain_glossary",
            "confidence": entry.get("confidence"),
        },
        "dictionary": {},
        "user_vocab": {},
        "created_at": jsonable(entry.get("created_at")),
        "updated_at": jsonable(entry.get("updated_at")),
    }


def find_dictionary_entry(conn: Any, clean_word: str) -> Optional[dict[str, Any]]:
    candidates = vocab_lookup_candidates(clean_word)
    if not candidates:
        return None
    row = conn.execute(
        """
        SELECT term, lemma, phonetic, part_of_speech, definition_zh, definition_en, source, priority
        FROM reader.dictionary_entries d
        WHERE d.language = 'en'
          AND (
            lower(d.term) = ANY(%s::text[])
            OR lower(coalesce(d.lemma, '')) = ANY(%s::text[])
          )
        ORDER BY
          COALESCE(array_position(%s::text[], lower(d.term)), 999),
          COALESCE(array_position(%s::text[], lower(coalesce(d.lemma, ''))), 999),
          d.priority ASC,
          lower(d.term) ASC
        LIMIT 1
        """,
        (candidates, candidates, candidates, candidates),
    ).fetchone()
    return dict(row) if row else None


def ensure_dictionary_vocab_item(conn: Any, book_id: str, clean_word: str, dictionary: dict[str, Any]) -> str:
    surface = clean_vocab_word(clean_word)
    lemma = clean_vocab_word(dictionary.get("lemma") or dictionary.get("term") or surface)
    if not surface:
        surface = lemma
    vocab_id = stable_id("vocab", book_id, lemma, surface)
    part_of_speech = str(dictionary.get("part_of_speech") or "")
    definition_zh = str(dictionary.get("definition_zh") or "")
    part_of_speech_zh = lookup_part_of_speech_zh(part_of_speech)
    popup_speak_text_zh = lookup_popup_speak_text_zh(lemma or surface, part_of_speech, definition_zh)
    lexeme = conn.execute(
        """
        INSERT INTO reader.lexemes (
            id, lemma, surface, language, part_of_speech, phonetic, short_definition, source, created_at, updated_at
        )
        VALUES (%s, %s, %s, 'en', %s, %s, %s, %s, now(), now())
        ON CONFLICT (language, lemma, surface) DO UPDATE
        SET part_of_speech = COALESCE(EXCLUDED.part_of_speech, reader.lexemes.part_of_speech),
            phonetic = COALESCE(EXCLUDED.phonetic, reader.lexemes.phonetic),
            short_definition = COALESCE(NULLIF(EXCLUDED.short_definition, ''), reader.lexemes.short_definition),
            source = COALESCE(NULLIF(EXCLUDED.source, ''), reader.lexemes.source),
            updated_at = now()
        RETURNING id
        """,
        (
            stable_id("lex", "en", lemma, surface),
            lemma,
            surface,
            part_of_speech,
            dictionary.get("phonetic"),
            definition_zh,
            dictionary.get("source") or "dictionary",
        ),
    ).fetchone()
    lexeme_id = str((lexeme or {}).get("id") or stable_id("lex", "en", lemma, surface))
    vocab = conn.execute(
        """
        INSERT INTO reader.book_vocab_items (
            id, book_id, lexeme_id, surface, lemma, context_meaning, meaning_source,
            alignment_status, alignment_reason, representative_sentence_en, representative_sentence_zh,
            occurrence_count, chapter_count, score, status, metadata, created_at, updated_at
        )
        VALUES (
            %s, %s, %s, %s, %s, NULL, 'dictionary_fallback',
            'dictionary_fallback', %s, NULL, NULL,
            0, 0, 1, 'candidate', %s, now(), now()
        )
        ON CONFLICT (book_id, lemma, surface) DO UPDATE
        SET lexeme_id = EXCLUDED.lexeme_id,
            context_meaning = CASE
                WHEN COALESCE(reader.book_vocab_items.meaning_source, 'none') IN ('none', 'dictionary_fallback', 'online_lookup')
                THEN EXCLUDED.context_meaning
                ELSE reader.book_vocab_items.context_meaning
            END,
            meaning_source = CASE
                WHEN COALESCE(reader.book_vocab_items.meaning_source, 'none') IN ('none', 'dictionary_fallback', 'online_lookup')
                THEN 'dictionary_fallback'
                ELSE reader.book_vocab_items.meaning_source
            END,
            alignment_status = CASE
                WHEN COALESCE(reader.book_vocab_items.meaning_source, 'none') IN ('none', 'dictionary_fallback', 'online_lookup')
                  OR COALESCE(reader.book_vocab_items.alignment_status, 'unknown') = 'unknown'
                THEN 'dictionary_fallback'
                ELSE reader.book_vocab_items.alignment_status
            END,
            alignment_reason = CASE
                WHEN COALESCE(reader.book_vocab_items.meaning_source, 'none') IN ('none', 'dictionary_fallback', 'online_lookup')
                THEN EXCLUDED.alignment_reason
                ELSE COALESCE(reader.book_vocab_items.alignment_reason, EXCLUDED.alignment_reason)
            END,
            metadata = CASE
                WHEN COALESCE(reader.book_vocab_items.meaning_source, 'none') = 'online_lookup'
                THEN (
                    COALESCE(reader.book_vocab_items.metadata, '{}'::jsonb)
                    - 'online_lookup'
                    - 'provider'
                    - 'confidence'
                    - 'generated_at'
                    - 'definition_en'
                ) || EXCLUDED.metadata
                ELSE COALESCE(reader.book_vocab_items.metadata, '{}'::jsonb) || EXCLUDED.metadata
            END,
            updated_at = now()
        RETURNING id
        """,
        (
            vocab_id,
            book_id,
            lexeme_id,
            surface,
            lemma,
            f"Fallback dictionary entry from {dictionary.get('source') or 'dictionary'}",
            db.jsonb(
                {
                    "source": "lookup_dictionary_fallback",
                    "dictionary": {
                        "term": dictionary.get("term") or "",
                        "lemma": dictionary.get("lemma") or "",
                        "source": dictionary.get("source") or "",
                    },
                    "part_of_speech": part_of_speech,
                    "part_of_speech_zh": part_of_speech_zh,
                    "popup_speak_text_zh": popup_speak_text_zh,
                }
            ),
        ),
    ).fetchone()
    return str((vocab or {}).get("id") or vocab_id)


def query_online_word_meaning(clean_word: str, sentence: Optional[str]) -> Optional[dict[str, Any]]:
    if not ENABLE_HERMES_ONLINE_LOOKUP:
        return None
    word = clean_vocab_word(clean_word)
    if not word:
        return None
    context_sentence = str(sentence or "").strip()
    prompt = f"""
你是 Reader 英文查词助手。请只为用户点击的英文词生成一个适合阅读弹窗的短中文义项。
要求：
- 只返回 JSON，不要 Markdown。
- 中文义项必须短，适合单击查词弹窗。
- 如果句子上下文足够，请优先给出该句中的意思；如果不足，请给出最常见且保守的中文义。
- 不要编造生命读经专门术语；不确定时 confidence 降低。
- part_of_speech 使用英文全称，如 noun / verb / adjective / adverb / proper noun。

英文词：{word}
当前句子：{context_sentence}

JSON 字段：
{{
  "lemma": "{word}",
  "part_of_speech": "noun",
  "part_of_speech_zh": "名词",
  "meaning_zh": "中文短义",
  "definition_en": "short English definition",
  "confidence": 0.7
}}
"""
    try:
        response = call_hermes_runtime(prompt, session_id=None, timeout_seconds=90)
        if response.get("status") != "success":
            return None
        data = extract_json_object(str(response.get("reply") or "{}"))
    except Exception:
        return None
    meaning_zh = str(data.get("meaning_zh") or "").strip()
    if not meaning_zh:
        return None
    lemma = clean_vocab_word(str(data.get("lemma") or word)) or word
    part_of_speech = str(data.get("part_of_speech") or "").strip().lower()
    part_of_speech_zh = str(data.get("part_of_speech_zh") or "").strip() or lookup_part_of_speech_zh(part_of_speech)
    try:
        confidence = float(data.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.5
    return {
        "lemma": lemma,
        "part_of_speech": part_of_speech,
        "part_of_speech_zh": part_of_speech_zh,
        "meaning_zh": meaning_zh[:120],
        "definition_en": str(data.get("definition_en") or "").strip()[:240],
        "confidence": max(0.0, min(1.0, confidence)),
        "provider": "hermes_qwen_online_lookup",
    }


def ensure_online_vocab_item(
    conn: Any,
    book_id: str,
    clean_word: str,
    sentence: Optional[str],
    online_result: dict[str, Any],
) -> str:
    surface = clean_vocab_word(clean_word)
    lemma = clean_vocab_word(str(online_result.get("lemma") or surface)) or surface
    if not surface:
        surface = lemma
    meaning_zh = str(online_result.get("meaning_zh") or "").strip()
    part_of_speech = str(online_result.get("part_of_speech") or "").strip()
    part_of_speech_zh = str(online_result.get("part_of_speech_zh") or "").strip() or lookup_part_of_speech_zh(part_of_speech)
    popup_speak_text_zh = lookup_popup_speak_text_zh(lemma or surface, part_of_speech, meaning_zh)
    lexeme = conn.execute(
        """
        INSERT INTO reader.lexemes (
            id, lemma, surface, language, part_of_speech, phonetic, short_definition, source, created_at, updated_at
        )
        VALUES (%s, %s, %s, 'en', %s, NULL, %s, 'online_lookup', now(), now())
        ON CONFLICT (language, lemma, surface) DO UPDATE
        SET part_of_speech = COALESCE(NULLIF(EXCLUDED.part_of_speech, ''), reader.lexemes.part_of_speech),
            short_definition = COALESCE(NULLIF(EXCLUDED.short_definition, ''), reader.lexemes.short_definition),
            updated_at = now()
        RETURNING id
        """,
        (stable_id("lex", "en", lemma, surface), lemma, surface, part_of_speech, meaning_zh),
    ).fetchone()
    lexeme_id = str((lexeme or {}).get("id") or stable_id("lex", "en", lemma, surface))
    vocab_id = stable_id("vocab", book_id, lemma, surface)
    metadata = {
        "source": "hermes_qwen_online_lookup",
        "online_lookup": True,
        "provider": online_result.get("provider") or "hermes_qwen_online_lookup",
        "part_of_speech": part_of_speech,
        "part_of_speech_zh": part_of_speech_zh,
        "popup_speak_text_zh": popup_speak_text_zh,
        "definition_en": online_result.get("definition_en") or "",
        "confidence": online_result.get("confidence"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    row = conn.execute(
        """
        INSERT INTO reader.book_vocab_items (
            id, book_id, lexeme_id, surface, lemma, context_meaning, meaning_source,
            alignment_status, alignment_reason, representative_sentence_en, representative_sentence_zh,
            occurrence_count, chapter_count, score, status, metadata, created_at, updated_at
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, 'online_lookup',
            'online_lookup', 'Hermes/Qwen online lookup fallback; user correction can override.',
            %s, NULL, 0, 0, %s, 'candidate', %s, now(), now()
        )
        ON CONFLICT (book_id, lemma, surface) DO UPDATE
        SET lexeme_id = EXCLUDED.lexeme_id,
            context_meaning = CASE
                WHEN COALESCE(reader.book_vocab_items.meaning_source, 'none') IN ('none', 'dictionary_fallback', 'online_lookup')
                THEN EXCLUDED.context_meaning
                ELSE reader.book_vocab_items.context_meaning
            END,
            meaning_source = CASE
                WHEN COALESCE(reader.book_vocab_items.meaning_source, 'none') IN ('none', 'dictionary_fallback', 'online_lookup')
                THEN 'online_lookup'
                ELSE reader.book_vocab_items.meaning_source
            END,
            alignment_status = CASE
                WHEN COALESCE(reader.book_vocab_items.meaning_source, 'none') IN ('none', 'dictionary_fallback', 'online_lookup')
                THEN 'online_lookup'
                ELSE reader.book_vocab_items.alignment_status
            END,
            alignment_reason = CASE
                WHEN COALESCE(reader.book_vocab_items.meaning_source, 'none') IN ('none', 'dictionary_fallback', 'online_lookup')
                THEN EXCLUDED.alignment_reason
                ELSE reader.book_vocab_items.alignment_reason
            END,
            representative_sentence_en = COALESCE(NULLIF(reader.book_vocab_items.representative_sentence_en, ''), EXCLUDED.representative_sentence_en),
            metadata = COALESCE(reader.book_vocab_items.metadata, '{}'::jsonb) || EXCLUDED.metadata,
            updated_at = now()
        RETURNING id
        """,
        (
            vocab_id,
            book_id,
            lexeme_id,
            surface,
            lemma,
            meaning_zh,
            str(sentence or "").strip() or None,
            float(online_result.get("confidence") or 0.5),
            db.jsonb(metadata),
        ),
    ).fetchone()
    return str((row or {}).get("id") or vocab_id)


def ensure_manual_correction_vocab_item(
    conn: Any,
    book_id: str,
    word: str,
    meaning_zh: str,
    sentence: Optional[str],
) -> str:
    surface = clean_vocab_word(word)
    lemma = surface
    if not surface:
        raise HTTPException(status_code=400, detail="word is required")
    part_of_speech = ""
    popup_speak_text_zh = lookup_popup_speak_text_zh(surface, part_of_speech, meaning_zh)
    lexeme = conn.execute(
        """
        INSERT INTO reader.lexemes (
            id, lemma, surface, language, part_of_speech, phonetic, short_definition, source, created_at, updated_at
        )
        VALUES (%s, %s, %s, 'en', NULL, NULL, %s, 'user_glossary', now(), now())
        ON CONFLICT (language, lemma, surface) DO UPDATE
        SET short_definition = COALESCE(NULLIF(EXCLUDED.short_definition, ''), reader.lexemes.short_definition),
            source = 'user_glossary',
            updated_at = now()
        RETURNING id
        """,
        (stable_id("lex", "en", lemma, surface), lemma, surface, meaning_zh),
    ).fetchone()
    lexeme_id = str((lexeme or {}).get("id") or stable_id("lex", "en", lemma, surface))
    vocab_id = stable_id("vocab", book_id, lemma, surface)
    row = conn.execute(
        """
        INSERT INTO reader.book_vocab_items (
            id, book_id, lexeme_id, surface, lemma, context_meaning, meaning_source,
            alignment_status, alignment_reason, representative_sentence_en, representative_sentence_zh,
            occurrence_count, chapter_count, score, status, metadata, created_at, updated_at
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, 'user_glossary',
            'confirmed_context_meaning', '用户手动填写的释义，优先于在线查询和自动抽取。',
            %s, NULL, 0, 0, 1, 'candidate', %s, now(), now()
        )
        ON CONFLICT (book_id, lemma, surface) DO UPDATE
        SET lexeme_id = EXCLUDED.lexeme_id,
            context_meaning = EXCLUDED.context_meaning,
            meaning_source = 'user_glossary',
            alignment_status = 'confirmed_context_meaning',
            alignment_reason = EXCLUDED.alignment_reason,
            representative_sentence_en = COALESCE(NULLIF(EXCLUDED.representative_sentence_en, ''), reader.book_vocab_items.representative_sentence_en),
            metadata = COALESCE(reader.book_vocab_items.metadata, '{}'::jsonb) || EXCLUDED.metadata,
            updated_at = now()
        RETURNING id
        """,
        (
            vocab_id,
            book_id,
            lexeme_id,
            surface,
            lemma,
            meaning_zh,
            str(sentence or "").strip() or None,
            db.jsonb(
                {
                    "source": "manual_lookup_correction",
                    "part_of_speech": part_of_speech,
                    "part_of_speech_zh": "",
                    "popup_speak_text_zh": popup_speak_text_zh,
                    "corrected_at": datetime.now(timezone.utc).isoformat(),
                }
            ),
        ),
    ).fetchone()
    return str((row or {}).get("id") or vocab_id)


def vocab_row(row: dict[str, Any]) -> dict[str, Any]:
    glossary_meaning = row.get("glossary_meaning_zh") or ""
    raw_context_meaning = row.get("context_meaning") or ""
    dictionary_meaning = row.get("dictionary_definition_zh") or ""
    has_chinese_sentence = bool(row.get("representative_sentence_zh"))
    use_dictionary = bool(dictionary_meaning and not glossary_meaning and not raw_context_meaning and not has_chinese_sentence)
    meaning_source = row.get("meaning_source") or "none"
    if glossary_meaning:
        meaning_source = "user_glossary" if row.get("glossary_source") == "user" else "book_glossary"
    elif use_dictionary:
        meaning_source = "dictionary_fallback"
    metadata = jsonable(row.get("metadata") or {})
    if not isinstance(metadata, dict):
        metadata = {}
    part_of_speech = str(metadata.get("part_of_speech") or row.get("dictionary_part_of_speech") or "")
    context_meaning_zh = glossary_meaning or raw_context_meaning or (dictionary_meaning if use_dictionary else "")
    part_of_speech_zh = str(metadata.get("part_of_speech_zh") or lookup_part_of_speech_zh(part_of_speech))
    popup_speak_text_zh = str(
        metadata.get("popup_speak_text_zh")
        or lookup_popup_speak_text_zh(row.get("lemma") or row.get("surface") or "", part_of_speech, context_meaning_zh)
    )
    return {
        "id": row.get("id"),
        "book_id": row.get("book_id"),
        "surface": row.get("surface"),
        "lemma": row.get("lemma"),
        "context_meaning_zh": context_meaning_zh,
        "part_of_speech": part_of_speech,
        "part_of_speech_zh": part_of_speech_zh,
        "popup_speak_text_zh": popup_speak_text_zh,
        "meaning_source": meaning_source,
        "alignment_status": row.get("alignment_status") or "unknown",
        "alignment_reason": row.get("alignment_reason") or "",
        "representative_sentence_en": row.get("representative_sentence_en") or "",
        "representative_sentence_zh": row.get("representative_sentence_zh") or "",
        "occurrence_count": row.get("occurrence_count") or 0,
        "chapter_count": row.get("chapter_count") or 0,
        "score": row.get("score") or 0,
        "status": row.get("status") or "candidate",
        "user_note": row.get("user_note") or "",
        "metadata": metadata,
        "glossary": {
            "term": row.get("glossary_term") or "",
            "meaning_zh": glossary_meaning,
            "source": row.get("glossary_source") or "",
            "confidence": row.get("glossary_confidence"),
        },
        "dictionary": {
            "term": row.get("dictionary_term") or "",
            "lemma": row.get("dictionary_lemma") or "",
            "phonetic": row.get("dictionary_phonetic") or "",
            "part_of_speech": row.get("dictionary_part_of_speech") or "",
            "definition_zh": dictionary_meaning,
            "source": row.get("dictionary_source") or "",
        },
        "user_vocab": {
            "mastery_level": row.get("user_mastery_level") or 0,
            "next_review_at": jsonable(row.get("user_next_review_at")),
            "last_reviewed_at": jsonable(row.get("user_last_reviewed_at")),
            "review_count": row.get("user_review_count") or 0,
        },
        "created_at": jsonable(row.get("created_at")),
        "updated_at": jsonable(row.get("updated_at")),
    }


def build_book_vocabulary(book_id: str, payload: VocabBuildRequest) -> dict[str, Any]:
    book = book_with_latest_file(book_id)
    epub_path = epub_path_for_book(book)
    if not epub_path.exists():
        raise HTTPException(status_code=404, detail=f"EPUB file missing: {epub_path}")
    output_dir = vocabulary_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    basename = f"{safe_slug(str(book.get('title') or book_id))}-{book_id}-vocab"
    json_path = output_dir / f"{basename}.json"
    csv_path = output_dir / f"{basename}.csv"
    script = require_reader_script("sentence_reader_book_vocab.py")
    command = [
        sys.executable,
        str(script),
        str(epub_path),
        "--output-json",
        str(json_path),
        "--output-csv",
        str(csv_path),
        "--limit",
        str(vocab_limit(payload.limit, 500)),
        "--min-count",
        str(max(1, int(payload.min_count))),
        "--insert-db",
        "--book-id",
        book_id,
    ]
    result = run_reader_json_script(command, timeout=120)
    report = result.get("json") if isinstance(result.get("json"), dict) else {}
    return {
        "ok": True,
        "schema": "sentence_reader.vocab_build_api.v1",
        "book_id": book_id,
        "json_path": str(json_path),
        "csv_path": str(csv_path),
        "quality": report.get("quality", {}),
        "db": report.get("db"),
        "command": {
            "returncode": result.get("returncode"),
            "output": result.get("output"),
        },
    }


def list_book_vocabulary(
    book_id: str,
    *,
    status: Optional[str],
    alignment_status: Optional[str],
    query: Optional[str],
    limit: int,
) -> dict[str, Any]:
    book_with_latest_file(book_id)
    conditions = ["bvi.book_id = %s"]
    params: list[Any] = [book_id]
    if status and status != "all":
        conditions.append("bvi.status = %s")
        params.append(status)
    if alignment_status and alignment_status != "all":
        conditions.append("bvi.alignment_status = %s")
        params.append(alignment_status)
    if query:
        needle = f"%{query.strip().lower()}%"
        conditions.append(
            "(lower(bvi.surface) LIKE %s OR lower(bvi.lemma) LIKE %s OR lower(coalesce(g.meaning_zh, bvi.context_meaning, d.definition_zh, '')) LIKE %s)"
        )
        params.extend([needle, needle, needle])
    params.append(vocab_limit(limit))
    with db.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
              bvi.*,
              g.term AS glossary_term,
              g.meaning_zh AS glossary_meaning_zh,
              g.source AS glossary_source,
              g.confidence AS glossary_confidence,
              d.term AS dictionary_term,
              d.lemma AS dictionary_lemma,
              d.phonetic AS dictionary_phonetic,
              d.part_of_speech AS dictionary_part_of_speech,
              d.definition_zh AS dictionary_definition_zh,
              d.source AS dictionary_source,
              u.mastery_level AS user_mastery_level,
              u.next_review_at AS user_next_review_at,
              u.last_reviewed_at AS user_last_reviewed_at,
              u.review_count AS user_review_count
            FROM reader.book_vocab_items bvi
            {glossary_lateral_sql()}
            {dictionary_lateral_sql()}
            LEFT JOIN reader.user_vocab_items u
              ON COALESCE(u.lemma, '') = COALESCE(bvi.lemma, '')
             AND u.surface = bvi.surface
            WHERE {' AND '.join(conditions)}
            ORDER BY
              CASE bvi.status
                WHEN 'reviewing' THEN 0
                WHEN 'saved' THEN 1
                WHEN 'candidate' THEN 2
                WHEN 'known' THEN 3
                ELSE 4
              END,
              bvi.score DESC,
              bvi.occurrence_count DESC,
              bvi.surface ASC
            LIMIT %s
            """,
            tuple(params),
        ).fetchall()
    items = [vocab_row(dict(row)) for row in rows]
    return {
        "ok": True,
        "schema": "sentence_reader.book_vocab_list.v1",
        "book_id": book_id,
        "count": len(items),
        "items": items,
        "columns": {
            "context_meaning_zh": "精确短义项；没有确认时为空。",
            "representative_sentence_zh": "书里抽出的对应中文句；这是证据列。",
            "alignment_status": "confirmed_context_meaning / paraphrased_context_meaning / context_sentence_available / suspected_alignment_mismatch / missing_chinese_sentence",
        },
    }


def lookup_book_word(
    book_id: str,
    word: str,
    sentence_id: Optional[str],
    sentence_text: Optional[str] = None,
) -> dict[str, Any]:
    book_with_latest_file(book_id)
    clean_word = clean_vocab_word(word)
    lookup_terms = vocab_lookup_terms(word)
    normalized_lookup = normalize_vocab_lookup_text(word)
    if not clean_word and not lookup_terms:
        raise HTTPException(status_code=400, detail="word is required")
    with db.connect() as conn:
        item = conn.execute(
            f"""
            SELECT
              bvi.*,
              g.term AS glossary_term,
              g.meaning_zh AS glossary_meaning_zh,
              g.source AS glossary_source,
              g.confidence AS glossary_confidence,
              d.term AS dictionary_term,
              d.lemma AS dictionary_lemma,
              d.phonetic AS dictionary_phonetic,
              d.part_of_speech AS dictionary_part_of_speech,
              d.definition_zh AS dictionary_definition_zh,
              d.source AS dictionary_source
            FROM reader.book_vocab_items bvi
            {glossary_lateral_sql()}
            {dictionary_lateral_sql()}
            WHERE bvi.book_id = %s AND (
              lower(bvi.surface) = ANY(%s::text[])
              OR lower(coalesce(bvi.lemma, '')) = ANY(%s::text[])
              OR regexp_replace(lower(bvi.surface), '[^a-z]', '', 'g') = %s
              OR regexp_replace(lower(coalesce(bvi.lemma, '')), '[^a-z]', '', 'g') = %s
            )
              AND bvi.status <> 'ignored'
            ORDER BY bvi.score DESC, bvi.occurrence_count DESC
            LIMIT 1
            """,
            (book_id, lookup_terms, lookup_terms, clean_word, clean_word),
        ).fetchone()
        domain_item = None
        lifestudy_enabled = book_lifestudy_domain_enabled(conn, book_id)
        if lifestudy_enabled:
            domain_item = find_domain_glossary_entry(conn, book_id, lookup_terms, clean_word)
        if not item and not domain_item and len(normalized_lookup.split()) <= 1:
            dictionary = find_dictionary_entry(conn, clean_word)
            if dictionary:
                vocab_id = ensure_dictionary_vocab_item(conn, book_id, clean_word, dictionary)
                item = selected_vocab_row(conn, book_id, vocab_id)
            else:
                online = query_online_word_meaning(clean_word, sentence_text)
                if online:
                    vocab_id = ensure_online_vocab_item(conn, book_id, clean_word, sentence_text, online)
                    item = selected_vocab_row(conn, book_id, vocab_id)
        occurrence_rows = []
        item_payload = None
        if item:
            item_dict = dict(item)
            if (
                not ENABLE_HERMES_ONLINE_LOOKUP
                and str(item_dict.get("meaning_source") or "") == "online_lookup"
                and len(normalized_lookup.split()) <= 1
            ):
                dictionary = find_dictionary_entry(conn, clean_word)
                if dictionary:
                    vocab_id = ensure_dictionary_vocab_item(conn, book_id, clean_word, dictionary)
                    item = selected_vocab_row(conn, book_id, vocab_id)
                    item_dict = dict(item)
            item_payload = item_dict if "context_meaning_zh" in item_dict else vocab_row(item_dict)
            if domain_item:
                current_source = str(item_payload.get("meaning_source") or "")
                current_meaning = str(item_payload.get("context_meaning_zh") or "")
                current_alignment = str(item_payload.get("alignment_status") or "")
                should_prefer_lifestudy = (
                    not current_meaning
                    or current_source in {"none", "dictionary_fallback"}
                    or current_alignment == "dictionary_fallback"
                )
                if should_prefer_lifestudy:
                    item_payload = domain_item
                    occurrence_rows = []
                    item = None
            meaning = str(item_payload.get("context_meaning_zh") or "")
            meaning_like = f"%{meaning}%"
            if item:
                item_surface = str(item_dict.get("surface") or "")
                item_lemma = str(item_dict.get("lemma") or item_surface)
                occurrence_rows = conn.execute(
                    """
                    SELECT *
                    FROM reader.book_word_occurrences
                    WHERE book_id = %s AND (lower(surface) = lower(%s) OR lower(lemma) = lower(%s))
                    ORDER BY
                      CASE WHEN %s <> '' AND chinese_sentence LIKE %s THEN 0 ELSE 1 END,
                      CASE
                        WHEN chapter_locator LIKE '%%-day%%' THEN 0
                        WHEN chapter_locator LIKE '%%-outline%%' THEN 1
                        WHEN chapter_locator LIKE '%%front-%%' THEN 2
                        ELSE 1
                      END,
                      CASE WHEN chinese_sentence IS NULL OR chinese_sentence = '' THEN 1 ELSE 0 END,
                      chapter_locator ASC,
                      sentence_index ASC
                    LIMIT 32
                    """,
                    (book_id, item_surface, item_lemma, meaning, meaning_like),
                ).fetchall()
        elif domain_item:
            item_payload = domain_item
    occurrences = []
    seen_occurrences: set[tuple[str, str]] = set()
    for row in occurrence_rows:
        row_dict = dict(row)
        key = (str(row_dict.get("english_sentence") or ""), str(row_dict.get("chinese_sentence") or ""))
        if key in seen_occurrences:
            continue
        occurrences.append(jsonable(row_dict))
        seen_occurrences.add(key)
        if len(occurrences) >= 8:
            break
    return {
        "ok": True,
        "schema": "sentence_reader.book_lookup.v1",
        "book_id": book_id,
        "word": word,
        "normalized_word": clean_word,
        "normalized_lookup": normalized_lookup,
        "sentence_id": sentence_id,
        "found": item_payload is not None,
        "item": item_payload,
        "occurrences": occurrences,
    }


def review_plan_for_rating(rating: str, current_mastery: int) -> dict[str, Any]:
    normalized = str(rating or "").strip().lower()
    now = datetime.now(timezone.utc)
    if normalized in {"unknown", "again", "hard", "not_known"}:
        return {
            "rating": "unknown",
            "mastery_level": max(0, current_mastery - 1),
            "next_review_at": now + timedelta(days=1),
            "book_status": "reviewing",
            "event_kind": "mark_unknown",
        }
    if normalized in {"fuzzy", "blur", "unclear", "medium"}:
        return {
            "rating": "fuzzy",
            "mastery_level": max(1, min(3, current_mastery + 1)),
            "next_review_at": now + timedelta(days=3),
            "book_status": "reviewing",
            "event_kind": "mark_unknown",
        }
    if normalized in {"known", "easy", "good"}:
        new_mastery = min(5, max(1, current_mastery + 1))
        interval_days = 14 if new_mastery < 4 else 30
        return {
            "rating": "known",
            "mastery_level": new_mastery,
            "next_review_at": now + timedelta(days=interval_days),
            "book_status": "known",
            "event_kind": "mark_known",
        }
    raise HTTPException(status_code=400, detail="rating must be unknown, fuzzy, or known")


def selected_vocab_row(conn: Any, book_id: str, item_id: str) -> dict[str, Any]:
    row = conn.execute(
        f"""
        SELECT
          bvi.*,
          g.term AS glossary_term,
          g.meaning_zh AS glossary_meaning_zh,
          g.source AS glossary_source,
          g.confidence AS glossary_confidence,
          d.term AS dictionary_term,
          d.lemma AS dictionary_lemma,
          d.phonetic AS dictionary_phonetic,
          d.part_of_speech AS dictionary_part_of_speech,
          d.definition_zh AS dictionary_definition_zh,
          d.source AS dictionary_source,
          u.mastery_level AS user_mastery_level,
          u.next_review_at AS user_next_review_at,
          u.last_reviewed_at AS user_last_reviewed_at,
          u.review_count AS user_review_count
        FROM reader.book_vocab_items bvi
        {glossary_lateral_sql()}
        {dictionary_lateral_sql()}
        LEFT JOIN reader.user_vocab_items u
          ON COALESCE(u.lemma, '') = COALESCE(bvi.lemma, '')
         AND u.surface = bvi.surface
        WHERE bvi.book_id = %s AND bvi.id = %s
        """,
        (book_id, item_id),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="vocab item not found")
    return vocab_row(dict(row))


def safe_lookup_sentence_id(conn: Any, sentence_id: Optional[str]) -> Optional[str]:
    value = str(sentence_id or "").strip()
    if not value:
        return None
    row = conn.execute("SELECT 1 FROM reader.sentences WHERE id = %s", (value,)).fetchone()
    return value if row else None


def reviewed_vocab_row(conn: Any, book_id: str, item_id: str) -> dict[str, Any]:
    return selected_vocab_row(conn, book_id, item_id)


def review_book_vocabulary_item(book_id: str, item_id: str, payload: VocabReviewCreate) -> dict[str, Any]:
    book_with_latest_file(book_id)
    with db.connect() as conn:
        item = conn.execute(
            "SELECT * FROM reader.book_vocab_items WHERE book_id = %s AND id = %s",
            (book_id, item_id),
        ).fetchone()
        if not item:
            raise HTTPException(status_code=404, detail="vocab item not found")
        existing = conn.execute(
            """
            SELECT * FROM reader.user_vocab_items
            WHERE COALESCE(lemma, '') = COALESCE(%s, '') AND surface = %s
            """,
            (item.get("lemma") or "", item.get("surface") or ""),
        ).fetchone()
        current_mastery = int((existing or {}).get("mastery_level") or 0)
        plan = review_plan_for_rating(payload.rating, current_mastery)
        now = datetime.now(timezone.utc)
        user_vocab_id = (existing or {}).get("id") or new_id("uvocab")
        conn.execute(
            """
            INSERT INTO reader.user_vocab_items (
                id, lexeme_id, surface, lemma, mastery_level, next_review_at,
                last_reviewed_at, review_count, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, 1, now(), now())
            ON CONFLICT (lemma, surface) DO UPDATE
            SET lexeme_id = COALESCE(EXCLUDED.lexeme_id, reader.user_vocab_items.lexeme_id),
                mastery_level = EXCLUDED.mastery_level,
                next_review_at = EXCLUDED.next_review_at,
                last_reviewed_at = EXCLUDED.last_reviewed_at,
                review_count = reader.user_vocab_items.review_count + 1,
                updated_at = now()
            """,
            (
                user_vocab_id,
                item.get("lexeme_id"),
                item.get("surface") or "",
                item.get("lemma") or item.get("surface") or "",
                int(plan["mastery_level"]),
                plan["next_review_at"],
                now,
            ),
        )
        conn.execute(
            """
            UPDATE reader.book_vocab_items
            SET status = %s, updated_at = now()
            WHERE book_id = %s AND id = %s
            """,
            (plan["book_status"], book_id, item_id),
        )
        conn.execute(
            """
            INSERT INTO reader.lookup_events (
                id, book_id, sentence_id, surface, lemma, event_kind, context, created_at
            )
            VALUES (%s, %s, NULL, %s, %s, %s, %s, now())
            """,
            (
                new_id("lookup"),
                book_id,
                item.get("surface") or "",
                item.get("lemma"),
                plan["event_kind"],
                db.jsonb({"source": "vocab_review", "rating": plan["rating"], "item_id": item_id}),
            ),
        )
        item_row = reviewed_vocab_row(conn, book_id, item_id)
    return {
        "ok": True,
        "schema": "sentence_reader.vocab_review.v1",
        "rating": plan["rating"],
        "item": item_row,
    }


def list_book_glossary(book_id: str) -> dict[str, Any]:
    book_with_latest_file(book_id)
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, book_id, term, meaning_zh, source, confidence, created_at, updated_at
            FROM reader.book_glossary
            WHERE book_id = %s
            ORDER BY source DESC, lower(term) ASC
            """,
            (book_id,),
        ).fetchall()
    return {
        "ok": True,
        "schema": "sentence_reader.book_glossary.v1",
        "book_id": book_id,
        "count": len(rows),
        "items": [jsonable(dict(row)) for row in rows],
    }


def export_book_glossary_csv(book_id: str) -> Response:
    book = book_with_latest_file(book_id)
    payload = list_book_glossary(book_id)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["term", "meaning_zh", "source", "confidence", "updated_at"])
    for item in payload["items"]:
        writer.writerow(
            [
                item.get("term") or "",
                item.get("meaning_zh") or "",
                item.get("source") or "",
                item.get("confidence") or "",
                item.get("updated_at") or "",
            ]
        )
    filename = f"{safe_slug(str(book.get('title') or book_id))}-glossary.csv"
    return Response(
        buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{quote(filename)}"'},
    )


def local_json_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=f"expected report missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"invalid report JSON: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail=f"expected object report: {path}")
    return data


def cognitive_ops_run_dir(kind: str) -> Path:
    safe_kind = re.sub(r"[^a-z0-9_\-]+", "-", kind.lower()).strip("-") or "run"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = default_cognitive_ops_dir() / safe_kind
    base.mkdir(parents=True, exist_ok=True)
    return base / stamp


def path_is_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def markdown_block(value: Any, fallback: str = "") -> str:
    return str(value if value is not None else fallback).replace("\r", " ").strip()


def render_cognitive_review_item_markdown(report: dict[str, Any]) -> str:
    item = report.get("queue_item") if isinstance(report.get("queue_item"), dict) else {}
    draft = report.get("draft") if isinstance(report.get("draft"), dict) else {}
    candidate = draft.get("book_intake_candidate") if isinstance(draft.get("book_intake_candidate"), dict) else {}
    book = candidate.get("book") if isinstance(candidate.get("book"), dict) else {}
    note = candidate.get("note") if isinstance(candidate.get("note"), dict) else {}
    model = candidate.get("proposed_model") if isinstance(candidate.get("proposed_model"), dict) else {}
    preflight = report.get("preflight") if isinstance(report.get("preflight"), dict) else {}

    lines = [
        "# Sentence Reader Cognitive Review Item",
        "",
        f"- Generated at: {markdown_block(report.get('generated_at'))}",
        f"- Status: `{markdown_block(item.get('status'))}`",
        f"- Draft id: `{markdown_block(item.get('draft_id'))}`",
        f"- Candidate intake id: `{markdown_block(item.get('candidate_intake_id'))}`",
        f"- Draft path: `{markdown_block(item.get('draft_path'))}`",
        f"- Target path: `{markdown_block(item.get('target_path'))}`",
        f"- Quality: `{markdown_block(item.get('quality_status'))}` / `{markdown_block(item.get('quality_score'))}`",
        "",
        "## Book",
        "",
        f"- Title: {markdown_block(book.get('title'), 'Unknown')}",
        f"- Author: {markdown_block(book.get('author'), 'Unknown')}",
        "",
        "## Source Evidence",
        "",
        f"> {markdown_block(note.get('content'))}",
        "",
        "## User Interpretation",
        "",
        markdown_block(note.get("user_interpretation")),
        "",
        "## Why It Matters",
        "",
        markdown_block(note.get("why_it_matters")),
        "",
        "## Proposed Model",
        "",
        f"- Model id: `{markdown_block(model.get('id'))}`",
        f"- Name: {markdown_block(model.get('name'))}",
        f"- Solves: {markdown_block(model.get('solves'))}",
        "",
    ]
    for section, title in [
        ("judgement_steps", "Judgement Steps"),
        ("evidence_required", "Evidence Required"),
        ("misuse_risks", "Misuse Risks"),
        ("output_requirements", "Output Requirements"),
    ]:
        values = model.get(section) if isinstance(model.get(section), list) else []
        lines.extend([f"## {title}", ""])
        if values:
            lines.extend(f"- {markdown_block(value)}" for value in values)
        else:
            lines.append("- Not specified.")
        lines.append("")

    if item.get("blocking_reasons"):
        lines.extend(["## Blocking Reasons", ""])
        lines.extend(f"- {markdown_block(reason)}" for reason in item["blocking_reasons"])
        lines.append("")
    if item.get("warnings"):
        lines.extend(["## Warnings", ""])
        lines.extend(f"- {markdown_block(warning)}" for warning in item["warnings"])
        lines.append("")

    lines.extend(
        [
            "## Approval Boundary",
            "",
            "- This report is read-only.",
            "- The app may run preflight dry-runs.",
            "- Active-pack mutation still requires the explicit approved operator path.",
            "",
            "## Preflight",
            "",
            f"- Status: `{markdown_block(preflight.get('status'))}`",
            f"- Dry run: `{markdown_block(preflight.get('dry_run'))}`",
            f"- Selected count: `{markdown_block(preflight.get('selected_count'))}`",
            f"- Report: `{markdown_block(preflight.get('report_path'))}`",
            "",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def report_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return 0


def summarize_operator_report(path: Path, root: Path) -> Optional[dict[str, Any]]:
    try:
        report = local_json_file(path)
    except HTTPException:
        return None
    if report.get("schema") != "sentence_reader.active_pack_operator_report.v1":
        return None
    if str(report.get("cognitive_os_dir") or "") != str(root):
        return None
    rollback = report.get("rollback_manifest") if isinstance(report.get("rollback_manifest"), dict) else {}
    quality_gate = report.get("quality_gate") if isinstance(report.get("quality_gate"), dict) else {}
    rebuild = report.get("active_pack_rebuild") if isinstance(report.get("active_pack_rebuild"), dict) else {}
    return {
        "report_path": str(path),
        "generated_at": report.get("generated_at"),
        "status": report.get("status"),
        "dry_run": report.get("dry_run"),
        "approved": report.get("approved"),
        "selected_count": report.get("selected_count", 0),
        "selected_drafts": report.get("selected_drafts", []),
        "run_dir": report.get("run_dir"),
        "quality_gate": {
            "ok": quality_gate.get("ok"),
            "skipped": quality_gate.get("skipped"),
            "reason": quality_gate.get("reason"),
        },
        "active_pack_rebuild": {
            "ok": rebuild.get("ok"),
            "skipped": rebuild.get("skipped"),
            "reason": rebuild.get("reason"),
        },
        "rollback_manifest_path": rollback.get("rollback_manifest_path"),
        "mtime": report_mtime(path),
    }


def cognitive_operator_history(root: Path, history_limit: int) -> list[dict[str, Any]]:
    candidates: list[Path] = []
    app_support = default_cognitive_ops_dir()
    if app_support.exists():
        candidates.extend(app_support.glob("operator_*/*/active_pack_operator_report.json"))
    root_operator_runs = root / "incoming" / "sentence_reader_drafts" / "operator_runs"
    if root_operator_runs.exists():
        candidates.extend(root_operator_runs.glob("*/active_pack_operator_report.json"))

    summaries: list[dict[str, Any]] = []
    for path in sorted(set(candidates), key=report_mtime, reverse=True):
        summary = summarize_operator_report(path, root)
        if summary:
            summaries.append(summary)
        if len(summaries) >= history_limit:
            break
    return summaries


def queue_items_by_status(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        status = str(item.get("status") or "unknown")
        grouped.setdefault(status, []).append(item)
    return grouped


def render_cognitive_dashboard_markdown(report: dict[str, Any]) -> str:
    counts = report.get("counts") if isinstance(report.get("counts"), dict) else {}
    items = report.get("items") if isinstance(report.get("items"), list) else []
    history = report.get("approval_history") if isinstance(report.get("approval_history"), list) else []
    grouped = queue_items_by_status([item for item in items if isinstance(item, dict)])

    lines = [
        "# Sentence Reader Cognitive Dashboard",
        "",
        f"- Generated at: {markdown_block(report.get('generated_at'))}",
        f"- Cognitive OS: `{markdown_block(report.get('cognitive_os_dir'))}`",
        f"- Draft count: `{markdown_block(report.get('draft_count'))}`",
        f"- Ready: `{markdown_block(counts.get('ready_to_approve', 0))}`",
        f"- Needs review: `{markdown_block(counts.get('needs_review', 0))}`",
        f"- Blocked: `{markdown_block(counts.get('blocked', 0))}`",
        f"- Already promoted: `{markdown_block(counts.get('already_promoted', 0))}`",
        "",
        "## Safety Policy",
        "",
        "- Review detail is read-only.",
        "- Preflight is dry-run only.",
        "- Approval requires `APPROVE <candidate_intake_id>`.",
        "- The App should never hide rollback or quality-gate results.",
        "",
        "## Drafts",
        "",
    ]
    if not items:
        lines.extend(["No draft items found.", ""])
    for status in ["ready_to_approve", "needs_review", "blocked", "already_promoted"]:
        status_items = grouped.get(status, [])
        if not status_items:
            continue
        lines.extend([f"### {status}", ""])
        for item in status_items:
            warnings = item.get("warnings") if isinstance(item.get("warnings"), list) else []
            blocking = item.get("blocking_reasons") if isinstance(item.get("blocking_reasons"), list) else []
            lines.extend(
                [
                    f"- `{markdown_block(item.get('candidate_intake_id') or item.get('draft_id'))}` · {markdown_block(item.get('book_title'))}",
                    f"  - Quality: `{markdown_block(item.get('quality_status'))}` / `{markdown_block(item.get('quality_score'))}`",
                    f"  - Model: `{markdown_block(item.get('model_id'))}`",
                    f"  - Draft: `{markdown_block(item.get('draft_path'))}`",
                    f"  - Target: `{markdown_block(item.get('target_path'))}`",
                ]
            )
            if warnings:
                lines.append(f"  - Warnings: {', '.join(markdown_block(value) for value in warnings[:5])}")
            if blocking:
                lines.append(f"  - Blocking: {', '.join(markdown_block(value) for value in blocking[:5])}")
        lines.append("")

    lines.extend(["## Approval History", ""])
    if not history:
        lines.extend(["No approval or operator history found for this Cognitive OS root.", ""])
    for item in history:
        lines.extend(
            [
                f"- `{markdown_block(item.get('status'))}` · approved=`{markdown_block(item.get('approved'))}` · dry_run=`{markdown_block(item.get('dry_run'))}` · selected=`{markdown_block(item.get('selected_count'))}`",
                f"  - Report: `{markdown_block(item.get('report_path'))}`",
                f"  - Rebuild ok: `{markdown_block((item.get('active_pack_rebuild') or {}).get('ok'))}`",
                f"  - Quality ok: `{markdown_block((item.get('quality_gate') or {}).get('ok'))}` skipped=`{markdown_block((item.get('quality_gate') or {}).get('skipped'))}`",
                f"  - Rollback: `{markdown_block(item.get('rollback_manifest_path'))}`",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def build_cognitive_review_queue(payload: CognitiveReviewQueueRun) -> dict[str, Any]:
    root = cognitive_os_root_from_value(payload.cognitive_os_dir)
    limit = max(0, min(int(payload.limit), 500))
    run_dir = cognitive_ops_run_dir("review_queue")
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "sentence_reader_review_queue.json"
    markdown_path = run_dir / "sentence_reader_review_queue.md"
    script = require_reader_script("sentence_reader_review_queue.py")
    command = [
        sys.executable,
        str(script),
        "--cognitive-os-dir",
        str(root),
        "--report",
        str(report_path),
        "--markdown",
        str(markdown_path),
        "--limit",
        str(limit),
        "--allow-empty",
    ]
    command_result = run_reader_json_script(command, timeout=60)
    report = local_json_file(report_path)
    return {
        "ok": True,
        "schema": report.get("schema"),
        "cognitive_os_dir": str(root),
        "run_dir": str(run_dir),
        "report_path": str(report_path),
        "markdown_path": str(markdown_path),
        "draft_count": report.get("draft_count", 0),
        "counts": report.get("counts", {}),
        "items": report.get("items", []),
        "operator_rules": report.get("operator_rules", []),
        "command": command_result,
    }


def build_cognitive_dashboard(payload: CognitiveDashboardRun) -> dict[str, Any]:
    root = cognitive_os_root_from_value(payload.cognitive_os_dir)
    queue = build_cognitive_review_queue(CognitiveReviewQueueRun(cognitive_os_dir=str(root), limit=payload.limit))
    history = cognitive_operator_history(root, max(0, min(int(payload.history_limit), 100)))
    run_dir = cognitive_ops_run_dir("dashboard")
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "sentence_reader_cognitive_dashboard.json"
    markdown_path = run_dir / "sentence_reader_cognitive_dashboard.md"
    report = {
        "ok": True,
        "schema": "sentence_reader.cognitive_dashboard.v1",
        "generated_at": now_iso(),
        "cognitive_os_dir": str(root),
        "run_dir": str(run_dir),
        "draft_count": queue.get("draft_count", 0),
        "counts": queue.get("counts", {}),
        "items": queue.get("items", []),
        "queue_report_path": queue.get("report_path"),
        "queue_markdown_path": queue.get("markdown_path"),
        "approval_history": history,
        "safety_policy": {
            "approval_requires_exact_confirmation": "APPROVE <candidate_intake_id>",
            "app_can_silently_mutate_active_pack": False,
            "preflight_is_dry_run": True,
            "show_rollback_and_quality_gate": True,
        },
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(render_cognitive_dashboard_markdown(report), encoding="utf-8")
    report["report_path"] = str(report_path)
    report["markdown_path"] = str(markdown_path)
    return report


def select_cognitive_queue_item(queue: dict[str, Any], payload: CognitiveReviewItemRun) -> dict[str, Any]:
    items = queue.get("items") if isinstance(queue.get("items"), list) else []
    if not items:
        raise HTTPException(status_code=404, detail="no cognitive review items found")

    if payload.draft_path:
        target = Path(payload.draft_path).expanduser().resolve()
        for item in items:
            draft_path = item.get("draft_path")
            if draft_path and Path(str(draft_path)).expanduser().resolve() == target:
                return item
        raise HTTPException(status_code=404, detail=f"draft_path not found in queue: {payload.draft_path}")

    if payload.draft_id or payload.candidate_intake_id:
        for item in items:
            if payload.draft_id and item.get("draft_id") == payload.draft_id:
                return item
            if payload.candidate_intake_id and item.get("candidate_intake_id") == payload.candidate_intake_id:
                return item
        raise HTTPException(status_code=404, detail="requested draft_id/candidate_intake_id not found in queue")

    preferred = payload.prefer_statuses or ["ready_to_approve", "needs_review", "blocked"]
    for status in preferred:
        for item in items:
            if item.get("status") == status:
                return item
    return items[0]


def selected_operator_command(
    root: Path,
    run_dir: Path,
    *,
    draft_ids: list[str],
    candidate_intake_ids: list[str],
    draft_paths: list[str],
    allow_needs_review: bool,
    dry_run: bool = True,
    approved: bool = False,
    allow_empty: bool = True,
    overwrite: bool = False,
    skip_quality_gate: bool = False,
) -> list[str]:
    if not draft_ids and not candidate_intake_ids and not draft_paths:
        raise HTTPException(status_code=422, detail="operator run requires at least one draft_id, candidate_intake_id, or draft_path")
    script = require_reader_script("sentence_reader_active_pack_operator.py")
    command = [
        sys.executable,
        str(script),
        "--cognitive-os-dir",
        str(root),
        "--run-dir",
        str(run_dir),
    ]
    if dry_run:
        command.append("--dry-run")
    if approved:
        command.append("--approved")
    if allow_empty:
        command.append("--allow-empty")
    for draft_id in draft_ids:
        command.extend(["--draft-id", draft_id])
    for candidate_id in candidate_intake_ids:
        command.extend(["--draft-id", candidate_id])
    for draft_path in draft_paths:
        command.extend(["--draft", draft_path])
    if allow_needs_review:
        command.append("--allow-needs-review")
    if overwrite:
        command.append("--overwrite")
    if skip_quality_gate:
        command.append("--skip-quality-gate")
    return command


def run_cognitive_operator_preflight(payload: CognitiveOperatorPreflight) -> dict[str, Any]:
    root = cognitive_os_root_from_value(payload.cognitive_os_dir)
    run_dir = cognitive_ops_run_dir("operator_preflight")
    run_dir.mkdir(parents=True, exist_ok=True)
    command = selected_operator_command(
        root,
        run_dir,
        draft_ids=payload.draft_ids,
        candidate_intake_ids=payload.candidate_intake_ids,
        draft_paths=payload.draft_paths,
        allow_needs_review=payload.allow_needs_review,
    )
    command_result = run_reader_json_script(command, timeout=90)
    report_path = run_dir / "active_pack_operator_report.json"
    report = local_json_file(report_path)
    return {
        "ok": True,
        "schema": report.get("schema"),
        "status": report.get("status"),
        "dry_run": report.get("dry_run") is True,
        "approved": report.get("approved") is True,
        "cognitive_os_dir": str(root),
        "run_dir": str(run_dir),
        "report_path": str(report_path),
        "queue_counts": report.get("queue_counts", {}),
        "selected_count": report.get("selected_count", 0),
        "selected_drafts": report.get("selected_drafts", []),
        "preflight": report.get("preflight", {}),
        "preflight_report": report.get("preflight_report", {}),
        "command": command_result,
    }


def approval_confirmation_phrase(candidate_intake_id: str) -> str:
    return f"APPROVE {candidate_intake_id}"


def run_cognitive_operator_approve(payload: CognitiveOperatorApprove) -> dict[str, Any]:
    candidate_id = str(payload.candidate_intake_id or "").strip()
    if not candidate_id:
        raise HTTPException(status_code=422, detail="candidate_intake_id is required")
    expected_confirmation = approval_confirmation_phrase(candidate_id)
    if payload.confirmation_text.strip() != expected_confirmation:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "confirmation_mismatch",
                "expected": expected_confirmation,
                "received": payload.confirmation_text,
            },
        )
    if payload.skip_quality_gate and not str(payload.skip_quality_gate_reason or "").strip():
        raise HTTPException(status_code=422, detail="skip_quality_gate_reason is required when skip_quality_gate=true")

    root = cognitive_os_root_from_value(payload.cognitive_os_dir)
    queue = build_cognitive_review_queue(CognitiveReviewQueueRun(cognitive_os_dir=str(root), limit=500))
    item = select_cognitive_queue_item(
        queue,
        CognitiveReviewItemRun(cognitive_os_dir=str(root), candidate_intake_id=candidate_id),
    )
    status = str(item.get("status") or "")
    if status == "blocked":
        raise HTTPException(status_code=409, detail={"error": "draft_blocked", "item": item})
    if status == "already_promoted" and not payload.overwrite:
        raise HTTPException(status_code=409, detail={"error": "draft_already_promoted", "item": item})
    if status != "ready_to_approve" and not payload.allow_needs_review:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "draft_not_ready_to_approve",
                "status": status,
                "requires_allow_needs_review": status == "needs_review",
                "item": item,
            },
        )

    preflight = run_cognitive_operator_preflight(
        CognitiveOperatorPreflight(
            cognitive_os_dir=str(root),
            candidate_intake_ids=[candidate_id],
            allow_needs_review=payload.allow_needs_review,
        )
    )
    if preflight.get("status") != "dry_run" or preflight.get("selected_count") != 1:
        raise HTTPException(status_code=409, detail={"error": "preflight_failed", "preflight": preflight})

    run_dir = cognitive_ops_run_dir("operator_approved")
    run_dir.mkdir(parents=True, exist_ok=True)
    command = selected_operator_command(
        root,
        run_dir,
        draft_ids=[],
        candidate_intake_ids=[candidate_id],
        draft_paths=[],
        allow_needs_review=payload.allow_needs_review,
        dry_run=False,
        approved=True,
        allow_empty=False,
        overwrite=payload.overwrite,
        skip_quality_gate=payload.skip_quality_gate,
    )
    command_result = run_reader_json_script(command, timeout=120)
    report_path = run_dir / "active_pack_operator_report.json"
    report = local_json_file(report_path)
    response = {
        "ok": report.get("status") == "success",
        "schema": report.get("schema"),
        "status": report.get("status"),
        "dry_run": report.get("dry_run") is True,
        "approved": report.get("approved") is True,
        "cognitive_os_dir": str(root),
        "run_dir": str(run_dir),
        "report_path": str(report_path),
        "queue_counts": report.get("queue_counts", {}),
        "selected_count": report.get("selected_count", 0),
        "selected_drafts": report.get("selected_drafts", []),
        "preflight_before_approval": preflight,
        "preflight": report.get("preflight", {}),
        "preflight_report": report.get("preflight_report", {}),
        "promotion": report.get("promotion", {}),
        "promotion_report": report.get("promotion_report", {}),
        "active_pack_rebuild": report.get("active_pack_rebuild", {}),
        "quality_gate": report.get("quality_gate", {}),
        "rollback_manifest": report.get("rollback_manifest", {}),
        "rollback_result": report.get("rollback_result", {}),
        "confirmation": {
            "expected": expected_confirmation,
            "matched": True,
        },
        "skip_quality_gate": {
            "requested": payload.skip_quality_gate,
            "reason": payload.skip_quality_gate_reason,
        },
        "command": command_result,
    }
    if response["ok"] is not True:
        raise HTTPException(status_code=500, detail=response)
    return response


def build_cognitive_review_item(payload: CognitiveReviewItemRun) -> dict[str, Any]:
    root = cognitive_os_root_from_value(payload.cognitive_os_dir)
    queue = build_cognitive_review_queue(CognitiveReviewQueueRun(cognitive_os_dir=str(root), limit=500))
    item = select_cognitive_queue_item(queue, payload)
    draft_path = Path(str(item.get("draft_path") or "")).expanduser()
    if not draft_path.exists():
        raise HTTPException(status_code=404, detail=f"draft file missing: {draft_path}")
    if not path_is_under(draft_path, root / "incoming" / "sentence_reader_drafts"):
        raise HTTPException(status_code=400, detail=f"draft path outside Sentence Reader draft directory: {draft_path}")

    draft = local_json_file(draft_path)
    preflight = run_cognitive_operator_preflight(
        CognitiveOperatorPreflight(
            cognitive_os_dir=str(root),
            draft_paths=[str(draft_path)],
            allow_needs_review=item.get("status") == "needs_review",
        )
    )

    run_dir = cognitive_ops_run_dir("review_item")
    run_dir.mkdir(parents=True, exist_ok=True)
    basename = safe_slug(str(item.get("candidate_intake_id") or item.get("draft_id") or "review-item"))
    report_path = run_dir / f"{basename}.json"
    markdown_path = run_dir / f"{basename}.md"
    report = {
        "ok": True,
        "schema": "sentence_reader.cognitive_review_item.v1",
        "generated_at": now_iso(),
        "cognitive_os_dir": str(root),
        "run_dir": str(run_dir),
        "queue_item": item,
        "draft": draft,
        "preflight": preflight,
        "approval_policy": {
            "app_can_mutate_active_pack": False,
            "requires_explicit_operator_approval": True,
            "reason": "V2.0I shows details and dry-run preflight only.",
        },
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(render_cognitive_review_item_markdown(report), encoding="utf-8")
    report["report_path"] = str(report_path)
    report["markdown_path"] = str(markdown_path)
    return report


def run_cognitive_operator_dry_run(payload: CognitiveOperatorDryRun) -> dict[str, Any]:
    root = cognitive_os_root_from_value(payload.cognitive_os_dir)
    run_dir = cognitive_ops_run_dir("operator_dry_runs")
    run_dir.mkdir(parents=True, exist_ok=True)
    script = require_reader_script("sentence_reader_active_pack_operator.py")
    command = [
        sys.executable,
        str(script),
        "--cognitive-os-dir",
        str(root),
        "--dry-run",
        "--run-dir",
        str(run_dir),
    ]
    if payload.all_ready:
        command.append("--all-ready")
    if payload.allow_empty:
        command.append("--allow-empty")
    if payload.allow_needs_review:
        command.append("--allow-needs-review")
    command_result = run_reader_json_script(command, timeout=90)
    report_path = run_dir / "active_pack_operator_report.json"
    report = local_json_file(report_path)
    return {
        "ok": True,
        "schema": report.get("schema"),
        "status": report.get("status"),
        "dry_run": report.get("dry_run") is True,
        "approved": report.get("approved") is True,
        "cognitive_os_dir": str(root),
        "run_dir": str(run_dir),
        "report_path": str(report_path),
        "queue_counts": report.get("queue_counts", {}),
        "selected_count": report.get("selected_count", 0),
        "selected_drafts": report.get("selected_drafts", []),
        "preflight": report.get("preflight", {}),
        "preflight_report": report.get("preflight_report", {}),
        "command": command_result,
    }


def sentence_reader_incoming_dir(root: Path) -> Path:
    return root / "incoming" / "sentence_reader"


def annotation_sentence_index(row: dict[str, Any]) -> str:
    range_locator = row.get("range_locator") or {}
    metadata = row.get("metadata") or {}
    raw = range_locator.get("sentenceIndex", metadata.get("sentenceIndex", ""))
    return str(raw) if raw is not None else ""


def annotation_export_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        item = jsonable(dict(row))
        item["export_index"] = index
        item["sentence_index"] = annotation_sentence_index(item)
        items.append(item)
    return items


def markdown_line(text: Any) -> str:
    return str(text or "").replace("\r", " ").replace("\n", " ").strip()


def render_markdown_export(book: dict[str, Any], annotations: list[dict[str, Any]], generated_at: str) -> str:
    lines = [
        f"# {markdown_line(book.get('title'))}",
        "",
        f"- Author: {markdown_line(book.get('author')) or 'Unknown'}",
        f"- Book hash: `{markdown_line(book.get('book_hash'))}`",
        f"- Exported at: {generated_at}",
        f"- Annotation count: {len(annotations)}",
        "",
    ]
    if not annotations:
        lines.extend(["## Annotations", "", "No annotations yet.", ""])
        return "\n".join(lines)

    for item in annotations:
        kind_title = "Red Highlight" if item.get("kind") == "red_highlight" else "Note"
        chapter = markdown_line(item.get("chapter_title")) or markdown_line(item.get("chapter_locator"))
        lines.extend(
            [
                f"## {item['export_index']}. {kind_title} · {chapter}",
                "",
                f"- Locator: `{markdown_line(item.get('chapter_locator'))}`",
                f"- Sentence index: `{markdown_line(item.get('sentence_index'))}`",
                f"- Created at: {markdown_line(item.get('created_at'))}",
                f"- Updated at: {markdown_line(item.get('updated_at'))}",
                "",
                "Source sentence:",
                "",
                f"> {markdown_line(item.get('source_text'))}",
                "",
            ]
        )
        note_text = markdown_line(item.get("note_text"))
        if note_text:
            lines.extend(["Note:", "", note_text, ""])
        if item.get("kind") == "red_highlight":
            lines.extend([f"Color: `{markdown_line(item.get('color')) or 'red'}`", ""])
    return "\n".join(lines)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def living_book_slug(book: dict[str, Any]) -> str:
    title_slug = safe_slug(str(book.get("title") or "book"))
    stable_part = str(book.get("book_hash") or book.get("id") or "")[:12]
    return safe_slug(f"{title_slug}-{stable_part}") if stable_part else title_slug


def living_book_safe_filename(book: dict[str, Any], source_path: Path) -> str:
    stem = safe_slug(str(book.get("title") or source_path.stem or "book"))
    suffix = source_path.suffix.lower() or ".epub"
    return f"{stem}{suffix}"


def relative_to_bundle(bundle_dir: Path, path: Path) -> str:
    try:
        return path.relative_to(bundle_dir).as_posix()
    except ValueError:
        return str(path)


def has_reviewed_marker(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        head = path.read_text(encoding="utf-8", errors="ignore")[:4096].lower()
    except OSError:
        return False
    return any(
        marker in head
        for marker in (
            "status: reviewed",
            "reviewed: true",
            '"status": "reviewed"',
            '"reviewed": true',
            "状态：已审阅",
            "状态: 已审阅",
        )
    )


def atomic_write_text(path: Path, text: str) -> None:
    """Replace a generated text file atomically without exposing a partial write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        temporary_path = Path(temporary_name)
        if temporary_path.exists():
            temporary_path.unlink()


def write_living_book_text(path: Path, text: str, *, protect_reviewed: bool = True) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if protect_reviewed and has_reviewed_marker(path):
        return {"path": str(path), "written": False, "reason": "reviewed_file_not_overwritten"}
    atomic_write_text(path, text.rstrip() + "\n")
    return {"path": str(path), "written": True}


def write_living_book_json(path: Path, payload: dict[str, Any], *, protect_reviewed: bool = True) -> dict[str, Any]:
    return write_living_book_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2),
        protect_reviewed=protect_reviewed,
    )


def living_book_book_row(book_id: str) -> dict[str, Any]:
    return jsonable(book_with_latest_file(book_id))


def living_book_annotations(book_id: str) -> list[dict[str, Any]]:
    with db.connect() as conn:
        annotation_rows = conn.execute(
            """
            SELECT * FROM reader.annotations
            WHERE book_id = %s
            ORDER BY chapter_locator ASC, created_at ASC
            """,
            (book_id,),
        ).fetchall()
        audio_rows = conn.execute(
            """
            SELECT * FROM reader.audio_notes
            WHERE book_id = %s
            ORDER BY created_at ASC
            """,
            (book_id,),
        ).fetchall()
    audio_by_annotation: dict[str, list[dict[str, Any]]] = {}
    for audio in [jsonable(dict(row)) for row in audio_rows]:
        annotation_id = str(audio.get("annotation_id") or "")
        if annotation_id:
            audio_by_annotation.setdefault(annotation_id, []).append(audio)
    items = annotation_export_items([dict(row) for row in annotation_rows])
    for item in items:
        item["audio_notes"] = audio_by_annotation.get(str(item.get("id") or ""), [])
    return items


def render_living_annotations_markdown(book: dict[str, Any], annotations: list[dict[str, Any]], generated_at: str) -> str:
    lines = [
        f"# {markdown_line(book.get('title'))} - 批注",
        "",
        "> 本文件由 Click/Reader 数据库导出，便于人阅读和 Hermes/知识库引用；批注主存储仍是 `reader.annotations`。",
        "",
        f"- Book ID: `{markdown_line(book.get('id'))}`",
        f"- Exported at: {generated_at}",
        f"- Annotation count: {len(annotations)}",
        "",
    ]
    if not annotations:
        lines.extend(["## 暂无批注", ""])
        return "\n".join(lines)
    for item in annotations:
        kind = "标红" if item.get("kind") == "red_highlight" else "备注"
        chapter = markdown_line(item.get("chapter_title")) or markdown_line(item.get("chapter_locator"))
        lines.extend(
            [
                f"## {item.get('export_index')}. {kind} · {chapter}",
                "",
                f"- Annotation ID: `{markdown_line(item.get('id'))}`",
                f"- Locator: `{markdown_line(item.get('chapter_locator'))}`",
                f"- Sentence index: `{markdown_line(item.get('sentence_index'))}`",
                f"- Created at: {markdown_line(item.get('created_at'))}",
                f"- Updated at: {markdown_line(item.get('updated_at'))}",
                "",
                "### 原文",
                "",
                f"> {markdown_line(item.get('source_text'))}",
                "",
            ]
        )
        note = markdown_line(item.get("note_text"))
        if note:
            lines.extend(["### 我的备注", "", note, ""])
        for audio in item.get("audio_notes") or []:
            transcript = markdown_line(audio.get("transcript"))
            if transcript:
                lines.extend(
                    [
                        "### 语音备注转写",
                        "",
                        transcript,
                        "",
                        f"- Audio note: `{markdown_line(audio.get('id'))}`",
                        f"- Status: `{markdown_line(audio.get('status'))}`",
                        "",
                    ]
                )
    return "\n".join(lines)


def render_book_home(book: dict[str, Any], manifest: dict[str, Any]) -> str:
    title = markdown_line(book.get("title")) or markdown_line(book.get("id"))
    author = markdown_line(book.get("author")) or "未知作者"
    paths = manifest["paths"]
    return "\n".join(
        [
            f"# {title}",
            "",
            "## 这本书是什么",
            "",
            f"- 书名：{title}",
            f"- 作者：{author}",
            f"- Book ID：`{markdown_line(book.get('id'))}`",
            f"- 文件类型：`{markdown_line(book.get('source_kind'))}`",
            f"- 文件 hash：`{markdown_line(manifest.get('file_hash'))}`",
            "",
            "## 数据主从关系",
            "",
            "- Click/Reader 数据库是阅读运行和批注主存储。",
            "- `reader.annotations` 是批注主表。",
            "- `reader.audio_notes` 是语音备注主表。",
            "- `2_批注数据/批注.json` 和 `2_批注数据/批注.md` 是导出层，可从数据库重建。",
            "- 原书保持干净，不把批注写回 EPUB/PDF。",
            "",
            "## 文件入口",
            "",
            f"- 原书：`{paths['original_dir']}`",
            f"- 批注 JSON：`{paths['annotations_json']}`",
            f"- 批注 Markdown：`{paths['annotations_markdown']}`",
            f"- 融合阅读：`{paths['fused_reading_dir']}`",
            f"- 书籍整理：`{paths['book_notes_dir']}`",
            f"- 思想模型：`{paths['thought_model_dir']}`",
            f"- Hermes 调用：`{paths['hermes_dir']}`",
            f"- 对话复盘：`{paths['dialogue_review_dir']}`",
            "",
            "## Hermes 使用建议",
            "",
            "- 如果用户问书中内容，读取原书、书籍整理或融合阅读。",
            "- 如果用户问“我读这本书时怎么想”，优先读取批注 JSON/Markdown。",
            "- 如果用户要求这本书参与判断，读取 Hermes 调用卡、思想模型草稿、相关原文和相关批注。",
            "- Hermes 输出必须区分：书中明确说过、按本书逻辑推演、Hermes 综合判断。",
        ]
    )


def preserve_book_home_extensions(path: Path, generated: str) -> str:
    """Keep worker-owned sections when Click refreshes the generated book home."""
    try:
        existing = path.read_text(encoding="utf-8")
    except OSError:
        return generated
    marker = re.search(r"(?m)^## worker_[^\n]*$", existing)
    if marker is None:
        return generated
    extension = existing[marker.start():].strip()
    return f"{generated.rstrip()}\n\n{extension}" if extension else generated


def render_hermes_call_card(book: dict[str, Any]) -> str:
    title = markdown_line(book.get("title")) or markdown_line(book.get("id"))
    author = markdown_line(book.get("author")) or "未知作者"
    return "\n".join(
        [
            "# 书籍调用卡",
            "",
            "status: draft",
            "",
            "## 书名",
            "",
            title,
            "",
            "## 作者",
            "",
            author,
            "",
            "## 适合参与的问题",
            "",
            "- 待用户或 Hermes 根据批注和思想模型补充",
            "",
            "## 核心视角",
            "",
            "本调用卡是 Click 自动生成的 P1 草稿，只负责给 Hermes 路由，不替代原文、批注或思想模型。",
            "",
            "## Hermes 使用规则",
            "",
            "- 如果用户要求书中依据，必须读取原文、融合阅读或明确出处。",
            "- 如果用户要求“我怎么想”，必须读取 `../2_批注数据/批注.json` 或 `../2_批注数据/批注.md`。",
            "- 如果用户要求书活过来，必须读取 `../5_书籍思想模型/`，且草稿模型不能当作最终事实。",
            "- 输出必须区分：书中明确说过、按本书逻辑推演、超出本书范围的 Hermes 综合判断。",
            "",
            "## 文件入口",
            "",
            "- 原书：`../1_原书/`",
            "- 批注：`../2_批注数据/`",
            "- 融合阅读：`../3_融合阅读/`",
            "- 思想模型：`../5_书籍思想模型/`",
        ]
    )


def render_fused_chapter(book: dict[str, Any], chapter: str, annotations: list[dict[str, Any]], generated_at: str) -> str:
    title = markdown_line(book.get("title")) or markdown_line(book.get("id"))
    lines = [
        f"# {markdown_line(chapter) or '未命名章节'}_融合阅读",
        "",
        f"- Book: {title}",
        f"- Generated at: {generated_at}",
        "- Scope: only annotated fragments for Living Books P1",
        "",
    ]
    for item in annotations:
        kind = "标红" if item.get("kind") == "red_highlight" else "备注"
        lines.extend(
            [
                f"## 片段 {item.get('export_index')}",
                "",
                "【原文位置】",
                "",
                f"- Chapter locator: `{markdown_line(item.get('chapter_locator'))}`",
                f"- Sentence index: `{markdown_line(item.get('sentence_index'))}`",
                f"- Annotation ID: `{markdown_line(item.get('id'))}`",
                "",
                "【原文】",
                "",
                f"> {markdown_line(item.get('source_text'))}",
                "",
                "【我的标注】",
                "",
                f"- 类型：{kind}",
                f"- 颜色：{markdown_line(item.get('color')) or ('red' if item.get('kind') == 'red_highlight' else '')}",
                "",
            ]
        )
        note = markdown_line(item.get("note_text"))
        if note:
            lines.extend(["【我的备注】", "", note, ""])
        for audio in item.get("audio_notes") or []:
            transcript = markdown_line(audio.get("transcript"))
            if transcript:
                lines.extend(["【语音备注转写】", "", transcript, ""])
        lines.extend(
            [
                "【可用于】",
                "",
                "- 回答这本书相关内容",
                "- 回答我读这本书时怎么想",
                "- 让 Hermes 使用原文和批注作为证据",
                "",
            ]
        )
    return "\n".join(lines)


DEFAULT_LIVING_BOOK_TAXONOMY: list[dict[str, Any]] = [
    {
        "category_id": "pending",
        "level": 1,
        "parent_id": None,
        "name": "待分类",
        "directory_name": "00_待分类",
        "description": "尚未人工确认分类的书。",
        "aliases": ["未分类", "待整理"],
        "status": "active",
    },
    {
        "category_id": "pending.unclassified",
        "level": 2,
        "parent_id": "pending",
        "name": "待分类",
        "directory_name": "00_待分类",
        "description": "Hermes 尚不能可靠归类，等待用户确认。",
        "aliases": ["普通阅读", "待人工确认"],
        "status": "active",
    },
    {
        "category_id": "faith_spiritual",
        "level": 1,
        "parent_id": None,
        "name": "信仰与属灵",
        "directory_name": "01_信仰与属灵",
        "description": "圣经、祷告、属灵生命和教会历史。",
        "aliases": ["信仰", "属灵", "圣经"],
        "status": "active",
    },
    {
        "category_id": "faith_spiritual.prayer",
        "level": 2,
        "parent_id": "faith_spiritual",
        "name": "祷告",
        "directory_name": "祷告",
        "description": "祷告、与主交通、属灵操练相关书籍。",
        "aliases": ["祈祷", "交通", "亲近主"],
        "status": "active",
    },
    {
        "category_id": "faith_spiritual.bible_study",
        "level": 2,
        "parent_id": "faith_spiritual",
        "name": "圣经研读",
        "directory_name": "圣经研读",
        "description": "经文、生命读经、解经和圣经主题研究。",
        "aliases": ["生命读经", "经文", "福音"],
        "status": "active",
    },
    {
        "category_id": "faith_spiritual.spiritual_life",
        "level": 2,
        "parent_id": "faith_spiritual",
        "name": "属灵生命",
        "directory_name": "属灵生命",
        "description": "生命经历、属灵成长和实行。",
        "aliases": ["生命", "成长", "操练"],
        "status": "active",
    },
    {
        "category_id": "faith_spiritual.church_history",
        "level": 2,
        "parent_id": "faith_spiritual",
        "name": "教会历史",
        "directory_name": "教会历史",
        "description": "教会史、人物和传统。",
        "aliases": ["历史", "教会史"],
        "status": "active",
    },
    {
        "category_id": "cognition_learning",
        "level": 1,
        "parent_id": None,
        "name": "认知与学习",
        "directory_name": "02_认知与学习",
        "description": "学习、阅读、认知科学和研究方法。",
        "aliases": ["学习", "认知", "阅读"],
        "status": "active",
    },
    {
        "category_id": "cognition_learning.learning_methods",
        "level": 2,
        "parent_id": "cognition_learning",
        "name": "学习方法",
        "directory_name": "学习方法",
        "description": "学习方法、技能训练和复习系统。",
        "aliases": ["学习", "技能", "训练"],
        "status": "active",
    },
    {
        "category_id": "cognition_learning.reading_writing",
        "level": 2,
        "parent_id": "cognition_learning",
        "name": "阅读写作",
        "directory_name": "阅读写作",
        "description": "阅读、笔记、写作和知识表达。",
        "aliases": ["阅读", "写作", "笔记"],
        "status": "active",
    },
    {
        "category_id": "cognition_learning.cognitive_science",
        "level": 2,
        "parent_id": "cognition_learning",
        "name": "认知科学",
        "directory_name": "认知科学",
        "description": "记忆、注意力、心理学和认知机制。",
        "aliases": ["记忆", "心理学", "大脑"],
        "status": "active",
    },
    {
        "category_id": "cognition_learning.research_methods",
        "level": 2,
        "parent_id": "cognition_learning",
        "name": "研究方法",
        "directory_name": "研究方法",
        "description": "研究、论文、调查和论证方法。",
        "aliases": ["研究", "论文", "调查"],
        "status": "active",
    },
    {
        "category_id": "business",
        "level": 1,
        "parent_id": None,
        "name": "商业与经营",
        "directory_name": "03_商业与经营",
        "description": "战略、运营、营销和管理。",
        "aliases": ["经营", "管理", "商业"],
        "status": "active",
    },
    {
        "category_id": "business.strategy",
        "level": 2,
        "parent_id": "business",
        "name": "战略",
        "directory_name": "战略",
        "description": "竞争、定位、资源配置和长期选择。",
        "aliases": ["策略", "竞争", "定位"],
        "status": "active",
    },
    {
        "category_id": "business.operations",
        "level": 2,
        "parent_id": "business",
        "name": "运营",
        "directory_name": "运营",
        "description": "流程、供应链、组织运转和指标。",
        "aliases": ["流程", "供应链", "指标"],
        "status": "active",
    },
    {
        "category_id": "business.marketing",
        "level": 2,
        "parent_id": "business",
        "name": "营销",
        "directory_name": "营销",
        "description": "品牌、增长、广告和销售。",
        "aliases": ["品牌", "广告", "销售"],
        "status": "active",
    },
    {
        "category_id": "business.management",
        "level": 2,
        "parent_id": "business",
        "name": "管理",
        "directory_name": "管理",
        "description": "组织、团队和管理方法。",
        "aliases": ["组织", "团队"],
        "status": "active",
    },
    {
        "category_id": "technology_ai",
        "level": 1,
        "parent_id": None,
        "name": "AI与技术",
        "directory_name": "04_AI与技术",
        "description": "AI、编程、自动化和产品设计。",
        "aliases": ["AI", "技术", "软件"],
        "status": "active",
    },
    {
        "category_id": "technology_ai.ai_tools",
        "level": 2,
        "parent_id": "technology_ai",
        "name": "AI工具",
        "directory_name": "AI工具",
        "description": "AI 工具、模型、agent 和工作流。",
        "aliases": ["人工智能", "模型", "agent"],
        "status": "active",
    },
    {
        "category_id": "technology_ai.programming",
        "level": 2,
        "parent_id": "technology_ai",
        "name": "编程",
        "directory_name": "编程",
        "description": "编程、软件工程和代码实践。",
        "aliases": ["代码", "软件工程"],
        "status": "active",
    },
    {
        "category_id": "technology_ai.automation",
        "level": 2,
        "parent_id": "technology_ai",
        "name": "自动化",
        "directory_name": "自动化",
        "description": "自动化、脚本和系统集成。",
        "aliases": ["自动化", "脚本"],
        "status": "active",
    },
    {
        "category_id": "technology_ai.product_design",
        "level": 2,
        "parent_id": "technology_ai",
        "name": "产品设计",
        "directory_name": "产品设计",
        "description": "产品、交互、体验和系统设计。",
        "aliases": ["产品", "设计", "交互"],
        "status": "active",
    },
    {
        "category_id": "personal_growth",
        "level": 1,
        "parent_id": None,
        "name": "个人成长",
        "directory_name": "05_个人成长",
        "description": "判断、时间、表达和行动能力。",
        "aliases": ["成长", "能力"],
        "status": "active",
    },
    {
        "category_id": "personal_growth.decision",
        "level": 2,
        "parent_id": "personal_growth",
        "name": "决策判断",
        "directory_name": "决策判断",
        "description": "判断、选择和复杂问题处理。",
        "aliases": ["判断", "决策", "选择"],
        "status": "active",
    },
    {
        "category_id": "personal_growth.time_management",
        "level": 2,
        "parent_id": "personal_growth",
        "name": "时间管理",
        "directory_name": "时间管理",
        "description": "时间、精力、习惯和执行。",
        "aliases": ["时间", "习惯", "执行"],
        "status": "active",
    },
    {
        "category_id": "personal_growth.communication",
        "level": 2,
        "parent_id": "personal_growth",
        "name": "表达沟通",
        "directory_name": "表达沟通",
        "description": "表达、演讲、写作沟通和说服。",
        "aliases": ["表达", "沟通", "说服"],
        "status": "active",
    },
    {
        "category_id": "faith_spiritual.training",
        "level": 2,
        "parent_id": "faith_spiritual",
        "name": "晨兴与训练",
        "directory_name": "晨兴与训练",
        "description": "晨兴圣言、特会训练、长老训练和成全材料。",
        "aliases": ["晨兴", "训练", "特会", "ITERO", "HWMR"],
        "status": "active",
    },
    {
        "category_id": "cognition_learning.logic_reasoning",
        "level": 2,
        "parent_id": "cognition_learning",
        "name": "逻辑与论证",
        "directory_name": "逻辑与论证",
        "description": "形式逻辑、批判性思维、论证与推理方法。",
        "aliases": ["逻辑", "论证", "推理", "批判性思维"],
        "status": "active",
    },
    {
        "category_id": "cognition_learning.psychology",
        "level": 2,
        "parent_id": "cognition_learning",
        "name": "心理学",
        "directory_name": "心理学",
        "description": "人格、社会、发展和应用心理学。",
        "aliases": ["心理", "人格", "行为"],
        "status": "active",
    },
    {
        "category_id": "business.fundamentals",
        "level": 2,
        "parent_id": "business",
        "name": "商业基础",
        "directory_name": "商业基础",
        "description": "商业通识、商业认知和经营基本原理。",
        "aliases": ["商业通识", "商业认知", "底层逻辑"],
        "status": "active",
    },
    {
        "category_id": "business.product_user",
        "level": 2,
        "parent_id": "business",
        "name": "产品与用户",
        "directory_name": "产品与用户",
        "description": "用户研究、需求、产品方法与商业模式。",
        "aliases": ["产品", "用户", "需求", "商业模式"],
        "status": "active",
    },
    {
        "category_id": "business.economics_finance",
        "level": 2,
        "parent_id": "business",
        "name": "经济与金融",
        "directory_name": "经济与金融",
        "description": "经济学、宏观经济、金融和公司财务。",
        "aliases": ["经济", "宏观", "金融", "财务"],
        "status": "active",
    },
    {
        "category_id": "business.review_execution",
        "level": 2,
        "parent_id": "business",
        "name": "复盘与执行",
        "directory_name": "复盘与执行",
        "description": "经营复盘、方法落地、执行改进和组织学习。",
        "aliases": ["复盘", "打法", "执行", "改进"],
        "status": "active",
    },
    {
        "category_id": "technology_ai.data_systems",
        "level": 2,
        "parent_id": "technology_ai",
        "name": "数据与系统",
        "directory_name": "数据与系统",
        "description": "数据工程、信息系统和系统架构。",
        "aliases": ["数据", "系统", "架构"],
        "status": "active",
    },
    {
        "category_id": "personal_growth.execution_reflection",
        "level": 2,
        "parent_id": "personal_growth",
        "name": "行动与复盘",
        "directory_name": "行动与复盘",
        "description": "个人行动、习惯改进、自我复盘和能力精进。",
        "aliases": ["行动", "复盘", "精进"],
        "status": "active",
    },
    {
        "category_id": "personal_growth.career_growth",
        "level": 2,
        "parent_id": "personal_growth",
        "name": "职业与成长",
        "directory_name": "职业与成长",
        "description": "职业发展、职场能力和长期成长。",
        "aliases": ["职业", "职场", "成长"],
        "status": "active",
    },
    {
        "category_id": "tcm_health",
        "level": 1,
        "parent_id": None,
        "name": "中医与健康",
        "directory_name": "06_中医与健康",
        "description": "中医理论、经典、诊疗、方药、针灸和文献研究。",
        "aliases": ["中医", "健康", "医学"],
        "status": "active",
    },
    {
        "category_id": "tcm_health.fundamentals",
        "level": 2,
        "parent_id": "tcm_health",
        "name": "基础理论",
        "directory_name": "基础理论",
        "description": "中医基础理论和学科总论。",
        "aliases": ["中医基础", "基础理论"],
        "status": "active",
    },
    {
        "category_id": "tcm_health.classics",
        "level": 2,
        "parent_id": "tcm_health",
        "name": "经典",
        "directory_name": "经典",
        "description": "内经、伤寒、金匮及经典选读。",
        "aliases": ["内经", "伤寒", "金匮", "经典"],
        "status": "active",
    },
    {
        "category_id": "tcm_health.herbs_formulas",
        "level": 2,
        "parent_id": "tcm_health",
        "name": "中药与方剂",
        "directory_name": "中药与方剂",
        "description": "中药、方剂和配伍应用。",
        "aliases": ["中药", "方剂", "本草"],
        "status": "active",
    },
    {
        "category_id": "tcm_health.acupuncture_meridians",
        "level": 2,
        "parent_id": "tcm_health",
        "name": "针灸与经络",
        "directory_name": "针灸与经络",
        "description": "针灸、经络和腧穴。",
        "aliases": ["针灸", "经络", "腧穴"],
        "status": "active",
    },
    {
        "category_id": "tcm_health.diagnosis_clinical",
        "level": 2,
        "parent_id": "tcm_health",
        "name": "诊断与临床",
        "directory_name": "诊断与临床",
        "description": "中医诊断、辨证和临床应用。",
        "aliases": ["诊断", "辨证", "临床"],
        "status": "active",
    },
    {
        "category_id": "tcm_health.pharmacology_research",
        "level": 2,
        "parent_id": "tcm_health",
        "name": "药理与研究",
        "directory_name": "药理与研究",
        "description": "中药药理、实验和现代研究。",
        "aliases": ["药理", "实验", "现代研究"],
        "status": "active",
    },
    {
        "category_id": "tcm_health.literature",
        "level": 2,
        "parent_id": "tcm_health",
        "name": "文献与医史",
        "directory_name": "文献与医史",
        "description": "中医文献、目录学和医学史。",
        "aliases": ["文献", "医史", "目录"],
        "status": "active",
    },
    {
        "category_id": "history_humanities",
        "level": 1,
        "parent_id": None,
        "name": "历史与人文",
        "directory_name": "07_历史与人文",
        "description": "中国史、世界史、思想经典和社会文化。",
        "aliases": ["历史", "人文", "思想"],
        "status": "active",
    },
    {
        "category_id": "history_humanities.chinese_history",
        "level": 2,
        "parent_id": "history_humanities",
        "name": "中国史",
        "directory_name": "中国史",
        "description": "中国通史、断代史、人物和制度。",
        "aliases": ["中国历史", "明史", "清史"],
        "status": "active",
    },
    {
        "category_id": "history_humanities.world_history",
        "level": 2,
        "parent_id": "history_humanities",
        "name": "世界史",
        "directory_name": "世界史",
        "description": "世界历史、地区史和文明史。",
        "aliases": ["世界历史", "文明史"],
        "status": "active",
    },
    {
        "category_id": "history_humanities.thought_classics",
        "level": 2,
        "parent_id": "history_humanities",
        "name": "思想与经典",
        "directory_name": "思想与经典",
        "description": "哲学、思想史和传统经典。",
        "aliases": ["思想", "哲学", "经典"],
        "status": "active",
    },
    {
        "category_id": "history_humanities.social_culture",
        "level": 2,
        "parent_id": "history_humanities",
        "name": "社会与文化",
        "directory_name": "社会与文化",
        "description": "社会研究、文化观察和民俗。",
        "aliases": ["社会", "文化", "民俗"],
        "status": "active",
    },
    {
        "category_id": "literature_arts",
        "level": 1,
        "parent_id": None,
        "name": "文学与艺术",
        "directory_name": "08_文学与艺术",
        "description": "文学作品、散文、漫画和艺术。",
        "aliases": ["文学", "小说", "艺术", "漫画"],
        "status": "active",
    },
    {
        "category_id": "literature_arts.classical_chinese",
        "level": 2,
        "parent_id": "literature_arts",
        "name": "中国古典文学",
        "directory_name": "中国古典文学",
        "description": "中国古典小说、诗文和文学作品。",
        "aliases": ["古典文学", "古典小说", "诗词"],
        "status": "active",
    },
    {
        "category_id": "literature_arts.modern_chinese",
        "level": 2,
        "parent_id": "literature_arts",
        "name": "中国现当代文学",
        "directory_name": "中国现当代文学",
        "description": "中国现代与当代文学作品。",
        "aliases": ["现代文学", "当代文学"],
        "status": "active",
    },
    {
        "category_id": "literature_arts.world_literature",
        "level": 2,
        "parent_id": "literature_arts",
        "name": "外国文学",
        "directory_name": "外国文学",
        "description": "外国小说、诗歌和戏剧。",
        "aliases": ["外国小说", "世界文学"],
        "status": "active",
    },
    {
        "category_id": "literature_arts.comics_visual",
        "level": 2,
        "parent_id": "literature_arts",
        "name": "漫画与图像",
        "directory_name": "漫画与图像",
        "description": "漫画、图像叙事和轻阅读。",
        "aliases": ["漫画", "绘本", "图像"],
        "status": "active",
    },
    {
        "category_id": "projects_systems",
        "level": 1,
        "parent_id": None,
        "name": "项目与工作系统",
        "directory_name": "09_项目与工作系统",
        "description": "项目手册、内部知识资产和工作系统资料。",
        "aliases": ["项目", "工作系统", "内部资料"],
        "status": "active",
    },
    {
        "category_id": "projects_systems.marketplace_os",
        "level": 2,
        "parent_id": "projects_systems",
        "name": "Marketplace OS",
        "directory_name": "Marketplace OS",
        "description": "Marketplace OS 经营系统和训练资料。",
        "aliases": ["亚马逊", "Marketplace", "电商经营"],
        "status": "active",
    },
    {
        "category_id": "projects_systems.click_reader",
        "level": 2,
        "parent_id": "projects_systems",
        "name": "Click / Reader",
        "directory_name": "Click Reader",
        "description": "Click、Reader 和学习系统的产品资料。",
        "aliases": ["Click", "Reader", "阅读系统"],
        "status": "active",
    },
    {
        "category_id": "projects_systems.content_creation",
        "level": 2,
        "parent_id": "projects_systems",
        "name": "内容创作",
        "directory_name": "内容创作",
        "description": "选题、配文、脚本和内容资产。",
        "aliases": ["内容", "配文", "脚本", "选题"],
        "status": "active",
    },
]


LIVING_BOOK_JOB_TERMINAL_STATUSES = {"needs_review", "complete", "reviewed", "skipped_reviewed"}
LIVING_BOOK_ANALYSIS_STATES = {
    "not_requested",
    "queued",
    "running",
    "draft_ready",
    "needs_review",
    "complete",
    "failed",
}
LIVING_BOOK_ANALYSIS_THREAD_LOCK = threading.Lock()
LIVING_BOOK_ANALYSIS_THREADS: set[str] = set()


def read_living_book_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_living_book_json_unprotected(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2).rstrip() + "\n")
    return {"path": str(path), "written": True}


def living_book_index_dir(lb_root: Path) -> Path:
    return lb_root / ("indexes" if lb_root.name == "_system" else "index")


def living_book_jobs_path(lb_root: Path) -> Path:
    jobs_dir = "jobs" if lb_root.name == "_system" else "_jobs"
    return lb_root / jobs_dir / "living_book_jobs.json"


def living_book_status_path(bundle_dir: Path) -> Path:
    return bundle_dir / "_status" / "living_book_status.json"


def living_book_analysis_path(bundle_dir: Path) -> Path:
    return bundle_dir / "_status" / "analysis.json"


def default_living_book_analysis(book_id: str, bundle_dir: Optional[Path] = None) -> dict[str, Any]:
    return {
        "schema": "click.living_book.analysis_state.v1",
        "book_id": book_id,
        "bundle_dir": str(bundle_dir) if bundle_dir else "",
        "state": "not_requested",
        "requested_at": None,
        "started_at": None,
        "finished_at": None,
        "requested_by": None,
        "runtime": None,
        "model": None,
        "invocation_id": None,
        "last_error": None,
        "outputs": {},
        "updated_at": now_iso(),
    }


def write_living_book_analysis(bundle_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    state = str(payload.get("state") or "not_requested")
    if state not in LIVING_BOOK_ANALYSIS_STATES:
        raise ValueError(f"invalid Living Book analysis state: {state}")
    normalized = {
        **payload,
        "schema": "click.living_book.analysis_state.v1",
        "bundle_dir": str(bundle_dir),
        "state": state,
        "updated_at": now_iso(),
    }
    write_living_book_json_unprotected(living_book_analysis_path(bundle_dir), normalized)
    return normalized


def living_book_analysis_state_for_book(
    book_id: str,
    *,
    knowledge_base_root: Optional[str] = None,
    create: bool = False,
) -> dict[str, Any]:
    try:
        book = living_book_book_row(book_id)
    except HTTPException:
        return default_living_book_analysis(book_id)
    bundle_dir = living_book_bundle_dir(book, knowledge_base_root)
    path = living_book_analysis_path(bundle_dir)
    payload = read_living_book_json(path, {})
    if payload.get("schema") == "click.living_book.analysis_state.v1" and payload.get("state") in LIVING_BOOK_ANALYSIS_STATES:
        return payload
    default = default_living_book_analysis(book_id, bundle_dir)
    if create:
        bundle_dir.mkdir(parents=True, exist_ok=True)
        return write_living_book_analysis(bundle_dir, default)
    return default


def living_book_legacy_bundle_dir(book: dict[str, Any], knowledge_base_root: Optional[str] = None) -> Path:
    return legacy_living_books_root(knowledge_base_root) / "books" / living_book_slug(book)


def living_book_pending_bundle_dir(book: dict[str, Any], knowledge_base_root: Optional[str] = None) -> Path:
    return knowledge_base_root_path(knowledge_base_root) / "00_待分类" / living_book_slug(book)


def living_book_identity(
    book: dict[str, Any], knowledge_base_root: Optional[str] = None
) -> dict[str, str]:
    migration_entry = living_book_migration_entry(book, knowledge_base_root)
    if migration_entry:
        mapped_work_id = str(migration_entry.get("work_id") or "").strip()
        mapped_edition_id = str(migration_entry.get("edition_id") or "").strip()
        if mapped_work_id and mapped_edition_id:
            return {
                "work_id": mapped_work_id,
                "edition_id": mapped_edition_id,
            }
    title = re.sub(r"\s+", " ", str(book.get("title") or "").strip().casefold())
    author = re.sub(r"\s+", " ", str(book.get("author") or "").strip().casefold())
    source_kind = str(book.get("source_kind") or book.get("file_kind") or "unknown").strip().casefold()
    source_hash = str(book.get("file_hash") or book.get("book_hash") or "").strip()
    normalized_source_hash = (
        source_hash if not source_hash or source_hash.startswith("sha256:") else f"sha256:{source_hash}"
    )
    work_key = f"{title}\n{author}"
    edition_key = normalized_source_hash or (
        f"{source_kind}\n{book.get('id') or ''}\n{book.get('file_path') or ''}"
    )
    return {
        "work_id": f"work_{hashlib.sha256(work_key.encode('utf-8')).hexdigest()[:16]}",
        "edition_id": f"edition_{hashlib.sha256(edition_key.encode('utf-8')).hexdigest()[:16]}",
    }


def living_book_v2_bundle_dir(book: dict[str, Any], knowledge_base_root: Optional[str] = None) -> Path:
    identity = living_book_identity(book, knowledge_base_root)
    return (
        knowledge_base_root_path(knowledge_base_root)
        / "30_Resources"
        / "Books"
        / identity["work_id"]
        / identity["edition_id"]
    )


def living_book_migration_map_path(knowledge_base_root: Optional[str] = None) -> Path:
    return knowledge_base_root_path(knowledge_base_root) / "_system" / "migrations" / "living_books_v1_map.json"


def living_book_migration_entry(
    book: dict[str, Any], knowledge_base_root: Optional[str] = None
) -> Optional[dict[str, Any]]:
    payload = read_living_book_json(living_book_migration_map_path(knowledge_base_root), {})
    book_id = str(book.get("id") or "")
    for entry in payload.get("entries") or []:
        if str(entry.get("book_id") or "") == book_id:
            return entry
    return None


def living_book_mapped_bundle_dir(book: dict[str, Any], knowledge_base_root: Optional[str] = None) -> Optional[Path]:
    entry = living_book_migration_entry(book, knowledge_base_root)
    if entry is None:
        return None
    root = knowledge_base_root_path(knowledge_base_root).resolve()
    candidate_value = entry.get("write_owner_path") or entry.get("legacy_bundle_path")
    if not candidate_value:
        return None
    candidate = Path(str(candidate_value)).expanduser()
    try:
        candidate.resolve().relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.exists() else None


def _bundle_from_canonical_source(path: Path, knowledge_base_root: Path) -> Optional[Path]:
    try:
        relative = path.resolve().relative_to(knowledge_base_root.resolve())
    except (FileNotFoundError, OSError, ValueError):
        return None
    if "1_原书" not in relative.parts:
        return None
    index = relative.parts.index("1_原书")
    return knowledge_base_root.joinpath(*relative.parts[:index])


def reconcile_living_book_write_owner(
    book_id: str,
    payload: LivingBookWriteOwnerReconcileRequest,
    *,
    knowledge_base_root: Optional[str] = None,
) -> dict[str, Any]:
    """Adopt a verified existing V2 bundle through the official migration map."""
    kb_root = knowledge_base_root_path(knowledge_base_root).resolve()
    books_root = (kb_root / "30_Resources" / "Books").resolve()
    target = Path(payload.target_bundle).expanduser().resolve()
    try:
        target_relative = target.relative_to(books_root)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="target_bundle must be inside KnowledgeBase/30_Resources/Books") from exc
    if len(target_relative.parts) != 2:
        raise HTTPException(status_code=422, detail="target_bundle must have work_id/edition_id shape")
    work_id, edition_id = target_relative.parts
    if not re.fullmatch(r"work_[a-z0-9]{12,64}", work_id) or not re.fullmatch(
        r"edition_[a-z0-9]{12,64}", edition_id
    ):
        raise HTTPException(status_code=422, detail="target_bundle has invalid V2 identity")
    manifest_path = target / "book_manifest.json"
    manifest = read_living_book_json(manifest_path, {})
    if manifest.get("schema") != "jiangyu.knowledge.book_manifest.v2":
        raise HTTPException(status_code=422, detail="target bundle does not have a V2 manifest")
    if manifest.get("work_id") != work_id or manifest.get("edition_id") != edition_id:
        raise HTTPException(status_code=409, detail="target manifest identity does not match its path")
    if str(manifest.get("book_id") or "") != book_id:
        raise HTTPException(status_code=409, detail="target manifest belongs to another Reader book")
    expected_hash = str(payload.expected_sha256 or "").strip().lower().removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise HTTPException(status_code=422, detail="expected_sha256 is invalid")
    canonical_relative = str(
        (manifest.get("source") or {}).get("canonical_file")
        or manifest.get("canonical_source_file")
        or ""
    )
    canonical_source = target / canonical_relative
    if not canonical_relative or not canonical_source.is_file():
        raise HTTPException(status_code=409, detail="target canonical source is missing")
    if file_sha256(canonical_source) != expected_hash:
        raise HTTPException(status_code=409, detail="target canonical source hash mismatch")

    book_before = book_with_latest_file(book_id)
    if str(book_before.get("book_hash") or "").strip().lower() != expected_hash:
        raise HTTPException(status_code=409, detail="Reader book hash does not match the target edition")
    source_path = Path(str(book_before.get("file_path") or "")).expanduser()
    previous_write_owner = _bundle_from_canonical_source(source_path, kb_root)
    map_path = living_book_migration_map_path(str(kb_root))
    migration_map = read_living_book_json(map_path, {})
    entries = list(migration_map.get("entries") or [])
    matches = [entry for entry in entries if str(entry.get("book_id") or "") == book_id]
    if len(matches) > 1:
        raise HTTPException(status_code=409, detail="migration map has multiple entries for this book")
    if matches and Path(str(matches[0].get("write_owner_path") or "")).expanduser().resolve() != target:
        raise HTTPException(status_code=409, detail="book already has a different official write owner")
    target_owners = [
        entry
        for entry in entries
        if Path(str(entry.get("write_owner_path") or "")).expanduser().resolve() == target
        and str(entry.get("book_id") or "") != book_id
    ]
    if target_owners:
        raise HTTPException(status_code=409, detail="target bundle is already owned by another book")

    previous_entry = dict(matches[0]) if matches else None
    previous_migration_map = json.loads(json.dumps(migration_map))
    observed_at = now_iso()
    retained_legacy_bundle = str(
        (previous_entry or {}).get("legacy_bundle_path")
        or previous_write_owner
        or source_path.parent
    )
    retained_previous_owner = str(
        (previous_entry or {}).get("previous_write_owner_path")
        or previous_write_owner
        or source_path.parent
    )
    entry = {
        **(previous_entry or {}),
        "entry_id": str((previous_entry or {}).get("entry_id") or stable_id("living_book_asset", str(target))),
        "book_id": book_id,
        "book_slug": manifest.get("book_slug") or target.name,
        "title": payload.title or manifest.get("title") or book_before.get("title"),
        "author": payload.author if payload.author is not None else manifest.get("author") or book_before.get("author"),
        "source_kind": manifest.get("source_kind") or book_before.get("source_kind"),
        "source_hash": f"sha256:{expected_hash}",
        "work_id": work_id,
        "edition_id": edition_id,
        "legacy_bundle_path": retained_legacy_bundle,
        "proposed_v2_path": str(target),
        "migration_state": "cutover",
        "previous_write_owner_path": retained_previous_owner,
        "write_owner_path": str(target),
        "copy_completed": True,
        "verified": True,
        "cutover_allowed": True,
        "cutover_at": str((previous_entry or {}).get("cutover_at") or observed_at),
        "canonical_source_sha256_at_cutover": expected_hash,
        "legacy_read_compatible": True,
        "legacy_deleted": False,
        "runtime_binding": "reader_api",
        "runtime_refresh_required": True,
        "reconciliation_mode": "adopt_existing_v2_target",
    }
    if matches:
        entries[entries.index(matches[0])] = entry
    else:
        entries.append(entry)
    migration_map["entries"] = entries
    migration_map["entry_count"] = len(entries)
    migration_map["updated_at"] = observed_at
    migration_map["policy"] = {
        **(migration_map.get("policy") or {}),
        "cutover_books": sum(1 for item in entries if item.get("migration_state") == "cutover"),
        "existing_v2_reconciliation_uses_verified_source_hash": True,
        "duplicate_sidecars_are_preserved": True,
    }

    rollback_root = Path(
        os.environ.get(
            "CLICK_LIVING_BOOK_ROLLBACK_DIR",
            str(Path.home() / "Documents" / "ClickData" / "migrations" / "rollback"),
        )
    ).expanduser()
    rollback_path = rollback_root / (
        f"living_book_reconcile_{book_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    book_title_before = str(book_before.get("title") or "")
    book_author_before = str(book_before.get("author") or "") or None
    write_living_book_json_unprotected(map_path, migration_map)
    metadata_updated = payload.title is not None or payload.author is not None
    metadata_audit_updates: list[str] = []
    try:
        if metadata_updated:
            update_library_book_metadata(
                book_id,
                LibraryMetadataPatch(
                    title=payload.title,
                    author=payload.author,
                    source=payload.metadata_source,
                    evidence=payload.metadata_evidence,
                ),
            )
        runtime_refresh = generate_living_book_bundle(book_id, knowledge_base_root=str(kb_root))
        if Path(str(runtime_refresh.get("bundle_dir") or "")).resolve() != target:
            raise RuntimeError("Reader refreshed a bundle other than the official write owner")
        runtime_book = book_with_latest_file(book_id)
        runtime_path = Path(str(runtime_book.get("file_path") or "")).expanduser().resolve()
        try:
            runtime_path.relative_to((target / "1_原书").resolve())
        except ValueError as exc:
            raise RuntimeError("Reader did not prefer the official write-owner source") from exc
        refreshed_manifest = read_living_book_json(manifest_path, {})
        if refreshed_manifest.get("work_id") != work_id or refreshed_manifest.get("edition_id") != edition_id:
            raise RuntimeError("runtime refresh changed the official V2 identity")
        if metadata_updated:
            for key, value in list(refreshed_manifest.items()):
                if not re.fullmatch(r"worker_[a-z0-9_]+_model_extraction", str(key)) or not isinstance(value, dict):
                    continue
                audit = value.get("metadata_audit")
                if not isinstance(audit, dict):
                    continue
                value["metadata_audit"] = {
                    **audit,
                    "status": "resolved",
                    "disposition": "corrected_via_click_formal_metadata_override",
                    "resolution": {
                        "title": payload.title or refreshed_manifest.get("title"),
                        "author": payload.author if payload.author is not None else refreshed_manifest.get("author"),
                        "source": payload.metadata_source,
                        "evidence": payload.metadata_evidence,
                        "resolved_at": now_iso(),
                    },
                }
                refreshed_manifest[key] = value
                metadata_audit_updates.append(str(key))
            if metadata_audit_updates:
                write_living_book_json_unprotected(manifest_path, refreshed_manifest)
        entry.update(
            {
                "runtime_refresh_completed": True,
                "runtime_refresh_at": now_iso(),
                "runtime_manifest_schema": refreshed_manifest.get("schema"),
                "runtime_refresh_kind": "reader_api_reconciliation",
            }
        )
        migration_map["updated_at"] = now_iso()
        write_living_book_json_unprotected(map_path, migration_map)
    except Exception:
        write_living_book_json_unprotected(map_path, previous_migration_map)
        if metadata_updated:
            update_library_book_metadata(
                book_id,
                LibraryMetadataPatch(
                    title=book_title_before,
                    author=book_author_before,
                    source="write_owner_reconciliation_rollback",
                    evidence=str(rollback_path),
                ),
            )
        raise

    rollback_payload = {
        "schema": "jiangyu.migration.rollback.living_book_reconciliation.v1",
        "created_at": now_iso(),
        "map_path": str(map_path),
        "book_id": book_id,
        "previous_entry": previous_entry,
        "activated_entry": entry,
        "source_bundle_preserved": bool(previous_write_owner and previous_write_owner.is_dir()),
        "target_bundle": str(target),
        "canonical_source_sha256": expected_hash,
        "rollback_action": "restore previous_entry or remove the reconciliation entry; do not delete either bundle",
    }
    write_living_book_json_unprotected(rollback_path, rollback_payload)
    return {
        "ok": True,
        "schema": "click.living_book.write_owner_reconciliation.v1",
        "book_id": book_id,
        "previous_write_owner": str(previous_write_owner) if previous_write_owner else None,
        "write_owner": str(target),
        "runtime_file_path": str(runtime_path),
        "rollback_path": str(rollback_path),
        "metadata": {
            "title": runtime_book.get("title"),
            "author": runtime_book.get("author"),
        },
        "metadata_audit_updates": metadata_audit_updates,
        "worker_extensions_preserved": any(
            key.startswith("worker_") for key in refreshed_manifest
        ),
        "runtime_refresh": runtime_refresh,
    }


def living_book_bundle_dir(book: dict[str, Any], knowledge_base_root: Optional[str] = None) -> Path:
    mapped_bundle = living_book_mapped_bundle_dir(book, knowledge_base_root)
    if mapped_bundle is not None:
        return mapped_bundle
    pending_bundle = living_book_pending_bundle_dir(book, knowledge_base_root)
    if pending_bundle.exists():
        return pending_bundle
    legacy_bundle = living_book_legacy_bundle_dir(book, knowledge_base_root)
    if legacy_bundle.exists():
        return legacy_bundle
    v2_bundle = living_book_v2_bundle_dir(book, knowledge_base_root)
    if v2_bundle.exists():
        return v2_bundle
    return v2_bundle


def living_book_bundle_layout_status(kb_root: Path, bundle_dir: Path) -> str:
    try:
        relative = bundle_dir.relative_to(kb_root)
    except ValueError:
        return "external"
    parts = relative.parts
    if len(parts) >= 2 and parts[0] == "LivingBooks" and parts[1] == "books":
        return "legacy_transition"
    if parts and parts[0] == "00_待分类":
        return "pending_classification"
    if len(parts) >= 2 and parts[0] == "30_Resources" and parts[1] == "Books":
        return "knowledgebase_v2"
    return "classified_library"


def initial_living_book_taxonomy() -> dict[str, Any]:
    return {
        "schema": "click.living_books.taxonomy.v1",
        "updated_at": now_iso(),
        "categories": DEFAULT_LIVING_BOOK_TAXONOMY,
    }


def living_book_default_index() -> dict[str, Any]:
    return {
        "schema": "click.living_books.book_category_index.v1",
        "updated_at": now_iso(),
        "books": [],
    }


def living_book_default_pending_categories() -> dict[str, Any]:
    return {
        "schema": "click.living_books.pending_categories.v1",
        "updated_at": now_iso(),
        "items": [],
    }


def living_book_default_jobs() -> dict[str, Any]:
    return {
        "schema": "click.living_books.jobs.v1",
        "updated_at": now_iso(),
        "jobs": [],
    }


def ensure_living_book_control_files(lb_root: Path) -> dict[str, Any]:
    index_dir = living_book_index_dir(lb_root)
    jobs_path = living_book_jobs_path(lb_root)
    index_dir.mkdir(parents=True, exist_ok=True)
    jobs_path.parent.mkdir(parents=True, exist_ok=True)

    taxonomy_path = index_dir / "taxonomy.json"
    pending_path = index_dir / "pending_categories.json"
    category_index_path = index_dir / "book_category_index.json"
    topics_path = index_dir / "主题-书籍对应表.md"

    if not taxonomy_path.exists():
        write_living_book_json_unprotected(taxonomy_path, initial_living_book_taxonomy())
    if not pending_path.exists():
        write_living_book_json_unprotected(pending_path, living_book_default_pending_categories())
    if not category_index_path.exists():
        write_living_book_json_unprotected(category_index_path, living_book_default_index())
    if not jobs_path.exists():
        write_living_book_json_unprotected(jobs_path, living_book_default_jobs())
    reconcile_legacy_living_book_jobs(lb_root)
    if not topics_path.exists():
        topics_path.write_text(
            "# 主题-书籍对应表\n\n"
            "> 由 Click Living Books index 生成。分类建议先写索引；物理迁移必须人工确认。\n\n"
            "暂无书籍分类记录。\n",
            encoding="utf-8",
        )

    return {
        "taxonomy": str(taxonomy_path),
        "pending_categories": str(pending_path),
        "book_category_index": str(category_index_path),
        "topics_markdown": str(topics_path),
        "jobs": str(jobs_path),
    }


def category_by_id(taxonomy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item.get("category_id")): item for item in taxonomy.get("categories") or []}


def active_taxonomy_categories(taxonomy: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in taxonomy.get("categories") or [] if item.get("status") == "active"]


def category_path_objects(taxonomy: dict[str, Any], category_id: str) -> list[dict[str, str]]:
    by_id = category_by_id(taxonomy)
    current = by_id.get(category_id)
    path: list[dict[str, str]] = []
    seen: set[str] = set()
    while current:
        current_id = str(current.get("category_id") or "")
        if not current_id or current_id in seen:
            break
        seen.add(current_id)
        path.append(
            {
                "category_id": current_id,
                "name": str(current.get("name") or current_id),
                "directory_name": str(current.get("directory_name") or current.get("name") or current_id),
            }
        )
        parent_id = current.get("parent_id")
        current = by_id.get(str(parent_id)) if parent_id else None
    return list(reversed(path))


def category_names(path: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("name") or item.get("category_id") or "") for item in path if item]


LIVING_BOOK_PROCESSING_POLICIES = {
    "normal",
    "reference_only",
    "do_not_process",
    "incomplete_reference",
}


def validate_living_book_taxonomy_categories(categories: list[dict[str, Any]]) -> dict[str, Any]:
    if not categories:
        raise HTTPException(status_code=422, detail="taxonomy categories are empty")
    by_id: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for item in categories:
        category_id = str(item.get("category_id") or "").strip()
        if not category_id:
            errors.append("category_id is required")
            continue
        if category_id in by_id:
            errors.append(f"duplicate category_id: {category_id}")
            continue
        by_id[category_id] = item
        if int(item.get("level") or 0) not in {1, 2}:
            errors.append(f"invalid level for {category_id}")
        if not str(item.get("name") or "").strip():
            errors.append(f"name is required for {category_id}")
        if not str(item.get("directory_name") or "").strip():
            errors.append(f"directory_name is required for {category_id}")
        if item.get("status") not in {"active", "inactive"}:
            errors.append(f"invalid status for {category_id}")

    for category_id, item in by_id.items():
        level = int(item.get("level") or 0)
        parent_id = str(item.get("parent_id") or "").strip()
        if level == 1 and parent_id:
            errors.append(f"level-1 category cannot have parent: {category_id}")
        if level == 2:
            parent = by_id.get(parent_id)
            if parent is None:
                errors.append(f"missing parent for {category_id}: {parent_id}")
            elif int(parent.get("level") or 0) != 1:
                errors.append(f"parent must be level 1 for {category_id}")

    if errors:
        raise HTTPException(status_code=422, detail={"taxonomy_errors": errors})
    return {
        "category_count": len(by_id),
        "top_level_count": sum(1 for item in by_id.values() if int(item.get("level") or 0) == 1),
        "leaf_count": sum(1 for item in by_id.values() if int(item.get("level") or 0) == 2),
    }


def upgrade_living_book_classification_records_v2(
    lb_root: Path,
    taxonomy: dict[str, Any],
) -> dict[str, Any]:
    """Add the V2 processing policy to confirmed legacy index records without moving bundles."""
    index_path = living_book_index_dir(lb_root) / "book_category_index.json"
    index_payload = read_living_book_json(index_path, living_book_default_index())
    kb_root = lb_root.parent.resolve()
    active_ids = {str(item.get("category_id")) for item in active_taxonomy_categories(taxonomy)}
    migrated_book_ids: list[str] = []
    skipped: list[dict[str, str]] = []
    manifest_backups: list[str] = []
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")

    for entry in index_payload.get("books") or []:
        if entry.get("processing_policy") in LIVING_BOOK_PROCESSING_POLICIES:
            continue
        bundle_dir = Path(str(entry.get("bundle_dir") or "")).expanduser()
        try:
            bundle_dir.resolve().relative_to(kb_root)
        except (OSError, ValueError):
            skipped.append({"book_id": str(entry.get("book_id") or ""), "reason": "bundle_outside_knowledge_base"})
            continue
        manifest_path = bundle_dir / "book_manifest.json"
        if not manifest_path.is_file():
            skipped.append({"book_id": str(entry.get("book_id") or ""), "reason": "manifest_missing"})
            continue
        manifest = read_living_book_json(manifest_path, {})
        classification = manifest.get("classification") or {}
        if classification.get("status") != "confirmed":
            skipped.append({"book_id": str(entry.get("book_id") or ""), "reason": "classification_not_confirmed"})
            continue
        primary_path = classification.get("primary_category_path") or []
        primary_id = str((primary_path[-1] if primary_path else {}).get("category_id") or "")
        if primary_id not in active_ids or primary_id == "pending.unclassified":
            skipped.append({"book_id": str(entry.get("book_id") or ""), "reason": "invalid_primary_category"})
            continue

        backup_path = manifest_path.with_name(f"book_manifest.json.bak-taxonomy-v2-{timestamp}")
        shutil.copy2(manifest_path, backup_path)
        classification["processing_policy"] = "normal"
        classification["taxonomy_version"] = 2
        classification["v2_migrated_at"] = now_iso()
        manifest["classification"] = classification
        write_living_book_json_unprotected(manifest_path, manifest)
        entry["processing_policy"] = "normal"
        entry["updated_at"] = now_iso()
        migrated_book_ids.append(str(entry.get("book_id") or ""))
        manifest_backups.append(str(backup_path))

    index_backup_path: Optional[Path] = None
    if migrated_book_ids:
        index_backup_path = index_path.with_name(f"book_category_index.json.bak-taxonomy-v2-{timestamp}")
        shutil.copy2(index_path, index_backup_path)
        index_payload["updated_at"] = now_iso()
        write_living_book_json_unprotected(index_path, index_payload)
        render_living_book_topics_markdown(lb_root, index_payload)
    return {
        "migrated_count": len(migrated_book_ids),
        "migrated_book_ids": migrated_book_ids,
        "skipped": skipped,
        "index_backup_path": str(index_backup_path) if index_backup_path else None,
        "manifest_backups": manifest_backups,
    }


def apply_default_living_book_taxonomy(payload: LivingBookTaxonomyApplyDefaultRequest) -> dict[str, Any]:
    if payload.confirmation_text != "APPLY LIBRARY TAXONOMY V2":
        raise HTTPException(
            status_code=422,
            detail="confirmation_text must be APPLY LIBRARY TAXONOMY V2",
        )
    lb_root = living_books_root(payload.knowledge_base_root)
    paths = ensure_living_book_control_files(lb_root)
    validation = validate_living_book_taxonomy_categories(DEFAULT_LIVING_BOOK_TAXONOMY)
    taxonomy_path = living_book_index_dir(lb_root) / "taxonomy.json"
    backup_path: Optional[Path] = None
    if taxonomy_path.exists():
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = taxonomy_path.with_name(f"taxonomy.json.bak-{timestamp}")
        shutil.copy2(taxonomy_path, backup_path)
    taxonomy = {
        "schema": "click.living_books.taxonomy.v1",
        "version": 2,
        "updated_at": now_iso(),
        "evidence": payload.evidence or "User-approved library reclassification",
        "categories": DEFAULT_LIVING_BOOK_TAXONOMY,
    }
    write_living_book_json_unprotected(taxonomy_path, taxonomy)
    classification_migration = upgrade_living_book_classification_records_v2(lb_root, taxonomy)
    return {
        "ok": True,
        "schema": "click.living_books.taxonomy_apply.v1",
        "taxonomy_path": str(taxonomy_path),
        "backup_path": str(backup_path) if backup_path else None,
        "validation": validation,
        "control_files": paths,
        "taxonomy": taxonomy,
        "classification_migration": classification_migration,
    }


def infer_living_book_category(book: dict[str, Any], annotations: list[dict[str, Any]], taxonomy: dict[str, Any]) -> dict[str, Any]:
    text_parts = [
        str(book.get("title") or ""),
        str(book.get("author") or ""),
        " ".join(str(item.get("source_text") or "") for item in annotations[:20]),
        " ".join(str(item.get("note_text") or "") for item in annotations[:20]),
    ]
    haystack = " ".join(text_parts).lower()
    candidates = [
        (
            "faith_spiritual.training",
            ["晨兴", "训练", "特会", "itero", "hwmr", "半年度", "长老及负责弟兄"],
            0.9,
        ),
        (
            "faith_spiritual.prayer",
            ["prayer", "pray", "祷告", "祈祷", "交通", "亲近主"],
            0.82,
        ),
        (
            "faith_spiritual.bible_study",
            ["bible", "gospel", "grace", "ministry", "scripture", "christ", "神", "圣经", "经文", "生命读经", "福音"],
            0.78,
        ),
        (
            "tcm_health.acupuncture_meridians",
            ["针灸", "经络", "腧穴", "针刺"],
            0.9,
        ),
        (
            "tcm_health.herbs_formulas",
            ["中药学", "方剂", "本草", "配伍"],
            0.88,
        ),
        (
            "tcm_health.pharmacology_research",
            ["中药药理", "药理学", "实验研究"],
            0.88,
        ),
        (
            "tcm_health.diagnosis_clinical",
            ["中医诊断", "诊断学", "辨证", "临床"],
            0.86,
        ),
        (
            "tcm_health.classics",
            ["黄帝内经", "内经选读", "金匮要略", "伤寒论"],
            0.9,
        ),
        (
            "tcm_health.literature",
            ["中医文献", "医史", "医学史"],
            0.86,
        ),
        (
            "tcm_health.fundamentals",
            ["中医基础", "中医学基础"],
            0.86,
        ),
        (
            "business.product_user",
            ["用户", "需求", "产品经理", "产品方法", "商业模式"],
            0.82,
        ),
        (
            "business.economics_finance",
            ["经济学", "宏观经济", "金融", "财务", "理财"],
            0.82,
        ),
        (
            "business.review_execution",
            ["复盘", "打法", "执行", "经验转化"],
            0.8,
        ),
        (
            "business.management",
            ["管理", "组织", "团队", "领导力", "向上管理"],
            0.78,
        ),
        (
            "business.marketing",
            ["营销", "增长", "广告", "销售", "品牌"],
            0.76,
        ),
        (
            "business.strategy",
            ["strategy", "strategic", "competitive", "战略", "策略", "竞争", "决策"],
            0.72,
        ),
        (
            "projects_systems.marketplace_os",
            ["marketplace os", "亚马逊运营", "电商经营"],
            0.86,
        ),
        (
            "projects_systems.content_creation",
            ["配文集", "内容创作", "文案", "脚本"],
            0.78,
        ),
        (
            "literature_arts.comics_visual",
            ["漫画", "绘本", "桂宝"],
            0.84,
        ),
        (
            "literature_arts.classical_chinese",
            ["演示古典小说", "古典小说", "诗词"],
            0.78,
        ),
        (
            "cognition_learning.logic_reasoning",
            ["逻辑", "论证", "推理", "批判性思维"],
            0.84,
        ),
        (
            "cognition_learning.psychology",
            ["人格心理", "社会心理", "发展心理"],
            0.82,
        ),
        (
            "technology_ai.ai_tools",
            ["ai", "artificial intelligence", "software", "automation", "code", "人工智能", "软件", "自动化", "编程"],
            0.7,
        ),
        (
            "cognition_learning.learning_methods",
            ["learning", "reading", "skill", "habit", "decision", "学习", "阅读", "技能", "习惯", "判断", "认知"],
            0.68,
        ),
    ]
    active_ids = {str(item.get("category_id")) for item in active_taxonomy_categories(taxonomy)}
    for category_id, keywords, confidence in candidates:
        if category_id in active_ids and any(keyword in haystack for keyword in keywords):
            return {
                "category_id": category_id,
                "confidence": confidence,
                "tags": [keyword for keyword in keywords if keyword in haystack][:5],
                "pending_category_suggestions": [],
            }
    return {
        "category_id": "pending.unclassified",
        "confidence": 0.36,
        "tags": ["待人工细分"],
        "pending_category_suggestions": [
            {
                "suggested_path": ["待人工确认"],
                "reason": "No precise active taxonomy match was found; use general reading until reviewed.",
            }
        ],
    }


def default_living_book_classification(generated_at: str) -> dict[str, Any]:
    return {
        "status": "pending",
        "primary_category_path": [],
        "secondary_category_paths": [],
        "tags": [],
        "confidence": 0,
        "pending_category_suggestions": [],
        "generated_by": "click_living_books",
        "generated_at": generated_at,
        "needs_review": True,
    }


def update_living_book_category_index(lb_root: Path, book: dict[str, Any], bundle_dir: Path, classification: dict[str, Any]) -> dict[str, Any]:
    index_path = living_book_index_dir(lb_root) / "book_category_index.json"
    index_payload = read_living_book_json(index_path, living_book_default_index())
    books = [item for item in index_payload.get("books") or [] if item.get("book_id") != book.get("id")]
    primary_path = category_names(classification.get("primary_category_path") or [])
    secondary_paths = [category_names(path) for path in classification.get("secondary_category_paths") or []]
    books.append(
        {
            "book_id": book.get("id"),
            "book_slug": living_book_slug(book),
            "bundle_dir": str(bundle_dir),
            "layout_status": living_book_bundle_layout_status(lb_root.parent, bundle_dir),
            "title": book.get("title"),
            "author": book.get("author"),
            "primary_category_path": primary_path,
            "secondary_category_paths": secondary_paths,
            "tags": classification.get("tags") or [],
            "status": classification.get("status") or "draft",
            "processing_policy": classification.get("processing_policy") or "normal",
            "updated_at": now_iso(),
        }
    )
    index_payload["updated_at"] = now_iso()
    index_payload["books"] = sorted(books, key=lambda item: str(item.get("title") or item.get("book_slug") or ""))
    write_living_book_json_unprotected(index_path, index_payload)
    render_living_book_topics_markdown(lb_root, index_payload)
    return index_payload


def update_living_book_pending_categories(lb_root: Path, book: dict[str, Any], suggestions: list[dict[str, Any]]) -> dict[str, Any]:
    pending_path = living_book_index_dir(lb_root) / "pending_categories.json"
    payload = read_living_book_json(pending_path, living_book_default_pending_categories())
    existing = [
        item
        for item in payload.get("items") or []
        if item.get("book_id") != book.get("id") or item.get("status") != "pending_review"
    ]
    for suggestion in suggestions:
        suggested_path = suggestion.get("suggested_path") or ["待人工确认"]
        existing.append(
            {
                "pending_id": stable_id("pendingcat", book.get("id"), "/".join(suggested_path)),
                "book_id": book.get("id"),
                "suggested_path": suggested_path,
                "reason": suggestion.get("reason") or "No precise active taxonomy match.",
                "status": "pending_review",
                "created_at": now_iso(),
            }
        )
    payload["updated_at"] = now_iso()
    payload["items"] = existing
    write_living_book_json_unprotected(pending_path, payload)
    return payload


def render_living_book_topics_markdown(lb_root: Path, index_payload: dict[str, Any]) -> None:
    lines = [
        "# 主题-书籍对应表",
        "",
        "> 由 Click Living Books index 生成。分类建议先写索引；物理迁移必须人工确认。",
        "",
    ]
    books = index_payload.get("books") or []
    if not books:
        lines.append("暂无书籍分类记录。")
    for item in books:
        primary = " / ".join(item.get("primary_category_path") or []) or "待分类"
        secondary = "; ".join(" / ".join(path) for path in item.get("secondary_category_paths") or []) or "无"
        tags = "、".join(item.get("tags") or []) or "无"
        lines.extend(
            [
                f"## {markdown_line(item.get('title')) or markdown_line(item.get('book_slug'))}",
                "",
                f"- Book ID: `{markdown_line(item.get('book_id'))}`",
                f"- Slug: `{markdown_line(item.get('book_slug'))}`",
                f"- Primary: {primary}",
                f"- Secondary: {secondary}",
                f"- Tags: {tags}",
                f"- Status: `{markdown_line(item.get('status'))}`",
                f"- Processing policy: `{markdown_line(item.get('processing_policy')) or 'normal'}`",
                "",
            ]
        )
    (living_book_index_dir(lb_root) / "主题-书籍对应表.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def read_living_book_jobs(lb_root: Path) -> dict[str, Any]:
    return read_living_book_json(living_book_jobs_path(lb_root), living_book_default_jobs())


def write_living_book_jobs(lb_root: Path, payload: dict[str, Any]) -> None:
    payload["updated_at"] = now_iso()
    write_living_book_json_unprotected(living_book_jobs_path(lb_root), payload)


def reconcile_legacy_living_book_jobs(lb_root: Path) -> dict[str, Any]:
    payload = read_living_book_json(living_book_jobs_path(lb_root), living_book_default_jobs())
    jobs = payload.get("jobs") or []
    changed = 0
    for job in jobs:
        legacy_job = job.get("job_type") == "classify_and_generate_drafts" and not job.get("manual_request")
        if not legacy_job:
            continue
        state = str(job.get("status") or "")
        if state in {"waiting_delay", "queued", "pending"}:
            job.update(
                {
                    "status": "not_requested",
                    "policy_transition": "policy_changed_to_manual",
                    "run_after": None,
                    "updated_at": now_iso(),
                }
            )
            changed += 1
        elif state == "running":
            job["policy_transition"] = "finish_current_run_then_manual_only"
            job["updated_at"] = now_iso()
            changed += 1
    if changed:
        payload["jobs"] = jobs
        write_living_book_jobs(lb_root, payload)
    return {"changed": changed, "jobs": jobs}


def living_book_due_at(minutes: int = 5) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def parse_living_book_time(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def upsert_living_book_job(lb_root: Path, book: dict[str, Any], bundle_dir: Path, *, force: bool = False, requested_by: str = "click_user") -> dict[str, Any]:
    jobs_payload = read_living_book_jobs(lb_root)
    jobs = jobs_payload.get("jobs") or []
    job_id = stable_id("lbjob", book.get("id"), "manual_hermes_qwen_analysis")
    existing = next((item for item in jobs if item.get("job_id") == job_id), None)
    if existing and existing.get("status") in {"queued", "running"}:
        return existing
    if existing and existing.get("status") in LIVING_BOOK_JOB_TERMINAL_STATUSES and not force:
        return existing
    if existing:
        existing.update(
            {
                "book_slug": living_book_slug(book),
                "job_type": "analyze_book_with_hermes_qwen",
                "run_after": None,
                "status": "queued",
                "manual_request": True,
                "requested_by": requested_by,
                "last_error": None,
                "updated_at": now_iso(),
            }
        )
        job = existing
    else:
        job = {
            "job_id": job_id,
            "book_id": book.get("id"),
            "book_slug": living_book_slug(book),
            "job_type": "analyze_book_with_hermes_qwen",
            "run_after": None,
            "status": "queued",
            "manual_request": True,
            "requested_by": requested_by,
            "attempt_count": 0,
            "last_error": None,
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        jobs.append(job)
    jobs_payload["jobs"] = jobs
    write_living_book_jobs(lb_root, jobs_payload)
    return job


def update_living_book_job(lb_root: Path, job_id: str, **updates: Any) -> dict[str, Any]:
    jobs_payload = read_living_book_jobs(lb_root)
    jobs = jobs_payload.get("jobs") or []
    target: dict[str, Any] = {}
    for job in jobs:
        if job.get("job_id") == job_id:
            job.update(updates)
            job["updated_at"] = now_iso()
            target = job
            break
    jobs_payload["jobs"] = jobs
    write_living_book_jobs(lb_root, jobs_payload)
    return target


def write_living_book_status(
    bundle_dir: Path,
    book: dict[str, Any],
    *,
    classification_status: str,
    hermes_draft_status: str,
    last_job_id: Optional[str] = None,
    last_error: Optional[str] = None,
) -> dict[str, Any]:
    payload = {
        "schema": "click.living_book.status.v1",
        "book_id": book.get("id"),
        "book_slug": living_book_slug(book),
        "bundle_dir": str(bundle_dir),
        "bundle_status": "generated" if bundle_dir.exists() else "missing",
        "classification_status": classification_status,
        "hermes_draft_status": hermes_draft_status,
        "last_job_id": last_job_id,
        "last_error": last_error,
        "updated_at": now_iso(),
    }
    write_living_book_json_unprotected(living_book_status_path(bundle_dir), payload)
    return payload


def living_book_status_payload(book: dict[str, Any], bundle_dir: Path, lb_root: Path) -> dict[str, Any]:
    ensure_living_book_control_files(lb_root)
    manifest = read_living_book_json(bundle_dir / "book_manifest.json", {})
    status = read_living_book_json(living_book_status_path(bundle_dir), {})
    analysis = read_living_book_json(
        living_book_analysis_path(bundle_dir),
        default_living_book_analysis(str(book.get("id") or ""), bundle_dir),
    )
    jobs = read_living_book_jobs(lb_root).get("jobs") or []
    book_jobs = [item for item in jobs if item.get("book_id") == book.get("id")]
    kb_root = lb_root.parent
    thinking_protocol_path = bundle_dir / "5_书籍思想模型" / "思维协议.md"
    reading_strategy_path = bundle_dir / "5_书籍思想模型" / "阅读策略.md"
    dialogue_rules_path = bundle_dir / "6_Hermes调用" / "思想对话规则.md"
    reading_coach_mode_path = bundle_dir / "6_Hermes调用" / "阅读导师模式.md"
    return {
        "ok": True,
        "schema": "click.living_book.status_response.v1",
        "book_id": book.get("id"),
        "book_slug": living_book_slug(book),
        "bundle_dir": str(bundle_dir),
        "layout_status": living_book_bundle_layout_status(kb_root, bundle_dir),
        "bundle_exists": bundle_dir.exists(),
        "manifest_exists": (bundle_dir / "book_manifest.json").exists(),
        "classification": manifest.get("classification"),
        "thinking_protocol_exists": thinking_protocol_path.exists(),
        "reading_strategy_exists": reading_strategy_path.exists(),
        "dialogue_rules_exists": dialogue_rules_path.exists(),
        "reading_coach_mode_exists": reading_coach_mode_path.exists(),
        "thinking_protocol_path": str(thinking_protocol_path),
        "reading_strategy_path": str(reading_strategy_path),
        "dialogue_rules_path": str(dialogue_rules_path),
        "reading_coach_mode_path": str(reading_coach_mode_path),
        "status": status or None,
        "analysis": analysis,
        "jobs": book_jobs,
        "index_paths": ensure_living_book_control_files(lb_root),
    }


def run_living_book_classification(book_id: str, *, knowledge_base_root: Optional[str] = None) -> dict[str, Any]:
    book = living_book_book_row(book_id)
    lb_root = living_books_root(knowledge_base_root)
    ensure_living_book_control_files(lb_root)
    bundle_dir = living_book_bundle_dir(book, knowledge_base_root)
    if not (bundle_dir / "book_manifest.json").exists():
        generate_living_book_bundle(book_id, knowledge_base_root=knowledge_base_root)
    manifest_path = bundle_dir / "book_manifest.json"
    manifest = read_living_book_json(manifest_path, {})
    taxonomy = read_living_book_json(living_book_index_dir(lb_root) / "taxonomy.json", initial_living_book_taxonomy())
    annotations = living_book_annotations(book_id)
    inferred = infer_living_book_category(book, annotations, taxonomy)
    primary_path = category_path_objects(taxonomy, inferred["category_id"])
    secondary_paths: list[list[dict[str, str]]] = []
    classification = {
        "status": "draft",
        "primary_category_path": primary_path,
        "secondary_category_paths": secondary_paths,
        "tags": inferred.get("tags") or [],
        "confidence": inferred.get("confidence", 0),
        "pending_category_suggestions": inferred.get("pending_category_suggestions") or [],
        "generated_by": "click_living_books_classifier",
        "generated_at": now_iso(),
        "needs_review": True,
    }
    manifest["classification"] = classification
    write_living_book_json_unprotected(manifest_path, manifest)
    update_living_book_category_index(lb_root, book, bundle_dir, classification)
    update_living_book_pending_categories(lb_root, book, classification["pending_category_suggestions"])
    status = write_living_book_status(
        bundle_dir,
        book,
        classification_status="draft",
        hermes_draft_status=str(living_book_analysis_state_for_book(book_id, knowledge_base_root=knowledge_base_root, create=True).get("state") or "not_requested"),
        last_job_id=None,
    )
    return {
        "ok": True,
        "schema": "click.living_book.classification_result.v1",
        "book_id": book_id,
        "bundle_dir": str(bundle_dir),
        "classification": classification,
        "status": status,
    }


def confirm_living_book_classification(
    book_id: str,
    payload: LivingBookClassificationConfirmRequest,
) -> dict[str, Any]:
    book = living_book_book_row(book_id)
    lb_root = living_books_root(payload.knowledge_base_root)
    ensure_living_book_control_files(lb_root)
    bundle_dir = living_book_bundle_dir(book, payload.knowledge_base_root)
    if not (bundle_dir / "book_manifest.json").exists():
        generate_living_book_bundle(book_id, knowledge_base_root=payload.knowledge_base_root)
    manifest_path = bundle_dir / "book_manifest.json"
    manifest = read_living_book_json(manifest_path, {})
    if not manifest:
        raise HTTPException(status_code=409, detail="Living Book manifest is missing")

    taxonomy_path = living_book_index_dir(lb_root) / "taxonomy.json"
    taxonomy = read_living_book_json(taxonomy_path, initial_living_book_taxonomy())
    validate_living_book_taxonomy_categories(list(taxonomy.get("categories") or []))
    by_id = category_by_id(taxonomy)

    def active_leaf(category_id: str) -> dict[str, Any]:
        category = by_id.get(category_id)
        if category is None:
            raise HTTPException(status_code=422, detail=f"unknown category_id: {category_id}")
        if category.get("status") != "active":
            raise HTTPException(status_code=422, detail=f"inactive category_id: {category_id}")
        if int(category.get("level") or 0) != 2:
            raise HTTPException(status_code=422, detail=f"category must be a level-2 leaf: {category_id}")
        return category

    primary_category_id = str(payload.primary_category_id or "").strip()
    active_leaf(primary_category_id)
    if primary_category_id == "pending.unclassified":
        raise HTTPException(status_code=422, detail="a confirmed classification cannot remain pending")

    secondary_category_ids: list[str] = []
    for raw_category_id in payload.secondary_category_ids:
        category_id = str(raw_category_id or "").strip()
        if not category_id or category_id == primary_category_id or category_id in secondary_category_ids:
            continue
        active_leaf(category_id)
        if category_id == "pending.unclassified":
            raise HTTPException(status_code=422, detail="pending cannot be a secondary confirmed category")
        secondary_category_ids.append(category_id)
    if len(secondary_category_ids) > 3:
        raise HTTPException(status_code=422, detail="at most 3 secondary categories are allowed")

    processing_policy = str(payload.processing_policy or "normal").strip()
    if processing_policy not in LIVING_BOOK_PROCESSING_POLICIES:
        raise HTTPException(
            status_code=422,
            detail={"invalid_processing_policy": processing_policy, "allowed": sorted(LIVING_BOOK_PROCESSING_POLICIES)},
        )

    tags: list[str] = []
    seen_tags: set[str] = set()
    for raw_tag in payload.tags:
        tag = re.sub(r"\s+", " ", str(raw_tag or "")).strip()[:32]
        if tag and tag not in seen_tags:
            tags.append(tag)
            seen_tags.add(tag)
    tags = tags[:12]
    generated_at = now_iso()
    classification = {
        "status": "confirmed",
        "primary_category_path": category_path_objects(taxonomy, primary_category_id),
        "secondary_category_paths": [
            category_path_objects(taxonomy, category_id) for category_id in secondary_category_ids
        ],
        "tags": tags,
        "confidence": 1,
        "pending_category_suggestions": [],
        "processing_policy": processing_policy,
        "generated_by": "user_confirmed_taxonomy_v2",
        "generated_at": generated_at,
        "confirmed_at": generated_at,
        "evidence": payload.evidence or "User-confirmed library reclassification",
        "needs_review": False,
    }
    manifest["classification"] = classification
    write_living_book_json_unprotected(manifest_path, manifest)
    update_living_book_category_index(lb_root, book, bundle_dir, classification)
    update_living_book_pending_categories(lb_root, book, [])
    analysis_state = living_book_analysis_state_for_book(
        book_id,
        knowledge_base_root=payload.knowledge_base_root,
        create=True,
    )
    status = write_living_book_status(
        bundle_dir,
        book,
        classification_status="confirmed",
        hermes_draft_status=str(analysis_state.get("state") or "not_requested"),
        last_job_id=None,
    )
    organization = update_library_book_organization(
        book_id,
        LibraryOrganizationPatch(
            custom_category=" / ".join(category_names(classification["primary_category_path"])),
            tags=tags,
        ),
        source="user_confirmed_taxonomy_v2",
    )
    return {
        "ok": True,
        "schema": "click.living_book.classification_confirmed.v1",
        "book_id": book_id,
        "bundle_dir": str(bundle_dir),
        "manifest_path": str(manifest_path),
        "classification": classification,
        "status": status,
        "organization": organization.get("organization"),
    }


def living_book_draft_header(title: str, generated_at: str) -> list[str]:
    return [
        f"# {title}",
        "",
        "status: draft",
        "generated_by: click_living_books_static_template",
        "needs_review: true",
        "source_coverage: partial",
        f"generated_at: {generated_at}",
        "",
        "> P1.1 自动草稿。它只整理 Click manifest、分类和已导出批注，不等于最终书籍思想模型。",
        "",
    ]


def render_living_book_summary_draft(book: dict[str, Any], manifest: dict[str, Any], annotations: list[dict[str, Any]], generated_at: str) -> str:
    title = markdown_line(book.get("title")) or markdown_line(book.get("id"))
    classification = manifest.get("classification") or {}
    primary = " / ".join(category_names(classification.get("primary_category_path") or [])) or "待分类"
    lines = living_book_draft_header("全书摘要", generated_at)
    lines.extend(
        [
            f"- 书名：{title}",
            f"- 作者：{markdown_line(book.get('author')) or '未知作者'}",
            f"- 当前主分类：{primary}",
            f"- 批注数量：{len(annotations)}",
            "",
            "## 草稿摘要",
            "",
            "这份摘要目前只基于书籍 manifest、分类结果和已导出的批注生成。Hermes 或用户后续需要结合原书与融合阅读继续补全。",
            "",
        ]
    )
    if annotations:
        lines.extend(["## 已有批注线索", ""])
        for item in annotations[:12]:
            source = markdown_line(item.get("source_text"))
            note = markdown_line(item.get("note_text"))
            lines.append(f"- {source[:160] if source else '无原文摘录'}")
            if note:
                lines.append(f"  - 我的备注：{note[:160]}")
        lines.append("")
    return "\n".join(lines)


def render_living_book_concepts_draft(annotations: list[dict[str, Any]], generated_at: str) -> str:
    lines = living_book_draft_header("核心概念", generated_at)
    lines.extend(["## 候选概念", ""])
    if not annotations:
        lines.extend(["暂无批注线索。后续应由 Hermes 读取原书或融合阅读后补充。", ""])
        return "\n".join(lines)
    words: dict[str, int] = {}
    for item in annotations:
        text = f"{item.get('source_text') or ''} {item.get('note_text') or ''}".lower()
        for token in re.findall(r"[\w\u4e00-\u9fff]{2,}", text):
            if len(token) > 24:
                continue
            words[token] = words.get(token, 0) + 1
    for token, count in sorted(words.items(), key=lambda pair: (-pair[1], pair[0]))[:20]:
        lines.append(f"- {token}（出现 {count} 次）")
    lines.append("")
    return "\n".join(lines)


def render_living_book_notes_overview_draft(annotations: list[dict[str, Any]], generated_at: str) -> str:
    lines = living_book_draft_header("我的备注总览", generated_at)
    if not annotations:
        lines.extend(["暂无批注。", ""])
        return "\n".join(lines)
    by_kind: dict[str, int] = {}
    for item in annotations:
        kind = str(item.get("kind") or "unknown")
        by_kind[kind] = by_kind.get(kind, 0) + 1
    lines.extend(["## 统计", ""])
    for kind, count in sorted(by_kind.items()):
        lines.append(f"- {kind}: {count}")
    lines.extend(["", "## 备注摘录", ""])
    for item in annotations[:30]:
        note = markdown_line(item.get("note_text"))
        source = markdown_line(item.get("source_text"))
        if note:
            lines.append(f"- {note[:180]}")
        elif source:
            lines.append(f"- 标红：{source[:180]}")
    lines.append("")
    return "\n".join(lines)


def render_living_book_outline_draft(book: dict[str, Any], manifest: dict[str, Any], generated_at: str) -> str:
    title = markdown_line(book.get("title")) or markdown_line(book.get("id"))
    lines = living_book_draft_header("目录", generated_at)
    lines.extend(
        [
            f"- 书名：{title}",
            f"- 原书：`{markdown_line(manifest.get('canonical_source_file'))}`",
            "",
            "P1.1 暂不强行抽取全书目录。后续 Hermes 可通过 `book_manifest.json` 和原书文件补全正式目录草稿。",
            "",
        ]
    )
    return "\n".join(lines)


def render_living_book_thought_draft(title: str, body: str, generated_at: str) -> str:
    lines = living_book_draft_header(title, generated_at)
    lines.extend([body, ""])
    return "\n".join(lines)


def living_book_top_chapter_lines(annotations: list[dict[str, Any]], limit: int = 5) -> list[str]:
    counts: dict[str, int] = {}
    for item in annotations:
        chapter = markdown_line(item.get("chapter_title") or item.get("chapter_locator") or "未分类")
        counts[chapter] = counts.get(chapter, 0) + 1
    return [f"- {chapter}: {count} 条" for chapter, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:limit]]


def living_book_keyword_line(annotations: list[dict[str, Any]], limit: int = 12) -> str:
    counts: dict[str, int] = {}
    for item in annotations:
        text = f"{item.get('source_text') or ''} {item.get('note_text') or ''}"
        for token in re.findall(r"[\w\u4e00-\u9fff]{2,}", text):
            token = token.strip()
            if len(token) > 18:
                continue
            counts[token] = counts.get(token, 0) + 1
    keywords = [token for token, _ in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:limit]]
    return "、".join(keywords) if keywords else "暂无足够批注关键词"


def living_book_protocol_evidence_lines(bundle_dir: Path, annotations: list[dict[str, Any]], limit: int = 8) -> list[str]:
    lines: list[str] = []
    annotations_path = bundle_dir / "2_批注数据" / "批注.json"
    for item in annotations[:limit]:
        source = markdown_line(item.get("source_text"))
        note = markdown_line(item.get("note_text"))
        chapter = markdown_line(item.get("chapter_title") or item.get("chapter_locator"))
        if note:
            lines.append(f"- 用户批注：{note[:180]}（章节：{chapter or '未知'}，source_path: `{annotations_path}`）")
        elif source:
            lines.append(f"- 书中摘录：{source[:180]}（章节：{chapter or '未知'}，source_path: `{annotations_path}`）")
    fused_dir = bundle_dir / "3_融合阅读"
    if fused_dir.exists():
        for path in sorted(fused_dir.glob("*.md"))[:3]:
            lines.append(f"- 融合阅读文件：`{path}`")
    if not lines:
        lines.append(f"- 当前没有可用批注；只能引用 manifest 和原书路径（source_path: `{bundle_dir / 'book_manifest.json'}`）。")
    return lines


def living_book_protocol_draft_header(title: str, generated_at: str, phase: str = "P1.3") -> list[str]:
    return [
        f"# {title}",
        "",
        "status: draft",
        "generated_by: click_living_books",
        "needs_review: true",
        "source_coverage: partial",
        f"generated_at: {generated_at}",
        "",
        f"> {phase} 自动草稿。它只基于 Click manifest、批注和融合阅读元数据生成；用户审阅前不等于最终书籍思想模型。",
        "",
    ]


def render_living_book_thinking_protocol_draft(
    book: dict[str, Any],
    manifest: dict[str, Any],
    annotations: list[dict[str, Any]],
    bundle_dir: Path,
    generated_at: str,
) -> str:
    title = markdown_line(book.get("title")) or markdown_line(book.get("id"))
    classification = manifest.get("classification") if isinstance(manifest.get("classification"), dict) else {}
    primary = " / ".join(category_names(classification.get("primary_category_path") or [])) or "待分类"
    chapter_lines = living_book_top_chapter_lines(annotations)
    evidence_lines = living_book_protocol_evidence_lines(bundle_dir, annotations)
    keyword_line = living_book_keyword_line(annotations)
    lines = living_book_protocol_draft_header("思维协议", generated_at)
    lines.extend(
        [
            "## 协议用途",
            "",
            f"- 让 Hermes 按《{title}》这一册书的资料，评价一个具体想法、项目或行动方案。",
            "- 回答时必须分清：书中明确说过、按本书逻辑推演、Hermes 综合判断。",
            f"- 当前分类：{primary}；批注数量：{len(annotations)}。",
            "",
            "## 本书关注的核心问题",
            "",
            f"- 从已导出批注看，当前关键词线索：{keyword_line}。",
        ]
    )
    if chapter_lines:
        lines.extend(["- 批注集中章节：", *chapter_lines])
    else:
        lines.append("- 暂无批注集中章节，需先补充阅读证据。")
    lines.extend(
        [
            "",
            "## 本书判断问题的顺序",
            "",
            "- 先读取 `6_Hermes调用/思想对话规则.md` 和 `6_Hermes调用/书籍调用卡.md`。",
            "- 再读取 `5_书籍思想模型/思维协议.md` 与已生成的思想模型草稿。",
            "- 然后查 `2_批注数据/批注.json`、`2_批注数据/批注.md` 和 `3_融合阅读/`。",
            "- 最后把用户问题放进本书逻辑里，输出四段式判断。",
            "",
            "## 本书认为重要的证据",
            "",
            "- 书中原文摘录优先于模型推断。",
            "- 用户批注用于判断用户当时关注点，不能伪装成原书观点。",
            "- 融合阅读用于恢复上下文，不能替代原书证据。",
            "- manifest、调用卡和状态文件只证明资料来源与生成状态。",
            "",
            "## 本书的推演规则",
            "",
            "- `书中明确说过` 只放已有原文、批注导出或融合阅读可支持的内容。",
            "- `按本书逻辑推演` 可以从证据继续推，但必须写明这是推演。",
            "- `Hermes 综合判断` 可以结合用户当前目标和其他常识，但不得说成原书原意。",
            "- 若证据不足，要明确说缺哪类材料，并给下一步验证办法。",
            "",
            "## 本书不适合判断的问题",
            "",
            "- 不适合单独承担多书比较、跨学科最终结论或用户长期人生决策。",
            "- 不适合替代用户审阅；draft 文件只提供可执行草稿。",
            "- 不适合把本书逻辑强行套到完全无关的技术、法律、财务或医疗结论上。",
            "",
            "## 可引用证据",
            "",
            *evidence_lines,
            "",
            "## 使用边界",
            "",
            "- 必须在回答中保留证据路径或片段。",
            "- 必须明确区分书中明说、按书推演、Hermes 综合判断。",
            "- 不能把 reviewed 之前的草稿当成最终真理。",
            "- 如果用户问的是“这本书怎么看/评价/反驳”，优先按本协议组织回答。",
            "",
        ]
    )
    return "\n".join(lines)


def render_living_book_dialogue_rules_draft(
    book: dict[str, Any],
    manifest: dict[str, Any],
    annotations: list[dict[str, Any]],
    bundle_dir: Path,
    generated_at: str,
) -> str:
    title = markdown_line(book.get("title")) or markdown_line(book.get("id"))
    lines = living_book_protocol_draft_header("思想对话规则", generated_at)
    lines.extend(
        [
            "## 触发条件",
            "",
            f"- 用户说“按《{title}》/按这本书/这本书怎么看/这本书评价/这本书反驳”时触发。",
            "- 用户讨论自己的读书想法、项目想法或行动方案，并要求用这本书判断时触发。",
            "- 用户只要普通摘要、列书或查批注时，不强制进入四段式思想对话。",
            "",
            "## 读取顺序",
            "",
            "1. `6_Hermes调用/思想对话规则.md`",
            "2. `5_书籍思想模型/思维协议.md`",
            "3. `6_Hermes调用/书籍调用卡.md`",
            "4. `5_书籍思想模型/这本书怎么思考.md`、`这本书的核心主张.md`、`这本书的判断标准.md`、`这本书的盲区.md`",
            "5. `2_批注数据/批注.json`、`2_批注数据/批注.md`、`3_融合阅读/`",
            "",
            "## 回答格式",
            "",
            "## 书中明确说过",
            "",
            "只写有路径或片段支持的原书/批注/融合阅读证据。",
            "",
            "## 按本书逻辑推演",
            "",
            "基于上一段证据继续推理，明确标注这是推演。",
            "",
            "## Hermes 综合判断",
            "",
            "结合用户问题给直接判断，不把综合判断伪装成原书观点。",
            "",
            "## 下一步怎么验证",
            "",
            "给出用户下一步应回到哪段原文、哪条批注或哪个行动实验。",
            "",
            "## 禁止事项",
            "",
            "- 禁止整段搬运知识库结果代替回答。",
            "- 禁止把用户批注说成作者观点。",
            "- 禁止把模型推断写成“书中明确说”。",
            "- 禁止在没有证据路径时假装读过某段内容。",
            "",
            "## 证据要求",
            "",
            f"- 当前批注数量：{len(annotations)}。",
            f"- 必须优先返回 `source_path`，例如 `{bundle_dir / '2_批注数据' / '批注.json'}`。",
            "- 若引用融合阅读，必须给出具体 `.md` 路径。",
            "- 若证据不足，直接说明缺口，不编造。",
            "",
            "## 示例问题",
            "",
            f"- 按《{title}》的方式看我的想法。",
            "- 让这本书评价我的项目。",
            "- 这本书会怎么反驳我？",
            "- 我这样读书，和这本书的逻辑差距在哪里？",
            "",
        ]
    )
    return "\n".join(lines)


def living_book_analysis_excerpt(book: dict[str, Any], *, max_chars: int = 36000, chapter_limit: int = 10) -> dict[str, Any]:
    epub_path = epub_path_for_book(book)
    publication = epub_publication(epub_path, book=book)
    chapters = publication.get("chapters") or []
    if not chapters:
        return {"toc": publication.get("toc") or [], "chapters": [], "source_coverage": "none"}
    if len(chapters) <= chapter_limit:
        selected_indexes = list(range(len(chapters)))
    else:
        selected_indexes = sorted({round(index * (len(chapters) - 1) / (chapter_limit - 1)) for index in range(chapter_limit)})
    selected: list[dict[str, Any]] = []
    remaining = max_chars
    with zipfile.ZipFile(epub_path) as epub:
        for index in selected_indexes:
            chapter = chapters[index]
            try:
                markup = zip_text(epub, str(chapter.get("href") or ""))
            except KeyError:
                continue
            plain = lite_extract_text(markup)
            if not plain:
                continue
            allowance = max(1200, remaining // max(1, len(selected_indexes) - len(selected)))
            excerpt = plain[:allowance]
            remaining -= len(excerpt)
            selected.append(
                {
                    "chapter_index": index,
                    "title": chapter.get("title") or f"Chapter {index + 1}",
                    "href": chapter.get("href") or "",
                    "excerpt": excerpt,
                }
            )
            if remaining <= 0:
                break
    return {
        "toc": (publication.get("toc") or [])[:500],
        "chapters": selected,
        "source_coverage": "sampled_across_reading_order",
        "chapter_count": len(chapters),
        "sampled_chapter_count": len(selected),
    }


def call_living_book_qwen(book: dict[str, Any], context: dict[str, Any], annotations: list[dict[str, Any]]) -> dict[str, Any]:
    invocation_id = new_id("hermesqwen")
    chapter_blocks = []
    for item in context.get("chapters") or []:
        chapter_blocks.append(
            f"### {item.get('title')}\n来源：{item.get('href')}\n{item.get('excerpt')}"
        )
    annotation_blocks = []
    for item in annotations[:80]:
        source = str(item.get("source_text") or "").strip()
        note = str(item.get("note_text") or "").strip()
        if source or note:
            annotation_blocks.append(f"- 原文：{source[:500]}\n  用户备注：{note[:500]}")
    prompt = f"""
你是 Hermes 中负责 Living Book 的本地 Qwen 分析器。只能根据下面提供的书籍材料和用户批注工作，禁止补写没有证据的书中内容。

书名：{book.get('title') or ''}
作者：{book.get('author') or ''}
章节总数：{context.get('chapter_count') or 0}
抽样方式：{context.get('source_coverage') or 'none'}

目录：
{json.dumps(context.get('toc') or [], ensure_ascii=False)}

章节材料：
{chr(10).join(chapter_blocks)}

用户批注：
{chr(10).join(annotation_blocks) if annotation_blocks else '暂无用户批注'}

请只输出一个 JSON 对象，不要 Markdown。字段必须为：
- category_path: 1 到 3 个中文层级字符串数组
- tags: 3 到 8 个短标签数组
- summary: 基于材料的全书摘要；材料不足要明确说抽样范围
- outline: 字符串数组
- core_concepts: 字符串数组
- core_claims: 字符串数组
- thinking_rules: 字符串数组
- judgement_standards: 字符串数组
- blind_spots: 字符串数组
- reading_strategy: 字符串数组
- dialogue_rules: 字符串数组
- evidence: 对象数组，每项含 claim、source_href、source_quote
- confidence: 0 到 1
- source_coverage_note: 字符串
""".strip()
    payload = {
        "model": LIVING_BOOK_QWEN_MODEL,
        "messages": [
            {
                "role": "system",
                "content": "你是严谨的本地书籍分析器。只使用给定证据，严格输出 JSON，不创建 Hermes memory 或 session 资产。",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.15,
        "max_tokens": 3200,
        "stream": False,
    }
    request = URLRequest(
        f"{HERMES_MODEL_GATEWAY_BASE_URL}/v1/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Hermes-Project": "click-living-books",
            "X-Hermes-Source": "click-manual-book-analysis",
            "X-Hermes-Invocation": invocation_id,
        },
    )
    with urlopen(request, timeout=360) as response:
        raw = json.loads(response.read().decode("utf-8"))
    choices = raw.get("choices") if isinstance(raw, dict) else None
    content = str((((choices or [{}])[0].get("message") or {}).get("content") or ""))
    parsed = extract_json_object(content)
    if not isinstance(parsed, dict) or not str(parsed.get("summary") or "").strip():
        raise RuntimeError("Hermes Qwen did not return a valid Living Book JSON result")
    model = str(raw.get("model") or LIVING_BOOK_QWEN_MODEL)
    return {
        "schema": "click.living_book.hermes_qwen_receipt.v1",
        "generated_by": "hermes_qwen_model_gateway",
        "runtime": "hermes_model_gateway",
        "model": model,
        "invocation_id": invocation_id,
        "gateway_response_id": raw.get("id"),
        "usage": raw.get("usage") or {},
        "result": parsed,
    }


def _analysis_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def render_qwen_analysis_markdown(title: str, value: Any, receipt: dict[str, Any], *, source_coverage: str) -> str:
    lines = [
        f"# {title}",
        "",
        "status: draft",
        "generated_by: hermes_qwen_model_gateway",
        f"runtime: {receipt.get('runtime')}",
        f"model: {receipt.get('model')}",
        f"invocation_id: {receipt.get('invocation_id')}",
        "needs_review: true",
        f"source_coverage: {source_coverage}",
        "",
    ]
    if isinstance(value, str):
        lines.extend([value.strip() or "暂无可验证内容。", ""])
    elif isinstance(value, list):
        rows = _analysis_strings(value)
        lines.extend([*(f"- {row}" for row in rows), ""] if rows else ["暂无可验证内容。", ""])
    else:
        lines.extend([json.dumps(value, ensure_ascii=False, indent=2), ""])
    return "\n".join(lines)


def run_living_book_qwen_analysis(book_id: str, *, knowledge_base_root: Optional[str] = None) -> dict[str, Any]:
    book = living_book_book_row(book_id)
    bundle_dir = living_book_bundle_dir(book, knowledge_base_root)
    if not (bundle_dir / "book_manifest.json").exists():
        generate_living_book_bundle(book_id, knowledge_base_root=knowledge_base_root)
    lb_root = living_books_root(knowledge_base_root)
    analysis = living_book_analysis_state_for_book(book_id, knowledge_base_root=knowledge_base_root, create=True)
    job_id = str(analysis.get("job_id") or "")
    analysis.update({"state": "running", "started_at": now_iso(), "last_error": None})
    write_living_book_analysis(bundle_dir, analysis)
    if job_id:
        update_living_book_job(lb_root, job_id, status="running", last_error=None)
    context = living_book_analysis_excerpt(book)
    annotations = living_book_annotations(book_id)
    receipt = call_living_book_qwen(book, context, annotations)
    result = receipt["result"]
    source_coverage = str(result.get("source_coverage_note") or context.get("source_coverage") or "sampled")
    outputs = {
        "outline": write_living_book_draft(
            bundle_dir / "4_书籍整理" / "目录.md",
            render_qwen_analysis_markdown("目录", result.get("outline"), receipt, source_coverage=source_coverage),
        ),
        "summary": write_living_book_draft(
            bundle_dir / "4_书籍整理" / "全书摘要.md",
            render_qwen_analysis_markdown("全书摘要", result.get("summary"), receipt, source_coverage=source_coverage),
        ),
        "concepts": write_living_book_draft(
            bundle_dir / "4_书籍整理" / "核心概念.md",
            render_qwen_analysis_markdown("核心概念", result.get("core_concepts"), receipt, source_coverage=source_coverage),
        ),
        "core_claims": write_living_book_draft(
            bundle_dir / "5_书籍思想模型" / "这本书的核心主张.md",
            render_qwen_analysis_markdown("这本书的核心主张", result.get("core_claims"), receipt, source_coverage=source_coverage),
        ),
        "thinking_rules": write_living_book_draft(
            bundle_dir / "5_书籍思想模型" / "思维协议.md",
            render_qwen_analysis_markdown("思维协议", result.get("thinking_rules"), receipt, source_coverage=source_coverage),
        ),
        "judgement": write_living_book_draft(
            bundle_dir / "5_书籍思想模型" / "这本书的判断标准.md",
            render_qwen_analysis_markdown("这本书的判断标准", result.get("judgement_standards"), receipt, source_coverage=source_coverage),
        ),
        "blind_spots": write_living_book_draft(
            bundle_dir / "5_书籍思想模型" / "这本书的盲区.md",
            render_qwen_analysis_markdown("这本书的盲区", result.get("blind_spots"), receipt, source_coverage=source_coverage),
        ),
        "reading_strategy": write_living_book_draft(
            bundle_dir / "5_书籍思想模型" / "阅读策略.md",
            render_qwen_analysis_markdown("阅读策略", result.get("reading_strategy"), receipt, source_coverage=source_coverage),
        ),
        "dialogue_rules": write_living_book_draft(
            bundle_dir / "6_Hermes调用" / "思想对话规则.md",
            render_qwen_analysis_markdown("思想对话规则", result.get("dialogue_rules"), receipt, source_coverage=source_coverage),
        ),
        "evidence": write_living_book_json_unprotected(
            bundle_dir / "4_书籍整理" / "分析证据.json",
            {
                "schema": "click.living_book.analysis_evidence.v1",
                "invocation_id": receipt["invocation_id"],
                "items": result.get("evidence") if isinstance(result.get("evidence"), list) else [],
            },
        ),
        "receipt": write_living_book_json_unprotected(
            bundle_dir / "_status" / "hermes_qwen_analysis.json",
            receipt,
        ),
    }
    manifest_path = bundle_dir / "book_manifest.json"
    manifest = read_living_book_json(manifest_path, {})
    category_path = _analysis_strings(result.get("category_path"))[:3]
    manifest["classification"] = {
        "status": "draft",
        "primary_category_path": [
            {"category_id": f"hermes.{index + 1}", "name": name, "directory_name": name}
            for index, name in enumerate(category_path)
        ],
        "secondary_category_paths": [],
        "tags": _analysis_strings(result.get("tags"))[:8],
        "confidence": float(result.get("confidence") or 0),
        "generated_by": "hermes_qwen_model_gateway",
        "invocation_id": receipt["invocation_id"],
        "generated_at": now_iso(),
        "needs_review": True,
    }
    write_living_book_json_unprotected(manifest_path, manifest)
    update_living_book_category_index(lb_root, book, bundle_dir, manifest["classification"])
    analysis.update(
        {
            "state": "needs_review",
            "finished_at": now_iso(),
            "runtime": receipt["runtime"],
            "model": receipt["model"],
            "invocation_id": receipt["invocation_id"],
            "last_error": None,
            "outputs": outputs,
        }
    )
    analysis = write_living_book_analysis(bundle_dir, analysis)
    if job_id:
        update_living_book_job(
            lb_root,
            job_id,
            status="needs_review",
            last_error=None,
            attempt_count=int(next((item.get("attempt_count") or 0 for item in read_living_book_jobs(lb_root).get("jobs") or [] if item.get("job_id") == job_id), 0)) + 1,
        )
    write_living_book_status(
        bundle_dir,
        book,
        classification_status="draft",
        hermes_draft_status="needs_review",
        last_job_id=job_id or None,
    )
    return {
        "ok": True,
        "schema": "click.living_book.hermes_qwen_analysis_result.v1",
        "book_id": book_id,
        "bundle_dir": str(bundle_dir),
        "generated_by": receipt["generated_by"],
        "runtime": receipt["runtime"],
        "model": receipt["model"],
        "invocation_id": receipt["invocation_id"],
        "source_coverage": source_coverage,
        "needs_review": True,
        "outputs": outputs,
        "analysis": analysis,
    }


def living_book_runtime_unavailable(exc: BaseException) -> bool:
    if isinstance(exc, (URLError, TimeoutError, ConnectionError)):
        return True
    message = f"{exc.__class__.__name__}: {exc}".lower()
    return any(
        marker in message
        for marker in (
            "connection refused",
            "connection reset",
            "failed to establish",
            "timed out",
            "timeout",
            "network is unreachable",
            "nodename nor servname",
        )
    )


def execute_living_book_analysis_job(
    book_id: str,
    job_id: str,
    *,
    knowledge_base_root: Optional[str] = None,
) -> dict[str, Any]:
    book = living_book_book_row(book_id)
    bundle_dir = living_book_bundle_dir(book, knowledge_base_root)
    lb_root = living_books_root(knowledge_base_root)
    try:
        return run_living_book_qwen_analysis(book_id, knowledge_base_root=knowledge_base_root)
    except Exception as exc:  # noqa: BLE001 - the persistent job owns retry/error state.
        waiting = living_book_runtime_unavailable(exc)
        state = "queued" if waiting else "failed"
        analysis = living_book_analysis_state_for_book(
            book_id,
            knowledge_base_root=knowledge_base_root,
            create=True,
        )
        analysis.update(
            {
                "state": state,
                "job_id": job_id,
                "last_error": f"{exc.__class__.__name__}: {exc}",
                "waiting_reason": "waiting_for_local_hermes" if waiting else None,
                "finished_at": None if waiting else now_iso(),
            }
        )
        analysis = write_living_book_analysis(bundle_dir, analysis)
        update_living_book_job(
            lb_root,
            job_id,
            status=state,
            last_error=analysis["last_error"],
            attempt_count=int(
                next(
                    (
                        item.get("attempt_count") or 0
                        for item in read_living_book_jobs(lb_root).get("jobs") or []
                        if item.get("job_id") == job_id
                    ),
                    0,
                )
            )
            + 1,
        )
        write_living_book_status(
            bundle_dir,
            book,
            classification_status="pending",
            hermes_draft_status=state,
            last_job_id=job_id,
            last_error=analysis["last_error"],
        )
        return {
            "ok": False,
            "book_id": book_id,
            "job_id": job_id,
            "status": state,
            "retryable": waiting,
            "analysis": analysis,
        }


def start_living_book_analysis_job(
    book_id: str,
    job_id: str,
    *,
    knowledge_base_root: Optional[str] = None,
) -> bool:
    with LIVING_BOOK_ANALYSIS_THREAD_LOCK:
        if job_id in LIVING_BOOK_ANALYSIS_THREADS:
            return False
        LIVING_BOOK_ANALYSIS_THREADS.add(job_id)

    def runner() -> None:
        try:
            execute_living_book_analysis_job(
                book_id,
                job_id,
                knowledge_base_root=knowledge_base_root,
            )
        finally:
            with LIVING_BOOK_ANALYSIS_THREAD_LOCK:
                LIVING_BOOK_ANALYSIS_THREADS.discard(job_id)

    threading.Thread(
        target=runner,
        daemon=True,
        name=f"living-book-analysis-{job_id}",
    ).start()
    return True


def queue_living_book_analysis(
    book_id: str,
    *,
    knowledge_base_root: Optional[str] = None,
    requested_by: str = "click_user",
    force: bool = False,
    start_async: bool = True,
) -> dict[str, Any]:
    book = living_book_book_row(book_id)
    bundle_dir = living_book_bundle_dir(book, knowledge_base_root)
    if not (bundle_dir / "book_manifest.json").exists():
        generate_living_book_bundle(book_id, knowledge_base_root=knowledge_base_root)
    analysis = living_book_analysis_state_for_book(
        book_id,
        knowledge_base_root=knowledge_base_root,
        create=True,
    )
    state = str(analysis.get("state") or "not_requested")
    if state in {"queued", "running"}:
        return {
            "ok": True,
            "accepted": False,
            "duplicate": True,
            "book_id": book_id,
            "job_id": analysis.get("job_id"),
            "analysis": analysis,
        }
    if state in {"needs_review", "complete"} and not force:
        raise HTTPException(status_code=409, detail="Book analysis already exists; use reanalyze with explicit confirmation")

    lb_root = living_books_root(knowledge_base_root)
    ensure_living_book_control_files(lb_root)
    job = upsert_living_book_job(
        lb_root,
        book,
        bundle_dir,
        force=force,
        requested_by=requested_by,
    )
    analysis.update(
        {
            "state": "queued",
            "job_id": job.get("job_id"),
            "requested_at": now_iso(),
            "started_at": None,
            "finished_at": None,
            "requested_by": requested_by,
            "runtime": None,
            "model": None,
            "invocation_id": None,
            "last_error": None,
            "waiting_reason": None,
        }
    )
    analysis = write_living_book_analysis(bundle_dir, analysis)
    write_living_book_status(
        bundle_dir,
        book,
        classification_status="pending",
        hermes_draft_status="queued",
        last_job_id=str(job.get("job_id") or "") or None,
    )
    started = False
    if start_async:
        started = start_living_book_analysis_job(
            book_id,
            str(job.get("job_id") or ""),
            knowledge_base_root=knowledge_base_root,
        )
    return {
        "ok": True,
        "accepted": True,
        "duplicate": False,
        "book_id": book_id,
        "job_id": job.get("job_id"),
        "worker_started": started,
        "analysis": analysis,
    }


def living_book_reading_mode_recommendation(annotations: list[dict[str, Any]]) -> tuple[str, str]:
    note_count = sum(1 for item in annotations if str(item.get("note_text") or "").strip())
    highlight_count = len(annotations) - note_count
    if note_count >= 8:
        return "项目化阅读 / 模型提取", "你已经留下较多自己的备注，下一步不该停在摘要，而应把本书转成判断模型、行动规则或项目输出。"
    if len(annotations) >= 12:
        return "精读关键章节", "你已有足够标注线索，应先回到高密度章节做二次阅读，而不是从头平均通读。"
    if highlight_count >= 5:
        return "快读后补问题", "当前多为摘录线索，先补充问题和反对点，再决定是否精读。"
    return "扫读 / 问题化阅读", "当前证据偏少，先确认这本书是否真的服务你的问题，再投入深读。"


def render_living_book_reading_strategy_draft(
    book: dict[str, Any],
    manifest: dict[str, Any],
    annotations: list[dict[str, Any]],
    bundle_dir: Path,
    generated_at: str,
) -> str:
    title = markdown_line(book.get("title")) or markdown_line(book.get("id"))
    classification = manifest.get("classification") if isinstance(manifest.get("classification"), dict) else {}
    primary = " / ".join(category_names(classification.get("primary_category_path") or [])) or "待分类"
    mode, mode_reason = living_book_reading_mode_recommendation(annotations)
    chapter_lines = living_book_top_chapter_lines(annotations, limit=6)
    evidence_lines = living_book_protocol_evidence_lines(bundle_dir, annotations, limit=6)
    keyword_line = living_book_keyword_line(annotations, limit=14)
    note_count = sum(1 for item in annotations if str(item.get("note_text") or "").strip())
    lines = living_book_protocol_draft_header("阅读策略", generated_at, phase="P1.4")
    lines.extend(
        [
            "## 策略用途",
            "",
            f"- 帮 Hermes 判断用户应该如何读《{title}》，以及这本书该转成什么输出。",
            "- 这个文件用于阅读策略教练，不用于替代原书、批注或最终人工审阅。",
            f"- 当前分类：{primary}；批注数量：{len(annotations)}；备注数量：{note_count}。",
            "",
            "## 推荐阅读模式",
            "",
            f"- 建议模式：{mode}",
            f"- 理由：{mode_reason}",
            "",
            "## 不建议的读法",
            "",
            "- 不建议平均通读后只做普通摘要。",
            "- 不建议只收藏金句，不回答“它如何改变我的判断”。",
            "- 不建议把用户批注等同于作者观点。",
            "- 不建议在证据不足时强行输出成熟思想模型。",
            "",
            "## 用户已经有的线索",
            "",
            f"- 关键词线索：{keyword_line}。",
        ]
    )
    if chapter_lines:
        lines.extend(["- 批注集中章节：", *chapter_lines])
    lines.extend(
        [
            "",
            "## 知识差距清单",
            "",
            "- 缺少：用户当前阅读目的和要服务的项目/能力。",
            "- 缺少：哪些观点已被用户验证，哪些只是被触动。",
            "- 缺少：反对点、疑问点和可迁移场景。",
            "- 缺少：读完后要沉淀成模型、项目规则、内容选题还是行动实验。",
            "",
            "## 章节优先级",
            "",
        ]
    )
    if chapter_lines:
        lines.extend(chapter_lines)
    else:
        lines.append("- 暂无章节优先级；先从目录和少量批注建立问题清单。")
    lines.extend(
        [
            "",
            "## 阅读问题",
            "",
            "- 这本书真正要解决的核心问题是什么？",
            "- 哪些内容已经被用户批注证明有触动？",
            "- 用户原来的读法哪里太平均、太摘要化或太空泛？",
            "- 哪些章节应该精读，哪些章节只需要扫读？",
            "- 读完之后应沉淀成一个判断模型、项目规则、反例清单还是行动实验？",
            "",
            "## 输出物建议",
            "",
            "- 一个“本书如何判断问题”的模型卡。",
            "- 一个“我和这本书的差距”清单。",
            "- 一个可执行的 7 天或 14 天实践问题表。",
            "- 一个可迁移到项目或生活决策的规则草案。",
            "",
            "## 可引用证据",
            "",
            *evidence_lines,
            "",
            "## 使用边界",
            "",
            "- 阅读策略必须根据用户问题动态调整，不能把本草稿当死规则。",
            "- 如果用户没有给阅读目的，Hermes 应先用现有批注推断一个临时目标，并明确这是临时判断。",
            "- 如果用户给出弱读法，Hermes 应直接挑战，但必须说明证据和理由。",
            "",
        ]
    )
    return "\n".join(lines)


def render_living_book_reading_coach_mode_draft(
    book: dict[str, Any],
    manifest: dict[str, Any],
    annotations: list[dict[str, Any]],
    bundle_dir: Path,
    generated_at: str,
) -> str:
    title = markdown_line(book.get("title")) or markdown_line(book.get("id"))
    mode, mode_reason = living_book_reading_mode_recommendation(annotations)
    lines = living_book_protocol_draft_header("阅读导师模式", generated_at, phase="P1.4")
    lines.extend(
        [
            "## 触发条件",
            "",
            f"- 用户问“我该怎么读《{title}》/这本书应该怎么读/我这样读对吗”。",
            "- 用户问“我和这本书的差距在哪里/这本书该用于哪个项目或能力”。",
            "- 用户给出一个阅读计划，希望 Hermes 挑战、修正或排序。",
            "",
            "## 读取顺序",
            "",
            "1. `6_Hermes调用/阅读导师模式.md`",
            "2. `5_书籍思想模型/阅读策略.md`",
            "3. `5_书籍思想模型/思维协议.md`",
            "4. `6_Hermes调用/思想对话规则.md` 和 `书籍调用卡.md`",
            "5. `2_批注数据/批注.json`、`2_批注数据/批注.md`、`3_融合阅读/`",
            "",
            "## 回答格式",
            "",
            "## 你的读法判断",
            "",
            "直接判断用户当前读法强在哪里、弱在哪里。弱读法要明确挑战，不要只鼓励。",
            "",
            "## 应该怎么读",
            "",
            f"默认建议：{mode}。理由：{mode_reason}",
            "",
            "## 你和这本书的差距",
            "",
            "列出用户已经知道什么、缺什么、误用或低估了什么。",
            "",
            "## 章节和问题优先级",
            "",
            "给出先读哪些章节/片段，以及用哪些问题检验。",
            "",
            "## 读完要沉淀什么",
            "",
            "必须建议输出物：模型卡、项目规则、行动清单、反例清单、内容选题或实践实验。",
            "",
            "## 禁止事项",
            "",
            "- 禁止把阅读导师模式变成普通摘要。",
            "- 禁止无差别建议精读全书。",
            "- 禁止不看批注就判断用户差距。",
            "- 禁止把模型综合判断伪装成原书明说。",
            "",
            "## 证据要求",
            "",
            f"- 当前批注数量：{len(annotations)}。",
            f"- 必须优先返回 `source_path`，例如 `{bundle_dir / '5_书籍思想模型' / '阅读策略.md'}`。",
            "- 需要区分书中内容、用户批注、阅读策略草稿和 Hermes 综合判断。",
            "",
            "## 示例问题",
            "",
            f"- 我想读《{title}》，应该怎么读？",
            "- 我这样读对吗？",
            "- 我和这本书要求的思维差距在哪里？",
            "- 这本书应该转成什么模型或行动？",
            "",
        ]
    )
    return "\n".join(lines)


def write_living_book_draft(path: Path, text: str) -> dict[str, Any]:
    if has_reviewed_marker(path):
        target = path.with_name(f"{path.stem}.new{path.suffix}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text.rstrip() + "\n", encoding="utf-8")
        return {
            "path": str(target),
            "written": True,
            "reason": "reviewed_file_not_overwritten_draft_written_new",
            "protected_path": str(path),
        }
    return write_living_book_text(path, text, protect_reviewed=True)


def run_living_book_draft_generation(
    book_id: str,
    *,
    knowledge_base_root: Optional[str] = None,
    use_hermes_runtime: bool = False,
) -> dict[str, Any]:
    if not use_hermes_runtime:
        raise HTTPException(status_code=409, detail="Living Book analysis requires an explicit manual Hermes/Qwen request")
    return run_living_book_qwen_analysis(book_id, knowledge_base_root=knowledge_base_root)

    # Legacy static generator retained below only as unreachable migration context.
    book = living_book_book_row(book_id)
    lb_root = living_books_root(knowledge_base_root)
    ensure_living_book_control_files(lb_root)
    bundle_dir = living_book_bundle_dir(book, knowledge_base_root)
    if not (bundle_dir / "book_manifest.json").exists():
        generate_living_book_bundle(book_id, knowledge_base_root=knowledge_base_root)
    classification_result = run_living_book_classification(book_id, knowledge_base_root=knowledge_base_root)
    manifest = read_living_book_json(bundle_dir / "book_manifest.json", {})
    annotations = living_book_annotations(book_id)
    generated_at = now_iso()
    outputs = {
        "outline": write_living_book_draft(
            bundle_dir / "4_书籍整理" / "目录.md",
            render_living_book_outline_draft(book, manifest, generated_at),
        ),
        "summary": write_living_book_draft(
            bundle_dir / "4_书籍整理" / "全书摘要.md",
            render_living_book_summary_draft(book, manifest, annotations, generated_at),
        ),
        "concepts": write_living_book_draft(
            bundle_dir / "4_书籍整理" / "核心概念.md",
            render_living_book_concepts_draft(annotations, generated_at),
        ),
        "notes_overview": write_living_book_draft(
            bundle_dir / "4_书籍整理" / "我的备注总览.md",
            render_living_book_notes_overview_draft(annotations, generated_at),
        ),
        "thinking": write_living_book_draft(
            bundle_dir / "5_书籍思想模型" / "这本书怎么思考.md",
            render_living_book_thought_draft(
                "这本书怎么思考",
                "请后续结合原书、批注和融合阅读，将本书转化成可复用的思考方法，而不是普通摘要。",
                generated_at,
            ),
        ),
        "core_claim": write_living_book_draft(
            bundle_dir / "5_书籍思想模型" / "这本书的核心主张.md",
            render_living_book_thought_draft(
                "这本书的核心主张",
                "当前为占位草稿。正式版本需要区分原书明说、按书中逻辑推导、以及 Hermes 综合判断。",
                generated_at,
            ),
        ),
        "judgement": write_living_book_draft(
            bundle_dir / "5_书籍思想模型" / "这本书的判断标准.md",
            render_living_book_thought_draft(
                "这本书的判断标准",
                "当前为占位草稿。后续应整理这本书如何判断重要、正确、风险和行动优先级。",
                generated_at,
            ),
        ),
        "blind_spots": write_living_book_draft(
            bundle_dir / "5_书籍思想模型" / "这本书的盲区.md",
            render_living_book_thought_draft(
                "这本书的盲区",
                "当前为占位草稿。后续应记录本书没有覆盖、可能过时或需要其他书互补的部分。",
                generated_at,
            ),
        ),
        "thinking_protocol": write_living_book_draft(
            bundle_dir / "5_书籍思想模型" / "思维协议.md",
            render_living_book_thinking_protocol_draft(book, manifest, annotations, bundle_dir, generated_at),
        ),
        "reading_strategy": write_living_book_draft(
            bundle_dir / "5_书籍思想模型" / "阅读策略.md",
            render_living_book_reading_strategy_draft(book, manifest, annotations, bundle_dir, generated_at),
        ),
        "dialogue_rules": write_living_book_draft(
            bundle_dir / "6_Hermes调用" / "思想对话规则.md",
            render_living_book_dialogue_rules_draft(book, manifest, annotations, bundle_dir, generated_at),
        ),
        "reading_coach_mode": write_living_book_draft(
            bundle_dir / "6_Hermes调用" / "阅读导师模式.md",
            render_living_book_reading_coach_mode_draft(book, manifest, annotations, bundle_dir, generated_at),
        ),
        "call_card": write_living_book_draft(
            bundle_dir / "6_Hermes调用" / "书籍调用卡.md",
            render_hermes_call_card(book)
            + "\n\n## P1.1 生成状态\n\nstatus: draft\ngenerated_by: click_living_books_static_template\nneeds_review: true\nsource_coverage: partial\n",
        ),
    }
    hermes_runtime_status = "not_requested"
    if use_hermes_runtime:
        hermes_runtime_status = "legacy_static_pipeline_disabled"
    job = upsert_living_book_job(lb_root, book, bundle_dir)
    update_living_book_job(
        lb_root,
        str(job.get("job_id")),
        status="needs_review",
        last_error=None,
        attempt_count=int(job.get("attempt_count") or 0) + 1,
    )
    status = write_living_book_status(
        bundle_dir,
        book,
        classification_status="draft",
        hermes_draft_status="needs_review",
        last_job_id=str(job.get("job_id")),
    )
    return {
        "ok": True,
        "schema": "click.living_book.hermes_draft_result.v1",
        "book_id": book_id,
        "bundle_dir": str(bundle_dir),
        "generated_by": "click_living_books_static_template",
        "source_coverage": "partial",
        "needs_review": True,
        "hermes_runtime_status": hermes_runtime_status,
        "classification": classification_result.get("classification"),
        "outputs": outputs,
        "status": status,
    }


def run_due_living_book_jobs(*, knowledge_base_root: Optional[str] = None, limit: int = 20, force: bool = False) -> dict[str, Any]:
    lb_root = living_books_root(knowledge_base_root)
    ensure_living_book_control_files(lb_root)
    jobs_payload = read_living_book_jobs(lb_root)
    results: list[dict[str, Any]] = []
    processed = 0
    for job in jobs_payload.get("jobs") or []:
        if processed >= limit:
            break
        if job.get("job_type") != "analyze_book_with_hermes_qwen" or not job.get("manual_request"):
            continue
        status = str(job.get("status") or "")
        if status == "running":
            continue
        if status != "queued" and not (force and status in {"failed", "needs_review", "complete"}):
            continue
        result = execute_living_book_analysis_job(
            str(job.get("book_id") or ""),
            str(job.get("job_id") or ""),
            knowledge_base_root=knowledge_base_root,
        )
        results.append({"job_id": job.get("job_id"), "status": (result.get("analysis") or {}).get("state"), "result": result})
        processed += 1
    return {
        "ok": True,
        "schema": "click.living_books.jobs_run_result.v1",
        "processed": processed,
        "results": results,
    }


def living_book_existing_reviewed_placeholders(bundle_dir: Path) -> list[dict[str, Any]]:
    placeholders = {
        "5_书籍思想模型/这本书怎么思考.md": "# 这本书怎么思考\n\nstatus: draft\n\nP1 自动预留。请在后续人工或 Hermes 审阅后补充。\n",
        "5_书籍思想模型/这本书的核心主张.md": "# 这本书的核心主张\n\nstatus: draft\n\nP1 自动预留。请在后续人工或 Hermes 审阅后补充。\n",
        "5_书籍思想模型/这本书的判断标准.md": "# 这本书的判断标准\n\nstatus: draft\n\nP1 自动预留。请在后续人工或 Hermes 审阅后补充。\n",
        "5_书籍思想模型/这本书的盲区.md": "# 这本书的盲区\n\nstatus: draft\n\nP1 自动预留。请在后续人工或 Hermes 审阅后补充。\n",
        "5_书籍思想模型/思维协议.md": "# 思维协议\n\nstatus: draft\ngenerated_by: click_living_books\nneeds_review: true\nsource_coverage: partial\n\nP1.3 自动预留。请在后续生成草稿或人工审阅后补充。\n",
        "5_书籍思想模型/阅读策略.md": "# 阅读策略\n\nstatus: draft\ngenerated_by: click_living_books\nneeds_review: true\nsource_coverage: partial\n\nP1.4 自动预留。请在后续生成草稿或人工审阅后补充。\n",
        "6_Hermes调用/思想对话规则.md": "# 思想对话规则\n\nstatus: draft\ngenerated_by: click_living_books\nneeds_review: true\nsource_coverage: partial\n\nP1.3 自动预留。请在后续生成草稿或人工审阅后补充。\n",
        "6_Hermes调用/阅读导师模式.md": "# 阅读导师模式\n\nstatus: draft\ngenerated_by: click_living_books\nneeds_review: true\nsource_coverage: partial\n\nP1.4 自动预留。请在后续生成草稿或人工审阅后补充。\n",
    }
    results = []
    for relative_path, text in placeholders.items():
        target = bundle_dir / relative_path
        if target.exists():
            continue
        results.append(write_living_book_text(target, text, protect_reviewed=True))
    return results


def generate_living_book_bundle(book_id: str, *, knowledge_base_root: Optional[str] = None) -> dict[str, Any]:
    book = living_book_book_row(book_id)
    source_kind = str(book.get("source_kind") or book.get("file_kind") or "").lower()
    if source_kind not in {"epub", "pdf"}:
        raise HTTPException(status_code=422, detail="Living Books P1 currently supports EPUB and PDF books")

    source_file_value = str(book.get("file_path") or "")
    if not source_file_value:
        raise HTTPException(status_code=422, detail="book has no source file path")
    source_file = Path(source_file_value).expanduser()
    if not source_file.exists():
        raise HTTPException(status_code=404, detail=f"book source file missing: {source_file}")

    compatibility: dict[str, Any] = {}
    pdf_preflight: dict[str, Any] = {}
    if source_kind == "epub":
        compatibility = ensure_book_epub_assets(book)
    else:
        pdf_preflight = inspect_pdf(source_file)

    generated_at = now_iso()
    kb_root = knowledge_base_root_path(knowledge_base_root)
    lb_root = living_books_root(str(kb_root))
    control_paths = ensure_living_book_control_files(lb_root)
    bundle_dir = living_book_bundle_dir(book, str(kb_root))
    original_dir = bundle_dir / "1_原书"
    dirs = [
        original_dir,
        bundle_dir / "2_批注数据",
        bundle_dir / "3_融合阅读",
        bundle_dir / "4_书籍整理",
        bundle_dir / "5_书籍思想模型",
        bundle_dir / "6_Hermes调用",
        bundle_dir / "7_对话复盘",
        bundle_dir / "_status",
    ]
    for directory in dirs:
        directory.mkdir(parents=True, exist_ok=True)

    canonical_source = original_dir / living_book_safe_filename(book, source_file)
    copied_source = False
    if source_file.resolve() != canonical_source.resolve():
        if not canonical_source.exists() or file_sha256(canonical_source) != file_sha256(source_file):
            shutil.copy2(source_file, canonical_source)
            copied_source = True
    source_hash = str(book.get("file_hash") or "") or file_sha256(canonical_source)
    byte_size = canonical_source.stat().st_size
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO reader.book_files (id, book_id, file_path, file_kind, file_hash, byte_size)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (book_id, file_path) DO UPDATE
            SET file_kind = EXCLUDED.file_kind,
                file_hash = EXCLUDED.file_hash,
                byte_size = EXCLUDED.byte_size
            """,
            (
                stable_id("file", book["id"], str(canonical_source)),
                book["id"],
                str(canonical_source),
                source_kind,
                source_hash,
                byte_size,
            ),
        )

    annotations = living_book_annotations(book_id)
    paths = {
        "book_home": "0_书籍首页.md",
        "original_dir": "1_原书",
        "canonical_source_file": relative_to_bundle(bundle_dir, canonical_source),
        "annotations_json": "2_批注数据/批注.json",
        "annotations_markdown": "2_批注数据/批注.md",
        "fused_reading_dir": "3_融合阅读",
        "book_notes_dir": "4_书籍整理",
        "thought_model_dir": "5_书籍思想模型",
        "hermes_dir": "6_Hermes调用",
        "dialogue_review_dir": "7_对话复盘",
    }
    existing_manifest = read_living_book_json(bundle_dir / "book_manifest.json", {})
    existing_classification = existing_manifest.get("classification") or default_living_book_classification(generated_at)
    migration_entry = living_book_migration_entry(book, str(kb_root)) or {}
    identity = living_book_identity(book, str(kb_root))
    layout_status = living_book_bundle_layout_status(kb_root, bundle_dir)
    manifest_schema = (
        "jiangyu.knowledge.book_manifest.v2"
        if layout_status == "knowledgebase_v2"
        else "click.living_book.manifest.v1"
    )
    normalized_source_hash = f"sha256:{source_hash}" if not str(source_hash).startswith("sha256:") else source_hash
    existing_lifecycle = dict(existing_manifest.get("lifecycle") or {})
    existing_analysis_state = str(existing_lifecycle.get("analysis_state") or "")
    existing_review_state = str(existing_lifecycle.get("review_state") or "")
    verified_worker_extractions = [
        value
        for key, value in existing_manifest.items()
        if re.fullmatch(r"worker_[a-z0-9_]+_model_extraction", str(key))
        and isinstance(value, dict)
        and value.get("source_sha256_verified") is True
        and bool(value.get("generated_files"))
    ]
    if verified_worker_extractions and existing_review_state == "in_review" and existing_analysis_state in {"", "not_requested"}:
        existing_analysis_state = "draft_ready"
    manifest = {
        **existing_manifest,
        "schema": manifest_schema,
        "manifest_version": 2 if manifest_schema == "jiangyu.knowledge.book_manifest.v2" else 1,
        "work_id": identity["work_id"],
        "edition_id": identity["edition_id"],
        "book_id": book["id"],
        "book_slug": living_book_slug(book),
        "title": book.get("title"),
        "author": str(book.get("author") or ""),
        "language": "unknown",
        "source_kind": book.get("source_kind"),
        "file_hash": normalized_source_hash,
        "canonical_source_file": paths["canonical_source_file"],
        "runtime_owner": "click_reader",
        "annotation_primary_store": "reader.annotations",
        "audio_note_primary_store": "reader.audio_notes",
        "source": {
            "kind": source_kind,
            "content_hash": normalized_source_hash,
            "canonical_file": paths["canonical_source_file"],
            "provenance": [
                {
                    "path_at_import": str(source_file),
                    "observed_at": generated_at,
                    "retained": True,
                }
            ],
        },
        "ownership": {
            "publication_owner": "knowledge_base",
            "runtime_owner": "click_reader",
            "runtime_reader": "click_reader",
            "annotation_primary_store": "reader.annotations",
            "audio_note_primary_store": "reader.audio_notes",
            "hermes_role": "authorized_processor",
        },
        "generated_at": generated_at,
        "canonical_bundle_dir": str(bundle_dir),
        "library_layout": {
            "schema": "jiangyu.knowledge_base.library_layout.v2",
            "status": layout_status,
            "control_root": str(lb_root),
            "final_classified_path_requires_user_confirmation": True,
        },
        "provenance": {
            "source_file_at_sync": str(source_file),
            "copied_to_canonical_source": copied_source,
        },
        "paths": paths,
        "privacy": {
            "local_only": True,
            "git_publish_allowed": False,
        },
        "classification": existing_classification,
        "analysis_policy": {
            "default": "not_requested",
            "automatic_analysis_after_import": False,
            "manual_action_required": True,
            "processor": "hermes_qwen_local",
        },
        "lifecycle": {
            "status": str(existing_lifecycle.get("status") or "captured"),
            "analysis_state": (
                existing_analysis_state
                if existing_analysis_state in LIVING_BOOK_ANALYSIS_STATES
                else "not_requested"
            ),
            "review_state": (
                existing_review_state
                if existing_review_state in {"unreviewed", "in_review", "reviewed"}
                else "unreviewed"
            ),
        },
        "legacy": {
            "read_compatible": (
                bool(migration_entry.get("legacy_read_compatible"))
                if layout_status == "knowledgebase_v2"
                else True
            ),
            "legacy_bundle_path": migration_entry.get("legacy_bundle_path"),
            "legacy_manifest_schema": (
                migration_entry.get("legacy_manifest_schema")
                or (existing_manifest.get("legacy") or {}).get("legacy_manifest_schema")
                or (
                    existing_manifest.get("schema")
                    if existing_manifest.get("schema") != "jiangyu.knowledge.book_manifest.v2"
                    else None
                )
            ),
            "migration_state": migration_entry.get("migration_state"),
            "migration_map": str(living_book_migration_map_path(str(kb_root))),
        },
    }
    if source_kind == "epub":
        manifest["epub_compatibility"] = {
            "schema": compatibility.get("schema") or EPUB_COMPATIBILITY_REPORT_SCHEMA,
            "reading_profile": compatibility.get("reading_profile") or "UNKNOWN",
            "toc_depth_counts": compatibility.get("toc_depth_counts") or {},
            "display_variants_available": bool((compatibility.get("display_variants") or {}).get("available")),
            "correction_queue_required": bool(compatibility.get("correction_queue_required")),
            "original_epub_modified": bool(compatibility.get("original_epub_modified")),
        }
    else:
        manifest["pdf_preflight"] = {
            "schema": pdf_preflight.get("schema") or PDF_PREFLIGHT_SCHEMA,
            "document_profile": pdf_preflight.get("document_profile") or "unknown",
            "page_count": int(pdf_preflight.get("page_count") or 0),
            "text_page_count": int(pdf_preflight.get("text_page_count") or 0),
            "scanned_page_count": int(pdf_preflight.get("scanned_page_count") or 0),
            "encrypted": bool(pdf_preflight.get("encrypted")),
            "unlocked": bool(pdf_preflight.get("unlocked")),
            "source_pdf_modified": False,
            "capabilities": pdf_preflight.get("capabilities") or {},
        }

    outputs: dict[str, Any] = {}
    outputs["manifest"] = write_living_book_json(bundle_dir / "book_manifest.json", manifest, protect_reviewed=False)
    book_home_path = bundle_dir / "0_书籍首页.md"
    outputs["book_home"] = write_living_book_text(
        book_home_path,
        preserve_book_home_extensions(book_home_path, render_book_home(book, manifest)),
    )

    annotation_payload = {
        "schema": "click.living_book.annotations_export.v1",
        "generated_at": generated_at,
        "book": {
            "id": book.get("id"),
            "title": book.get("title"),
            "author": book.get("author"),
            "source_kind": book.get("source_kind"),
            "book_hash": book.get("book_hash"),
        },
        "primary_store": {
            "annotations": "reader.annotations",
            "audio_notes": "reader.audio_notes",
        },
        "annotation_count": len(annotations),
        "annotations": annotations,
        "export_note": "Generated export for KnowledgeBase/Hermes. Do not treat this JSON as the runtime annotation source of truth.",
    }
    outputs["annotations_json"] = write_living_book_json(bundle_dir / paths["annotations_json"], annotation_payload)
    outputs["annotations_markdown"] = write_living_book_text(
        bundle_dir / paths["annotations_markdown"],
        render_living_annotations_markdown(book, annotations, generated_at),
    )

    by_chapter: dict[str, list[dict[str, Any]]] = {}
    for item in annotations:
        chapter_key = str(item.get("chapter_title") or item.get("chapter_locator") or "chapter")
        by_chapter.setdefault(chapter_key, []).append(item)
    fused_outputs = []
    for index, (chapter, chapter_annotations) in enumerate(sorted(by_chapter.items()), start=1):
        chapter_slug = safe_slug(chapter) or f"chapter-{index:03d}"
        target = bundle_dir / "3_融合阅读" / f"{index:03d}_{chapter_slug}_融合阅读.md"
        fused_outputs.append(write_living_book_text(target, render_fused_chapter(book, chapter, chapter_annotations, generated_at)))
    outputs["fused_reading"] = fused_outputs

    hermes_manifest = {
        "schema": "click.living_book.hermes_manifest.v1",
        "book_id": book["id"],
        "work_id": identity["work_id"],
        "edition_id": identity["edition_id"],
        "book_slug": living_book_slug(book),
        "call_card": "书籍调用卡.md",
        "book_manifest": "../book_manifest.json",
        "authorization": {
            "schema": "click.living_book.hermes_authorization.v1",
            "grantee": "hermes-ai-gateway",
            "access": "read_only",
            "allowed_paths": [
                "../book_manifest.json",
                "../0_书籍首页.md",
                "../2_批注数据/批注.json",
                "../2_批注数据/批注.md",
                "../3_融合阅读",
                "../4_书籍整理",
                "../5_书籍思想模型",
                "书籍调用卡.md",
                "思想对话规则.md",
                "阅读导师模式.md",
            ],
            "forbidden_sinks": [
                "hermes_memory",
                "hermes_session",
                "hermes_repo",
                "marketplace_os_database",
            ],
            "requires_explicit_mode": True,
        },
        "evidence_policy": {
            "must_distinguish_source_quote": True,
            "must_distinguish_book_logic_inference": True,
            "must_distinguish_hermes_synthesis": True,
            "draft_models_are_not_final": True,
        },
        "read_modes": {
            "book_summary": ["../0_书籍首页.md", "../4_书籍整理"],
            "user_reading_memory": ["../2_批注数据/批注.json", "../2_批注数据/批注.md", "../4_书籍整理/我的备注总览.md"],
            "living_book_dialogue": [
                "书籍调用卡.md",
                "思想对话规则.md",
                "../5_书籍思想模型/思维协议.md",
                "../5_书籍思想模型",
                "../3_融合阅读",
                "../2_批注数据",
            ],
            "reading_strategy_coach": [
                "阅读导师模式.md",
                "../5_书籍思想模型/阅读策略.md",
                "../5_书籍思想模型/思维协议.md",
                "思想对话规则.md",
                "书籍调用卡.md",
                "../2_批注数据/批注.json",
                "../2_批注数据/批注.md",
                "../3_融合阅读",
            ],
        },
    }
    outputs["hermes_manifest"] = write_living_book_json(
        bundle_dir / "6_Hermes调用" / "hermes_manifest.json",
        hermes_manifest,
    )
    outputs["call_card"] = write_living_book_text(
        bundle_dir / "6_Hermes调用" / "书籍调用卡.md",
        render_hermes_call_card(book),
    )
    outputs["thought_model_placeholders"] = living_book_existing_reviewed_placeholders(bundle_dir)
    if source_kind == "epub":
        compatibility_export = dict(compatibility)
        compatibility_export.pop("source_file", None)
        outputs["epub_compatibility"] = write_living_book_json_unprotected(
            bundle_dir / "_status" / "epub_compatibility.json",
            compatibility_export,
        )
    else:
        pdf_export = dict(pdf_preflight)
        pdf_export.pop("source_file", None)
        outputs["pdf_preflight"] = write_living_book_json_unprotected(
            bundle_dir / "_status" / "pdf_preflight.json",
            pdf_export,
        )
    analysis = read_living_book_json(living_book_analysis_path(bundle_dir), {})
    if analysis.get("state") not in LIVING_BOOK_ANALYSIS_STATES:
        analysis = write_living_book_analysis(bundle_dir, default_living_book_analysis(book_id, bundle_dir))
    elif analysis.get("bundle_dir") != str(bundle_dir):
        analysis = write_living_book_analysis(bundle_dir, analysis)
    outputs["status"] = write_living_book_status(
        bundle_dir,
        book,
        classification_status=str(existing_classification.get("status") or "pending"),
        hermes_draft_status=str(analysis.get("state") or "not_requested"),
        last_job_id=str(analysis.get("job_id") or "") or None,
    )

    return {
        "ok": True,
        "schema": "click.living_book.sync_result.v1",
        "knowledge_base_root": str(kb_root),
        "living_books_root": str(lb_root),
        "control_paths": control_paths,
        "book_id": book["id"],
        "book_slug": living_book_slug(book),
        "bundle_dir": str(bundle_dir),
        "layout_status": living_book_bundle_layout_status(kb_root, bundle_dir),
        "canonical_source_file": str(canonical_source),
        "annotation_count": len(annotations),
        "fused_chapter_count": len(fused_outputs),
        "annotation_primary_store": "reader.annotations",
        "audio_note_primary_store": "reader.audio_notes",
        "background_job": None,
        "analysis": analysis,
        "outputs": outputs,
        "privacy": manifest["privacy"],
    }


def insert_export_record(conn: Any, book_id: str, export_kind: str, output_path: str, annotation_count: int) -> dict[str, Any]:
    row = conn.execute(
        """
        INSERT INTO reader.exports (id, book_id, export_kind, output_path, annotation_count, created_at)
        VALUES (%s, %s, %s, %s, %s, now())
        RETURNING *
        """,
        (new_id("exp"), book_id, export_kind, output_path, annotation_count),
    ).fetchone()
    return dict(row)


def insert_sync_event(
    conn: Any,
    source_kind: str,
    source_id: str,
    target_system: str,
    payload: dict[str, Any],
    status: str = "pending",
    last_error: Optional[str] = None,
) -> dict[str, Any]:
    row = conn.execute(
        """
        INSERT INTO reader.sync_events (
            id, source_kind, source_id, target_system, payload, status, last_error, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now())
        RETURNING *
        """,
        (new_id("sync"), source_kind, source_id, target_system, db.jsonb(payload), status, last_error),
    ).fetchone()
    return dict(row)


def hermes_annotation_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = annotation_export_items(rows)
    for item in items:
        item["evidence_unit"] = {
            "source_sentence": item.get("source_text", ""),
            "note": item.get("note_text") or "",
            "locator": {
                "chapter_title": item.get("chapter_title") or "",
                "chapter_locator": item.get("chapter_locator") or "",
                "sentence_index": item.get("sentence_index") or "",
                "range_locator": item.get("range_locator") or {},
            },
        }
    return items


def build_hermes_sync_payload(book: dict[str, Any], annotations: list[dict[str, Any]], generated_at: str) -> dict[str, Any]:
    return {
        "schema": "sentence_reader.hermes_sync.v1",
        "generated_at": generated_at,
        "source_app": "Sentence Reader",
        "target_system": "hermes_cognitive_os",
        "book": book,
        "annotation_count": len(annotations),
        "annotations": annotations,
        "cognitive_contract": {
            "purpose": "Turn verified reading annotations into reusable Hermes/Cognitive OS source material.",
            "rules": [
                "Use source_sentence as evidence, not as decoration.",
                "Preserve chapter_locator and sentence_index when turning notes into model cards or cognitive rules.",
                "Do not claim the book supports an idea unless at least one annotation explicitly supports it.",
                "If a note is ambiguous, keep it as a question or hypothesis instead of a hard rule.",
            ],
        },
    }


def update_sync_event(conn: Any, sync_event_id: str, status: str, payload: dict[str, Any], last_error: Optional[str] = None) -> dict[str, Any]:
    row = conn.execute(
        """
        UPDATE reader.sync_events
        SET payload = %s,
            status = %s,
            last_error = %s,
            updated_at = now()
        WHERE id = %s
        RETURNING *
        """,
        (db.jsonb(payload), status, last_error, sync_event_id),
    ).fetchone()
    return dict(row)


LIVING_BOOK_EVIDENCE_SYNC_TARGET = "knowledge_base_living_book"
LIVING_BOOK_EVIDENCE_SYNC_THREAD_LOCK = threading.Lock()
LIVING_BOOK_EVIDENCE_SYNC_THREAD: Optional[threading.Thread] = None


def enqueue_living_book_evidence_sync(
    conn: Any,
    *,
    source_kind: str,
    source_id: str,
    book_id: str,
    operation: str,
    details: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return insert_sync_event(
        conn,
        source_kind,
        source_id,
        LIVING_BOOK_EVIDENCE_SYNC_TARGET,
        {
            "schema": "click.living_book.evidence_sync_event.v1",
            "book_id": book_id,
            "operation": operation,
            "source_kind": source_kind,
            "source_id": source_id,
            "queued_at": now_iso(),
            "knowledge_base_root": str(default_knowledge_base_root()),
            "details": details or {},
        },
    )


def export_living_book_evidence(
    book_id: str,
    *,
    knowledge_base_root: Optional[str] = None,
    source_event_ids: Optional[list[str]] = None,
) -> dict[str, Any]:
    book = living_book_book_row(book_id)
    bundle_dir = living_book_bundle_dir(book, knowledge_base_root)
    kb_root = knowledge_base_root_path(knowledge_base_root)
    manifest_path = bundle_dir / "book_manifest.json"
    manifest = read_living_book_json(manifest_path, {})
    identity = living_book_identity(book, knowledge_base_root)
    layout_status = living_book_bundle_layout_status(kb_root, bundle_dir)
    manifest_is_current = bool(manifest) and (
        layout_status != "knowledgebase_v2"
        or (
            manifest.get("schema") == "jiangyu.knowledge.book_manifest.v2"
            and manifest.get("canonical_bundle_dir") == str(bundle_dir)
            and manifest.get("work_id") == identity["work_id"]
            and manifest.get("edition_id") == identity["edition_id"]
        )
    )
    if not manifest_is_current:
        generated = generate_living_book_bundle(book_id, knowledge_base_root=knowledge_base_root)
        bundle_dir = Path(str(generated["bundle_dir"]))
        manifest_path = bundle_dir / "book_manifest.json"

    generated_at = now_iso()
    manifest = read_living_book_json(manifest_path, {})
    annotations = living_book_annotations(book_id)
    paths = manifest.get("paths") or {}
    annotations_json_path = bundle_dir / str(paths.get("annotations_json") or "2_批注数据/批注.json")
    annotations_markdown_path = bundle_dir / str(paths.get("annotations_markdown") or "2_批注数据/批注.md")
    annotation_payload = {
        "schema": "click.living_book.annotations_export.v1",
        "generated_at": generated_at,
        "book": {
            "id": book.get("id"),
            "title": book.get("title"),
            "author": book.get("author"),
            "source_kind": book.get("source_kind"),
            "book_hash": book.get("book_hash"),
        },
        "primary_store": {
            "annotations": "reader.annotations",
            "audio_notes": "reader.audio_notes",
        },
        "annotation_count": len(annotations),
        "annotations": annotations,
        "source_event_ids": source_event_ids or [],
        "export_note": "Generated evidence projection. PostgreSQL remains the runtime source of truth.",
    }
    outputs = {
        "annotations_json": write_living_book_json(
            annotations_json_path,
            annotation_payload,
            protect_reviewed=False,
        ),
        "annotations_markdown": write_living_book_text(
            annotations_markdown_path,
            render_living_annotations_markdown(book, annotations, generated_at),
            protect_reviewed=False,
        ),
    }
    status_payload = {
        "schema": "click.living_book.evidence_sync_status.v1",
        "book_id": book_id,
        "bundle_dir": str(bundle_dir),
        "annotation_count": len(annotations),
        "source_event_ids": source_event_ids or [],
        "primary_store": "reader.annotations",
        "audio_note_primary_store": "reader.audio_notes",
        "synced_at": generated_at,
        "state": "synced",
    }
    outputs["status"] = write_living_book_json_unprotected(
        bundle_dir / "_status" / "evidence_sync.json",
        status_payload,
    )
    return {
        "ok": True,
        **status_payload,
        "outputs": outputs,
    }


def pending_living_book_evidence_sync_count() -> int:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT count(*) AS count FROM reader.sync_events WHERE target_system = %s AND status = 'pending'",
            (LIVING_BOOK_EVIDENCE_SYNC_TARGET,),
        ).fetchone()
    return int((row or {}).get("count") or 0)


def process_living_book_evidence_sync_events(limit: int = 100) -> dict[str, Any]:
    limit = max(1, min(int(limit), 500))
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM reader.sync_events
            WHERE target_system = %s AND status = 'pending'
            ORDER BY created_at ASC
            LIMIT %s
            """,
            (LIVING_BOOK_EVIDENCE_SYNC_TARGET, limit),
        ).fetchall()
    events = [jsonable(dict(row)) for row in rows]
    grouped: dict[str, list[dict[str, Any]]] = {}
    missing_book_events: list[dict[str, Any]] = []
    for event in events:
        payload = event.get("payload") or {}
        book_id = str(payload.get("book_id") or "").strip()
        if book_id:
            grouped.setdefault(book_id, []).append(event)
        else:
            missing_book_events.append(event)

    results: list[dict[str, Any]] = []
    for event in missing_book_events:
        error = "evidence sync event missing book_id"
        payload = {**(event.get("payload") or {}), "failed_at": now_iso(), "error": error}
        with db.connect() as conn:
            update_sync_event(conn, str(event["id"]), "failed", payload, error)
        results.append({"event_id": event["id"], "status": "failed", "error": error})

    for book_id, book_events in grouped.items():
        event_ids = [str(event["id"]) for event in book_events]
        try:
            export_result = export_living_book_evidence(book_id, source_event_ids=event_ids)
            synced_at = now_iso()
            with db.connect() as conn:
                for event in book_events:
                    event_payload = event.get("payload") or {}
                    update_sync_event(
                        conn,
                        str(event["id"]),
                        "synced",
                        {
                            **event_payload,
                            "synced_at": synced_at,
                            "bundle_dir": export_result["bundle_dir"],
                            "annotation_count": export_result["annotation_count"],
                            "evidence_sync_schema": export_result["schema"],
                        },
                    )
            results.append(
                {
                    "book_id": book_id,
                    "event_ids": event_ids,
                    "status": "synced",
                    "bundle_dir": export_result["bundle_dir"],
                    "annotation_count": export_result["annotation_count"],
                }
            )
        except Exception as exc:  # noqa: BLE001 - preserve the committed source mutation and expose retry state.
            error = f"{exc.__class__.__name__}: {exc}"
            failed_at = now_iso()
            with db.connect() as conn:
                for event in book_events:
                    event_payload = event.get("payload") or {}
                    update_sync_event(
                        conn,
                        str(event["id"]),
                        "failed",
                        {**event_payload, "failed_at": failed_at, "error": error},
                        error,
                    )
            results.append({"book_id": book_id, "event_ids": event_ids, "status": "failed", "error": error})

    failed_count = sum(1 for item in results if item["status"] == "failed")
    return {
        "ok": failed_count == 0,
        "schema": "click.living_book.evidence_sync_run.v1",
        "attempted_event_count": len(events),
        "book_count": len(grouped),
        "failed_count": failed_count,
        "results": results,
    }


def run_living_book_evidence_sync_worker() -> None:
    global LIVING_BOOK_EVIDENCE_SYNC_THREAD
    try:
        while True:
            result = process_living_book_evidence_sync_events(limit=100)
            if result["attempted_event_count"] == 0:
                break
    except Exception:
        # Reader API must remain available if PostgreSQL or the KnowledgeBase disk is temporarily unavailable.
        pass
    finally:
        with LIVING_BOOK_EVIDENCE_SYNC_THREAD_LOCK:
            LIVING_BOOK_EVIDENCE_SYNC_THREAD = None
        try:
            if pending_living_book_evidence_sync_count() > 0:
                start_living_book_evidence_sync_worker()
        except Exception:
            pass


def start_living_book_evidence_sync_worker() -> bool:
    global LIVING_BOOK_EVIDENCE_SYNC_THREAD
    with LIVING_BOOK_EVIDENCE_SYNC_THREAD_LOCK:
        if LIVING_BOOK_EVIDENCE_SYNC_THREAD is not None and LIVING_BOOK_EVIDENCE_SYNC_THREAD.is_alive():
            return False
        thread = threading.Thread(
            target=run_living_book_evidence_sync_worker,
            daemon=True,
            name="living-book-evidence-sync",
        )
        LIVING_BOOK_EVIDENCE_SYNC_THREAD = thread
        thread.start()
    return True


@app.on_event("startup")
def start_pending_living_book_evidence_sync() -> None:
    start_living_book_evidence_sync_worker()


def load_hermes_sync_payload(payload_path: Path) -> dict[str, Any]:
    if not payload_path.exists():
        raise FileNotFoundError(f"sync payload missing: {payload_path}")
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"sync payload JSON invalid: {payload_path}: {exc}") from exc
    if payload.get("schema") != "sentence_reader.hermes_sync.v1":
        raise ValueError(f"unsupported sync payload schema: {payload.get('schema')}")
    return payload


def write_hermes_ingestion_files(sync_event: dict[str, Any], sync_payload: dict[str, Any], root: Path, ingested_at: str) -> dict[str, Any]:
    incoming_dir = sentence_reader_incoming_dir(root)
    incoming_dir.mkdir(parents=True, exist_ok=True)

    event_id = str(sync_event["id"])
    payload_path = incoming_dir / f"{safe_slug(event_id)}.payload.json"
    manifest_path = incoming_dir / f"{safe_slug(event_id)}.manifest.json"
    source_payload = sync_event.get("payload") or {}
    manifest = {
        "schema": "sentence_reader.hermes_ingestion_manifest.v1",
        "ingested_at": ingested_at,
        "source": {
            "app": "Sentence Reader",
            "sync_event_id": event_id,
            "source_kind": sync_event.get("source_kind"),
            "source_id": sync_event.get("source_id"),
            "source_payload_path": source_payload.get("payload_path"),
        },
        "target": {
            "system": "hermes_cognitive_os",
            "queue": "incoming/sentence_reader",
            "payload_path": str(payload_path),
        },
        "policy": {
            "active_pack_mutation": False,
            "requires_human_or_pipeline_review": True,
            "reason": "Reader annotations are source assets; they must not auto-become cognitive models.",
        },
        "summary": {
            "book_title": (sync_payload.get("book") or {}).get("title"),
            "annotation_count": sync_payload.get("annotation_count", 0),
        },
    }
    payload_path.write_text(json.dumps(sync_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"payload_path": str(payload_path), "manifest_path": str(manifest_path), "manifest": manifest}


def validate_audio_status(status: str) -> str:
    if status not in {"pending", "transcribed", "failed"}:
        raise HTTPException(status_code=422, detail="audio note status must be pending, transcribed, or failed")
    return status


def sync_reader_audio_note_to_voice_inbox(audio_note_id: str) -> None:
    try:
        project_reader_audio_note_to_voice_inbox(audio_note_id)
    except Exception as exc:  # noqa: BLE001 - Reader capture must survive a temporary Voice Inbox failure.
        print(
            f"Click Voice projection deferred for reader audio note {audio_note_id}: {exc}",
            file=sys.stderr,
        )


def sentence_reader_app_support_dir() -> Path:
    configured = os.getenv("SENTENCE_READER_APP_SUPPORT", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Library" / "Application Support" / "SentenceReader"


def lan_audio_extension(mime_type: str) -> str:
    normalized = mime_type.split(";")[0].strip().lower()
    return {
        "audio/mp4": ".m4a",
        "audio/m4a": ".m4a",
        "audio/x-m4a": ".m4a",
        "audio/aac": ".aac",
        "audio/wav": ".wav",
        "audio/wave": ".wav",
        "audio/webm": ".webm",
        "audio/ogg": ".ogg",
        "application/octet-stream": ".audio",
    }.get(normalized, mimetypes.guess_extension(normalized) or ".audio")


def decode_audio_base64(value: str) -> bytes:
    raw = value.strip()
    if "," in raw and raw.lower().startswith("data:"):
        raw = raw.split(",", 1)[1]
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception as exc:  # noqa: BLE001 - report a clear API error.
        raise HTTPException(status_code=422, detail="invalid audio_base64") from exc
    if not data:
        raise HTTPException(status_code=422, detail="audio_base64 is empty")
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="audio note is too large")
    return data


def note_text_with_transcript(current: str, transcript: str) -> str:
    current_text = str(current or "").strip()
    transcript_text = normalize_note_text(transcript)
    if not transcript_text:
        return current_text
    if not current_text or current_text == VOICE_NOTE_PENDING_TEXT:
        return transcript_text
    if VOICE_NOTE_PENDING_TEXT in current_text:
        return normalize_note_text(current_text.replace(VOICE_NOTE_PENDING_TEXT, transcript_text))
    if transcript_text in current_text:
        return current_text
    return normalize_note_text(f"{current_text}\n{transcript_text}")


def note_text_with_failure(current: str) -> str:
    current_text = str(current or "").strip()
    if not current_text or current_text == VOICE_NOTE_PENDING_TEXT:
        return VOICE_NOTE_FAILED_TEXT
    if VOICE_NOTE_PENDING_TEXT in current_text:
        return normalize_note_text(current_text.replace(VOICE_NOTE_PENDING_TEXT, VOICE_NOTE_FAILED_TEXT))
    return current_text


def apply_audio_note_to_annotation(conn: Any, audio_row: dict[str, Any]) -> Optional[dict[str, Any]]:
    annotation_id = str(audio_row.get("annotation_id") or "").strip()
    if not annotation_id:
        return None
    annotation = conn.execute("SELECT * FROM reader.annotations WHERE id = %s", (annotation_id,)).fetchone()
    if not annotation:
        return None
    metadata = annotation.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    metadata = dict(metadata)
    voice_note = metadata.get("voice_note") or {}
    if not isinstance(voice_note, dict):
        voice_note = {}
    transcript = normalize_note_text(str(audio_row.get("transcript") or ""))
    status_value = str(audio_row.get("status") or "pending")
    voice_note.update(
        {
            "audio_note_id": audio_row.get("id"),
            "status": status_value,
            "provider": audio_row.get("provider") or "",
            "audio_hash": audio_row.get("audio_hash") or "",
            "updated_at": now_iso(),
        }
    )
    if transcript:
        voice_note["raw_transcript"] = transcript
    error_message = str(audio_row.get("error_message") or "").strip()
    if error_message:
        voice_note["error_message"] = error_message
    metadata["voice_note"] = voice_note

    current_note = str(annotation.get("note_text") or "")
    if status_value == "transcribed" and transcript:
        next_note = note_text_with_transcript(current_note, transcript)
    elif status_value == "failed":
        next_note = note_text_with_failure(current_note)
    else:
        next_note = current_note.strip() or VOICE_NOTE_PENDING_TEXT

    row = conn.execute(
        """
        UPDATE reader.annotations
        SET note_text = %s,
            metadata = %s,
            updated_at = now()
        WHERE id = %s
        RETURNING *
        """,
        (next_note, db.jsonb(metadata), annotation_id),
    ).fetchone()
    return dict(row) if row else None


def lan_audio_raw_result(
    *,
    mime_type: str,
    audio_byte_count: int,
    pipeline_result: Optional[dict[str, Any]] = None,
    async_processing: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "mime_type": mime_type,
        "audio_bytes": audio_byte_count,
        "async_processing": async_processing,
        "voice_pipeline": {
            "schema": MAC_VOICE_PIPELINE_SCHEMA,
            "pipeline": MAC_VOICE_PIPELINE_ID,
            "mac_side_processing": True,
            "app_role": "capture_upload_only",
            "purpose": "reader_lan_audio_note",
        },
    }
    if pipeline_result is not None:
        result["voice_pipeline_result"] = pipeline_result
    return result


def run_lan_audio_note_transcription(audio_note_id: str, audio_path: Path, mime_type: str, audio_byte_count: int) -> None:
    try:
        pipeline_result = mac_voice_pipeline_transcribe(audio_path, purpose="reader_lan_audio_note", timeout=90.0)
    except Exception as exc:  # noqa: BLE001 - preserve the already saved audio note.
        pipeline_result = {"ok": False, "error": str(exc), "status": "failed"}
    transcript: Optional[str] = None
    status_value = "failed"
    error_message: Optional[str] = None
    if pipeline_result.get("ok"):
        transcript = normalize_note_text(str(pipeline_result.get("transcript") or ""))
        if transcript:
            status_value = "transcribed"
        else:
            error_message = "Mac voice pipeline returned an empty transcript"
    else:
        error_message = str(pipeline_result.get("error") or "Mac voice pipeline failed")
    raw_result = lan_audio_raw_result(
        mime_type=mime_type,
        audio_byte_count=audio_byte_count,
        pipeline_result=pipeline_result,
        async_processing=True,
    )
    with db.connect() as conn:
        row = conn.execute(
            """
            UPDATE reader.audio_notes
            SET transcript = %s,
                raw_result = %s,
                status = %s,
                error_message = %s,
                updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (transcript, db.jsonb(raw_result), status_value, error_message, audio_note_id),
        ).fetchone()
        if row:
            apply_audio_note_to_annotation(conn, dict(row))
            enqueue_living_book_evidence_sync(
                conn,
                source_kind="audio_note",
                source_id=audio_note_id,
                book_id=str(row["book_id"]),
                operation="audio_note_transcription_finished",
                details={"status": status_value},
            )
    if row:
        start_living_book_evidence_sync_worker()
        sync_reader_audio_note_to_voice_inbox(audio_note_id)


def start_lan_audio_note_transcription(audio_note_id: str, audio_path: Path, mime_type: str, audio_byte_count: int) -> None:
    thread = threading.Thread(
        target=run_lan_audio_note_transcription,
        args=(audio_note_id, audio_path, mime_type, audio_byte_count),
        daemon=True,
        name=f"lan-audio-note-{audio_note_id}",
    )
    thread.start()


def funasr_server_json(path: str, payload: Optional[dict[str, Any]] = None, timeout: float = 45.0) -> dict[str, Any]:
    url = f"http://127.0.0.1:18081{path}"
    if payload is None:
        request = URLRequest(url, method="GET")
    else:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = URLRequest(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - local-only FunASR service.
        return json.loads(response.read().decode("utf-8"))


def app_support_books_dir() -> Path:
    return sentence_reader_app_support_dir() / "Books"


def library_file_status(file_path: str) -> dict[str, Any]:
    path = Path(str(file_path or "")).expanduser()
    exists = path.exists()
    owned = False
    try:
        path.resolve().relative_to(app_support_books_dir().resolve())
        owned = True
    except (FileNotFoundError, ValueError):
        try:
            relative = path.resolve().relative_to(knowledge_base_root_path().resolve())
            owned = "1_原书" in relative.parts
        except (FileNotFoundError, ValueError):
            owned = False
    return {
        "file_path": str(path) if file_path else "",
        "exists": exists,
        "owned_internal_copy": owned,
        "extension": path.suffix.lower().lstrip("."),
    }


_ANDROID_BOOK_SOURCE_CACHE_LOCK = threading.RLock()
_ANDROID_BOOK_SOURCE_CACHE: dict[tuple[str, str, str], dict[str, Any]] = {}


def click_owned_internal_book_source_path(file_path: str) -> Optional[Path]:
    raw_path = str(file_path or "").strip()
    if not raw_path:
        return None
    try:
        resolved = Path(raw_path).expanduser().resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError):
        return None
    if not resolved.is_file():
        return None
    try:
        resolved.relative_to(app_support_books_dir().resolve())
        return resolved
    except (FileNotFoundError, OSError, ValueError):
        pass
    try:
        relative = resolved.relative_to(knowledge_base_root_path().resolve())
    except (FileNotFoundError, OSError, ValueError):
        return None
    return resolved if "1_原书" in relative.parts else None


def android_book_source_file_hash(handle: Any) -> str:
    digest = hashlib.sha256()
    handle.seek(0)
    for chunk in iter(lambda: handle.read(ANDROID_BOOK_UPLOAD_CHUNK_BYTES), b""):
        digest.update(chunk)
    handle.seek(0)
    return digest.hexdigest()


def _resolve_android_book_source(book_id: str, conn: Any, *, keep_open: bool) -> dict[str, Any]:
    book_row = conn.execute(
        """
        SELECT b.*, ls.metadata AS library_metadata
        FROM reader.books b
        LEFT JOIN reader.library_state ls ON ls.book_id=b.id
        WHERE b.id=%s
        """,
        (book_id,),
    ).fetchone()
    if not book_row:
        raise HTTPException(status_code=404, detail="book not found")
    book = dict(book_row)
    source_kind = str(book.get("source_kind") or "").strip().lower()
    if source_kind not in {"epub", "pdf"}:
        raise HTTPException(status_code=404, detail="book source is unavailable")
    candidates = conn.execute(
        """
        SELECT id, book_id, file_path, file_kind, file_hash, byte_size, created_at
        FROM reader.book_files
        WHERE book_id=%s
        ORDER BY created_at DESC, id DESC
        """,
        (book_id,),
    ).fetchall()
    for raw_candidate in candidates:
        candidate = dict(raw_candidate)
        if str(candidate.get("file_kind") or "").strip().lower() != source_kind:
            continue
        source_path = click_owned_internal_book_source_path(str(candidate.get("file_path") or ""))
        if source_path is None or source_path.suffix.lower() != f".{source_kind}":
            continue
        cache_key = (book_id, str(candidate.get("id") or ""), str(source_path))
        for attempt in range(2):
            try:
                handle = source_path.open("rb")
            except OSError:
                break
            try:
                before = os.fstat(handle.fileno())
                identity = (int(before.st_size), int(before.st_mtime_ns))
                with _ANDROID_BOOK_SOURCE_CACHE_LOCK:
                    cached = _ANDROID_BOOK_SOURCE_CACHE.get(cache_key)
                    if cached and cached.get("identity") == identity:
                        source_hash = str(cached["file_hash"])
                    else:
                        source_hash = android_book_source_file_hash(handle)
                    after = os.fstat(handle.fileno())
                    after_identity = (int(after.st_size), int(after.st_mtime_ns))
                    if after_identity != identity:
                        _ANDROID_BOOK_SOURCE_CACHE.pop(cache_key, None)
                        handle.close()
                        if attempt == 0:
                            continue
                        raise HTTPException(status_code=409, detail="book source changed during snapshot")
                    _ANDROID_BOOK_SOURCE_CACHE[cache_key] = {
                        "identity": identity,
                        "file_hash": source_hash,
                    }
                byte_size = identity[0]
                stored_hash = str(candidate.get("file_hash") or "").strip().lower()
                stored_size = (
                    int(candidate["byte_size"])
                    if candidate.get("byte_size") is not None
                    else None
                )
                if stored_hash != source_hash or stored_size != byte_size:
                    conn.execute(
                        """
                        UPDATE reader.book_files
                        SET file_hash=%s, byte_size=%s
                        WHERE id=%s
                          AND (file_hash IS DISTINCT FROM %s OR byte_size IS DISTINCT FROM %s)
                        """,
                        (source_hash, byte_size, candidate["id"], source_hash, byte_size),
                    )
                snapshot = {
                    "book_id": book_id,
                    "file_id": str(candidate.get("id") or ""),
                    "file_path": str(source_path),
                    "file_kind": source_kind,
                    "file_hash": source_hash,
                    "byte_size": byte_size,
                    "mtime_ns": identity[1],
                }
                resolved = {
                    **book,
                    "file_path": snapshot["file_path"],
                    "file_kind": snapshot["file_kind"],
                    "file_hash": snapshot["file_hash"],
                    "byte_size": snapshot["byte_size"],
                    "_android_source_snapshot": snapshot,
                }
                if keep_open:
                    resolved["_android_source_handle"] = handle
                else:
                    handle.close()
                return resolved
            except BaseException:
                if not handle.closed:
                    handle.close()
                raise
    raise HTTPException(status_code=404, detail="Click-owned book source is unavailable")


def resolve_android_book_source(
    book_id: str,
    *,
    conn: Any = None,
    keep_open: bool = False,
) -> dict[str, Any]:
    if conn is not None:
        return _resolve_android_book_source(book_id, conn, keep_open=keep_open)
    with db.connect() as owned_conn:
        return _resolve_android_book_source(book_id, owned_conn, keep_open=keep_open)


def library_progress(position: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not position:
        return {
            "has_position": False,
            "page_index": 0,
            "total_pages": 1,
            "page_ratio": 0,
            "percent": 0,
            "chapter_locator": "",
            "updated_at": None,
        }
    ratio = float(position.get("page_ratio") or 0)
    ratio = max(0, min(1, ratio))
    return {
        "has_position": True,
        "page_index": int(position.get("page_index") or 0),
        "total_pages": max(1, int(position.get("total_pages") or 1)),
        "page_ratio": ratio,
        "percent": int(round(ratio * 100)),
        "chapter_locator": position.get("chapter_locator") or "",
        "updated_at": position.get("updated_at"),
    }


def library_book_card(row: dict[str, Any]) -> dict[str, Any]:
    row = preferred_existing_book_file(row)
    file_status = library_file_status(str(row.get("file_path") or ""))
    metadata = row.get("library_metadata") or row.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    custom_category = str(metadata.get("custom_category") or "").strip()
    tags = [str(tag).strip() for tag in (metadata.get("tags") or []) if str(tag).strip()]
    note_count = int(row.get("note_count") or 0)
    red_count = int(row.get("red_count") or 0)
    annotation_count = int(row.get("annotation_count") or 0)
    progress = library_progress(
        {
            "page_index": row.get("page_index"),
            "total_pages": row.get("total_pages"),
            "page_ratio": row.get("page_ratio"),
            "chapter_locator": row.get("chapter_locator"),
            "updated_at": row.get("position_updated_at"),
        }
        if row.get("chapter_locator") is not None
        else None
    )
    lan_available = file_status["exists"] and file_status["extension"] == "epub"
    is_pdf = file_status["exists"] and file_status["extension"] == "pdf"
    book_stub = {
        "id": row.get("id"),
        "title": row.get("title"),
        "author": row.get("author"),
        "book_hash": row.get("book_hash"),
        "source_kind": row.get("source_kind"),
    }
    compatibility = read_book_epub_assets(str(row.get("id") or ""))
    contract = android_book_contract_fields(row, compatibility)
    return {
        "id": row.get("id"),
        "title": row.get("title"),
        "author": row.get("author"),
        "source_kind": row.get("source_kind"),
        "book_hash": row.get("book_hash"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "last_opened_at": row.get("last_opened_at"),
        "recent_activity_at": row.get("position_updated_at") or row.get("last_opened_at") or row.get("updated_at"),
        "file": {
            **file_status,
            "file_kind": row.get("file_kind"),
            "file_hash": row.get("file_hash"),
            "byte_size": row.get("byte_size"),
        },
        "progress": progress,
        "reading_state": library_reading_state(progress, row),
        "compatibility": {
            "status": contract["compatibility_status"],
            "reading_profile": contract["reading_profile"],
            "toc_depth": contract["toc_depth"],
            "display_variants_available": contract["display_variants_available"],
            "correction_queue_required": bool(compatibility.get("correction_queue_required")),
        },
        "living_book_analysis": {
            "state": contract["analysis_state"],
            "updated_at": contract["analysis_updated_at"],
            "manual_only": True,
        },
        "cover": library_cover_info(book_stub, file_status),
        "organization": {
            "favorite": bool(metadata.get("favorite") or False),
            "author": row.get("author") or "未知作者",
            "custom_category": custom_category,
            "category": custom_category or "未分类",
            "tags": tags,
        },
        "counts": {
            "annotations": annotation_count,
            "notes": note_count,
            "red_highlights": red_count,
            "audio_notes": int(row.get("audio_note_count") or 0),
        },
        "status": {
            "hidden": bool(row.get("hidden") or False),
            "lan_available": lan_available,
            "owned_internal_copy": file_status["owned_internal_copy"],
        },
        "reader_capabilities": {
            "mac_native": file_status["exists"] and file_status["extension"] in {"epub", "pdf"},
            "lan_web": lan_available,
            "android_native": lan_available,
            "pdf_local_only_p1": is_pdf,
        },
        "actions": {
            "native_reader_url": f"sentence-reader://open-native?book_id={row.get('id')}",
            "continue_reading_url": f"/lan/reader?book_id={row.get('id')}" if lan_available else "",
            "notes_filter": f"/library?view=notes&book_id={row.get('id')}",
            "red_filter": f"/library?view=red&book_id={row.get('id')}",
            "analysis_status_url": f"/books/{row.get('id')}/living-book/analysis-status",
            "analyze_url": f"/books/{row.get('id')}/living-book/analyze",
            "reanalyze_url": f"/books/{row.get('id')}/living-book/reanalyze",
        },
    }


def library_recent_annotations(limit: int = 80) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT a.*,
                   b.title AS book_title,
                   b.author AS book_author
            FROM reader.annotations a
            JOIN reader.books b ON b.id = a.book_id
            LEFT JOIN reader.library_state ls ON ls.book_id = b.id
            WHERE COALESCE(ls.hidden, false) = false
            ORDER BY a.updated_at DESC, a.created_at DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    output: list[dict[str, Any]] = []
    for row in rows:
        item = jsonable(dict(row))
        source_text = str(item.get("source_text") or "")
        note_text = str(item.get("note_text") or "")
        preview = note_text.strip() or source_text.strip()
        output.append(
            {
                "id": item.get("id"),
                "book_id": item.get("book_id"),
                "book_title": item.get("book_title"),
                "book_author": item.get("book_author"),
                "kind": item.get("kind"),
                "source_text": source_text,
                "note_text": note_text,
                "preview": preview[:220],
                "chapter_title": item.get("chapter_title"),
                "chapter_locator": item.get("chapter_locator"),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
                "actions": {
                    "native_reader_url": f"sentence-reader://open-native?book_id={item.get('book_id')}",
                    "continue_reading_url": f"/lan/reader?book_id={item.get('book_id')}",
                },
            }
        )
    return output


def library_dashboard_payload(include_hidden: bool = False) -> dict[str, Any]:
    # The dashboard is a read path. Re-parsing every owned EPUB here made each
    # shelf refresh take several seconds and repeated filesystem/ZIP work while
    # the user was only browsing. Normal imports already register their owned
    # copy transactionally; the legacy recovery scan stays available only as an
    # explicit maintenance switch.
    owned_scan: dict[str, Any] = {
        "scanned": 0,
        "imported": 0,
        "skipped": 0,
        "errors": [],
        "deferred": True,
        "reason": "dashboard_read_path_is_side_effect_free",
    }
    if os.getenv("SENTENCE_READER_SCAN_OWNED_EPUB_ON_DASHBOARD") == "1":
        owned_scan = sync_owned_epub_library()
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT b.*,
                   bf.file_path,
                   bf.file_kind,
                   bf.file_hash,
                   bf.byte_size,
                   (
                       SELECT jsonb_agg(
                           jsonb_build_object(
                               'file_path', candidate.file_path,
                               'file_kind', candidate.file_kind,
                               'file_hash', candidate.file_hash,
                               'byte_size', candidate.byte_size
                           )
                           ORDER BY candidate.created_at DESC, candidate.id DESC
                       )
                       FROM reader.book_files candidate
                       WHERE candidate.book_id = b.id
                   ) AS file_candidates,
                   rp.chapter_locator,
                   rp.page_index,
                   rp.total_pages,
                   rp.page_ratio,
                   rp.updated_at AS position_updated_at,
                   COALESCE(counts.annotation_count, 0) AS annotation_count,
                   COALESCE(counts.note_count, 0) AS note_count,
                   COALESCE(counts.red_count, 0) AS red_count,
                   COALESCE(audio.audio_note_count, 0) AS audio_note_count,
                   COALESCE(ls.hidden, false) AS hidden,
                   COALESCE(ls.metadata, '{}'::jsonb) AS library_metadata
            FROM reader.books b
            LEFT JOIN LATERAL (
                SELECT file_path, file_kind, file_hash, byte_size
                FROM reader.book_files
                WHERE book_id = b.id
                ORDER BY created_at DESC, id DESC
                LIMIT 1
            ) bf ON true
            LEFT JOIN reader.reading_positions rp ON rp.book_id = b.id
            LEFT JOIN LATERAL (
                SELECT count(*) AS annotation_count,
                       count(*) FILTER (WHERE kind = 'note') AS note_count,
                       count(*) FILTER (WHERE kind = 'red_highlight') AS red_count
                FROM reader.annotations
                WHERE book_id = b.id
            ) counts ON true
            LEFT JOIN LATERAL (
                SELECT count(*) AS audio_note_count
                FROM reader.audio_notes
                WHERE book_id = b.id
            ) audio ON true
            LEFT JOIN reader.library_state ls ON ls.book_id = b.id
            WHERE (%s OR COALESCE(ls.hidden, false) = false)
            ORDER BY b.last_opened_at DESC NULLS LAST, rp.updated_at DESC NULLS LAST, b.created_at DESC
            """,
            (include_hidden,),
        ).fetchall()
    books = [library_book_card(jsonable(dict(row))) for row in rows]
    visible_books = [book for book in books if not book["status"]["hidden"]]
    hidden_books = [book for book in books if book["status"]["hidden"]]
    current = visible_books[0] if visible_books else None
    recent_annotations = library_recent_annotations()
    recent_notes = [item for item in recent_annotations if item.get("kind") == "note"][:20]
    recent_red = [item for item in recent_annotations if item.get("kind") == "red_highlight"][:20]
    total_notes = sum(book["counts"]["notes"] for book in visible_books)
    total_red = sum(book["counts"]["red_highlights"] for book in visible_books)
    favorite_books = [book for book in visible_books if book.get("organization", {}).get("favorite")]
    authors: dict[str, list[dict[str, Any]]] = {}
    categories: dict[str, list[dict[str, Any]]] = {}
    for book in visible_books:
        org = book.get("organization") or {}
        authors.setdefault(str(org.get("author") or "未知作者"), []).append(book)
        categories.setdefault(str(org.get("category") or "未分类"), []).append(book)
    author_groups = [
        {"author": name, "count": len(items), "books": items}
        for name, items in sorted(authors.items(), key=lambda pair: (-len(pair[1]), pair[0]))
    ]
    category_groups = [
        {"category": name, "count": len(items), "books": items}
        for name, items in sorted(categories.items(), key=lambda pair: (pair[0] == "未分类", pair[0]))
    ]
    return {
        "ok": True,
        "schema": "sentence_reader.library_dashboard.v1",
        "ui_version": "library_v2",
        "generated_at": now_iso(),
        "source": {
            "data": "Reader API + PostgreSQL",
            "ui": "Tabler-style local web shell",
            "structure_reference": "Komga-style library/book/progress organization",
            "external_system_embedded": False,
            "owned_epub_scan": owned_scan,
        },
        "summary": {
            "book_count": len(visible_books),
            "note_count": total_notes,
            "red_highlight_count": total_red,
            "annotation_count": total_notes + total_red,
            "hidden_count": sum(1 for book in books if book["status"]["hidden"]),
            "favorite_count": len(favorite_books),
            "author_count": len(author_groups),
            "category_count": len([group for group in category_groups if group["category"] != "未分类"]),
        },
        "current_book": current,
        "books": visible_books,
        "hidden_books": hidden_books,
        "recent_books": visible_books[:6],
        "favorite_books": favorite_books,
        "author_groups": author_groups,
        "category_groups": category_groups,
        "recent_annotations": recent_annotations,
        "recent_notes": recent_notes,
        "recent_red_highlights": recent_red,
        "navigation": [
            {"id": "home", "title": "首页"},
            {"id": "library", "title": "书库"},
            {"id": "favorites", "title": "收藏"},
            {"id": "authors", "title": "作者"},
            {"id": "categories", "title": "分类"},
            {"id": "vocab", "title": "单词"},
            {"id": "notes", "title": "笔记"},
            {"id": "red", "title": "红标"},
            {"id": "settings", "title": "设置"},
        ],
    }


def decode_library_import_base64(value: str) -> bytes:
    raw = value.strip()
    if "," in raw and raw.lower().startswith("data:"):
        raw = raw.split(",", 1)[1]
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception as exc:  # noqa: BLE001 - API must report a clear import error.
        raise HTTPException(status_code=422, detail="invalid content_base64") from exc
    if not data:
        raise HTTPException(status_code=422, detail="content_base64 is empty")
    if len(data) > BOOK_IMPORT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="book import is too large")
    return data


def android_import_staging_dir() -> Path:
    return app_support_books_dir() / ".android-import-staging"


def normalize_android_import_headers(request: FastAPIRequest, requested_sha256: str) -> dict[str, Any]:
    book_hash = str(requested_sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", book_hash):
        raise HTTPException(status_code=422, detail="sha256 path must be 64 hexadecimal characters")

    raw_filename = str(request.headers.get("X-Click-Filename") or "").strip()
    try:
        filename = unquote(raw_filename, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="X-Click-Filename is not valid UTF-8") from exc
    if (
        not filename
        or len(filename) > 240
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or any(ord(character) < 32 for character in filename)
    ):
        raise HTTPException(status_code=422, detail="X-Click-Filename must be a safe file name")

    source_kind = str(request.headers.get("X-Click-Source-Kind") or "").strip().lower()
    if source_kind not in {"epub", "pdf"}:
        raise HTTPException(status_code=422, detail="X-Click-Source-Kind must be epub or pdf")
    if Path(filename).suffix.lower() != f".{source_kind}":
        raise HTTPException(status_code=422, detail=f"file name extension must be .{source_kind}")

    raw_byte_size = str(request.headers.get("X-Click-Byte-Size") or "").strip()
    if not re.fullmatch(r"[0-9]+", raw_byte_size):
        raise HTTPException(status_code=422, detail="X-Click-Byte-Size must be a positive integer")
    byte_size = int(raw_byte_size)
    if byte_size <= 0:
        raise HTTPException(status_code=422, detail="X-Click-Byte-Size must be greater than zero")
    if byte_size > BOOK_IMPORT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="book import is too large")

    raw_content_length = str(request.headers.get("Content-Length") or "").strip()
    if raw_content_length:
        if not re.fullmatch(r"[0-9]+", raw_content_length):
            raise HTTPException(status_code=422, detail="Content-Length is invalid")
        content_length = int(raw_content_length)
        if content_length > BOOK_IMPORT_MAX_BYTES:
            raise HTTPException(status_code=413, detail="book import is too large")
        if content_length != byte_size:
            raise HTTPException(status_code=422, detail="Content-Length does not match X-Click-Byte-Size")

    return {
        "filename": filename,
        "source_kind": source_kind,
        "book_hash": book_hash,
        "byte_size": byte_size,
    }


async def stage_android_book_upload(
    request: FastAPIRequest,
    *,
    expected_sha256: str,
    expected_byte_size: int,
) -> Path:
    directory = android_import_staging_dir()
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    staging_path = directory / f".{expected_sha256}.{uuid4().hex}.part"
    descriptor = os.open(staging_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    digest = hashlib.sha256()
    received = 0
    try:
        with os.fdopen(descriptor, "wb") as output:
            async for chunk in request.stream():
                if not chunk:
                    continue
                received += len(chunk)
                if received > BOOK_IMPORT_MAX_BYTES:
                    raise HTTPException(status_code=413, detail="book import is too large")
                if received > expected_byte_size:
                    raise HTTPException(status_code=422, detail="request body exceeds X-Click-Byte-Size")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if received != expected_byte_size:
            raise HTTPException(status_code=422, detail="request body size does not match X-Click-Byte-Size")
        if digest.hexdigest() != expected_sha256:
            raise HTTPException(status_code=422, detail="request body sha256 does not match URL")
        return staging_path
    except BaseException:
        staging_path.unlink(missing_ok=True)
        raise


def validate_android_epub_upload(path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as epub:
            members = epub.infolist()
            if not members or len(members) > ANDROID_EPUB_MAX_ENTRIES:
                raise HTTPException(status_code=422, detail="EPUB archive has an invalid entry count")
            if any(member.flag_bits & 0x1 for member in members):
                raise HTTPException(status_code=422, detail="encrypted EPUB archives are not supported")
            if any(member.file_size > ANDROID_EPUB_MAX_MEMBER_BYTES for member in members):
                raise HTTPException(status_code=413, detail="EPUB contains an oversized entry")
            if sum(member.file_size for member in members) > ANDROID_EPUB_MAX_UNCOMPRESSED_BYTES:
                raise HTTPException(status_code=413, detail="EPUB expands beyond the safe import limit")
            names = {member.filename for member in members}
            if "mimetype" not in names or "META-INF/container.xml" not in names:
                raise HTTPException(status_code=422, detail="EPUB is missing required container files")
            if epub.read("mimetype").strip() != b"application/epub+zip":
                raise HTTPException(status_code=422, detail="EPUB mimetype is invalid")
        return epub_publication(path)
    except HTTPException:
        raise
    except (KeyError, OSError, RuntimeError, zipfile.BadZipFile, ET.ParseError) as exc:
        raise HTTPException(status_code=422, detail="invalid EPUB file") from exc


def validate_android_pdf_upload(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as source:
            header = source.read(1024)
            source.seek(max(0, path.stat().st_size - 65_536))
            trailer = source.read()
        if b"%PDF-" not in header or b"%%EOF" not in trailer:
            raise HTTPException(status_code=422, detail="invalid PDF file")
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            raise HTTPException(status_code=422, detail="password-protected PDF import is not supported")
        if len(reader.pages) <= 0:
            raise HTTPException(status_code=422, detail="PDF has no readable pages")
        metadata = reader.metadata
        return {
            "title": str(getattr(metadata, "title", "") or "").strip(),
            "author": str(getattr(metadata, "author", "") or "").strip(),
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - parser errors must not expose local staging paths.
        raise HTTPException(status_code=422, detail="invalid PDF file") from exc


def validate_android_book_upload(path: Path, source_kind: str) -> dict[str, Any]:
    if source_kind == "epub":
        return validate_android_epub_upload(path)
    if source_kind == "pdf":
        return validate_android_pdf_upload(path)
    raise HTTPException(status_code=422, detail="unsupported book source kind")


def atomic_install_import_source(
    source_path: Path,
    canonical_path: Path,
    *,
    expected_sha256: str,
    expected_byte_size: int,
) -> bool:
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    if canonical_path.exists():
        if (
            canonical_path.is_file()
            and canonical_path.stat().st_size == expected_byte_size
            and file_sha256(canonical_path) == expected_sha256
        ):
            return False
        raise HTTPException(status_code=409, detail="canonical book file conflicts with the uploaded hash")

    pending_path = canonical_path.parent / f".{canonical_path.name}.{uuid4().hex}.importing"
    digest = hashlib.sha256()
    copied = 0
    try:
        descriptor = os.open(pending_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with source_path.open("rb") as source, os.fdopen(descriptor, "wb") as output:
            while True:
                chunk = source.read(ANDROID_BOOK_UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                copied += len(chunk)
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if copied != expected_byte_size or digest.hexdigest() != expected_sha256:
            raise HTTPException(status_code=422, detail="staged book changed before canonical import")
        os.replace(pending_path, canonical_path)
        return True
    finally:
        pending_path.unlink(missing_ok=True)


def canonical_import_library_file(
    source_path: Path,
    *,
    filename: str,
    source_kind: str,
    book_hash: str,
    byte_size: int,
    title: str,
    author: Optional[str],
    library_source: str,
    library_metadata: Optional[dict[str, Any]] = None,
    update_existing_book: bool = False,
    merge_library_metadata: bool = True,
) -> dict[str, Any]:
    normalized_kind = str(source_kind or "").strip().lower()
    if normalized_kind not in {"epub", "pdf"}:
        raise HTTPException(status_code=422, detail="unsupported book source kind")
    if not re.fullmatch(r"[0-9a-f]{64}", str(book_hash or "").strip().lower()):
        raise HTTPException(status_code=422, detail="invalid book sha256")
    if not source_path.is_file() or byte_size <= 0 or source_path.stat().st_size != byte_size:
        raise HTTPException(status_code=422, detail="staged book file is incomplete")

    normalized_hash = book_hash.lower()
    normalized_title = re.sub(r"\s+", " ", str(title or "").strip()) or Path(filename).stem
    normalized_author = re.sub(r"\s+", " ", str(author or "").strip()) or None
    canonical_path: Optional[Path] = None
    created_canonical = False
    duplicate = False
    try:
        with db.connect() as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)",
                (f"click-book-import:{normalized_hash}",),
            )
            book = conn.execute(
                """
                INSERT INTO reader.books (
                  id, title, author, source_kind, book_hash, created_at, updated_at, last_opened_at
                )
                VALUES (%s, %s, %s, %s, %s, now(), now(), now())
                ON CONFLICT (book_hash) DO NOTHING
                RETURNING *
                """,
                (new_id("book"), normalized_title, normalized_author, normalized_kind, normalized_hash),
            ).fetchone()
            if not book:
                duplicate = True
                book = conn.execute(
                    "SELECT * FROM reader.books WHERE book_hash=%s FOR UPDATE",
                    (normalized_hash,),
                ).fetchone()
                if not book:
                    raise RuntimeError("book hash conflict did not resolve to a row")
                if str(book.get("source_kind") or "").strip().lower() != normalized_kind:
                    raise HTTPException(status_code=409, detail="book hash already belongs to another source kind")
                if update_existing_book:
                    book = conn.execute(
                        """
                        UPDATE reader.books
                        SET title=%s, author=%s, updated_at=now(), last_opened_at=now()
                        WHERE id=%s
                        RETURNING *
                        """,
                        (normalized_title, normalized_author, book["id"]),
                    ).fetchone()

            book_dict = dict(book)
            if normalized_kind == "epub":
                canonical_path = app_support_books_dir() / normalized_hash / "book.epub"
            else:
                original_dir = living_book_bundle_dir(book_dict) / "1_原书"
                canonical_path = original_dir / living_book_safe_filename(book_dict, Path(filename))
                if canonical_path.exists() and file_sha256(canonical_path) != normalized_hash:
                    canonical_path = original_dir / f"{canonical_path.stem}-{normalized_hash[:10]}.pdf"

            created_canonical = atomic_install_import_source(
                source_path,
                canonical_path,
                expected_sha256=normalized_hash,
                expected_byte_size=byte_size,
            )
            conn.execute(
                """
                INSERT INTO reader.book_files (id, book_id, file_path, file_kind, file_hash, byte_size)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (book_id, file_path) DO UPDATE
                SET file_kind=EXCLUDED.file_kind,
                    file_hash=EXCLUDED.file_hash,
                    byte_size=EXCLUDED.byte_size
                """,
                (
                    stable_id("file", book["id"], str(canonical_path)),
                    book["id"],
                    str(canonical_path),
                    normalized_kind,
                    normalized_hash,
                    byte_size,
                ),
            )
            metadata = {
                "filename": filename,
                "owned_internal_copy": True,
                **dict(library_metadata or {}),
            }
            metadata_update = (
                "reader.library_state.metadata || EXCLUDED.metadata"
                if merge_library_metadata
                else "EXCLUDED.metadata"
            )
            conn.execute(
                f"""
                INSERT INTO reader.library_state (book_id, hidden, source, metadata, created_at, updated_at)
                VALUES (%s, false, %s, %s, now(), now())
                ON CONFLICT (book_id) DO UPDATE
                SET hidden=false,
                    source=EXCLUDED.source,
                    metadata={metadata_update},
                    updated_at=now()
                """,
                (book["id"], library_source, db.jsonb(metadata)),
            )
    except BaseException:
        if created_canonical and canonical_path is not None:
            canonical_path.unlink(missing_ok=True)
        raise

    if canonical_path is None:
        raise RuntimeError("canonical book path was not created")
    return {
        "book": jsonable(dict(book)),
        "file_path": str(canonical_path),
        "file_hash": normalized_hash,
        "byte_size": byte_size,
        "source_kind": normalized_kind,
        "duplicate": duplicate,
    }


def import_library_epub(payload: LibraryImport) -> dict[str, Any]:
    filename = Path(payload.filename).name
    if not filename.lower().endswith(".epub"):
        raise HTTPException(status_code=422, detail="only EPUB import is supported")
    data = decode_library_import_base64(payload.content_base64)
    book_hash = hashlib.sha256(data).hexdigest()
    with tempfile.TemporaryDirectory(prefix="click-epub-import-") as temp_dir:
        source_path = Path(temp_dir) / "source.epub"
        source_path.write_bytes(data)
        publication = epub_publication(source_path)
        title = (payload.title or publication.get("title") or Path(filename).stem).strip() or Path(filename).stem
        author = (payload.author or publication.get("author") or "").strip() or None
        imported = canonical_import_library_file(
            source_path,
            filename=filename,
            source_kind="epub",
            book_hash=book_hash,
            byte_size=len(data),
            title=title,
            author=author,
            library_source="library_web_import",
            library_metadata=(
                {
                    "bibliographic_override": {
                        "title": title,
                        "author": author,
                        "source": "explicit_library_import_metadata",
                        "evidence": "library import request",
                        "updated_at": now_iso(),
                    }
                }
                if payload.title is not None or payload.author is not None
                else None
            ),
            update_existing_book=True,
            merge_library_metadata=False,
        )
    living_book = generate_living_book_bundle(str(imported["book"]["id"]))
    return {
        "ok": True,
        "schema": "sentence_reader.library_import.v1",
        "book": imported["book"],
        "file_path": imported["file_path"],
        "owned_internal_copy": True,
        "original_source_can_be_deleted": True,
        "living_book": living_book,
    }


def import_library_pdf(payload: LibraryImport) -> dict[str, Any]:
    filename = Path(payload.filename).name
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=422, detail="PDF filename must end with .pdf")
    data = decode_library_import_base64(payload.content_base64)
    book_hash = hashlib.sha256(data).hexdigest()
    with tempfile.TemporaryDirectory(prefix="click-pdf-import-") as temp_dir:
        source_path = Path(temp_dir) / "source.pdf"
        source_path.write_bytes(data)
        preflight = inspect_pdf(source_path)
        if preflight.get("document_profile") == "invalid":
            raise HTTPException(status_code=422, detail="invalid PDF file")
        title = (payload.title or preflight.get("title") or Path(filename).stem).strip() or Path(filename).stem
        author = (payload.author or preflight.get("author") or "").strip() or None
        imported = canonical_import_library_file(
            source_path,
            filename=filename,
            source_kind="pdf",
            book_hash=book_hash,
            byte_size=len(data),
            title=title,
            author=author,
            library_source="library_web_pdf_import",
            library_metadata={
                "canonical_asset_owner": "knowledge_base_living_book",
                "pdf_profile": preflight.get("document_profile"),
            },
            update_existing_book=True,
            merge_library_metadata=True,
        )
    living_book = generate_living_book_bundle(str(imported["book"]["id"]))
    return {
        "ok": True,
        "schema": "sentence_reader.library_import.v1",
        "book": imported["book"],
        "file_path": imported["file_path"],
        "canonical_source_file": imported["file_path"],
        "owned_internal_copy": True,
        "canonical_asset_owner": "knowledge_base_living_book",
        "original_source_can_be_deleted": True,
        "pdf_preflight": {key: value for key, value in preflight.items() if key != "source_file"},
        "living_book": living_book,
    }


def import_library_book(payload: LibraryImport) -> dict[str, Any]:
    suffix = Path(payload.filename).suffix.lower()
    if suffix == ".epub":
        return import_library_epub(payload)
    if suffix == ".pdf":
        return import_library_pdf(payload)
    raise HTTPException(status_code=422, detail="only EPUB and PDF import are supported")


def library_bibliographic_override(metadata: Any) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    value = metadata.get("bibliographic_override")
    return dict(value) if isinstance(value, dict) else {}


def update_library_book_metadata(book_id: str, payload: LibraryMetadataPatch) -> dict[str, Any]:
    if payload.title is None and payload.author is None:
        raise HTTPException(status_code=422, detail="title or author is required")
    source = re.sub(r"\s+", " ", str(payload.source or "user_confirmed").strip()) or "user_confirmed"
    evidence = re.sub(r"\s+", " ", str(payload.evidence or "").strip()) or None
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT b.*, COALESCE(ls.metadata, '{}'::jsonb) AS library_metadata
            FROM reader.books b
            LEFT JOIN reader.library_state ls ON ls.book_id=b.id
            WHERE b.id=%s
            FOR UPDATE OF b
            """,
            (book_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="book not found")
        current = dict(row)
        title = (
            re.sub(r"\s+", " ", str(payload.title).strip())
            if payload.title is not None
            else str(current.get("title") or "").strip()
        )
        author = (
            re.sub(r"\s+", " ", str(payload.author).strip()) or None
            if payload.author is not None
            else (str(current.get("author") or "").strip() or None)
        )
        if not title:
            raise HTTPException(status_code=422, detail="title cannot be empty")
        updated = conn.execute(
            """
            UPDATE reader.books
            SET title=%s, author=%s, updated_at=now()
            WHERE id=%s
            RETURNING *
            """,
            (title, author, book_id),
        ).fetchone()
        metadata = dict(current.get("library_metadata") or {})
        metadata["bibliographic_override"] = {
            "title": title,
            "author": author,
            "source": source,
            "evidence": evidence,
            "updated_at": now_iso(),
        }
        conn.execute(
            """
            INSERT INTO reader.library_state (book_id, hidden, source, metadata, created_at, updated_at)
            VALUES (%s, false, %s, %s, now(), now())
            ON CONFLICT (book_id) DO UPDATE
            SET metadata=EXCLUDED.metadata,
                updated_at=now()
            """,
            (book_id, "bibliographic_metadata_patch", db.jsonb(metadata)),
        )
    return {
        "ok": True,
        "schema": "sentence_reader.library_metadata.v1",
        "book": jsonable(dict(updated)),
        "bibliographic_override": metadata["bibliographic_override"],
    }


def sync_owned_epub_library() -> dict[str, Any]:
    if os.getenv("SENTENCE_READER_SKIP_OWNED_EPUB_SCAN") == "1":
        return {"scanned": 0, "imported": 0, "skipped": 0, "errors": [], "disabled": True}
    books_dir = app_support_books_dir()
    if not books_dir.exists():
        return {"scanned": 0, "imported": 0, "skipped": 0, "errors": []}

    imported = 0
    skipped = 0
    errors: list[dict[str, str]] = []
    for epub_path in sorted(books_dir.glob("*/book.epub")):
        root_name = epub_path.parent.name
        try:
            publication = epub_publication(epub_path)
        except HTTPException as exc:
            skipped += 1
            errors.append({"path": str(epub_path), "reason": str(exc.detail)})
            continue

        publication_title = str(publication.get("title") or epub_path.parent.name).strip() or epub_path.parent.name
        publication_author = str(publication.get("author") or "").strip() or None
        byte_size = epub_path.stat().st_size
        book_hash = root_name
        book_id = stable_id("book", "owned-epub", book_hash)
        with db.connect() as conn:
            existing = conn.execute(
                """
                SELECT b.title, b.author, COALESCE(ls.metadata, '{}'::jsonb) AS library_metadata
                FROM reader.books b
                LEFT JOIN reader.library_state ls ON ls.book_id=b.id
                WHERE b.book_hash=%s
                """,
                (book_hash,),
            ).fetchone()
            override = library_bibliographic_override(
                dict(existing).get("library_metadata") if existing else {}
            )
            title = str(override.get("title") or publication_title).strip() or publication_title
            author = (
                str(override.get("author") or "").strip() or None
                if "author" in override
                else publication_author
            )
            book = conn.execute(
                """
                INSERT INTO reader.books (id, title, author, source_kind, book_hash, created_at, updated_at, last_opened_at)
                VALUES (%s, %s, %s, 'epub', %s, now(), now(), NULL)
                ON CONFLICT (book_hash) DO UPDATE
                SET title = EXCLUDED.title,
                    author = EXCLUDED.author,
                    updated_at = now()
                RETURNING *
                """,
                (book_id, title, author, book_hash),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO reader.book_files (id, book_id, file_path, file_kind, file_hash, byte_size)
                VALUES (%s, %s, %s, 'epub', %s, %s)
                ON CONFLICT (book_id, file_path) DO UPDATE
                SET file_hash = EXCLUDED.file_hash,
                    byte_size = EXCLUDED.byte_size
                """,
                (stable_id("file", book["id"], str(epub_path)), book["id"], str(epub_path), book_hash, byte_size),
            )
            conn.execute(
                """
                INSERT INTO reader.library_state (book_id, hidden, source, metadata, created_at, updated_at)
                VALUES (%s, false, %s, %s, now(), now())
                ON CONFLICT (book_id) DO UPDATE
                SET hidden = reader.library_state.hidden,
                    source = EXCLUDED.source,
                    metadata = reader.library_state.metadata || EXCLUDED.metadata,
                    updated_at = now()
                """,
                (
                    book["id"],
                    "owned_epub_scan",
                    db.jsonb({"owned_internal_copy": True, "root_name": root_name, "scan_path": str(epub_path)}),
                ),
            )
        try:
            generate_living_book_bundle(str(book["id"]))
        except Exception as exc:  # noqa: BLE001 - keep the owned EPUB even when its sidecar needs repair.
            errors.append({"path": str(epub_path), "reason": f"living_book_base_bundle: {exc}"})
        imported += 1

    return {"scanned": imported + skipped, "imported": imported, "skipped": skipped, "errors": errors}


def hide_library_books(book_ids: list[str], *, source: str) -> dict[str, Any]:
    return set_library_books_hidden(book_ids, hidden=True, source=source)


def restore_library_books(book_ids: list[str], *, source: str) -> dict[str, Any]:
    return set_library_books_hidden(book_ids, hidden=False, source=source)


def set_library_books_hidden(book_ids: list[str], *, hidden: bool, source: str) -> dict[str, Any]:
    unique_ids: list[str] = []
    seen: set[str] = set()
    for raw_id in book_ids:
        book_id = str(raw_id or "").strip()
        if book_id and book_id not in seen:
            unique_ids.append(book_id)
            seen.add(book_id)
    if not unique_ids:
        raise HTTPException(status_code=422, detail="book_ids is empty")

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, title FROM reader.books WHERE id = ANY(%s)",
            (unique_ids,),
        ).fetchall()
        found_ids = {str(row["id"]) for row in rows}
        missing_ids = [book_id for book_id in unique_ids if book_id not in found_ids]
        if missing_ids:
            raise HTTPException(status_code=404, detail={"missing_book_ids": missing_ids})

        hidden_rows = []
        for book_id in unique_ids:
            row = conn.execute(
                """
                INSERT INTO reader.library_state (book_id, hidden, source, metadata, created_at, updated_at)
                VALUES (%s, %s, %s, %s, now(), now())
                ON CONFLICT (book_id) DO UPDATE
                SET hidden = EXCLUDED.hidden,
                    source = EXCLUDED.source,
                    metadata = reader.library_state.metadata || EXCLUDED.metadata,
                    updated_at = now()
                RETURNING *
                """,
                (
                    book_id,
                    hidden,
                    source,
                    db.jsonb(
                        {
                            "non_destructive": True,
                            "does_not_delete_epub": True,
                            "does_not_delete_postgresql_data": True,
                            "does_not_delete_notes": True,
                            "batch_size": len(unique_ids),
                        }
                    ),
                ),
            ).fetchone()
            hidden_rows.append(jsonable(dict(row)))

    schema = "sentence_reader.library_hide.v1" if hidden else "sentence_reader.library_restore.v1"
    return {
        "ok": True,
        "schema": schema,
        "book_ids": unique_ids,
        "affected_count": len(hidden_rows),
        "hidden_count": len(hidden_rows) if hidden else 0,
        "restored_count": len(hidden_rows) if not hidden else 0,
        "non_destructive": True,
        "hidden": hidden,
        "library_states": hidden_rows,
    }


def update_library_book_organization(
    book_id: str,
    payload: LibraryOrganizationPatch,
    *,
    source: str = "library_web_organization",
) -> dict[str, Any]:
    book = book_with_latest_file(book_id)
    with db.connect() as conn:
        current = conn.execute(
            "SELECT metadata FROM reader.library_state WHERE book_id = %s",
            (book_id,),
        ).fetchone()
        metadata = dict((current or {}).get("metadata") or {})
        if payload.favorite is not None:
            metadata["favorite"] = bool(payload.favorite)
        if payload.custom_category is not None:
            category = re.sub(r"\s+", " ", payload.custom_category).strip()
            if category:
                metadata["custom_category"] = category[:48]
            else:
                metadata.pop("custom_category", None)
        if payload.tags is not None:
            tags = []
            seen_tags: set[str] = set()
            for raw_tag in payload.tags:
                tag = re.sub(r"\s+", " ", str(raw_tag or "")).strip()
                if tag and tag not in seen_tags:
                    tags.append(tag[:32])
                    seen_tags.add(tag)
            if tags:
                metadata["tags"] = tags[:12]
            else:
                metadata.pop("tags", None)
        row = conn.execute(
            """
            INSERT INTO reader.library_state (book_id, hidden, source, metadata, created_at, updated_at)
            VALUES (%s, false, %s, %s, now(), now())
            ON CONFLICT (book_id) DO UPDATE
            SET metadata = EXCLUDED.metadata,
                source = EXCLUDED.source,
                hidden = reader.library_state.hidden,
                updated_at = now()
            RETURNING *
            """,
            (book_id, source, db.jsonb(metadata)),
        ).fetchone()
    return {
        "ok": True,
        "schema": "sentence_reader.library_organization.v1",
        "book_id": book_id,
        "book": jsonable(dict(book)),
        "library_state": jsonable(dict(row)),
        "organization": {
            "favorite": bool(metadata.get("favorite") or False),
            "custom_category": metadata.get("custom_category") or "",
            "category": metadata.get("custom_category") or "未分类",
            "tags": metadata.get("tags") or [],
        },
    }


def update_library_books_organization(payload: LibraryBatchOrganizationPatch) -> dict[str, Any]:
    unique_ids: list[str] = []
    seen: set[str] = set()
    for raw_id in payload.book_ids:
        book_id = str(raw_id or "").strip()
        if book_id and book_id not in seen:
            unique_ids.append(book_id)
            seen.add(book_id)
    if not unique_ids:
        raise HTTPException(status_code=422, detail="book_ids is empty")
    if payload.favorite is None and payload.custom_category is None and payload.tags is None:
        raise HTTPException(status_code=422, detail="organization patch is empty")

    patch = LibraryOrganizationPatch(
        favorite=payload.favorite,
        custom_category=payload.custom_category,
        tags=payload.tags,
    )
    results = [update_library_book_organization(book_id, patch) for book_id in unique_ids]
    return {
        "ok": True,
        "schema": "sentence_reader.library_batch_organization.v1",
        "book_ids": unique_ids,
        "affected_count": len(results),
        "results": results,
    }


def library_page_html() -> str:
    return r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>Sentence Reader Library</title>
  <script>
    (function(){
      var forcedModern = /(?:^|[?&])ui=modern(?:&|$)/.test(window.location.search || '');
      var ok = !!(window.Promise && window.fetch && document.querySelector && window.addEventListener);
      if (!forcedModern && !ok) window.location.replace('/library-lite?reason=capability');
    }());
  </script>
  <style>
    :root { color-scheme: dark; --bg:#090b10; --panel:#11151d; --panel-2:#171c26; --line:#283142; --text:#f5f7fb; --muted:#9ca8ba; --blue:#4f8cff; --green:#28c76f; --red:#ff5f57; --amber:#ffb020; }
    * { box-sizing:border-box; }
    html, body { margin:0; min-height:100%; background:var(--bg); color:var(--text); font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif; }
    button, input, select { font:inherit; }
    button { border:1px solid var(--line); background:#1d2634; color:var(--text); border-radius:7px; padding:8px 11px; cursor:pointer; }
    button.primary { background:var(--blue); border-color:var(--blue); color:white; }
    button.ghost { background:transparent; }
    button.danger { border-color:rgba(255,95,87,.52); color:#ffb5b0; background:rgba(255,95,87,.08); }
    button:disabled { opacity:.48; cursor:default; }
    input, select { width:100%; border:1px solid var(--line); background:#0d1118; color:var(--text); border-radius:7px; padding:9px 10px; }
    .app { min-height:100vh; display:grid; grid-template-columns:236px minmax(0,1fr) 336px; }
    .sidebar { border-right:1px solid var(--line); background:#0d1118; padding:18px 14px; position:sticky; top:0; height:100vh; }
    .brand { display:flex; align-items:center; gap:10px; margin-bottom:22px; }
    .brand-mark { width:36px; height:36px; border-radius:8px; background:linear-gradient(135deg,#4f8cff,#28c76f); display:grid; place-items:center; font-weight:800; }
    .brand strong { display:block; font-size:15px; }
    .brand span { display:block; color:var(--muted); font-size:12px; margin-top:2px; }
    .nav { display:grid; gap:7px; }
    .nav button { width:100%; text-align:left; display:flex; justify-content:space-between; background:transparent; }
    .nav button.active { background:#182235; border-color:#31517e; }
    .main { min-width:0; padding:18px 20px 28px; }
    .toolbar { display:grid; grid-template-columns:minmax(180px,1fr) 146px 126px auto; gap:10px; align-items:center; margin-bottom:16px; }
    .hero { display:grid; grid-template-columns:1.2fr .8fr; gap:14px; margin-bottom:16px; }
    .metric-row { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; }
    .metric, .panel, .book-card, .detail { border:1px solid var(--line); background:var(--panel); border-radius:8px; }
    .metric { padding:14px; }
    .metric b { display:block; font-size:22px; margin-bottom:4px; }
    .metric span { color:var(--muted); font-size:12px; }
    .section-title { display:flex; align-items:center; justify-content:space-between; margin:8px 0 10px; }
    .section-title h1 { margin:0; font-size:22px; letter-spacing:0; }
    .section-title small { color:var(--muted); }
    .books { display:grid; grid-template-columns:repeat(auto-fill,minmax(178px,1fr)); gap:12px; }
    .books.list { grid-template-columns:1fr; }
    .book-card { min-width:0; padding:12px; text-align:left; transition:border-color .12s ease, background .12s ease; }
    .book-card:hover, .book-card.selected { border-color:#4f8cff; background:#151c28; }
    .cover { width:100%; aspect-ratio:3/4; border-radius:7px; background:linear-gradient(160deg,#22314a,#11151d 62%,#243f36); border:1px solid #2d394d; display:flex; align-items:flex-end; padding:12px; margin-bottom:10px; overflow:hidden; }
    .cover span { display:block; font-weight:700; line-height:1.32; word-break:break-word; }
    .book-title { font-weight:700; line-height:1.35; min-height:38px; }
    .book-meta, .book-path, .muted { color:var(--muted); font-size:12px; }
    .book-path { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; margin-top:4px; }
    .progress { height:7px; background:#252d3a; border-radius:99px; overflow:hidden; margin:10px 0 8px; }
    .progress > i { display:block; height:100%; background:var(--green); width:0; }
    .badges { display:flex; flex-wrap:wrap; gap:5px; margin-top:8px; }
    .badge { border:1px solid var(--line); color:var(--muted); border-radius:999px; padding:2px 7px; font-size:11px; }
    .badge.green { color:#9af0bf; border-color:rgba(40,199,111,.35); }
    .badge.red { color:#ffb5b0; border-color:rgba(255,95,87,.35); }
    .detail { border-left:1px solid var(--line); background:#0d1118; padding:18px 16px; position:sticky; top:0; height:100vh; overflow:auto; }
    .detail h2 { margin:8px 0 6px; font-size:20px; line-height:1.35; }
    .detail-actions { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin:14px 0; }
    .detail-actions button.primary { grid-column:1 / -1; }
    .detail-stat { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; margin:12px 0; }
    .detail-stat div { background:var(--panel); border:1px solid var(--line); border-radius:7px; padding:9px; }
    .detail-stat b { display:block; font-size:18px; }
    .detail-stat span { display:block; color:var(--muted); font-size:11px; margin-top:2px; }
    .drop { border:1px dashed #49617f; background:#101722; border-radius:8px; padding:14px; margin-top:14px; }
    .drop strong { display:block; margin-bottom:6px; }
    .toast { position:fixed; left:50%; bottom:20px; transform:translateX(-50%); background:#162237; border:1px solid #355581; padding:10px 14px; border-radius:8px; opacity:0; pointer-events:none; transition:opacity .16s ease; z-index:20; }
    .toast.show { opacity:1; }
    @media (max-width: 980px) {
      .app { grid-template-columns:1fr; }
      .sidebar { position:static; height:auto; border-right:0; border-bottom:1px solid var(--line); }
      .nav { grid-template-columns:repeat(3,minmax(0,1fr)); }
      .detail { position:static; height:auto; border-left:0; border-top:1px solid var(--line); }
      .toolbar { grid-template-columns:1fr 1fr; }
      .hero { grid-template-columns:1fr; }
    }
    @media (max-width: 620px) {
      .main { padding:14px 12px 20px; }
      .metric-row { grid-template-columns:repeat(2,minmax(0,1fr)); }
      .books { grid-template-columns:repeat(2,minmax(0,1fr)); }
      .toolbar { grid-template-columns:1fr; }
      .nav { grid-template-columns:1fr 1fr; }
    }
  </style>
</head>
<body>
  <div class="app" data-ui-style="tabler-inspired" data-structure-reference="komga-style-library">
    <aside class="sidebar">
      <div class="brand"><div class="brand-mark">C</div><div><strong>Click</strong><span>本地书库</span></div></div>
      <nav class="nav" id="nav"></nav>
      <div class="drop">
        <strong>导入 EPUB / PDF</strong>
        <div class="muted">文件会复制到 Mac 内部书库，原文件可删除。</div>
        <input id="fileInput" type="file" accept=".epub,.pdf,application/epub+zip,application/pdf" style="margin-top:10px">
      </div>
    </aside>
    <main class="main">
      <div class="section-title">
        <h1>书库</h1>
        <small id="updatedAt">正在加载...</small>
      </div>
      <div class="toolbar">
        <input id="search" placeholder="搜索书名、作者、路径">
        <select id="sort">
          <option value="recent">最近阅读</option>
          <option value="title">书名</option>
          <option value="notes">笔记最多</option>
          <option value="red">红标最多</option>
        </select>
        <select id="view">
          <option value="grid">封面墙</option>
          <option value="list">列表</option>
        </select>
        <button class="primary" id="refresh">刷新</button>
      </div>
      <section class="hero">
        <div class="metric-row">
          <div class="metric"><b id="metricBooks">0</b><span>书籍</span></div>
          <div class="metric"><b id="metricNotes">0</b><span>笔记</span></div>
          <div class="metric"><b id="metricRed">0</b><span>红标</span></div>
          <div class="metric"><b id="metricHidden">0</b><span>隐藏</span></div>
        </div>
        <div class="panel" style="padding:14px">
          <strong>主界面边界</strong>
          <p class="muted" style="margin:8px 0 0">这是 Reader API + PostgreSQL 的单系统主界面。Tabler 只做视觉风格，Komga 只做书库结构参考。</p>
        </div>
      </section>
      <div class="section-title"><h1 id="listTitle">全部书籍</h1><small id="listCount">0 本</small></div>
      <section id="books" class="books"></section>
    </main>
    <aside class="detail" id="detail"></aside>
  </div>
  <div id="toast" class="toast"></div>
  <script>
    const state = { dashboard:null, books:[], selected:null, filter:'library', query:'', view:'grid', sort:'recent' };
    const $ = (id) => document.getElementById(id);
    const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    function isMacAppSurface() { return new URLSearchParams(window.location.search).get('surface') === 'mac-app'; }
    function toast(text) { const node = $('toast'); node.textContent = text; node.classList.add('show'); setTimeout(() => node.classList.remove('show'), 2600); }
    async function api(url, options) {
      const response = await fetch(url, options);
      if (!response.ok) throw new Error(await response.text());
      return response.json();
    }
    function navItems() {
      const counts = state.dashboard ? state.dashboard.summary : {};
      return [
        ['library','书库', counts.book_count || 0],
        ['recent','最近阅读',''],
        ['notes','笔记', counts.note_count || 0],
        ['red','红标', counts.red_highlight_count || 0],
        ['import','导入',''],
        ['settings','设置','']
      ];
    }
    function renderNav() {
      $('nav').innerHTML = navItems().map(([id,title,count]) => `<button class="${state.filter === id ? 'active' : ''}" data-filter="${id}"><span>${title}</span><span>${count}</span></button>`).join('');
      $('nav').querySelectorAll('button').forEach((button) => button.onclick = () => {
        state.filter = button.dataset.filter;
        if (state.filter === 'import') $('fileInput').click();
        render();
      });
    }
    function bookMatches(book) {
      const query = state.query.trim().toLowerCase();
      if (query) {
        const haystack = [book.title, book.author, book.file.file_path].join(' ').toLowerCase();
        if (!haystack.includes(query)) return false;
      }
      if (state.filter === 'notes') return book.counts.notes > 0;
      if (state.filter === 'red') return book.counts.red_highlights > 0;
      if (state.filter === 'recent') return book.progress.has_position || book.last_opened_at;
      return true;
    }
    function sortedBooks() {
      const books = state.books.filter(bookMatches);
      books.sort((a,b) => {
        if (state.sort === 'title') return String(a.title || '').localeCompare(String(b.title || ''), 'zh-Hans-CN');
        if (state.sort === 'notes') return b.counts.notes - a.counts.notes;
        if (state.sort === 'red') return b.counts.red_highlights - a.counts.red_highlights;
        return String(b.recent_activity_at || '').localeCompare(String(a.recent_activity_at || ''));
      });
      return books;
    }
    function coverText(book) {
      const title = book.title || 'Untitled';
      return title.length > 34 ? `${title.slice(0,34)}...` : title;
    }
    function renderBooks() {
      const books = sortedBooks();
      $('books').className = `books ${state.view === 'list' ? 'list' : ''}`;
      $('listTitle').textContent = state.filter === 'notes' ? '有笔记的书' : state.filter === 'red' ? '有红标的书' : state.filter === 'recent' ? '最近阅读' : '全部书籍';
      $('listCount').textContent = `${books.length} 本`;
      if (!books.length) {
        $('books').innerHTML = `<div class="panel" style="padding:18px">没有匹配的书。可以导入 EPUB 或 PDF，或者换一个筛选条件。</div>`;
        return;
      }
      $('books').innerHTML = books.map((book) => `
        <button class="book-card ${state.selected && state.selected.id === book.id ? 'selected' : ''}" data-book="${esc(book.id)}">
          <div class="cover"><span>${esc(coverText(book))}</span></div>
          <div class="book-title">${esc(book.title || book.id)}</div>
          <div class="book-meta">${esc(book.author || '未知作者')}</div>
          <div class="progress"><i style="width:${book.progress.percent}%"></i></div>
          <div class="book-meta">${book.progress.percent}% · ${book.reader_capabilities?.mac_native ? 'Mac 可阅读' : (book.status.lan_available ? '可阅读' : '文件不可用')}</div>
          <div class="badges">
            <span class="badge">${book.counts.notes} 笔记</span>
            <span class="badge red">${book.counts.red_highlights} 红标</span>
            ${book.file.owned_internal_copy ? '<span class="badge green">内部副本</span>' : ''}
          </div>
          <div class="book-path">${esc(book.file.file_path || '')}</div>
        </button>
      `).join('');
      $('books').querySelectorAll('.book-card').forEach((button) => button.onclick = () => {
        state.selected = state.books.find((book) => book.id === button.dataset.book);
        render();
      });
    }
    function renderDetail() {
      const book = state.selected || state.books[0];
      if (!book) {
        $('detail').innerHTML = `<h2>还没有书</h2><p class="muted">先导入一本 EPUB 或 PDF。</p>`;
        return;
      }
      state.selected = book;
      $('detail').innerHTML = `
        <div class="cover" style="height:260px; aspect-ratio:auto"><span>${esc(coverText(book))}</span></div>
        <h2>${esc(book.title || book.id)}</h2>
        <div class="muted">${esc(book.author || '未知作者')}</div>
        <div class="detail-stat">
          <div><b>${book.progress.percent}%</b><span>进度</span></div>
          <div><b>${book.counts.notes}</b><span>笔记</span></div>
          <div><b>${book.counts.red_highlights}</b><span>红标</span></div>
        </div>
        <div class="detail-actions">
          <button class="primary" id="continue" ${(isMacAppSurface() && book.reader_capabilities?.mac_native) || book.status.lan_available ? '' : 'disabled'}>继续阅读</button>
          <button id="showNotes">看笔记</button>
          <button id="showRed">看红标</button>
          <button id="reveal">显示副本</button>
          <button class="danger" id="hide">从书库移除</button>
        </div>
        <p class="muted">内部副本：${book.file.owned_internal_copy ? '是' : '否'}<br>文件存在：${book.file.exists ? '是' : '否'}<br>${esc(book.file.file_path || '')}</p>
      `;
      $('continue').onclick = () => {
        if (isMacAppSurface() && book.actions.native_reader_url) {
          window.location.href = book.actions.native_reader_url;
          return;
        }
        if (book.actions.continue_reading_url) window.location.href = book.actions.continue_reading_url;
      };
      $('showNotes').onclick = () => { state.filter = 'notes'; render(); };
      $('showRed').onclick = () => { state.filter = 'red'; render(); };
      $('reveal').onclick = async () => { await api(`/api/library/books/${book.id}/reveal`, { method:'POST' }); toast('已在 Mac Finder 中显示内部副本'); };
      $('hide').onclick = async () => {
        if (!confirm(`从书库移除《${book.title || book.id}》？不会删除 EPUB、笔记或数据库数据。`)) return;
        await api(`/api/library/books/${book.id}/hide`, { method:'POST' });
        toast('已从书库列表移除，数据保留');
        await loadDashboard();
      };
    }
    function renderMetrics() {
      const summary = state.dashboard ? state.dashboard.summary : {};
      $('metricBooks').textContent = summary.book_count || 0;
      $('metricNotes').textContent = summary.note_count || 0;
      $('metricRed').textContent = summary.red_highlight_count || 0;
      $('metricHidden').textContent = summary.hidden_count || 0;
      $('updatedAt').textContent = state.dashboard ? `更新于 ${new Date(state.dashboard.generated_at).toLocaleString()}` : '未连接';
    }
    function render() { renderNav(); renderMetrics(); renderBooks(); renderDetail(); }
    async function loadDashboard() {
      state.dashboard = await api('/api/library/dashboard');
      state.books = state.dashboard.books || [];
      if (state.selected) state.selected = state.books.find((book) => book.id === state.selected.id) || state.books[0] || null;
      else state.selected = state.dashboard.current_book || state.books[0] || null;
      render();
    }
    async function importFile(file) {
      const buffer = await file.arrayBuffer();
      let binary = '';
      const bytes = new Uint8Array(buffer);
      for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
      toast('正在导入书籍...');
      await api('/api/library/import', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({ filename:file.name, content_base64:btoa(binary) })
      });
      toast('导入完成，原文件可删除');
      await loadDashboard();
    }
    $('search').oninput = (event) => { state.query = event.target.value; renderBooks(); };
    $('sort').onchange = (event) => { state.sort = event.target.value; renderBooks(); };
    $('view').onchange = (event) => { state.view = event.target.value; renderBooks(); };
    $('refresh').onclick = () => loadDashboard().catch((error) => toast(`刷新失败：${error.message}`));
    $('fileInput').onchange = (event) => {
      const file = event.target.files && event.target.files[0];
      if (file) importFile(file).catch((error) => toast(`导入失败：${error.message}`));
      event.target.value = '';
    };
    loadDashboard().catch((error) => {
      $('books').innerHTML = `<div class="panel" style="padding:18px">主界面加载失败：${esc(error.message)}</div>`;
      toast('主界面加载失败');
    });
  </script>
</body>
</html>"""


class LiteHTMLTextExtractor(HTMLParser):
    block_tags = {"p", "section", "article", "li", "br", "h1", "h2", "h3", "h4", "blockquote"}
    skip_tags = {"head", "script", "style", "nav", "svg"}
    skip_class_tokens = {"nav", "navigation", "toc", "calibre_nav"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    @staticmethod
    def attr_value(attrs: list[tuple[str, Optional[str]]], name: str) -> str:
        for key, value in attrs:
            if key.lower() == name:
                return str(value or "")
        return ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag_name = tag.lower()
        if self.skip_depth:
            self.skip_depth += 1
            return
        class_tokens = set(re.split(r"\s+", self.attr_value(attrs, "class").strip().lower()))
        role = self.attr_value(attrs, "role").strip().lower()
        epub_type = self.attr_value(attrs, "epub:type").strip().lower()
        if (
            tag_name in self.skip_tags
            or bool(class_tokens & self.skip_class_tokens)
            or role in {"navigation", "doc-toc"}
            or epub_type == "toc"
        ):
            self.skip_depth = 1
            return
        if tag_name in self.block_tags:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.skip_depth:
            self.skip_depth = max(0, self.skip_depth - 1)

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        text = str(data or "").strip()
        if text:
            self.parts.append(text)

    def text(self) -> str:
        raw = " ".join(self.parts).replace("\xa0", " ")
        raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
        raw = re.sub(r"\s*\n\s*", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


LITE_INLINE_NAV_RE = re.compile(
    r"^(上一篇|下一篇|回目录|回页首|回页頂|上一章|下一章|previous|next|contents|top)(\s*[|｜/·,，;；-]\s*(上一篇|下一篇|回目录|回页首|回页頂|上一章|下一章|previous|next|contents|top))*$",
    flags=re.IGNORECASE,
)


def lite_clean_text(text: str) -> str:
    lines: list[str] = []
    for line in re.split(r"\n+", str(text or "")):
        clean = re.sub(r"\s+", " ", line).strip()
        if not clean:
            continue
        nav_compact = re.sub(r"\s+", "", clean)
        if nav_compact in {"上一篇回目录下一篇", "上一篇回页首回目录下一篇", "回目录", "回页首", "上一章下一章"}:
            continue
        if LITE_INLINE_NAV_RE.match(clean):
            continue
        lines.append(clean)
    return "\n".join(lines).strip()


def lite_extract_text(html_text: str) -> str:
    parser = LiteHTMLTextExtractor()
    parser.feed(str(html_text or ""))
    parser.close()
    return lite_clean_text(parser.text())


def lite_split_sentences(text: str) -> list[str]:
    sentences: list[str] = []
    for paragraph in re.split(r"\n+", str(text or "")):
        paragraph = re.sub(r"\s+", " ", paragraph).strip()
        if not paragraph:
            continue
        for part in re.split(r"(?<=[。！？!?])\s*|(?<=[.!?])\s+", paragraph):
            clean = part.strip()
            if not clean:
                continue
            if len(clean) <= 520:
                sentences.append(clean)
            else:
                for index in range(0, len(clean), 420):
                    chunk = clean[index : index + 420].strip()
                    if chunk:
                        sentences.append(chunk)
    return sentences or ([text.strip()] if str(text or "").strip() else [])


def lite_chapter_payload(book_id: str, chapter_index: int) -> dict[str, Any]:
    book = book_with_latest_file(book_id)
    epub_path = epub_path_for_book(book)
    publication = epub_publication(epub_path, book=book)
    chapters = publication["chapters"]
    if chapter_index < 0 or chapter_index >= len(chapters):
        raise HTTPException(status_code=404, detail="chapter not found")
    chapter = chapters[chapter_index]
    with zipfile.ZipFile(epub_path) as epub:
        try:
            raw_html = zip_text(epub, chapter["href"])
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="chapter asset missing") from exc
    text = lite_extract_text(raw_html)
    return {
        "book": jsonable(book),
        "publication": publication,
        "chapters": chapters,
        "chapter": chapter,
        "chapter_index": chapter_index,
        "sentences": lite_split_sentences(text),
    }


LITE_FRONT_MATTER_RE = re.compile(
    r"(版权|图书在版|CIP|ISBN|责任编辑|版权所有|书名原文|中国版本图书馆)",
    flags=re.IGNORECASE,
)
LITE_FRONT_MATTER_TITLE_RE = re.compile(
    r"^(cover|title\s*page|封面|扉页|目录|目次|table\s+of\s+contents|contents|版权|版权页|copyright)$",
    flags=re.IGNORECASE,
)
LITE_FRONT_MATTER_HREF_RE = re.compile(
    r"(^|/)(cover[^/]*|titlepage|title-page|toc|contents?|nav|copyright|[^/]*index|section0*1)\.(x?html?|xml)$",
    flags=re.IGNORECASE,
)
LITE_READING_CHAPTER_RE = re.compile(
    r"(第\s*\d+\s*[章节篇]|第[一二三四五六七八九十百零〇]+[章节篇]|引言|前言|序|推荐序|正文|Chapter)",
    flags=re.IGNORECASE,
)
LITE_MAIN_CHAPTER_RE = re.compile(
    r"(第\s*\d+\s*[章节篇]|第[一二三四五六七八九十百零〇]+[章节篇]|Chapter\s+\d+)",
    flags=re.IGNORECASE,
)
LITE_FIRST_MAIN_CHAPTER_RE = re.compile(
    r"(第\s*1\s*[章节篇]|第一[章节篇]|Chapter\s+1\b)",
    flags=re.IGNORECASE,
)
LITE_SCREEN_PAGED_MODE = "screen_paged"


def lite_is_probable_front_matter(chapter: dict[str, Any], toc_title: str = "") -> bool:
    title = re.sub(r"\s+", " ", str(toc_title or chapter.get("title") or "")).strip()
    href = str(chapter.get("href") or chapter.get("locator") or "").strip()
    if LITE_FRONT_MATTER_TITLE_RE.search(title):
        return True
    if LITE_FRONT_MATTER_HREF_RE.search(href):
        return True
    return False


def lite_reading_order(book_id: str) -> dict[str, Any]:
    book = book_with_latest_file(book_id)
    epub_path = epub_path_for_book(book)
    publication = epub_publication(epub_path, book=book)
    toc_by_chapter: dict[int, dict[str, Any]] = {}
    for entry in publication.get("toc") or []:
        try:
            chapter_index = int(entry.get("chapter_index"))
        except (TypeError, ValueError):
            continue
        current = toc_by_chapter.get(chapter_index)
        if current is None or int(entry.get("level") or 0) < int(current.get("level") or 0):
            toc_by_chapter[chapter_index] = dict(entry)
    reading_order: list[dict[str, Any]] = []
    for chapter in publication["chapters"]:
        index = int(chapter.get("index") or 0)
        toc_entry = toc_by_chapter.get(index) or {}
        toc_title = str(toc_entry.get("title") or "").strip()
        title = toc_title or str(chapter.get("title") or "").strip() or f"第 {index + 1} 节"
        reading_order.append(
            {
                "book_id": book_id,
                "chapter_index": index,
                "href": str(chapter.get("href") or ""),
                "title": str(chapter.get("title") or ""),
                "toc_title": toc_title,
                "display_title": title,
                "level": int(toc_entry.get("level") or 0),
                "is_probable_front_matter": lite_is_probable_front_matter(chapter, toc_title),
            }
        )
    return {"book": jsonable(book), "publication": publication, "reading_order": reading_order}


def lite_chapter_readability_score(payload: dict[str, Any]) -> int:
    sentences = [str(item or "").strip() for item in (payload.get("sentences") or []) if str(item or "").strip()]
    if not sentences:
        return 0
    chapter = payload.get("chapter") or {}
    title = str(chapter.get("title") or "")
    preview = " ".join(sentences[:12])
    if LITE_FRONT_MATTER_RE.search(title) or LITE_FRONT_MATTER_RE.search(preview[:700]):
        return 0
    score = min(len(preview), 2000) + min(len(sentences), 30) * 20
    if LITE_READING_CHAPTER_RE.search(title) or LITE_READING_CHAPTER_RE.search(preview[:120]):
        score += 1000
    return score


def lite_first_readable_chapter_payload(
    book_id: str,
    chapter_index: int,
    *,
    prefer_main_chapter: bool = False,
) -> tuple[dict[str, Any], bool]:
    manifest = lite_reading_order(book_id)
    reading_order = manifest["reading_order"]
    chapter_count = len(reading_order)
    if chapter_count <= 1:
        return lite_chapter_payload(book_id, chapter_index), False
    if chapter_index < 0 or chapter_index >= chapter_count:
        chapter_index = 0
    scan_order = list(range(chapter_index + 1, chapter_count)) + list(range(0, chapter_index))
    current_entry = reading_order[chapter_index]
    current = lite_chapter_payload(book_id, chapter_index)
    current_score = lite_chapter_readability_score(current)
    current_title = str(current_entry.get("display_title") or "")
    if current_score > 0 and not current_entry.get("is_probable_front_matter") and (
        not prefer_main_chapter or LITE_MAIN_CHAPTER_RE.search(current_title)
    ):
        return current, False
    fallback: Optional[tuple[dict[str, Any], bool]] = None
    main_fallback: Optional[tuple[dict[str, Any], bool]] = None
    for candidate_index in scan_order:
        entry = reading_order[candidate_index]
        if entry.get("is_probable_front_matter"):
            continue
        candidate = lite_chapter_payload(book_id, candidate_index)
        score = lite_chapter_readability_score(candidate)
        if score <= 0:
            continue
        candidate_title = str(entry.get("display_title") or (candidate.get("chapter") or {}).get("title") or "")
        candidate_preview = " ".join(str(item or "") for item in (candidate.get("sentences") or [])[:3])
        candidate_label = f"{candidate_title} {candidate_preview[:160]}"
        if not prefer_main_chapter:
            return candidate, True
        if LITE_FIRST_MAIN_CHAPTER_RE.search(candidate_label):
            return candidate, True
        if LITE_MAIN_CHAPTER_RE.search(candidate_label) and main_fallback is None:
            main_fallback = (candidate, True)
        if fallback is None:
            fallback = (candidate, True)
    if main_fallback is not None:
        return main_fallback
    if fallback is not None:
        return fallback
    return current, False


def lite_adjacent_readable_chapter_index(book_id: str, chapter_index: int, direction: int) -> Optional[int]:
    manifest = lite_reading_order(book_id)
    reading_order = manifest["reading_order"]
    if not reading_order:
        return None
    indexes = [int(entry.get("chapter_index") or 0) for entry in reading_order]
    try:
        current_position = indexes.index(int(chapter_index))
    except ValueError:
        current_position = 0
    step = 1 if direction >= 0 else -1
    position = current_position + step
    while 0 <= position < len(reading_order):
        entry = reading_order[position]
        if not entry.get("is_probable_front_matter"):
            return int(entry.get("chapter_index") or 0)
        position += step
    return None


def lite_chapter_index_for_locator(book_id: str, chapter_locator: str) -> Optional[int]:
    locator = str(chapter_locator or "").split("#", 1)[0].strip()
    if not locator:
        return None
    try:
        book = book_with_latest_file(book_id)
        publication = epub_publication(epub_path_for_book(book), book=book)
    except Exception:
        return None
    for chapter in publication.get("chapters") or []:
        if locator in {str(chapter.get("locator") or ""), str(chapter.get("href") or "")}:
            return int(chapter.get("index") or 0)
    return None


def lite_continue_href(book_id: str, progress: dict[str, Any]) -> str:
    if progress.get("has_position"):
        chapter_index = lite_chapter_index_for_locator(book_id, str(progress.get("chapter_locator") or ""))
        if chapter_index is not None:
            return f"/reader-lite?{lite_query(book_id=book_id, chapter=chapter_index, page=int(progress.get('page_index') or 0), auto=0)}"
    return f"/reader-lite?{lite_query(book_id=book_id, auto=1)}"


def lite_reading_progress_ratio(book_id: str, chapter_index: int, page_index: int = 0, total_pages: int = 1) -> float:
    try:
        manifest = lite_reading_order(book_id)
    except Exception:
        return 0.0
    readable = [
        int(entry.get("chapter_index") or 0)
        for entry in manifest.get("reading_order") or []
        if not entry.get("is_probable_front_matter")
    ]
    if not readable:
        return 0.0
    try:
        position = readable.index(int(chapter_index))
    except ValueError:
        return 0.0
    safe_total_pages = max(1, int(total_pages or 1))
    safe_page = max(0, min(int(page_index or 0), safe_total_pages - 1))
    intra_chapter = safe_page / float(safe_total_pages)
    return max(0.0, min(1.0, (position + intra_chapter) / float(len(readable))))


def lite_upsert_reading_position(
    book_id: str,
    chapter: dict[str, Any],
    chapter_index: int,
    *,
    page_index: int = 0,
    total_pages: int = 1,
    first_sentence_index: Optional[str] = None,
) -> None:
    safe_total_pages = max(1, int(total_pages or 1))
    safe_page = max(0, min(int(page_index or 0), safe_total_pages - 1))
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO reader.reading_positions (
                book_id, chapter_id, chapter_locator, page_index, total_pages, page_ratio, locator, updated_at
            )
            VALUES (%s, NULL, %s, %s, %s, %s, %s, now())
            ON CONFLICT (book_id) DO UPDATE
            SET chapter_locator = EXCLUDED.chapter_locator,
                page_index = EXCLUDED.page_index,
                total_pages = EXCLUDED.total_pages,
                page_ratio = EXCLUDED.page_ratio,
                locator = EXCLUDED.locator,
                updated_at = now()
            """,
            (
                book_id,
                str(chapter.get("locator") or chapter.get("href") or ""),
                safe_page,
                safe_total_pages,
                lite_reading_progress_ratio(book_id, chapter_index, safe_page, safe_total_pages),
                db.jsonb(
                    {
                        "source": "ClickLiteReader",
                        "chapterIndex": chapter_index,
                        "pageIndex": safe_page,
                        "totalPages": safe_total_pages,
                        "firstSentenceIndex": str(first_sentence_index or ""),
                        "chapterTitle": str(chapter.get("title") or ""),
                        "mode": LITE_SCREEN_PAGED_MODE,
                    }
                ),
            ),
        )


def lite_sentence_index(row: dict[str, Any]) -> str:
    metadata = row.get("metadata") or {}
    if isinstance(metadata, dict) and metadata.get("sentenceIndex") is not None:
        return str(metadata.get("sentenceIndex"))
    locator = row.get("range_locator") or {}
    if isinstance(locator, dict):
        for key in ("sentenceIndex", "sentence_index"):
            if locator.get(key) is not None:
                return str(locator.get(key))
    return ""


def lite_annotations_by_sentence(book_id: str, chapter_locator: str) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for row in list_annotations(book_id):
        if str(row.get("chapter_locator") or "") != str(chapter_locator or ""):
            continue
        sentence_index = lite_sentence_index(row)
        if not sentence_index:
            continue
        bucket = output.setdefault(sentence_index, {})
        if row.get("kind") == "red_highlight":
            bucket["red"] = row
        elif row.get("kind") == "note":
            bucket["note"] = row
    return output


def lite_query(**params: Any) -> str:
    parts = []
    for key, value in params.items():
        if value is None:
            continue
        parts.append(f"{quote(str(key))}={quote(str(value))}")
    return "&".join(parts)


def library_lite_html(request: FastAPIRequest) -> str:
    payload = library_dashboard_payload(include_hidden=False)
    rows: list[str] = []
    for book in payload.get("books") or []:
        counts = book.get("counts") or {}
        progress = book.get("progress") or {}
        status = book.get("status") or {}
        title = html_escape(str(book.get("title") or "未命名书籍"))
        author = html_escape(str(book.get("author") or "未知作者"))
        meta = f"{int(progress.get('percent') or 0)}% · 备注 {int(counts.get('notes') or 0)} · 红标 {int(counts.get('red_highlights') or 0)}"
        if status.get("lan_available"):
            book_id = str(book.get("id") or "")
            continue_href = lite_continue_href(book_id, progress)
            toc_href = f"/reader-lite/toc?book_id={quote(book_id)}"
            action = f'<div class="actions"><a class="open primary" href="{continue_href}">继续读</a><a class="open secondary" href="{toc_href}">目录</a></div>'
        else:
            action = '<span class="disabled">此书暂不支持 Lite 阅读</span>'
        rows.append(
            f"""<li>
  <div><strong>{title}</strong><span>{author}</span><em>{html_escape(meta)}</em></div>
  {action}
</li>"""
        )
    if not rows:
        rows.append('<li><div><strong>暂无书籍</strong><span>请先在现代书库导入 EPUB。</span></div></li>')
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Click Lite 书库</title>
  <style>
    body{{margin:0;background:#080806;color:#f6f0e8;font-family:Arial,"Microsoft YaHei",sans-serif}}
    main{{max-width:760px;margin:0 auto;padding:18px 14px 34px}}
    h1{{font-size:30px;margin:0 0 12px}}
    a{{color:#f0d36b;text-decoration:none}}
    .top{{margin-bottom:16px}}
    ul{{list-style:none;margin:0;padding:0}}
    li{{border:1px solid #332f24;background:#14120d;margin:10px 0;padding:14px;overflow:hidden}}
    strong{{display:block;font-size:21px;line-height:1.25}}
    span,em{{display:block;color:#bdb4a5;font-size:14px;font-style:normal;margin-top:5px}}
    .actions{{display:flex;gap:8px;margin-top:12px}}
    .open,.disabled{{display:block;padding:12px 14px;background:#f0d36b;color:#15120a;font-weight:800;text-align:center}}
    .actions .open{{flex:1}}
    .secondary{{background:#211f18;color:#f0d36b;border:1px solid #4b3e20}}
    .disabled{{background:#2a2923;color:#aaa}}
  </style>
</head>
<body>
<main>
  <div class="top"><a href="/home-lite">返回首页</a></div>
  <h1>Click Lite 书库</h1>
  <ul>{''.join(rows)}</ul>
</main>
</body>
</html>"""


def reader_lite_html(
    request: FastAPIRequest,
    *,
    book_id: str,
    chapter_index: int,
    page_index: int,
    selected_sentence: Optional[str] = None,
    auto_skip: bool = False,
    notice: str = "",
) -> str:
    if auto_skip:
        payload, skipped_empty_chapter = lite_first_readable_chapter_payload(
            book_id,
            chapter_index,
            prefer_main_chapter=True,
        )
    else:
        payload = lite_chapter_payload(book_id, chapter_index)
        skipped_empty_chapter = False
    book = payload["book"]
    chapter = payload["chapter"]
    chapter_index = int(payload["chapter_index"])
    sentences = payload["sentences"]
    page_index = max(0, int(page_index or 0))
    lite_upsert_reading_position(book_id, chapter, chapter_index, page_index=page_index, total_pages=max(1, page_index + 1))
    prev_href = ""
    next_href = ""
    prev_chapter = lite_adjacent_readable_chapter_index(book_id, chapter_index, -1)
    if prev_chapter is not None:
        prev_href = f"/reader-lite?{lite_query(book_id=book_id, chapter=prev_chapter, page=0, auto=0)}"
    next_chapter = lite_adjacent_readable_chapter_index(book_id, chapter_index, 1)
    if next_chapter is not None:
        next_href = f"/reader-lite?{lite_query(book_id=book_id, chapter=next_chapter, page=0, auto=0)}"
    annotations = lite_annotations_by_sentence(book_id, str(chapter.get("locator") or ""))
    sentence_blocks: list[str] = []
    for offset, sentence in enumerate(sentences):
        sentence_index = str(offset)
        existing = annotations.get(sentence_index) or {}
        red = existing.get("red")
        note = existing.get("note")
        is_selected = str(selected_sentence or "") == sentence_index
        red_class = " red" if red else ""
        selected_class = " selected" if is_selected else ""
        selected_href = f"/reader-lite?{lite_query(book_id=book_id, chapter=chapter_index, page=page_index, selected=sentence_index, auto=0)}#s{sentence_index}"
        red_badge = '<span class="badge">已标红</span>' if red else ""
        note_badge = '<span class="badge">有备注</span>' if note else ""
        sentence_html = f"""<div class="sentence-unit" data-sentence-index="{html_escape(sentence_index)}"><p class="sentence{red_class}{selected_class}" id="s{sentence_index}">
  <a class="sentence-link" href="{selected_href}">{html_escape(sentence)}</a>
  {red_badge}{note_badge}
</p>"""
        if is_selected:
            note_text = html_escape(str((note or {}).get("note_text") or ""))
            red_label = "取消红标" if red else "标红"
            common_hidden = f"""
      <input type="hidden" name="book_id" value="{html_escape(book_id)}">
      <input type="hidden" name="chapter" value="{chapter_index}">
      <input type="hidden" name="page" value="{page_index}">
      <input type="hidden" name="selected" value="{html_escape(sentence_index)}">
      <input type="hidden" name="sentence_index" value="{html_escape(sentence_index)}">
      <input type="hidden" name="chapter_locator" value="{html_escape(str(chapter.get('locator') or ''))}">
      <input type="hidden" name="chapter_title" value="{html_escape(str(chapter.get('title') or ''))}">
      <input type="hidden" name="source_text" value="{html_escape(sentence)}">"""
            sentence_html += f"""<section class="selected-panel">
  <div class="selected-actions">
    <form class="action-form" method="post" action="/reader-lite/red">{common_hidden}<button type="submit">{red_label}</button></form>
    <details class="lite-action"><summary>备注</summary><form method="post" action="/reader-lite/note">{common_hidden}<textarea name="note_text" rows="3" placeholder="写备注">{note_text}</textarea><button type="submit">保存</button></form></details>
    <details class="lite-action"><summary>语音</summary><form method="post" enctype="multipart/form-data" action="/reader-lite/audio-note">{common_hidden}<input type="file" name="audio_file" accept="audio/*" capture><button type="submit">上传</button></form></details>
  </div>
</section>"""
        sentence_html += "</div>"
        sentence_blocks.append(sentence_html)
    if not sentence_blocks:
        sentence_blocks.append('<p class="sentence extraction-failed">此章节正文提取失败。</p>')
    prev_link = f'<a class="page-turn prev" id="prevPage" href="{prev_href}" aria-label="上一章">‹</a>' if prev_href else ""
    next_link = f'<a class="page-turn next" id="nextPage" href="{next_href}" aria-label="下一章">›</a>' if next_href else ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{html_escape(str(book.get('title') or 'Click Lite 阅读'))}</title>
  <style>
    html,body{{min-height:100%}}
    body{{margin:0;background:#080806;color:#f6f0e8;font-family:Arial,"Microsoft YaHei",sans-serif}}
    main{{max-width:760px;margin:0 auto;padding:4px 10px 78px}}
    a{{color:#f0d36b;text-decoration:none}}
    .article{{font-size:22px;line-height:1.58}}
    .sentence-unit{{display:block}}
    .sentence{{margin:0 0 12px;padding:1px 0 7px;border-bottom:1px solid #17140f}}
    .sentence.red{{background:#1c0f0d}}
    .sentence-link{{color:#f6f0e8;text-decoration:none}}
    .sentence.red .sentence-link{{color:#ffbeb8}}
    .sentence.selected{{background:#1d1a12;outline:1px solid #f0d36b;padding:6px}}
    .badge{{display:inline-block;margin:4px 6px 0 0;padding:2px 5px;background:#3a1714;color:#ffd4cd;font-size:11px;text-decoration:none}}
    .selected-panel{{border:1px solid #f0d36b;background:#17150f;padding:6px;margin:6px 0 1px}}
    .selected-actions{{display:flex;gap:6px;align-items:flex-start}}
    .action-form,.lite-action{{flex:1;min-width:0;margin:0}}
    .lite-action form{{margin-top:6px}}
    summary{{cursor:pointer;color:#15120a;background:#f0d36b;font-weight:800;padding:9px 4px;text-align:center;list-style:none}}
    summary::-webkit-details-marker{{display:none}}
    button{{width:100%;border:0;background:#f0d36b;color:#15120a;font-weight:800;padding:9px 4px;font-size:16px}}
    textarea{{width:100%;box-sizing:border-box;background:#090806;color:#f6f0e8;border:1px solid #383226;padding:10px;font-size:16px}}
    input[type=file]{{display:block;width:100%;box-sizing:border-box;margin-bottom:8px;color:#f6f0e8}}
    .page-turn{{position:fixed;top:35%;bottom:35%;z-index:20;width:34px;color:rgba(240,211,107,.18);font-size:38px;line-height:30vh;text-align:center;text-decoration:none;background:transparent}}
    .page-turn.prev{{left:0}}
    .page-turn.next{{right:0}}
    .page-turn:active,.page-turn:focus{{color:#f0d36b;background:rgba(240,211,107,.08);outline:0}}
    .lite-paged body{{height:100%;overflow:hidden}}
    .lite-paged main{{height:100vh;overflow:hidden;box-sizing:border-box;padding-bottom:78px}}
    .lite-paged .sentence-unit{{display:none}}
  </style>
</head>
<body>
{prev_link}{next_link}
<main>
  <article class="article">
    {''.join(sentence_blocks)}
  </article>
</main>
<script>
(function(){{
  document.documentElement.className += ' lite-paged';
  var bookId = {json.dumps(book_id)};
  var chapterIndex = {chapter_index};
  var initialPage = {page_index};
  var selectedSentence = {json.dumps(str(selected_sentence) if selected_sentence is not None else "")};
  var previousHref = {json.dumps(prev_href)};
  var nextHref = {json.dumps(next_href)};
  var startX = 0;
  var startY = 0;
  var previous = document.getElementById('prevPage');
  var next = document.getElementById('nextPage');
  var article = document.getElementsByClassName('article')[0];
  var units = article ? article.getElementsByClassName('sentence-unit') : [];
  var pages = [];
  var currentPage = 0;

  function viewportHeight() {{
    if (window.visualViewport && window.visualViewport.height) return window.visualViewport.height;
    return window.innerHeight || document.documentElement.clientHeight || document.body.clientHeight || 640;
  }}

  function bottomSafeReserve() {{
    return 84;
  }}

  function unitHeight(unit) {{
    unit.style.display = 'block';
    var height = unit.offsetHeight || 1;
    unit.style.display = 'none';
    return height;
  }}

  function buildPages() {{
    var maxHeight = Math.max(180, viewportHeight() - bottomSafeReserve());
    var page = [];
    var pageHeight = 0;
    pages = [];
    for (var index = 0; index < units.length; index += 1) {{
      var unit = units[index];
      var height = unitHeight(unit);
      if (page.length && pageHeight + height > maxHeight) {{
        pages.push(page);
        page = [];
        pageHeight = 0;
      }}
      page.push(unit);
      pageHeight += height;
    }}
    if (page.length) pages.push(page);
    if (!pages.length && units.length) pages.push([units[0]]);
  }}

  function selectedPageIndex() {{
    if (!selectedSentence) return -1;
    for (var pageIndex = 0; pageIndex < pages.length; pageIndex += 1) {{
      for (var itemIndex = 0; itemIndex < pages[pageIndex].length; itemIndex += 1) {{
        if (pages[pageIndex][itemIndex].getAttribute('data-sentence-index') === selectedSentence) {{
          return pageIndex;
        }}
      }}
    }}
    return -1;
  }}

  function savePosition() {{
    var first = '';
    if (pages[currentPage] && pages[currentPage][0]) {{
      first = pages[currentPage][0].getAttribute('data-sentence-index') || '';
    }}
    var image = new Image();
    image.src = '/reader-lite/position?book_id=' + encodeURIComponent(bookId)
      + '&chapter=' + encodeURIComponent(String(chapterIndex))
      + '&page=' + encodeURIComponent(String(currentPage))
      + '&total_pages=' + encodeURIComponent(String(Math.max(1, pages.length)))
      + '&first_sentence=' + encodeURIComponent(first)
      + '&_=' + String(new Date().getTime());
  }}

  function setQueryParam(url, key, value) {{
    var parts = String(url || '').split('#');
    var hash = parts.length > 1 ? '#' + parts.slice(1).join('#') : '';
    var base = parts[0];
    var pair = encodeURIComponent(key) + '=' + encodeURIComponent(String(value));
    if (base.indexOf('?') < 0) return base + '?' + pair + hash;
    var queryParts = base.split('?');
    var query = queryParts[1] ? queryParts[1].split('&') : [];
    var found = false;
    for (var i = 0; i < query.length; i += 1) {{
      if (decodeURIComponent(query[i].split('=')[0] || '') === key) {{
        query[i] = pair;
        found = true;
      }}
    }}
    if (!found) query.push(pair);
    return queryParts[0] + '?' + query.join('&') + hash;
  }}

  function syncVisiblePageForms() {{
    if (!pages[currentPage]) return;
    for (var j = 0; j < pages[currentPage].length; j += 1) {{
      var unit = pages[currentPage][j];
      var links = unit.getElementsByTagName('a');
      for (var linkIndex = 0; linkIndex < links.length; linkIndex += 1) {{
        if (links[linkIndex].className.indexOf('sentence-link') >= 0) {{
          links[linkIndex].href = setQueryParam(links[linkIndex].href, 'page', currentPage);
        }}
      }}
      var inputs = unit.getElementsByTagName('input');
      for (var inputIndex = 0; inputIndex < inputs.length; inputIndex += 1) {{
        if (inputs[inputIndex].name === 'page') inputs[inputIndex].value = String(currentPage);
      }}
    }}
  }}

  function showPage(pageIndex, shouldSave) {{
    if (!pages.length) return;
    currentPage = Math.max(0, Math.min(pageIndex, pages.length - 1));
    for (var i = 0; i < units.length; i += 1) units[i].style.display = 'none';
    for (var j = 0; j < pages[currentPage].length; j += 1) pages[currentPage][j].style.display = 'block';
    syncVisiblePageForms();
    if (previous) previous.href = currentPage > 0 ? '#' : previousHref;
    if (next) next.href = currentPage + 1 < pages.length ? '#' : nextHref;
    window.scrollTo(0, 0);
    if (shouldSave !== false) savePosition();
  }}

  function nextPage() {{
    if (currentPage + 1 < pages.length) {{
      showPage(currentPage + 1, true);
      return false;
    }}
    return true;
  }}

  function previousPage() {{
    if (currentPage > 0) {{
      showPage(currentPage - 1, true);
      return false;
    }}
    return true;
  }}

  if (previous) previous.onclick = function() {{ return previousPage(); }};
  if (next) next.onclick = function() {{ return nextPage(); }};

  document.addEventListener('touchstart', function(event){{
    if (!event.touches || !event.touches.length) return;
    startX = event.touches[0].clientX;
    startY = event.touches[0].clientY;
  }}, false);
  document.addEventListener('touchend', function(event){{
    if (!event.changedTouches || !event.changedTouches.length) return;
    var dx = event.changedTouches[0].clientX - startX;
    var dy = event.changedTouches[0].clientY - startY;
    if (Math.abs(dx) < 54 || Math.abs(dx) < Math.abs(dy) * 1.4) return;
    if (dx > 0 && previous && previousPage()) window.location.href = previous.href;
    if (dx < 0 && next && nextPage()) window.location.href = next.href;
  }}, false);

  buildPages();
  var selectedPage = selectedPageIndex();
  showPage(selectedPage >= 0 ? selectedPage : initialPage, true);
  window.onresize = function() {{
    var first = '';
    if (pages[currentPage] && pages[currentPage][0]) first = pages[currentPage][0].getAttribute('data-sentence-index') || '';
    buildPages();
    var target = 0;
    for (var pageIndex = 0; pageIndex < pages.length; pageIndex += 1) {{
      for (var itemIndex = 0; itemIndex < pages[pageIndex].length; itemIndex += 1) {{
        if (pages[pageIndex][itemIndex].getAttribute('data-sentence-index') === first) target = pageIndex;
      }}
    }}
    showPage(target, true);
  }};
}}());
</script>
</body>
</html>"""


def reader_lite_toc_html(request: FastAPIRequest, *, book_id: str) -> str:
    manifest = lite_reading_order(book_id)
    book = manifest["book"]
    reading_order = manifest["reading_order"]
    rows: list[str] = []
    for entry in reading_order:
        index = int(entry.get("chapter_index") or 0)
        title = str(entry.get("display_title") or "").strip() or f"第 {index + 1} 节"
        level = max(0, min(int(entry.get("level") or 0), 4))
        front = bool(entry.get("is_probable_front_matter"))
        class_name = "front-matter" if front else "readable"
        note = "<small>封面/版权/目录入口</small>" if front else ""
        rows.append(
            f"""<li class="{class_name}" style="padding-left:{level * 14}px">
  <a href="/reader-lite?{lite_query(book_id=book_id, chapter=index, page=0, auto=0)}">{html_escape(title)}</a>{note}
</li>"""
        )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Click Lite 目录</title>
  <style>
    body{{margin:0;background:#080806;color:#f6f0e8;font-family:Arial,"Microsoft YaHei",sans-serif}}
    main{{max-width:760px;margin:0 auto;padding:14px 12px 36px}}
    a{{color:#f0d36b;text-decoration:none}}
    h1{{font-size:24px;margin:10px 0 14px}}
    ol{{margin:0;padding:0;list-style:none}}
    li{{border-bottom:1px solid #2b261d;padding:13px 0;font-size:19px;line-height:1.35}}
    li a{{display:block;color:#f6f0e8}}
    small{{display:block;color:#8f887d;font-size:13px;margin-top:4px}}
    .front-matter a{{color:#8f887d}}
  </style>
</head>
<body>
<main>
  <a href="/library-lite">返回书库</a> · <a href="/reader-lite?{lite_query(book_id=book_id, auto=1)}">开始阅读</a>
  <h1>{html_escape(str(book.get('title') or '未命名书籍'))} 目录</h1>
  <ol data-lite-manifest="readingOrder spine toc">{''.join(rows)}</ol>
</main>
</body>
</html>"""


def library_page_html_v2() -> str:
    return r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>Sentence Reader Library</title>
  <script>
    (function(){
      var forcedModern = /(?:^|[?&])ui=modern(?:&|$)/.test(window.location.search || '');
      var ok = !!(window.Promise && window.fetch && document.querySelector && window.addEventListener);
      if (!forcedModern && !ok) window.location.replace('/library-lite?reason=capability');
    }());
  </script>
  <style>
    :root {
      color-scheme: dark;
      --bg:#000; --surface:#1c1c1e; --panel:#171719; --panel-2:#2c2c2e;
      --line:rgba(255,255,255,.1); --text:#f5f5f7; --muted:#98989d; --soft:#d1d1d6;
      --accent:#0a84ff; --jade:#64d2ff; --cyan:#64d2ff; --coral:#ff9f0a; --green:#30d158; --danger:#ff453a;
      --shadow:0 16px 42px rgba(0,0,0,.34);
    }
    * { box-sizing:border-box; }
    html, body { margin:0; min-height:100%; background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC",system-ui,sans-serif; }
    body { overflow-x:hidden; }
    button, input, select { font:inherit; }
    button { border:0; border-radius:9px; background:var(--panel-2); color:var(--text); padding:8px 12px; cursor:pointer; min-height:36px; }
    button:hover { background:#3a3a3c; }
    button.primary { background:var(--accent); color:white; font-weight:700; }
    button.ghost { background:transparent; color:var(--soft); }
    button.subtle { background:rgba(255,255,255,.065); border:1px solid var(--line); color:var(--soft); }
    button.danger { background:rgba(233,120,107,.12); color:#ffb5aa; border:1px solid rgba(233,120,107,.35); }
    button:disabled { opacity:.46; cursor:default; }
    input, select { width:100%; border:1px solid var(--line); background:rgba(255,255,255,.08); color:var(--text); border-radius:10px; padding:9px 12px; outline:none; }
    input:focus, select:focus { border-color:rgba(10,132,255,.7); box-shadow:0 0 0 3px rgba(10,132,255,.16); }
    .app-shell { min-height:100vh; display:grid; grid-template-columns:248px minmax(0,1fr); }
    .sidebar { border-right:1px solid var(--line); background:rgba(28,28,30,.94); padding:24px 13px; position:sticky; top:0; height:100vh; backdrop-filter:blur(28px) saturate(1.25); }
    .brand { margin:4px 10px 24px; }
    .brand strong { display:block; font-size:19px; line-height:1.2; letter-spacing:-.2px; }
    .brand span { display:block; color:var(--muted); font-size:12px; margin-top:5px; }
    .nav { display:grid; gap:4px; }
    .nav button { width:100%; display:grid; grid-template-columns:22px minmax(0,1fr) auto; gap:10px; align-items:center; background:transparent; color:var(--soft); text-align:left; font-weight:600; }
    .nav button.active { background:rgba(10,132,255,.24); color:white; }
    .nav-symbol { width:18px; height:18px; display:grid; place-items:center; color:inherit; }
    .nav-symbol svg { width:18px; height:18px; fill:none; stroke:currentColor; stroke-width:1.8; stroke-linecap:round; stroke-linejoin:round; }
    .nav-count { color:var(--muted); font-size:12px; font-variant-numeric:tabular-nums; }
    .nav button.active .nav-count { color:rgba(255,255,255,.72); }
    .side-action { margin-top:18px; display:grid; gap:8px; }
    .main { min-width:0; padding:0 32px 48px; }
    .topbar { position:sticky; top:0; z-index:20; display:grid; grid-template-columns:minmax(260px,560px) auto auto; justify-content:end; gap:9px; align-items:center; margin:0 -10px 8px; padding:16px 10px 12px; background:rgba(0,0,0,.78); backdrop-filter:blur(24px) saturate(1.3); }
    .topbar input { border-radius:10px; background:rgba(118,118,128,.22); }
    .status-pill { display:inline-flex; align-items:center; gap:7px; color:var(--muted); background:transparent; border:0; border-radius:999px; padding:8px 5px; font-size:12px; }
    .status-dot { width:7px; height:7px; border-radius:50%; background:var(--green); }
    .view { display:none; }
    .view.active { display:block; }
    .section-head { display:flex; align-items:end; justify-content:space-between; gap:12px; margin:22px 0 14px; }
    .section-head h1, .section-head h2 { margin:0; letter-spacing:0; }
    .section-head h1 { font-size:28px; letter-spacing:-.5px; }
    .section-head h2 { font-size:18px; }
    .section-head p { margin:5px 0 0; color:var(--muted); font-size:13px; }
    .continue-hero { min-height:330px; border:1px solid rgba(228,180,83,.22); border-radius:8px; background:radial-gradient(circle at 18% 15%,rgba(228,180,83,.16),transparent 28%),radial-gradient(circle at 82% 28%,rgba(125,185,196,.12),transparent 30%),linear-gradient(135deg,#171a10,#0d100b 62%,#050604); display:grid; grid-template-columns:minmax(180px,230px) minmax(0,1fr); gap:28px; padding:24px; box-shadow:var(--shadow); overflow:hidden; }
    .hero-copy { min-width:0; display:flex; flex-direction:column; justify-content:center; max-width:780px; }
    .eyebrow { color:var(--accent); font-size:12px; letter-spacing:1px; text-transform:uppercase; font-weight:900; }
    .hero-title { font-size:38px; line-height:1.13; margin:10px 0 8px; word-break:break-word; max-width:760px; display:-webkit-box; -webkit-box-orient:vertical; -webkit-line-clamp:3; overflow:hidden; }
    .hero-meta { color:var(--muted); font-size:14px; }
    .hero-actions { display:flex; flex-wrap:wrap; gap:10px; margin-top:18px; }
    .cover-frame { position:relative; width:100%; aspect-ratio:.72; border-radius:12px; overflow:hidden; background:#1c1c1e; border:1px solid rgba(255,255,255,.08); box-shadow:0 12px 28px rgba(0,0,0,.28); }
    .cover-frame[data-open-book] { cursor:pointer; }
    .cover-frame[data-open-book]:focus { outline:2px solid rgba(228,180,83,.65); outline-offset:3px; }
    .cover-frame img { width:100%; height:100%; object-fit:cover; display:block; }
    .cover-frame.hero-cover { align-self:center; min-height:270px; }
    .cover-frame.hero-cover::after { content:""; position:absolute; inset:0; background:linear-gradient(180deg,rgba(0,0,0,.02) 25%,rgba(0,0,0,.42) 100%); pointer-events:none; }
    .cover-frame.small { width:92px; flex:0 0 92px; }
    .cover-phrases { position:absolute; z-index:2; left:14px; right:14px; bottom:14px; display:grid; gap:7px; }
    .cover-phrases span { display:block; border:1px solid rgba(255,255,255,.16); background:rgba(7,8,6,.68); color:#f9f2dc; border-radius:999px; padding:7px 10px; font-size:13px; line-height:1.1; font-weight:900; text-align:center; backdrop-filter:blur(8px); }
    .cover-phrases span:nth-child(2) { color:#cceee0; }
    .cover-phrases span:nth-child(3) { color:#f6d28a; }
    .progress { height:3px; background:rgba(255,255,255,.14); border-radius:999px; overflow:hidden; margin-top:9px; }
    .progress i { display:block; height:100%; background:var(--accent); width:0; }
    .rail { display:grid; grid-auto-flow:column; grid-auto-columns:minmax(150px, 190px); gap:12px; overflow:auto; padding-bottom:4px; }
    .book-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(142px,190px)); gap:28px 22px; align-items:start; }
    .book-card { position:relative; min-width:0; text-align:left; background:transparent; border:0; border-radius:14px; padding:0; transition:transform .14s ease, opacity .14s ease; }
    .book-card:hover, .book-card:focus { transform:translateY(-2px); outline:none; }
    .book-card:focus .cover-frame { box-shadow:0 0 0 3px rgba(10,132,255,.55),0 12px 28px rgba(0,0,0,.28); }
    .book-card.selected .cover-frame { box-shadow:0 0 0 3px var(--accent),0 12px 28px rgba(0,0,0,.28); }
    .book-card.opening { opacity:.62; pointer-events:none; }
    .book-card .cover-frame { margin-bottom:10px; }
    .book-title { font-weight:650; font-size:14px; line-height:1.34; min-height:38px; word-break:break-word; display:-webkit-box; -webkit-box-orient:vertical; -webkit-line-clamp:2; overflow:hidden; }
    .book-meta { color:var(--muted); font-size:12px; margin-top:3px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .book-row { display:flex; align-items:center; gap:8px; margin-top:8px; flex-wrap:wrap; }
    .badge { font-size:11px; color:var(--soft); border:1px solid var(--line); border-radius:999px; padding:3px 7px; background:#11120f; }
    .badge.state { color:#11120f; background:var(--jade); border-color:var(--jade); font-weight:800; }
    .badge.red { color:#ffc1b7; border-color:rgba(217,133,106,.4); }
    .card-check { display:none; position:absolute; z-index:4; left:12px; top:12px; width:18px; height:18px; accent-color:var(--accent); }
    .book-card.selectable .card-check { display:block; }
    .card-secondary { position:absolute; z-index:4; right:8px; top:8px; display:flex; opacity:0; transition:opacity .12s ease; }
    .book-card:hover .card-secondary, .book-card:focus-within .card-secondary { opacity:1; }
    .card-action { width:30px; height:30px; min-height:30px; padding:0; border-radius:50%; background:rgba(20,20,22,.74); border:1px solid rgba(255,255,255,.16); color:white; font-size:13px; backdrop-filter:blur(12px); }
    .card-action:hover { color:white; border-color:rgba(255,255,255,.3); background:rgba(50,50,52,.9); }
    .shelf-header { display:flex; align-items:center; justify-content:space-between; gap:18px; padding-top:10px; }
    .shelf-header h1 { margin:0; font-size:31px; letter-spacing:-.7px; }
    .shelf-header p { margin:5px 0 0; color:var(--muted); font-size:13px; }
    .scope-tabs { width:min(380px,100%); display:grid; grid-template-columns:repeat(3,1fr); gap:2px; padding:3px; margin:20px 0 24px; border-radius:9px; background:rgba(118,118,128,.24); }
    .scope-tabs button { min-height:29px; padding:4px 12px; border-radius:7px; background:transparent; color:var(--soft); font-size:13px; }
    .scope-tabs button.active { background:#636366; color:white; box-shadow:0 1px 4px rgba(0,0,0,.35); }
    .folder-breadcrumb { min-height:0; margin:0 0 14px; }
    .folder-breadcrumb:empty { display:none; }
    .folder-back { display:inline-flex; align-items:center; gap:7px; min-height:32px; padding:4px 8px; background:transparent; color:var(--accent); }
    .shelf-section { margin:0 0 28px; }
    .shelf-section.hidden { display:none; }
    .shelf-section-title { display:flex; align-items:center; justify-content:space-between; margin:0 0 13px; }
    .shelf-section-title h2 { margin:0; font-size:18px; letter-spacing:-.2px; }
    .shelf-section-title span { color:var(--muted); font-size:12px; }
    .folder-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(178px,214px)); gap:24px 22px; }
    .folder-card { min-width:0; padding:0; background:transparent; text-align:left; color:var(--text); }
    .folder-card:hover { background:transparent; }
    .folder-preview { aspect-ratio:1.02; display:grid; grid-template-columns:1fr 1fr; gap:8px; padding:12px; border-radius:18px; background:rgba(10,132,255,.12); border:1px solid rgba(10,132,255,.16); transition:background .14s ease, transform .14s ease; }
    .folder-card:hover .folder-preview, .folder-card:focus .folder-preview { background:rgba(10,132,255,.2); transform:translateY(-2px); }
    .folder-preview .cover-frame { aspect-ratio:.78; border-radius:7px; box-shadow:0 5px 12px rgba(0,0,0,.25); }
    .folder-placeholder { border-radius:7px; background:rgba(255,255,255,.06); }
    .folder-label { display:flex; align-items:center; gap:7px; margin-top:10px; font-weight:650; font-size:14px; line-height:1.3; }
    .folder-label svg { width:16px; height:16px; fill:var(--accent); }
    .folder-count { margin:3px 0 0 23px; color:var(--muted); font-size:12px; }
    .home-empty { grid-column:1 / -1; min-height:240px; display:grid; place-items:center; text-align:center; }
    .toolbar { display:grid; grid-template-columns:minmax(180px,1fr) 140px 140px auto auto; gap:10px; margin-bottom:14px; align-items:center; }
    .batchbar { display:none; align-items:center; justify-content:space-between; gap:10px; border:1px solid rgba(215,168,79,.3); background:rgba(215,168,79,.09); border-radius:10px; padding:10px 12px; margin-bottom:12px; }
    .batchbar.show { display:flex; }
    .asset-list { display:grid; gap:10px; }
    .asset-card { border:1px solid var(--line); background:var(--panel); border-radius:10px; padding:13px; display:grid; grid-template-columns:minmax(0,1fr) auto; gap:12px; align-items:center; }
    .asset-card strong { display:block; margin-bottom:5px; }
    .asset-card p { margin:0; color:var(--soft); line-height:1.55; }
    .group-list { display:grid; gap:18px; }
    .group-panel { border:1px solid var(--line); background:rgba(255,255,255,.025); border-radius:8px; padding:14px; }
    .group-panel h2 { margin:0 0 12px; font-size:18px; display:flex; justify-content:space-between; gap:10px; }
    .favorite-mark { position:absolute; z-index:2; left:12px; top:12px; min-width:24px; height:24px; border-radius:999px; display:grid; place-items:center; color:#1a1408; background:var(--accent); font-weight:900; box-shadow:0 8px 24px rgba(0,0,0,.32); }
    .book-card.selectable .favorite-mark { left:38px; }
    .org-panel { border:1px solid rgba(228,180,83,.22); background:rgba(228,180,83,.055); border-radius:8px; padding:12px; margin-top:12px; display:grid; gap:9px; }
    .org-row { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:8px; align-items:center; }
    .org-tags { display:flex; flex-wrap:wrap; gap:6px; }
    .empty, .error-box { border:1px dashed #545541; background:#12130e; border-radius:12px; padding:22px; color:var(--soft); }
    .drawer-backdrop { position:fixed; inset:0; background:rgba(0,0,0,.45); opacity:0; pointer-events:none; transition:opacity .15s ease; z-index:30; }
    .drawer { position:fixed; right:0; top:0; bottom:0; width:min(430px, 92vw); background:#10110c; border-left:1px solid var(--line); transform:translateX(104%); transition:transform .18s ease; z-index:31; padding:18px; overflow:auto; box-shadow:var(--shadow); }
    .drawer.open { transform:translateX(0); }
    .drawer-backdrop.show { opacity:1; pointer-events:auto; }
    .drawer h2 { margin:10px 0 6px; }
    .drawer-actions { display:grid; grid-template-columns:1fr 1fr; gap:9px; margin:14px 0; }
    .drawer-actions .primary { grid-column:1 / -1; }
    details { border:1px solid var(--line); border-radius:8px; padding:10px; color:var(--muted); }
    details summary { color:var(--soft); cursor:pointer; }
    .modal-backdrop { position:fixed; inset:0; z-index:45; display:none; place-items:center; padding:18px; background:rgba(0,0,0,.58); }
    .modal-backdrop.show { display:grid; }
    .modal { width:min(520px,100%); border:1px solid rgba(233,120,107,.32); border-radius:10px; background:#11130e; box-shadow:var(--shadow); padding:18px; }
    .modal h2 { margin:0 0 8px; font-size:20px; }
    .modal p { margin:0 0 12px; color:var(--soft); line-height:1.55; }
    .modal ul { margin:0 0 16px; padding-left:18px; color:var(--muted); line-height:1.65; }
    .modal-actions { display:flex; gap:10px; justify-content:flex-end; flex-wrap:wrap; }
    .toast { position:fixed; left:50%; bottom:20px; transform:translateX(-50%); background:#1d1e17; border:1px solid #4d4c36; padding:10px 14px; border-radius:9px; opacity:0; pointer-events:none; transition:opacity .16s ease; z-index:50; display:flex; align-items:center; gap:12px; max-width:min(92vw,620px); }
    .toast.show { opacity:1; pointer-events:auto; }
    .toast button { min-height:30px; padding:5px 9px; }
    @media (max-width: 980px) {
      .app-shell { grid-template-columns:210px minmax(0,1fr); }
      .topbar, .toolbar { grid-template-columns:1fr 1fr; }
      .main { padding-left:22px; padding-right:22px; }
    }
    @media (max-width: 640px) {
      .main { padding:14px 12px 24px; }
      .topbar, .toolbar { grid-template-columns:1fr; }
      .continue-hero { grid-template-columns:1fr; padding:18px; gap:18px; }
      .continue-hero .cover-frame { max-width:190px; min-height:252px; }
      .hero-title { font-size:28px; }
      .app-shell { grid-template-columns:1fr; }
      .sidebar { position:static; height:auto; border-right:0; border-bottom:1px solid var(--line); }
      .nav { grid-template-columns:repeat(5,minmax(0,1fr)); }
      .nav button { grid-template-columns:1fr; justify-items:center; gap:4px; font-size:11px; }
      .nav-count { display:none; }
      .book-grid { grid-template-columns:repeat(2,minmax(0,1fr)); }
    }
  </style>
</head>
<body>
  <div class="app-shell" data-library-v2="true" data-native-reader-contract="sentence-reader://open-native">
    <aside class="sidebar">
      <div class="brand"><strong>Click</strong><span>本地精读</span></div>
      <nav class="nav" id="nav"></nav>
    </aside>
    <main class="main">
      <div class="topbar">
        <input id="search" placeholder="搜索书名或作者">
        <span class="status-pill"><i class="status-dot"></i><span id="serviceStatus">正在连接</span></span>
        <button class="subtle" id="refresh" aria-label="刷新" title="刷新">↻</button>
      </div>

      <section id="homeView" class="view active" data-product-home="true">
        <header class="shelf-header">
          <div><h1 id="shelfTitle">阅读</h1><p id="shelfSubtitle">打开封面继续阅读，整理功能收在需要时出现。</p></div>
          <div>
            <button class="subtle" id="homeManage">整理</button>
            <button class="primary" id="topImport" aria-label="导入书籍">＋</button>
          </div>
        </header>
        <div class="scope-tabs" role="tablist" aria-label="书架范围">
          <button class="active" data-library-scope="all" role="tab">全部</button>
          <button data-library-scope="recent" role="tab">最近</button>
          <button data-library-scope="favorites" role="tab">收藏</button>
        </div>
        <div id="folderBreadcrumb" class="folder-breadcrumb"></div>
        <section id="folderSection" class="shelf-section">
          <div class="shelf-section-title"><h2>文件夹</h2><span id="folderCount"></span></div>
          <div id="homeFolderGrid" class="folder-grid"></div>
        </section>
        <section id="homeBookSection" class="shelf-section">
          <div class="shelf-section-title"><h2 id="homeBookTitle">未整理</h2><span id="homeBookCount"></span></div>
          <div id="homeBookGrid" class="book-grid"></div>
        </section>
      </section>

      <section id="libraryView" class="view">
        <div class="section-head"><div><h1>书库</h1><p>封面墙优先，管理动作收在更多里。</p></div><small id="bookCount"></small></div>
        <div class="toolbar">
          <input id="librarySearch" placeholder="在书库中搜索">
          <select id="sort">
            <option value="recent">最近阅读</option>
            <option value="title">书名</option>
            <option value="notes">笔记最多</option>
            <option value="red">红标最多</option>
          </select>
          <select id="stateFilter">
            <option value="all">全部状态</option>
            <option value="在读">在读</option>
            <option value="未开始">未开始</option>
            <option value="已读">已读</option>
            <option value="搁置">搁置</option>
          </select>
          <button class="subtle" id="manageToggle">管理</button>
        </div>
        <div id="batchbar" class="batchbar"><span id="batchCount">管理模式 · 已选择 0 本</span><span><button class="subtle" id="selectAllCurrent">全选当前</button> <button class="subtle" id="batchClear">清空选择</button> <button class="subtle" id="batchExit">退出管理</button> <button class="subtle" id="batchFavorite">批量收藏</button> <button class="subtle" id="batchOrganize">批量分类</button> <button class="subtle" id="batchExport">批量导出</button> <button class="danger" id="batchHide">移出书库</button></span></div>
        <section id="bookGrid" class="book-grid"></section>
      </section>

      <section id="favoritesView" class="view">
        <div class="section-head"><div><h1>收藏</h1><p>这里放你主动判定值得反复读的书。</p></div><small id="favoriteCount"></small></div>
        <section id="favoriteGrid" class="book-grid"></section>
      </section>

      <section id="authorsView" class="view">
        <div class="section-head"><div><h1>作者</h1><p>按作者聚合，适合追踪一个人的思想系统。</p></div><small id="authorCount"></small></div>
        <section id="authorGroups" class="group-list"></section>
      </section>

      <section id="categoriesView" class="view">
        <div class="section-head"><div><h1>分类</h1><p>按你自己的书架逻辑组织，不被文件名牵着走。</p></div><small id="categoryCount"></small></div>
        <section id="categoryGroups" class="group-list"></section>
      </section>

      <section id="notesView" class="view">
        <div class="section-head"><div><h1>笔记</h1><p>这里是你的读书判断，不再只是筛选书籍。</p></div></div>
        <section id="notesList" class="asset-list"></section>
      </section>

      <section id="redView" class="view">
        <div class="section-head"><div><h1>红标</h1><p>重要句子的摘录库。</p></div></div>
        <section id="redList" class="asset-list"></section>
      </section>

      <section id="settingsView" class="view">
        <div class="section-head"><div><h1>设置</h1><p>恢复和连接信息。</p></div></div>
        <section id="settingsPanel" class="asset-list"></section>
      </section>
    </main>
  </div>
  <div id="drawerBackdrop" class="drawer-backdrop"></div>
  <aside id="drawer" class="drawer" aria-hidden="true"></aside>
  <div id="removeModal" class="modal-backdrop" aria-hidden="true"></div>
  <div id="orgModal" class="modal-backdrop" aria-hidden="true"></div>
  <input id="fileInput" type="file" accept=".epub,.pdf,application/epub+zip,application/pdf" hidden>
  <div id="toast" class="toast"></div>
  <script>
    const state = { dashboard:null, books:[], hiddenBooks:[], assets:[], view:'home', query:'', sort:'recent', stateFilter:'all', libraryScope:'all', folderPath:'', manageMode:false, selectedBook:null, activeBookId:null, selectedIds:new Set(), pendingRemove:null, lastRemoved:null, lastImport:null, error:null, openingBookId:null, openingRequestId:null };
    const $ = (id) => document.getElementById(id);
    const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const isMacAppSurface = () => new URLSearchParams(window.location.search).get('surface') === 'mac-app';
    const byId = (id) => state.books.find((book) => book.id === id) || state.hiddenBooks.find((book) => book.id === id);
    function toast(text, actionLabel = '', action = null) {
      const node = $('toast');
      node.innerHTML = `<span>${esc(text)}</span>${actionLabel ? `<button class="subtle" id="toastAction">${esc(actionLabel)}</button>` : ''}`;
      node.classList.add('show');
      if (actionLabel && action) $('toastAction').onclick = action;
      clearTimeout(state.toastTimer);
      state.toastTimer = setTimeout(() => node.classList.remove('show'), actionLabel ? 6200 : 2600);
    }
    async function api(url, options) {
      const response = await fetch(url, options);
      if (!response.ok) throw new Error(await response.text());
      return response.json();
    }
    function navItems() {
      const summary = state.dashboard?.summary || {};
      return [
        ['home','阅读', summary.book_count || 0],
        ['notes','笔记', summary.note_count || 0],
        ['red','红标', summary.red_highlight_count || 0],
        ['vocab','单词',''],
        ['settings','设置','']
      ];
    }
    function navSymbol(id) {
      const icons = {
        home:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5.5c3.2-.8 5.9-.2 8 1.6v12c-2.1-1.8-4.8-2.4-8-1.6zM20 5.5c-3.2-.8-5.9-.2-8 1.6v12c2.1-1.8 4.8-2.4 8-1.6z"/></svg>',
        notes:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 4h14v16H5zM8 8h8M8 12h8M8 16h5"/></svg>',
        red:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 4h12v16l-6-3-6 3z"/></svg>',
        vocab:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 6h14M8 6c.6 5 3.1 8.2 7.5 10M16 6c-.7 4.1-3 7.3-7 9.5M6 19h12"/></svg>',
        settings:'<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6 7 7M17 17l1.4 1.4M18.4 5.6 17 7M7 17l-1.4 1.4"/></svg>'
      };
      return icons[id] || '';
    }
    function vocabURL(bookID = null) {
      const id = bookID || state.activeBookId || state.dashboard?.current_book?.id || state.books[0]?.id || '';
      return id ? `/vocab?book_id=${encodeURIComponent(id)}` : '/vocab';
    }
    function setView(view) {
      if (view === 'vocab') {
        window.location.href = vocabURL();
        return;
      }
      state.view = view; closeDrawer(); render();
    }
    function renderNav() {
      $('nav').innerHTML = navItems().map(([id,title,count]) => {
        const active = state.view === id || (id === 'home' && ['library','favorites','authors','categories'].includes(state.view));
        return `<button class="${active ? 'active' : ''}" data-view="${id}"><span class="nav-symbol">${navSymbol(id)}</span><span>${title}</span><span class="nav-count">${count}</span></button>`;
      }).join('');
      document.querySelectorAll('[data-view], [data-view-jump]').forEach((button) => button.onclick = () => setView(button.dataset.view || button.dataset.viewJump));
    }
    function visible(view) { document.querySelectorAll('.view').forEach((node) => node.classList.toggle('active', node.id === `${view}View`)); }
    function bookAssetText(book) {
      return state.assets.filter((asset) => asset.book_id === book.id).map((asset) => [asset.source_text, asset.note_text, asset.preview, asset.chapter_title].join(' ')).join(' ');
    }
    function bookMatches(book) {
      const query = (state.query || '').trim().toLowerCase();
      if (state.stateFilter !== 'all' && book.reading_state !== state.stateFilter) return false;
      if (!query) return true;
      const org = book.organization || {};
      const haystack = [book.title, book.author, org.category, org.tags?.join(' '), book.file?.file_path, book.reading_state, bookAssetText(book)].join(' ').toLowerCase();
      return haystack.includes(query);
    }
    function sortedBooks(source = state.books) {
      const books = source.filter(bookMatches);
      books.sort((a,b) => {
        if (state.sort === 'title') return String(a.title || '').localeCompare(String(b.title || ''), 'zh-Hans-CN');
        if (state.sort === 'notes') return (b.counts?.notes || 0) - (a.counts?.notes || 0);
        if (state.sort === 'red') return (b.counts?.red_highlights || 0) - (a.counts?.red_highlights || 0);
        return String(b.recent_activity_at || '').localeCompare(String(a.recent_activity_at || ''));
      });
      return books;
    }
    function filteredGroups(groups, key) {
      return groups.map((group) => {
        const books = sortedBooks(group.books || []);
        return {title: group[key] || '未分类', count: books.length, books};
      }).filter((group) => group.count > 0);
    }
    function progressText(book) { return `${book.progress?.percent || 0}%`; }
    function chapterText(book) {
      const locator = book.progress?.chapter_locator || '';
      if (!locator) return '尚未开始';
      return locator.split('/').pop().replace(/\.(xhtml|html|htm)$/i, '') || locator;
    }
    function syncNativeBookOpening() {
      document.querySelectorAll('[data-open-book-card]').forEach((card) => {
        const opening = Boolean(state.openingBookId && card.dataset.book === state.openingBookId);
        card.classList.toggle('opening', opening);
        card.setAttribute('aria-busy', opening ? 'true' : 'false');
      });
    }
    function setNativeBookOpenState({bookId=null, requestId=null, phase='idle', message=''}) {
      if (phase === 'opening') {
        state.openingBookId = bookId;
        state.openingRequestId = requestId;
      } else if (!requestId || requestId === state.openingRequestId) {
        state.openingBookId = null;
        state.openingRequestId = null;
      }
      syncNativeBookOpening();
      if (message) toast(message);
    }
    function requestNativeBookOpen(book) {
      const handler = window.webkit?.messageHandlers?.sentenceReader;
      if (!handler?.postMessage) return false;
      if (state.openingBookId) {
        toast(state.openingBookId === book.id ? '正在打开这本书...' : '正在打开另一本书，请稍候');
        return true;
      }
      const requestId = `library-open-${Date.now()}-${Math.random().toString(36).slice(2)}`;
      setNativeBookOpenState({bookId:book.id, requestId, phase:'opening', message:'正在打开正文...'});
      try {
        handler.postMessage({type:'libraryOpenBook', book_id:book.id, request_id:requestId});
        return true;
      } catch (error) {
        setNativeBookOpenState({requestId, phase:'failed', message:'原生阅读器通信失败，正在使用兼容打开方式。'});
        return false;
      }
    }
    window.__clickNativeBookOpenState = (payload) => {
      if (!payload || (state.openingRequestId && payload.request_id && payload.request_id !== state.openingRequestId)) return;
      setNativeBookOpenState({
        bookId:payload.book_id || state.openingBookId,
        requestId:payload.request_id || state.openingRequestId,
        phase:payload.phase || 'idle',
        message:payload.message || '',
      });
    };
    function openBook(book) {
      if (isMacAppSurface() && book?.reader_capabilities?.mac_native && book.actions?.native_reader_url) {
        if (requestNativeBookOpen(book)) return;
        window.location.href = book.actions.native_reader_url;
        return;
      }
      if (!book?.status?.lan_available) { toast('这本书当前只支持 Click Mac 原生阅读。'); return; }
      if (book.actions?.continue_reading_url) window.location.href = book.actions.continue_reading_url;
    }
    function cover(book, cls='', showPhrases=false, openable=false) {
      const phrases = showPhrases ? '<div class="cover-phrases"><span>逐句读懂</span><span>语境查词</span><span>复习沉淀</span></div>' : '';
      const openAttrs = openable
        ? ` data-open-book="${esc(book.id)}" role="button" tabindex="0" aria-label="打开 ${esc(book.title || book.id)}"`
        : '';
      return `<div class="cover-frame ${cls}"${openAttrs}><img src="${esc(book.cover?.url || '')}" alt="${esc(book.title || '书籍封面')}" loading="lazy">${phrases}</div>`;
    }
    function bookCard(book) {
      const checked = state.selectedIds.has(book.id) ? 'checked' : '';
      const org = book.organization || {};
      const favorite = org.favorite ? '<div class="favorite-mark" title="已收藏">★</div>' : '';
      const modeClass = state.manageMode ? 'selectable' : '';
      const selectedClass = state.selectedIds.has(book.id) ? 'selected' : '';
      return `<article class="book-card ${modeClass} ${selectedClass}" tabindex="0" data-book="${esc(book.id)}" data-open-book-card="true">
        <input class="card-check" type="checkbox" data-select="${esc(book.id)}" ${checked} aria-label="选择书籍">
        ${favorite}
        ${cover(book)}
        <div class="book-title">${esc(book.title || book.id)}</div>
        <div class="book-meta">${esc(book.author || '未知作者')}</div>
        <div class="progress"><i style="width:${book.progress?.percent || 0}%"></i></div>
        <div class="book-meta">${progressText(book)}</div>
        <div class="card-secondary" data-card-secondary="true">
          <button class="card-action" data-details="${esc(book.id)}" aria-label="更多">•••</button>
        </div>
      </article>`;
    }
    function syncBookSelection(id) {
      document.querySelectorAll(`[data-book="${CSS.escape(id)}"]`).forEach((card) => {
        const selected = state.selectedIds.has(id);
        card.classList.toggle('selected', selected);
        const box = card.querySelector('[data-select]');
        if (box) box.checked = selected;
      });
      renderBatchbar();
    }
    function toggleBookSelection(id) {
      if (!id) return;
      if (state.selectedIds.has(id)) state.selectedIds.delete(id); else state.selectedIds.add(id);
      syncBookSelection(id);
    }
    function bindBookCards() {
      document.querySelectorAll('[data-open-book-card]').forEach((card) => {
        card.onclick = (event) => {
          if (event.target.closest('button') || event.target.closest('input')) return;
          const book = byId(card.dataset.book);
          state.activeBookId = book?.id || null;
          if (state.manageMode) { toggleBookSelection(book?.id); return; }
          openBook(book);
        };
        card.onfocus = () => { state.activeBookId = card.dataset.book; };
      });
      document.querySelectorAll('[data-details]').forEach((button) => button.onclick = () => openDrawer(byId(button.dataset.details)));
      document.querySelectorAll('[data-select]').forEach((box) => box.onchange = () => {
        if (box.checked) state.selectedIds.add(box.dataset.select); else state.selectedIds.delete(box.dataset.select);
        syncBookSelection(box.dataset.select);
      });
      syncNativeBookOpening();
    }
    function bindOpenBookTargets(root = document) {
      root.querySelectorAll('[data-open-book]').forEach((target) => {
        target.onclick = (event) => {
          event.stopPropagation();
          openBook(byId(target.dataset.openBook));
        };
        target.onkeydown = (event) => {
          if (event.key !== 'Enter' && event.key !== ' ') return;
          event.preventDefault();
          event.stopPropagation();
          openBook(byId(target.dataset.openBook));
        };
      });
    }
    async function updateBookOrganization(book, payload, message = '已更新') {
      if (!book?.id) return;
      await api(`/api/library/books/${book.id}/organization`, {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
      toast(message);
      await loadDashboard();
      const updated = byId(book.id);
      if (state.selectedBook?.id === book.id && updated && $('drawer').classList.contains('open')) openDrawer(updated);
    }
    async function toggleFavorite(book) {
      if (!book) return;
      const next = !(book.organization?.favorite || false);
      await updateBookOrganization(book, {favorite: next}, next ? '已收藏' : '已取消收藏');
    }
    function renderBookGroup(containerId, groups, emptyText) {
      const container = $(containerId);
      if (!groups.length) {
        container.innerHTML = `<div class="empty">${esc(emptyText)}</div>`;
        return;
      }
      container.innerHTML = groups.map((group) => `<section class="group-panel"><h2><span>${esc(group.title)}</span><span>${esc(group.count)} 本</span></h2><div class="book-grid">${group.books.map(bookCard).join('')}</div></section>`).join('');
      bindBookCards();
    }
    function renderOrganizationViews() {
      const favoriteBooks = sortedBooks(state.dashboard?.favorite_books || []);
      $('favoriteCount').textContent = `${favoriteBooks.length} 本`;
      $('favoriteGrid').innerHTML = favoriteBooks.length ? favoriteBooks.map(bookCard).join('') : `<div class="empty"><h2>还没有收藏</h2><p>在书卡或详情里点“收藏”，把最值得反复读的书放到这里。</p></div>`;
      bindBookCards();

      const authorGroups = filteredGroups(state.dashboard?.author_groups || [], 'author');
      $('authorCount').textContent = `${authorGroups.length} 位作者`;
      renderBookGroup('authorGroups', authorGroups, '还没有作者分组。');

      const categoryGroups = filteredGroups(state.dashboard?.category_groups || [], 'category');
      $('categoryCount').textContent = `${categoryGroups.length} 个分类`;
      renderBookGroup('categoryGroups', categoryGroups, '还没有分类。');
    }
    function normalizedFolderPath(book) {
      const raw = String(book?.organization?.custom_category || book?.organization?.category || '').trim();
      if (!raw || raw === '未分类') return '';
      return raw.split(/\s*(?:\/|›|>)\s*/).filter(Boolean).join('/');
    }
    function booksInsideFolder(path) {
      if (!path) return state.books;
      return state.books.filter((book) => {
        const category = normalizedFolderPath(book);
        return category === path || category.startsWith(`${path}/`);
      });
    }
    function childFolders(path) {
      const prefix = path ? `${path}/` : '';
      const groups = new Map();
      state.books.forEach((book) => {
        const category = normalizedFolderPath(book);
        if (!category || (path && category !== path && !category.startsWith(prefix))) return;
        const rest = path ? category.slice(prefix.length) : category;
        if (!rest) return;
        const name = rest.split('/')[0];
        const fullPath = prefix + name;
        if (!groups.has(fullPath)) groups.set(fullPath, []);
        groups.get(fullPath).push(book);
      });
      return Array.from(groups, ([folderPath, books]) => ({folderPath, name:folderPath.split('/').pop(), books}))
        .sort((a,b) => a.name.localeCompare(b.name, 'zh-Hans-CN'));
    }
    function directBooks(path) {
      return state.books.filter((book) => normalizedFolderPath(book) === path);
    }
    function folderPreview(books) {
      const cells = Array.from({length:4}, (_, index) => {
        const book = books[index];
        return book ? cover(book) : '<span class="folder-placeholder" aria-hidden="true"></span>';
      });
      return cells.join('');
    }
    function folderCard(folder) {
      return `<button class="folder-card" data-folder-path="${esc(folder.folderPath)}" aria-label="打开文件夹 ${esc(folder.name)}">
        <span class="folder-preview">${folderPreview(folder.books.slice(0,4))}</span>
        <span class="folder-label"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6.5h7l2 2h9v10H3z"/></svg><span>${esc(folder.name)}</span></span>
        <span class="folder-count">${folder.books.length} 本</span>
      </button>`;
    }
    function renderHome() {
      const query = (state.query || '').trim();
      const allScope = state.libraryScope === 'all';
      const folderPath = allScope && !query ? state.folderPath : '';
      const folders = allScope && !query ? childFolders(folderPath) : [];
      let books;
      if (query) {
        books = sortedBooks(state.books);
      } else if (state.libraryScope === 'recent') {
        books = sortedBooks(state.dashboard?.recent_books || state.books).slice(0, 20);
      } else if (state.libraryScope === 'favorites') {
        books = sortedBooks(state.dashboard?.favorite_books || []);
      } else {
        books = sortedBooks(directBooks(folderPath));
      }

      const scopeTitles = {all:'全部', recent:'最近', favorites:'收藏'};
      $('shelfTitle').textContent = folderPath ? folderPath.split('/').pop() : '阅读';
      $('shelfSubtitle').textContent = query
        ? `搜索“${query}”`
        : folderPath
          ? `书架 › ${folderPath.replaceAll('/', ' › ')}`
          : '打开封面继续阅读；文件夹和书籍保持同一套层级。';
      document.querySelectorAll('[data-library-scope]').forEach((button) => {
        const active = button.dataset.libraryScope === state.libraryScope;
        button.classList.toggle('active', active);
        button.setAttribute('aria-selected', active ? 'true' : 'false');
      });

      $('folderBreadcrumb').innerHTML = folderPath
        ? `<button class="folder-back" id="folderBack" aria-label="返回上一级">‹ 返回上一级</button>`
        : '';
      $('folderSection').classList.toggle('hidden', folders.length === 0);
      $('folderCount').textContent = folders.length ? `${folders.length} 个` : '';
      $('homeFolderGrid').innerHTML = folders.map(folderCard).join('');

      const title = query ? '搜索结果' : state.libraryScope === 'all' ? (folderPath ? '直属书籍' : '未整理') : scopeTitles[state.libraryScope];
      $('homeBookTitle').textContent = title;
      $('homeBookCount').textContent = `${books.length} 本`;
      $('homeBookGrid').innerHTML = books.length
        ? books.map(bookCard).join('')
        : `<div class="home-empty"><div><h2>${query ? '没有找到书籍' : '这里还没有书'}</h2><p>${query ? '换一个书名或作者试试。' : '可以导入书籍，或在整理时把书移到这里。'}</p></div></div>`;

      document.querySelectorAll('[data-folder-path]').forEach((button) => button.onclick = () => {
        state.folderPath = button.dataset.folderPath || '';
        renderHome();
        window.scrollTo({top:0, behavior:'smooth'});
      });
      if ($('folderBack')) $('folderBack').onclick = () => {
        const parts = state.folderPath.split('/').filter(Boolean);
        parts.pop();
        state.folderPath = parts.join('/');
        renderHome();
      };
      bindBookCards();
    }
    function renderLibrary() {
      const books = sortedBooks();
      $('bookCount').textContent = `${books.length} 本`;
      $('bookGrid').innerHTML = books.length ? books.map(bookCard).join('') : `<div class="empty"><h2>没有匹配的书</h2><p>换一个搜索词，或者导入新的 EPUB / PDF。</p></div>`;
      bindBookCards();
      renderBatchbar();
    }
    function assetMatches(asset, kind) {
      if (kind && asset.kind !== kind) return false;
      const query = (state.query || '').trim().toLowerCase();
      if (!query) return true;
      return [asset.book_title, asset.book_author, asset.source_text, asset.note_text, asset.preview, asset.chapter_title].join(' ').toLowerCase().includes(query);
    }
    function assetCard(asset) {
      const label = asset.kind === 'note' ? '笔记' : '红标';
      return `<article class="asset-card" data-asset="${esc(asset.id)}"><div><strong>${esc(label)} · ${esc(asset.book_title || '')}</strong><p>${esc(asset.preview || asset.source_text || '')}</p><div class="book-meta">${esc(asset.chapter_title || asset.chapter_locator || '')}</div></div><button class="subtle" data-open-asset="${esc(asset.book_id)}">回到原文</button></article>`;
    }
    function groupedAssetList(assets) {
      const groups = new Map();
      assets.forEach((asset) => {
        const key = asset.book_id || asset.book_title || 'unknown';
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(asset);
      });
      return Array.from(groups.values()).map((items, index) => {
        const first = items[0] || {};
        const cards = items.map((asset) => {
          const label = asset.kind === 'note' ? '笔记' : '红标';
          const chapter = asset.chapter_title || asset.chapter_locator || label;
          return `<article class="asset-card" data-asset="${esc(asset.id)}"><div><strong>${esc(chapter)}</strong><p>${esc(asset.preview || asset.source_text || '')}</p></div><button class="subtle" data-open-asset="${esc(asset.book_id)}">回到原文</button></article>`;
        }).join('');
        return `<details ${index === 0 ? 'open' : ''}><summary>${esc(first.book_title || '未命名书籍')} · ${items.length} 条</summary><div class="asset-list" style="margin-top:10px">${cards}</div></details>`;
      }).join('');
    }
    function bindAssets() { document.querySelectorAll('[data-open-asset]').forEach((button) => button.onclick = () => openBook(byId(button.dataset.openAsset))); }
    function renderAssets() {
      const notes = state.assets.filter((asset) => assetMatches(asset, 'note'));
      const red = state.assets.filter((asset) => assetMatches(asset, 'red_highlight'));
      $('notesList').innerHTML = notes.length ? groupedAssetList(notes) : `<div class="empty"><h2>还没有笔记</h2><p>在正文里双击句子即可写备注。</p></div>`;
      $('redList').innerHTML = red.length ? groupedAssetList(red) : `<div class="empty"><h2>还没有红标</h2><p>在正文里双指点按或右键句子即可标红。</p></div>`;
      bindAssets();
    }
    function renderSettings() {
      const ok = state.dashboard?.ok;
      const importResult = state.lastImport ? `<article class="asset-card"><div><strong>最近导入成功</strong><p>${esc(state.lastImport.book?.title || '')}</p><div class="book-meta">已复制到内部书库，原文件可删除。</div></div><button class="subtle" data-open-import="${esc(state.lastImport.book?.id || '')}">打开</button></article>` : '';
      const hidden = state.hiddenBooks || [];
      const hiddenList = hidden.length ? hidden.map((book) => `<article class="asset-card"><div><strong>${esc(book.title || book.id)}</strong><p>${esc(book.author || '未知作者')} · 已移出书库，数据仍保留。</p></div><button class="subtle" data-restore-book="${esc(book.id)}">恢复</button></article>`).join('') : `<article class="asset-card"><div><strong>已移出书库</strong><p>这里暂时没有隐藏的书。</p></div><button class="subtle" id="hiddenRefresh">刷新</button></article>`;
      $('settingsPanel').innerHTML = `${importResult}<article class="asset-card"><div><strong>Click 状态</strong><p>${ok ? '运行正常。' : '暂时未连接，请重启 App。'}</p></div><button class="subtle" id="settingsRefresh">重新检查</button></article><article class="asset-card"><div><strong>手机同步</strong><p>Android Click 打开时，会在同一 Wi‑Fi 自动发现这台 Mac；无需常驻后台。</p></div></article><details><summary>已移出书库 · ${hidden.length} 本</summary><div class="asset-list" style="margin-top:10px">${hiddenList}</div></details><details><summary>高级信息</summary><p id="advancedInfo">书籍 ${state.books.length} 本，已移出 ${hidden.length} 本，笔记 ${state.dashboard?.summary?.note_count || 0} 条，红标 ${state.dashboard?.summary?.red_highlight_count || 0} 条。文件状态可在书籍详情中查看。</p><button class="subtle" id="copyLocal">复制连接地址</button></details>`;
      $('settingsRefresh').onclick = () => loadDashboard();
      if ($('hiddenRefresh')) $('hiddenRefresh').onclick = () => loadDashboard();
      $('copyLocal').onclick = () => navigator.clipboard?.writeText(location.origin + '/library').then(() => toast('已复制地址')).catch(() => toast(location.origin + '/library'));
      document.querySelectorAll('[data-open-import]').forEach((button) => button.onclick = () => openBook(byId(button.dataset.openImport)));
      document.querySelectorAll('[data-restore-book]').forEach((button) => button.onclick = () => restoreBooks([button.dataset.restoreBook], '已恢复到书库').catch((error) => toast(`恢复失败：${error.message}`)));
    }
    function renderBatchbar() {
      const count = state.selectedIds.size;
      $('batchbar').classList.toggle('show', state.manageMode || count > 0);
      $('manageToggle').textContent = state.manageMode ? '退出管理' : '管理';
      $('batchCount').textContent = state.manageMode ? `管理模式 · 已选择 ${count} 本` : `已选择 ${count} 本`;
      ['batchClear','batchFavorite','batchOrganize','batchExport','batchHide'].forEach((id) => { $(id).disabled = count === 0; });
    }
    function openRemoveModal(bookIds) {
      const ids = Array.from(new Set(bookIds.filter(Boolean)));
      const books = ids.map(byId).filter(Boolean);
      if (!ids.length || !books.length) return;
      state.pendingRemove = { ids, books };
      const title = books.length === 1 ? `《${books[0].title || books[0].id}》` : `${books.length} 本书`;
      $('removeModal').innerHTML = `<div class="modal" role="dialog" aria-modal="true" aria-labelledby="removeTitle">
        <h2 id="removeTitle">移出书库 ${esc(title)}</h2>
        <p>这不是删除。它只会从当前书库列表隐藏，方便你把正在读的书留下来。</p>
        <ul>
          <li>不会删除 EPUB 文件或内部副本。</li>
          <li>不会删除阅读进度、笔记、红标和单词本。</li>
          <li>可以在“设置”里的“已移出书库”恢复。</li>
        </ul>
        <div class="modal-actions"><button class="subtle" id="removeCancel">取消</button><button class="danger" id="removeConfirm">移出书库</button></div>
      </div>`;
      $('removeModal').classList.add('show');
      $('removeModal').setAttribute('aria-hidden', 'false');
      $('removeCancel').onclick = closeRemoveModal;
      $('removeConfirm').onclick = () => confirmRemove().catch((error) => toast(`移出失败：${error.message}`));
    }
    function closeRemoveModal() {
      $('removeModal').classList.remove('show');
      $('removeModal').setAttribute('aria-hidden', 'true');
      state.pendingRemove = null;
    }
    async function hideBooks(bookIds, message) {
      const ids = Array.from(new Set(bookIds.filter(Boolean)));
      if (!ids.length) return;
      await api('/api/library/books/batch-hide', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({book_ids:ids})});
      ids.forEach((id) => state.selectedIds.delete(id));
      state.lastRemoved = { ids, books: ids.map(byId).filter(Boolean) };
      toast(message || `已移出 ${ids.length} 本，数据保留`, '撤销', () => restoreBooks(ids, '已恢复到书库').catch((error) => toast(`恢复失败：${error.message}`)));
      await loadDashboard();
    }
    async function restoreBooks(bookIds, message) {
      const ids = Array.from(new Set(bookIds.filter(Boolean)));
      if (!ids.length) return;
      await api('/api/library/books/batch-restore', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({book_ids:ids})});
      ids.forEach((id) => state.selectedIds.delete(id));
      toast(message || `已恢复 ${ids.length} 本`);
      await loadDashboard();
    }
    async function confirmRemove() {
      const pending = state.pendingRemove;
      if (!pending?.ids?.length) return;
      const count = pending.ids.length;
      closeRemoveModal();
      await hideBooks(pending.ids, count === 1 ? '已移出书库，数据保留' : `已移出 ${count} 本，数据保留`);
      if (state.selectedBook && pending.ids.includes(state.selectedBook.id)) closeDrawer();
    }
    async function hideSingleBook(book) {
      if (!book) return;
      openRemoveModal([book.id]);
    }
    async function batchHide() {
      if (!state.selectedIds.size) return;
      openRemoveModal(Array.from(state.selectedIds));
    }
    async function favoriteSelectedBooks() {
      const ids = Array.from(state.selectedIds);
      if (!ids.length) return;
      await api('/api/library/books/batch-organization', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({book_ids:ids, favorite:true})});
      state.selectedIds.clear();
      toast(`已收藏 ${ids.length} 本`);
      await loadDashboard();
    }
    function openBatchOrgModal() {
      const ids = Array.from(state.selectedIds);
      if (!ids.length) return;
      closeDrawer();
      const categoryOptions = Array.from(new Set((state.dashboard?.category_groups || []).map((group) => group.category).filter(Boolean))).filter((name) => name !== '未分类');
      $('orgModal').innerHTML = `<div class="modal" role="dialog" aria-modal="true" aria-labelledby="orgTitle">
        <h2 id="orgTitle">批量分类 · ${ids.length} 本</h2>
        <p>空字段保持不变。这里适合把一组书放进同一书架，或统一覆盖一组标签。</p>
        <div class="org-panel">
          <strong>收藏状态</strong>
          <select id="batchFavoriteMode"><option value="keep">保持不变</option><option value="yes">设为收藏</option><option value="no">取消收藏</option></select>
          <strong>自定义分类</strong>
          <input id="batchCategoryField" list="batchCategoryList" placeholder="例如：广告、英语、战略">
          <datalist id="batchCategoryList">${categoryOptions.map((name) => `<option value="${esc(name)}"></option>`).join('')}</datalist>
          <strong>标签</strong>
          <input id="batchTagField" placeholder="覆盖标签，用逗号分隔；留空则不改">
        </div>
        <div class="modal-actions"><button class="subtle" id="orgCancel">取消</button><button class="primary" id="orgConfirm">应用</button></div>
      </div>`;
      $('orgModal').classList.add('show');
      $('orgModal').setAttribute('aria-hidden', 'false');
      $('orgCancel').onclick = closeOrgModal;
      $('orgConfirm').onclick = () => applyBatchOrganization().catch((error) => toast(`批量分类失败：${error.message}`));
    }
    function closeOrgModal() {
      $('orgModal').classList.remove('show');
      $('orgModal').setAttribute('aria-hidden', 'true');
    }
    async function applyBatchOrganization() {
      const ids = Array.from(state.selectedIds);
      if (!ids.length) return;
      const payload = {book_ids: ids};
      const favoriteMode = $('batchFavoriteMode').value;
      if (favoriteMode !== 'keep') payload.favorite = favoriteMode === 'yes';
      const category = $('batchCategoryField').value.trim();
      if (category) payload.custom_category = category;
      const tagText = $('batchTagField').value.trim();
      if (tagText) payload.tags = tagText.split(/[，,]/).map((item) => item.trim()).filter(Boolean);
      if (Object.keys(payload).length === 1) { toast('没有选择要修改的字段'); return; }
      await api('/api/library/books/batch-organization', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
      state.selectedIds.clear();
      closeOrgModal();
      toast(`已更新 ${ids.length} 本`);
      await loadDashboard();
    }
    async function batchExport() {
      if (!state.selectedIds.size) return;
      for (const id of Array.from(state.selectedIds)) await api(`/books/${id}/export`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({include_json:true})});
      toast('批量导出完成');
    }
    function openDrawer(book) {
      if (!book) return;
      state.selectedBook = book;
      const org = book.organization || {};
      const tagText = (org.tags || []).join('，');
      const favoriteText = org.favorite ? '取消收藏' : '收藏';
      const analysisState = book.living_book_analysis?.state || 'not_requested';
      const analysisText = analysisState === 'queued' ? '已排队' : analysisState === 'running' ? '分析中' : analysisState === 'needs_review' ? '待审阅' : analysisState === 'complete' ? '已完成' : analysisState === 'failed' ? '分析失败' : '未分析';
      const analysisAction = ['queued','running'].includes(analysisState) ? '查看分析状态' : ['needs_review','complete'].includes(analysisState) ? '重新分析' : analysisState === 'failed' ? '重试分析' : '分析';
      const categoryOptions = Array.from(new Set((state.dashboard?.category_groups || []).map((group) => group.category).filter(Boolean))).filter((name) => name !== '未分类');
      const chips = (org.tags || []).length ? `<div class="org-tags">${org.tags.map((tag) => `<span class="badge">${esc(tag)}</span>`).join('')}</div>` : '';
      $('drawer').innerHTML = `<button class="ghost" id="drawerClose">关闭</button>${cover(book)}<h2>${esc(book.title || book.id)}</h2><div class="book-meta">${esc(book.author || '未知作者')} · ${esc(book.reading_state || '')}</div><div class="book-row"><span class="badge">${esc(org.category || '未分类')}</span><span class="badge">${esc(analysisText)}</span>${book.compatibility?.correction_queue_required ? '<span class="badge">待纠错</span>' : ''}${org.favorite ? '<span class="badge">已收藏</span>' : ''}</div><div class="progress"><i style="width:${book.progress?.percent || 0}%"></i></div><div class="drawer-actions"><button class="primary" id="drawerOpen">继续阅读</button><button class="subtle" id="drawerFavorite">${favoriteText}</button><button class="subtle" id="drawerVocab">单词本</button><button class="subtle" id="drawerAnalyze">${esc(analysisAction)}</button><button class="subtle" data-view-jump="notes">笔记</button><button class="subtle" data-view-jump="red">红标</button><button class="subtle" id="drawerReveal">显示副本</button><button class="subtle" id="drawerExport">导出</button><button class="subtle" id="drawerManage">选择管理</button></div><section class="org-panel"><strong>自定义分类</strong><div class="org-row"><input id="categoryField" list="categoryList" value="${esc(org.custom_category || '')}" placeholder="例如：战略、英语、属灵书籍"><button class="primary" id="saveCategory">保存</button></div><datalist id="categoryList">${categoryOptions.map((name) => `<option value="${esc(name)}"></option>`).join('')}</datalist><strong>标签</strong><div class="org-row"><input id="tagField" value="${esc(tagText)}" placeholder="用逗号分隔，例如：面试,精读,复习"><button class="subtle" id="saveTags">保存</button></div>${chips}</section><details><summary>高级信息</summary><p>文件存在：${book.file?.exists ? '是' : '否'}<br>内部副本：${book.file?.owned_internal_copy ? '是' : '否'}<br>阅读类型：${esc(book.compatibility?.reading_profile || 'UNKNOWN')}<br>${esc(book.file?.file_path || '')}</p></details>`;
      $('drawer').classList.add('open');
      $('drawer').setAttribute('aria-hidden', 'false');
      $('drawerBackdrop').classList.add('show');
      $('drawerClose').onclick = closeDrawer;
      $('drawerOpen').onclick = () => openBook(book);
      $('drawerFavorite').onclick = () => toggleFavorite(book).catch((error) => toast(`收藏失败：${error.message}`));
      $('drawerVocab').onclick = () => { window.location.href = vocabURL(book.id); };
      $('drawerAnalyze').onclick = async () => {
        if (['queued','running'].includes(analysisState)) {
          const status = await api(book.actions.analysis_status_url);
          toast(`分析状态：${status.analysis?.state || analysisState}`);
          await loadDashboard();
          return;
        }
        const reanalyze = ['needs_review','complete'].includes(analysisState);
        if (reanalyze && !confirm('重新分析不会覆盖已审阅文件；新结果将写入草稿。继续吗？')) return;
        $('drawerAnalyze').disabled = true;
        $('drawerAnalyze').textContent = '已排队';
        await api(reanalyze ? book.actions.reanalyze_url : book.actions.analyze_url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({requested_by:'click_library', force:reanalyze})});
        toast('分析已排队，可继续阅读');
        await loadDashboard();
      };
      $('drawerReveal').onclick = async () => { await api(`/api/library/books/${book.id}/reveal`, {method:'POST'}); toast('已在 Finder 显示'); };
      $('drawerExport').onclick = async () => { await api(`/books/${book.id}/export`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({include_json:true})}); toast('导出完成'); };
      $('drawerManage').onclick = () => { state.manageMode = true; state.selectedIds.add(book.id); closeDrawer(); setView('library'); toast('已进入管理模式，可移出单本或批量整理'); };
      $('saveCategory').onclick = () => updateBookOrganization(book, {custom_category:$('categoryField').value}, '分类已保存').catch((error) => toast(`保存失败：${error.message}`));
      $('saveTags').onclick = () => {
        const tags = $('tagField').value.split(/[，,]/).map((item) => item.trim()).filter(Boolean);
        updateBookOrganization(book, {tags}, '标签已保存').catch((error) => toast(`保存失败：${error.message}`));
      };
      document.querySelectorAll('#drawer [data-view-jump]').forEach((button) => button.onclick = () => setView(button.dataset.viewJump));
    }
    function closeDrawer() { $('drawer').classList.remove('open'); $('drawer').setAttribute('aria-hidden', 'true'); $('drawerBackdrop').classList.remove('show'); }
    function renderError() {
      $('homeView').innerHTML = `<div class="error-box"><h2>书库暂时打不开</h2><p>${esc(state.error || '本地服务未响应。')}</p><button class="primary" id="retryLoad">重试</button></div>`;
      $('retryLoad').onclick = () => location.reload();
    }
    function render() {
      renderNav();
      visible(state.view);
      $('serviceStatus').textContent = state.dashboard?.ok ? '可阅读' : '未连接';
      if (state.error) { renderError(); return; }
      renderHome(); renderLibrary(); renderOrganizationViews(); renderAssets(); renderSettings();
    }
    async function loadDashboard() {
      state.error = null;
      state.dashboard = await api('/api/library/dashboard?include_hidden=true');
      state.books = state.dashboard.books || [];
      state.hiddenBooks = state.dashboard.hidden_books || [];
      state.assets = state.dashboard.recent_annotations || [];
      Array.from(state.selectedIds).forEach((id) => { if (!byId(id)) state.selectedIds.delete(id); });
      state.activeBookId = state.activeBookId || state.dashboard.current_book?.id || state.books[0]?.id || null;
      render();
    }
    async function importFile(file) {
      const buffer = await file.arrayBuffer();
      let binary = '';
      const bytes = new Uint8Array(buffer);
      for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
      toast('正在导入书籍...');
      state.lastImport = await api('/api/library/import', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({filename:file.name, content_base64:btoa(binary)})});
      toast('导入完成');
      await loadDashboard();
      setView('settings');
    }
    function selectNext(delta) {
      const books = sortedBooks();
      if (!books.length) return;
      const index = Math.max(0, books.findIndex((book) => book.id === state.activeBookId));
      const next = books[(index + delta + books.length) % books.length];
      state.activeBookId = next.id;
      const node = document.querySelector(`[data-book="${CSS.escape(next.id)}"]`);
      if (node) node.focus({preventScroll:false});
    }
    $('search').oninput = (event) => { state.query = event.target.value; $('librarySearch').value = state.query; render(); };
    $('librarySearch').oninput = (event) => { state.query = event.target.value; $('search').value = state.query; render(); };
    $('sort').onchange = (event) => { state.sort = event.target.value; render(); };
    $('stateFilter').onchange = (event) => { state.stateFilter = event.target.value; render(); };
    $('refresh').onclick = () => loadDashboard().catch((error) => { state.error = error.message; render(); });
    ['topImport'].forEach((id) => $(id).onclick = () => $('fileInput').click());
    document.querySelectorAll('[data-library-scope]').forEach((button) => button.onclick = () => {
      state.libraryScope = button.dataset.libraryScope || 'all';
      state.folderPath = '';
      renderHome();
    });
    $('homeManage').onclick = () => {
      state.manageMode = true;
      setView('library');
      toast('已进入整理模式，完成后会回到阅读书架');
    };
    $('manageToggle').onclick = () => {
      state.manageMode = !state.manageMode;
      if (!state.manageMode) state.selectedIds.clear();
      if (!state.manageMode) setView('home'); else render();
    };
    $('selectAllCurrent').onclick = () => {
      const books = sortedBooks();
      const allSelected = books.length > 0 && books.every((book) => state.selectedIds.has(book.id));
      books.forEach((book) => allSelected ? state.selectedIds.delete(book.id) : state.selectedIds.add(book.id));
      renderLibrary();
    };
    $('batchClear').onclick = () => { state.selectedIds.clear(); renderLibrary(); };
    $('batchExit').onclick = () => { state.manageMode = false; state.selectedIds.clear(); setView('home'); };
    $('batchFavorite').onclick = () => favoriteSelectedBooks().catch((error) => toast(`批量收藏失败：${error.message}`));
    $('batchOrganize').onclick = openBatchOrgModal;
    $('batchHide').onclick = () => batchHide().catch((error) => toast(`移出失败：${error.message}`));
    $('batchExport').onclick = () => batchExport().catch((error) => toast(`导出失败：${error.message}`));
    $('drawerBackdrop').onclick = closeDrawer;
    $('fileInput').onchange = (event) => {
      const file = event.target.files && event.target.files[0];
      if (file) importFile(file).catch((error) => toast(`导入失败：${error.message}`));
      event.target.value = '';
    };
    document.addEventListener('keydown', (event) => {
      if (event.defaultPrevented) return;
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'f') { event.preventDefault(); $('search').focus(); return; }
      if (event.key === 'Escape') { closeDrawer(); closeOrgModal(); closeRemoveModal(); return; }
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') { selectNext(1); return; }
      if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') { selectNext(-1); return; }
      if (event.key === 'Enter' && state.activeBookId && !['INPUT','SELECT','TEXTAREA','BUTTON'].includes(document.activeElement.tagName)) openBook(byId(state.activeBookId));
    });
    loadDashboard().catch((error) => { state.error = error.message; render(); });
  </script>
</body>
</html>'''


def vocabulary_page_html() -> str:
    return r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sentence Reader Vocabulary</title>
  <style>
    :root { color-scheme: dark; --bg:#070807; --panel:#141612; --panel2:#1d211a; --line:#34392e; --text:#f7f3e8; --muted:#aaa590; --accent:#d7a84f; --green:#8fbe7a; --danger:#e9786b; }
    * { box-sizing:border-box; }
    html, body { margin:0; min-height:100%; background:var(--bg); color:var(--text); font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif; }
    button, input, select { font:inherit; }
    button { border:0; border-radius:8px; background:var(--panel2); color:var(--text); padding:9px 12px; cursor:pointer; }
    button.primary { background:var(--accent); color:#17120a; font-weight:800; }
    button.subtle { background:#10120e; border:1px solid var(--line); color:#ddd3b8; }
    button.danger { background:rgba(233,120,107,.12); border:1px solid rgba(233,120,107,.34); color:#ffc1b8; }
    input, select { width:100%; border:1px solid var(--line); background:#10120e; color:var(--text); border-radius:8px; padding:10px 12px; outline:none; }
    .shell { max-width:1180px; margin:0 auto; padding:22px; }
    .top { display:grid; grid-template-columns:auto minmax(190px,1fr) minmax(150px,180px) minmax(118px,150px) minmax(138px,170px) minmax(160px,1fr) auto auto auto; gap:10px; align-items:center; margin-bottom:16px; }
    .brand { min-width:0; }
    .brand h1 { margin:0; font-size:24px; letter-spacing:0; }
    .brand p { margin:5px 0 0; color:var(--muted); font-size:13px; }
    .stats { display:flex; flex-wrap:wrap; gap:8px; margin:10px 0 16px; }
    .pill { border:1px solid var(--line); background:#10120e; border-radius:999px; padding:5px 9px; color:#ddd3b8; font-size:12px; }
    .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:12px; }
    .card { border:1px solid var(--line); background:var(--panel); border-radius:10px; padding:13px; min-width:0; display:grid; gap:9px; }
    .word { display:flex; align-items:baseline; justify-content:space-between; gap:8px; }
    .word strong { font-size:20px; word-break:break-word; }
    .meaning { color:var(--accent); font-weight:800; min-height:20px; }
    .sentence { color:#e8dfc7; line-height:1.55; font-size:13px; display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden; }
    .zh { color:#c9c2ac; }
    .actions { display:flex; flex-wrap:wrap; gap:7px; }
    .study { display:none; border:1px solid var(--line); background:linear-gradient(180deg,#171a14,#11130f); border-radius:10px; padding:16px; margin:0 0 16px; }
    .study.show { display:grid; gap:12px; }
    .study-head { display:flex; justify-content:space-between; align-items:center; gap:12px; color:var(--muted); font-size:13px; }
    .study-word { font-size:30px; font-weight:850; }
    .study-card { display:grid; gap:10px; min-height:170px; }
    .study-answer { display:none; border-top:1px solid var(--line); padding-top:10px; }
    .study-answer.show { display:grid; gap:8px; }
    .empty { border:1px dashed #555941; color:var(--muted); border-radius:10px; padding:24px; }
    .review { color:#ffd0ca; border-color:rgba(233,120,107,.38); }
    .known { opacity:.58; }
    @media (max-width:820px) { .top { grid-template-columns:1fr; } .shell { padding:14px; } }
  </style>
</head>
<body>
  <main class="shell">
    <div class="top">
      <button class="subtle" id="back">书库</button>
      <div class="brand"><h1>单词本</h1><p id="subtitle">按书生成，中文句是证据，短义项只在确认时显示。</p></div>
      <select id="bookSelect"></select>
      <select id="statusFilter">
        <option value="all">全部状态</option>
        <option value="candidate">候选</option>
        <option value="reviewing">复习中</option>
        <option value="known">已掌握</option>
        <option value="ignored">已忽略</option>
      </select>
      <select id="alignmentFilter">
        <option value="all">全部对齐</option>
        <option value="confirmed_context_meaning">直译确认</option>
        <option value="paraphrased_context_meaning">上下文意译</option>
        <option value="context_sentence_available">有中文句</option>
        <option value="suspected_alignment_mismatch">疑似错配</option>
        <option value="missing_chinese_sentence">缺中文句</option>
      </select>
      <input id="query" placeholder="查单词或中文义项">
      <button class="primary" id="studyToggle">学习</button>
      <button class="subtle" id="exportGlossary">导出</button>
      <button class="subtle" id="lifeStudyReview">审校</button>
    </div>
    <div class="stats" id="stats"></div>
    <section class="study" id="studyPanel"></section>
    <section class="grid" id="grid"></section>
  </main>
  <script>
    const params = new URLSearchParams(location.search);
    const state = { books:[], bookId:params.get('book_id') || '', items:[], query:'', status:'all', alignment:'all', study:{open:false,index:0,answer:false} };
    const $ = (id) => document.getElementById(id);
    const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    async function api(url, options) {
      const response = await fetch(url, options);
      if (!response.ok) throw new Error(await response.text());
      return response.json();
    }
    function speak(text) {
      const value = String(text || '').trim();
      if (!value || !window.speechSynthesis) return;
      window.speechSynthesis.cancel();
      const utterance = new SpeechSynthesisUtterance(value);
      utterance.lang = /^[\x00-\x7F]+$/.test(value) ? 'en-US' : 'zh-CN';
      utterance.rate = .92;
      window.speechSynthesis.speak(utterance);
    }
    function dueScore(item) {
      const next = item.user_vocab?.next_review_at ? Date.parse(item.user_vocab.next_review_at) : 0;
      if (!next) return 1;
      return next <= Date.now() ? 0 : 2;
    }
    function studyQueue() {
      return state.items
        .filter((item) => !['known','ignored'].includes(item.status))
        .sort((a, b) => dueScore(a) - dueScore(b) || Number(b.score || 0) - Number(a.score || 0));
    }
    function filtersURL() {
      const search = new URLSearchParams();
      search.set('limit', '500');
      if (state.query.trim()) search.set('query', state.query.trim());
      if (state.status !== 'all') search.set('status', state.status);
      if (state.alignment !== 'all') search.set('alignment_status', state.alignment);
      return `/books/${encodeURIComponent(state.bookId)}/vocab?${search.toString()}`;
    }
    async function loadBooks() {
      const dashboard = await api('/api/library/dashboard');
      state.books = dashboard.books || [];
      if (!state.bookId) state.bookId = dashboard.current_book?.id || state.books[0]?.id || '';
      $('bookSelect').innerHTML = state.books.map((book) => `<option value="${esc(book.id)}">${esc(book.title || book.id)}</option>`).join('');
      $('bookSelect').value = state.bookId;
    }
    async function loadVocab() {
      if (!state.bookId) { renderEmpty('没有可用书籍'); return; }
      const payload = await api(filtersURL());
      state.items = payload.items || [];
      state.study.index = Math.min(state.study.index, Math.max(0, studyQueue().length - 1));
      render();
    }
    function renderStats() {
      const total = state.items.length;
      const confirmed = state.items.filter((item) => item.alignment_status === 'confirmed_context_meaning').length;
      const paraphrased = state.items.filter((item) => item.alignment_status === 'paraphrased_context_meaning').length;
      const suspect = state.items.filter((item) => item.alignment_status === 'suspected_alignment_mismatch').length;
      const meaning = state.items.filter((item) => item.context_meaning_zh).length;
      $('stats').innerHTML = [`${total} 个词`, `${meaning} 个短义项`, `${confirmed} 个直译确认`, `${paraphrased} 个意译`, `${suspect} 个疑似错配`].map((text) => `<span class="pill">${esc(text)}</span>`).join('');
    }
    function renderEmpty(text) { $('grid').innerHTML = `<div class="empty">${esc(text)}</div>`; }
    function alignmentTitle(status) {
      return ({
        confirmed_context_meaning: '直译确认',
        paraphrased_context_meaning: '上下文意译',
        context_sentence_available: '有中文句',
        suspected_alignment_mismatch: '疑似错配',
        missing_chinese_sentence: '缺中文句',
        needs_review: '需复核'
      })[status] || status || '未知';
    }
    function card(item) {
      const meaning = item.context_meaning_zh || '看中文句';
      const cls = item.status === 'known' ? ' known' : '';
      const isReview = ['needs_review', 'suspected_alignment_mismatch', 'missing_chinese_sentence'].includes(item.alignment_status);
      const review = `<span class="pill${isReview ? ' review' : ''}">${esc(alignmentTitle(item.alignment_status))}</span>`;
      const source = item.meaning_source === 'user_glossary' ? '<span class="pill">用户修正</span>' : (item.meaning_source === 'dictionary_fallback' ? '<span class="pill">词典短释</span>' : '');
      return `<article class="card${cls}" data-id="${esc(item.id)}">
        <div class="word"><strong>${esc(item.surface)}</strong><span>${esc(item.occurrence_count)} 次</span></div>
        <div class="meaning">${esc(meaning)}</div>
        <div class="sentence">${esc(item.representative_sentence_en || '')}</div>
        <div class="sentence zh">${esc(item.representative_sentence_zh || '')}</div>
        <div>${review}<span class="pill">${esc(item.status || '')}</span>${source}</div>
        <div class="actions">
          <button class="subtle" data-speak-word="${esc(item.surface)}">读词</button>
          <button class="subtle" data-speak-sentence="${esc(item.representative_sentence_en || '')}">读句</button>
          <button class="subtle" data-edit="${esc(item.id)}">修正</button>
          <button class="primary" data-status="reviewing" data-item="${esc(item.id)}">复习</button>
          <button class="subtle" data-status="known" data-item="${esc(item.id)}">掌握</button>
          <button class="danger" data-status="ignored" data-item="${esc(item.id)}">忽略</button>
        </div>
      </article>`;
    }
    async function editMeaning(itemId) {
      const item = state.items.find((candidate) => candidate.id === itemId);
      if (!item) return;
      const next = window.prompt(`${item.surface} 的本句义`, item.context_meaning_zh || '');
      if (next === null) return;
      const value = next.trim();
      if (!value) return;
      await api(`/books/${encodeURIComponent(state.bookId)}/vocab/${encodeURIComponent(item.id)}`, {
        method:'PATCH',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({context_meaning_zh:value})
      });
      await loadVocab();
    }
    function bind() {
      document.querySelectorAll('[data-speak-word]').forEach((button) => button.onclick = () => speak(button.dataset.speakWord));
      document.querySelectorAll('[data-speak-sentence]').forEach((button) => button.onclick = () => speak(button.dataset.speakSentence));
      document.querySelectorAll('[data-edit]').forEach((button) => button.onclick = () => editMeaning(button.dataset.edit).catch((error) => alert(`保存失败：${error.message}`)));
      document.querySelectorAll('[data-status]').forEach((button) => button.onclick = async () => {
        await api(`/books/${encodeURIComponent(state.bookId)}/vocab/${encodeURIComponent(button.dataset.item)}`, {
          method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({status:button.dataset.status})
        });
        await loadVocab();
      });
    }
    function render() {
      renderStats();
      $('grid').innerHTML = state.items.length ? state.items.map(card).join('') : `<div class="empty">没有匹配的词。</div>`;
      bind();
      renderStudy();
    }
    function currentStudyItem() {
      const queue = studyQueue();
      return { queue, item: queue[state.study.index] || null };
    }
    function renderStudy() {
      const panel = $('studyPanel');
      panel.classList.toggle('show', state.study.open);
      if (!state.study.open) { panel.innerHTML = ''; return; }
      const { queue, item } = currentStudyItem();
      if (!item) {
        panel.innerHTML = `<div class="study-head"><strong>学习模式</strong><span>没有待学词</span></div><div class="empty">当前筛选下没有需要复习的词。</div>`;
        return;
      }
      const answer = state.study.answer ? ' show' : '';
      const meaning = item.context_meaning_zh || '未确认短义项';
      const source = item.meaning_source === 'dictionary_fallback' ? '<span class="pill">词典短释</span>' : (item.meaning_source === 'user_glossary' ? '<span class="pill">用户修正</span>' : '');
      panel.innerHTML = `<div class="study-head"><strong>学习模式</strong><span>${esc(state.study.index + 1)} / ${esc(queue.length)} · 掌握度 ${esc(item.user_vocab?.mastery_level || 0)}</span></div>
        <div class="study-card">
          <div class="study-word">${esc(item.surface)}</div>
          <div class="sentence">${esc(item.representative_sentence_en || '')}</div>
          <div class="actions">
            <button class="subtle" id="studySpeakWord">读词</button>
            <button class="subtle" id="studySpeakSentence">读句</button>
            <button class="primary" id="studyAnswer">${state.study.answer ? '隐藏答案' : '显示答案'}</button>
            <button class="subtle" id="studySkip">下一个</button>
          </div>
          <div class="study-answer${answer}">
            <div class="meaning">${esc(meaning)}</div>
            <div class="sentence zh">${esc(item.representative_sentence_zh || '')}</div>
            <div><span class="pill">${esc(alignmentTitle(item.alignment_status))}</span><span class="pill">${esc(item.status || '')}</span>${source}</div>
            <div class="actions">
              <button class="danger" data-review="unknown">不认识</button>
              <button class="subtle" data-review="fuzzy">模糊</button>
              <button class="primary" data-review="known">认识</button>
            </div>
          </div>
        </div>`;
      $('studySpeakWord').onclick = () => speak(item.surface);
      $('studySpeakSentence').onclick = () => speak(item.representative_sentence_en);
      $('studyAnswer').onclick = () => { state.study.answer = !state.study.answer; renderStudy(); };
      $('studySkip').onclick = () => { state.study.index = (state.study.index + 1) % queue.length; state.study.answer = false; renderStudy(); };
      document.querySelectorAll('[data-review]').forEach((button) => button.onclick = async () => {
        const payload = await api(`/books/${encodeURIComponent(state.bookId)}/vocab/${encodeURIComponent(item.id)}/review`, {
          method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({rating:button.dataset.review})
        });
        const index = state.items.findIndex((candidate) => candidate.id === item.id);
        if (index >= 0) state.items[index] = payload.item;
        state.study.answer = false;
        state.study.index = Math.min(state.study.index, Math.max(0, studyQueue().length - 1));
        render();
      });
    }
    $('back').onclick = () => { location.href = state.bookId ? `/library?book_id=${encodeURIComponent(state.bookId)}` : '/library'; };
    $('studyToggle').onclick = () => { state.study.open = !state.study.open; state.study.answer = false; renderStudy(); };
    $('exportGlossary').onclick = () => { if (state.bookId) location.href = `/books/${encodeURIComponent(state.bookId)}/glossary/export.csv`; };
    $('lifeStudyReview').onclick = () => { location.href = '/lifestudy/vocab/review'; };
    $('bookSelect').onchange = async (event) => { state.bookId = event.target.value; history.replaceState(null, '', `/vocab?book_id=${encodeURIComponent(state.bookId)}`); await loadVocab(); };
    $('statusFilter').onchange = async (event) => { state.status = event.target.value; await loadVocab(); };
    $('alignmentFilter').onchange = async (event) => { state.alignment = event.target.value; await loadVocab(); };
    $('query').oninput = (() => { let timer = 0; return (event) => { state.query = event.target.value; clearTimeout(timer); timer = setTimeout(loadVocab, 180); }; })();
    loadBooks().then(loadVocab).catch((error) => renderEmpty(error.message));
  </script>
</body>
</html>'''


def lifestudy_vocab_review_page_html() -> str:
    return r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Life-study Vocabulary Review</title>
  <style>
    :root { color-scheme: dark; --bg:#070807; --panel:#141612; --panel2:#1d211a; --line:#34392e; --text:#f7f3e8; --muted:#aaa590; --accent:#d7a84f; --green:#8fbe7a; --danger:#e9786b; }
    * { box-sizing:border-box; }
    body { margin:0; background:var(--bg); color:var(--text); font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif; }
    button, input, textarea, select { font:inherit; }
    button { border:0; border-radius:8px; background:var(--panel2); color:var(--text); padding:9px 12px; cursor:pointer; }
    button.primary { background:var(--accent); color:#17120a; font-weight:800; }
    button.subtle { background:#10120e; border:1px solid var(--line); color:#ddd3b8; }
    button.danger { background:rgba(233,120,107,.12); border:1px solid rgba(233,120,107,.34); color:#ffc1b8; }
    select, textarea, input { width:100%; border:1px solid var(--line); background:#10120e; color:var(--text); border-radius:8px; padding:9px 10px; outline:none; }
    textarea { min-height:68px; resize:vertical; line-height:1.45; }
    .shell { max-width:1240px; margin:0 auto; padding:22px; }
    .top { display:grid; grid-template-columns:auto 1fr auto auto; gap:10px; align-items:center; margin-bottom:14px; }
    h1 { margin:0; font-size:24px; letter-spacing:0; }
    .muted { color:var(--muted); font-size:13px; }
    .stats { display:flex; flex-wrap:wrap; gap:8px; margin:12px 0 16px; }
    .pill { border:1px solid var(--line); background:#10120e; border-radius:999px; padding:5px 9px; color:#ddd3b8; font-size:12px; }
    .bad { color:#ffd0ca; border-color:rgba(233,120,107,.42); }
    .good { color:#d9f5c7; border-color:rgba(143,190,122,.42); }
    .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(340px,1fr)); gap:12px; }
    .card { border:1px solid var(--line); background:var(--panel); border-radius:10px; padding:13px; display:grid; gap:9px; min-width:0; }
    .card h2 { margin:0; font-size:20px; word-break:break-word; }
    .meaning { color:var(--accent); font-weight:800; }
    .evidence { color:#e8dfc7; line-height:1.55; font-size:13px; }
    .zh { color:#c9c2ac; }
    .row { display:grid; grid-template-columns:120px 1fr; gap:8px; align-items:start; }
    .actions { display:flex; flex-wrap:wrap; gap:7px; }
    .notice { border:1px dashed #555941; color:#ddd3b8; border-radius:10px; padding:14px; margin-bottom:14px; line-height:1.5; }
    pre { white-space:pre-wrap; word-break:break-word; background:#10120e; border:1px solid var(--line); border-radius:10px; padding:12px; color:#d8d0ba; }
    @media (max-width:820px) { .top { grid-template-columns:1fr; } .row { grid-template-columns:1fr; } .shell { padding:14px; } }
  </style>
</head>
<body>
  <main class="shell">
    <div class="top">
      <button class="subtle" id="back">单词</button>
      <div><h1>生命读经词库审校</h1><div class="muted">只保存审校文件，不直接写数据库。</div></div>
      <button class="primary" id="dryRun">Dry-run</button>
      <button class="subtle" id="reload">刷新</button>
    </div>
    <section class="notice" id="notice"></section>
    <div class="stats" id="stats"></div>
    <section class="grid" id="grid"></section>
    <pre id="dryRunOutput" style="display:none"></pre>
  </main>
  <script>
    const state = { payload:null };
    const $ = (id) => document.getElementById(id);
    const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    async function api(url, options) {
      const response = await fetch(url, options);
      if (!response.ok) throw new Error(await response.text());
      return response.json();
    }
    function decisionLabel(value) {
      return ({pending:'待审', approve:'通过', correct:'修正', reject:'拒绝'})[value] || value;
    }
    function renderStats() {
      const q = state.payload?.quality || {};
      const c = state.payload?.decision_counts || {};
      const canExpand = state.payload?.can_expand_next_volume;
      $('stats').innerHTML = [
        `总数 ${q.term_count || 0}`,
        `A ${q.grade_counts?.A || 0}`,
        `B ${q.grade_counts?.B || 0}`,
        `待审 ${c.pending || 0}`,
        `通过 ${c.approve || 0}`,
        `修正 ${c.correct || 0}`,
        `拒绝 ${c.reject || 0}`,
        `污染 ${q.dictionary_pollution_count || 0}`,
        canExpand ? '可进入下一卷' : '不可进入下一卷'
      ].map((text, index) => `<span class="pill ${index === 8 ? (canExpand ? 'good' : 'bad') : ''}">${esc(text)}</span>`).join('');
      $('notice').textContent = canExpand
        ? 'Genesis 审校已满足下一卷前置条件。真正写库仍需命令行显式 --apply。'
        : '当前仍不能扩下一卷：所有条目必须完成 approve/correct/reject，审后精度需 >=85%，且不能有缺失书内行或通用词典污染。';
    }
    function card(item) {
      const decision = item.decision || 'pending';
      return `<article class="card" data-term="${esc(item.term)}">
        <h2>${esc(item.term)}</h2>
        <div><span class="pill">Grade ${esc(item.quality_grade)}</span><span class="pill">${esc(decisionLabel(decision))}</span><span class="pill">Page ${esc(item.source_page)}</span></div>
        <div class="meaning">${esc(item.final_meaning_zh || item.current_meaning_zh || '')}</div>
        <div class="evidence">${esc(item.evidence_en || '')}</div>
        <div class="evidence zh">${esc(item.evidence_zh_simp || '')}</div>
        <div class="row"><label>决定</label><select data-decision="${esc(item.term)}">
          ${['pending','approve','correct','reject'].map((value) => `<option value="${value}" ${decision === value ? 'selected' : ''}>${decisionLabel(value)}</option>`).join('')}
        </select></div>
        <div class="row"><label>修正义项</label><input data-correction="${esc(item.term)}" value="${esc(item.corrected_meaning_zh || '')}" placeholder="仅 decision=correct 时填写"></div>
        <div class="row"><label>备注</label><textarea data-note="${esc(item.term)}" placeholder="reject 必须填写理由">${esc(item.review_note || '')}</textarea></div>
        <div class="actions"><button class="primary" data-save="${esc(item.term)}">保存</button><button class="subtle" data-approve="${esc(item.term)}">通过</button><button class="danger" data-reject="${esc(item.term)}">拒绝</button></div>
      </article>`;
    }
    async function saveDecision(term, forced = null) {
      const card = document.querySelector(`[data-term="${CSS.escape(term)}"]`);
      const decision = forced || card.querySelector('[data-decision]').value;
      const corrected = card.querySelector('[data-correction]').value.trim();
      const note = card.querySelector('[data-note]').value.trim();
      state.payload = await api('/api/lifestudy/vocab/review/decision', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({term, decision, corrected_meaning_zh:corrected, note})
      });
      render();
    }
    function bind() {
      document.querySelectorAll('[data-save]').forEach((button) => button.onclick = () => saveDecision(button.dataset.save).catch((error) => alert(`保存失败：${error.message}`)));
      document.querySelectorAll('[data-approve]').forEach((button) => button.onclick = () => saveDecision(button.dataset.approve, 'approve').catch((error) => alert(`保存失败：${error.message}`)));
      document.querySelectorAll('[data-reject]').forEach((button) => button.onclick = () => saveDecision(button.dataset.reject, 'reject').catch((error) => alert(`拒绝需要备注：${error.message}`)));
    }
    function render() {
      renderStats();
      $('grid').innerHTML = (state.payload?.items || []).map(card).join('');
      bind();
    }
    async function load() {
      state.payload = await api('/api/lifestudy/vocab/review');
      render();
    }
    $('back').onclick = () => { location.href = '/vocab'; };
    $('reload').onclick = () => load().catch((error) => alert(`加载失败：${error.message}`));
    $('dryRun').onclick = async () => {
      const result = await api('/api/lifestudy/vocab/review/dry-run', {method:'POST'});
      $('dryRunOutput').style.display = 'block';
      $('dryRunOutput').textContent = JSON.stringify(result.result || {ok:result.ok, stderr:result.stderr}, null, 2);
      await load();
    };
    load().catch((error) => { $('notice').textContent = `加载失败：${error.message}`; });
  </script>
</body>
</html>'''


@app.get("/health")
def health() -> dict[str, Any]:
    processing_worker = voice_processing_worker_metrics()
    try:
        return {
            "ok": True,
            "schema": RUNTIME_HEALTH_SCHEMA,
            "runtime": runtime_payload(),
            "database": db.health(),
            "voice_processing_worker": processing_worker,
        }
    except Exception as exc:  # noqa: BLE001 - health endpoint must expose boundary failures.
        return {
            "ok": False,
            "schema": RUNTIME_HEALTH_SCHEMA,
            "runtime": runtime_payload(),
            "voice_processing_worker": processing_worker,
            "error": exc.__class__.__name__,
            "detail": str(exc),
        }


@app.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/library", response_class=HTMLResponse)
def library_page(request: FastAPIRequest) -> HTMLResponse:
    if should_use_lite_ui(request):
        return apply_lite_ui_cookie(HTMLResponse(library_lite_html(request)), request)
    return apply_lite_ui_cookie(HTMLResponse(library_page_html_v2()), request)


@app.get("/library-lite", response_class=HTMLResponse)
def library_lite_page(request: FastAPIRequest) -> HTMLResponse:
    return apply_lite_ui_cookie(HTMLResponse(library_lite_html(request)), request)


@app.get("/reader-lite", response_class=HTMLResponse)
def reader_lite_page(
    request: FastAPIRequest,
    book_id: str,
    chapter: int = 0,
    page: int = 0,
    selected: Optional[str] = None,
    auto: int = 1,
    notice: str = "",
) -> HTMLResponse:
    return apply_lite_ui_cookie(
        HTMLResponse(
            reader_lite_html(
                request,
                book_id=book_id,
                chapter_index=chapter,
                page_index=page,
                selected_sentence=selected,
                auto_skip=bool(auto),
                notice=notice,
            )
        ),
        request,
    )


@app.get("/reader-lite/toc", response_class=HTMLResponse)
def reader_lite_toc_page(request: FastAPIRequest, book_id: str) -> HTMLResponse:
    return apply_lite_ui_cookie(HTMLResponse(reader_lite_toc_html(request, book_id=book_id)), request)


@app.get("/reader-lite/position")
def reader_lite_position(
    request: FastAPIRequest,
    book_id: str,
    chapter: int = 0,
    page: int = 0,
    total_pages: int = 1,
    first_sentence: str = "",
) -> Response:
    payload = lite_chapter_payload(book_id, chapter)
    lite_upsert_reading_position(
        book_id,
        payload["chapter"],
        int(payload["chapter_index"]),
        page_index=page,
        total_pages=total_pages,
        first_sentence_index=first_sentence,
    )
    return Response(status_code=204)


def reader_lite_redirect(book_id: str, chapter: int, page: int, notice: str, selected: Optional[str] = None) -> RedirectResponse:
    query = lite_query(book_id=book_id, chapter=chapter, page=page, selected=selected, auto=0, notice=notice)
    suffix = f"#s{selected}" if selected else ""
    url = f"/reader-lite?{query}{suffix}"
    return RedirectResponse(url=url, status_code=303)


def lite_parse_content_disposition(value: str) -> dict[str, str]:
    output: dict[str, str] = {}
    for piece in str(value or "").split(";"):
        piece = piece.strip()
        if "=" not in piece:
            continue
        key, raw = piece.split("=", 1)
        output[key.strip().lower()] = raw.strip().strip('"')
    return output


def lite_parse_multipart(body: bytes, content_type: str) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    match = re.search(r'boundary="?([^";]+)"?', content_type or "", flags=re.IGNORECASE)
    if not match:
        raise HTTPException(status_code=422, detail="multipart boundary missing")
    boundary = ("--" + match.group(1)).encode("utf-8")
    fields: dict[str, str] = {}
    files: dict[str, dict[str, Any]] = {}
    for raw_part in body.split(boundary):
        part = raw_part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if part.endswith(b"--"):
            part = part[:-2].rstrip(b"\r\n")
        if b"\r\n\r\n" not in part:
            continue
        header_blob, data = part.split(b"\r\n\r\n", 1)
        headers: dict[str, str] = {}
        for line in header_blob.decode("utf-8", errors="ignore").split("\r\n"):
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
        disposition = lite_parse_content_disposition(headers.get("content-disposition", ""))
        name = disposition.get("name", "")
        if not name:
            continue
        filename = disposition.get("filename", "")
        if filename:
            if data.endswith(b"\r\n"):
                data = data[:-2]
            files[name] = {
                "filename": filename,
                "content_type": headers.get("content-type", "application/octet-stream"),
                "data": data,
            }
        else:
            fields[name] = data.decode("utf-8", errors="replace").strip()
    return fields, files


async def lite_request_form(request: FastAPIRequest) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    content_type = str(request.headers.get("content-type") or "")
    body = await request.body()
    if content_type.lower().startswith("application/x-www-form-urlencoded"):
        parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
        return {key: values[-1] if values else "" for key, values in parsed.items()}, {}
    if content_type.lower().startswith("multipart/form-data"):
        return lite_parse_multipart(body, content_type)
    raise HTTPException(status_code=415, detail="unsupported lite form content type")


def lite_required(fields: dict[str, str], name: str) -> str:
    value = str(fields.get(name) or "")
    if not value and name in {"book_id", "sentence_index", "chapter_locator"}:
        raise HTTPException(status_code=422, detail=f"{name} is required")
    return value


def lite_int(fields: dict[str, str], name: str, default: int = 0) -> int:
    try:
        return int(str(fields.get(name) or default))
    except ValueError:
        return default


def reader_lite_annotation_payload(
    *,
    book_id: str,
    sentence_index: str,
    chapter_locator: str,
    chapter_title: str,
    source_text: str,
    kind: str,
    note_text: Optional[str] = None,
    color: Optional[str] = None,
) -> AnnotationCreate:
    return AnnotationCreate(
        book_id=book_id,
        kind=kind,
        source_text=source_text,
        note_text=note_text,
        color=color,
        chapter_title=chapter_title,
        chapter_locator=chapter_locator,
        range_locator={"chapterLocator": chapter_locator, "sentenceIndex": sentence_index},
        metadata={"source": "ClickLiteReader", "sentenceIndex": sentence_index},
    )


@app.post("/reader-lite/red")
async def reader_lite_red(request: FastAPIRequest) -> RedirectResponse:
    fields, _files = await lite_request_form(request)
    book_id = lite_required(fields, "book_id")
    chapter = lite_int(fields, "chapter")
    page = lite_int(fields, "page")
    sentence_index = lite_required(fields, "sentence_index")
    chapter_locator = lite_required(fields, "chapter_locator")
    chapter_title = str(fields.get("chapter_title") or "")
    source_text = str(fields.get("source_text") or "")
    existing = lite_annotations_by_sentence(book_id, chapter_locator).get(sentence_index, {}).get("red")
    if existing:
        delete_annotation(str(existing.get("id")))
        return reader_lite_redirect(book_id, chapter, page, "已取消红标")
    create_annotation(
        reader_lite_annotation_payload(
            book_id=book_id,
            sentence_index=sentence_index,
            chapter_locator=chapter_locator,
            chapter_title=chapter_title,
            source_text=source_text,
            kind="red_highlight",
            color="red",
        )
    )
    return reader_lite_redirect(book_id, chapter, page, "已标红")


@app.post("/reader-lite/note")
async def reader_lite_note(request: FastAPIRequest) -> RedirectResponse:
    fields, _files = await lite_request_form(request)
    book_id = lite_required(fields, "book_id")
    chapter = lite_int(fields, "chapter")
    page = lite_int(fields, "page")
    sentence_index = lite_required(fields, "sentence_index")
    selected = str(fields.get("selected") or sentence_index)
    chapter_locator = lite_required(fields, "chapter_locator")
    chapter_title = str(fields.get("chapter_title") or "")
    source_text = str(fields.get("source_text") or "")
    note_text = str(fields.get("note_text") or "")
    note = normalize_note_text(note_text)
    existing = lite_annotations_by_sentence(book_id, chapter_locator).get(sentence_index, {}).get("note")
    if existing:
        patch_annotation(str(existing.get("id")), AnnotationPatch(note_text=note))
    elif note:
        create_annotation(
            reader_lite_annotation_payload(
                book_id=book_id,
                sentence_index=sentence_index,
                chapter_locator=chapter_locator,
                chapter_title=chapter_title,
                source_text=source_text,
                kind="note",
                note_text=note,
            )
        )
    return reader_lite_redirect(book_id, chapter, page, "备注已保存", selected)


@app.post("/reader-lite/audio-note")
async def reader_lite_audio_note(request: FastAPIRequest) -> RedirectResponse:
    fields, files = await lite_request_form(request)
    book_id = lite_required(fields, "book_id")
    chapter = lite_int(fields, "chapter")
    page = lite_int(fields, "page")
    sentence_index = lite_required(fields, "sentence_index")
    selected = str(fields.get("selected") or sentence_index)
    chapter_locator = lite_required(fields, "chapter_locator")
    chapter_title = str(fields.get("chapter_title") or "")
    source_text = str(fields.get("source_text") or "")
    upload = files.get("audio_file") or {}
    book_with_latest_file(book_id)
    audio_data = upload.get("data") or b""
    if not audio_data:
        raise HTTPException(status_code=422, detail="audio file is empty")
    if len(audio_data) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="audio note is too large")
    filename = str(upload.get("filename") or "")
    mime_type = str(upload.get("content_type") or "") or mimetypes.guess_type(filename)[0] or "audio/m4a"
    annotations = lite_annotations_by_sentence(book_id, chapter_locator)
    note_annotation = annotations.get(sentence_index, {}).get("note")
    if not note_annotation:
        note_annotation = create_annotation(
            reader_lite_annotation_payload(
                book_id=book_id,
                sentence_index=sentence_index,
                chapter_locator=chapter_locator,
                chapter_title=chapter_title,
                source_text=source_text,
                kind="note",
                note_text=VOICE_NOTE_PENDING_TEXT,
            )
        )
    audio_hash = hashlib.sha256(audio_data).hexdigest()
    file_id = new_id("liteaud")
    audio_dir = sentence_reader_app_support_dir() / "AudioNotes" / "Lite"
    audio_dir.mkdir(parents=True, exist_ok=True)
    audio_path = audio_dir / f"{file_id}{lan_audio_extension(mime_type)}"
    audio_path.write_bytes(audio_data)
    audio_note = create_audio_note(
        AudioNoteCreate(
            book_id=book_id,
            annotation_id=str(note_annotation.get("id")),
            audio_path=str(audio_path),
            audio_hash=audio_hash,
            provider="mac_voice_pipeline",
            raw_result=lan_audio_raw_result(mime_type=mime_type, audio_byte_count=len(audio_data), async_processing=True),
            status="pending",
        )
    )
    start_lan_audio_note_transcription(str(audio_note.get("id")), audio_path, mime_type, len(audio_data))
    return reader_lite_redirect(book_id, chapter, page, "语音备注已保存，Mac 正在后台转写", selected)


@app.get("/vocab", response_class=HTMLResponse)
def vocabulary_page() -> HTMLResponse:
    return HTMLResponse(vocabulary_page_html())


@app.get("/lifestudy/vocab/review", response_class=HTMLResponse)
def lifestudy_vocab_review_page() -> HTMLResponse:
    return HTMLResponse(lifestudy_vocab_review_page_html())


@app.get("/api/lifestudy/vocab/review")
def get_lifestudy_vocab_review() -> dict[str, Any]:
    return lifestudy_review_api_payload()


@app.post("/api/lifestudy/vocab/review/decision")
def post_lifestudy_vocab_review_decision(payload: LifeStudyVocabReviewDecision) -> dict[str, Any]:
    return update_lifestudy_review_decision(payload)


@app.post("/api/lifestudy/vocab/review/dry-run")
def post_lifestudy_vocab_review_dry_run() -> dict[str, Any]:
    return dry_run_lifestudy_review_apply()


@app.get("/api/library/dashboard")
def get_library_dashboard(include_hidden: bool = False) -> dict[str, Any]:
    return library_dashboard_payload(include_hidden=include_hidden)


@app.get("/api/library/books/{book_id}/cover")
def get_library_book_cover(book_id: str) -> Response:
    book = book_with_latest_file(book_id)
    path = Path(str(book.get("file_path") or "")).expanduser()
    if path.exists() and path.suffix.lower() == ".epub":
        asset = epub_cover_asset(path)
        if asset:
            with zipfile.ZipFile(path) as epub:
                try:
                    data = epub.read(asset["href"])
                    media_type = asset.get("media_type") or mimetypes.guess_type(asset["href"])[0] or "image/jpeg"
                    return Response(content=data, media_type=media_type)
                except KeyError:
                    pass
    if path.exists() and path.suffix.lower() == ".pdf":
        cached = ensure_pdf_cover_cache(book, path)
        if cached:
            return FileResponse(cached, media_type="image/png")
    return Response(content=generated_cover_svg(book), media_type="image/svg+xml")


@app.post("/api/library/import")
def post_library_import(payload: LibraryImport) -> dict[str, Any]:
    return import_library_book(payload)


@app.patch("/api/library/books/{book_id}/metadata")
def patch_library_book_metadata(book_id: str, payload: LibraryMetadataPatch) -> dict[str, Any]:
    return update_library_book_metadata(book_id, payload)


@app.post("/api/library/books/batch-hide")
def batch_hide_library_books(payload: LibraryBatchHide) -> dict[str, Any]:
    return hide_library_books(payload.book_ids, source="library_web_batch_hide")


@app.post("/api/library/books/batch-restore")
def batch_restore_library_books(payload: LibraryBatchHide) -> dict[str, Any]:
    return restore_library_books(payload.book_ids, source="library_web_batch_restore")


@app.post("/api/library/books/{book_id}/hide")
def hide_library_book(book_id: str) -> dict[str, Any]:
    result = hide_library_books([book_id], source="library_web_hide")
    return {**result, "book_id": book_id, "library_state": result["library_states"][0]}


@app.post("/api/library/books/{book_id}/restore")
def restore_library_book(book_id: str) -> dict[str, Any]:
    result = restore_library_books([book_id], source="library_web_restore")
    return {**result, "book_id": book_id, "library_state": result["library_states"][0]}


@app.patch("/api/library/books/{book_id}/organization")
def patch_library_book_organization(book_id: str, payload: LibraryOrganizationPatch) -> dict[str, Any]:
    return update_library_book_organization(book_id, payload)


@app.post("/api/library/books/batch-organization")
def patch_library_books_organization(payload: LibraryBatchOrganizationPatch) -> dict[str, Any]:
    return update_library_books_organization(payload)


@app.post("/api/library/books/{book_id}/reveal")
def reveal_library_book(book_id: str) -> dict[str, Any]:
    book = book_with_latest_file(book_id)
    file_path = str(book.get("file_path") or "")
    if not file_path:
        raise HTTPException(status_code=404, detail="book has no file path to reveal")
    path = Path(file_path).expanduser()
    if not path.exists():
        raise HTTPException(status_code=404, detail="book file missing")
    try:
        subprocess.Popen(["open", "-R", str(path)])  # noqa: S603,S607 - local Mac Finder integration.
    except Exception as exc:  # noqa: BLE001 - present a clean local integration error.
        raise HTTPException(status_code=500, detail=f"Finder reveal failed: {exc}") from exc
    return {
        "ok": True,
        "schema": "sentence_reader.library_reveal.v1",
        "book_id": book_id,
        "file_path": str(path),
    }


@app.get("/lan/reader", response_class=HTMLResponse)
def lan_reader_page() -> HTMLResponse:
    return HTMLResponse(lan_reader_html())


@app.get("/lan/books")
def lan_books() -> list[dict[str, Any]]:
    books = list_books()
    output: list[dict[str, Any]] = []
    for book in books:
        file_path = str(book.get("file_path") or "")
        is_epub = bool(file_path) and (book.get("source_kind") == "epub" or file_path.lower().endswith(".epub"))
        output.append(
            {
                "id": book.get("id"),
                "title": book.get("title"),
                "author": book.get("author"),
                "source_kind": book.get("source_kind"),
                "book_hash": book.get("book_hash"),
                "lan_available": is_epub and Path(file_path).expanduser().exists(),
                "lan_reader_url": f"/lan/reader?book_id={book.get('id')}",
                "file_kind": book.get("file_kind"),
                "byte_size": book.get("byte_size"),
                "last_opened_at": book.get("last_opened_at"),
            }
        )
    return output


@app.get("/lan/books/{book_id}/manifest")
def lan_book_manifest(book_id: str) -> dict[str, Any]:
    book = book_with_latest_file(book_id)
    source_kind = str(book.get("source_kind") or book.get("file_kind") or "").lower()
    if source_kind != "epub":
        raise HTTPException(
            status_code=422,
            detail="LAN web reader currently supports EPUB only; open PDF in Click for Mac",
        )
    publication = epub_publication(epub_path_for_book(book), book=book)
    with db.connect() as conn:
        position = conn.execute("SELECT * FROM reader.reading_positions WHERE book_id = %s", (book_id,)).fetchone()
    return {
        "ok": True,
        "schema": "sentence_reader.lan_manifest.v1",
        "book": jsonable(book),
        "publication": publication,
        "chapters": publication["chapters"],
        "toc": publication.get("toc", []),
        "position": jsonable(dict(position)) if position else None,
        "reader": {
            "page_url": "/lan/reader",
            "trusted_lan_only": True,
            "external_public_access": False,
        },
    }


@app.get("/lan/books/{book_id}/chapters/{chapter_index}")
def lan_book_chapter(book_id: str, chapter_index: int) -> dict[str, Any]:
    book = book_with_latest_file(book_id)
    epub_path = epub_path_for_book(book)
    publication = epub_publication(epub_path, book=book)
    chapters = publication["chapters"]
    if chapter_index < 0 or chapter_index >= len(chapters):
        raise HTTPException(status_code=404, detail="chapter not found")
    chapter = chapters[chapter_index]
    with zipfile.ZipFile(epub_path) as epub:
        try:
            raw_html = zip_text(epub, chapter["href"])
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="chapter asset missing") from exc
    return {
        "ok": True,
        "schema": "sentence_reader.lan_chapter.v1",
        "book_id": book_id,
        "chapter": chapter,
        "index": chapter_index,
        "locator": chapter["locator"],
        "title": chapter.get("title"),
        "html": transform_epub_html_assets(book_id, chapter["href"], raw_html),
    }


@app.get("/lan/books/{book_id}/asset/{asset_path:path}")
def lan_book_asset(book_id: str, asset_path: str) -> Response:
    book = book_with_latest_file(book_id)
    epub_path = epub_path_for_book(book)
    safe_path = safe_epub_member(asset_path)
    with zipfile.ZipFile(epub_path) as epub:
        try:
            data = epub.read(safe_path)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="asset not found") from exc
    media_type = mimetypes.guess_type(safe_path)[0] or "application/octet-stream"
    return Response(content=data, media_type=media_type)


def android_sync_cursor(conn: Any) -> str:
    rows = conn.execute(
        """
        SELECT max(updated_at) AS value FROM reader.books
        UNION ALL
        SELECT max(updated_at) AS value FROM reader.library_state
        UNION ALL
        SELECT max(updated_at) AS value FROM reader.reading_positions
        UNION ALL
        SELECT max(updated_at) AS value FROM reader.annotations
        UNION ALL
        SELECT max(updated_at) AS value FROM reader.audio_notes
        """
    ).fetchall()
    values = [row.get("value") for row in rows if row and row.get("value")]
    try:
        for path in knowledge_base_root_path().glob("**/_status/analysis.json"):
            analysis = read_living_book_json(path, {})
            updated_at = parse_living_book_time(str(analysis.get("updated_at") or ""))
            if updated_at:
                values.append(updated_at)
    except OSError:
        pass
    return jsonable(max(values)) if values else now_iso()


def android_visible_books_and_removed_ids(conn: Any) -> tuple[list[dict[str, Any]], list[str]]:
    visible_rows = conn.execute(
        """
        SELECT b.*,
               ls.metadata AS library_metadata
        FROM reader.books b
        LEFT JOIN reader.library_state ls ON ls.book_id = b.id
        WHERE COALESCE(ls.hidden, false) = false
          AND lower(COALESCE(b.source_kind, '')) IN ('epub', 'pdf')
        ORDER BY b.last_opened_at DESC NULLS LAST, b.created_at DESC
        """
    ).fetchall()
    removed_rows = conn.execute(
        """
        SELECT ls.book_id
        FROM reader.library_state ls
        JOIN reader.books b ON b.id = ls.book_id
        WHERE ls.hidden = true
          AND lower(COALESCE(b.source_kind, '')) IN ('epub', 'pdf')
        ORDER BY ls.updated_at DESC
        """
    ).fetchall()
    visible_books: list[dict[str, Any]] = []
    for row in visible_rows:
        try:
            visible_books.append(resolve_android_book_source(str(row["id"]), conn=conn))
        except HTTPException as exc:
            if exc.status_code not in {404, 409}:
                raise
            visible_books.append({**dict(row), "_android_source_error": str(exc.detail)})
    return visible_books, [str(row["book_id"]) for row in removed_rows]


def android_book_organization(book: dict[str, Any]) -> dict[str, Any]:
    raw_metadata = book.get("library_metadata")
    metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
    custom_category = re.sub(r"\s+", " ", str(metadata.get("custom_category") or "")).strip()
    tags = [str(value).strip() for value in (metadata.get("tags") or []) if str(value).strip()]
    return {
        "favorite": bool(metadata.get("favorite") or False),
        "custom_category": custom_category,
        "category": custom_category or "未分类",
        "tags": tags,
    }


def android_safe_book_payload(book: dict[str, Any]) -> dict[str, Any]:
    safe_keys = (
        "id",
        "title",
        "author",
        "source_kind",
        "book_hash",
        "created_at",
        "updated_at",
        "last_opened_at",
        "file_kind",
        "file_hash",
        "byte_size",
    )
    return {key: jsonable(book.get(key)) for key in safe_keys if key in book}


def android_source_contract(book: dict[str, Any]) -> dict[str, Any]:
    resolved = (
        book
        if isinstance(book.get("_android_source_snapshot"), dict)
        else resolve_android_book_source(str(book.get("id") or ""))
    )
    snapshot = dict(resolved["_android_source_snapshot"])
    return {
        "kind": snapshot["file_kind"],
        "url": f"/v1/android/books/{snapshot['book_id']}/source",
        "file_hash": snapshot["file_hash"],
        "byte_size": snapshot["byte_size"],
        "persistent_original": True,
        "cached_by_android": False,
    }


def android_source_projection_fields(book: dict[str, Any]) -> dict[str, Any]:
    source = android_source_contract(book)
    return {
        "source_kind": source["kind"],
        "source_url": source["url"],
        "source_hash": source["file_hash"],
        "source_byte_size": source["byte_size"],
    }


def android_publication_payload(book: dict[str, Any], *, include_chapters: bool) -> dict[str, Any]:
    epub_path = epub_path_for_book(book)
    compatibility = ensure_book_epub_assets(book)
    android_epub_path = android_epub_path_for_book(book, compatibility)
    publication = epub_publication(epub_path, book=book)
    contract = android_book_contract_fields(book, compatibility)
    payload: dict[str, Any] = {
        "book": {**android_safe_book_payload(book), **contract},
        "source": android_source_contract(book),
        "publication": publication,
        "epub": {
            "url": f"/v1/android/books/{book.get('id')}/epub",
            "file_hash": file_sha256(android_epub_path),
            "source_file_hash": book.get("file_hash") or book.get("book_hash") or file_sha256(epub_path),
            "byte_size": android_epub_path.stat().st_size,
            "runtime_derivative": android_epub_path != epub_path,
            "cached_by_android": True,
        },
        "cover": {
            "url": f"/v1/android/books/{book.get('id')}/cover",
            "cached_by_android": True,
            "source": "library_cover_api",
        },
        "compatibility": {
            "schema": compatibility.get("schema") or EPUB_COMPATIBILITY_REPORT_SCHEMA,
            **contract,
            "image_only_ratio": compatibility.get("image_only_ratio", 0),
            "page_progression": compatibility.get("page_progression") or "ltr",
            "source_fixed_layout": bool(compatibility.get("source_fixed_layout")),
            "recommended_spread": compatibility.get("recommended_spread") or "never",
            "correction_queue_required": bool(compatibility.get("correction_queue_required")),
        },
        "display_variants": {
            "schema": DISPLAY_VARIANT_SCHEMA,
            "available": contract["display_variants_available"],
            "url": f"/v1/android/books/{book.get('id')}/display-variants" if contract["display_variants_available"] else "",
            "cached_by_android": True,
            "offset_contract": (compatibility.get("display_variants") or {}).get("offset_contract") or "",
        },
        "living_book_analysis": {
            "state": contract["analysis_state"],
            "updated_at": contract["analysis_updated_at"],
            "status_url": f"/books/{book.get('id')}/living-book/analysis-status",
            "analyze_url": f"/books/{book.get('id')}/living-book/analyze",
        },
        "organization": android_book_organization(book),
    }
    if include_chapters:
        chapters: list[dict[str, Any]] = []
        with zipfile.ZipFile(epub_path) as epub:
            for chapter in publication.get("chapters") or []:
                try:
                    raw_html = zip_text(epub, chapter["href"])
                except KeyError:
                    raw_html = ""
                chapters.append(
                    {
                        "index": chapter.get("index"),
                        "locator": chapter.get("locator"),
                        "title": chapter.get("title"),
                        "href": chapter.get("href"),
                        "html": transform_epub_html_assets(str(book.get("id") or ""), str(chapter.get("href") or ""), raw_html)
                        if raw_html
                        else "",
                    }
                )
        payload["chapters"] = chapters
    return payload


def android_pdf_sync_payload(book: dict[str, Any]) -> dict[str, Any]:
    contract = android_book_contract_fields(book, {})
    return {
        "book": {**android_safe_book_payload(book), **contract},
        "source": android_source_contract(book),
        "cover": {
            "url": f"/v1/android/books/{book.get('id')}/cover",
            "cached_by_android": True,
            "source": "library_cover_api",
        },
        "living_book_analysis": {
            "state": contract["analysis_state"],
            "updated_at": contract["analysis_updated_at"],
            "status_url": f"/books/{book.get('id')}/living-book/analysis-status",
            "analyze_url": f"/books/{book.get('id')}/living-book/analyze",
        },
        "organization": android_book_organization(book),
    }


def android_book_sync_payload(
    book: dict[str, Any],
    *,
    include_chapters: bool = False,
    conn: Any = None,
) -> dict[str, Any]:
    book_id = str(book.get("id") or "")
    if not isinstance(book.get("_android_source_snapshot"), dict):
        book = resolve_android_book_source(book_id, conn=conn)
    source_kind = str(book.get("source_kind") or book.get("file_kind") or "").strip().lower()
    if source_kind == "epub":
        payload = android_publication_payload(book, include_chapters=include_chapters)
    elif source_kind == "pdf":
        # PDF participates in metadata/position/annotation sync, but never enters the EPUB
        # publication/chapter builder.
        payload = android_pdf_sync_payload(book)
    else:
        raise ValueError("unsupported Android book source kind")
    def load_rows(active_conn: Any) -> tuple[Any, list[Any], list[Any], int]:
        position = active_conn.execute(
            """
            SELECT rp.*, COALESCE(rv.version, 1) AS server_version
            FROM reader.reading_positions rp
            LEFT JOIN reader.android_sync_resource_versions rv
              ON rv.resource_type='position' AND rv.resource_id=rp.book_id
            WHERE rp.book_id = %s
            """,
            (book_id,),
        ).fetchone()
        annotations = active_conn.execute(
            """
            SELECT a.*, COALESCE(rv.version, 1) AS server_version
            FROM reader.annotations a
            LEFT JOIN reader.android_sync_resource_versions rv
              ON rv.resource_type='annotation' AND rv.resource_id=a.id
            WHERE a.book_id = %s
            ORDER BY a.chapter_locator ASC, a.created_at ASC
            """,
            (book_id,),
        ).fetchall()
        audio_notes = active_conn.execute(
            """
            SELECT an.id, an.annotation_id, an.book_id, an.audio_hash, an.duration_seconds, an.provider,
                   an.transcript, an.status, an.error_message, an.created_at, an.updated_at,
                   COALESCE(rv.version, 1) AS server_version
            FROM reader.audio_notes an
            LEFT JOIN reader.android_sync_resource_versions rv
              ON rv.resource_type='audio_note' AND rv.resource_id=an.id
            WHERE an.book_id = %s
            ORDER BY an.created_at ASC
            """,
            (book_id,),
        ).fetchall()
        version_row = active_conn.execute(
            """
            SELECT COALESCE(version, 1) AS version
            FROM reader.android_sync_resource_versions
            WHERE resource_type='book' AND resource_id=%s
            """,
            (book_id,),
        ).fetchone()
        return position, list(annotations), list(audio_notes), int((version_row or {}).get("version") or 1)

    if conn is None:
        with db.connect() as owned_conn:
            position, annotations, audio_notes, book_version = load_rows(owned_conn)
    else:
        position, annotations, audio_notes, book_version = load_rows(conn)
    payload["book"]["server_version"] = book_version
    payload.update(
        {
            "position": jsonable(dict(position)) if position else None,
            "annotations": [jsonable(dict(row)) for row in annotations],
            "audio_notes": [jsonable(dict(row)) for row in audio_notes],
        }
    )
    return payload


def android_changed_rows(since: str) -> dict[str, Any]:
    with db.connect() as conn:
        cursor = android_sync_cursor(conn)
        books = conn.execute(
            """
            SELECT b.*,
                   ls.metadata AS library_metadata,
                   bf.file_path,
                   bf.file_kind,
                   bf.file_hash,
                   bf.byte_size
            FROM reader.books b
            LEFT JOIN reader.library_state ls ON ls.book_id = b.id
            LEFT JOIN LATERAL (
                SELECT file_path, file_kind, file_hash, byte_size
                FROM reader.book_files
                WHERE book_id = b.id
                ORDER BY created_at DESC
                LIMIT 1
            ) bf ON true
            WHERE COALESCE(ls.hidden, false) = false
              AND lower(COALESCE(b.source_kind, bf.file_kind, '')) IN ('epub', 'pdf')
              AND (
                b.updated_at > %s::timestamptz
                OR COALESCE(b.last_opened_at, b.created_at) > %s::timestamptz
                OR COALESCE(ls.updated_at, b.updated_at) > %s::timestamptz
              )
            ORDER BY b.updated_at DESC
            """,
            (since, since, since),
        ).fetchall()
        resolved_books: list[dict[str, Any]] = []
        for row in books:
            try:
                resolved_books.append(resolve_android_book_source(str(row["id"]), conn=conn))
            except HTTPException as exc:
                if exc.status_code not in {404, 409}:
                    raise
        books = resolved_books
        removed_books = conn.execute(
            """
            SELECT ls.book_id
            FROM reader.library_state ls
            JOIN reader.books b ON b.id = ls.book_id
            WHERE ls.hidden = true
              AND lower(COALESCE(b.source_kind, '')) IN ('epub', 'pdf')
              AND ls.updated_at > %s::timestamptz
            ORDER BY ls.updated_at ASC
            """,
            (since,),
        ).fetchall()
        positions = conn.execute(
            """
            SELECT rp.*
            FROM reader.reading_positions rp
            JOIN reader.books b ON b.id = rp.book_id
            LEFT JOIN reader.library_state ls ON ls.book_id = rp.book_id
            WHERE rp.updated_at > %s::timestamptz
              AND COALESCE(ls.hidden, false) = false
              AND lower(COALESCE(b.source_kind, '')) IN ('epub', 'pdf')
            ORDER BY rp.updated_at ASC
            """,
            (since,),
        ).fetchall()
        annotations = conn.execute(
            """
            SELECT a.*
            FROM reader.annotations a
            JOIN reader.books b ON b.id = a.book_id
            LEFT JOIN reader.library_state ls ON ls.book_id = a.book_id
            WHERE (a.updated_at > %s::timestamptz OR a.created_at > %s::timestamptz)
              AND COALESCE(ls.hidden, false) = false
              AND lower(COALESCE(b.source_kind, '')) IN ('epub', 'pdf')
            ORDER BY a.updated_at ASC, a.created_at ASC
            """,
            (since, since),
        ).fetchall()
        audio_notes = conn.execute(
            """
            SELECT an.id, an.annotation_id, an.book_id, an.audio_hash, an.duration_seconds, an.provider,
                   an.transcript, an.raw_result, an.status, an.error_message, an.created_at, an.updated_at
            FROM reader.audio_notes an
            JOIN reader.books b ON b.id = an.book_id
            LEFT JOIN reader.library_state ls ON ls.book_id = an.book_id
            WHERE (an.updated_at > %s::timestamptz OR an.created_at > %s::timestamptz)
              AND COALESCE(ls.hidden, false) = false
              AND lower(COALESCE(b.source_kind, '')) IN ('epub', 'pdf')
            ORDER BY an.updated_at ASC, an.created_at ASC
            """,
            (since, since),
        ).fetchall()
        visible_contract_books = conn.execute(
            """
            SELECT b.*,
                   ls.metadata AS library_metadata,
                   bf.file_path,
                   bf.file_kind,
                   bf.file_hash,
                   bf.byte_size
            FROM reader.books b
            LEFT JOIN reader.library_state ls ON ls.book_id = b.id
            LEFT JOIN LATERAL (
                SELECT file_path, file_kind, file_hash, byte_size
                FROM reader.book_files
                WHERE book_id = b.id
                ORDER BY created_at DESC
                LIMIT 1
            ) bf ON true
            WHERE COALESCE(ls.hidden, false) = false
              AND lower(COALESCE(b.source_kind, bf.file_kind, '')) IN ('epub', 'pdf')
            """
        ).fetchall()
    since_time = parse_living_book_time(since)
    book_contracts: list[dict[str, Any]] = []
    for row in visible_contract_books:
        book = jsonable(dict(row))
        contract = android_book_contract_fields(book)
        contract_time = parse_living_book_time(str(contract.get("analysis_updated_at") or ""))
        if contract_time and (since_time is None or contract_time > since_time):
            book_contracts.append({"book_id": book.get("id"), **contract})
    return {
        "cursor": cursor,
        "books": [
            {
                **android_safe_book_payload(dict(row)),
                **android_source_projection_fields(dict(row)),
                **android_book_contract_fields(dict(row)),
                "organization": android_book_organization(dict(row)),
            }
            for row in books
        ],
        "book_contracts": book_contracts,
        "removed_book_ids": [str(row["book_id"]) for row in removed_books],
        "positions": [jsonable(dict(row)) for row in positions],
        "annotations": [jsonable(dict(row)) for row in annotations],
        "audio_notes": [jsonable(dict(row)) for row in audio_notes],
    }


def android_event_changes(conn: Any, events: list[dict[str, Any]]) -> dict[str, Any]:
    book_resource_types = {"book", "library_state", "book_asset"}
    book_ids = {
        str(event["resource_id"])
        for event in events
        if str(event.get("resource_type") or "") in book_resource_types
    }
    position_ids = {
        str(event["resource_id"])
        for event in events
        if str(event.get("resource_type") or "") == "position"
    }
    annotation_ids = {
        str(event["resource_id"])
        for event in events
        if str(event.get("resource_type") or "") == "annotation"
    }
    audio_note_ids = {
        str(event["resource_id"])
        for event in events
        if str(event.get("resource_type") or "") == "audio_note"
    }
    books_to_refresh = {
        str(event["resource_id"])
        for event in events
        if str(event.get("resource_type") or "") in {"book", "book_asset"}
    }

    visible_books, _ = android_visible_books_and_removed_ids(conn)
    visible_by_id = {
        str(row.get("id") or ""): row
        for row in visible_books
        if isinstance(row.get("_android_source_snapshot"), dict)
    }
    changed_books: list[dict[str, Any]] = []
    removed_book_ids: list[str] = []
    for book_id in sorted(book_ids):
        book = visible_by_id.get(book_id)
        if not book:
            removed_book_ids.append(book_id)
            continue
        version = conn.execute(
            """
            SELECT COALESCE(version, 1) AS version
            FROM reader.android_sync_resource_versions
            WHERE resource_type='book' AND resource_id=%s
            """,
            (book_id,),
        ).fetchone()
        changed_books.append(
            {
                **android_safe_book_payload(book),
                **android_source_projection_fields(book),
                **android_book_contract_fields(book),
                "server_version": int((version or {}).get("version") or 1),
                "organization": android_book_organization(book),
            }
        )

    positions: list[dict[str, Any]] = []
    if position_ids:
        rows = conn.execute(
            """
            SELECT rp.*, COALESCE(rv.version, 1) AS server_version
            FROM reader.reading_positions rp
            JOIN reader.books b ON b.id=rp.book_id
            LEFT JOIN reader.library_state ls ON ls.book_id=rp.book_id
            LEFT JOIN reader.android_sync_resource_versions rv
              ON rv.resource_type='position' AND rv.resource_id=rp.book_id
            WHERE rp.book_id = ANY(%s)
              AND COALESCE(ls.hidden, false)=false
              AND lower(COALESCE(b.source_kind, '')) IN ('epub', 'pdf')
            """,
            (list(position_ids),),
        ).fetchall()
        positions = [jsonable(dict(row)) for row in rows]
    returned_position_ids = {str(row.get("book_id") or "") for row in positions}

    annotations: list[dict[str, Any]] = []
    if annotation_ids:
        rows = conn.execute(
            """
            SELECT a.*, COALESCE(rv.version, 1) AS server_version
            FROM reader.annotations a
            JOIN reader.books b ON b.id=a.book_id
            LEFT JOIN reader.library_state ls ON ls.book_id=a.book_id
            LEFT JOIN reader.android_sync_resource_versions rv
              ON rv.resource_type='annotation' AND rv.resource_id=a.id
            WHERE a.id = ANY(%s)
              AND COALESCE(ls.hidden, false)=false
              AND lower(COALESCE(b.source_kind, '')) IN ('epub', 'pdf')
            """,
            (list(annotation_ids),),
        ).fetchall()
        annotations = [jsonable(dict(row)) for row in rows]
    returned_annotation_ids = {str(row.get("id") or "") for row in annotations}

    audio_notes: list[dict[str, Any]] = []
    if audio_note_ids:
        rows = conn.execute(
            """
            SELECT an.id, an.annotation_id, an.book_id, an.audio_hash, an.duration_seconds,
                   an.provider, an.transcript, an.status, an.error_message,
                   an.created_at, an.updated_at, COALESCE(rv.version, 1) AS server_version
            FROM reader.audio_notes an
            JOIN reader.books b ON b.id=an.book_id
            LEFT JOIN reader.library_state ls ON ls.book_id=an.book_id
            LEFT JOIN reader.android_sync_resource_versions rv
              ON rv.resource_type='audio_note' AND rv.resource_id=an.id
            WHERE an.id = ANY(%s)
              AND COALESCE(ls.hidden, false)=false
              AND lower(COALESCE(b.source_kind, '')) IN ('epub', 'pdf')
            """,
            (list(audio_note_ids),),
        ).fetchall()
        audio_notes = [jsonable(dict(row)) for row in rows]
    returned_audio_note_ids = {str(row.get("id") or "") for row in audio_notes}

    return {
        "books": changed_books,
        "books_to_refresh": sorted(book_id for book_id in books_to_refresh if book_id in visible_by_id),
        "book_contracts": [],
        "removed_book_ids": removed_book_ids,
        "positions": positions,
        "removed_position_book_ids": sorted(position_ids - returned_position_ids),
        "annotations": annotations,
        "removed_annotation_ids": sorted(annotation_ids - returned_annotation_ids),
        "audio_notes": audio_notes,
        "removed_audio_note_ids": sorted(audio_note_ids - returned_audio_note_ids),
    }


def android_operation_metadata(operation: AndroidSyncOperation) -> dict[str, Any]:
    return {
        "source": "android_readium_native_reader",
        "operation_id": operation.operation_id,
        "device_id": operation.device_id or "",
        "base_server_version": operation.base_server_version or "",
        "client_created_at": operation.created_at or "",
        "client_updated_at": operation.updated_at or "",
    }


def android_operation_dict(operation: AndroidSyncOperation) -> dict[str, Any]:
    if hasattr(operation, "model_dump"):
        return operation.model_dump()
    return operation.dict()


def android_operation_base_version(operation: AndroidSyncOperation) -> Optional[int]:
    raw = str(operation.base_server_version or (operation.payload or {}).get("base_server_version") or "").strip()
    if not raw or not re.fullmatch(r"\d+", raw):
        return None
    return int(raw)


def android_annotation_conflict(
    operation: AndroidSyncOperation,
    *,
    current: Optional[dict[str, Any]],
    version_state: Optional[dict[str, Any]],
    reason: str,
) -> tuple[dict[str, Any], str, int]:
    current_version = int((version_state or {}).get("version") or 0)
    result = {
        "ok": False,
        "operation_id": operation.operation_id,
        "operation_type": operation.operation_type,
        "conflict": True,
        "retryable": False,
        "error": reason,
        "base_server_version": operation.base_server_version or "",
        "server_version": current_version,
        "server_deleted": bool((version_state or {}).get("deleted")),
        "server_record": jsonable(current) if current else None,
        "client_payload": jsonable(dict(operation.payload or {})),
    }
    return result, "conflict", 409


def apply_android_sync_operation_v2(
    conn: Any,
    operation: AndroidSyncOperation,
    default_device_id: str,
) -> tuple[dict[str, Any], str, int]:
    payload = dict(operation.payload or {})
    operation.device_id = operation.device_id or default_device_id
    op_type = operation.operation_type.strip().lower()
    if not operation.operation_id:
        return {
            "ok": False,
            "operation_id": "",
            "operation_type": op_type,
            "retryable": False,
            "error": "operation_id is required",
        }, "permanent_failed", 422

    conn.execute("SELECT set_config('click.android_device_id', %s, true)", (default_device_id,))
    conn.execute("SELECT set_config('click.android_operation_id', %s, true)", (operation.operation_id,))

    if op_type == "reading_position_updated":
        book_id = str(operation.book_id or payload.get("book_id") or "").strip()
        chapter_locator = str(payload.get("chapter_locator") or "").strip()
        if not book_id or not chapter_locator:
            return {
                "ok": False,
                "operation_id": operation.operation_id,
                "operation_type": op_type,
                "retryable": False,
                "error": "book_id and chapter_locator are required",
            }, "permanent_failed", 422
        client_updated_at = parse_living_book_time(str(operation.updated_at or operation.created_at or ""))
        checkpoint = conn.execute(
            """
            INSERT INTO reader.android_reading_position_checkpoints (
              book_id, device_id, chapter_id, chapter_locator, page_index, total_pages,
              page_ratio, locator, client_updated_at, operation_id, server_updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (book_id, device_id) DO UPDATE
            SET chapter_id=EXCLUDED.chapter_id,
                chapter_locator=EXCLUDED.chapter_locator,
                page_index=EXCLUDED.page_index,
                total_pages=EXCLUDED.total_pages,
                page_ratio=EXCLUDED.page_ratio,
                locator=EXCLUDED.locator,
                client_updated_at=EXCLUDED.client_updated_at,
                operation_id=EXCLUDED.operation_id,
                server_updated_at=now()
            RETURNING *
            """,
            (
                book_id,
                default_device_id,
                payload.get("chapter_id"),
                chapter_locator,
                int(payload.get("page_index") or 0),
                max(1, int(payload.get("total_pages") or 1)),
                float(payload.get("page_ratio") or 0),
                db.jsonb(dict(payload.get("locator") or {})),
                client_updated_at,
                operation.operation_id,
            ),
        ).fetchone()
        position = conn.execute(
            """
            INSERT INTO reader.reading_positions (
              book_id, chapter_id, chapter_locator, page_index, total_pages, page_ratio, locator, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (book_id) DO UPDATE
            SET chapter_id=EXCLUDED.chapter_id,
                chapter_locator=EXCLUDED.chapter_locator,
                page_index=EXCLUDED.page_index,
                total_pages=EXCLUDED.total_pages,
                page_ratio=EXCLUDED.page_ratio,
                locator=EXCLUDED.locator,
                updated_at=now()
            RETURNING *
            """,
            (
                book_id,
                payload.get("chapter_id"),
                chapter_locator,
                int(payload.get("page_index") or 0),
                max(1, int(payload.get("total_pages") or 1)),
                float(payload.get("page_ratio") or 0),
                db.jsonb(dict(payload.get("locator") or {})),
            ),
        ).fetchone()
        version = android_resource_version(conn, "position", book_id) or {}
        result = {
            **jsonable(dict(position)),
            "device_id": default_device_id,
            "device_checkpoint_updated_at": jsonable(checkpoint["server_updated_at"]),
            "server_version": int(version.get("version") or 1),
        }
        return {
            "ok": True,
            "operation_id": operation.operation_id,
            "operation_type": op_type,
            "result": result,
        }, "applied", 200

    if op_type in {"annotation_created", "red_highlight_created", "note_created"}:
        book_id = str(operation.book_id or payload.get("book_id") or "").strip()
        chapter_locator = str(payload.get("chapter_locator") or "").strip()
        if not book_id or not chapter_locator:
            return {
                "ok": False,
                "operation_id": operation.operation_id,
                "operation_type": op_type,
                "retryable": False,
                "error": "book_id and chapter_locator are required",
            }, "permanent_failed", 422
        metadata = dict(payload.get("metadata") or {})
        metadata.update(android_operation_metadata(operation))
        kind = str(payload.get("kind") or ("red_highlight" if op_type == "red_highlight_created" else "note"))
        annotation_id = new_id("ann")
        row = conn.execute(
            """
            INSERT INTO reader.annotations (
              id, book_id, sentence_id, kind, source_text, note_text, color,
              chapter_title, chapter_locator, range_locator, metadata, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())
            RETURNING *
            """,
            (
                annotation_id,
                book_id,
                payload.get("sentence_id"),
                kind,
                str(payload.get("source_text") or ""),
                payload.get("note_text"),
                payload.get("color") or ("red" if kind == "red_highlight" else None),
                payload.get("chapter_title"),
                chapter_locator,
                db.jsonb(dict(payload.get("range_locator") or {})),
                db.jsonb(metadata),
            ),
        ).fetchone()
        enqueue_living_book_evidence_sync(
            conn,
            source_kind="annotation",
            source_id=annotation_id,
            book_id=book_id,
            operation="annotation_created",
            details={"kind": kind},
        )
        version = android_resource_version(conn, "annotation", annotation_id) or {}
        result = {**jsonable(dict(row)), "server_version": int(version.get("version") or 1)}
        return {
            "ok": True,
            "operation_id": operation.operation_id,
            "operation_type": op_type,
            "result": result,
        }, "applied", 200

    if op_type in {"annotation_updated", "annotation_deleted"}:
        annotation_id = str(payload.get("annotation_id") or "").strip()
        if not annotation_id:
            return {
                "ok": False,
                "operation_id": operation.operation_id,
                "operation_type": op_type,
                "retryable": False,
                "error": "annotation_id is required",
            }, "permanent_failed", 422
        current_row = conn.execute(
            "SELECT * FROM reader.annotations WHERE id=%s FOR UPDATE",
            (annotation_id,),
        ).fetchone()
        current = dict(current_row) if current_row else None
        version_state = android_resource_version(conn, "annotation", annotation_id)
        if op_type == "annotation_deleted" and not current and version_state and version_state.get("deleted"):
            return {
                "ok": True,
                "operation_id": operation.operation_id,
                "operation_type": op_type,
                "already_deleted": True,
                "result": {
                    "id": annotation_id,
                    "deleted": True,
                    "server_version": int(version_state.get("version") or 1),
                },
            }, "applied", 200
        if not current:
            return android_annotation_conflict(
                operation,
                current=None,
                version_state=version_state,
                reason="annotation was deleted or does not exist; client content was preserved in this conflict receipt",
            )
        current_version = int((version_state or {}).get("version") or 1)
        base_version = android_operation_base_version(operation)
        if base_version is None:
            return android_annotation_conflict(
                operation,
                current=current,
                version_state=version_state or {"version": current_version, "deleted": False},
                reason="base_server_version is required for annotation updates and deletes",
            )
        if base_version != current_version:
            return android_annotation_conflict(
                operation,
                current=current,
                version_state=version_state or {"version": current_version, "deleted": False},
                reason="annotation changed on another device; no content was overwritten",
            )
        if op_type == "annotation_updated":
            metadata_patch = payload.get("metadata")
            if isinstance(metadata_patch, dict):
                metadata_patch = {**metadata_patch, **android_operation_metadata(operation)}
            row = conn.execute(
                """
                UPDATE reader.annotations
                SET note_text=COALESCE(%s, note_text),
                    color=COALESCE(%s, color),
                    metadata=COALESCE(%s, metadata),
                    updated_at=now()
                WHERE id=%s
                RETURNING *
                """,
                (
                    payload.get("note_text"),
                    payload.get("color"),
                    db.jsonb(metadata_patch) if isinstance(metadata_patch, dict) else None,
                    annotation_id,
                ),
            ).fetchone()
            enqueue_living_book_evidence_sync(
                conn,
                source_kind="annotation",
                source_id=annotation_id,
                book_id=str(row["book_id"]),
                operation="annotation_updated",
            )
            version = android_resource_version(conn, "annotation", annotation_id) or {}
            result = {**jsonable(dict(row)), "server_version": int(version.get("version") or current_version + 1)}
        else:
            row = conn.execute(
                "DELETE FROM reader.annotations WHERE id=%s RETURNING id, book_id, kind, chapter_locator",
                (annotation_id,),
            ).fetchone()
            enqueue_living_book_evidence_sync(
                conn,
                source_kind="annotation",
                source_id=annotation_id,
                book_id=str(row["book_id"]),
                operation="annotation_deleted",
                details={"kind": row.get("kind"), "chapter_locator": row.get("chapter_locator"), "tombstone": True},
            )
            version = android_resource_version(conn, "annotation", annotation_id) or {}
            result = {
                "id": annotation_id,
                "deleted": True,
                "server_version": int(version.get("version") or current_version + 1),
            }
        return {
            "ok": True,
            "operation_id": operation.operation_id,
            "operation_type": op_type,
            "result": result,
        }, "applied", 200

    if op_type == "book_organization_updated":
        book_id = str(operation.book_id or payload.get("book_id") or "").strip()
        exists = conn.execute("SELECT 1 AS value FROM reader.books WHERE id=%s", (book_id,)).fetchone()
        if not book_id or not exists:
            return {
                "ok": False,
                "operation_id": operation.operation_id,
                "operation_type": op_type,
                "retryable": False,
                "error": "book does not exist",
            }, "permanent_failed", 404
        current = conn.execute(
            "SELECT metadata FROM reader.library_state WHERE book_id=%s",
            (book_id,),
        ).fetchone()
        metadata = dict((current or {}).get("metadata") or {})
        if "favorite" in payload:
            metadata["favorite"] = bool(payload.get("favorite"))
        if "custom_category" in payload:
            category = re.sub(r"\s+", " ", str(payload.get("custom_category") or "")).strip()
            if category:
                metadata["custom_category"] = category[:48]
            else:
                metadata.pop("custom_category", None)
        if "tags" in payload:
            tags = list(dict.fromkeys(str(tag or "").strip()[:32] for tag in payload.get("tags") or [] if str(tag or "").strip()))
            if tags:
                metadata["tags"] = tags[:12]
            else:
                metadata.pop("tags", None)
        row = conn.execute(
            """
            INSERT INTO reader.library_state (book_id, hidden, source, metadata, created_at, updated_at)
            VALUES (%s, false, 'android_readium_organization', %s, now(), now())
            ON CONFLICT (book_id) DO UPDATE
            SET metadata=EXCLUDED.metadata,
                source=EXCLUDED.source,
                updated_at=now()
            RETURNING *
            """,
            (book_id, db.jsonb(metadata)),
        ).fetchone()
        version = android_resource_version(conn, "library_state", book_id) or {}
        return {
            "ok": True,
            "operation_id": operation.operation_id,
            "operation_type": op_type,
            "result": {
                "book_id": book_id,
                "organization": android_book_organization({"library_metadata": metadata}),
                "server_version": int(version.get("version") or 1),
                "updated_at": jsonable(row["updated_at"]),
            },
        }, "applied", 200

    if op_type in {"audio_note_created", "living_book_analysis_requested"}:
        # These paths include a durable audio file or an asynchronous Mac job,
        # so they retain their existing reconciliation logic. The operation
        # receipt still prevents normal retries from duplicating the outcome.
        result = apply_android_sync_operation(operation, default_device_id)
        if op_type == "audio_note_created" and isinstance(result.get("result"), dict):
            audio = result["result"].get("audio_note")
            if isinstance(audio, dict):
                result["result"]["audio_note"] = {
                    key: jsonable(audio.get(key))
                    for key in (
                        "id",
                        "annotation_id",
                        "book_id",
                        "audio_hash",
                        "duration_seconds",
                        "provider",
                        "transcript",
                        "status",
                        "error_message",
                        "created_at",
                        "updated_at",
                    )
                    if key in audio
                }
        return result, "applied" if result.get("ok") else "permanent_failed", 200 if result.get("ok") else 422

    return {
        "ok": False,
        "operation_id": operation.operation_id,
        "operation_type": op_type,
        "retryable": False,
        "error": f"unsupported operation_type: {operation.operation_type}",
    }, "permanent_failed", 422


def apply_android_sync_operation(operation: AndroidSyncOperation, default_device_id: str) -> dict[str, Any]:
    payload = dict(operation.payload or {})
    operation.device_id = operation.device_id or default_device_id
    op_type = operation.operation_type.strip().lower()
    if not operation.operation_id:
        raise HTTPException(status_code=400, detail="operation_id is required")
    with db.connect() as conn:
        duplicate = conn.execute(
            """
            SELECT id, kind
            FROM reader.annotations
            WHERE metadata->>'operation_id' = %s
            LIMIT 1
            """,
            (operation.operation_id,),
        ).fetchone()
    if duplicate and op_type in {"annotation_created", "red_highlight_created", "note_created"}:
        return {"ok": True, "duplicate": True, "operation_id": operation.operation_id, "result": jsonable(dict(duplicate))}

    if op_type == "living_book_analysis_requested":
        book_id = str(operation.book_id or payload.get("book_id") or "").strip()
        if not book_id:
            raise HTTPException(status_code=400, detail="book_id is required for living_book_analysis_requested")
        result = queue_living_book_analysis(
            book_id,
            requested_by=f"android:{operation.device_id or default_device_id or 'unknown'}",
            force=bool(payload.get("force")),
            start_async=True,
        )
        return {"ok": True, "operation_id": operation.operation_id, "result": jsonable(result)}

    if op_type == "book_organization_updated":
        book_id = str(operation.book_id or payload.get("book_id") or "").strip()
        if not book_id:
            raise HTTPException(status_code=400, detail="book_id is required for book_organization_updated")
        result = update_library_book_organization(
            book_id,
            LibraryOrganizationPatch(
                favorite=payload.get("favorite") if "favorite" in payload else None,
                custom_category=payload.get("custom_category") if "custom_category" in payload else None,
                tags=payload.get("tags") if "tags" in payload else None,
            ),
            source="android_readium_organization",
        )
        return {"ok": True, "operation_id": operation.operation_id, "result": jsonable(result)}

    if op_type == "reading_position_updated":
        book_id = str(operation.book_id or payload.get("book_id") or "").strip()
        if not book_id:
            raise HTTPException(status_code=400, detail="book_id is required for reading_position_updated")
        result = upsert_position(
            book_id,
            PositionUpsert(
                chapter_id=payload.get("chapter_id"),
                chapter_locator=str(payload.get("chapter_locator") or ""),
                page_index=int(payload.get("page_index") or 0),
                total_pages=max(1, int(payload.get("total_pages") or 1)),
                page_ratio=float(payload.get("page_ratio") or 0),
                locator=dict(payload.get("locator") or {}),
            ),
        )
        return {"ok": True, "operation_id": operation.operation_id, "result": jsonable(result)}

    if op_type in {"annotation_created", "red_highlight_created", "note_created"}:
        book_id = str(operation.book_id or payload.get("book_id") or "").strip()
        if not book_id:
            raise HTTPException(status_code=400, detail="book_id is required for annotation operation")
        metadata = dict(payload.get("metadata") or {})
        metadata.update(android_operation_metadata(operation))
        kind = str(payload.get("kind") or ("red_highlight" if op_type == "red_highlight_created" else "note"))
        result = create_annotation(
            AnnotationCreate(
                book_id=book_id,
                sentence_id=payload.get("sentence_id"),
                kind=kind,
                source_text=str(payload.get("source_text") or ""),
                note_text=payload.get("note_text"),
                color=payload.get("color") or ("red" if kind == "red_highlight" else None),
                chapter_title=payload.get("chapter_title"),
                chapter_locator=str(payload.get("chapter_locator") or ""),
                range_locator=dict(payload.get("range_locator") or {}),
                metadata=metadata,
            ),
        )
        return {"ok": True, "operation_id": operation.operation_id, "result": jsonable(result)}

    if op_type == "annotation_updated":
        annotation_id = str(payload.get("annotation_id") or "").strip()
        if not annotation_id:
            raise HTTPException(status_code=400, detail="annotation_id is required")
        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            metadata = {**metadata, **android_operation_metadata(operation)}
        result = patch_annotation(
            annotation_id,
            AnnotationPatch(note_text=payload.get("note_text"), color=payload.get("color"), metadata=metadata),
        )
        return {"ok": True, "operation_id": operation.operation_id, "result": jsonable(result)}

    if op_type == "annotation_deleted":
        annotation_id = str(payload.get("annotation_id") or "").strip()
        if not annotation_id:
            raise HTTPException(status_code=400, detail="annotation_id is required")
        result = delete_annotation(annotation_id)
        return {"ok": True, "operation_id": operation.operation_id, "result": jsonable(result)}

    if op_type == "audio_note_created":
        book_id = str(operation.book_id or payload.get("book_id") or "").strip()
        audio_base64 = str(payload.get("audio_base64") or "")
        if not book_id or not audio_base64:
            raise HTTPException(status_code=400, detail="book_id and audio_base64 are required")
        annotation_id = str(duplicate.get("id") or "") if duplicate else ""
        if not annotation_id:
            metadata = dict(payload.get("metadata") or {})
            metadata.update(android_operation_metadata(operation))
            annotation = create_annotation(
                AnnotationCreate(
                    book_id=book_id,
                    kind="note",
                    source_text=str(payload.get("source_text") or ""),
                    note_text=VOICE_NOTE_PENDING_TEXT,
                    chapter_title=payload.get("chapter_title"),
                    chapter_locator=str(payload.get("chapter_locator") or ""),
                    range_locator=dict(payload.get("range_locator") or {}),
                    metadata=metadata,
                )
            )
            annotation_id = str(annotation.get("id") or "")

        with db.connect() as conn:
            existing_audio = conn.execute(
                "SELECT * FROM reader.audio_notes WHERE annotation_id = %s ORDER BY created_at DESC LIMIT 1",
                (annotation_id,),
            ).fetchone()
        if existing_audio:
            audio_result = jsonable(dict(existing_audio))
        else:
            audio_result = lan_audio_note_transcribe(
                LANAudioTranscribe(
                    book_id=book_id,
                    annotation_id=annotation_id,
                    audio_base64=audio_base64,
                    mime_type=str(payload.get("mime_type") or "audio/wav"),
                    duration_seconds=payload.get("duration_seconds"),
                )
            )
        with db.connect() as conn:
            annotation = conn.execute("SELECT * FROM reader.annotations WHERE id = %s", (annotation_id,)).fetchone()
        result = jsonable(dict(annotation)) if annotation else {"id": annotation_id, "kind": "note"}
        result["audio_note"] = audio_result
        return {"ok": True, "operation_id": operation.operation_id, "result": result}

    return {"ok": False, "operation_id": operation.operation_id, "error": f"unsupported operation_type: {operation.operation_type}"}


@app.get("/v1/android/readium/health")
def android_readium_health(request: FastAPIRequest) -> dict[str, Any]:
    require_android_access(request)
    return {
        "ok": True,
        "schema": "click.android.readium.health.v1",
        "reader_api": "available",
        "readium": {
            "toolkit": "Readium Kotlin Toolkit",
            "expected_dependency": "org.readium.kotlin-toolkit",
            "version_policy": "Maven Central dependency; Android client owns runtime integration",
            "forked_legado": False,
            "forked_moon_reader": False,
        },
        "sync": {
            "source_of_truth": "Mac Reader API / PostgreSQL",
            "android_role": "local replica + offline operation queue",
            "full_sync": "/v1/android/sync/full",
            "changes": "/v1/android/sync/changes",
            "operations": "/v1/android/sync/operations",
        },
        "tts": {
            "default": "Android sherpa-onnx local voice package",
            "local_runtime": "sherpa-onnx",
            "local_model_delivery": "user-imported verified package in Android app-private storage",
            "online_optional": "Mac edge-tts",
            "online_voice": EDGE_TTS_VOICE,
            "offline_fallback": "installed Android TextToSpeech voice with network_required=false",
            "local_private_network_tts": False,
            "azure_default": False,
        },
    }


@app.get("/v1/android/app-update")
def android_app_update(
    request: FastAPIRequest,
    current_version_code: int = 0,
) -> dict[str, Any]:
    require_android_access(request)
    latest = load_android_update_manifest()
    if latest is None:
        return {
            "ok": True,
            "schema": ANDROID_UPDATE_SCHEMA,
            "available": False,
            "reason": "not_published",
        }
    available = latest["version_code"] > max(0, current_version_code)
    return {
        "ok": True,
        "schema": ANDROID_UPDATE_SCHEMA,
        "available": available,
        "version_code": latest["version_code"],
        "version_name": latest["version_name"],
        "apk_url": f"/v1/android/app-update/{latest['artifact_id']}/apk" if available else "",
        "apk_bytes": latest["apk_bytes"],
        "apk_sha256": latest["apk_sha256"],
        "certificate_sha256": latest["certificate_sha256"],
        "min_sdk": latest["min_sdk"],
        "mandatory": bool(latest.get("mandatory", False)),
        "release_notes": str(latest.get("release_notes") or "")[:2000],
        "published_at": str(latest.get("published_at") or ""),
    }


@app.get("/v1/android/app-update/{artifact_id}/apk")
def android_app_update_apk(request: FastAPIRequest, artifact_id: str) -> FileResponse:
    require_android_access(request)
    latest = load_android_update_manifest()
    if latest is None or artifact_id != latest["artifact_id"]:
        raise HTTPException(status_code=404, detail="Android update artifact not found")
    return FileResponse(
        latest["apk_path"],
        media_type="application/vnd.android.package-archive",
        filename=f"Click-{latest['version_name']}.apk",
    )


@app.put("/v1/android/imports/{requested_sha256}")
async def android_import_book(request: FastAPIRequest, requested_sha256: str) -> dict[str, Any]:
    device_id = require_android_access(request)
    upload = normalize_android_import_headers(request, requested_sha256)
    staging_path: Optional[Path] = None
    try:
        staging_path = await stage_android_book_upload(
            request,
            expected_sha256=upload["book_hash"],
            expected_byte_size=upload["byte_size"],
        )
        metadata = validate_android_book_upload(staging_path, upload["source_kind"])
        imported = canonical_import_library_file(
            staging_path,
            filename=upload["filename"],
            source_kind=upload["source_kind"],
            book_hash=upload["book_hash"],
            byte_size=upload["byte_size"],
            title=str(metadata.get("title") or Path(upload["filename"]).stem),
            author=str(metadata.get("author") or "").strip() or None,
            library_source="android_stream_import",
            library_metadata={
                "android_device_id": device_id,
                "canonical_asset_owner": (
                    "knowledge_base_living_book"
                    if upload["source_kind"] == "pdf"
                    else "sentence_reader_app_support"
                ),
            },
            update_existing_book=False,
            merge_library_metadata=True,
        )
        return {
            "ok": True,
            "book_id": imported["book"]["id"],
            "source_kind": imported["source_kind"],
            "file_hash": imported["file_hash"],
            "byte_size": imported["byte_size"],
            "duplicate": imported["duplicate"],
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - internal paths and database details must never reach Android.
        raise HTTPException(status_code=500, detail="book import failed") from exc
    finally:
        if staging_path is not None:
            staging_path.unlink(missing_ok=True)
        try:
            android_import_staging_dir().rmdir()
        except OSError:
            pass


@app.get("/v1/android/sync/manifest")
def android_sync_manifest(request: FastAPIRequest) -> dict[str, Any]:
    require_android_access(request)
    with db.connect() as conn:
        books, removed_book_ids = android_visible_books_and_removed_ids(conn)
        cursor = android_sync_cursor(conn)
        sequence = android_current_sequence(conn)
    projectable_books = [
        book
        for book in books
        if isinstance(book.get("_android_source_snapshot"), dict)
    ]
    return {
        "ok": True,
        "schema": "click.android.sync.manifest.v1",
        "generated_at": now_iso(),
        "cursor": cursor,
        "watermark_sequence": sequence,
        "source_of_truth": "mac_reader_api_postgresql",
        "android_role": "local_replica_offline_queue",
        "full_sync_url": "/v1/android/sync/full",
        "changes_url": "/v1/android/sync/changes",
        "operations_url": "/v1/android/sync/operations",
        "book_count": len(projectable_books),
        "removed_book_ids": removed_book_ids,
        "books": [
            {
                "id": book.get("id"),
                "title": book.get("title"),
                "author": book.get("author"),
                "source_kind": book.get("source_kind"),
                "book_hash": book.get("book_hash"),
                "lan_reader_url": f"/lan/reader?book_id={book.get('id')}",
                "file_kind": book.get("file_kind"),
                "byte_size": book.get("byte_size"),
                **android_source_projection_fields(book),
                "last_opened_at": book.get("last_opened_at"),
                **android_book_contract_fields(book),
            }
            for book in projectable_books
        ],
    }


@app.get("/v1/android/sync/full")
def android_sync_full(request: FastAPIRequest, include_chapters: bool = True) -> dict[str, Any]:
    require_android_access(request)
    payloads: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    with db.connect() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        books, removed_book_ids = android_visible_books_and_removed_ids(conn)
        cursor = android_sync_cursor(conn)
        watermark_sequence = android_current_sequence(conn)
        for book in books:
            file_path_raw = str(book.get("file_path") or "").strip()
            file_path = Path(file_path_raw).expanduser()
            source_kind = str(book.get("source_kind") or book.get("file_kind") or "").lower()
            contract_marker = hashlib.sha256(
                "\0".join(
                    [
                        str(book.get("id") or ""),
                        str(book.get("updated_at") or ""),
                        str(book.get("file_hash") or ""),
                        str(book.get("book_hash") or ""),
                        str(book.get("byte_size") or ""),
                        source_kind,
                    ]
                ).encode("utf-8")
            ).hexdigest()
            if source_kind not in {"epub", "pdf"}:
                skipped.append(
                    {
                        "book_id": str(book.get("id") or ""),
                        "reason": "unsupported_source_kind",
                        "source_kind": source_kind or "unknown",
                    }
                )
                continue
            if (
                not file_path_raw
                or not file_path.exists()
                or file_path.suffix.lower() != f".{source_kind}"
            ):
                errors.append(
                    {
                        "book_id": str(book.get("id") or ""),
                        "reason": "missing_source",
                        "source_kind": source_kind,
                        "error": f"{source_kind.upper()} source is unavailable",
                        "contract_marker": contract_marker,
                    }
                )
                continue
            try:
                payloads.append(android_book_sync_payload(book, include_chapters=include_chapters, conn=conn))
            except Exception as exc:  # noqa: BLE001 - one damaged book must not block sync of the rest.
                errors.append(
                    {
                        "book_id": str(book.get("id") or ""),
                        "reason": "publication_build_failed",
                        "error": f"{exc.__class__.__name__}: {exc}",
                        "contract_marker": contract_marker,
                    }
                )
    return {
        # A damaged book must not prevent a new Android device from accepting the
        # valid part of the same repeatable-read baseline. Per-book failures stay
        # explicit so the client can show a repair warning without retrying the
        # entire library forever.
        "ok": True,
        "partial_success": bool(errors),
        "error_count": len(errors),
        "schema": f"{ANDROID_SYNC_SCHEMA}.full",
        "generated_at": now_iso(),
        "cursor": cursor,
        "watermark_sequence": watermark_sequence,
        "include_chapters": include_chapters,
        "source_of_truth": "mac_reader_api_postgresql",
        "sync_deletion_semantics": "books hidden in Click library are removed from Android local cache; Mac EPUB and annotations are retained",
        "visible_book_ids": [str(book.get("id") or "") for book in books],
        "removed_book_ids": removed_book_ids,
        "android_cache_scope": [
            "books",
            "source",
            "epub",
            "pdf",
            "publication",
            "chapters",
            "annotations",
            "audio_notes",
            "positions",
            "epub_compatibility",
            "display_variants",
            "living_book_analysis_state",
        ],
        "books": payloads,
        "errors": errors,
        "skipped_books": skipped,
    }


@app.get("/v1/android/sync/changes")
def android_sync_changes(
    request: FastAPIRequest,
    after_sequence: int = 0,
    limit: int = 100,
    since: str = "",
) -> dict[str, Any]:
    require_android_access(request)
    requested_since = since.strip()
    if requested_since:
        if parse_living_book_time(requested_since) is None:
            raise HTTPException(status_code=400, detail="since must be an ISO-8601 timestamp")
        changes = android_changed_rows(requested_since)
        return {
            "ok": True,
            "schema": "click.android.sync.changes.v1",
            "generated_at": now_iso(),
            "since": requested_since,
            "deprecated_cursor": True,
            **changes,
        }
    with db.connect() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        page = android_event_page(conn, after_sequence, limit)
        changes = android_event_changes(conn, page["events"])
    return {
        "ok": True,
        "schema": f"{ANDROID_SYNC_SCHEMA}.changes",
        "generated_at": now_iso(),
        "after_sequence": page["after_sequence"],
        "next_sequence": page["next_sequence"],
        "cursor": str(page["next_sequence"]),
        "has_more": page["has_more"],
        "limit": page["limit"],
        "events": [
            {
                "sequence": event["sequence"],
                "resource_type": event["resource_type"],
                "resource_id": event["resource_id"],
                "action": event["action"],
                "version": event["version"],
                "committed_at": jsonable(event["committed_at"]),
            }
            for event in page["events"]
        ],
        **changes,
    }


@app.post("/v1/android/sync/operations")
def android_sync_operations(request: FastAPIRequest, payload: AndroidSyncOperations) -> dict[str, Any]:
    authorized_device_id = require_android_access(request)
    if payload.device_id != authorized_device_id:
        raise HTTPException(status_code=403, detail="Android payload device ID does not match credentials")
    prepared: list[tuple[AndroidSyncOperation, str, Optional[dict[str, Any]]]] = []
    batch_hashes: dict[str, str] = {}
    with db.connect() as conn:
        for operation in payload.operations:
            if not operation.operation_id:
                raise HTTPException(status_code=422, detail="operation_id is required")
            if operation.device_id and operation.device_id != authorized_device_id:
                raise HTTPException(status_code=403, detail="operation device ID does not match credentials")
            operation.device_id = authorized_device_id
            request_hash = android_operation_request_hash(
                authorized_device_id,
                android_operation_dict(operation),
            )
            previous_hash = batch_hashes.get(operation.operation_id)
            if previous_hash and previous_hash != request_hash:
                raise HTTPException(status_code=409, detail=f"operation_id reused with different payload: {operation.operation_id}")
            batch_hashes[operation.operation_id] = request_hash
            receipt = android_operation_receipt(conn, operation.operation_id)
            if receipt and receipt["request_hash"] != request_hash:
                raise HTTPException(status_code=409, detail=f"operation_id reused with different payload: {operation.operation_id}")
            prepared.append((operation, request_hash, receipt))

    results: list[dict[str, Any]] = []
    annotation_write_applied = False
    for operation, request_hash, prefetched_receipt in prepared:
        if prefetched_receipt:
            cached = dict(prefetched_receipt.get("result") or {})
            cached["duplicate"] = True
            cached["receipt_status"] = prefetched_receipt["status"]
            cached["receipt_http_status"] = prefetched_receipt["http_status"]
            results.append(cached)
            continue
        try:
            with db.connect() as conn:
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)",
                    (f"reader.android.operation:{operation.operation_id}",),
                )
                receipt = android_operation_receipt(conn, operation.operation_id)
                if receipt:
                    if receipt["request_hash"] != request_hash:
                        raise HTTPException(
                            status_code=409,
                            detail=f"operation_id reused with different payload: {operation.operation_id}",
                        )
                    result = dict(receipt.get("result") or {})
                    result["duplicate"] = True
                    result["receipt_status"] = receipt["status"]
                    result["receipt_http_status"] = receipt["http_status"]
                else:
                    result, receipt_status, receipt_http_status = apply_android_sync_operation_v2(
                        conn,
                        operation,
                        authorized_device_id,
                    )
                    store_android_operation_receipt(
                        conn,
                        operation_id=operation.operation_id,
                        device_id=authorized_device_id,
                        request_hash=request_hash,
                        status=receipt_status,
                        http_status=receipt_http_status,
                        result=result,
                    )
                    result["receipt_status"] = receipt_status
                    result["receipt_http_status"] = receipt_http_status
                    annotation_write_applied = annotation_write_applied or (
                        bool(result.get("ok")) and operation.operation_type.strip().lower().startswith("annotation")
                    )
                results.append(result)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - operation-level conflicts/errors must be reported per item.
            results.append(
                {
                    "ok": False,
                    "operation_id": operation.operation_id,
                    "operation_type": operation.operation_type,
                    "retryable": True,
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )
    if annotation_write_applied:
        start_living_book_evidence_sync_worker()
    with db.connect() as conn:
        next_sequence = android_current_sequence(conn)
    return {
        "ok": all(item.get("ok") for item in results),
        "schema": f"{ANDROID_SYNC_SCHEMA}.operations_result",
        "device_id": authorized_device_id,
        "received": len(payload.operations),
        "applied": sum(1 for item in results if item.get("ok")),
        "failed": sum(1 for item in results if not item.get("ok")),
        "results": results,
        "next_sequence": next_sequence,
        "cursor": str(next_sequence),
    }


@app.get("/v1/android/books/{book_id}/epub")
def android_book_epub(request: FastAPIRequest, book_id: str) -> FileResponse:
    require_android_access(request)
    book = resolve_android_book_source(book_id)
    compatibility = ensure_book_epub_assets(book)
    epub_path = android_epub_path_for_book(book, compatibility)
    if not epub_path.exists():
        raise HTTPException(status_code=404, detail="EPUB file missing")
    filename = f"{safe_slug(str(book.get('title') or book_id))}.epub"
    return FileResponse(
        epub_path,
        media_type="application/epub+zip",
        filename=filename,
    )


def stream_android_book_source(handle: Any) -> Any:
    try:
        while True:
            chunk = handle.read(ANDROID_BOOK_UPLOAD_CHUNK_BYTES)
            if not chunk:
                break
            yield chunk
    finally:
        handle.close()


@app.get("/v1/android/books/{book_id}/source")
def android_book_source(request: FastAPIRequest, book_id: str) -> StreamingResponse:
    require_android_access(request)
    book = resolve_android_book_source(book_id, keep_open=True)
    snapshot = dict(book["_android_source_snapshot"])
    source_kind = str(snapshot["file_kind"])
    source_hash = str(snapshot["file_hash"])
    byte_size = int(snapshot["byte_size"])
    handle = book.pop("_android_source_handle")
    filename = f"{safe_slug(str(book.get('title') or book_id))}.{source_kind}"
    return StreamingResponse(
        stream_android_book_source(handle),
        media_type="application/epub+zip" if source_kind == "epub" else "application/pdf",
        headers={
            "Content-Length": str(byte_size),
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "X-Click-Byte-Size": str(byte_size),
            "X-Click-File-SHA256": source_hash,
        },
    )


@app.get("/v1/android/books/{book_id}/display-variants")
def android_book_display_variants(request: FastAPIRequest, book_id: str) -> FileResponse:
    require_android_access(request)
    book = resolve_android_book_source(book_id)
    report = ensure_book_epub_assets(book)
    path = Path(str(report.get("display_variants_path") or "")).expanduser()
    if not path.exists() or not (report.get("display_variants") or {}).get("available"):
        raise HTTPException(status_code=404, detail="display variants are unavailable")
    return FileResponse(path, media_type="application/json", filename=f"{book_id}-display-variants.json")


@app.get("/v1/android/books/{book_id}/compatibility")
def android_book_compatibility(request: FastAPIRequest, book_id: str) -> dict[str, Any]:
    require_android_access(request)
    book = resolve_android_book_source(book_id)
    report = dict(ensure_book_epub_assets(book))
    for private_key in ("source_file", "runtime_epub_path", "display_variants_path"):
        report.pop(private_key, None)
    return {"ok": report.get("status") != "error", **report}


@app.get("/v1/android/books/{book_id}/publication")
def android_book_publication(request: FastAPIRequest, book_id: str, include_chapters: bool = False) -> dict[str, Any]:
    require_android_access(request)
    book = resolve_android_book_source(book_id)
    return {
        "ok": True,
        "schema": "click.android.book.publication.v1",
        **android_book_sync_payload(book, include_chapters=include_chapters),
    }


def mobile_tts_response(
    request: FastAPIRequest,
    payload: AndroidTTSCreate,
    *,
    audio_prefix: str,
) -> dict[str, Any]:
    require_android_access(request)
    result = post_lookup_tts(LookupTTSCreate(text=payload.text, voice=payload.voice or EDGE_TTS_VOICE))
    raw_audio_url = str(result.get("audio_url") or "")
    audio_id = Path(raw_audio_url).stem if raw_audio_url else ""
    audio_url = f"{audio_prefix}/{audio_id}/audio" if audio_id else ""
    return {
        "ok": bool(result.get("ok")),
        "schema": "click.android.tts.v1",
        "engine": result.get("engine") or "browser_speech_synthesis",
        "voice": result.get("voice") or payload.voice or EDGE_TTS_VOICE,
        "kind": payload.kind,
        "book_id": payload.book_id,
        "locator": payload.locator,
        "audio_url": audio_url,
        "android_offline_fallback": "TextToSpeech",
        "azure_default": False,
        "text": result.get("text") or payload.text,
        "error": result.get("error") or "",
    }


@app.post("/v1/mobile/tts")
def mobile_tts(request: FastAPIRequest, payload: AndroidTTSCreate) -> dict[str, Any]:
    return mobile_tts_response(
        request,
        payload,
        audio_prefix="/v1/mobile/tts",
    )


@app.post("/v1/android/tts")
def android_tts(request: FastAPIRequest, payload: AndroidTTSCreate) -> dict[str, Any]:
    return mobile_tts_response(
        request,
        payload,
        audio_prefix="/v1/android/tts",
    )


@app.get("/v1/mobile/tts/{audio_id}/audio")
def mobile_tts_audio(request: FastAPIRequest, audio_id: str) -> FileResponse:
    require_android_access(request)
    return get_lookup_tts(audio_id)


@app.get("/v1/android/tts/{audio_id}/audio")
def android_tts_audio(request: FastAPIRequest, audio_id: str) -> FileResponse:
    require_android_access(request)
    return get_lookup_tts(audio_id)


@app.get("/v1/android/books/{book_id}/cover")
def android_book_cover(request: FastAPIRequest, book_id: str) -> Response:
    require_android_access(request)
    return get_library_book_cover(book_id)


@app.get("/v1/android/books/{book_id}/lookup")
def android_book_lookup(
    request: FastAPIRequest,
    book_id: str,
    word: str,
    sentence_id: Optional[str] = None,
    sentence: Optional[str] = None,
) -> dict[str, Any]:
    require_android_access(request)
    return lookup_book_word(book_id, word, sentence_id, sentence)


@app.get("/v1/android/books/{book_id}/annotations")
def android_book_annotations(request: FastAPIRequest, book_id: str) -> list[dict[str, Any]]:
    require_android_access(request)
    return list_annotations(book_id)


@app.get("/v1/android/audio-notes/{audio_note_id}/audio")
def android_audio_note_audio(request: FastAPIRequest, audio_note_id: str) -> FileResponse:
    require_android_access(request)
    return get_audio_note_audio(audio_note_id)


@app.post("/v1/android/audio-notes/transcribe")
def android_audio_note_transcribe(
    request: FastAPIRequest,
    payload: LANAudioTranscribe,
) -> dict[str, Any]:
    require_android_access(request)
    return lan_audio_note_transcribe(payload)


@app.post("/lan/audio-notes/transcribe")
def lan_audio_note_transcribe(payload: LANAudioTranscribe) -> dict[str, Any]:
    book_with_latest_file(payload.book_id)
    audio_data = decode_audio_base64(payload.audio_base64)
    audio_note_id = new_id("aud")
    audio_hash = hashlib.sha256(audio_data).hexdigest()
    audio_dir = sentence_reader_app_support_dir() / "AudioNotes" / "LAN"
    audio_dir.mkdir(parents=True, exist_ok=True)
    audio_path = audio_dir / f"{audio_note_id}{lan_audio_extension(payload.mime_type)}"
    audio_path.write_bytes(audio_data)

    provider = "mac_voice_pipeline"
    raw_result = lan_audio_raw_result(mime_type=payload.mime_type, audio_byte_count=len(audio_data), async_processing=True)
    status_value = "pending"

    with db.connect() as conn:
        audio_row = conn.execute(
            """
            INSERT INTO reader.audio_notes (
                id, annotation_id, book_id, audio_path, audio_hash, duration_seconds,
                provider, transcript, raw_result, status, error_message, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())
            RETURNING *
            """,
            (
                audio_note_id,
                payload.annotation_id,
                payload.book_id,
                str(audio_path),
                audio_hash,
                payload.duration_seconds,
                provider,
                None,
                db.jsonb(raw_result),
                status_value,
                None,
            ),
        ).fetchone()
        if audio_row:
            apply_audio_note_to_annotation(conn, dict(audio_row))
            enqueue_living_book_evidence_sync(
                conn,
                source_kind="audio_note",
                source_id=audio_note_id,
                book_id=payload.book_id,
                operation="audio_note_created",
                details={"status": status_value, "capture_path": "lan_audio_note"},
            )
    if audio_row:
        start_living_book_evidence_sync_worker()
        sync_reader_audio_note_to_voice_inbox(audio_note_id)
    start_lan_audio_note_transcription(audio_note_id, audio_path, payload.mime_type, len(audio_data))
    return {
        "ok": True,
        "accepted": True,
        "schema": "sentence_reader.lan_audio_transcription.v1",
        "audio_note_id": audio_note_id,
        "annotation_id": payload.annotation_id,
        "status": status_value,
        "provider": provider,
        "voice_pipeline": {
            "schema": MAC_VOICE_PIPELINE_SCHEMA,
            "pipeline": MAC_VOICE_PIPELINE_ID,
            "mac_side_processing": True,
            "app_role": "capture_upload_only",
            "purpose": "reader_lan_audio_note",
        },
        "transcript": "",
        "audio_hash": audio_hash,
        "error_message": "",
        "async_processing": True,
        "pending_text": VOICE_NOTE_PENDING_TEXT,
    }


@app.post("/books")
def create_book(request: FastAPIRequest, payload: BookCreate) -> dict[str, Any]:
    require_mobile_admin_access(request)
    source_kind = str(payload.source_kind or "").strip().lower()
    source_path: Optional[Path] = None
    stored_source_path: Optional[str] = None
    source_hash = payload.file_hash
    source_byte_size = payload.byte_size
    if payload.file_path:
        stored_source_path = str(Path(payload.file_path).expanduser())
        source_path = click_owned_internal_book_source_path(payload.file_path)
        if source_path is None:
            raise HTTPException(status_code=422, detail="book file must be a Click-owned internal copy")
        if source_kind in {"epub", "pdf"} and source_path.suffix.lower() != f".{source_kind}":
            raise HTTPException(status_code=422, detail="book file extension does not match source kind")
        actual_hash = file_sha256(source_path)
        actual_byte_size = source_path.stat().st_size
        declared_hash = str(payload.file_hash or "").strip().lower()
        if declared_hash and (
            not re.fullmatch(r"[0-9a-f]{64}", declared_hash)
            or declared_hash != actual_hash
        ):
            raise HTTPException(status_code=422, detail="book file sha256 does not match its source")
        declared_book_hash = str(payload.book_hash or "").strip().lower()
        if (
            re.fullmatch(r"[0-9a-f]{64}", declared_book_hash)
            and declared_book_hash != actual_hash
        ):
            raise HTTPException(status_code=422, detail="book hash does not match its source")
        if payload.byte_size is not None and int(payload.byte_size) != actual_byte_size:
            raise HTTPException(status_code=422, detail="book byte size does not match its source")
        source_hash = actual_hash
        source_byte_size = actual_byte_size
    book_id = new_id("book")
    with db.connect() as conn:
        row = conn.execute(
            """
            INSERT INTO reader.books (id, title, author, source_kind, book_hash, created_at, updated_at, last_opened_at)
            VALUES (%s, %s, %s, %s, %s, now(), now(), now())
            ON CONFLICT (book_hash) DO UPDATE
            SET title = EXCLUDED.title,
                author = EXCLUDED.author,
                updated_at = now(),
                last_opened_at = now()
            RETURNING *
            """,
            (book_id, payload.title, payload.author, source_kind, payload.book_hash),
        ).fetchone()
        if source_path is not None:
            conn.execute(
                """
                INSERT INTO reader.book_files (id, book_id, file_path, file_kind, file_hash, byte_size)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (book_id, file_path) DO UPDATE
                SET file_kind = EXCLUDED.file_kind,
                    file_hash = EXCLUDED.file_hash,
                    byte_size = EXCLUDED.byte_size
                """,
                (
                    new_id("file"),
                    row["id"],
                    stored_source_path,
                    source_kind,
                    source_hash,
                    source_byte_size,
                ),
            )
    result = dict(row)
    if source_path is not None and source_kind in {"epub", "pdf"}:
        result["living_book"] = generate_living_book_bundle(str(row["id"]))
    return result


@app.get("/books")
def list_books() -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT b.*,
                   bf.file_path,
                   bf.file_kind,
                   bf.file_hash,
                   bf.byte_size
            FROM reader.books b
            LEFT JOIN LATERAL (
                SELECT file_path, file_kind, file_hash, byte_size
                FROM reader.book_files
                WHERE book_id = b.id
                ORDER BY created_at DESC
                LIMIT 1
            ) bf ON true
            ORDER BY b.last_opened_at DESC NULLS LAST, b.created_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


@app.get("/books/{book_id}")
def get_book(book_id: str) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM reader.books WHERE id = %s", (book_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="book not found")
    return dict(row)


@app.put("/books/{book_id}/position")
def upsert_position(book_id: str, payload: PositionUpsert) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute(
            """
            INSERT INTO reader.reading_positions (
                book_id, chapter_id, chapter_locator, page_index, total_pages, page_ratio, locator, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (book_id) DO UPDATE
            SET chapter_id = EXCLUDED.chapter_id,
                chapter_locator = EXCLUDED.chapter_locator,
                page_index = EXCLUDED.page_index,
                total_pages = EXCLUDED.total_pages,
                page_ratio = EXCLUDED.page_ratio,
                locator = EXCLUDED.locator,
                updated_at = now()
            RETURNING *
            """,
            (
                book_id,
                payload.chapter_id,
                payload.chapter_locator,
                payload.page_index,
                payload.total_pages,
                payload.page_ratio,
                db.jsonb(payload.locator),
            ),
        ).fetchone()
    return dict(row)


@app.get("/books/{book_id}/position")
def get_position(book_id: str) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM reader.reading_positions WHERE book_id = %s", (book_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="position not found")
    return dict(row)


@app.post("/sentences")
def upsert_sentence(payload: SentenceUpsert) -> dict[str, Any]:
    sentence_id = new_id("sent")
    with db.connect() as conn:
        row = conn.execute(
            """
            INSERT INTO reader.sentences (
                id, book_id, chapter_id, chapter_locator, sentence_index,
                sentence_text_hash, text, range_locator
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (book_id, chapter_locator, sentence_index, sentence_text_hash) DO UPDATE
            SET text = EXCLUDED.text,
                range_locator = EXCLUDED.range_locator
            RETURNING *
            """,
            (
                sentence_id,
                payload.book_id,
                payload.chapter_id,
                payload.chapter_locator,
                payload.sentence_index,
                payload.sentence_text_hash,
                payload.text,
                db.jsonb(payload.range_locator),
            ),
        ).fetchone()
    return dict(row)


@app.post("/annotations")
def create_annotation(payload: AnnotationCreate) -> dict[str, Any]:
    annotation_id = new_id("ann")
    with db.connect() as conn:
        row = conn.execute(
            """
            INSERT INTO reader.annotations (
                id, book_id, sentence_id, kind, source_text, note_text, color,
                chapter_title, chapter_locator, range_locator, metadata, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())
            RETURNING *
            """,
            (
                annotation_id,
                payload.book_id,
                payload.sentence_id,
                payload.kind,
                payload.source_text,
                payload.note_text,
                payload.color,
                payload.chapter_title,
                payload.chapter_locator,
                db.jsonb(payload.range_locator),
                db.jsonb(payload.metadata),
            ),
        ).fetchone()
        enqueue_living_book_evidence_sync(
            conn,
            source_kind="annotation",
            source_id=annotation_id,
            book_id=payload.book_id,
            operation="annotation_created",
            details={"kind": payload.kind},
        )
    start_living_book_evidence_sync_worker()
    return dict(row)


@app.get("/books/{book_id}/annotations")
def list_annotations(book_id: str) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM reader.annotations
            WHERE book_id = %s
            ORDER BY chapter_locator ASC, created_at ASC
            """,
            (book_id,),
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/books/{book_id}/vocab/build")
def post_book_vocab_build(book_id: str, payload: VocabBuildRequest = Body(default_factory=VocabBuildRequest)) -> dict[str, Any]:
    return build_book_vocabulary(book_id, payload)


@app.get("/books/{book_id}/vocab")
def get_book_vocab(
    book_id: str,
    status: Optional[str] = None,
    alignment_status: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 300,
) -> dict[str, Any]:
    return list_book_vocabulary(
        book_id,
        status=status,
        alignment_status=alignment_status,
        query=query,
        limit=limit,
    )


@app.get("/books/{book_id}/lookup")
def get_book_lookup(
    book_id: str,
    word: str,
    sentence_id: Optional[str] = None,
    sentence: Optional[str] = None,
) -> dict[str, Any]:
    return lookup_book_word(book_id, word, sentence_id, sentence)


@app.get("/books/{book_id}/glossary")
def get_book_glossary(book_id: str) -> dict[str, Any]:
    return list_book_glossary(book_id)


@app.get("/books/{book_id}/glossary/export.csv")
def get_book_glossary_export(book_id: str) -> Response:
    return export_book_glossary_csv(book_id)


@app.patch("/books/{book_id}/vocab/{item_id}")
def patch_book_vocab_item(book_id: str, item_id: str, payload: VocabPatch) -> dict[str, Any]:
    book_with_latest_file(book_id)
    updates: list[str] = []
    params: list[Any] = []
    meaning_patch = payload.context_meaning_zh is not None
    clean_meaning = str(payload.context_meaning_zh or "").strip() if meaning_patch else ""
    if payload.status is not None:
        updates.append("status = %s")
        params.append(payload.status)
    if payload.context_meaning_zh is not None:
        updates.append("context_meaning = %s")
        updates.append("meaning_source = %s")
        params.extend([clean_meaning or None, "user_glossary" if clean_meaning else "none"])
        if payload.alignment_status is None and clean_meaning:
            updates.append("alignment_status = 'confirmed_context_meaning'")
        if payload.alignment_reason is None and clean_meaning:
            updates.append("alignment_reason = %s")
            params.append("用户修正的本句义，优先于自动抽取结果。")
    if payload.alignment_status is not None:
        updates.append("alignment_status = %s")
        params.append(payload.alignment_status)
    if payload.alignment_reason is not None:
        updates.append("alignment_reason = %s")
        params.append(payload.alignment_reason)
    if payload.user_note is not None:
        updates.append("user_note = %s")
        params.append(payload.user_note)
    with db.connect() as conn:
        current = conn.execute(
            "SELECT * FROM reader.book_vocab_items WHERE book_id = %s AND id = %s",
            (book_id, item_id),
        ).fetchone()
        if not current:
            raise HTTPException(status_code=404, detail="vocab item not found")
        if updates:
            params.extend([book_id, item_id])
            row = conn.execute(
                f"""
                UPDATE reader.book_vocab_items
                SET {', '.join(updates)}, updated_at = now()
                WHERE book_id = %s AND id = %s
                RETURNING *
                """,
                tuple(params),
            ).fetchone()
        else:
            row = current
        if meaning_patch:
            row_dict = dict(row or current)
            term = normalize_glossary_term(row_dict.get("surface"))
            if term and clean_meaning:
                conn.execute(
                    """
                    INSERT INTO reader.book_glossary (
                        id, book_id, term, meaning_zh, source, confidence, created_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, 'user', 1, now(), now())
                    ON CONFLICT (book_id, term) DO UPDATE
                    SET meaning_zh = EXCLUDED.meaning_zh,
                        source = 'user',
                        confidence = 1,
                        updated_at = now()
                    """,
                    (new_id("gloss"), book_id, term, clean_meaning),
                )
                conn.execute(
                    """
                    UPDATE reader.book_vocab_items
                    SET context_meaning = %s,
                        meaning_source = 'user_glossary',
                        alignment_status = 'confirmed_context_meaning',
                        alignment_reason = '用户修正的本句义，优先于自动抽取结果。',
                        updated_at = now()
                    WHERE book_id = %s AND lower(surface) = %s
                    """,
                    (clean_meaning, book_id, term),
                )
            elif term:
                conn.execute(
                    "DELETE FROM reader.book_glossary WHERE book_id = %s AND lower(term) = %s",
                    (book_id, term),
                )
            conn.execute(
                """
                INSERT INTO reader.lookup_events (
                    id, book_id, sentence_id, surface, lemma, event_kind, context, created_at
                )
                VALUES (%s, %s, NULL, %s, %s, 'edit_meaning', %s, now())
                """,
                (
                    new_id("lookup"),
                    book_id,
                    row_dict.get("surface") or "",
                    row_dict.get("lemma"),
                    db.jsonb(
                        {
                            "source": "vocab_patch",
                            "item_id": item_id,
                            "old_meaning": current.get("context_meaning"),
                            "new_meaning": clean_meaning,
                            "glossary_term": term,
                        }
                    ),
                ),
            )
        return selected_vocab_row(conn, book_id, item_id)


@app.post("/books/{book_id}/lookup-corrections")
def post_lookup_correction(book_id: str, payload: LookupCorrectionCreate) -> dict[str, Any]:
    book_with_latest_file(book_id)
    clean_meaning = str(payload.meaning_zh or "").strip()
    if not clean_meaning:
        raise HTTPException(status_code=400, detail="meaning_zh is required")
    clean_word = clean_vocab_word(payload.word)
    lookup_terms = vocab_lookup_terms(payload.word)
    if clean_word and clean_word not in lookup_terms:
        lookup_terms.append(clean_word)
    if not clean_word and not lookup_terms:
        raise HTTPException(status_code=400, detail="word is required")
    with db.connect() as conn:
        current = conn.execute(
            """
            SELECT *
            FROM reader.book_vocab_items
            WHERE book_id = %s AND (
              lower(surface) = ANY(%s::text[])
              OR lower(coalesce(lemma, '')) = ANY(%s::text[])
              OR regexp_replace(lower(surface), '[^a-z]', '', 'g') = %s
              OR regexp_replace(lower(coalesce(lemma, '')), '[^a-z]', '', 'g') = %s
            )
            ORDER BY
              CASE meaning_source WHEN 'user_glossary' THEN 0 ELSE 1 END,
              score DESC,
              occurrence_count DESC
            LIMIT 1
            """,
            (book_id, lookup_terms, lookup_terms, clean_word, clean_word),
        ).fetchone()
        if current:
            item_id = str(current.get("id"))
        else:
            item_id = ensure_manual_correction_vocab_item(
                conn,
                book_id,
                clean_word or payload.word,
                clean_meaning,
                payload.sentence,
            )
        term = normalize_glossary_term(clean_word or payload.word)
        if term:
            conn.execute(
                """
                INSERT INTO reader.book_glossary (
                    id, book_id, term, meaning_zh, source, confidence, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, 'user', 1, now(), now())
                ON CONFLICT (book_id, term) DO UPDATE
                SET meaning_zh = EXCLUDED.meaning_zh,
                    source = 'user',
                    confidence = 1,
                    updated_at = now()
                """,
                (new_id("gloss"), book_id, term, clean_meaning),
            )
        conn.execute(
            """
            UPDATE reader.book_vocab_items
            SET context_meaning = %s,
                meaning_source = 'user_glossary',
                alignment_status = 'confirmed_context_meaning',
                alignment_reason = '用户手动填写的释义，优先于在线查询和自动抽取。',
                metadata = COALESCE(metadata, '{}'::jsonb) || %s,
                updated_at = now()
            WHERE book_id = %s AND (
              id = %s
              OR lower(surface) = ANY(%s::text[])
              OR lower(coalesce(lemma, '')) = ANY(%s::text[])
              OR regexp_replace(lower(surface), '[^a-z]', '', 'g') = %s
              OR regexp_replace(lower(coalesce(lemma, '')), '[^a-z]', '', 'g') = %s
            )
            """,
            (
                clean_meaning,
                db.jsonb(
                    {
                        "source": "manual_lookup_correction",
                        "popup_speak_text_zh": lookup_popup_speak_text_zh(clean_word or payload.word, "", clean_meaning),
                        "corrected_at": datetime.now(timezone.utc).isoformat(),
                    }
                ),
                book_id,
                item_id,
                lookup_terms,
                lookup_terms,
                clean_word,
                clean_word,
            ),
        )
        conn.execute(
            """
            INSERT INTO reader.lookup_events (
                id, book_id, sentence_id, surface, lemma, event_kind, context, created_at
            )
            VALUES (%s, %s, %s, %s, %s, 'edit_meaning', %s, now())
            """,
            (
                new_id("lookup"),
                book_id,
                safe_lookup_sentence_id(conn, payload.sentence_id),
                payload.word,
                clean_word,
                db.jsonb(
                    {
                        "source": "lookup_correction",
                        "new_meaning": clean_meaning,
                        "sentence": payload.sentence or "",
                        "glossary_term": term,
                    }
                ),
            ),
        )
        return selected_vocab_row(conn, book_id, item_id)


@app.post("/books/{book_id}/vocab/{item_id}/review")
def post_book_vocab_review(book_id: str, item_id: str, payload: VocabReviewCreate) -> dict[str, Any]:
    return review_book_vocabulary_item(book_id, item_id, payload)


@app.post("/books/{book_id}/lookup-events")
def post_lookup_event(book_id: str, payload: LookupEventCreate) -> dict[str, Any]:
    book_with_latest_file(book_id)
    event_id = new_id("lookup")
    with db.connect() as conn:
        sentence_id = safe_lookup_sentence_id(conn, payload.sentence_id)
        row = conn.execute(
            """
            INSERT INTO reader.lookup_events (
                id, book_id, sentence_id, surface, lemma, event_kind, context, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            RETURNING *
            """,
            (
                event_id,
                book_id,
                sentence_id,
                payload.surface,
                payload.lemma,
                payload.event_kind,
                db.jsonb(payload.context),
            ),
        ).fetchone()
    return jsonable(dict(row))


@app.post("/lookup/tts")
def post_lookup_tts(payload: LookupTTSCreate) -> dict[str, Any]:
    text = clean_lookup_tts_text(payload.text)
    if not text:
        raise HTTPException(status_code=422, detail="text is required")
    voice = clean_lookup_tts_text(payload.voice or EDGE_TTS_VOICE) or EDGE_TTS_VOICE
    command = edge_tts_path()
    if not command:
        return {
            "ok": False,
            "engine": "browser_speech_synthesis",
            "voice": voice,
            "audio_url": "",
            "text": text,
        }
    audio_id = stable_id("lookup_tts", voice, text)
    audio_path = lookup_tts_dir() / f"{audio_id}.mp3"
    generated, detail = synthesize_lookup_tts_low_priority(command, text, voice, audio_path)
    if not generated:
        return {
            "ok": False,
            "engine": "browser_speech_synthesis",
            "voice": voice,
            "audio_url": "",
            "text": text,
            "error": detail,
        }
    return {
        "ok": True,
        "engine": "edge-tts",
        "voice": voice,
        "audio_url": f"/lookup/tts/{audio_id}.mp3",
        "text": text,
    }


@app.post("/lookup/tts/status")
def post_lookup_tts_status(payload: LookupTTSCreate) -> dict[str, Any]:
    text = clean_lookup_tts_text(payload.text)
    if not text:
        raise HTTPException(status_code=422, detail="text is required")
    voice = clean_lookup_tts_text(payload.voice or EDGE_TTS_VOICE) or EDGE_TTS_VOICE
    audio_id = stable_id("lookup_tts", voice, text)
    audio_path = lookup_tts_dir() / f"{audio_id}.mp3"
    cached = audio_path.exists()
    return {
        "ok": True,
        "engine": "edge-tts",
        "voice": voice,
        "audio_url": f"/lookup/tts/{audio_id}.mp3" if cached else "",
        "text": text,
        "cached": cached,
    }


@app.get("/lookup/tts/{audio_id}.mp3")
def get_lookup_tts(audio_id: str) -> FileResponse:
    if not re.fullmatch(r"lookup_tts_[0-9a-f]{24}", audio_id or ""):
        raise HTTPException(status_code=404, detail="audio not found")
    audio_path = lookup_tts_dir() / f"{audio_id}.mp3"
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail="audio not found")
    return FileResponse(audio_path, media_type="audio/mpeg")


@app.post("/books/{book_id}/export")
def export_book(book_id: str, payload: ExportGenerate = Body(default_factory=ExportGenerate)) -> dict[str, Any]:
    with db.connect() as conn:
        book_row = conn.execute("SELECT * FROM reader.books WHERE id = %s", (book_id,)).fetchone()
        if not book_row:
            raise HTTPException(status_code=404, detail="book not found")
        annotation_rows = conn.execute(
            """
            SELECT * FROM reader.annotations
            WHERE book_id = %s
            ORDER BY chapter_locator ASC, created_at ASC
            """,
            (book_id,),
        ).fetchall()

        book = jsonable(dict(book_row))
        annotations = annotation_export_items([dict(row) for row in annotation_rows])
        generated_at = now_iso()
        output_dir = export_output_dir(payload)
        output_dir.mkdir(parents=True, exist_ok=True)
        basename = f"{safe_slug(book.get('title') or book_id)}-{book_id}-annotations"

        markdown_path = output_dir / f"{basename}.md"
        markdown_text = render_markdown_export(book, annotations, generated_at)
        markdown_path.write_text(markdown_text, encoding="utf-8")
        exports = [
            insert_export_record(conn, book_id, "markdown", str(markdown_path), len(annotations)),
        ]

        json_path: Optional[Path] = None
        if payload.include_json:
            json_path = output_dir / f"{basename}.json"
            payload_json = {
                "schema": "sentence_reader.annotations_export.v1",
                "generated_at": generated_at,
                "book": book,
                "annotation_count": len(annotations),
                "annotations": annotations,
            }
            json_path.write_text(json.dumps(payload_json, ensure_ascii=False, indent=2), encoding="utf-8")
            exports.append(insert_export_record(conn, book_id, "json", str(json_path), len(annotations)))

    return {
        "ok": True,
        "book_id": book_id,
        "annotation_count": len(annotations),
        "markdown_path": str(markdown_path),
        "json_path": str(json_path) if json_path else None,
        "exports": exports,
    }


@app.post("/books/{book_id}/living-book/sync")
def sync_living_book(book_id: str, payload: LivingBookSyncRequest = Body(default_factory=LivingBookSyncRequest)) -> dict[str, Any]:
    return generate_living_book_bundle(book_id, knowledge_base_root=payload.knowledge_base_root)


@app.post("/books/{book_id}/living-book/reconcile-write-owner")
def reconcile_living_book_write_owner_api(
    book_id: str,
    payload: LivingBookWriteOwnerReconcileRequest,
) -> dict[str, Any]:
    return reconcile_living_book_write_owner(book_id, payload)


@app.get("/living-books/evidence-sync/status")
def get_living_book_evidence_sync_status() -> dict[str, Any]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT status, count(*) AS count
            FROM reader.sync_events
            WHERE target_system = %s
            GROUP BY status
            """,
            (LIVING_BOOK_EVIDENCE_SYNC_TARGET,),
        ).fetchall()
    counts = {str(row["status"]): int(row["count"]) for row in rows}
    return {
        "ok": True,
        "schema": "click.living_book.evidence_sync_status_response.v1",
        "target_system": LIVING_BOOK_EVIDENCE_SYNC_TARGET,
        "counts": {
            "pending": counts.get("pending", 0),
            "synced": counts.get("synced", 0),
            "failed": counts.get("failed", 0),
        },
        "worker_running": bool(
            LIVING_BOOK_EVIDENCE_SYNC_THREAD is not None and LIVING_BOOK_EVIDENCE_SYNC_THREAD.is_alive()
        ),
        "knowledge_base_root": str(default_knowledge_base_root()),
    }


@app.post("/living-books/evidence-sync/run")
def run_living_book_evidence_sync(limit: int = 100, retry_failed: bool = False) -> dict[str, Any]:
    if retry_failed:
        with db.connect() as conn:
            conn.execute(
                """
                UPDATE reader.sync_events
                SET status = 'pending', last_error = NULL, updated_at = now()
                WHERE target_system = %s AND status = 'failed'
                """,
                (LIVING_BOOK_EVIDENCE_SYNC_TARGET,),
            )
    result = process_living_book_evidence_sync_events(limit=limit)
    result["pending_count"] = pending_living_book_evidence_sync_count()
    result["retry_failed"] = retry_failed
    if result["pending_count"]:
        start_living_book_evidence_sync_worker()
    return result


@app.get("/books/{book_id}/living-book/status")
def get_living_book_status(book_id: str, knowledge_base_root: Optional[str] = None) -> dict[str, Any]:
    book = living_book_book_row(book_id)
    lb_root = living_books_root(knowledge_base_root)
    bundle_dir = living_book_bundle_dir(book, knowledge_base_root)
    return living_book_status_payload(book, bundle_dir, lb_root)


@app.get("/living-books/taxonomy")
def get_living_book_taxonomy(knowledge_base_root: Optional[str] = None) -> dict[str, Any]:
    lb_root = living_books_root(knowledge_base_root)
    ensure_living_book_control_files(lb_root)
    taxonomy_path = living_book_index_dir(lb_root) / "taxonomy.json"
    taxonomy = read_living_book_json(taxonomy_path, initial_living_book_taxonomy())
    validation = validate_living_book_taxonomy_categories(list(taxonomy.get("categories") or []))
    return {
        "ok": True,
        "schema": "click.living_books.taxonomy_response.v1",
        "taxonomy_path": str(taxonomy_path),
        "validation": validation,
        "taxonomy": taxonomy,
    }


@app.post("/living-books/taxonomy/apply-default")
def apply_default_living_book_taxonomy_api(
    payload: LivingBookTaxonomyApplyDefaultRequest,
) -> dict[str, Any]:
    return apply_default_living_book_taxonomy(payload)


@app.get("/books/{book_id}/living-book/analysis-status")
def get_living_book_analysis_status(book_id: str, knowledge_base_root: Optional[str] = None) -> dict[str, Any]:
    book = living_book_book_row(book_id)
    bundle_dir = living_book_bundle_dir(book, knowledge_base_root)
    if not (bundle_dir / "book_manifest.json").exists():
        generate_living_book_bundle(book_id, knowledge_base_root=knowledge_base_root)
    analysis = living_book_analysis_state_for_book(
        book_id,
        knowledge_base_root=knowledge_base_root,
        create=True,
    )
    return {
        "ok": True,
        "schema": "click.living_book.analysis_status_response.v1",
        "book_id": book_id,
        "analysis": analysis,
        "manual_only": True,
        "auto_analysis_enabled": False,
    }


@app.post("/books/{book_id}/living-book/analyze")
def analyze_living_book(
    book_id: str,
    payload: LivingBookAnalyzeRequest = Body(default_factory=LivingBookAnalyzeRequest),
) -> dict[str, Any]:
    return queue_living_book_analysis(
        book_id,
        knowledge_base_root=payload.knowledge_base_root,
        requested_by=payload.requested_by,
        force=payload.force,
        start_async=True,
    )


@app.post("/books/{book_id}/living-book/reanalyze")
def reanalyze_living_book(
    book_id: str,
    payload: LivingBookAnalyzeRequest = Body(default_factory=LivingBookAnalyzeRequest),
) -> dict[str, Any]:
    return queue_living_book_analysis(
        book_id,
        knowledge_base_root=payload.knowledge_base_root,
        requested_by=payload.requested_by,
        force=True,
        start_async=True,
    )


@app.post("/books/{book_id}/living-book/classify")
def classify_living_book(
    book_id: str,
    payload: LivingBookClassifyRequest = Body(default_factory=LivingBookClassifyRequest),
) -> dict[str, Any]:
    return run_living_book_classification(book_id, knowledge_base_root=payload.knowledge_base_root)


@app.patch("/books/{book_id}/living-book/classification")
def confirm_living_book_classification_api(
    book_id: str,
    payload: LivingBookClassificationConfirmRequest,
) -> dict[str, Any]:
    return confirm_living_book_classification(book_id, payload)


@app.post("/books/{book_id}/living-book/generate-drafts")
def generate_living_book_drafts(
    book_id: str,
    payload: LivingBookGenerateDraftsRequest = Body(default_factory=LivingBookGenerateDraftsRequest),
) -> dict[str, Any]:
    return run_living_book_draft_generation(
        book_id,
        knowledge_base_root=payload.knowledge_base_root,
        use_hermes_runtime=payload.use_hermes_runtime,
    )


@app.post("/living-books/jobs/run-due")
def run_living_books_due_jobs(payload: LivingBookJobsRunRequest = Body(default_factory=LivingBookJobsRunRequest)) -> dict[str, Any]:
    return run_due_living_book_jobs(
        knowledge_base_root=payload.knowledge_base_root,
        limit=payload.limit,
        force=payload.force,
    )


@app.get("/books/{book_id}/exports")
def list_exports(book_id: str) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM reader.exports
            WHERE book_id = %s
            ORDER BY created_at DESC
            """,
            (book_id,),
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/books/{book_id}/sync/hermes")
def sync_book_to_hermes(book_id: str, payload: HermesSyncGenerate = Body(default_factory=HermesSyncGenerate)) -> dict[str, Any]:
    with db.connect() as conn:
        book_row = conn.execute("SELECT * FROM reader.books WHERE id = %s", (book_id,)).fetchone()
        if not book_row:
            raise HTTPException(status_code=404, detail="book not found")
        annotation_rows = conn.execute(
            """
            SELECT * FROM reader.annotations
            WHERE book_id = %s
            ORDER BY chapter_locator ASC, created_at ASC
            """,
            (book_id,),
        ).fetchall()

        selected_ids = set(payload.annotation_ids)
        annotation_dicts = [dict(row) for row in annotation_rows]
        if selected_ids:
            annotation_dicts = [row for row in annotation_dicts if row.get("id") in selected_ids]
        if not payload.include_red_highlights:
            annotation_dicts = [row for row in annotation_dicts if row.get("kind") != "red_highlight"]

        book = jsonable(dict(book_row))
        annotations = hermes_annotation_items(annotation_dicts)
        generated_at = now_iso()
        sync_payload = build_hermes_sync_payload(book, annotations, generated_at)

        output_dir = hermes_sync_output_dir(payload)
        output_dir.mkdir(parents=True, exist_ok=True)
        basename = f"{safe_slug(book.get('title') or book_id)}-{book_id}-hermes-sync"
        payload_path = output_dir / f"{basename}.json"
        payload_path.write_text(json.dumps(sync_payload, ensure_ascii=False, indent=2), encoding="utf-8")

        sync_event = insert_sync_event(
            conn,
            "book",
            book_id,
            "hermes_cognitive_os",
            {
                "schema": sync_payload["schema"],
                "payload_path": str(payload_path),
                "annotation_count": len(annotations),
                "generated_at": generated_at,
            },
        )

    return {
        "ok": True,
        "book_id": book_id,
        "target_system": "hermes_cognitive_os",
        "status": "pending",
        "annotation_count": len(annotations),
        "payload_path": str(payload_path),
        "sync_event": jsonable(sync_event),
    }


@app.get("/books/{book_id}/sync-events")
def list_sync_events(book_id: str) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM reader.sync_events
            WHERE source_kind = %s AND source_id = %s
            ORDER BY created_at DESC
            """,
            ("book", book_id),
        ).fetchall()
    return [jsonable(dict(row)) for row in rows]


@app.post("/sync/hermes/ingest")
def ingest_pending_hermes_sync_events(payload: HermesIngestRun = Body(default_factory=HermesIngestRun)) -> dict[str, Any]:
    limit = max(1, min(int(payload.limit), 100))
    root = cognitive_os_root(payload)
    if not payload.dry_run:
        sentence_reader_incoming_dir(root).mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    with db.connect() as conn:
        if payload.sync_event_ids:
            rows = conn.execute(
                """
                SELECT * FROM reader.sync_events
                WHERE id = ANY(%s) AND target_system = %s AND status = %s
                ORDER BY created_at ASC
                """,
                (payload.sync_event_ids, "hermes_cognitive_os", "pending"),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM reader.sync_events
                WHERE target_system = %s AND status = %s
                ORDER BY created_at ASC
                LIMIT %s
                """,
                ("hermes_cognitive_os", "pending", limit),
            ).fetchall()

        for row in rows:
            event = jsonable(dict(row))
            event_payload = event.get("payload") or {}
            event_id = event["id"]
            try:
                source_payload_path = event_payload.get("payload_path")
                if not source_payload_path:
                    raise ValueError("sync event payload missing payload_path")
                sync_payload = load_hermes_sync_payload(Path(str(source_payload_path)).expanduser())
                ingested_at = now_iso()
                if payload.dry_run:
                    results.append(
                        {
                            "id": event_id,
                            "status": "dry_run_ready",
                            "source_payload_path": source_payload_path,
                            "incoming_dir": str(sentence_reader_incoming_dir(root)),
                        }
                    )
                    continue

                written = write_hermes_ingestion_files(event, sync_payload, root, ingested_at)
                updated_payload = {
                    **event_payload,
                    "ingested_at": ingested_at,
                    "ingested_payload_path": written["payload_path"],
                    "ingestion_manifest_path": written["manifest_path"],
                    "ingestion_schema": written["manifest"]["schema"],
                }
                updated = update_sync_event(conn, event_id, "synced", updated_payload)
                results.append(
                    {
                        "id": event_id,
                        "status": "synced",
                        "payload_path": written["payload_path"],
                        "manifest_path": written["manifest_path"],
                        "sync_event": jsonable(updated),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - event-level failure must not stop the whole batch.
                error = f"{exc.__class__.__name__}: {exc}"
                if not payload.dry_run:
                    updated_payload = {**event_payload, "ingestion_error": error, "ingestion_failed_at": now_iso()}
                    update_sync_event(conn, event_id, "failed", updated_payload, error)
                results.append({"id": event_id, "status": "failed", "error": error})

    synced = sum(1 for item in results if item["status"] == "synced")
    failed = sum(1 for item in results if item["status"] == "failed")
    dry_ready = sum(1 for item in results if item["status"] == "dry_run_ready")
    return {
        "ok": failed == 0,
        "target_system": "hermes_cognitive_os",
        "dry_run": payload.dry_run,
        "incoming_dir": str(sentence_reader_incoming_dir(root)),
        "attempted": len(results),
        "synced_count": synced,
        "failed_count": failed,
        "dry_run_ready_count": dry_ready,
        "events": results,
    }


@app.get("/cognitive/review-queue")
def get_cognitive_review_queue(cognitive_os_dir: Optional[str] = None, limit: int = 100) -> dict[str, Any]:
    return build_cognitive_review_queue(CognitiveReviewQueueRun(cognitive_os_dir=cognitive_os_dir, limit=limit))


@app.post("/cognitive/review-queue")
def post_cognitive_review_queue(payload: CognitiveReviewQueueRun = Body(default_factory=CognitiveReviewQueueRun)) -> dict[str, Any]:
    return build_cognitive_review_queue(payload)


@app.get("/cognitive/dashboard")
def get_cognitive_dashboard(cognitive_os_dir: Optional[str] = None, limit: int = 100, history_limit: int = 20) -> dict[str, Any]:
    return build_cognitive_dashboard(CognitiveDashboardRun(cognitive_os_dir=cognitive_os_dir, limit=limit, history_limit=history_limit))


@app.post("/cognitive/dashboard")
def post_cognitive_dashboard(payload: CognitiveDashboardRun = Body(default_factory=CognitiveDashboardRun)) -> dict[str, Any]:
    return build_cognitive_dashboard(payload)


@app.post("/cognitive/review-item")
def post_cognitive_review_item(payload: CognitiveReviewItemRun = Body(default_factory=CognitiveReviewItemRun)) -> dict[str, Any]:
    return build_cognitive_review_item(payload)


@app.post("/cognitive/operator/dry-run")
def post_cognitive_operator_dry_run(payload: CognitiveOperatorDryRun = Body(default_factory=CognitiveOperatorDryRun)) -> dict[str, Any]:
    return run_cognitive_operator_dry_run(payload)


@app.post("/cognitive/operator/preflight")
def post_cognitive_operator_preflight(payload: CognitiveOperatorPreflight) -> dict[str, Any]:
    return run_cognitive_operator_preflight(payload)


@app.post("/cognitive/operator/approve")
def post_cognitive_operator_approve(payload: CognitiveOperatorApprove) -> dict[str, Any]:
    return run_cognitive_operator_approve(payload)


@app.patch("/annotations/{annotation_id}")
def patch_annotation(annotation_id: str, payload: AnnotationPatch) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute(
            """
            UPDATE reader.annotations
            SET note_text = COALESCE(%s, note_text),
                color = COALESCE(%s, color),
                metadata = COALESCE(%s, metadata),
                updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (payload.note_text, payload.color, db.jsonb(payload.metadata) if payload.metadata is not None else None, annotation_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="annotation not found")
        enqueue_living_book_evidence_sync(
            conn,
            source_kind="annotation",
            source_id=annotation_id,
            book_id=str(row["book_id"]),
            operation="annotation_updated",
        )
    start_living_book_evidence_sync_worker()
    return dict(row)


@app.post("/annotations/{annotation_id}/clean")
def clean_annotation_note(annotation_id: str, payload: Optional[AnnotationCleanRequest] = None) -> dict[str, Any]:
    payload = payload or AnnotationCleanRequest()
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM reader.annotations WHERE id = %s", (annotation_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="annotation not found")
    original_note = str(row.get("note_text") or "").strip()
    if not original_note:
        raise HTTPException(status_code=422, detail="annotation note is empty")
    prompt = f"""
你是 Reader 语音备注整理器，通过 Hermes 调用 Qwen。请只整理用户口述备注，不要扩写事实，不要添加原文没有的信息。
任务：
1. 删除明显口头语和填充词，例如“呃”“啊”“那个”“就是”。
2. 补齐中文标点，必要时分段。
3. 保留用户原意、关键词和判断。
4. 只返回 JSON：{{"cleaned_text":"整理后的中文备注"}}

原始备注：
{original_note}
""".strip()
    try:
        response = call_hermes_runtime(prompt, session_id=f"reader_annotation_clean_{annotation_id}", timeout_seconds=120)
        reply = str(response.get("reply") or response.get("text") or response.get("message") or "")
        parsed = extract_json_object(reply)
        cleaned_text = normalize_note_text(str(parsed.get("cleaned_text") or parsed.get("text") or ""))
    except Exception as exc:  # noqa: BLE001 - surface Hermes/Qwen cleanup failures to the editor.
        raise HTTPException(status_code=502, detail=f"Hermes Qwen cleanup failed: {exc}") from exc
    if not cleaned_text:
        raise HTTPException(status_code=502, detail="Hermes Qwen cleanup returned empty text")

    metadata = row.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    metadata = dict(metadata)
    metadata["ai_cleanup"] = {
        "provider": "hermes_qwen",
        "status": "cleaned",
        "cleaned_at": now_iso(),
        "raw_note_text": original_note,
        "applied": bool(payload.apply),
    }
    with db.connect() as conn:
        updated = conn.execute(
            """
            UPDATE reader.annotations
            SET note_text = CASE WHEN %s THEN %s ELSE note_text END,
                metadata = %s,
                updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (bool(payload.apply), cleaned_text, db.jsonb(metadata), annotation_id),
        ).fetchone()
        enqueue_living_book_evidence_sync(
            conn,
            source_kind="annotation",
            source_id=annotation_id,
            book_id=str(updated["book_id"]),
            operation="annotation_ai_cleanup_updated",
            details={"applied": bool(payload.apply)},
        )
    start_living_book_evidence_sync_worker()
    return {"ok": True, "cleaned_text": cleaned_text, "annotation": dict(updated)}


@app.delete("/annotations/{annotation_id}")
def delete_annotation(annotation_id: str) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute(
            "DELETE FROM reader.annotations WHERE id = %s RETURNING id, book_id, kind, chapter_locator",
            (annotation_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="annotation not found")
        enqueue_living_book_evidence_sync(
            conn,
            source_kind="annotation",
            source_id=annotation_id,
            book_id=str(row["book_id"]),
            operation="annotation_deleted",
            details={
                "kind": row.get("kind"),
                "chapter_locator": row.get("chapter_locator"),
                "tombstone": True,
            },
        )
    start_living_book_evidence_sync_worker()
    return {"ok": True, "id": row["id"]}


@app.post("/audio-notes")
def create_audio_note(payload: AudioNoteCreate) -> dict[str, Any]:
    validate_audio_status(payload.status)
    audio_note_id = new_id("aud")
    with db.connect() as conn:
        row = conn.execute(
            """
            INSERT INTO reader.audio_notes (
                id, annotation_id, book_id, audio_path, audio_hash, duration_seconds,
                provider, transcript, raw_result, status, error_message, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())
            RETURNING *
            """,
            (
                audio_note_id,
                payload.annotation_id,
                payload.book_id,
                payload.audio_path,
                payload.audio_hash,
                payload.duration_seconds,
                payload.provider,
                payload.transcript,
                db.jsonb(payload.raw_result),
                payload.status,
                payload.error_message,
            ),
        ).fetchone()
        if row:
            apply_audio_note_to_annotation(conn, dict(row))
            enqueue_living_book_evidence_sync(
                conn,
                source_kind="audio_note",
                source_id=audio_note_id,
                book_id=payload.book_id,
                operation="audio_note_created",
                details={"status": payload.status},
            )
    if row:
        start_living_book_evidence_sync_worker()
        sync_reader_audio_note_to_voice_inbox(audio_note_id)
    return dict(row)


@app.get("/audio-notes/{audio_note_id}")
def get_audio_note(audio_note_id: str) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM reader.audio_notes WHERE id = %s", (audio_note_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="audio note not found")
    return dict(row)


@app.get("/audio-notes/{audio_note_id}/audio")
def get_audio_note_audio(audio_note_id: str) -> FileResponse:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT audio_path FROM reader.audio_notes WHERE id = %s",
            (audio_note_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="audio note not found")
    audio_path = Path(str(row.get("audio_path") or "")).expanduser()
    if not audio_path.is_file():
        raise HTTPException(status_code=404, detail="audio note file missing")
    media_type = {
        ".m4a": "audio/mp4",
        ".mp4": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".webm": "audio/webm",
        ".ogg": "audio/ogg",
    }.get(audio_path.suffix.lower(), "application/octet-stream")
    return FileResponse(audio_path, media_type=media_type, filename=audio_path.name)


@app.patch("/audio-notes/{audio_note_id}")
def patch_audio_note(audio_note_id: str, payload: AudioNotePatch) -> dict[str, Any]:
    if payload.status is not None:
        validate_audio_status(payload.status)
    with db.connect() as conn:
        row = conn.execute(
            """
            UPDATE reader.audio_notes
            SET annotation_id = COALESCE(%s, annotation_id),
                audio_hash = COALESCE(%s, audio_hash),
                duration_seconds = COALESCE(%s, duration_seconds),
                provider = COALESCE(%s, provider),
                transcript = COALESCE(%s, transcript),
                raw_result = COALESCE(%s, raw_result),
                status = COALESCE(%s, status),
                error_message = %s,
                updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (
                payload.annotation_id,
                payload.audio_hash,
                payload.duration_seconds,
                payload.provider,
                payload.transcript,
                db.jsonb(payload.raw_result) if payload.raw_result is not None else None,
                payload.status,
                payload.error_message,
                audio_note_id,
            ),
        ).fetchone()
        if row:
            apply_audio_note_to_annotation(conn, dict(row))
            enqueue_living_book_evidence_sync(
                conn,
                source_kind="audio_note",
                source_id=audio_note_id,
                book_id=str(row["book_id"]),
                operation="audio_note_updated",
                details={"status": row.get("status")},
            )
        else:
            raise HTTPException(status_code=404, detail="audio note not found")
    start_living_book_evidence_sync_worker()
    sync_reader_audio_note_to_voice_inbox(audio_note_id)
    return dict(row)


@app.get("/books/{book_id}/audio-notes")
def list_audio_notes(book_id: str) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM reader.audio_notes
            WHERE book_id = %s
            ORDER BY created_at DESC
            """,
            (book_id,),
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/exports")
def create_export(payload: ExportCreate) -> dict[str, Any]:
    with db.connect() as conn:
        return insert_export_record(conn, payload.book_id, payload.export_kind, payload.output_path, payload.annotation_count)


def main() -> None:
    import uvicorn

    from reader_api.config import api_host, api_port

    uvicorn.run("reader_api.app:app", host=api_host(), port=api_port(), reload=False)


if __name__ == "__main__":
    main()
