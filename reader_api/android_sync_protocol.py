from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from reader_api import db


ANDROID_SYNC_SCHEMA = "click.android.sync.v2"
DEFAULT_EVENT_PAGE_SIZE = 100
MAX_EVENT_PAGE_SIZE = 500


def operation_request_hash(device_id: str, operation: Mapping[str, Any]) -> str:
    canonical = {
        "device_id": str(device_id or ""),
        "operation_id": str(operation.get("operation_id") or ""),
        "operation_type": str(operation.get("operation_type") or ""),
        "book_id": str(operation.get("book_id") or ""),
        "payload": operation.get("payload") or {},
        "base_server_version": str(operation.get("base_server_version") or ""),
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def current_sequence(conn: Any) -> int:
    row = conn.execute(
        "SELECT COALESCE(max(sequence), 0) AS sequence FROM reader.android_sync_events"
    ).fetchone()
    return int((row or {}).get("sequence") or 0)


def event_page(conn: Any, after_sequence: int, limit: int = DEFAULT_EVENT_PAGE_SIZE) -> dict[str, Any]:
    safe_after = max(0, int(after_sequence))
    safe_limit = max(1, min(int(limit), MAX_EVENT_PAGE_SIZE))
    rows = conn.execute(
        """
        SELECT sequence, resource_type, resource_id, action, version,
               origin_device_id, operation_id, committed_at
        FROM reader.android_sync_events
        WHERE sequence > %s
        ORDER BY sequence ASC
        LIMIT %s
        """,
        (safe_after, safe_limit),
    ).fetchall()
    events = [dict(row) for row in rows]
    next_sequence = int(events[-1]["sequence"]) if events else safe_after
    more = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM reader.android_sync_events WHERE sequence > %s) AS value",
        (next_sequence,),
    ).fetchone()
    return {
        "events": events,
        "after_sequence": safe_after,
        "next_sequence": next_sequence,
        "has_more": bool((more or {}).get("value")),
        "limit": safe_limit,
    }


def operation_receipt(conn: Any, operation_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT operation_id, device_id, request_hash, status, http_status, result,
               created_at, updated_at
        FROM reader.android_sync_operation_receipts
        WHERE operation_id = %s
        """,
        (operation_id,),
    ).fetchone()
    return dict(row) if row else None


def store_operation_receipt(
    conn: Any,
    *,
    operation_id: str,
    device_id: str,
    request_hash: str,
    status: str,
    http_status: int,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    row = conn.execute(
        """
        INSERT INTO reader.android_sync_operation_receipts (
          operation_id, device_id, request_hash, status, http_status, result, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, now(), now())
        RETURNING operation_id, device_id, request_hash, status, http_status, result, created_at, updated_at
        """,
        (
            operation_id,
            device_id,
            request_hash,
            status,
            int(http_status),
            db.jsonb(dict(result)),
        ),
    ).fetchone()
    return dict(row)


def resource_version(conn: Any, resource_type: str, resource_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT resource_type, resource_id, version, deleted, last_sequence, updated_at
        FROM reader.android_sync_resource_versions
        WHERE resource_type = %s AND resource_id = %s
        """,
        (resource_type, resource_id),
    ).fetchone()
    return dict(row) if row else None
