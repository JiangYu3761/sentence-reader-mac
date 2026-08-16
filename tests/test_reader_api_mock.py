from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

import reader_api.app as app_module
import reader_api.db as db_module


class FakeCursor:
    def __init__(self, result: Any):
        self.result = result

    def fetchone(self) -> Any:
        return self.result

    def fetchall(self) -> list[Any]:
        return list(self.result or [])


class FakeConn:
    def __init__(self, state: "FakeReaderDb"):
        self.state = state

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> FakeCursor:
        sql = " ".join(query.lower().split())

        if "insert into reader.books" in sql:
            book_id, title, author, source_kind, book_hash = params
            row = next((item for item in self.state.books.values() if item["book_hash"] == book_hash), None)
            if row:
                row.update({"title": title, "author": author, "source_kind": source_kind})
            else:
                row = {
                    "id": book_id,
                    "title": title,
                    "author": author,
                    "source_kind": source_kind,
                    "book_hash": book_hash,
                    "created_at": self.state.now,
                    "updated_at": self.state.now,
                    "last_opened_at": self.state.now,
                }
                self.state.books[book_id] = row
            return FakeCursor(row)

        if "insert into reader.book_files" in sql:
            file_id, book_id, file_path, file_kind, file_hash, byte_size = params
            self.state.book_files[(book_id, file_path)] = {
                "id": file_id,
                "book_id": book_id,
                "file_path": file_path,
                "file_kind": file_kind,
                "file_hash": file_hash,
                "byte_size": byte_size,
                "created_at": self.state.now,
            }
            return FakeCursor({"ok": True})

        if "from reader.books b left join lateral" in sql:
            rows: list[dict[str, Any]] = []
            for book in self.state.books.values():
                files = [item for item in self.state.book_files.values() if item["book_id"] == book["id"]]
                file = files[-1] if files else {}
                rows.append(
                    {
                        **book,
                        "file_path": file.get("file_path"),
                        "file_kind": file.get("file_kind"),
                        "file_hash": file.get("file_hash"),
                        "byte_size": file.get("byte_size"),
                    }
                )
            rows.sort(key=lambda row: row["last_opened_at"], reverse=True)
            return FakeCursor(rows)

        if "select * from reader.books where id = %s" in sql:
            return FakeCursor(self.state.books.get(params[0]))

        if "insert into reader.reading_positions" in sql:
            book_id, chapter_id, chapter_locator, page_index, total_pages, page_ratio, locator = params
            row = {
                "book_id": book_id,
                "chapter_id": chapter_id,
                "chapter_locator": chapter_locator,
                "page_index": page_index,
                "total_pages": total_pages,
                "page_ratio": page_ratio,
                "locator": locator,
                "updated_at": self.state.now,
            }
            self.state.positions[book_id] = row
            return FakeCursor(row)

        if "select * from reader.reading_positions where book_id = %s" in sql:
            return FakeCursor(self.state.positions.get(params[0]))

        if "insert into reader.sentences" in sql:
            sentence_id, book_id, chapter_id, chapter_locator, sentence_index, sentence_hash, text, locator = params
            row = {
                "id": sentence_id,
                "book_id": book_id,
                "chapter_id": chapter_id,
                "chapter_locator": chapter_locator,
                "sentence_index": sentence_index,
                "sentence_text_hash": sentence_hash,
                "text": text,
                "range_locator": locator,
                "created_at": self.state.now,
            }
            self.state.sentences[sentence_id] = row
            return FakeCursor(row)

        if "insert into reader.annotations" in sql:
            (
                annotation_id,
                book_id,
                sentence_id,
                kind,
                source_text,
                note_text,
                color,
                chapter_title,
                chapter_locator,
                range_locator,
                metadata,
            ) = params
            row = {
                "id": annotation_id,
                "book_id": book_id,
                "sentence_id": sentence_id,
                "kind": kind,
                "source_text": source_text,
                "note_text": note_text,
                "color": color,
                "chapter_title": chapter_title,
                "chapter_locator": chapter_locator,
                "range_locator": range_locator,
                "metadata": metadata,
                "created_at": self.state.now,
                "updated_at": self.state.now,
            }
            self.state.annotations[annotation_id] = row
            return FakeCursor(row)

        if "select * from reader.annotations" in sql:
            book_id = params[0]
            rows = [row for row in self.state.annotations.values() if row["book_id"] == book_id]
            rows.sort(key=lambda row: (row["chapter_locator"], row["created_at"]))
            return FakeCursor(rows)

        if "update reader.annotations" in sql:
            note_text, color, metadata, annotation_id = params
            row = self.state.annotations.get(annotation_id)
            if row:
                if note_text is not None:
                    row["note_text"] = note_text
                if color is not None:
                    row["color"] = color
                if metadata is not None:
                    row["metadata"] = metadata
                row["updated_at"] = self.state.now
            return FakeCursor(row)

        if "delete from reader.annotations" in sql:
            annotation_id = params[0]
            row = self.state.annotations.pop(annotation_id, None)
            return FakeCursor(
                {
                    "id": annotation_id,
                    "book_id": row["book_id"],
                    "kind": row["kind"],
                    "chapter_locator": row["chapter_locator"],
                }
                if row
                else None
            )

        if "insert into reader.exports" in sql:
            export_id, book_id, export_kind, output_path, annotation_count = params
            row = {
                "id": export_id,
                "book_id": book_id,
                "export_kind": export_kind,
                "output_path": output_path,
                "annotation_count": annotation_count,
                "created_at": self.state.now,
            }
            self.state.exports[export_id] = row
            return FakeCursor(row)

        if "select * from reader.exports" in sql:
            book_id = params[0]
            rows = [row for row in self.state.exports.values() if row["book_id"] == book_id]
            rows.sort(key=lambda row: row["created_at"], reverse=True)
            return FakeCursor(rows)

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
                "raw_result": raw_result,
                "status": status,
                "error_message": error_message,
                "created_at": self.state.now,
                "updated_at": self.state.now,
            }
            self.state.audio_notes[audio_note_id] = row
            return FakeCursor(row)

        if "update reader.audio_notes" in sql:
            (
                annotation_id,
                audio_hash,
                duration_seconds,
                provider,
                transcript,
                raw_result,
                status,
                error_message,
                audio_note_id,
            ) = params
            row = self.state.audio_notes.get(audio_note_id)
            if row:
                if annotation_id is not None:
                    row["annotation_id"] = annotation_id
                if audio_hash is not None:
                    row["audio_hash"] = audio_hash
                if duration_seconds is not None:
                    row["duration_seconds"] = duration_seconds
                if provider is not None:
                    row["provider"] = provider
                if transcript is not None:
                    row["transcript"] = transcript
                if raw_result is not None:
                    row["raw_result"] = raw_result
                if status is not None:
                    row["status"] = status
                row["error_message"] = error_message
                row["updated_at"] = self.state.now
            return FakeCursor(row)

        if "select * from reader.audio_notes" in sql:
            book_id = params[0]
            rows = [row for row in self.state.audio_notes.values() if row["book_id"] == book_id]
            rows.sort(key=lambda row: row["created_at"], reverse=True)
            return FakeCursor(rows)

        if "insert into reader.sync_events" in sql:
            sync_id, source_kind, source_id, target_system, payload, status, last_error = params
            row = {
                "id": sync_id,
                "source_kind": source_kind,
                "source_id": source_id,
                "target_system": target_system,
                "payload": payload,
                "status": status,
                "last_error": last_error,
                "created_at": self.state.now,
                "updated_at": self.state.now,
            }
            self.state.sync_events[sync_id] = row
            return FakeCursor(row)

        if "select * from reader.sync_events" in sql and "where target_system = %s and status = %s" in sql:
            target_system, status, limit = params
            rows = [
                row
                for row in self.state.sync_events.values()
                if row["target_system"] == target_system and row["status"] == status
            ]
            rows.sort(key=lambda row: row["created_at"])
            return FakeCursor(rows[:limit])

        if "select * from reader.sync_events" in sql and "where id = any(%s)" in sql:
            sync_ids, target_system, status = params
            ids = set(sync_ids)
            rows = [
                row
                for row in self.state.sync_events.values()
                if row["id"] in ids and row["target_system"] == target_system and row["status"] == status
            ]
            rows.sort(key=lambda row: row["created_at"])
            return FakeCursor(rows)

        if "update reader.sync_events" in sql:
            payload, status, last_error, sync_id = params
            row = self.state.sync_events.get(sync_id)
            if row:
                row["payload"] = payload
                row["status"] = status
                row["last_error"] = last_error
                row["updated_at"] = self.state.now
            return FakeCursor(row)

        if "select * from reader.sync_events" in sql:
            source_kind, source_id = params
            rows = [
                row
                for row in self.state.sync_events.values()
                if row["source_kind"] == source_kind and row["source_id"] == source_id
            ]
            rows.sort(key=lambda row: row["created_at"], reverse=True)
            return FakeCursor(rows)

        raise AssertionError(f"unhandled query in fake reader db: {sql}")


@dataclass
class FakeReaderDb:
    now: str = "2026-06-24T00:00:00+00:00"
    books: dict[str, dict[str, Any]] = field(default_factory=dict)
    book_files: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    sentences: dict[str, dict[str, Any]] = field(default_factory=dict)
    annotations: dict[str, dict[str, Any]] = field(default_factory=dict)
    positions: dict[str, dict[str, Any]] = field(default_factory=dict)
    exports: dict[str, dict[str, Any]] = field(default_factory=dict)
    audio_notes: dict[str, dict[str, Any]] = field(default_factory=dict)
    sync_events: dict[str, dict[str, Any]] = field(default_factory=dict)

    @contextmanager
    def connect(self) -> Iterator[FakeConn]:
        yield FakeConn(self)


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    fake = FakeReaderDb()
    monkeypatch.setattr(app_module.db, "connect", fake.connect)
    monkeypatch.setattr(app_module.db, "jsonb", lambda value: {} if value is None else value)
    monkeypatch.setattr(
        app_module.db,
        "health",
        lambda: {"ok": True, "database": "sentence_reader", "schema": "reader"},
    )
    return TestClient(app_module.app)


def test_health_uses_reader_database_contract(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["schema"] == "click.reader_api.health.v2"
    assert payload["database"] == {"ok": True, "database": "sentence_reader", "schema": "reader"}
    assert payload["runtime"]["contract"] == "click.reader_runtime.v1"
    assert payload["runtime"]["api_revision"] >= 2
    assert "voice.inbox.v2" in payload["runtime"]["capabilities"]


def test_database_health_binds_instance_without_exposing_raw_server_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = {
        "database": "sentence_reader",
        "schema": "reader",
        "server_addr": "::1/128",
        "server_port": 5432,
        "system_identifier": "7654321098765432109",
    }

    class HealthConn:
        def execute(self, query: str) -> FakeCursor:
            assert "pg_control_system()" in query
            return FakeCursor(row)

    @contextmanager
    def fake_connect() -> Iterator[HealthConn]:
        yield HealthConn()

    monkeypatch.setattr(db_module, "connect", fake_connect)
    result = db_module.health()
    expected = db_module.postgresql_instance_fingerprint(
        row["database"],
        row["system_identifier"],
    )

    assert result == {
        "ok": True,
        "database": "sentence_reader",
        "schema": "reader",
        "instance_fingerprint": expected,
    }
    assert len(expected) == 64
    assert expected != db_module.postgresql_instance_fingerprint(
        "other_reader_database",
        row["system_identifier"],
    )
    assert not {"server_addr", "server_port", "system_identifier"} & result.keys()


def test_book_sentence_note_position_crud_flow(client: TestClient) -> None:
    book = client.post(
        "/books",
        json={
            "title": "Good Strategy Bad Strategy",
            "author": "Richard Rumelt",
            "source_kind": "epub",
            "book_hash": "fixture-book-hash",
        },
    ).json()

    assert book["id"].startswith("book_")

    fetched_book = client.get(f"/books/{book['id']}")
    assert fetched_book.status_code == 200
    assert fetched_book.json()["title"] == "Good Strategy Bad Strategy"

    sentence = client.post(
        "/sentences",
        json={
            "book_id": book["id"],
            "chapter_locator": "chapter-1",
            "sentence_index": 1,
            "sentence_text_hash": "sentence-hash",
            "text": "Strategy is a coherent response to a challenge.",
            "range_locator": {"href": "chapter1.xhtml"},
        },
    ).json()

    note = client.post(
        "/annotations",
        json={
            "book_id": book["id"],
            "sentence_id": sentence["id"],
            "kind": "note",
            "source_text": sentence["text"],
            "note_text": "This is the exact unit Sentence Reader must persist.",
            "chapter_title": "Chapter 1",
            "chapter_locator": "chapter-1",
            "range_locator": {"href": "chapter1.xhtml"},
        },
    ).json()

    position = client.put(
        f"/books/{book['id']}/position",
        json={
            "chapter_locator": "chapter-1",
            "page_index": 2,
            "total_pages": 10,
            "page_ratio": 0.2,
            "locator": {"href": "chapter1.xhtml", "page": 3},
        },
    )
    assert position.status_code == 200
    assert position.json()["page_index"] == 2

    fetched_position = client.get(f"/books/{book['id']}/position")
    assert fetched_position.status_code == 200
    assert fetched_position.json()["locator"]["page"] == 3

    annotations = client.get(f"/books/{book['id']}/annotations")
    assert annotations.status_code == 200
    assert len(annotations.json()) == 1

    patched = client.patch(
        f"/annotations/{note['id']}",
        json={"note_text": "Updated note", "metadata": {"voice": False}},
    )
    assert patched.status_code == 200
    assert patched.json()["note_text"] == "Updated note"
    assert patched.json()["metadata"]["voice"] is False

    deleted = client.delete(f"/annotations/{note['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["ok"] is True

    assert client.get(f"/books/{book['id']}/annotations").json() == []


def test_red_highlight_and_export_flow(client: TestClient) -> None:
    book = client.post(
        "/books",
        json={"title": "Highlight Book", "source_kind": "epub", "book_hash": "highlight-book-hash"},
    ).json()

    highlight = client.post(
        "/annotations",
        json={
            "book_id": book["id"],
            "kind": "red_highlight",
            "source_text": "The sentence is the smallest action unit.",
            "color": "red",
            "chapter_locator": "chapter-2",
            "range_locator": {"href": "chapter2.xhtml"},
        },
    )

    assert highlight.status_code == 200
    assert highlight.json()["kind"] == "red_highlight"
    assert highlight.json()["color"] == "red"

    export = client.post(
        "/exports",
        json={
            "book_id": book["id"],
            "export_kind": "markdown",
            "output_path": "/tmp/sentence-reader/export.md",
            "annotation_count": 1,
        },
    )

    assert export.status_code == 200
    assert export.json()["export_kind"] == "markdown"
    assert export.json()["annotation_count"] == 1


def test_generated_markdown_json_export_flow(client: TestClient, tmp_path: Path) -> None:
    book = client.post(
        "/books",
        json={"title": "Export Book", "author": "Codex", "source_kind": "epub", "book_hash": "export-book-hash"},
    ).json()

    client.post(
        "/annotations",
        json={
            "book_id": book["id"],
            "kind": "note",
            "source_text": "Export should preserve the original sentence.",
            "note_text": "This note should be portable.",
            "chapter_title": "Export Chapter",
            "chapter_locator": "export-chapter.xhtml",
            "range_locator": {"sentenceIndex": "5"},
            "metadata": {"sentenceIndex": "5"},
        },
    )

    response = client.post(
        f"/books/{book['id']}/export",
        json={"output_dir": str(tmp_path), "include_json": True},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["annotation_count"] == 1
    markdown_path = Path(payload["markdown_path"])
    json_path = Path(payload["json_path"])
    assert markdown_path.exists()
    assert json_path.exists()
    assert "Export should preserve the original sentence." in markdown_path.read_text(encoding="utf-8")
    assert "sentence_reader.annotations_export.v1" in json_path.read_text(encoding="utf-8")

    exports = client.get(f"/books/{book['id']}/exports")
    assert exports.status_code == 200
    assert {item["export_kind"] for item in exports.json()} == {"markdown", "json"}


def test_audio_note_lifecycle(client: TestClient, tmp_path: Path) -> None:
    book = client.post(
        "/books",
        json={"title": "Voice Book", "source_kind": "epub", "book_hash": "voice-book-hash"},
    ).json()
    audio_path = tmp_path / "note.wav"
    audio_path.write_bytes(b"fake wav bytes")

    pending = client.post(
        "/audio-notes",
        json={
            "book_id": book["id"],
            "audio_path": str(audio_path),
            "duration_seconds": 2.5,
            "provider": "funasr",
            "status": "pending",
        },
    )
    assert pending.status_code == 200
    assert pending.json()["status"] == "pending"

    annotation = client.post(
        "/annotations",
        json={
            "book_id": book["id"],
            "kind": "note",
            "source_text": "Voice note source.",
            "note_text": "Transcribed text.",
            "chapter_locator": "voice.xhtml",
        },
    ).json()
    patched = client.patch(
        f"/audio-notes/{pending.json()['id']}",
        json={
            "annotation_id": annotation["id"],
            "provider": "funasr",
            "transcript": "Transcribed text.",
            "status": "transcribed",
            "raw_result": {"segments": 1},
        },
    )
    assert patched.status_code == 200
    assert patched.json()["annotation_id"] == annotation["id"]
    assert patched.json()["status"] == "transcribed"
    assert patched.json()["transcript"] == "Transcribed text."

    listed = client.get(f"/books/{book['id']}/audio-notes")
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert listed.json()[0]["raw_result"]["segments"] == 1


def test_multi_book_list_and_independent_state(client: TestClient) -> None:
    first = client.post(
        "/books",
        json={
            "title": "First EPUB",
            "source_kind": "epub",
            "book_hash": "first-book-hash",
        },
    ).json()
    second = client.post(
        "/books",
        json={
            "title": "Second EPUB",
            "source_kind": "epub",
            "book_hash": "second-book-hash",
        },
    ).json()

    books = client.get("/books")
    assert books.status_code == 200
    assert {item["book_hash"] for item in books.json()} == {"first-book-hash", "second-book-hash"}
    assert {item["file_path"] for item in books.json()} == {None}

    client.put(
        f"/books/{first['id']}/position",
        json={"chapter_locator": "a.xhtml", "page_index": 1, "total_pages": 9, "page_ratio": 0.125},
    )
    client.put(
        f"/books/{second['id']}/position",
        json={"chapter_locator": "b.xhtml", "page_index": 4, "total_pages": 10, "page_ratio": 0.444},
    )
    client.post(
        "/annotations",
        json={
            "book_id": first["id"],
            "kind": "note",
            "source_text": "First book sentence.",
            "note_text": "Only first book.",
            "chapter_locator": "a.xhtml",
        },
    )
    client.post(
        "/annotations",
        json={
            "book_id": second["id"],
            "kind": "red_highlight",
            "source_text": "Second book sentence.",
            "color": "red",
            "chapter_locator": "b.xhtml",
        },
    )

    assert client.get(f"/books/{first['id']}/position").json()["chapter_locator"] == "a.xhtml"
    assert client.get(f"/books/{second['id']}/position").json()["chapter_locator"] == "b.xhtml"
    assert client.get(f"/books/{first['id']}/annotations").json()[0]["kind"] == "note"
    assert client.get(f"/books/{second['id']}/annotations").json()[0]["kind"] == "red_highlight"


def test_hermes_sync_payload_and_event_flow(client: TestClient, tmp_path: Path) -> None:
    book = client.post(
        "/books",
        json={
            "title": "Cognitive Sync Book",
            "author": "Codex",
            "source_kind": "epub",
            "book_hash": "cognitive-sync-book-hash",
        },
    ).json()

    note = client.post(
        "/annotations",
        json={
            "book_id": book["id"],
            "kind": "note",
            "source_text": "A useful reading system preserves evidence and interpretation separately.",
            "note_text": "Evidence should stay tied to the exact source sentence.",
            "chapter_title": "Evidence",
            "chapter_locator": "evidence.xhtml",
            "range_locator": {"sentenceIndex": "3"},
            "metadata": {"sentenceIndex": "3"},
        },
    ).json()
    client.post(
        "/annotations",
        json={
            "book_id": book["id"],
            "kind": "red_highlight",
            "source_text": "Red highlights should also be syncable as low-friction signals.",
            "color": "red",
            "chapter_title": "Signals",
            "chapter_locator": "signals.xhtml",
            "range_locator": {"sentenceIndex": "4"},
            "metadata": {"sentenceIndex": "4"},
        },
    )

    response = client.post(
        f"/books/{book['id']}/sync/hermes",
        json={"output_dir": str(tmp_path), "annotation_ids": [note["id"]]},
    )

    assert response.status_code == 200
    result = response.json()
    assert result["ok"] is True
    assert result["target_system"] == "hermes_cognitive_os"
    assert result["status"] == "pending"
    assert result["annotation_count"] == 1

    payload_path = Path(result["payload_path"])
    assert payload_path.exists()
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["schema"] == "sentence_reader.hermes_sync.v1"
    assert payload["book"]["title"] == "Cognitive Sync Book"
    assert payload["annotation_count"] == 1
    assert payload["annotations"][0]["id"] == note["id"]
    assert payload["annotations"][0]["evidence_unit"]["locator"]["sentence_index"] == "3"
    assert "Use source_sentence as evidence" in " ".join(payload["cognitive_contract"]["rules"])

    events = client.get(f"/books/{book['id']}/sync-events")
    assert events.status_code == 200
    assert len(events.json()) == 1
    event = events.json()[0]
    assert event["target_system"] == "hermes_cognitive_os"
    assert event["status"] == "pending"
    assert event["payload"]["schema"] == "sentence_reader.hermes_sync.v1"
    assert event["payload"]["payload_path"] == str(payload_path)


def test_hermes_ingestion_worker_marks_pending_event_synced(client: TestClient, tmp_path: Path) -> None:
    book = client.post(
        "/books",
        json={
            "title": "Ingestion Book",
            "author": "Codex",
            "source_kind": "epub",
            "book_hash": "ingestion-book-hash",
        },
    ).json()
    client.post(
        "/annotations",
        json={
            "book_id": book["id"],
            "kind": "note",
            "source_text": "Ingestion should move source assets without mutating the active pack.",
            "note_text": "Keep the asset as evidence until a human promotes it.",
            "chapter_title": "Ingestion",
            "chapter_locator": "ingestion.xhtml",
            "range_locator": {"sentenceIndex": "6"},
            "metadata": {"sentenceIndex": "6"},
        },
    )

    sync = client.post(
        f"/books/{book['id']}/sync/hermes",
        json={"output_dir": str(tmp_path / "sync")},
    ).json()
    assert sync["status"] == "pending"

    response = client.post(
        "/sync/hermes/ingest",
        json={
            "cognitive_os_dir": str(tmp_path / "hermes_cognitive_os"),
            "sync_event_ids": [sync["sync_event"]["id"]],
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["ok"] is True
    assert result["attempted"] == 1
    assert result["synced_count"] == 1
    assert result["failed_count"] == 0

    ingested = result["events"][0]
    payload_path = Path(ingested["payload_path"])
    manifest_path = Path(ingested["manifest_path"])
    assert payload_path.exists()
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema"] == "sentence_reader.hermes_ingestion_manifest.v1"
    assert manifest["policy"]["active_pack_mutation"] is False
    assert manifest["target"]["queue"] == "incoming/sentence_reader"

    events = client.get(f"/books/{book['id']}/sync-events").json()
    assert events[0]["status"] == "synced"
    assert events[0]["payload"]["ingested_payload_path"] == str(payload_path)
    assert events[0]["payload"]["ingestion_manifest_path"] == str(manifest_path)
