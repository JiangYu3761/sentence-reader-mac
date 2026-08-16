#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx


BOOK_HASH_PREFIX = "reader-api-v16-export-smoke"
DEFAULT_BASE_URL = "http://127.0.0.1:18180"
DEFAULT_DATABASE_URL = "postgresql://localhost/sentence_reader"
ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = ROOT / ".venv-reader-api" / "bin" / "python"


def cleanup(database_url: str, book_id: Optional[str], book_hash: Optional[str]) -> dict[str, int]:
    try:
        import psycopg
    except ModuleNotFoundError:
        if Path(sys.executable).absolute() == VENV_PYTHON.absolute() or not VENV_PYTHON.is_file():
            raise RuntimeError("reader export smoke cleanup requires psycopg or .venv-reader-api")
        command = [
            str(VENV_PYTHON),
            str(Path(__file__).resolve()),
            "--cleanup-only",
            "--database-url",
            database_url,
        ]
        if book_id:
            command.extend(["--book-id", book_id])
        if book_hash:
            command.extend(["--book-hash", book_hash])
        subprocess.run(command, check=True)
        return {"delegated_to_reader_venv": 1}

    deleted_events = 0
    deleted_books = 0
    for _ in range(5):
        with psycopg.connect(database_url) as conn:
            fixture_book_ids: set[str] = set()
            if book_id:
                fixture_book_ids.add(book_id)
            if book_hash:
                rows = conn.execute(
                    "SELECT id FROM reader.books WHERE book_hash = %s AND book_hash LIKE %s",
                    (book_hash, f"{BOOK_HASH_PREFIX}-%"),
                ).fetchall()
                fixture_book_ids.update(str(row[0]) for row in rows)
            for fixture_book_id in fixture_book_ids:
                deleted_events += conn.execute(
                    """
                    DELETE FROM reader.sync_events
                    WHERE target_system = 'knowledge_base_living_book'
                      AND payload ->> 'book_id' = %s
                    """,
                    (fixture_book_id,),
                ).rowcount
                deleted_books += conn.execute(
                    "DELETE FROM reader.books WHERE id = %s AND book_hash LIKE %s",
                    (fixture_book_id, f"{BOOK_HASH_PREFIX}-%"),
                ).rowcount
            if book_hash:
                deleted_books += conn.execute(
                    "DELETE FROM reader.books WHERE book_hash = %s AND book_hash LIKE %s",
                    (book_hash, f"{BOOK_HASH_PREFIX}-%"),
                ).rowcount
            conn.commit()
        time.sleep(0.1)

    with psycopg.connect(database_url) as conn:
        if book_id:
            remaining = conn.execute(
                "SELECT count(*) FROM reader.books WHERE id = %s AND book_hash LIKE %s",
                (book_id, f"{BOOK_HASH_PREFIX}-%"),
            ).fetchone()[0]
            remaining_events = conn.execute(
                """
                SELECT count(*) FROM reader.sync_events
                WHERE target_system = 'knowledge_base_living_book'
                  AND payload ->> 'book_id' = %s
                """,
                (book_id,),
            ).fetchone()[0]
            if remaining or remaining_events:
                raise RuntimeError("reader export smoke cleanup left fixture database rows")
    return {"deleted_books": deleted_books, "deleted_events": deleted_events}


def cleanup_all_fixtures(database_url: str) -> dict[str, int]:
    try:
        import psycopg
    except ModuleNotFoundError:
        if Path(sys.executable).absolute() == VENV_PYTHON.absolute() or not VENV_PYTHON.is_file():
            raise RuntimeError("reader export smoke cleanup requires psycopg or .venv-reader-api")
        subprocess.run(
            [
                str(VENV_PYTHON),
                str(Path(__file__).resolve()),
                "--cleanup-all-fixtures",
                "--database-url",
                database_url,
            ],
            check=True,
        )
        return {"delegated_to_reader_venv": 1}

    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            "SELECT id, book_hash FROM reader.books WHERE book_hash LIKE %s",
            (f"{BOOK_HASH_PREFIX}-%",),
        ).fetchall()
    totals = {"deleted_books": 0, "deleted_events": 0}
    for fixture_book_id, fixture_book_hash in rows:
        result = cleanup(database_url, str(fixture_book_id), str(fixture_book_hash))
        totals["deleted_books"] += result.get("deleted_books", 0)
        totals["deleted_events"] += result.get("deleted_events", 0)
    return totals


def assert_ok(response: httpx.Response, label: str) -> dict | list:
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(f"{label} failed: status={response.status_code} body={response.text}")
    return response.json()


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test the V1.6 export API contract.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument("--cleanup-only", action="store_true")
    parser.add_argument("--cleanup-all-fixtures", action="store_true")
    parser.add_argument("--book-id")
    parser.add_argument("--book-hash")
    args = parser.parse_args()

    if args.cleanup_only:
        result = cleanup(args.database_url, args.book_id, args.book_hash)
        print(f"reader api export smoke cleanup PASS {result}")
        return 0
    if args.cleanup_all_fixtures:
        result = cleanup_all_fixtures(args.database_url)
        print(f"reader api export smoke full cleanup PASS {result}")
        return 0

    book_hash = f"{BOOK_HASH_PREFIX}-{uuid.uuid4().hex}"
    book_id: Optional[str] = None
    try:
        with tempfile.TemporaryDirectory(prefix="sentence-reader-v16-export-") as tmp:
            output_dir = Path(tmp)
            with httpx.Client(base_url=args.base_url, timeout=5.0) as client:
                health = assert_ok(client.get("/health"), "health")
                if not isinstance(health, dict) or not health.get("ok"):
                    raise RuntimeError(f"health returned not ok: {health}")

                book = assert_ok(
                    client.post(
                        "/books",
                        json={
                            "title": "V1.6 Export Smoke",
                            "author": "Codex",
                            "source_kind": "epub",
                            "book_hash": book_hash,
                            "file_path": "/tmp/sentence-reader-v16-export.epub",
                        },
                    ),
                    "book create",
                )
                assert isinstance(book, dict)
                book_id = book["id"]

                note = assert_ok(
                    client.post(
                        "/annotations",
                        json={
                            "book_id": book_id,
                            "kind": "note",
                            "source_text": "Export must preserve source sentence text.",
                            "note_text": "Portable note text.",
                            "chapter_title": "Export Chapter",
                            "chapter_locator": "export/chapter.xhtml",
                            "range_locator": {"sentenceIndex": "8"},
                            "metadata": {"sentenceIndex": "8"},
                        },
                    ),
                    "note create",
                )
                assert isinstance(note, dict)

                red = assert_ok(
                    client.post(
                        "/annotations",
                        json={
                            "book_id": book_id,
                            "kind": "red_highlight",
                            "source_text": "Export must also include red highlights.",
                            "color": "red",
                            "chapter_title": "Export Chapter",
                            "chapter_locator": "export/chapter.xhtml",
                            "range_locator": {"sentenceIndex": "9"},
                            "metadata": {"sentenceIndex": "9"},
                        },
                    ),
                    "red create",
                )
                assert isinstance(red, dict)

                export = assert_ok(
                    client.post(
                        f"/books/{book_id}/export",
                        json={"output_dir": str(output_dir), "include_json": True},
                    ),
                    "export generate",
                )
                assert isinstance(export, dict)
                if export.get("annotation_count") != 2:
                    raise RuntimeError(f"expected 2 annotations exported, got {export}")

                markdown_path = Path(export["markdown_path"])
                json_path = Path(export["json_path"])
                if not markdown_path.exists() or not json_path.exists():
                    raise RuntimeError(f"export files missing: {export}")
                markdown_text = markdown_path.read_text(encoding="utf-8")
                json_payload = json.loads(json_path.read_text(encoding="utf-8"))
                if "Export must preserve source sentence text." not in markdown_text:
                    raise RuntimeError("markdown export missing note source text")
                if "Export must also include red highlights." not in markdown_text:
                    raise RuntimeError("markdown export missing red highlight text")
                if json_payload.get("schema") != "sentence_reader.annotations_export.v1":
                    raise RuntimeError(f"json export schema mismatch: {json_payload}")

                exports = assert_ok(client.get(f"/books/{book_id}/exports"), "export list")
                assert isinstance(exports, list)
                kinds = {item["export_kind"] for item in exports}
                if kinds != {"markdown", "json"}:
                    raise RuntimeError(f"expected markdown/json export records, got {exports}")

        print("reader api export smoke PASS")
        return 0
    except Exception as exc:
        print(f"reader api export smoke FAIL: {exc}")
        return 1
    finally:
        cleanup(args.database_url, book_id, book_hash)


if __name__ == "__main__":
    raise SystemExit(main())
