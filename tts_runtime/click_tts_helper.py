#!/usr/bin/env python3
"""Private JSONL helper for Click Reader's Edge online TTS cache.

The helper deliberately owns no UI, reading-order, or playback state.  It
accepts text only on stdin, runs the edge_tts library in-process, and emits
machine-readable metadata that never includes the submitted text.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import random
import re
import signal
import stat
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

import edge_tts


PROTOCOL_VERSION = 1
HELPER_VERSION = "0.1.0"
EXPECTED_EDGE_TTS_VERSION = "7.2.8"
DISCLOSURE_REVISION = "edge-online-2026-07-v1"
CACHE_NAMESPACE = "reading-v2/probe-v1"
NETWORK_HOSTNAME = "speech.platform.bing.com"
NETWORK_PROTOCOL = "wss"
NETWORK_PORT = 443
NETWORK_MODE = "direct-no-proxy"
CACHE_KEY_CANONICALIZATION = (
    "UTF-8 JSON; object keys sorted ascending; ensure_ascii=false; "
    "separators=(',',':'); SHA-256 lowercase hexadecimal"
)
CACHE_FILE_LAYOUT = (
    "<cache-root>/<cache-key[0:2]>/<cache-key>.mp3, "
    "<cache-key>.boundaries.json, <cache-key>.manifest.json"
)
CACHE_KEY_FIELDS = (
    "backend_id",
    "book_id",
    "chapter_locator",
    "client_version",
    "edge_tts_client_version",
    "engine_revision",
    "fixture_id",
    "generation_params_hash",
    "locator_range",
    "normalized_text_hash",
    "normalizer_revision",
    "prosody_revision",
    "voice_cache_epoch",
    "voice_id",
)
CACHE_ROOT = (
    Path.home()
    / "Library"
    / "Application Support"
    / "Click"
    / "ReaderTTS"
    / "reading-v2"
    / "probe-v1"
)

MAX_TEXT_CHARACTERS = 100_000
MAX_INPUT_LINE_BYTES = 1_000_000
MIN_MP3_BYTES = 128
MAX_AUDIO_BYTES = 256 * 1024 * 1024
MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 128 * 1024

MAX_PROVIDER_ATTEMPTS = 3
PROVIDER_TOTAL_TIMEOUT_BUDGET_SECONDS = 60.0
SYNTHESIS_ATTEMPT_TIMEOUT_SECONDS = 18.0
VOICE_LIST_ATTEMPT_TIMEOUT_SECONDS = 18.0
RETRY_BASE_DELAY_SECONDS = 0.5
RETRY_MAX_DELAY_SECONDS = 2.0
RETRY_JITTER_RATIO = 0.25
CIRCUIT_FAILURE_THRESHOLD = 3
CIRCUIT_RESET_SECONDS = 5 * 60
CIRCUIT_STATE_SCHEMA_VERSION = 1
CIRCUIT_STATE_FILENAME = ".circuit-breaker.json"

_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_FIXTURE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_LOCATOR_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._~:/?#%+@=&;,\-\[\]\(\)]{0,511}$"
)
_VOICE_PATTERN = re.compile(r"^[A-Za-z0-9-]{3,96}$")
_RATE_VOLUME_PATTERN = re.compile(r"^[+-](?:[0-9]|[1-9][0-9]|100)%$")
_PITCH_PATTERN = re.compile(r"^[+-](?:[0-9]|[1-9][0-9]|100)Hz$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PARTIAL_PATTERN = re.compile(
    r"^[0-9a-f]{64}\.(?:mp3|boundaries\.json|manifest\.json)"
    r"\.partial\.(?P<pid>[1-9][0-9]*)\.(?P<token>[0-9a-f]{32})$"
)
_CIRCUIT_PARTIAL_PATTERN = re.compile(
    r"^\.circuit-breaker\.json\.partial\."
    r"(?P<pid>[1-9][0-9]*)\.(?P<token>[0-9a-f]{32})$"
)

_MPEG1_LAYER3_BITRATES = (
    0,
    32,
    40,
    48,
    56,
    64,
    80,
    96,
    112,
    128,
    160,
    192,
    224,
    256,
    320,
    0,
)
_MPEG2_LAYER3_BITRATES = (
    0,
    8,
    16,
    24,
    32,
    40,
    48,
    56,
    64,
    80,
    96,
    112,
    128,
    144,
    160,
    0,
)
_MPEG1_SAMPLE_RATES = (44_100, 48_000, 32_000)

_CACHE_CONTEXT_FIELDS = (
    "book_id",
    "fixture_id",
    "chapter_locator",
    "locator_range",
    "client_version",
    "voice_cache_epoch",
    "prosody_revision",
    "normalizer_revision",
    "engine_revision",
)
_SYNTHESIZE_FIELDS = {
    "type",
    "id",
    "op",
    "text",
    "voice",
    "cache_context",
    "synthesis",
    "disclosure",
}

_ERROR_MESSAGES = {
    "busy": "another generation job is active",
    "canceled": "generation was canceled",
    "circuit_open": "online TTS is temporarily paused after repeated failures",
    "disclosure_required": "current online-TTS disclosure authorization is required",
    "empty_audio": "provider returned no valid audio",
    "internal_error": "generation failed internally",
    "invalid_boundary": "provider returned no valid sentence boundary metadata",
    "invalid_request": "request is invalid",
    "invalid_voice_or_request": "provider rejected the voice or request",
    "network_unavailable": "online TTS network is unavailable",
    "protocol_error": "online TTS protocol response is invalid",
    "rate_limited": "online TTS request was rate limited",
    "runtime_version_mismatch": "bundled edge_tts runtime version does not match",
    "service_unavailable": "online TTS service is unavailable",
    "timeout": "online TTS request timed out",
}

_SELECTED_VOICE_IDS = (
    "zh-CN-YunjianNeural",
    "zh-CN-XiaoxiaoNeural",
)


class RequestError(Exception):
    """A request validation error whose details must not cross the IPC."""


class CircuitStateCorruptError(RuntimeError):
    """A private circuit-state file has invalid contents but a safe identity."""

    def __init__(self, device: int, inode: int) -> None:
        super().__init__("invalid circuit state")
        self.device = device
        self.inode = inode


class AudioValidationError(Exception):
    """Generated artifacts failed local validation."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


def _edge_tts_version() -> str:
    return str(getattr(edge_tts, "__version__", "unknown"))


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_request_id(value: Any) -> Optional[str]:
    if isinstance(value, str) and _ID_PATTERN.fullmatch(value):
        return value
    return None


def _error_result(
    request_id: Optional[str],
    op: str,
    category: str,
    *,
    retryable: bool = False,
    retry_after_ms: Optional[int] = None,
    attempt_count: Optional[int] = None,
    synthesis_elapsed_ms: Optional[int] = None,
    list_elapsed_ms: Optional[int] = None,
) -> Dict[str, Any]:
    safe_category = category if category in _ERROR_MESSAGES else "internal_error"
    result: Dict[str, Any] = {
        "type": "result",
        "id": request_id,
        "op": op,
        "status": "error",
        "error": {
            "category": safe_category,
            "retryable": bool(retryable),
            "message": _ERROR_MESSAGES[safe_category],
        },
    }
    if retry_after_ms is not None:
        result["retry_after_ms"] = max(0, int(retry_after_ms))
    if attempt_count is not None:
        result["attempt_count"] = max(0, int(attempt_count))
    if synthesis_elapsed_ms is not None:
        result["synthesis_elapsed_ms"] = max(0, int(synthesis_elapsed_ms))
    if list_elapsed_ms is not None:
        result["list_elapsed_ms"] = max(0, int(list_elapsed_ms))
    return result


def _ok_result(request_id: Optional[str], op: str, **values: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "type": "result",
        "id": request_id,
        "op": op,
        "status": "ok",
    }
    result.update(values)
    return result


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError("unsafe cache directory")
    path.chmod(0o700)


def _ensure_private_cache_hierarchy() -> None:
    expected_suffix = ("Click", "ReaderTTS", "reading-v2", "probe-v1")
    if tuple(CACHE_ROOT.parts[-4:]) == expected_suffix:
        paths = (
            CACHE_ROOT.parents[2],
            CACHE_ROOT.parents[1],
            CACHE_ROOT.parents[0],
            CACHE_ROOT,
        )
    else:
        # Unit tests replace CACHE_ROOT with an isolated temporary directory.
        paths = (CACHE_ROOT,)
    for path in paths:
        _ensure_private_directory(path)


def _is_private_regular_file(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and stat.S_IMODE(info.st_mode) == 0o600
    )


def _is_private_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and stat.S_IMODE(info.st_mode) == 0o700
    )


def _fsync_file(handle: Any) -> None:
    handle.flush()
    os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _elapsed_ms(started: float) -> int:
    return max(0, int(round((time.monotonic() - started) * 1000)))


def _circuit_state_path() -> Path:
    return CACHE_ROOT / CIRCUIT_STATE_FILENAME


def _empty_circuit_state() -> Dict[str, int]:
    return {
        "schema_version": CIRCUIT_STATE_SCHEMA_VERSION,
        "consecutive_retryable_failures": 0,
        "open_until_epoch_ms": 0,
        "updated_at_epoch_ms": 0,
    }


def _read_circuit_state() -> Dict[str, int]:
    path = _circuit_state_path()
    try:
        path_info = path.lstat()
    except FileNotFoundError:
        return _empty_circuit_state()
    except OSError as error:
        raise RuntimeError("unsafe circuit state") from error
    if (
        not stat.S_ISREG(path_info.st_mode)
        or stat.S_ISLNK(path_info.st_mode)
        or stat.S_IMODE(path_info.st_mode) != 0o600
    ):
        raise RuntimeError("unsafe circuit state")

    descriptor: Optional[int] = None
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        opened_info = os.fstat(descriptor)
        if (
            opened_info.st_dev != path_info.st_dev
            or opened_info.st_ino != path_info.st_ino
            or not stat.S_ISREG(opened_info.st_mode)
            or stat.S_IMODE(opened_info.st_mode) != 0o600
        ):
            raise RuntimeError("unsafe circuit state")
        if opened_info.st_size > 4096:
            raise CircuitStateCorruptError(
                opened_info.st_dev,
                opened_info.st_ino,
            )
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = None
            payload = handle.read(4097)
            final_info = os.fstat(handle.fileno())
    except CircuitStateCorruptError:
        raise
    except OSError as error:
        raise RuntimeError("unsafe circuit state") from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass

    if (
        len(payload) > 4096
        or len(payload) != final_info.st_size
        or final_info.st_dev != opened_info.st_dev
        or final_info.st_ino != opened_info.st_ino
        or final_info.st_size != opened_info.st_size
        or final_info.st_mtime_ns != opened_info.st_mtime_ns
    ):
        raise RuntimeError("circuit state changed while reading")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise CircuitStateCorruptError(
            opened_info.st_dev,
            opened_info.st_ino,
        ) from error
    expected_fields = set(_empty_circuit_state())
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise CircuitStateCorruptError(opened_info.st_dev, opened_info.st_ino)
    for field in expected_fields:
        item = value.get(field)
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            raise CircuitStateCorruptError(
                opened_info.st_dev,
                opened_info.st_ino,
            )
    if value["schema_version"] != CIRCUIT_STATE_SCHEMA_VERSION:
        raise CircuitStateCorruptError(opened_info.st_dev, opened_info.st_ino)
    return value


def _quarantine_corrupt_circuit_state(
    error: CircuitStateCorruptError,
) -> Optional[Path]:
    path = _circuit_state_path()
    try:
        path_info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as quarantine_error:
        raise RuntimeError("unable to isolate circuit state") from quarantine_error
    if (
        path_info.st_dev != error.device
        or path_info.st_ino != error.inode
        or not stat.S_ISREG(path_info.st_mode)
        or stat.S_ISLNK(path_info.st_mode)
        or stat.S_IMODE(path_info.st_mode) != 0o600
    ):
        raise RuntimeError("circuit state changed before isolation")

    quarantine = path.with_name(
        f"{CIRCUIT_STATE_FILENAME}.corrupt.{os.getpid()}.{uuid.uuid4().hex}"
    )
    try:
        os.replace(path, quarantine)
        quarantine_info = quarantine.lstat()
    except OSError as quarantine_error:
        raise RuntimeError("unable to isolate circuit state") from quarantine_error
    if (
        quarantine_info.st_dev != error.device
        or quarantine_info.st_ino != error.inode
        or not stat.S_ISREG(quarantine_info.st_mode)
        or stat.S_ISLNK(quarantine_info.st_mode)
        or stat.S_IMODE(quarantine_info.st_mode) != 0o600
    ):
        raise RuntimeError("unsafe isolated circuit state")
    _fsync_directory(CACHE_ROOT)
    return quarantine


def _recover_corrupt_circuit_state(
    error: CircuitStateCorruptError,
) -> Dict[str, int]:
    _ensure_private_cache_hierarchy()
    _quarantine_corrupt_circuit_state(error)
    reset = _empty_circuit_state()
    reset["updated_at_epoch_ms"] = int(time.time() * 1000)
    _write_circuit_state(reset)
    return reset


def _write_circuit_state(value: Mapping[str, int]) -> None:
    _ensure_private_cache_hierarchy()
    path = _circuit_state_path()
    partial = path.with_name(
        f"{CIRCUIT_STATE_FILENAME}.partial.{os.getpid()}.{uuid.uuid4().hex}"
    )
    descriptor: Optional[int] = None
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(partial, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        payload = _json_bytes(dict(value)) + b"\n"
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = None
            handle.write(payload)
            _fsync_file(handle)
        os.replace(partial, path)
        path.chmod(0o600)
        _fsync_directory(CACHE_ROOT)
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        _remove_paths((partial,))


def _normalised_circuit_state() -> Dict[str, int]:
    try:
        value = _read_circuit_state()
    except CircuitStateCorruptError as error:
        value = _recover_corrupt_circuit_state(error)
    now_ms = int(time.time() * 1000)
    if value["open_until_epoch_ms"] and value["open_until_epoch_ms"] <= now_ms:
        value = _empty_circuit_state()
        value["updated_at_epoch_ms"] = now_ms
        _write_circuit_state(value)
    return value


def _circuit_retry_after_ms() -> int:
    value = _normalised_circuit_state()
    return max(0, value["open_until_epoch_ms"] - int(time.time() * 1000))


def _record_provider_success() -> None:
    value = _normalised_circuit_state()
    if (
        value["consecutive_retryable_failures"] == 0
        and value["open_until_epoch_ms"] == 0
    ):
        return
    reset = _empty_circuit_state()
    reset["updated_at_epoch_ms"] = int(time.time() * 1000)
    _write_circuit_state(reset)


def _record_retryable_failure() -> int:
    value = _normalised_circuit_state()
    now_ms = int(time.time() * 1000)
    value["consecutive_retryable_failures"] += 1
    value["updated_at_epoch_ms"] = now_ms
    if value["consecutive_retryable_failures"] >= CIRCUIT_FAILURE_THRESHOLD:
        value["open_until_epoch_ms"] = now_ms + (CIRCUIT_RESET_SECONDS * 1000)
    _write_circuit_state(value)
    return max(0, value["open_until_epoch_ms"] - now_ms)


def _retry_delay_seconds(attempt_count: int) -> float:
    base = min(
        RETRY_MAX_DELAY_SECONDS,
        RETRY_BASE_DELAY_SECONDS * (2 ** max(0, attempt_count - 1)),
    )
    jitter = base * RETRY_JITTER_RATIO
    return max(0.0, min(RETRY_MAX_DELAY_SECONDS, base + random.uniform(-jitter, jitter)))


async def _sleep_before_retry(attempt_count: int) -> None:
    await asyncio.sleep(_retry_delay_seconds(attempt_count))


def _cache_paths(cache_key: str) -> Dict[str, Path]:
    if not _SHA256_PATTERN.fullmatch(cache_key):
        raise ValueError("invalid cache key")
    root = CACHE_ROOT
    shard = root / cache_key[:2]
    if os.path.commonpath((str(root), str(shard))) != str(root):
        raise ValueError("cache path escaped root")
    base = shard / cache_key
    return {
        "shard": shard,
        "audio": base.with_suffix(".mp3"),
        "metadata": base.with_suffix(".boundaries.json"),
        "manifest": base.with_suffix(".manifest.json"),
    }


def _remove_paths(paths: Iterable[Path]) -> None:
    for path in paths:
        try:
            if path.is_file() or path.is_symlink():
                path.unlink()
        except OSError:
            pass


def cleanup_partials() -> int:
    """Remove strict partial files whose recorded owner PID is no longer alive.

    The caller must hold the cache-root process lock.  A live PID is never
    touched, even though PID reuse can conservatively leave a stale file.
    """

    if not CACHE_ROOT.exists() or CACHE_ROOT.is_symlink():
        return 0
    removed = 0
    for current, directories, filenames in os.walk(CACHE_ROOT, followlinks=False):
        current_path = Path(current)
        directories[:] = [
            name
            for name in directories
            if not (current_path / name).is_symlink()
        ]
        for name in filenames:
            match = _PARTIAL_PATTERN.fullmatch(name)
            if match is None:
                continue
            path = current_path / name
            try:
                info = path.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                continue
            owner_pid = int(match.group("pid"))
            if owner_pid == os.getpid() or _pid_is_alive(owner_pid):
                continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def cleanup_stale_circuit_partials() -> int:
    """Remove private circuit-state partials only when their PID is dead.

    Live owners, including this process, are never touched.  The liveness and
    file identity checks are repeated immediately before unlinking so a
    changed path or a PID that became live is preserved conservatively.
    """

    if not _is_private_directory(CACHE_ROOT):
        return 0
    removed = 0
    try:
        candidates = tuple(CACHE_ROOT.iterdir())
    except OSError:
        return 0
    for path in candidates:
        match = _CIRCUIT_PARTIAL_PATTERN.fullmatch(path.name)
        if match is None:
            continue
        owner_pid = int(match.group("pid"))
        try:
            path_info = path.lstat()
        except OSError:
            continue
        if (
            not stat.S_ISREG(path_info.st_mode)
            or stat.S_ISLNK(path_info.st_mode)
            or stat.S_IMODE(path_info.st_mode) != 0o600
            or _pid_is_alive(owner_pid)
        ):
            continue
        try:
            confirmed_info = path.lstat()
        except OSError:
            continue
        if (
            confirmed_info.st_dev != path_info.st_dev
            or confirmed_info.st_ino != path_info.st_ino
            or not stat.S_ISREG(confirmed_info.st_mode)
            or stat.S_IMODE(confirmed_info.st_mode) != 0o600
            or _pid_is_alive(owner_pid)
        ):
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _acquire_process_lock() -> Optional[int]:
    lock_path = CACHE_ROOT / ".helper.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError:
        return None
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        payload = _json_bytes(
            {
                "helper_version": HELPER_VERSION,
                "pgid": os.getpgrp(),
                "pid": os.getpid(),
                "ppid": os.getppid(),
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
        ) + b"\n"
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, payload)
        os.fsync(descriptor)
        return descriptor
    except (BlockingIOError, OSError):
        os.close(descriptor)
        return None


def _release_process_lock(descriptor: Optional[int]) -> None:
    if descriptor is None:
        return
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(descriptor)
    except OSError:
        pass


def _network_environment_is_private() -> bool:
    forbidden = {
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "ftp_proxy",
        "ws_proxy",
        "wss_proxy",
        "requests_ca_bundle",
        "curl_ca_bundle",
        "ssl_cert_file",
        "ssl_cert_dir",
        "pythonpath",
        "pythonhome",
        "virtual_env",
    }
    present = {key.lower() for key in os.environ if key.lower() in forbidden}
    return (
        os.environ.get("CLICK_TTS_NETWORK_MODE") == NETWORK_MODE
        and not present
        and os.environ.get("NO_PROXY") == "*"
        and os.environ.get("no_proxy") == "*"
    )


def _normalise_synthesis(value: Any) -> Dict[str, str]:
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - {
        "rate",
        "volume",
        "pitch",
        "boundary",
    }:
        raise RequestError()
    settings = {
        "rate": value.get("rate", "+0%"),
        "volume": value.get("volume", "+0%"),
        "pitch": value.get("pitch", "+0Hz"),
        "boundary": value.get("boundary", "SentenceBoundary"),
    }
    if not isinstance(settings["rate"], str) or not _RATE_VOLUME_PATTERN.fullmatch(
        settings["rate"]
    ):
        raise RequestError()
    if not isinstance(settings["volume"], str) or not _RATE_VOLUME_PATTERN.fullmatch(
        settings["volume"]
    ):
        raise RequestError()
    if not isinstance(settings["pitch"], str) or not _PITCH_PATTERN.fullmatch(
        settings["pitch"]
    ):
        raise RequestError()
    if settings["boundary"] not in {"SentenceBoundary", "WordBoundary"}:
        raise RequestError()
    return settings


def _normalise_cache_context(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict) or set(value) != set(_CACHE_CONTEXT_FIELDS):
        raise RequestError()
    result: Dict[str, str] = {}
    for field in _CACHE_CONTEXT_FIELDS:
        item = value[field]
        if (
            not isinstance(item, str)
            or not item
            or len(item) > 512
            or "\x00" in item
        ):
            raise RequestError()
        result[field] = item
    if not _FIXTURE_ID_PATTERN.fullmatch(result["fixture_id"]):
        raise RequestError()
    for field in ("chapter_locator", "locator_range"):
        if not _LOCATOR_PATTERN.fullmatch(result[field]):
            raise RequestError()
    return result


def _validate_synthesize_request(
    request: Any,
) -> Tuple[Optional[str], str, str, Dict[str, str], Dict[str, str], str, str]:
    if not isinstance(request, dict) or set(request) - _SYNTHESIZE_FIELDS:
        raise RequestError()
    request_id = _safe_request_id(request.get("id"))
    if request_id is None:
        raise RequestError()
    if request.get("type") not in (None, "request") or request.get("op") != "synthesize":
        raise RequestError()
    text = request.get("text")
    voice = request.get("voice")
    if (
        not isinstance(text, str)
        or not text.strip()
        or len(text) > MAX_TEXT_CHARACTERS
    ):
        raise RequestError()
    if not isinstance(voice, str) or not _VOICE_PATTERN.fullmatch(voice):
        raise RequestError()
    context = _normalise_cache_context(request.get("cache_context"))
    settings = _normalise_synthesis(request.get("synthesis"))
    text_hash = _sha256_bytes(text.encode("utf-8"))
    parameters_hash = _sha256_bytes(_json_bytes(settings))
    key_material: Dict[str, str] = {
        **context,
        "normalized_text_hash": text_hash,
        "backend_id": "edge_online",
        "voice_id": voice,
        "edge_tts_client_version": _edge_tts_version(),
        "generation_params_hash": parameters_hash,
    }
    if set(key_material) != set(CACHE_KEY_FIELDS):
        raise RuntimeError("cache key contract mismatch")
    cache_key = _sha256_bytes(_json_bytes(key_material))
    return request_id, text, voice, context, settings, text_hash, cache_key


def _has_current_disclosure(request: Mapping[str, Any]) -> bool:
    disclosure = request.get("disclosure")
    return (
        isinstance(disclosure, dict)
        and set(disclosure) == {"revision", "authorized"}
        and disclosure.get("revision") == DISCLOSURE_REVISION
        and disclosure.get("authorized") is True
    )


def _looks_like_mp3(path: Path) -> bool:
    try:
        if path.stat().st_size < MIN_MP3_BYTES:
            return False
        with path.open("rb") as handle:
            prefix = handle.read(4096)
    except OSError:
        return False
    if prefix.startswith(b"ID3"):
        return True
    return any(
        prefix[index] == 0xFF and (prefix[index + 1] & 0xE0) == 0xE0
        for index in range(max(0, len(prefix) - 1))
    )


def _validate_boundaries(boundaries: Any) -> bool:
    if not isinstance(boundaries, list) or not boundaries:
        return False
    previous_offset = -1
    for ordinal, boundary in enumerate(boundaries):
        if not isinstance(boundary, dict) or set(boundary) != {
            "ordinal",
            "type",
            "offset",
            "duration",
        }:
            return False
        offset = boundary.get("offset")
        duration = boundary.get("duration")
        if (
            boundary.get("ordinal") != ordinal
            or boundary.get("type") not in {"SentenceBoundary", "WordBoundary"}
            or not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < previous_offset
            or not isinstance(duration, int)
            or isinstance(duration, bool)
            or duration < 0
        ):
            return False
        previous_offset = offset
    return any(
        boundary["offset"] + boundary["duration"] > 0 for boundary in boundaries
    )


def _synchsafe_integer(raw: bytes) -> int:
    if len(raw) != 4 or any(byte & 0x80 for byte in raw):
        raise AudioValidationError("empty_audio")
    return (raw[0] << 21) | (raw[1] << 14) | (raw[2] << 7) | raw[3]


def _layer3_frame(header: bytes) -> Optional[Tuple[int, int, int, int]]:
    """Return frame bytes, samples, sample rate, and MPEG version."""

    if len(header) != 4:
        return None
    value = int.from_bytes(header, "big")
    if (value >> 21) & 0x7FF != 0x7FF:
        return None
    version_id = (value >> 19) & 0x3
    layer_id = (value >> 17) & 0x3
    bitrate_index = (value >> 12) & 0xF
    sample_rate_index = (value >> 10) & 0x3
    padding = (value >> 9) & 0x1
    emphasis = value & 0x3
    if (
        version_id == 1
        or layer_id != 1
        or bitrate_index in (0, 15)
        or sample_rate_index == 3
        or emphasis == 2
    ):
        return None
    if version_id == 3:
        bitrate = _MPEG1_LAYER3_BITRATES[bitrate_index]
        sample_rate = _MPEG1_SAMPLE_RATES[sample_rate_index]
        samples_per_frame = 1152
        coefficient = 144_000
    else:
        bitrate = _MPEG2_LAYER3_BITRATES[bitrate_index]
        divisor = 2 if version_id == 2 else 4
        sample_rate = _MPEG1_SAMPLE_RATES[sample_rate_index] // divisor
        samples_per_frame = 576
        coefficient = 72_000
    frame_bytes = coefficient * bitrate // sample_rate + padding
    if frame_bytes < 24:
        return None
    return frame_bytes, samples_per_frame, sample_rate, version_id


def _mp3_duration_ms(path: Path) -> int:
    """Strictly scan every complete MPEG Layer III frame in the MP3."""

    try:
        info = path.lstat()
        size = info.st_size
    except OSError as error:
        raise AudioValidationError("empty_audio") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or size < MIN_MP3_BYTES
        or size > MAX_AUDIO_BYTES
    ):
        raise AudioValidationError("empty_audio")

    duration_ms = 0.0
    frame_count = 0
    stream_signature: Optional[Tuple[int, int]] = None
    try:
        with path.open("rb") as handle:
            prefix = handle.read(10)
            position = 0
            if prefix.startswith(b"ID3"):
                if len(prefix) != 10:
                    raise AudioValidationError("empty_audio")
                major_version = prefix[3]
                revision = prefix[4]
                flags = prefix[5]
                allowed_flags = {
                    2: 0xC0,
                    3: 0xE0,
                    4: 0xF0,
                }
                if (
                    major_version not in allowed_flags
                    or revision == 0xFF
                    or flags & ~allowed_flags[major_version]
                    or (flags & 0x10 and major_version != 4)
                ):
                    raise AudioValidationError("empty_audio")
                footer_bytes = 10 if flags & 0x10 else 0
                position = (
                    10
                    + _synchsafe_integer(prefix[6:10])
                    + footer_bytes
                )
                if position > size:
                    raise AudioValidationError("empty_audio")
                handle.seek(position)
            else:
                handle.seek(0)

            while position < size:
                remaining = size - position
                if remaining == 128:
                    marker = handle.read(3)
                    handle.seek(position)
                    if marker == b"TAG":
                        handle.seek(size)
                        position = size
                        break
                if remaining < 4:
                    raise AudioValidationError("empty_audio")
                header = handle.read(4)
                parsed = _layer3_frame(header)
                if parsed is None:
                    raise AudioValidationError("empty_audio")
                frame_bytes, samples_per_frame, sample_rate, version_id = parsed
                if frame_bytes > remaining:
                    raise AudioValidationError("empty_audio")
                signature = (version_id, sample_rate)
                if stream_signature is None:
                    stream_signature = signature
                elif stream_signature != signature:
                    raise AudioValidationError("empty_audio")
                handle.seek(frame_bytes - 4, os.SEEK_CUR)
                position += frame_bytes
                duration_ms += (samples_per_frame * 1000.0) / sample_rate
                frame_count += 1
    except AudioValidationError:
        raise
    except OSError as error:
        raise AudioValidationError("empty_audio") from error

    if frame_count == 0 or position != size or duration_ms <= 0:
        raise AudioValidationError("empty_audio")
    return max(1, int(round(duration_ms)))


def _selected_voice_result(voices: Any) -> list[Dict[str, Any]]:
    if not isinstance(voices, list):
        raise RuntimeError("invalid voice list")
    by_id: Dict[str, Mapping[str, Any]] = {}
    for voice in voices:
        if not isinstance(voice, dict):
            continue
        voice_id = voice.get("ShortName")
        if isinstance(voice_id, str) and voice_id in _SELECTED_VOICE_IDS:
            by_id.setdefault(voice_id, voice)

    result: list[Dict[str, Any]] = []
    service_fields = {
        "Locale": "locale",
        "Gender": "gender",
        "Status": "status",
        "SuggestedCodec": "suggested_codec",
    }
    for voice_id in _SELECTED_VOICE_IDS:
        source = by_id.get(voice_id)
        selected: Dict[str, Any] = {
            "voice_id": voice_id,
            "available": source is not None,
        }
        if source is not None:
            for provider_field, response_field in service_fields.items():
                value = source.get(provider_field)
                if isinstance(value, str) and value:
                    selected[response_field] = value
        result.append(selected)
    return result


def _load_cache_hit(
    cache_key: str,
    voice: str,
    text_hash: str,
    context: Mapping[str, str],
) -> Optional[Dict[str, Any]]:
    paths = _cache_paths(cache_key)
    if (
        not _is_private_directory(paths["shard"])
        or not all(
        _is_private_regular_file(paths[name])
        for name in ("audio", "metadata", "manifest")
        )
    ):
        return None
    try:
        audio_size = paths["audio"].stat().st_size
        metadata_size = paths["metadata"].stat().st_size
        manifest_size = paths["manifest"].stat().st_size
        if (
            audio_size < MIN_MP3_BYTES
            or audio_size > MAX_AUDIO_BYTES
            or metadata_size <= 0
            or metadata_size > MAX_METADATA_BYTES
            or manifest_size <= 0
            or manifest_size > MAX_MANIFEST_BYTES
        ):
            return None
        manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
        metadata = json.loads(paths["metadata"].read_text(encoding="utf-8"))
        parsed_audio_duration_ms = _mp3_duration_ms(paths["audio"])
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        AudioValidationError,
    ):
        return None
    if not isinstance(manifest, dict):
        return None
    expected_names = {
        "audio_file": paths["audio"].name,
        "metadata_file": paths["metadata"].name,
    }
    if (
        manifest.get("schema_version") != 1
        or manifest.get("cache_key") != cache_key
        or manifest.get("voice") != voice
        or manifest.get("text_sha256") != text_hash
        or manifest.get("fixture_id") != context["fixture_id"]
        or manifest.get("chapter_locator") != context["chapter_locator"]
        or manifest.get("locator_range") != context["locator_range"]
        or manifest.get("edge_tts_version") != _edge_tts_version()
        or manifest.get("audio_file") != expected_names["audio_file"]
        or manifest.get("metadata_file") != expected_names["metadata_file"]
        or manifest.get("audio_sha256") != _sha256_file(paths["audio"])
        or manifest.get("metadata_sha256") != _sha256_file(paths["metadata"])
        or manifest.get("audio_bytes") != audio_size
        or manifest.get("boundary_count")
        != (len(metadata) if isinstance(metadata, list) else -1)
        or not isinstance(manifest.get("attempt_count"), int)
        or isinstance(manifest.get("attempt_count"), bool)
        or not 1 <= manifest["attempt_count"] <= MAX_PROVIDER_ATTEMPTS
        or not isinstance(manifest.get("synthesis_elapsed_ms"), int)
        or isinstance(manifest.get("synthesis_elapsed_ms"), bool)
        or manifest["synthesis_elapsed_ms"] < 0
        or not isinstance(manifest.get("audio_duration_ms"), int)
        or isinstance(manifest.get("audio_duration_ms"), bool)
        or manifest["audio_duration_ms"] <= 0
        or manifest["audio_duration_ms"] != parsed_audio_duration_ms
        or not isinstance(manifest.get("synthesis_rtf"), (int, float))
        or isinstance(manifest.get("synthesis_rtf"), bool)
        or manifest["synthesis_rtf"] < 0
        or not _validate_boundaries(metadata)
    ):
        return None
    return {
        "source": "cache",
        "cache_key": cache_key,
        "audio_path": str(paths["audio"]),
        "metadata_path": str(paths["metadata"]),
        "manifest_path": str(paths["manifest"]),
        "audio_bytes": manifest["audio_bytes"],
        "boundary_count": manifest["boundary_count"],
        "char_count": manifest.get("char_count", 0),
        "fixture_id": manifest["fixture_id"],
        "chapter_locator": manifest["chapter_locator"],
        "locator_range": manifest["locator_range"],
        "audio_duration_ms": manifest["audio_duration_ms"],
        "synthesis_rtf": manifest["synthesis_rtf"],
    }


def _classify_provider_error(error: BaseException) -> Tuple[str, bool]:
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return "timeout", True
    status = getattr(error, "status", None)
    if status == 429:
        return "rate_limited", True
    if isinstance(status, int) and 500 <= status <= 599:
        return "service_unavailable", True
    if isinstance(status, int) and 400 <= status <= 499:
        return "invalid_voice_or_request", False
    name = error.__class__.__name__.lower()
    if any(token in name for token in ("connect", "network", "dns", "socket")):
        return "network_unavailable", True
    if "timeout" in name:
        return "timeout", True
    if "noaudio" in name:
        return "empty_audio", False
    if any(token in name for token in ("websocket", "protocol", "response")):
        return "protocol_error", False
    return "internal_error", False


def _validate_list_voices_request(request: Any) -> str:
    if (
        not isinstance(request, dict)
        or set(request) != {"type", "id", "op"}
        or request.get("type") != "request"
        or request.get("op") != "list_voices"
    ):
        raise RequestError()
    request_id = _safe_request_id(request.get("id"))
    if request_id is None:
        raise RequestError()
    return request_id


class ClickTTSRuntime:
    """Single-provider-job runtime used by the JSONL process loop."""

    def __init__(self) -> None:
        self._state_lock = asyncio.Lock()
        self._active_task: Optional[asyncio.Task[Any]] = None
        self._active_request_id: Optional[str] = None
        self._active_partials: Tuple[Path, ...] = ()

    @property
    def active_request_id(self) -> Optional[str]:
        return self._active_request_id

    async def cancel(self, target_id: Optional[str] = None) -> bool:
        async with self._state_lock:
            task = self._active_task
            active_id = self._active_request_id
            if task is None or task.done():
                return False
            if target_id is not None and target_id != active_id:
                return False
            task.cancel()
            return True

    async def _claim_provider_job(self, request_id: str) -> bool:
        async with self._state_lock:
            if self._active_task is not None and not self._active_task.done():
                return False
            self._active_task = asyncio.current_task()
            self._active_request_id = request_id
            return True

    async def _release_provider_job(self) -> None:
        async with self._state_lock:
            if self._active_task is asyncio.current_task():
                self._active_task = None
                self._active_request_id = None

    async def _synthesis_attempt(
        self,
        *,
        text: str,
        voice: str,
        settings: Mapping[str, str],
        partial_audio: Path,
    ) -> Tuple[list[Dict[str, int | str]], int]:
        _remove_paths((partial_audio,))
        communicate = edge_tts.Communicate(
            text,
            voice,
            rate=settings["rate"],
            volume=settings["volume"],
            pitch=settings["pitch"],
            boundary=settings["boundary"],
        )
        boundaries: list[Dict[str, int | str]] = []
        with partial_audio.open("xb") as audio_handle:
            partial_audio.chmod(0o600)
            async for chunk in communicate.stream():
                chunk_type = chunk.get("type") if isinstance(chunk, dict) else None
                if chunk_type == "audio":
                    data = chunk.get("data")
                    if isinstance(data, (bytes, bytearray)):
                        audio_handle.write(data)
                elif chunk_type in {"SentenceBoundary", "WordBoundary"}:
                    offset = chunk.get("offset")
                    duration = chunk.get("duration")
                    if (
                        isinstance(offset, int)
                        and not isinstance(offset, bool)
                        and isinstance(duration, int)
                        and not isinstance(duration, bool)
                    ):
                        boundaries.append(
                            {
                                "ordinal": len(boundaries),
                                "type": chunk_type,
                                "offset": offset,
                                "duration": duration,
                            }
                        )
            _fsync_file(audio_handle)

        if not _looks_like_mp3(partial_audio):
            raise AudioValidationError("empty_audio")
        audio_duration_ms = _mp3_duration_ms(partial_audio)
        if not _validate_boundaries(boundaries):
            raise AudioValidationError("invalid_boundary")
        return boundaries, audio_duration_ms

    async def list_voices(self, request: Any) -> Dict[str, Any]:
        started = time.monotonic()
        request_id = (
            _safe_request_id(request.get("id")) if isinstance(request, dict) else None
        )
        try:
            request_id = _validate_list_voices_request(request)
        except RequestError:
            return _error_result(
                request_id,
                "list_voices",
                "invalid_request",
                attempt_count=0,
                list_elapsed_ms=_elapsed_ms(started),
            )
        if _edge_tts_version() != EXPECTED_EDGE_TTS_VERSION:
            return _error_result(
                request_id,
                "list_voices",
                "runtime_version_mismatch",
                attempt_count=0,
                list_elapsed_ms=_elapsed_ms(started),
            )
        try:
            _ensure_private_cache_hierarchy()
        except BaseException:
            return _error_result(
                request_id,
                "list_voices",
                "internal_error",
                attempt_count=0,
                list_elapsed_ms=_elapsed_ms(started),
            )
        if not await self._claim_provider_job(request_id):
            return _error_result(
                request_id,
                "list_voices",
                "busy",
                attempt_count=0,
                list_elapsed_ms=_elapsed_ms(started),
            )

        attempt_count = 0
        try:
            while attempt_count < MAX_PROVIDER_ATTEMPTS:
                attempt_count += 1
                try:
                    voices = await asyncio.wait_for(
                        edge_tts.list_voices(),
                        timeout=VOICE_LIST_ATTEMPT_TIMEOUT_SECONDS,
                    )
                    selected = _selected_voice_result(voices)
                except asyncio.CancelledError:
                    return _error_result(
                        request_id,
                        "list_voices",
                        "canceled",
                        attempt_count=attempt_count,
                        list_elapsed_ms=_elapsed_ms(started),
                    )
                except BaseException as error:
                    category, retryable = _classify_provider_error(error)
                    if not retryable:
                        return _error_result(
                            request_id,
                            "list_voices",
                            category,
                            attempt_count=attempt_count,
                            list_elapsed_ms=_elapsed_ms(started),
                        )
                    try:
                        retry_after_ms = _record_retryable_failure()
                    except BaseException:
                        return _error_result(
                            request_id,
                            "list_voices",
                            "internal_error",
                            attempt_count=attempt_count,
                            list_elapsed_ms=_elapsed_ms(started),
                        )
                    if retry_after_ms > 0:
                        return _error_result(
                            request_id,
                            "list_voices",
                            category,
                            retryable=True,
                            retry_after_ms=retry_after_ms,
                            attempt_count=attempt_count,
                            list_elapsed_ms=_elapsed_ms(started),
                        )
                    if attempt_count >= MAX_PROVIDER_ATTEMPTS:
                        return _error_result(
                            request_id,
                            "list_voices",
                            category,
                            retryable=True,
                            attempt_count=attempt_count,
                            list_elapsed_ms=_elapsed_ms(started),
                        )
                    await _sleep_before_retry(attempt_count)
                    continue

                try:
                    _record_provider_success()
                except BaseException:
                    return _error_result(
                        request_id,
                        "list_voices",
                        "internal_error",
                        attempt_count=attempt_count,
                        list_elapsed_ms=_elapsed_ms(started),
                    )
                return _ok_result(
                    request_id,
                    "list_voices",
                    voices=selected,
                    attempt_count=attempt_count,
                    list_elapsed_ms=_elapsed_ms(started),
                )
            return _error_result(
                request_id,
                "list_voices",
                "internal_error",
                attempt_count=attempt_count,
                list_elapsed_ms=_elapsed_ms(started),
            )
        finally:
            await self._release_provider_job()

    async def synthesize(self, request: Any) -> Dict[str, Any]:
        started = time.monotonic()
        request_id = (
            _safe_request_id(request.get("id")) if isinstance(request, dict) else None
        )
        try:
            (
                request_id,
                text,
                voice,
                context,
                settings,
                text_hash,
                cache_key,
            ) = _validate_synthesize_request(request)
        except RequestError:
            return _error_result(
                request_id,
                "synthesize",
                "invalid_request",
                attempt_count=0,
                synthesis_elapsed_ms=_elapsed_ms(started),
            )

        if _edge_tts_version() != EXPECTED_EDGE_TTS_VERSION:
            return _error_result(
                request_id,
                "synthesize",
                "runtime_version_mismatch",
                attempt_count=0,
                synthesis_elapsed_ms=_elapsed_ms(started),
            )

        try:
            _ensure_private_cache_hierarchy()
            cached = _load_cache_hit(cache_key, voice, text_hash, context)
        except BaseException:
            return _error_result(
                request_id,
                "synthesize",
                "internal_error",
                attempt_count=0,
                synthesis_elapsed_ms=_elapsed_ms(started),
            )
        if cached is not None:
            return _ok_result(
                request_id,
                "synthesize",
                **cached,
                attempt_count=0,
                synthesis_elapsed_ms=_elapsed_ms(started),
            )

        if not _has_current_disclosure(request):
            return _error_result(
                request_id,
                "synthesize",
                "disclosure_required",
                attempt_count=0,
                synthesis_elapsed_ms=_elapsed_ms(started),
            )

        try:
            retry_after_ms = _circuit_retry_after_ms()
        except BaseException:
            return _error_result(
                request_id,
                "synthesize",
                "internal_error",
                attempt_count=0,
                synthesis_elapsed_ms=_elapsed_ms(started),
            )
        if retry_after_ms > 0:
            return _error_result(
                request_id,
                "synthesize",
                "circuit_open",
                retryable=True,
                retry_after_ms=retry_after_ms,
                attempt_count=0,
                synthesis_elapsed_ms=_elapsed_ms(started),
            )

        if not await self._claim_provider_job(request_id):
            return _error_result(
                request_id,
                "synthesize",
                "busy",
                attempt_count=0,
                synthesis_elapsed_ms=_elapsed_ms(started),
            )

        paths = _cache_paths(cache_key)
        attempt_count = 0
        try:
            _ensure_private_directory(paths["shard"])
            token = f"{os.getpid()}.{uuid.uuid4().hex}"
            partial_audio = paths["audio"].with_name(
                paths["audio"].name + f".partial.{token}"
            )
            partial_metadata = paths["metadata"].with_name(
                paths["metadata"].name + f".partial.{token}"
            )
            partial_manifest = paths["manifest"].with_name(
                paths["manifest"].name + f".partial.{token}"
            )
            self._active_partials = (
                partial_audio,
                partial_metadata,
                partial_manifest,
            )

            boundaries: list[Dict[str, int | str]]
            audio_duration_ms: int
            while attempt_count < MAX_PROVIDER_ATTEMPTS:
                attempt_count += 1
                try:
                    boundaries, audio_duration_ms = await asyncio.wait_for(
                        self._synthesis_attempt(
                            text=text,
                            voice=voice,
                            settings=settings,
                            partial_audio=partial_audio,
                        ),
                        timeout=SYNTHESIS_ATTEMPT_TIMEOUT_SECONDS,
                    )
                except asyncio.CancelledError:
                    return _error_result(
                        request_id,
                        "synthesize",
                        "canceled",
                        attempt_count=attempt_count,
                        synthesis_elapsed_ms=_elapsed_ms(started),
                    )
                except AudioValidationError as error:
                    return _error_result(
                        request_id,
                        "synthesize",
                        error.category,
                        attempt_count=attempt_count,
                        synthesis_elapsed_ms=_elapsed_ms(started),
                    )
                except BaseException as error:
                    category, retryable = _classify_provider_error(error)
                    if not retryable:
                        return _error_result(
                            request_id,
                            "synthesize",
                            category,
                            attempt_count=attempt_count,
                            synthesis_elapsed_ms=_elapsed_ms(started),
                        )
                    try:
                        retry_after_ms = _record_retryable_failure()
                    except BaseException:
                        return _error_result(
                            request_id,
                            "synthesize",
                            "internal_error",
                            attempt_count=attempt_count,
                            synthesis_elapsed_ms=_elapsed_ms(started),
                        )
                    if retry_after_ms > 0:
                        return _error_result(
                            request_id,
                            "synthesize",
                            category,
                            retryable=True,
                            retry_after_ms=retry_after_ms,
                            attempt_count=attempt_count,
                            synthesis_elapsed_ms=_elapsed_ms(started),
                        )
                    if attempt_count >= MAX_PROVIDER_ATTEMPTS:
                        return _error_result(
                            request_id,
                            "synthesize",
                            category,
                            retryable=True,
                            attempt_count=attempt_count,
                            synthesis_elapsed_ms=_elapsed_ms(started),
                        )
                    await _sleep_before_retry(attempt_count)
                    continue
                try:
                    _record_provider_success()
                except BaseException:
                    return _error_result(
                        request_id,
                        "synthesize",
                        "internal_error",
                        attempt_count=attempt_count,
                        synthesis_elapsed_ms=_elapsed_ms(started),
                    )
                break
            else:
                return _error_result(
                    request_id,
                    "synthesize",
                    "internal_error",
                    attempt_count=attempt_count,
                    synthesis_elapsed_ms=_elapsed_ms(started),
                )

            with partial_metadata.open("x", encoding="utf-8") as metadata_handle:
                partial_metadata.chmod(0o600)
                json.dump(
                    boundaries,
                    metadata_handle,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                metadata_handle.write("\n")
                _fsync_file(metadata_handle)

            synthesis_elapsed_ms = _elapsed_ms(started)
            synthesis_rtf = round(
                synthesis_elapsed_ms / max(1, audio_duration_ms),
                6,
            )
            manifest = {
                "schema_version": 1,
                "cache_key": cache_key,
                "backend_id": "edge_online",
                "voice": voice,
                "fixture_id": context["fixture_id"],
                "chapter_locator": context["chapter_locator"],
                "locator_range": context["locator_range"],
                "text_sha256": text_hash,
                "char_count": len(text),
                "edge_tts_version": _edge_tts_version(),
                "helper_version": HELPER_VERSION,
                "disclosure_revision": DISCLOSURE_REVISION,
                "audio_file": paths["audio"].name,
                "metadata_file": paths["metadata"].name,
                "audio_sha256": _sha256_file(partial_audio),
                "metadata_sha256": _sha256_file(partial_metadata),
                "audio_bytes": partial_audio.stat().st_size,
                "boundary_count": len(boundaries),
                "attempt_count": attempt_count,
                "synthesis_elapsed_ms": synthesis_elapsed_ms,
                "audio_duration_ms": audio_duration_ms,
                "synthesis_rtf": synthesis_rtf,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            with partial_manifest.open("x", encoding="utf-8") as manifest_handle:
                partial_manifest.chmod(0o600)
                json.dump(
                    manifest,
                    manifest_handle,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                manifest_handle.write("\n")
                _fsync_file(manifest_handle)

            for partial in self._active_partials:
                if stat.S_IMODE(partial.stat().st_mode) != 0o600:
                    raise AudioValidationError("internal_error")

            os.replace(partial_audio, paths["audio"])
            os.replace(partial_metadata, paths["metadata"])
            os.replace(partial_manifest, paths["manifest"])
            _fsync_directory(paths["shard"])

            return _ok_result(
                request_id,
                "synthesize",
                source="online",
                cache_key=cache_key,
                audio_path=str(paths["audio"]),
                metadata_path=str(paths["metadata"]),
                manifest_path=str(paths["manifest"]),
                audio_bytes=manifest["audio_bytes"],
                boundary_count=manifest["boundary_count"],
                char_count=manifest["char_count"],
                fixture_id=manifest["fixture_id"],
                chapter_locator=manifest["chapter_locator"],
                locator_range=manifest["locator_range"],
                attempt_count=manifest["attempt_count"],
                synthesis_elapsed_ms=manifest["synthesis_elapsed_ms"],
                audio_duration_ms=manifest["audio_duration_ms"],
                synthesis_rtf=manifest["synthesis_rtf"],
            )
        except asyncio.CancelledError:
            return _error_result(
                request_id,
                "synthesize",
                "canceled",
                attempt_count=attempt_count,
                synthesis_elapsed_ms=_elapsed_ms(started),
            )
        except AudioValidationError as error:
            return _error_result(
                request_id,
                "synthesize",
                error.category,
                attempt_count=attempt_count,
                synthesis_elapsed_ms=_elapsed_ms(started),
            )
        except BaseException:
            return _error_result(
                request_id,
                "synthesize",
                "internal_error",
                attempt_count=attempt_count,
                synthesis_elapsed_ms=_elapsed_ms(started),
            )
        finally:
            _remove_paths(self._active_partials)
            self._active_partials = ()
            await self._release_provider_job()


def ready_message() -> Dict[str, Any]:
    return {
        "type": "ready",
        "protocol_version": PROTOCOL_VERSION,
        "helper_version": HELPER_VERSION,
        "edge_tts_version": _edge_tts_version(),
        "runtime_version_ok": _edge_tts_version() == EXPECTED_EDGE_TTS_VERSION,
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "pgid": os.getpgrp(),
        "disclosure_revision": DISCLOSURE_REVISION,
        "cache_namespace": CACHE_NAMESPACE,
        "capabilities": {
            "operations": ["synthesize", "list_voices", "cancel", "shutdown"],
            "max_provider_attempts": MAX_PROVIDER_ATTEMPTS,
            "circuit_breaker": {
                "failure_threshold": CIRCUIT_FAILURE_THRESHOLD,
                "open_seconds": CIRCUIT_RESET_SECONDS,
            },
        },
        "network": {
            "hostname": NETWORK_HOSTNAME,
            "protocol": NETWORK_PROTOCOL,
            "port": NETWORK_PORT,
            "proxy_mode": NETWORK_MODE,
            "tls_verification": True,
        },
    }


def _emit(value: Mapping[str, Any]) -> None:
    line = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _ensure_own_process_group() -> bool:
    try:
        if os.getpgrp() != os.getpid():
            os.setpgid(0, 0)
    except OSError:
        pass
    return os.getpgrp() == os.getpid()


async def _read_json_line() -> Tuple[str, Any]:
    line = await asyncio.to_thread(sys.stdin.buffer.readline, MAX_INPUT_LINE_BYTES + 1)
    if not line:
        return "eof", None
    if len(line) > MAX_INPUT_LINE_BYTES or not line.endswith(b"\n"):
        return "invalid", None
    try:
        return "ok", json.loads(line.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return "invalid", None


async def run_jsonl() -> int:
    os.umask(0o077)
    if not _ensure_own_process_group():
        _emit(_error_result(None, "startup", "internal_error"))
        return 70
    if not _network_environment_is_private():
        _emit(_error_result(None, "startup", "internal_error"))
        return 70
    lock_descriptor: Optional[int] = None
    try:
        _ensure_private_cache_hierarchy()
        lock_descriptor = _acquire_process_lock()
        if lock_descriptor is None:
            _emit(_error_result(None, "startup", "busy"))
            return 75
        cleanup_partials()
        cleanup_stale_circuit_partials()
    except BaseException:
        _emit(_error_result(None, "startup", "internal_error"))
        return 70
    try:
        return await _run_locked_jsonl()
    finally:
        cleanup_stale_circuit_partials()
        _release_process_lock(lock_descriptor)


async def _run_locked_jsonl() -> int:
    runtime = ClickTTSRuntime()
    _emit(ready_message())
    input_task: Optional[asyncio.Task[Any]] = asyncio.create_task(_read_json_line())
    generation_task: Optional[asyncio.Task[Any]] = None
    generation_op: Optional[str] = None
    stopping = False
    loop = asyncio.get_running_loop()

    def request_stop() -> None:
        nonlocal stopping
        stopping = True
        if generation_task is not None:
            generation_task.cancel()

    for name in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(name, request_stop)
        except (NotImplementedError, RuntimeError):
            pass

    while not stopping:
        watched = {task for task in (input_task, generation_task) if task is not None}
        if not watched:
            break
        done, _pending = await asyncio.wait(
            watched, return_when=asyncio.FIRST_COMPLETED
        )

        if generation_task is not None and generation_task in done:
            try:
                _emit(generation_task.result())
            except BaseException:
                _emit(
                    _error_result(
                        runtime.active_request_id,
                        generation_op or "unknown",
                        "internal_error",
                    )
                )
            generation_task = None
            generation_op = None

        if input_task is not None and input_task in done:
            state, request = input_task.result()
            input_task = None
            if state == "eof":
                stopping = True
                break
            input_task = asyncio.create_task(_read_json_line())
            if state != "ok" or not isinstance(request, dict):
                _emit(_error_result(None, "unknown", "invalid_request"))
                continue

            request_id = _safe_request_id(request.get("id"))
            op = request.get("op")
            if request_id is None or not isinstance(op, str):
                _emit(_error_result(None, "unknown", "invalid_request"))
            elif op == "synthesize":
                if generation_task is not None:
                    _emit(_error_result(request_id, op, "busy"))
                else:
                    generation_task = asyncio.create_task(runtime.synthesize(request))
                    generation_op = op
            elif op == "list_voices":
                if generation_task is not None:
                    _emit(_error_result(request_id, op, "busy"))
                else:
                    generation_task = asyncio.create_task(runtime.list_voices(request))
                    generation_op = op
            elif op == "cancel":
                if set(request) - {"type", "id", "op", "target_id"}:
                    _emit(_error_result(request_id, op, "invalid_request"))
                    continue
                target_id = _safe_request_id(request.get("target_id"))
                if request.get("target_id") is not None and target_id is None:
                    _emit(_error_result(request_id, op, "invalid_request"))
                    continue
                canceled = await runtime.cancel(target_id)
                _emit(
                    _ok_result(
                        request_id,
                        op,
                        canceled=canceled,
                        target_id=target_id,
                    )
                )
            elif op == "shutdown":
                if set(request) - {"type", "id", "op"}:
                    _emit(_error_result(request_id, op, "invalid_request"))
                    continue
                await runtime.cancel()
                _emit(_ok_result(request_id, op))
                stopping = True
            else:
                _emit(_error_result(request_id, op, "invalid_request"))

    if input_task is not None:
        input_task.cancel()
    if generation_task is not None:
        generation_task.cancel()
        try:
            result = await generation_task
            _emit(result)
        except BaseException:
            pass
    cleanup_partials()
    return 0


def main() -> int:
    return asyncio.run(run_jsonl())


if __name__ == "__main__":
    raise SystemExit(main())
