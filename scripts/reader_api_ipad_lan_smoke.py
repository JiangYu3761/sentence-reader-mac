#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import sys
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import reader_api.app as app_module


class FakeCursor:
    def __init__(self, result: Any):
        self.result = result

    def fetchone(self) -> Any:
        return self.result

    def fetchall(self) -> list[Any]:
        return list(self.result or [])


class FakeConn:
    def __init__(self, epub_path: Path):
        self.book = {
            "id": "book_lan_smoke",
            "title": "LAN Smoke Book",
            "author": "Sentence Reader",
            "source_kind": "epub",
            "book_hash": "lan-smoke-book-v1",
            "created_at": "2026-06-25T00:00:00Z",
            "updated_at": "2026-06-25T00:00:00Z",
            "last_opened_at": "2026-06-25T00:00:00Z",
            "file_path": str(epub_path),
            "file_kind": "epub",
            "file_hash": "lan-smoke-file-v1",
            "byte_size": epub_path.stat().st_size,
        }
        self.position = {
            "book_id": self.book["id"],
            "chapter_locator": "OEBPS/Text/chapter1.xhtml",
            "page_index": 0,
            "total_pages": 1,
            "page_ratio": 0,
            "locator": {"source": "lan_reader", "chapterIndex": 0},
            "updated_at": "2026-06-25T00:00:00Z",
        }
        self.annotations = [
            {
                "id": "ann_lan_red",
                "book_id": self.book["id"],
                "kind": "red_highlight",
                "source_text": "第一句：不要被冒号切断。",
                "note_text": None,
                "color": "red",
                "chapter_title": "第一章",
                "chapter_locator": "OEBPS/Text/chapter1.xhtml",
                "range_locator": {"sentenceIndex": "0"},
                "metadata": {"source": "test", "sentenceIndex": "0"},
                "created_at": "2026-06-25T00:00:00Z",
                "updated_at": "2026-06-25T00:00:00Z",
            }
        ]
        self.audio_notes: list[dict[str, Any]] = []

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> FakeCursor:
        sql = " ".join(query.lower().split())
        if "from reader.books b left join lateral" in sql:
            if "where b.id = %s" in sql and params and params[0] != self.book["id"]:
                return FakeCursor(None)
            if "where b.id = %s" in sql:
                return FakeCursor(self.book)
            return FakeCursor([self.book])
        if "select * from reader.reading_positions where book_id = %s" in sql:
            return FakeCursor(self.position if params and params[0] == self.book["id"] else None)
        if "select * from reader.annotations" in sql:
            return FakeCursor(self.annotations if params and params[0] == self.book["id"] else [])
        if "insert into reader.audio_notes" in sql:
            (
                audio_note_id,
                annotation_id,
                book_id,
                audio_path,
                audio_hash,
                duration_seconds,
                provider,
                transcript,
                raw_result,
                status,
                error_message,
            ) = params
            row = {
                "id": audio_note_id,
                "annotation_id": annotation_id,
                "book_id": book_id,
                "audio_path": audio_path,
                "audio_hash": audio_hash,
                "duration_seconds": duration_seconds,
                "provider": provider,
                "transcript": transcript,
                "raw_result": getattr(raw_result, "obj", raw_result),
                "status": status,
                "error_message": error_message,
                "created_at": "2026-06-25T00:00:00Z",
                "updated_at": "2026-06-25T00:00:00Z",
            }
            self.audio_notes.append(row)
            return FakeCursor(row)
        if "select * from reader.audio_notes where id = %s" in sql:
            return FakeCursor(next((row for row in self.audio_notes if row["id"] == params[0]), None))
        raise AssertionError(f"unexpected SQL in LAN smoke: {query}")


def write_epub(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr(
            "META-INF/container.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
""",
        )
        zf.writestr(
            "OEBPS/content.opf",
            """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>LAN Smoke Book</dc:title>
    <dc:creator>Sentence Reader</dc:creator>
  </metadata>
  <manifest>
    <item id="chapter1" href="Text/chapter1.xhtml" media-type="application/xhtml+xml"/>
    <item id="chapter2" href="Text/chapter2.xhtml" media-type="application/xhtml+xml"/>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="pixel" href="Images/pixel.svg" media-type="image/svg+xml"/>
  </manifest>
  <spine>
    <itemref idref="chapter1"/>
    <itemref idref="chapter2"/>
  </spine>
</package>
""",
        )
        zf.writestr(
            "OEBPS/nav.xhtml",
            """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
  <body>
    <nav epub:type="toc">
      <ol>
        <li><a href="Text/chapter1.xhtml">第一部分</a>
          <ol>
            <li><a href="Text/chapter2.xhtml">第二章 子层</a></li>
          </ol>
        </li>
      </ol>
    </nav>
  </body>
</html>
""",
        )
        zf.writestr(
            "OEBPS/Text/chapter1.xhtml",
            """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
  <head><title>第一章</title><script>alert('no')</script></head>
  <body>
    <h1>第一章</h1>
    <p>第一句：不要被冒号切断。第二句；分号也不该硬切。第三句！</p>
    <img src="../Images/pixel.svg"/>
  </body>
</html>
""",
        )
        zf.writestr(
            "OEBPS/Text/chapter2.xhtml",
            """<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>第二章</h1><p>继续阅读。</p></body></html>""",
        )
        zf.writestr("OEBPS/Images/pixel.svg", "<svg xmlns='http://www.w3.org/2000/svg' width='1' height='1'></svg>")


@contextmanager
def fake_db(epub_path: Path) -> Iterator[None]:
    original = app_module.db.connect
    conn_instance = FakeConn(epub_path)

    @contextmanager
    def connect() -> Iterator[FakeConn]:
        yield conn_instance

    app_module.db.connect = connect  # type: ignore[assignment]
    try:
        yield
    finally:
        app_module.db.connect = original  # type: ignore[assignment]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="sentence-reader-lan-smoke-") as tmp:
        epub_path = Path(tmp) / "lan-smoke.epub"
        write_epub(epub_path)
        with fake_db(epub_path):
            client = TestClient(app_module.app)
            page = client.get("/lan/reader")
            assert page.status_code == 200
            assert "Sentence Reader LAN" in page.text
            for marker in [
                "tocToggle",
                "drawer-open",
                "turnPage",
                "layoutPages",
                "measuredContentWidth",
                "startVoiceNote",
                "/lan/audio-notes/transcribe",
                "audioFile",
                "nativeReaderAudioAvailable",
                "ClickNativeAudio.startReaderNote(state.book.id)",
                "__clickNativeReaderAudioDidUpload",
                "lan_reader_paginated",
                "noteToast",
                "sentenceBar",
                "barRed",
                "barNote",
                "barVoice",
                "barCopy",
                "fontSettings",
                "settingsSheet",
                "sentenceReaderLanSettings",
                "goLibraryHome",
                "longPressTimer",
                "100dvh",
                "toc-row",
                "--toc-indent",
                "data-level",
                "showReaderLoadError",
                "请从书库打开一本书",
                "currentBookTocItems",
            ]:
                assert marker in page.text, marker
            assert "state.books.find((book) => book.lan_available)" not in page.text
            assert "|| state.books[0]" not in page.text

            books = client.get("/lan/books")
            assert books.status_code == 200
            books_json = books.json()
            assert books_json[0]["lan_available"] is True

            manifest = client.get("/lan/books/book_lan_smoke/manifest")
            assert manifest.status_code == 200
            manifest_json = manifest.json()
            assert manifest_json["schema"] == "sentence_reader.lan_manifest.v1"
            assert len(manifest_json["chapters"]) == 2
            assert manifest_json["toc"][0]["title"] == "第一部分"
            assert manifest_json["toc"][0]["level"] == 0
            assert manifest_json["toc"][0]["chapter_index"] == 0
            assert manifest_json["toc"][1]["title"] == "第二章 子层"
            assert manifest_json["toc"][1]["level"] == 1
            assert manifest_json["toc"][1]["chapter_index"] == 1

            chapter = client.get("/lan/books/book_lan_smoke/chapters/0")
            assert chapter.status_code == 200
            chapter_json = chapter.json()
            assert chapter_json["schema"] == "sentence_reader.lan_chapter.v1"
            assert "<script" not in chapter_json["html"].lower()
            assert "/lan/books/book_lan_smoke/asset/OEBPS/Images/pixel.svg" in chapter_json["html"]

            asset = client.get("/lan/books/book_lan_smoke/asset/OEBPS/Images/pixel.svg")
            assert asset.status_code == 200
            assert asset.headers["content-type"].startswith("image/svg+xml")

            traversal = client.get("/lan/books/book_lan_smoke/asset/../content.opf")
            assert traversal.status_code in {400, 404}

            original_app_support = app_module.sentence_reader_app_support_dir
            original_start = app_module.start_lan_audio_note_transcription

            def app_support() -> Path:
                return Path(tmp) / "app-support"

            def noop_start(*args: Any, **kwargs: Any) -> None:
                return None

            app_module.sentence_reader_app_support_dir = app_support  # type: ignore[assignment]
            app_module.start_lan_audio_note_transcription = noop_start  # type: ignore[assignment]
            try:
                voice = client.post(
                    "/lan/audio-notes/transcribe",
                    json={
                        "book_id": "book_lan_smoke",
                        "audio_base64": base64.b64encode(b"fake-audio").decode("ascii"),
                        "mime_type": "audio/webm",
                        "duration_seconds": 1.25,
                    },
                )
            finally:
                app_module.sentence_reader_app_support_dir = original_app_support  # type: ignore[assignment]
                app_module.start_lan_audio_note_transcription = original_start  # type: ignore[assignment]
            assert voice.status_code == 200
            voice_json = voice.json()
            assert voice_json["schema"] == "sentence_reader.lan_audio_transcription.v1"
            assert voice_json["accepted"] is True
            assert voice_json["async_processing"] is True
            assert voice_json["status"] == "pending"
            assert voice_json["provider"] == "mac_voice_pipeline"
            assert voice_json["voice_pipeline"]["schema"] == "click.mac_voice_pipeline.v1"
            assert voice_json["voice_pipeline"]["app_role"] == "capture_upload_only"
            assert voice_json["pending_text"] == "语音转写中..."
            assert voice_json["error_message"] == ""
            audio_file = Path(app_support()) / "AudioNotes" / "LAN" / f"{voice_json['audio_note_id']}.webm"
            assert audio_file.exists()
            audio_note = client.get(f"/audio-notes/{voice_json['audio_note_id']}")
            assert audio_note.status_code == 200
            assert audio_note.json()["status"] == "pending"

    print(json.dumps({"ok": True, "smoke": "reader api ipad lan smoke PASS"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
