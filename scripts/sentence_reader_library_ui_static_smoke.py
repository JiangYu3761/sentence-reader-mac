#!/usr/bin/env python3
from __future__ import annotations

import sys
from contextlib import contextmanager
import os
from pathlib import Path
from typing import Any, Iterator

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import reader_api.app as app_module


SWIFT = ROOT / "Probe" / "NativeSentenceReader" / "SentenceReaderNative.swift"
APP = ROOT / "reader_api" / "app.py"
MIGRATION = ROOT / "migrations" / "reader" / "002_library_ui.sql"


class FakeCursor:
    def __init__(self, result: Any):
        self.result = result

    def fetchone(self) -> Any:
        return self.result

    def fetchall(self) -> list[Any]:
        return list(self.result or [])


class FakeConn:
    def __init__(self) -> None:
        self.hidden = False
        self.book = {
            "id": "book_library_smoke",
            "title": "Library UI Smoke",
            "author": "Sentence Reader",
            "source_kind": "epub",
            "book_hash": "library-ui-smoke",
            "created_at": "2026-06-26T00:00:00Z",
            "updated_at": "2026-06-26T00:00:00Z",
            "last_opened_at": "2026-06-26T00:00:00Z",
            "file_path": "/tmp/click-library-smoke-stale/book.epub",
            "file_kind": "epub",
            "file_hash": None,
            "byte_size": None,
            "file_candidates": [
                {
                    "file_path": "/tmp/click-library-smoke-stale/book.epub",
                    "file_kind": "epub",
                    "file_hash": None,
                    "byte_size": None,
                },
                {
                    "file_path": str(APP),
                    "file_kind": "epub",
                    "file_hash": "wrong-type",
                    "byte_size": APP.stat().st_size,
                },
                {
                    "file_path": str(ROOT / "fixtures" / "sentence-reader-smoke.epub"),
                    "file_kind": "epub",
                    "file_hash": "hash",
                    "byte_size": 1024,
                },
            ],
            "chapter_locator": "OEBPS/chapter.xhtml",
            "page_index": 2,
            "total_pages": 10,
            "page_ratio": 0.2,
            "position_updated_at": "2026-06-26T00:10:00Z",
            "annotation_count": 3,
            "note_count": 2,
            "red_count": 1,
            "audio_note_count": 1,
            "hidden": False,
            "library_metadata": {
                "favorite": True,
                "custom_category": "精读",
                "tags": ["测试", "分类"],
            },
        }
        self.annotation = {
            "id": "ann_library_smoke_note",
            "book_id": "book_library_smoke",
            "sentence_id": None,
            "kind": "note",
            "source_text": "真正的战略是集中力量解决关键问题。",
            "note_text": "这条可以转成读书后的行动判断。",
            "color": None,
            "chapter_title": "第一章",
            "chapter_locator": "OEBPS/chapter.xhtml",
            "range_locator": {"sentenceIndex": 2},
            "metadata": {},
            "created_at": "2026-06-26T00:10:00Z",
            "updated_at": "2026-06-26T00:12:00Z",
            "book_title": "Library UI Smoke",
            "book_author": "Sentence Reader",
        }

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> FakeCursor:
        sql = " ".join(query.lower().split())
        if "from reader.books b" in sql and "reader.library_state" in sql:
            if self.hidden and not params[0]:
                return FakeCursor([])
            row = {**self.book, "hidden": self.hidden}
            return FakeCursor([row])
        if "select id, title from reader.books where id = any(%s)" in sql:
            requested = set(params[0] if params else [])
            if self.book["id"] in requested:
                return FakeCursor([{"id": self.book["id"], "title": self.book["title"]}])
            return FakeCursor([])
        if "from reader.annotations a join reader.books b" in sql:
            return FakeCursor([] if self.hidden else [self.annotation])
        if "from reader.books b left join lateral" in sql and "where b.id = %s" in sql:
            return FakeCursor(self.book if params and params[0] == self.book["id"] else None)
        if "select metadata from reader.library_state where book_id = %s" in sql:
            return FakeCursor({"metadata": self.book.get("library_metadata") or {}})
        if "insert into reader.library_state" in sql and "set metadata = excluded.metadata" in sql:
            self.book["library_metadata"] = params[2]
            return FakeCursor(
                {
                    "book_id": params[0],
                    "hidden": self.hidden,
                    "source": params[1],
                    "metadata": params[2],
                    "created_at": "2026-06-26T00:00:00Z",
                    "updated_at": "2026-06-26T00:00:00Z",
                }
            )
        if "insert into reader.library_state" in sql:
            self.hidden = True
            return FakeCursor(
                {
                    "book_id": params[0],
                    "hidden": True,
                    "source": params[2],
                    "metadata": params[3],
                    "created_at": "2026-06-26T00:00:00Z",
                    "updated_at": "2026-06-26T00:00:00Z",
                }
            )
        raise AssertionError(f"unexpected SQL in library smoke: {query}")


@contextmanager
def fake_db(conn: FakeConn) -> Iterator[None]:
    original = app_module.db.connect
    original_jsonb = app_module.db.jsonb

    @contextmanager
    def connect() -> Iterator[FakeConn]:
        yield conn

    app_module.db.connect = connect  # type: ignore[assignment]
    app_module.db.jsonb = lambda value: value or {}  # type: ignore[assignment]
    try:
        yield
    finally:
        app_module.db.connect = original  # type: ignore[assignment]
        app_module.db.jsonb = original_jsonb  # type: ignore[assignment]


def require_markers(path: Path, markers: list[str]) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [marker for marker in markers if marker not in text]


def main() -> int:
    missing_files = [str(path) for path in (SWIFT, APP, MIGRATION) if not path.exists()]
    missing_markers: dict[str, list[str]] = {}
    for path, markers in {
        APP: [
            '@app.get("/library"',
            '@app.get("/api/library/dashboard")',
            '@app.get("/api/library/books/{book_id}/cover")',
            '@app.post("/api/library/import")',
            '@app.post("/api/library/books/{book_id}/hide")',
            '@app.post("/api/library/books/batch-organization")',
            '@app.patch("/api/library/books/{book_id}/organization")',
            '@app.post("/api/library/books/{book_id}/reveal")',
            "sentence_reader.library_dashboard.v1",
            "sentence_reader.library_batch_organization.v1",
            "library_v2",
            "library_page_html_v2",
            "data-library-v2",
            "continue-hero",
            "data-open-book-card",
            'aria-label="更多"',
            "manageMode",
            "manageToggle",
            "selectAllCurrent",
            "drawerManage",
            "homeFolderGrid",
            "data-library-scope",
            "normalizedFolderPath",
            "notesView",
            "redView",
            "batchHide",
            "batchFavorite",
            "batchOrganize",
            "batchExport",
            "epub_cover_asset",
            "generated_cover_svg",
            "sync_owned_epub_library",
            "SENTENCE_READER_SCAN_OWNED_EPUB_ON_DASHBOARD",
            "dashboard_read_path_is_side_effect_free",
            "preferred_existing_book_file",
            "owned_epub_scan",
            "native_reader_url",
            "get('surface') === 'mac-app'",
            "does_not_delete_notes",
        ],
        SWIFT: [
            "buildLibraryWebWindowController",
            "mainRootView",
            "libraryHomeWebView",
            "showMainLibrary",
            "hideMainLibraryForReading",
            "openNativeReaderFromLibraryBookID",
            "url.scheme == \"sentence-reader\"",
            "url.host == \"open-native\"",
            'URLQueryItem(name: "surface", value: "mac-app")',
            "nativeBookEntry(fromLibraryBook",
            "func libraryDashboard()",
            "SENTENCE_READER_API_BASE_URL",
            "ReaderAPIClient.configuredBaseURL().appendingPathComponent(\"library\")",
            "preferredIPadLibraryURL",
            "readerMoreButton",
            "showReaderMoreMenu",
            "WKUIDelegate",
            "runOpenPanelWith",
            "allowedContentTypes = [.epub, .pdf]",
            "旧书库表格仅作降级入口",
        ],
        MIGRATION: [
            "reader.library_state",
            "hidden BOOLEAN NOT NULL DEFAULT false",
            "Safe to re-run",
        ],
    }.items():
        missing = require_markers(path, markers)
        if missing:
            missing_markers[str(path)] = missing

    conn = FakeConn()
    os.environ["SENTENCE_READER_SKIP_OWNED_EPUB_SCAN"] = "1"
    with fake_db(conn):
        client = TestClient(app_module.app)
        page = client.get("/library")
        if page.status_code != 200:
            missing_markers["/library"] = [f"status={page.status_code}"]
        else:
            for marker in [
                "<title>Sentence Reader Library</title>",
                "data-library-v2",
                "Click",
                "Click",
                "打开封面继续阅读",
                "文件夹",
                "未整理",
                "data-library-scope",
                "收藏",
                "作者",
                "分类",
                "自定义分类",
                "笔记",
                "红标",
                "批量收藏",
                "批量分类",
                "批量导出",
                "移出书库",
                "管理模式",
                "退出管理",
                "选择管理",
                "搜索书名或作者",
            ]:
                if marker not in page.text:
                    missing_markers.setdefault("/library", []).append(marker)
            for forbidden in ["Reader API + PostgreSQL", "Tabler 只做", "Komga 只做", "iPad 访问", "状态与设置", "hero-manifest"]:
                if forbidden in page.text:
                    missing_markers.setdefault("/library", []).append(f"forbidden_visible:{forbidden}")
            for forbidden in ["card-actions", "drawerHide"]:
                if forbidden in page.text:
                    missing_markers.setdefault("/library", []).append(f"forbidden_hover_or_delete:{forbidden}")
            for forbidden in ["点击读懂", "brand-mark"]:
                if forbidden in page.text:
                    missing_markers.setdefault("/library", []).append(f"forbidden_brand:{forbidden}")

        dashboard = client.get("/api/library/dashboard")
        if dashboard.status_code != 200:
            missing_markers["/api/library/dashboard"] = [f"status={dashboard.status_code}"]
        else:
            payload = dashboard.json()
            if payload.get("schema") != "sentence_reader.library_dashboard.v1":
                missing_markers.setdefault("/api/library/dashboard", []).append("schema")
            if payload.get("ui_version") != "library_v2":
                missing_markers.setdefault("/api/library/dashboard", []).append("ui_version")
            if payload.get("summary", {}).get("book_count") != 1:
                missing_markers.setdefault("/api/library/dashboard", []).append("book_count")
            if payload.get("summary", {}).get("favorite_count") != 1:
                missing_markers.setdefault("/api/library/dashboard", []).append("favorite_count")
            if payload.get("books", [{}])[0].get("counts", {}).get("notes") != 2:
                missing_markers.setdefault("/api/library/dashboard", []).append("note_count")
            first_book = payload.get("books", [{}])[0]
            if first_book.get("organization", {}).get("category") != "精读":
                missing_markers.setdefault("/api/library/dashboard", []).append("organization_category")
            if "测试" not in first_book.get("organization", {}).get("tags", []):
                missing_markers.setdefault("/api/library/dashboard", []).append("organization_tags")
            if not first_book.get("cover", {}).get("url", "").endswith("/cover"):
                missing_markers.setdefault("/api/library/dashboard", []).append("cover")
            if first_book.get("reading_state") != "在读":
                missing_markers.setdefault("/api/library/dashboard", []).append("reading_state")
            if (
                first_book.get("file", {}).get("file_path")
                != str(ROOT / "fixtures" / "sentence-reader-smoke.epub")
                or first_book.get("file", {}).get("exists") is not True
                or first_book.get("file", {}).get("file_hash") != "hash"
                or first_book.get("file", {}).get("byte_size") != 1024
                or first_book.get("reader_capabilities", {}).get("mac_native") is not True
            ):
                missing_markers.setdefault("/api/library/dashboard", []).append(
                    "preferred_existing_book_file"
                )
            if not payload.get("recent_annotations"):
                missing_markers.setdefault("/api/library/dashboard", []).append("recent_annotations")
            if not payload.get("favorite_books") or not payload.get("author_groups") or not payload.get("category_groups"):
                missing_markers.setdefault("/api/library/dashboard", []).append("organization_groups")

        org = client.patch(
            "/api/library/books/book_library_smoke/organization",
            json={"favorite": False, "custom_category": "复盘", "tags": ["标签A"]},
        )
        if org.status_code != 200:
            missing_markers["/api/library/books/{book_id}/organization"] = [f"status={org.status_code}"]
        else:
            org_payload = org.json().get("organization", {})
            if org_payload.get("favorite") is not False or org_payload.get("category") != "复盘" or org_payload.get("tags") != ["标签A"]:
                missing_markers["/api/library/books/{book_id}/organization"] = ["payload"]
        batch_org = client.post(
            "/api/library/books/batch-organization",
            json={"book_ids": ["book_library_smoke"], "favorite": True, "custom_category": "批量", "tags": ["批量标签"]},
        )
        if batch_org.status_code != 200:
            missing_markers["/api/library/books/batch-organization"] = [f"status={batch_org.status_code}"]
        else:
            batch_payload = batch_org.json()
            if batch_payload.get("affected_count") != 1 or batch_payload.get("results", [{}])[0].get("organization", {}).get("category") != "批量":
                missing_markers["/api/library/books/batch-organization"] = ["payload"]

        cover = client.get("/api/library/books/book_library_smoke/cover")
        if cover.status_code != 200 or "image/" not in cover.headers.get("content-type", ""):
            missing_markers["/api/library/books/{book_id}/cover"] = [f"status={cover.status_code}", cover.headers.get("content-type", "")]

        hide = client.post("/api/library/books/book_library_smoke/hide")
        if hide.status_code != 200 or hide.json().get("non_destructive") is not True:
            missing_markers["/api/library/books/{book_id}/hide"] = [f"status={hide.status_code}", "non_destructive"]
        hidden_dashboard = client.get("/api/library/dashboard")
        if hidden_dashboard.status_code != 200 or hidden_dashboard.json().get("summary", {}).get("book_count") != 0:
            missing_markers.setdefault("/api/library/dashboard", []).append("hidden_filter")

    if missing_files or missing_markers:
        print(f"library ui static FAIL missing_files={missing_files} missing_markers={missing_markers}")
        return 1
    print("library ui static PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
