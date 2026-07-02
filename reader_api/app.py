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
import subprocess
import sys
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import uuid4
from xml.etree import ElementTree as ET

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

from reader_api import db
from reader_api.mobile_workspace import (
    EDGE_TTS_VOICE,
    MAC_VOICE_PIPELINE_ID,
    MAC_VOICE_PIPELINE_SCHEMA,
    call_hermes_runtime,
    edge_tts_path,
    extract_json_object,
    mac_voice_pipeline_transcribe,
    router as mobile_workspace_router,
)


app = FastAPI(title="Sentence Reader API", version="2.0.0")
app.include_router(mobile_workspace_router)


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
ENABLE_HERMES_ONLINE_LOOKUP = os.getenv(
    "SENTENCE_READER_ENABLE_HERMES_ONLINE_LOOKUP",
    "0",
).strip().lower() in {"1", "true", "yes", "on"}
VOICE_NOTE_PENDING_TEXT = "语音转写中..."
VOICE_NOTE_FAILED_TEXT = "语音已保存，转写失败，可稍后重试。"


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
    audio_base64: str
    mime_type: str = "audio/webm"
    duration_seconds: Optional[float] = None


class LibraryImport(BaseModel):
    filename: str
    content_base64: str
    title: Optional[str] = None
    author: Optional[str] = None


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
    for encoding in ("utf-8", "utf-16", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


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


def library_cover_info(book: dict[str, Any], file_status: dict[str, Any]) -> dict[str, Any]:
    has_epub_cover = False
    if file_status.get("exists") and file_status.get("extension") == "epub":
        has_epub_cover = epub_cover_asset(Path(str(file_status.get("file_path") or "")).expanduser()) is not None
    return {
        "url": f"/api/library/books/{book.get('id')}/cover",
        "kind": "epub" if has_epub_cover else "generated",
        "has_image": has_epub_cover,
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
                   bf.byte_size
            FROM reader.books b
            LEFT JOIN LATERAL (
                SELECT file_path, file_kind, file_hash, byte_size
                FROM reader.book_files
                WHERE book_id = b.id
                ORDER BY created_at DESC
                LIMIT 1
            ) bf ON true
            WHERE b.id = %s
            """,
            (book_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="book not found")
    return dict(row)


def epub_path_for_book(book: dict[str, Any]) -> Path:
    file_path = str(book.get("file_path") or "").strip()
    if not file_path:
        raise HTTPException(status_code=404, detail="book has no EPUB file path")
    return Path(file_path).expanduser()


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
    const state = { books: [], book: null, manifest: null, chapterIndex: 0, annotations: [], focused: null, redIDs: new Map(), noteByIndex: new Map(), saveTimer: 0, noteTimer: 0, sentenceTapTimer: 0, voiceToastTimer: 0, noteEditorAnnotationID: '', noteEditorAudioNoteID: '', pendingAudioPolls: new Map(), pageIndex: 0, totalPages: 1, pageTurnLockUntil: 0, pendingPageTurnDirection: 0, pendingPageTurnTimer: 0, wheelGestureDirection: 0, wheelGestureDistance: 0, wheelGestureConsumed: false, lastWheelEventAt: 0, wheelInertiaLockUntil: 0, touchStartX: 0, touchStartY: 0, touchStartTime: 0, touchSentence: null, longPressTimer: 0, longPressTriggered: false, recognition: null, mediaRecorder: null, nativeReaderAudioRecording: false, voiceChunks: [], voiceStartedAt: 0, voiceStream: null, lookup: null };
    // Keep the physical wheel gesture boundary separate from page animation:
    // one swipe triggers one page, while the next clear swipe may arrive before the animation ends.
    const pageTurnCooldownMs = 120;
    const wheelInertiaLockMs = 180;
    const wheelGestureIdleMs = 220;
    const wheelPageTurnThreshold = 96;
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
    }
    function handleHorizontalWheel(event) {
      const now = Date.now();
      const absX = Math.abs(event.deltaX || 0);
      const absY = Math.abs(event.deltaY || 0);
      const startsNewGesture = now - state.lastWheelEventAt > wheelGestureIdleMs;
      if (startsNewGesture) resetWheelGesture();
      state.lastWheelEventAt = now;
      if (absX < 10 || absX < absY * wheelDominanceRatio) {
        resetWheelGesture();
        return false;
      }
      if (now < state.wheelInertiaLockUntil && !startsNewGesture) {
        state.wheelGestureConsumed = true;
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
        void turnPage(turnDirection);
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
      systemWhen: ['editable-target', 'active-text-selection'],
      sentenceWhen: ['tap-focus-actions', 'english-tap-lookup', 'double-tap-note', 'context-click-red', 'long-press-red'],
      sentenceContextWinsEvenWithSelection: true,
      copyPath: 'command-c-or-non-sentence-context-menu'
    };
    function hasSystemTextSelection() {
      const selection = window.getSelection && window.getSelection();
      return !!(selection && !selection.isCollapsed && String(selection.toString() || '').trim().length > 0);
    }
    function isEditableTarget(target) {
      const node = target && target.nodeType === Node.ELEMENT_NODE ? target : target && target.parentElement;
      if (!node || !node.closest) return false;
      return !!node.closest('input, textarea, select, button, [contenteditable="true"], [contenteditable=""]');
    }
    function shouldLetSystemHandle(event, options = {}) {
      if (isEditableTarget(event && event.target)) return true;
      if (options.respectSelection !== false && hasSystemTextSelection()) return true;
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
      return hasSystemTextSelection();
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


def sentence_reader_app_support_dir() -> Path:
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
        request = Request(url, method="GET")
    else:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
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
        owned = False
    return {
        "file_path": str(path) if file_path else "",
        "exists": exists,
        "owned_internal_copy": owned,
        "extension": path.suffix.lower().lstrip("."),
    }


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
    book_stub = {"id": row.get("id"), "title": row.get("title"), "author": row.get("author"), "book_hash": row.get("book_hash")}
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
        "actions": {
            "native_reader_url": f"sentence-reader://open-native?book_id={row.get('id')}",
            "continue_reading_url": f"/lan/reader?book_id={row.get('id')}" if lan_available else "",
            "notes_filter": f"/library?view=notes&book_id={row.get('id')}",
            "red_filter": f"/library?view=red&book_id={row.get('id')}",
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
    owned_scan = sync_owned_epub_library()
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT b.*,
                   bf.file_path,
                   bf.file_kind,
                   bf.file_hash,
                   bf.byte_size,
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
                ORDER BY created_at DESC
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
    if len(data) > 120 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="EPUB import is too large")
    return data


def import_library_epub(payload: LibraryImport) -> dict[str, Any]:
    filename = Path(payload.filename).name
    if not filename.lower().endswith(".epub"):
        raise HTTPException(status_code=422, detail="only EPUB import is supported")
    data = decode_library_import_base64(payload.content_base64)
    book_hash = hashlib.sha256(data).hexdigest()
    root = app_support_books_dir() / book_hash
    root.mkdir(parents=True, exist_ok=True)
    epub_path = root / "book.epub"
    epub_path.write_bytes(data)
    try:
        publication = epub_publication(epub_path)
    except HTTPException:
        epub_path.unlink(missing_ok=True)
        raise
    title = (payload.title or publication.get("title") or Path(filename).stem).strip() or Path(filename).stem
    author = (payload.author or publication.get("author") or "").strip() or None
    book_id = new_id("book")
    with db.connect() as conn:
        book = conn.execute(
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
            (book_id, title, author, "epub", book_hash),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO reader.book_files (id, book_id, file_path, file_kind, file_hash, byte_size)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (book_id, file_path) DO UPDATE
            SET file_hash = EXCLUDED.file_hash,
                byte_size = EXCLUDED.byte_size
            """,
            (new_id("file"), book["id"], str(epub_path), "epub", book_hash, len(data)),
        )
        conn.execute(
            """
            INSERT INTO reader.library_state (book_id, hidden, source, metadata, created_at, updated_at)
            VALUES (%s, false, %s, %s, now(), now())
            ON CONFLICT (book_id) DO UPDATE
            SET hidden = false,
                source = EXCLUDED.source,
                metadata = EXCLUDED.metadata,
                updated_at = now()
            """,
            (
                book["id"],
                "library_web_import",
                db.jsonb({"filename": filename, "owned_internal_copy": True}),
            ),
        )
    return {
        "ok": True,
        "schema": "sentence_reader.library_import.v1",
        "book": jsonable(dict(book)),
        "file_path": str(epub_path),
        "owned_internal_copy": True,
        "original_source_can_be_deleted": True,
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

        title = str(publication.get("title") or epub_path.parent.name).strip() or epub_path.parent.name
        author = str(publication.get("author") or "").strip() or None
        byte_size = epub_path.stat().st_size
        book_hash = root_name
        book_id = stable_id("book", "owned-epub", book_hash)
        with db.connect() as conn:
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


def update_library_book_organization(book_id: str, payload: LibraryOrganizationPatch) -> dict[str, Any]:
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
            (book_id, "library_web_organization", db.jsonb(metadata)),
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
        <strong>导入 EPUB</strong>
        <div class="muted">文件会复制到 Mac 内部书库，原文件可删除。</div>
        <input id="fileInput" type="file" accept=".epub,application/epub+zip" style="margin-top:10px">
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
        $('books').innerHTML = `<div class="panel" style="padding:18px">没有匹配的书。可以导入 EPUB，或者换一个筛选条件。</div>`;
        return;
      }
      $('books').innerHTML = books.map((book) => `
        <button class="book-card ${state.selected && state.selected.id === book.id ? 'selected' : ''}" data-book="${esc(book.id)}">
          <div class="cover"><span>${esc(coverText(book))}</span></div>
          <div class="book-title">${esc(book.title || book.id)}</div>
          <div class="book-meta">${esc(book.author || '未知作者')}</div>
          <div class="progress"><i style="width:${book.progress.percent}%"></i></div>
          <div class="book-meta">${book.progress.percent}% · ${book.status.lan_available ? '可阅读' : '文件不可用'}</div>
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
        $('detail').innerHTML = `<h2>还没有书</h2><p class="muted">先导入一本 EPUB。</p>`;
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
          <button class="primary" id="continue" ${book.status.lan_available ? '' : 'disabled'}>继续阅读</button>
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
      toast('正在导入 EPUB...');
      await api('/api/library/import', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({ filename:file.name, content_base64:btoa(binary) })
      });
      toast('导入完成，原 EPUB 可删除');
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


def library_page_html_v2() -> str:
    return r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>Sentence Reader Library</title>
  <style>
    :root {
      color-scheme: dark;
      --bg:#070806; --surface:#0f110d; --panel:#151711; --panel-2:#20231a;
      --line:#323829; --text:#f7f2e7; --muted:#a9a995; --soft:#ddd4b7;
      --accent:#e4b453; --jade:#79b88b; --cyan:#7db9c4; --coral:#d9856a; --green:#8fbe7a; --danger:#e9786b;
      --shadow:0 28px 90px rgba(0,0,0,.42);
    }
    * { box-sizing:border-box; }
    html, body { margin:0; min-height:100%; background:linear-gradient(180deg,#0b0c08 0,#070806 46%,#050604 100%); color:var(--text); font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif; }
    body { overflow-x:hidden; }
    button, input, select { font:inherit; }
    button { border:0; border-radius:8px; background:var(--panel-2); color:var(--text); padding:9px 12px; cursor:pointer; min-height:38px; }
    button:hover { background:#2a2f23; }
    button.primary { background:linear-gradient(135deg,var(--accent),#f0cf75); color:#17120a; font-weight:800; }
    button.ghost { background:transparent; color:var(--soft); }
    button.subtle { background:#15160f; border:1px solid var(--line); color:var(--soft); }
    button.danger { background:rgba(233,120,107,.12); color:#ffb5aa; border:1px solid rgba(233,120,107,.35); }
    button:disabled { opacity:.46; cursor:default; }
    input, select { width:100%; border:1px solid var(--line); background:#10110c; color:var(--text); border-radius:8px; padding:10px 12px; outline:none; }
    input:focus, select:focus { border-color:rgba(215,168,79,.75); box-shadow:0 0 0 3px rgba(215,168,79,.12); }
    .app-shell { min-height:100vh; display:grid; grid-template-columns:232px minmax(0,1fr); }
    .sidebar { border-right:1px solid var(--line); background:linear-gradient(180deg,#11130d,#090a07); padding:22px 15px; position:sticky; top:0; height:100vh; }
    .brand { margin-bottom:22px; }
    .brand strong { display:block; font-size:22px; line-height:1.1; letter-spacing:0; }
    .nav { display:grid; gap:7px; }
    .nav button { width:100%; display:flex; align-items:center; justify-content:space-between; background:transparent; color:var(--soft); text-align:left; }
    .nav button.active { background:#222719; color:var(--text); box-shadow:inset 3px 0 0 var(--accent); }
    .side-action { margin-top:18px; display:grid; gap:8px; }
    .main { min-width:0; padding:22px 28px 42px; }
    .topbar { display:grid; grid-template-columns:minmax(260px,1fr) auto auto auto; gap:10px; align-items:center; margin-bottom:18px; }
    .status-pill { display:inline-flex; align-items:center; gap:7px; color:#cfe6c7; background:rgba(135,182,122,.12); border:1px solid rgba(135,182,122,.28); border-radius:999px; padding:8px 11px; font-size:12px; }
    .status-dot { width:7px; height:7px; border-radius:50%; background:var(--green); }
    .view { display:none; }
    .view.active { display:block; }
    .section-head { display:flex; align-items:end; justify-content:space-between; gap:12px; margin:18px 0 12px; }
    .section-head h1, .section-head h2 { margin:0; letter-spacing:0; }
    .section-head h1 { font-size:25px; }
    .section-head h2 { font-size:18px; }
    .section-head p { margin:5px 0 0; color:var(--muted); font-size:13px; }
    .continue-hero { min-height:380px; border:1px solid rgba(228,180,83,.22); border-radius:8px; background:radial-gradient(circle at 18% 15%,rgba(228,180,83,.16),transparent 28%),radial-gradient(circle at 82% 28%,rgba(125,185,196,.12),transparent 30%),linear-gradient(135deg,#171a10,#0d100b 62%,#050604); display:grid; grid-template-columns:minmax(210px,270px) minmax(0,1fr); gap:30px; padding:28px; box-shadow:var(--shadow); overflow:hidden; }
    .hero-copy { min-width:0; display:flex; flex-direction:column; justify-content:center; max-width:780px; }
    .eyebrow { color:var(--accent); font-size:12px; letter-spacing:1px; text-transform:uppercase; font-weight:900; }
    .hero-title { font-size:38px; line-height:1.13; margin:10px 0 8px; word-break:break-word; max-width:760px; }
    .hero-meta { color:var(--muted); font-size:14px; }
    .hero-actions { display:flex; flex-wrap:wrap; gap:10px; margin-top:18px; }
    .hero-manifest { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; margin-top:20px; }
    .manifest-item { border:1px solid rgba(255,255,255,.1); background:rgba(255,255,255,.04); border-radius:8px; padding:10px 11px; min-width:0; }
    .manifest-item strong { display:block; font-size:15px; line-height:1.25; color:var(--text); }
    .manifest-item span { display:block; margin-top:4px; color:var(--muted); font-size:12px; line-height:1.35; }
    .cover-frame { position:relative; width:100%; aspect-ratio:3/4; border-radius:8px; overflow:hidden; background:#171914; border:1px solid rgba(255,255,255,.09); box-shadow:0 22px 58px rgba(0,0,0,.48); }
    .cover-frame[data-open-book] { cursor:pointer; }
    .cover-frame[data-open-book]:focus { outline:2px solid rgba(228,180,83,.65); outline-offset:3px; }
    .cover-frame img { width:100%; height:100%; object-fit:cover; display:block; }
    .cover-frame.hero-cover { align-self:center; min-height:300px; }
    .cover-frame.hero-cover::after { content:""; position:absolute; inset:0; background:linear-gradient(180deg,rgba(0,0,0,.02) 25%,rgba(0,0,0,.42) 100%); pointer-events:none; }
    .cover-frame.small { width:92px; flex:0 0 92px; }
    .cover-phrases { position:absolute; z-index:2; left:14px; right:14px; bottom:14px; display:grid; gap:7px; }
    .cover-phrases span { display:block; border:1px solid rgba(255,255,255,.16); background:rgba(7,8,6,.68); color:#f9f2dc; border-radius:999px; padding:7px 10px; font-size:13px; line-height:1.1; font-weight:900; text-align:center; backdrop-filter:blur(8px); }
    .cover-phrases span:nth-child(2) { color:#cceee0; }
    .cover-phrases span:nth-child(3) { color:#f6d28a; }
    .progress { height:8px; background:#2a2b22; border-radius:999px; overflow:hidden; margin-top:16px; }
    .progress i { display:block; height:100%; background:linear-gradient(90deg,var(--accent),var(--green)); width:0; }
    .rail { display:grid; grid-auto-flow:column; grid-auto-columns:minmax(150px, 190px); gap:12px; overflow:auto; padding-bottom:4px; }
    .book-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(178px,1fr)); gap:14px; }
    .book-card { position:relative; min-width:0; text-align:left; background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; transition:border-color .12s ease, background .12s ease; }
    .book-card:hover, .book-card:focus { border-color:rgba(215,168,79,.62); background:#1c1d16; outline:none; }
    .book-card.selected { border-color:var(--accent); box-shadow:0 0 0 1px rgba(228,180,83,.25); }
    .book-card .cover-frame { margin-bottom:10px; }
    .book-title { font-weight:800; line-height:1.32; min-height:39px; word-break:break-word; }
    .book-meta { color:var(--muted); font-size:12px; margin-top:4px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .book-row { display:flex; align-items:center; gap:8px; margin-top:8px; flex-wrap:wrap; }
    .badge { font-size:11px; color:var(--soft); border:1px solid var(--line); border-radius:999px; padding:3px 7px; background:#11120f; }
    .badge.state { color:#11120f; background:var(--jade); border-color:var(--jade); font-weight:800; }
    .badge.red { color:#ffc1b7; border-color:rgba(217,133,106,.4); }
    .card-check { display:none; position:absolute; z-index:4; left:12px; top:12px; width:18px; height:18px; accent-color:var(--accent); }
    .book-card.selectable .card-check { display:block; }
    .card-secondary { margin-top:10px; padding-top:9px; border-top:1px solid rgba(255,255,255,.07); display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:6px; }
    .card-action { padding:5px 6px; min-height:30px; background:transparent; border:1px solid rgba(255,255,255,.08); color:var(--muted); font-size:12px; }
    .card-action:hover { color:var(--text); border-color:rgba(228,180,83,.32); background:rgba(228,180,83,.06); }
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
      .app-shell { grid-template-columns:1fr; }
      .sidebar { position:static; height:auto; border-right:0; border-bottom:1px solid var(--line); }
      .nav { grid-template-columns:repeat(5,minmax(0,1fr)); }
      .topbar, .toolbar { grid-template-columns:1fr 1fr; }
      .continue-hero { grid-template-columns:160px minmax(0,1fr); }
      .hero-title { font-size:32px; }
      .hero-manifest { grid-template-columns:1fr; }
    }
    @media (max-width: 640px) {
      .main { padding:14px 12px 24px; }
      .nav { grid-template-columns:1fr 1fr; }
      .topbar, .toolbar { grid-template-columns:1fr; }
      .continue-hero { grid-template-columns:1fr; padding:18px; gap:18px; }
      .continue-hero .cover-frame { max-width:190px; min-height:252px; }
      .hero-title { font-size:28px; }
      .hero-manifest { grid-template-columns:repeat(3,minmax(0,1fr)); gap:7px; margin-top:14px; }
      .manifest-item { padding:8px 6px; text-align:center; }
      .manifest-item strong { font-size:13px; }
      .manifest-item span { display:none; }
      .card-secondary { grid-template-columns:1fr; }
      .book-grid { grid-template-columns:repeat(2,minmax(0,1fr)); }
    }
  </style>
</head>
<body>
  <div class="app-shell" data-library-v2="true" data-native-reader-contract="sentence-reader://open-native">
    <aside class="sidebar">
      <div class="brand"><strong>Click</strong></div>
      <nav class="nav" id="nav"></nav>
      <div class="side-action">
        <button class="subtle" data-view-jump="settings">状态与设置</button>
      </div>
    </aside>
    <main class="main">
      <div class="topbar">
        <input id="search" placeholder="搜索书名、作者、分类、标签、笔记、红标">
        <span class="status-pill"><i class="status-dot"></i><span id="serviceStatus">正在连接</span></span>
        <button class="subtle" id="refresh">刷新</button>
        <button class="primary" id="topImport">导入</button>
      </div>

      <section id="homeView" class="view active" data-product-home="true">
        <div class="section-head"><div><h1>继续阅读</h1><p>封面、原句、单词本在同一个工作台里。</p></div></div>
        <section id="continueHero" class="continue-hero"></section>
        <div class="section-head"><div><h2>最近阅读</h2><p>点击封面直接进入正文。</p></div><button class="ghost" data-view-jump="library">全部书籍</button></div>
        <section id="recentRail" class="rail"></section>
        <div class="section-head"><div><h2>最近沉淀</h2><p>最近写下的笔记和红标。</p></div></div>
        <section id="recentAssets" class="asset-list"></section>
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
        <div class="section-head"><div><h1>设置</h1><p>日常阅读之外的状态和恢复入口。</p></div></div>
        <section id="settingsPanel" class="asset-list"></section>
      </section>
    </main>
  </div>
  <div id="drawerBackdrop" class="drawer-backdrop"></div>
  <aside id="drawer" class="drawer" aria-hidden="true"></aside>
  <div id="removeModal" class="modal-backdrop" aria-hidden="true"></div>
  <div id="orgModal" class="modal-backdrop" aria-hidden="true"></div>
  <input id="fileInput" type="file" accept=".epub,application/epub+zip" hidden>
  <div id="toast" class="toast"></div>
  <script>
    const state = { dashboard:null, books:[], hiddenBooks:[], assets:[], view:'home', query:'', sort:'recent', stateFilter:'all', manageMode:false, selectedBook:null, activeBookId:null, selectedIds:new Set(), pendingRemove:null, lastRemoved:null, lastImport:null, error:null };
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
        ['home','首页',''],
        ['library','书库', summary.book_count || 0],
        ['favorites','收藏', summary.favorite_count || 0],
        ['authors','作者', summary.author_count || 0],
        ['categories','分类', summary.category_count || 0],
        ['vocab','单词',''],
        ['notes','笔记', summary.note_count || 0],
        ['red','红标', summary.red_highlight_count || 0],
        ['settings','设置','']
      ];
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
      $('nav').innerHTML = navItems().map(([id,title,count]) => `<button class="${state.view === id ? 'active' : ''}" data-view="${id}"><span>${title}</span><span>${count}</span></button>`).join('');
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
    function openBook(book) {
      if (!book?.status?.lan_available) { toast('这本书的文件暂时不可读，请在详情里检查文件状态。'); return; }
      if (isMacAppSurface() && book.actions?.native_reader_url) {
        window.location.href = book.actions.native_reader_url;
        return;
      }
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
      const favoriteLabel = org.favorite ? '取消收藏' : '收藏';
      const modeClass = state.manageMode ? 'selectable' : '';
      const selectedClass = state.selectedIds.has(book.id) ? 'selected' : '';
      return `<article class="book-card ${modeClass} ${selectedClass}" tabindex="0" data-book="${esc(book.id)}" data-open-book-card="true">
        <input class="card-check" type="checkbox" data-select="${esc(book.id)}" ${checked} aria-label="选择书籍">
        ${favorite}
        ${cover(book)}
        <div class="book-title">${esc(book.title || book.id)}</div>
        <div class="book-meta">${esc(book.author || '未知作者')}</div>
        <div class="progress"><i style="width:${book.progress?.percent || 0}%"></i></div>
        <div class="book-row"><span class="badge state">${esc(book.reading_state || '未开始')}</span><span class="badge">${progressText(book)}</span><span class="badge">${esc(org.category || '未分类')}</span><span class="badge">${book.counts?.notes || 0} 笔记</span><span class="badge red">${book.counts?.red_highlights || 0} 红标</span></div>
        <div class="card-secondary" data-card-secondary="true">
          <button class="card-action" data-favorite="${esc(book.id)}">${favoriteLabel}</button>
          <button class="card-action" data-vocab="${esc(book.id)}">单词</button>
          <button class="card-action" data-details="${esc(book.id)}">详情</button>
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
      document.querySelectorAll('[data-vocab]').forEach((button) => button.onclick = (event) => {
        event.stopPropagation();
        window.location.href = vocabURL(button.dataset.vocab);
      });
      document.querySelectorAll('[data-details]').forEach((button) => button.onclick = () => openDrawer(byId(button.dataset.details)));
      document.querySelectorAll('[data-favorite]').forEach((button) => button.onclick = (event) => {
        event.stopPropagation();
        const book = byId(button.dataset.favorite);
        toggleFavorite(book).catch((error) => toast(`收藏失败：${error.message}`));
      });
      document.querySelectorAll('[data-select]').forEach((box) => box.onchange = () => {
        if (box.checked) state.selectedIds.add(box.dataset.select); else state.selectedIds.delete(box.dataset.select);
        syncBookSelection(box.dataset.select);
      });
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
    function renderHome() {
      const current = state.dashboard?.current_book || state.books[0];
      if (!current) {
        $('continueHero').innerHTML = `<div class="empty"><h2>先导入一本 EPUB</h2><p>导入后会复制到内部书库，原文件可以删除。</p><button class="primary" id="emptyImport">导入 EPUB</button></div>`;
        $('emptyImport').onclick = () => $('fileInput').click();
      } else {
        $('continueHero').innerHTML = `${cover(current, 'hero-cover', true, true)}<div class="hero-copy"><div class="eyebrow">Continue Reading</div><div class="hero-title">${esc(current.title || current.id)}</div><div class="hero-meta">${esc(current.author || '未知作者')} · ${esc(chapterText(current))} · ${progressText(current)}</div><div class="progress"><i style="width:${current.progress?.percent || 0}%"></i></div><div class="hero-manifest"><div class="manifest-item"><strong>逐句读懂</strong><span>英文原句和中文证据一起看。</span></div><div class="manifest-item"><strong>语境查词</strong><span>先看本句义，再看词典短释。</span></div><div class="manifest-item"><strong>复习沉淀</strong><span>查过的词进入主动学习。</span></div></div><div class="hero-actions"><button class="primary" id="heroContinue">继续阅读</button><button class="subtle" id="heroDetail">查看详情</button><button class="ghost" data-view-jump="notes">整理笔记</button></div></div>`;
        $('heroContinue').onclick = () => openBook(current);
        $('heroDetail').onclick = () => openDrawer(current);
        bindOpenBookTargets($('continueHero'));
      }
      const recent = (state.dashboard?.recent_books || state.books).slice(0, 6);
      $('recentRail').innerHTML = recent.length ? recent.map(bookCard).join('') : `<div class="empty">还没有最近阅读。</div>`;
      const assets = (state.dashboard?.recent_annotations || []).slice(0, 5);
      $('recentAssets').innerHTML = assets.length ? assets.map(assetCard).join('') : `<div class="empty">还没有笔记或红标。读书时双击写注释，双指点按标红。</div>`;
      bindBookCards();
    }
    function renderLibrary() {
      const books = sortedBooks();
      $('bookCount').textContent = `${books.length} 本`;
      $('bookGrid').innerHTML = books.length ? books.map(bookCard).join('') : `<div class="empty"><h2>没有匹配的书</h2><p>换一个搜索词，或者导入新的 EPUB。</p></div>`;
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
    function bindAssets() { document.querySelectorAll('[data-open-asset]').forEach((button) => button.onclick = () => openBook(byId(button.dataset.openAsset))); }
    function renderAssets() {
      const notes = state.assets.filter((asset) => assetMatches(asset, 'note'));
      const red = state.assets.filter((asset) => assetMatches(asset, 'red_highlight'));
      $('notesList').innerHTML = notes.length ? notes.map(assetCard).join('') : `<div class="empty"><h2>还没有笔记</h2><p>在正文里双击句子即可写备注。</p></div>`;
      $('redList').innerHTML = red.length ? red.map(assetCard).join('') : `<div class="empty"><h2>还没有红标</h2><p>在正文里双指点按或右键句子即可标红。</p></div>`;
      bindAssets();
    }
    function renderSettings() {
      const ok = state.dashboard?.ok;
      const importResult = state.lastImport ? `<article class="asset-card"><div><strong>最近导入成功</strong><p>${esc(state.lastImport.book?.title || '')}</p><div class="book-meta">已复制到内部书库，原文件可删除。</div></div><button class="subtle" data-open-import="${esc(state.lastImport.book?.id || '')}">打开</button></article>` : '';
      const hidden = state.hiddenBooks || [];
      const hiddenList = hidden.length ? hidden.map((book) => `<article class="asset-card"><div><strong>${esc(book.title || book.id)}</strong><p>${esc(book.author || '未知作者')} · 已移出书库，数据仍保留。</p></div><button class="subtle" data-restore-book="${esc(book.id)}">恢复</button></article>`).join('') : `<article class="asset-card"><div><strong>已移出书库</strong><p>这里暂时没有隐藏的书。</p></div><button class="subtle" id="hiddenRefresh">刷新</button></article>`;
      $('settingsPanel').innerHTML = `${importResult}<article class="asset-card"><div><strong>阅读服务</strong><p>${ok ? '已连接，可以正常打开书库和正文。' : '未连接，请重启 App。'}</p></div><button class="subtle" id="settingsRefresh">重新检查</button></article><article class="asset-card"><div><strong>iPad 访问</strong><p>同一局域网下打开本机地址的 /library，直接阅读地址仍保留 /lan/reader。</p></div><button class="subtle" id="copyLocal">复制本机地址</button></article><details open><summary>已移出书库 · ${hidden.length} 本</summary><div class="asset-list" style="margin-top:10px">${hiddenList}</div></details><details><summary>高级信息</summary><p id="advancedInfo">书籍 ${state.books.length} 本，已移出 ${hidden.length} 本，笔记 ${state.dashboard?.summary?.note_count || 0} 条，红标 ${state.dashboard?.summary?.red_highlight_count || 0} 条。文件状态可在书籍详情中查看。</p></details>`;
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
      const categoryOptions = Array.from(new Set((state.dashboard?.category_groups || []).map((group) => group.category).filter(Boolean))).filter((name) => name !== '未分类');
      const chips = (org.tags || []).length ? `<div class="org-tags">${org.tags.map((tag) => `<span class="badge">${esc(tag)}</span>`).join('')}</div>` : '';
      $('drawer').innerHTML = `<button class="ghost" id="drawerClose">关闭</button>${cover(book)}<h2>${esc(book.title || book.id)}</h2><div class="book-meta">${esc(book.author || '未知作者')} · ${esc(book.reading_state || '')}</div><div class="book-row"><span class="badge">${esc(org.category || '未分类')}</span>${org.favorite ? '<span class="badge">已收藏</span>' : ''}</div><div class="progress"><i style="width:${book.progress?.percent || 0}%"></i></div><div class="drawer-actions"><button class="primary" id="drawerOpen">继续阅读</button><button class="subtle" id="drawerFavorite">${favoriteText}</button><button class="subtle" id="drawerVocab">单词本</button><button class="subtle" data-view-jump="notes">笔记</button><button class="subtle" data-view-jump="red">红标</button><button class="subtle" id="drawerReveal">显示副本</button><button class="subtle" id="drawerExport">导出</button><button class="subtle" id="drawerManage">选择管理</button></div><section class="org-panel"><strong>自定义分类</strong><div class="org-row"><input id="categoryField" list="categoryList" value="${esc(org.custom_category || '')}" placeholder="例如：战略、英语、属灵书籍"><button class="primary" id="saveCategory">保存</button></div><datalist id="categoryList">${categoryOptions.map((name) => `<option value="${esc(name)}"></option>`).join('')}</datalist><strong>标签</strong><div class="org-row"><input id="tagField" value="${esc(tagText)}" placeholder="用逗号分隔，例如：面试,精读,复习"><button class="subtle" id="saveTags">保存</button></div>${chips}</section><details><summary>高级信息</summary><p>文件存在：${book.file?.exists ? '是' : '否'}<br>内部副本：${book.file?.owned_internal_copy ? '是' : '否'}<br>${esc(book.file?.file_path || '')}</p></details>`;
      $('drawer').classList.add('open');
      $('drawer').setAttribute('aria-hidden', 'false');
      $('drawerBackdrop').classList.add('show');
      $('drawerClose').onclick = closeDrawer;
      $('drawerOpen').onclick = () => openBook(book);
      $('drawerFavorite').onclick = () => toggleFavorite(book).catch((error) => toast(`收藏失败：${error.message}`));
      $('drawerVocab').onclick = () => { window.location.href = vocabURL(book.id); };
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
      toast('正在导入 EPUB...');
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
    $('manageToggle').onclick = () => {
      state.manageMode = !state.manageMode;
      if (!state.manageMode) state.selectedIds.clear();
      render();
    };
    $('selectAllCurrent').onclick = () => {
      const books = sortedBooks();
      const allSelected = books.length > 0 && books.every((book) => state.selectedIds.has(book.id));
      books.forEach((book) => allSelected ? state.selectedIds.delete(book.id) : state.selectedIds.add(book.id));
      renderLibrary();
    };
    $('batchClear').onclick = () => { state.selectedIds.clear(); renderLibrary(); };
    $('batchExit').onclick = () => { state.manageMode = false; state.selectedIds.clear(); render(); };
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
    try:
        return {"ok": True, "database": db.health()}
    except Exception as exc:  # noqa: BLE001 - health endpoint must expose boundary failures.
        return {"ok": False, "error": exc.__class__.__name__, "detail": str(exc)}


@app.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/library", response_class=HTMLResponse)
def library_page() -> HTMLResponse:
    return HTMLResponse(library_page_html_v2())


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
    return Response(content=generated_cover_svg(book), media_type="image/svg+xml")


@app.post("/api/library/import")
def post_library_import(payload: LibraryImport) -> dict[str, Any]:
    return import_library_epub(payload)


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
        conn.execute(
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
                None,
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
    start_lan_audio_note_transcription(audio_note_id, audio_path, payload.mime_type, len(audio_data))
    return {
        "ok": True,
        "accepted": True,
        "schema": "sentence_reader.lan_audio_transcription.v1",
        "audio_note_id": audio_note_id,
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
def create_book(payload: BookCreate) -> dict[str, Any]:
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
            (book_id, payload.title, payload.author, payload.source_kind, payload.book_hash),
        ).fetchone()
        if payload.file_path:
            conn.execute(
                """
                INSERT INTO reader.book_files (id, book_id, file_path, file_kind, file_hash, byte_size)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (book_id, file_path) DO UPDATE
                SET file_hash = EXCLUDED.file_hash,
                    byte_size = EXCLUDED.byte_size
                """,
                (new_id("file"), row["id"], payload.file_path, payload.source_kind, payload.file_hash, payload.byte_size),
            )
    return dict(row)


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
    if not audio_path.exists():
        proc = subprocess.run(
            [command, "--voice", voice, "--text", text, "--write-media", str(audio_path)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=45,
        )
        if proc.returncode != 0 or not audio_path.exists():
            detail = (proc.stderr or proc.stdout or "edge-tts failed").strip()
            return {
                "ok": False,
                "engine": "browser_speech_synthesis",
                "voice": voice,
                "audio_url": "",
                "text": text,
                "error": detail[:240],
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
    return {"ok": True, "cleaned_text": cleaned_text, "annotation": dict(updated)}


@app.delete("/annotations/{annotation_id}")
def delete_annotation(annotation_id: str) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute("DELETE FROM reader.annotations WHERE id = %s RETURNING id", (annotation_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="annotation not found")
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
    return dict(row)


@app.get("/audio-notes/{audio_note_id}")
def get_audio_note(audio_note_id: str) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM reader.audio_notes WHERE id = %s", (audio_note_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="audio note not found")
    return dict(row)


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
    if not row:
        raise HTTPException(status_code=404, detail="audio note not found")
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
