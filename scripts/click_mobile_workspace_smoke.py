#!/usr/bin/env python3
from __future__ import annotations

import base64
import atexit
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = ROOT / ".venv-reader-api" / "bin" / "python"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    import psycopg
    from psycopg import sql
except ModuleNotFoundError:
    if VENV_PYTHON.exists() and os.environ.get("CLICK_MOBILE_SMOKE_REEXEC") != "1":
        os.environ["CLICK_MOBILE_SMOKE_REEXEC"] = "1"
        os.environ["VIRTUAL_ENV"] = str(ROOT / ".venv-reader-api")
        os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])
    raise


DATABASE_NAME = "sentence_reader_click_mobile_workspace_test"
MAINTENANCE_URL = "postgresql://localhost/postgres"
DATABASE_URL = f"postgresql://localhost/{DATABASE_NAME}"


def recreate_database() -> None:
    with psycopg.connect(MAINTENANCE_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(DATABASE_NAME)))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DATABASE_NAME)))


def drop_database() -> None:
    with psycopg.connect(MAINTENANCE_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(DATABASE_NAME)))


def apply_migrations() -> None:
    completed = subprocess.run(
        [str(VENV_PYTHON), str(ROOT / "scripts" / "reader_pg_migrate.py"), "--database-url", DATABASE_URL],
        cwd=ROOT,
        env={**os.environ, "READER_DATABASE_URL": DATABASE_URL},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    require(completed.returncode == 0, completed.stdout)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    recreate_database()
    apply_migrations()
    atexit.register(drop_database)
    with tempfile.TemporaryDirectory(prefix="click-mobile-workspace-") as tmp:
        os.environ["CLICK_APP_SUPPORT_DIR"] = str(Path(tmp) / "Click")
        os.environ["CLICK_RECORDINGS_ROOT"] = str(Path(tmp) / "Recordings")
        os.environ["READER_DATABASE_URL"] = DATABASE_URL
        from reader_api.app import app  # noqa: PLC0415 - env must be set first.

        client = TestClient(app)

        for path, markers in {
            "/home": ["本地工作台", "Click 阅读", "Tingle", "Hermes", "/library", "/tingle", "/hermes", "entry-caption", "font-size:17px"],
            "/recordings": [
                "/v1/recordings",
                "MediaRecorder",
                "开始录音",
                "系统录音 / 上传音频",
                'capture="microphone"',
                "recordPanel",
                "recordTimer",
                "正在请求麦克风权限",
                "startTimer",
                "nativeAudioAvailable",
                "ClickNativeAudio.startRecording",
                "ClickNativeAudio.stopRecording",
                "__clickNativeAudioDidStart",
                "__clickNativeAudioDidUpload",
                "__clickNativeAudioDidError",
                "Android App 正在原生录音",
                "uploadAudioBlob",
                "recordingApiAvailable",
                "isAppleMobileCapture",
                "prefersSystemAudioCapture",
                "return isAppleMobileCapture() && !recordingApiAvailable();",
                "打开系统录音，完成后由 Mac 端处理",
                "record-action",
            ],
            "/hermes": [
                "/v1/runtime/chat",
                "/v1/voice/message",
                "class=\"actions\"",
                "min-height:68px",
                "grid-template-columns:1fr 1fr",
                'id="send"',
                'id="voiceFile"',
                'capture="microphone"',
                'data-recording="false"',
                "setVoiceButton",
                "enterkeyhint=\"send\"",
                "visualViewport",
                "--keyboard-inset",
                "nativeImeResizeAvailable",
                "usesNativeImeResize",
                "submitText",
                "chatPending",
                "uploadVoiceBlob",
                "nativeHermesVoiceAvailable",
                "ClickNativeAudio.startHermesVoice",
                "__clickNativeHermesVoiceDidStart",
                "__clickNativeHermesVoiceDidStop",
                "__clickNativeHermesVoiceDidUpload",
                "__clickNativeHermesVoiceDidError",
                "voiceApiAvailable",
                "isAppleMobileVoiceCapture",
                "prefersSystemVoiceCapture",
                "return isAppleMobileVoiceCapture()&&!voiceApiAvailable()",
                "正在请求麦克风权限",
                "录音中，再点一次停止并发送",
                "打开系统录音，完成后由 Mac 端处理",
            ],
        }.items():
            response = client.get(path)
            require(response.status_code == 200, f"{path} status={response.status_code}")
            text = response.text
            for marker in markers:
                require(marker in text, f"{path} missing {marker}")

        health = client.get("/v1/recordings/health")
        require(health.status_code == 200, "recordings health status")
        health_payload = health.json()
        require(health_payload["ok"] is True, "recordings health ok")
        require(health_payload["schema"] == "local.recordings.audio_asset.v1", "recording health schema")
        require(str(Path(tmp) / "Recordings") == health_payload["canonical_root"], "recording canonical root")
        require("Click/KnowledgeInbox/Recordings" in health_payload["legacy_root"], "legacy recording root")
        require(health_payload["legacy_read_only"] is True, "legacy root must be read-only")
        require(health_payload["uses_legacy_click_path_as_canonical"] is False, "legacy path must not be canonical")
        require(health_payload["uses_forbidden_hermes_recordings_path"] is False, "forbidden hermes path unused")

        access_status = client.get("/v1/mobile/access/status")
        require(access_status.status_code == 200, "access status route")
        require(access_status.json()["status"] == "local_debug", "local debug access status")
        local_lan_status = client.get("/v1/mobile/access/status", params={"device_id": "android-smoke-device"})
        require(local_lan_status.json()["status"] == "unknown", "default mobile access must fail closed")
        require(local_lan_status.json()["authorized"] is False, "unknown LAN device must not be authorized")

        synthetic_remote_host = ".".join(("100", "64", "0", "88"))
        remote_client = TestClient(app, client=(synthetic_remote_host, 12345))
        unauthorized = remote_client.post(
            "/v1/recordings",
            json={
                "audio_base64": base64.b64encode(b"blocked-audio").decode("ascii"),
                "mime_type": "audio/m4a",
                "device_id": "android-smoke-device",
            },
        )
        require(unauthorized.status_code == 403, "unauthorized mobile recording must be blocked")
        pending = client.get("/v1/mobile/access/pending")
        require(any(item["device_id"] == "android-smoke-device" for item in pending.json()["pending"]), "pending device must be listed")
        approval = client.post("/v1/mobile/access/approve", json={"device_id": "android-smoke-device", "device_name": "Smoke Android"})
        require(approval.status_code == 200, "approve status")
        token = approval.json()["access_token"]
        status = client.get("/v1/mobile/access/status", params={"device_id": "android-smoke-device", "access_token": token})
        require(status.json()["authorized"] is True, "approved device must be authorized")
        remote_health = remote_client.get(
            "/v1/recordings/health",
            headers={
                "X-Click-Device-Id": "android-smoke-device",
                "Authorization": f"Bearer {token}",
            },
        )
        require(remote_health.status_code == 200, "authenticated remote health")
        require("canonical_root" not in remote_health.json(), "remote health leaked canonical root")
        require("index" not in remote_health.json(), "remote health leaked index path")
        from fastapi import HTTPException  # noqa: PLC0415
        from starlette.requests import Request  # noqa: PLC0415
        from reader_api.mobile_workspace import require_mobile_access  # noqa: PLC0415

        remote_request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/tingle/inspirations",
                "headers": [],
                "query_string": b"",
                "client": (synthetic_remote_host, 12345),
                "server": ("click.test", 80),
                "scheme": "http",
            }
        )
        try:
            require_mobile_access(remote_request)
            raise AssertionError("strict remote request without identity was accepted")
        except HTTPException as error:
            require(error.status_code == 401, "strict remote missing identity must return 401")

        tingle_browser = TestClient(app)
        tingle_page = tingle_browser.get(
            "/tingle",
            headers={
                "X-Click-Device-Id": "android-smoke-device",
                "Authorization": f"Bearer {token}",
            },
        )
        require(tingle_page.status_code == 200, "Tingle authenticated HTML bootstrap")
        require(
            tingle_browser.cookies.get("click_device_id") == "android-smoke-device",
            "Tingle bootstrap device cookie",
        )
        require(
            tingle_browser.cookies.get("click_access_token") == token,
            "Tingle bootstrap access cookie",
        )
        tingle_history = tingle_browser.get("/tingle/inspirations")
        require(tingle_history.status_code == 200, "Tingle fetch must authenticate through HttpOnly cookies")

        fake_audio = base64.b64encode(b"not-a-real-audio-but-valid-base64").decode("ascii")
        created = client.post(
            "/v1/recordings",
            json={
                "audio_base64": fake_audio,
                "mime_type": "audio/m4a",
                "duration_seconds": 1.0,
                "device_id": "android-smoke-device",
                "access_token": token,
            },
        )
        require(created.status_code == 200, f"recording create status={created.status_code} body={created.text}")
        recording = created.json()["recording"]
        require(recording["schema"] == "local.recordings.audio_asset.v1", "recording schema")
        require(recording["status"] in {"saved", "needs_processing", "transcribed", "transcribed_needs_naming", "named"}, "recording status")
        require("audio_path" in recording, "loopback recording response lost local path")
        require("metadata_path" in recording, "loopback recording response lost local path")

        from reader_api.mobile_workspace import recording_row  # noqa: PLC0415

        rec_dir = Path(recording_row(recording["recording_id"])["metadata_path"]).parent
        require(str(rec_dir).startswith(str(Path(tmp) / "Recordings")), "recording must live under canonical root")
        require("Click/Standalone" in str(rec_dir), "default recording bucket")
        require("2026/" not in str(rec_dir) and "/07/" not in str(rec_dir), "recording path must not be month-based")
        for filename in ["original.m4a", "transcript.txt", "summary.txt", "title.txt", "metadata.json"]:
            require((rec_dir / filename).exists(), f"missing recording file {filename}")
        metadata = json.loads((rec_dir / "metadata.json").read_text(encoding="utf-8"))
        require(metadata["schema"] == "local.recordings.audio_asset.v1", "metadata schema")
        require(metadata["asset_type"] == "audio_asset", "metadata asset type")
        require(metadata["source_app"] == "Click", "metadata source app")
        require(metadata["source_feature"] == "Standalone recording", "metadata source feature")
        require(metadata["durability"] == "durable", "metadata durability")
        for key in ["audio_id", "created_at", "contexts", "transcript_status", "title_status", "summary_status"]:
            require(key in metadata, f"metadata missing {key}")
        require(metadata["storage"]["canonical_root"] == str(Path(tmp) / "Recordings"), "metadata canonical root")
        require(metadata["storage"]["legacy_read_only"] is True, "metadata legacy read-only")
        require(metadata["voice_pipeline"]["schema"] == "click.mac_voice_pipeline.v1", "recording voice pipeline schema")
        require(metadata["voice_pipeline"]["pipeline"] == "mac.local_audio.funasr.v1", "recording voice pipeline id")
        require(metadata["voice_pipeline"]["app_role"] == "capture_upload_only", "recording app role")

        listing = client.get("/v1/recordings").json()
        require(len(listing["recordings"]) == 1, "recording listing")
        patched = client.patch(
            f"/v1/recordings/{recording['recording_id']}",
            json={"title": "烟测标题", "category": "项目", "tags": ["smoke", "mobile"], "organized_status": "已整理"},
        )
        require(patched.status_code == 200, f"recording patch status={patched.status_code} body={patched.text}")
        patched_recording = patched.json()["recording"]
        require(patched_recording["title"] == "烟测标题", "patched title")
        require(patched_recording["category"] == "项目", "patched category")
        require(patched_recording["organized_status"] == "已整理", "organized status")
        require(patched_recording["user_title_override"] is True, "manual title override")

        reprocess = client.post(f"/v1/recordings/{recording['recording_id']}/reprocess", json={"dry_run": True})
        require(reprocess.status_code == 200, "reprocess dry-run route")
        require(reprocess.json()["dry_run"] is True, "reprocess dry-run response")

        audio = client.get(f"/v1/recordings/{recording['recording_id']}/audio")
        require(audio.status_code == 200, "recording audio route")

        hidden = client.post(f"/v1/recordings/{recording['recording_id']}/hide", json={"reason": "smoke"})
        require(hidden.status_code == 200, "recording hide route")
        require(hidden.json()["hidden"] is True, "hidden response")
        visible_after_hide = client.get("/v1/recordings").json()
        require(len(visible_after_hide["recordings"]) == 0, "hidden recording must leave default listing")
        hidden_listing = client.get("/v1/recordings", params={"include_hidden": "true"}).json()
        require(len(hidden_listing["recordings"]) == 1, "include_hidden listing")

        tingle_audio = base64.b64encode(b"tingle-same-original-audio").decode("ascii")
        tingle_context = {
            "schema": "click.tingle.local_metadata.v1",
            "title": "离线灵感",
            "note": "手机初始备注",
        }
        tingle_created = client.post(
            "/v1/recordings",
            json={
                "audio_base64": tingle_audio,
                "mime_type": "audio/wav",
                "duration_seconds": 1.0,
                "device_id": "android-smoke-device",
                "access_token": token,
                "client_capture_id": "android-tingle-metadata-smoke",
                "source": "click_android_native_tingle",
                "source_app": "Click",
                "source_feature": "Tingle",
                "contexts": [tingle_context],
            },
        )
        require(tingle_created.status_code == 200, tingle_created.text)
        tingle_recording = tingle_created.json()["recording"]
        require("audio_path" in tingle_recording, "loopback Tingle response lost local path")
        tingle_audio_path = Path(recording_row(tingle_recording["recording_id"])["audio_path"])
        original_audio = tingle_audio_path.read_bytes()
        original_stat = tingle_audio_path.stat()

        duplicate_context = {
            **tingle_context,
            "title": "离线灵感二次标题",
            "note": "同哈希重复请求安全更新",
        }
        tingle_duplicate = client.post(
            "/v1/recordings",
            json={
                "audio_base64": tingle_audio,
                "mime_type": "audio/wav",
                "duration_seconds": 1.0,
                "device_id": "android-smoke-device",
                "access_token": token,
                "client_capture_id": "android-tingle-metadata-smoke",
                "source": "click_android_native_tingle",
                "source_app": "Click",
                "source_feature": "Tingle",
                "contexts": [duplicate_context],
            },
        )
        require(tingle_duplicate.status_code == 200, tingle_duplicate.text)
        require(tingle_duplicate.json()["duplicate"] is True, "Tingle retry must be idempotent")
        require(
            tingle_duplicate.json()["recording"]["title"] == "离线灵感二次标题",
            "duplicate Tingle request must safely merge local title",
        )
        require(tingle_audio_path.read_bytes() == original_audio, "duplicate request changed original audio")

        metadata_only = client.patch(
            f"/v1/recordings/{tingle_recording['recording_id']}",
            json={
                "title": "只补元数据",
                "note": "PATCH 不重传音频",
                "expected_audio_hash": tingle_recording["audio_hash"],
                "client_capture_id": "android-tingle-metadata-smoke",
            },
        )
        require(metadata_only.status_code == 200, metadata_only.text)
        require(metadata_only.json()["recording"]["title"] == "只补元数据", "metadata-only title")
        local_contexts = metadata_only.json()["recording"]["contexts"]
        require(
            any(
                item.get("schema") == "click.tingle.local_metadata.v1"
                and item.get("note") == "PATCH 不重传音频"
                for item in local_contexts
            ),
            "metadata-only note",
        )
        require(tingle_audio_path.read_bytes() == original_audio, "metadata PATCH changed original audio")
        require(tingle_audio_path.stat().st_ino == original_stat.st_ino, "metadata PATCH replaced original audio")
        wrong_hash = client.patch(
            f"/v1/recordings/{tingle_recording['recording_id']}",
            json={
                "note": "不应保存",
                "expected_audio_hash": "0" * 64,
                "client_capture_id": "android-tingle-metadata-smoke",
            },
        )
        require(wrong_hash.status_code == 409, "mismatched Tingle metadata receipt must fail closed")

        diagnostics = client.get("/v1/mobile/diagnostics")
        require(diagnostics.status_code == 200, "diagnostics status")
        diag = diagnostics.json()
        require(diag["recordings"]["ok"] is True, "diagnostics recordings")
        require(diag["reader_api"]["library"] == "/library", "diagnostics reader")
        require(diag["edge_tts"]["voice"] == "zh-CN-YunjianNeural", "diagnostics edge tts voice")
        require(diag["recordings_store"]["canonical_root"] == str(Path(tmp) / "Recordings"), "diagnostics canonical root")
        require(diag["recordings_store"]["legacy_read_only"] is True, "diagnostics legacy read-only")
        require(diag["voice_pipeline"]["schema"] == "click.mac_voice_pipeline.v1", "diagnostics voice pipeline schema")
        require(diag["voice_pipeline"]["shared_by"] == ["reader_audio_note", "recording_asset", "hermes_voice_message"], "diagnostics shared voice pipeline")

        runtime_health = client.get("/v1/runtime/health")
        require(runtime_health.status_code == 200, "runtime health proxy route")

        voice = client.post(
            "/v1/voice/message",
            json={
                "audio_base64": fake_audio,
                "mime_type": "audio/m4a",
                "duration_seconds": 1.0,
                "device_id": "android-smoke-device",
                "access_token": token,
                "tts": False,
            },
        )
        require(voice.status_code == 200, f"voice route status={voice.status_code} body={voice.text}")
        voice_payload = voice.json()
        require(voice_payload["schema"] == "click.hermes_mobile.voice_message.v1", "voice schema")
        voice_detail = client.get(f"/v1/voice/message/{voice_payload['voice_id']}")
        require(voice_detail.status_code == 200, "voice detail route")
        voice_detail_payload = voice_detail.json()
        require(voice_detail_payload["schema"] == "click.hermes_mobile.voice_message.v1", "voice detail schema")
        require(voice_detail_payload["metadata"]["voice_pipeline"]["pipeline"] == "mac.local_audio.funasr.v1", "Hermes voice pipeline id")
        require(voice_detail_payload["metadata"]["voice_pipeline"]["purpose"] == "hermes_voice_message", "Hermes voice pipeline purpose")

        revoked = client.post("/v1/mobile/access/revoke", json={"device_id": "android-smoke-device"})
        require(revoked.status_code == 200, "revoke route")
        revoked_status = client.get("/v1/mobile/access/status", params={"device_id": "android-smoke-device", "access_token": token})
        require(revoked_status.json()["authorized"] is False, "revoked device must not be authorized")

        forbidden = Path(tmp) / "Click" / "HermesGateway" / "Recordings"
        require(not forbidden.exists(), "must not create HermesGateway/Recordings inside Click app support")
        require(not (Path(tmp) / "Click" / "KnowledgeInbox" / "Recordings").exists(), "must not write new recordings to legacy Click path")

    drop_database()
    atexit.unregister(drop_database)
    print("click mobile workspace smoke passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"click mobile workspace smoke failed: {exc}")
        raise SystemExit(1)
