from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any


RUNTIME_HEALTH_SCHEMA = "click.reader_api.health.v2"
RUNTIME_CONTRACT = "click.reader_runtime.v1"
RUNTIME_API_REVISION = 3
RUNTIME_CAPABILITIES = (
    "reader.library.v1",
    "reader.annotations.v1",
    "voice.inbox.v2",
    "voice.transcript_versions.v1",
    "voice.processing_jobs.v1",
    "voice.hermes_adapter.v1",
    "voice.reading_context.v1",
    "voice.discussions.v2",
    "voice.verified_actions.v2",
    "tingle.inspirations.v1",
    "tingle.permanent_delete.v1",
    "tingle.tombstone_replay_guard.v1",
)
RUNTIME_STARTED_AT = datetime.now(timezone.utc).isoformat()


def runtime_payload() -> dict[str, Any]:
    return {
        "contract": RUNTIME_CONTRACT,
        "api_revision": RUNTIME_API_REVISION,
        "capabilities": list(RUNTIME_CAPABILITIES),
        "process_id": os.getpid(),
        "started_at": RUNTIME_STARTED_AT,
        "required_migration": "014_android_sync_v2.sql",
    }
