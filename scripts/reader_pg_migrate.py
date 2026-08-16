#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path
from urllib.parse import urlparse, urlunparse


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_DIR = ROOT / "migrations" / "reader"
MIGRATION = MIGRATION_DIR / "001_reader_schema.sql"
DEFAULT_DATABASE_URL = "postgresql://localhost/sentence_reader"


def _load_psycopg():
    try:
        import psycopg
        from psycopg import sql
    except ImportError as exc:
        raise SystemExit(
            "reader pg migrate SKIP: psycopg is not installed. "
            "Install requirements-reader-api.txt after PostgreSQL is available."
        ) from exc
    return psycopg, sql


def database_url_from_args(args: argparse.Namespace) -> str:
    return (
        args.database_url
        or os.getenv("READER_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or DEFAULT_DATABASE_URL
    )


def maintenance_url(database_url: str) -> tuple[str, str]:
    parsed = urlparse(database_url)
    db_name = parsed.path.lstrip("/") or "sentence_reader"
    maintenance = parsed._replace(path="/postgres")
    return urlunparse(maintenance), db_name


def create_database_if_missing(database_url: str) -> None:
    psycopg, sql = _load_psycopg()
    maintenance, db_name = maintenance_url(database_url)
    try:
        with psycopg.connect(maintenance, autocommit=True) as conn:
            exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,)).fetchone()
            if not exists:
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
                print(f"created database {db_name}")
    except psycopg.OperationalError as exc:
        raise SystemExit(
            "reader pg migrate BLOCKED: PostgreSQL is not reachable. "
            f"database_url={database_url} detail={exc}"
        ) from exc


def apply_migration(database_url: str) -> None:
    psycopg, _ = _load_psycopg()
    migration_paths = sorted(MIGRATION_DIR.glob("*.sql"))
    if not migration_paths:
        raise SystemExit(f"reader pg migrate BLOCKED: no migrations found in {MIGRATION_DIR}")
    try:
        with psycopg.connect(database_url) as conn:
            for migration_path in migration_paths:
                conn.execute(migration_path.read_text(encoding="utf-8"))
            conn.commit()
    except psycopg.OperationalError as exc:
        raise SystemExit(
            "reader pg migrate BLOCKED: PostgreSQL is not reachable. "
            f"database_url={database_url} detail={exc}"
        ) from exc
    print(
        "reader pg migrate PASS "
        f"migrations={[path.name for path in migration_paths]} "
        f"database_url={database_url}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply Sentence Reader PostgreSQL migrations.")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--create-database", action="store_true")
    args = parser.parse_args()

    database_url = database_url_from_args(args)
    if args.create_database:
        create_database_if_missing(database_url)
    apply_migration(database_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
