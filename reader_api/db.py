from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import Any, Iterator, Optional, Union

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from reader_api.config import database_url


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    with psycopg.connect(database_url(), row_factory=dict_row) as conn:
        yield conn


def postgresql_instance_fingerprint(
    database: Any,
    system_identifier: Any,
) -> str:
    return hashlib.sha256(
        "\0".join(
            [
                str(database),
                str(system_identifier),
            ]
        ).encode("utf-8")
    ).hexdigest()


def health() -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT current_database() AS database,
                   current_schema() AS schema,
                   (SELECT system_identifier::text FROM pg_control_system()) AS system_identifier
            """
        ).fetchone()
    fingerprint = postgresql_instance_fingerprint(
        row["database"],
        row["system_identifier"],
    )
    return {
        "ok": True,
        "database": row["database"],
        "schema": row["schema"],
        "instance_fingerprint": fingerprint,
    }


def jsonb(value: Optional[Union[dict[str, Any], list[Any]]]) -> Jsonb:
    return Jsonb({} if value is None else value)
