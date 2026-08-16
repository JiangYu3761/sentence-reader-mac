from __future__ import annotations

import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from reader_api import db
from reader_api.mobile_workspace import (
    audio_extension,
    decode_audio_base64,
    require_mobile_access,
)
from reader_api.voice_processing import wake_voice_processing_worker
from reader_api.voice_workbench import click_voice_workbench_html
from reader_api.voice_reading_context import (
    CitationResolutionError,
    context_snapshot_matches,
    context_request_hash,
    resolve_snapshot_citation,
)
from reader_api.voice_inbox_service import (
    VOICE_RECORD_SCHEMA,
    append_discussion_message,
    archive_voice_record,
    clean_title,
    create_discussion,
    create_transcript_version,
    create_voice_record_from_audio,
    enqueue_processing_job,
    effective_transcript,
    discussion_message_by_client_id,
    get_discussion_or_404,
    list_actions_for_record,
    list_action_receipts,
    get_context_snapshot,
    list_context_snapshots,
    list_discussion_messages,
    list_discussions,
    list_processing_jobs,
    list_runs_for_record,
    list_transcript_versions,
    list_voice_records,
    list_voice_record_links,
    new_id,
    safe_intent_type,
    safe_record_status,
    safe_context_scope,
    safe_source,
    seconds_to_ms,
    soft_delete_voice_record,
    stable_id,
    upsert_voice_record,
    update_discussion_message,
    request_voice_action_apply,
    get_voice_record_or_404,
    voice_record_by_id,
    json_hash,
    latest_transcript_version,
    preferred_raw_transcript_version,
)


router = APIRouter()


class VoiceRecordCreate(BaseModel):
    audio_base64: Optional[str] = None
    audio_path: Optional[str] = None
    mime_type: str = "audio/webm"
    duration_seconds: Optional[float] = None
    source: str = "mac"
    source_platform: Optional[str] = None
    device_id: Optional[str] = None
    origin_ref: Optional[str] = None
    title: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    auto_transcribe: bool = False
    auto_understand: bool = False


class VoiceRecordPatch(BaseModel):
    title: Optional[str] = None
    summary: Optional[str] = None
    intent_type: Optional[str] = None
    status: Optional[str] = None
    project_hint: Optional[str] = None
    priority: Optional[int] = None


class VoiceActionApply(BaseModel):
    confirmed: bool = False


class VoiceTranscriptEdit(BaseModel):
    content: str = Field(min_length=1, max_length=200_000)


class VoiceContextBuild(BaseModel):
    scope: str = "recent_reading"
    selected_book_ids: list[str] = Field(default_factory=list, max_length=20)


class VoiceDiscussionCreate(BaseModel):
    title: Optional[str] = None
    context_scope: str = "recent_reading"
    selected_book_ids: list[str] = Field(default_factory=list, max_length=20)


class VoiceDiscussionMessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)
    client_message_id: str = Field(min_length=8, max_length=160)


def voice_inbox_root() -> Path:
    return Path(
        os.getenv(
            "CLICK_VOICE_INBOX_ROOT",
            str(Path.home() / "Library" / "Application Support" / "Click" / "VoiceInbox"),
        )
    ).expanduser()


def voice_record_storage_dir(voice_record_id: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", voice_record_id).strip("-") or stable_id("vr", voice_record_id)
    return voice_inbox_root() / "Records" / safe_id


def voice_output_dir() -> Path:
    return voice_inbox_root() / "Outputs"


def media_type_for_path(path: Path, fallback: str = "application/octet-stream") -> str:
    return {
        ".m4a": "audio/mp4",
        ".mp4": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".webm": "audio/webm",
        ".ogg": "audio/ogg",
    }.get(path.suffix.lower(), mimetypes.guess_type(str(path))[0] or fallback)


def write_uploaded_audio(payload: VoiceRecordCreate) -> tuple[Path, str]:
    if payload.audio_base64:
        audio_data = decode_audio_base64(payload.audio_base64)
        temp_id = new_id("vr")
        directory = voice_record_storage_dir(temp_id)
        directory.mkdir(parents=True, exist_ok=False)
        path = directory / f"original{audio_extension(payload.mime_type)}"
        path.write_bytes(audio_data)
        return path, temp_id
    if payload.audio_path:
        path = Path(payload.audio_path).expanduser()
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=422, detail="audio_path is missing")
        return path, stable_id("vr", str(path), path.stat().st_size)
    raise HTTPException(status_code=422, detail="audio_base64 or audio_path is required")


def compact_record(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "audio_url": f"/voice-records/{row['id']}/audio",
        "actions_url": f"/voice-records/{row['id']}/actions",
        "runs_url": f"/voice-records/{row['id']}/runs",
    }


def enqueue_transcription(record: dict[str, Any], *, retry_failed: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    job = enqueue_processing_job(
        str(record["id"]),
        "transcribe",
        payload={"audio_hash": record.get("audio_hash")},
        input_hash=str(record.get("audio_hash") or json_hash(record.get("audio_uri"))),
        retry_failed=retry_failed,
    )
    if job.get("status") == "pending":
        with db.connect() as conn:
            row = conn.execute(
                """
                UPDATE reader.voice_records
                SET status = 'transcribe_pending', failure_code = NULL,
                    failure_message = NULL, updated_at = now()
                WHERE id = %s
                RETURNING *
                """,
                (record["id"],),
            ).fetchone()
        record = dict(row)
    wake_voice_processing_worker()
    return record, job


def enqueue_cleanup(record: dict[str, Any], *, retry_failed: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    raw_version = preferred_raw_transcript_version(str(record["id"]))
    if not raw_version:
        raise HTTPException(status_code=409, detail="voice record has no raw FunASR transcript")
    job = enqueue_processing_job(
        str(record["id"]),
        "clean_transcript",
        payload={"transcript_version_id": raw_version["id"]},
        input_hash=str(raw_version["content_hash"]),
        retry_failed=retry_failed,
    )
    if job.get("status") == "pending":
        with db.connect() as conn:
            row = conn.execute(
                """
                UPDATE reader.voice_records
                SET status = 'cleanup_pending', failure_code = NULL,
                    failure_message = NULL, updated_at = now()
                WHERE id = %s
                RETURNING *
                """,
                (record["id"],),
            ).fetchone()
        record = dict(row)
    wake_voice_processing_worker()
    return record, job


def enqueue_understanding(record: dict[str, Any], *, retry_failed: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    transcript, version = effective_transcript(str(record["id"]))
    if not transcript.strip():
        raise HTTPException(status_code=409, detail="voice record has no transcript")
    job = enqueue_processing_job(
        str(record["id"]),
        "understand",
        payload={"transcript_version_id": (version or {}).get("id")},
        input_hash=str((version or {}).get("content_hash") or json_hash(transcript)),
        retry_failed=retry_failed,
    )
    if job.get("status") == "pending":
        with db.connect() as conn:
            row = conn.execute(
                """
                UPDATE reader.voice_records
                SET status = 'understand_pending', failure_code = NULL,
                    failure_message = NULL, updated_at = now()
                WHERE id = %s
                RETURNING *
                """,
                (record["id"],),
            ).fetchone()
        record = dict(row)
    wake_voice_processing_worker()
    return record, job


def legacy_voice_inbox_html() -> str:
    return """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <title>Voice Inbox</title>
  <style>
    :root{color-scheme:light;background:#f7f5f0;color:#161616;font-family:-apple-system,BlinkMacSystemFont,"Microsoft YaHei",sans-serif}
    *{box-sizing:border-box}body{margin:0;background:#f7f5f0;color:#161616}
    button,input,select,textarea{font:inherit}button{border:0;border-radius:8px;background:#1e293b;color:#fff;padding:9px 12px;font-weight:800;cursor:pointer}button.secondary{background:#e8e3d8;color:#1f2933}button.danger{background:#a33b35}button:disabled{opacity:.46;cursor:not-allowed}
    main{min-height:100vh;display:grid;grid-template-rows:auto 1fr}
    header{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 18px;border-bottom:1px solid #ddd6c7;background:#fffdf7}
    h1{font-size:22px;margin:0}.top-actions{display:flex;gap:8px;align-items:center}
    .shell{display:grid;grid-template-columns:184px minmax(310px,440px) minmax(360px,1fr);min-height:0}
    nav{border-right:1px solid #ddd6c7;background:#f0ece3;padding:14px;display:flex;flex-direction:column;gap:8px}
    nav button{width:100%;text-align:left;background:transparent;color:#2c2c2c;padding:10px;border-radius:8px}nav button.active{background:#1e293b;color:#fff}
    .capture{padding:14px;border-bottom:1px solid #ddd6c7;background:#fffdf7;display:grid;gap:10px}
    .capture-row{display:grid;grid-template-columns:1fr 1fr;gap:8px}.timer{font-size:28px;font-weight:900}.hint{font-size:13px;color:#6b6254;line-height:1.45}
    .list-pane{border-right:1px solid #ddd6c7;min-height:0;display:grid;grid-template-rows:auto 1fr;background:#fbfaf6}.filters{padding:10px 12px;border-bottom:1px solid #e4ddce;display:flex;gap:8px}.filters input{width:100%;border:1px solid #d5cbbc;border-radius:8px;padding:9px;background:#fff}
    #records{overflow:auto}.record{padding:12px;border-bottom:1px solid #ece5d7;display:grid;gap:6px;cursor:pointer}.record.active{background:#e9f0f8}.record-title{font-weight:900;line-height:1.25}.meta{display:flex;gap:6px;flex-wrap:wrap;font-size:12px;color:#645b50}.pill{border-radius:999px;background:#ebe4d6;color:#39342d;padding:3px 7px}.pill.warn{background:#ffe4d6;color:#8d2f21}.pill.ok{background:#d9eadc;color:#21542c}
    .detail{padding:18px;overflow:auto;background:#fff}.detail h2{margin:0 0 6px;font-size:24px}.detail-grid{display:grid;gap:14px;max-width:900px}.section{border:1px solid #e4ddce;border-radius:8px;padding:13px;background:#fffdf9}.section h3{font-size:15px;margin:0 0 8px;color:#433c34}.transcript{white-space:pre-wrap;line-height:1.65}.actions{display:flex;gap:8px;flex-wrap:wrap}.timeline{font-size:12px;line-height:1.45;white-space:pre-wrap;max-height:240px;overflow:auto;background:#f7f5f0;border-radius:8px;padding:10px}
    audio{width:100%}.empty{padding:26px;color:#776f63;text-align:center}.error{color:#9b2f28}.statusline{font-size:13px;color:#645b50}
    @media(max-width:880px){.shell{grid-template-columns:1fr}.detail{min-height:50vh}nav{display:grid;grid-template-columns:repeat(3,1fr);border-right:0;border-bottom:1px solid #ddd6c7}.list-pane{border-right:0}.capture-row{grid-template-columns:1fr 1fr}}
  </style>
</head>
<body>
<main>
  <header>
    <h1>Voice Inbox</h1>
    <div class="top-actions"><a href="/home">首页</a><button class="secondary" id="refresh">刷新</button></div>
  </header>
  <section class="shell">
    <nav id="nav">
      <button data-status="" class="active">全部</button>
      <button data-status="transcribe_pending">待转写</button>
      <button data-status="understand_pending">待理解</button>
      <button data-status="needs_user_confirmation">需确认</button>
      <button data-status="needs_followup">需追问</button>
      <button data-status="failed">失败</button>
      <button data-status="archived">已归档</button>
    </nav>
    <section class="list-pane">
      <div class="capture">
        <div class="timer" id="timer">00:00</div>
        <div class="capture-row">
          <button id="start">开始录音</button>
          <button id="stop" class="danger" disabled>停止保存</button>
        </div>
        <div class="capture-row">
          <button class="secondary" id="pick">导入音频</button>
          <button class="secondary" id="process" disabled>转写并理解</button>
        </div>
        <input id="audioFile" type="file" accept="audio/*" capture="microphone" hidden>
        <div class="hint" id="status">录音会进入同一个 VoiceRecord，不再分裂成 Mac/手机两个列表。</div>
      </div>
      <div class="filters"><input id="search" placeholder="搜索标题、摘要、正文"></div>
      <div id="records"></div>
    </section>
    <section class="detail" id="detail"><div class="empty">选择一条语音</div></section>
  </section>
</main>
<script>
let records=[], selected=null, statusFilter='', mediaRecorder=null, chunks=[], stream=null, startedAt=0, timer=0;
const $=id=>document.getElementById(id);
function esc(s){return String(s||'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
function fmt(ms){const t=Math.max(0,Math.floor(ms/1000));return String(Math.floor(t/60)).padStart(2,'0')+':'+String(t%60).padStart(2,'0')}
function setStatus(text){$('status').textContent=text}
function chooseMime(){for(const t of ['audio/mp4','audio/webm;codecs=opus','audio/webm','audio/ogg']){if(window.MediaRecorder&&MediaRecorder.isTypeSupported(t))return t}return ''}
function dataUrl(blob){return new Promise((res,rej)=>{const r=new FileReader();r.onload=()=>res(r.result);r.onerror=rej;r.readAsDataURL(blob)})}
function startTimer(){clearInterval(timer);$('timer').textContent='00:00';timer=setInterval(()=>$('timer').textContent=fmt(Date.now()-startedAt),300)}
function stopTimer(){clearInterval(timer);timer=0}
function sourceLabel(s){return ({mac:'Mac',click_mobile:'手机',import:'导入',platform_forward:'平台',browser:'浏览器',future:'未来'}[s]||s||'未知')}
function statusLabel(s){return ({transcribe_pending:'待转写',understand_pending:'待理解',needs_user_confirmation:'需确认',needs_followup:'需追问',action_pending:'待处理',action_applied:'已处理',settled:'已沉淀',archived:'已归档',failed_transcribe:'转写失败',failed_understand:'理解失败',failed_action:'动作失败'}[s]||s||'未知')}
function statusClass(s){return s&&s.startsWith('failed')?'warn':(['settled','archived','action_applied'].includes(s)?'ok':'')}
async function api(path, options){const r=await fetch(path, options);const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.detail||d.error||('HTTP '+r.status));return d}
async function load(){
  const query=statusFilter==='failed'?'':statusFilter;
  const data=await api('/voice-records'+(query?'?status='+encodeURIComponent(query):''));
  records=data.records||[];
  if(statusFilter==='failed')records=records.filter(r=>(r.status||'').startsWith('failed'));
  renderList();
  if(selected){const exists=records.find(r=>r.id===selected.id);if(exists)selectRecord(exists.id,false);else selected=null}
}
function renderList(){
  const q=$('search').value.trim().toLowerCase();
  const shown=records.filter(r=>!q||[r.title,r.summary,r.transcript].join(' ').toLowerCase().includes(q));
  $('records').innerHTML=shown.length?shown.map(r=>`
    <article class="record ${selected&&selected.id===r.id?'active':''}" data-id="${esc(r.id)}">
      <div class="record-title">${esc(r.title||'未命名语音')}</div>
      <div class="meta"><span class="pill">${sourceLabel(r.source)}</span><span class="pill ${statusClass(r.status)}">${statusLabel(r.status)}</span><span class="pill">${esc(r.intent_type||'unknown')}</span></div>
      <div class="statusline">${new Date(r.created_at).toLocaleString()}${r.duration_ms?' · '+Math.round(r.duration_ms/1000)+' 秒':''}</div>
    </article>`).join(''):'<div class="empty">暂无语音</div>';
  document.querySelectorAll('.record').forEach(el=>el.onclick=()=>selectRecord(el.dataset.id,true));
}
async function selectRecord(id, rerender){
  const data=await api('/voice-records/'+encodeURIComponent(id));
  selected=data.record;
  $('process').disabled=false;
  if(rerender)renderList();
  renderDetail(data);
}
function renderDetail(data){
  const r=data.record, actions=data.actions||[], runs=data.runs||[];
  $('detail').innerHTML=`
    <div class="detail-grid">
      <div>
        <h2>${esc(r.title||'未命名语音')}</h2>
        <div class="meta"><span class="pill">${sourceLabel(r.source)}</span><span class="pill ${statusClass(r.status)}">${statusLabel(r.status)}</span><span class="pill">${esc(r.intent_type||'unknown')}</span></div>
      </div>
      <div class="section"><h3>原始音频</h3><audio controls src="${r.audio_url}"></audio></div>
      <div class="section"><h3>转写正文</h3><div class="transcript">${esc(r.transcript||'还没有转写。')}</div></div>
      <div class="section"><h3>Hermes 摘要</h3><div>${esc(r.summary||'还没有理解结果。')}</div>${r.failure_message?`<p class="error">${esc(r.failure_message)}</p>`:''}</div>
      <div class="section"><h3>下一步动作</h3><div class="actions">${actions.length?actions.map(a=>`<button class="${a.requires_confirmation?'':'secondary'}" data-action="${esc(a.id)}">${esc(a.title||a.action_type)}</button>`).join(''):'暂无 action'}</div></div>
      <div class="section"><h3>操作</h3><div class="actions">
        <button onclick="transcribeSelected()">转写</button>
        <button onclick="understandSelected()">Hermes 理解</button>
        <button class="secondary" onclick="archiveSelected()">归档</button>
        <button class="danger" onclick="deleteSelected()">软删除</button>
      </div></div>
      <div class="section"><h3>处理时间线</h3><div class="timeline">${esc(JSON.stringify(runs.slice(0,8),null,2))}</div></div>
    </div>`;
  document.querySelectorAll('[data-action]').forEach(btn=>btn.onclick=()=>applyAction(btn.dataset.action));
}
async function uploadBlob(blob,duration){
  const audio_base64=await dataUrl(blob);
  const data=await api('/voice-records',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({audio_base64,mime_type:blob.type||'audio/webm',duration_seconds:duration,source:'mac',source_platform:'Mac Voice Inbox'})});
  selected=data.record;
  await load();
  await selectRecord(data.record.id,true);
  setStatus('已保存 VoiceRecord，可转写并交给 Hermes。');
}
$('start').onclick=async()=>{
  if(!(navigator.mediaDevices&&navigator.mediaDevices.getUserMedia&&window.MediaRecorder)){$('audioFile').click();return}
  stream=await navigator.mediaDevices.getUserMedia({audio:true});const mt=chooseMime();chunks=[];startedAt=Date.now();
  mediaRecorder=new MediaRecorder(stream,mt?{mimeType:mt}:undefined);
  mediaRecorder.ondataavailable=e=>{if(e.data&&e.data.size)chunks.push(e.data)};
  mediaRecorder.onstop=async()=>{stopTimer();if(stream){stream.getTracks().forEach(t=>t.stop());stream=null}const blob=new Blob(chunks,{type:mt||'audio/webm'});await uploadBlob(blob,(Date.now()-startedAt)/1000);$('start').disabled=false;$('stop').disabled=true};
  mediaRecorder.start();$('start').disabled=true;$('stop').disabled=false;setStatus('正在录音');startTimer();
};
$('stop').onclick=()=>{if(mediaRecorder&&mediaRecorder.state==='recording'){setStatus('正在保存');mediaRecorder.stop()}};
window.__clickVoiceInboxStartRecording=()=>{if(!$('start').disabled){$('start').click();return true}return false};
window.__clickVoiceInboxStopRecording=()=>{if(!$('stop').disabled){$('stop').click();return true}return false};
window.__clickVoiceInboxToggleRecording=()=>{$('stop').disabled?window.__clickVoiceInboxStartRecording():window.__clickVoiceInboxStopRecording();return true};
$('pick').onclick=()=>$('audioFile').click();
$('audioFile').onchange=async()=>{const file=$('audioFile').files&&$('audioFile').files[0];if(file)await uploadBlob(file,null);$('audioFile').value=''};
async function transcribeSelected(){if(!selected)return;setStatus('正在转写');const d=await api('/voice-records/'+selected.id+'/transcribe',{method:'POST'});await selectRecord(d.record.id,true);await load();setStatus('转写完成或已记录失败');}
async function understandSelected(){if(!selected)return;setStatus('Hermes 正在理解');const d=await api('/voice-records/'+selected.id+'/understand',{method:'POST'});await selectRecord(d.record.id,true);await load();setStatus('Hermes 处理完成或已记录失败');}
$('process').onclick=async()=>{await transcribeSelected();await understandSelected()};
async function applyAction(id){if(!selected)return;const d=await api('/voice-records/'+selected.id+'/actions/'+id+'/apply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirmed:true})});await selectRecord(selected.id,true);await load();setStatus('action 已应用并验证：'+(d.action&&d.action.status||''));}
async function archiveSelected(){if(!selected)return;const d=await api('/voice-records/'+selected.id+'/archive',{method:'POST'});await load();await selectRecord(d.record.id,true)}
async function deleteSelected(){if(!selected||!confirm('只软删除记录，不删除原始音频。继续？'))return;await api('/voice-records/'+selected.id,{method:'DELETE'});selected=null;$('detail').innerHTML='<div class="empty">选择一条语音</div>';await load()}
$('refresh').onclick=load;$('search').oninput=renderList;
document.querySelectorAll('#nav button').forEach(btn=>btn.onclick=()=>{document.querySelectorAll('#nav button').forEach(b=>b.classList.remove('active'));btn.classList.add('active');statusFilter=btn.dataset.status||'';load().catch(e=>setStatus(e.message))});
load().catch(e=>setStatus(e.message));
</script>
</body>
</html>
"""


def voice_inbox_html() -> str:
    return click_voice_workbench_html()


@router.get("/voice-inbox", response_class=HTMLResponse)
def voice_inbox_page(request: Request) -> HTMLResponse:
    require_mobile_access(request)
    return HTMLResponse(voice_inbox_html())


@router.get("/voice-records")
def get_voice_records(
    request: Request,
    status: Optional[str] = Query(default=None),
    source: Optional[str] = Query(default=None),
    include_deleted: bool = Query(default=False),
    limit: int = Query(default=100),
) -> dict[str, Any]:
    require_mobile_access(request)
    if status:
        safe_record_status(status)
    records = [compact_record(row) for row in list_voice_records(status=status, source=source, include_deleted=include_deleted, limit=limit)]
    return {"ok": True, "schema": VOICE_RECORD_SCHEMA, "records": records}


@router.post("/voice-records")
def post_voice_record(request: Request, payload: VoiceRecordCreate = Body(...)) -> dict[str, Any]:
    require_mobile_access(request, device_id=payload.device_id)
    audio_path, temp_id = write_uploaded_audio(payload)
    record = create_voice_record_from_audio(
        audio_path=audio_path,
        audio_hash=None,
        duration_ms=seconds_to_ms(payload.duration_seconds),
        mime_type=payload.mime_type,
        source=safe_source(payload.source),
        source_platform=payload.source_platform,
        device_id=payload.device_id,
        origin_ref=payload.origin_ref or temp_id,
        title=payload.title,
        metadata={**payload.metadata, "created_by": "voice_inbox_api"},
    )
    job = None
    if payload.auto_transcribe:
        record, job = enqueue_transcription(record, retry_failed=True)
    if payload.auto_understand and record.get("transcript"):
        record, job = enqueue_understanding(record, retry_failed=True)
    return {"ok": True, "schema": VOICE_RECORD_SCHEMA, "record": compact_record(record), "job": job}


@router.get("/voice-records/{voice_record_id}")
def get_voice_record(request: Request, voice_record_id: str) -> dict[str, Any]:
    require_mobile_access(request)
    record = get_voice_record_or_404(voice_record_id)
    return {
        "ok": True,
        "schema": VOICE_RECORD_SCHEMA,
        "record": compact_record(record),
        "actions": list_actions_for_record(voice_record_id),
        "action_receipts": list_action_receipts(voice_record_id),
        "links": list_voice_record_links(voice_record_id),
        "runs": list_runs_for_record(voice_record_id),
        "jobs": list_processing_jobs(voice_record_id),
        "transcript_versions": list_transcript_versions(voice_record_id),
        "context_snapshots": list_context_snapshots(voice_record_id),
        "discussions": list_discussions(voice_record_id),
    }


@router.patch("/voice-records/{voice_record_id}")
def patch_voice_record(request: Request, voice_record_id: str, payload: VoiceRecordPatch = Body(...)) -> dict[str, Any]:
    require_mobile_access(request)
    updates: list[str] = []
    params: list[Any] = []
    if payload.title is not None:
        updates.append("title = %s")
        params.append(clean_title(payload.title))
    if payload.summary is not None:
        updates.append("summary = %s")
        params.append(str(payload.summary).strip())
    if payload.intent_type is not None:
        updates.append("intent_type = %s")
        params.append(safe_intent_type(payload.intent_type))
    if payload.status is not None:
        updates.append("status = %s")
        params.append(safe_record_status(payload.status))
    if payload.project_hint is not None:
        updates.append("project_hint = %s")
        params.append(str(payload.project_hint).strip()[:160])
    if payload.priority is not None:
        updates.append("priority = %s")
        params.append(int(payload.priority))
    if not updates:
        return {"ok": True, "schema": VOICE_RECORD_SCHEMA, "record": compact_record(get_voice_record_or_404(voice_record_id))}
    params.append(voice_record_id)
    with db.connect() as conn:
        row = conn.execute(
            f"""
            UPDATE reader.voice_records
            SET {', '.join(updates)}, updated_at = now()
            WHERE id = %s AND deleted_at IS NULL
            RETURNING *
            """,
            tuple(params),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="voice record not found")
    return {"ok": True, "schema": VOICE_RECORD_SCHEMA, "record": compact_record(dict(row))}


@router.get("/voice-records/{voice_record_id}/audio")
def get_voice_record_audio(request: Request, voice_record_id: str) -> FileResponse:
    require_mobile_access(request)
    record = get_voice_record_or_404(voice_record_id)
    audio_path = Path(str(record.get("audio_uri") or "")).expanduser()
    if not audio_path.is_file():
        raise HTTPException(status_code=404, detail="audio file missing")
    return FileResponse(audio_path, media_type=media_type_for_path(audio_path), filename=audio_path.name)


@router.get("/voice-records/{voice_record_id}/actions")
def get_voice_record_actions(request: Request, voice_record_id: str) -> dict[str, Any]:
    require_mobile_access(request)
    get_voice_record_or_404(voice_record_id)
    return {"ok": True, "schema": VOICE_RECORD_SCHEMA, "actions": list_actions_for_record(voice_record_id)}


@router.get("/voice-records/{voice_record_id}/runs")
def get_voice_record_runs(request: Request, voice_record_id: str) -> dict[str, Any]:
    require_mobile_access(request)
    get_voice_record_or_404(voice_record_id)
    return {"ok": True, "schema": VOICE_RECORD_SCHEMA, "runs": list_runs_for_record(voice_record_id)}


@router.get("/voice-records/{voice_record_id}/jobs")
def get_voice_record_jobs(request: Request, voice_record_id: str) -> dict[str, Any]:
    require_mobile_access(request)
    get_voice_record_or_404(voice_record_id)
    return {"ok": True, "schema": VOICE_RECORD_SCHEMA, "jobs": list_processing_jobs(voice_record_id)}


@router.get("/voice-records/{voice_record_id}/transcript-versions")
def get_voice_record_transcript_versions(request: Request, voice_record_id: str) -> dict[str, Any]:
    require_mobile_access(request)
    get_voice_record_or_404(voice_record_id)
    return {"ok": True, "schema": VOICE_RECORD_SCHEMA, "versions": list_transcript_versions(voice_record_id)}


@router.post("/voice-records/{voice_record_id}/transcript-versions")
def post_voice_record_transcript_version(
    request: Request,
    voice_record_id: str,
    payload: VoiceTranscriptEdit = Body(...),
) -> dict[str, Any]:
    require_mobile_access(request)
    content = payload.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="transcript content is empty")
    with db.connect() as conn:
        record = voice_record_by_id(conn, voice_record_id)
        if not record or record.get("deleted_at") is not None:
            raise HTTPException(status_code=404, detail="voice record not found")
        parent_id = record.get("active_transcript_version_id")
        version = create_transcript_version(
            conn,
            voice_record_id=voice_record_id,
            version_type="user_edited",
            parent_version_id=str(parent_id) if parent_id else None,
            content=content,
            provider="click_voice_user",
            changes=[{"type": "user_edit", "confirmed": True}],
            metadata={"edited_in": "click_voice_workbench"},
        )
        next_status = record.get("status") if record.get("status") in {"archived", "settled"} else "understand_pending"
        updated = conn.execute(
            """
            UPDATE reader.voice_records
            SET transcript = %s, status = %s, failure_code = NULL,
                failure_message = NULL, updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (content, next_status, voice_record_id),
        ).fetchone()
    job = None
    if next_status == "understand_pending":
        updated_record, job = enqueue_understanding(dict(updated), retry_failed=True)
        updated = updated_record
    return {
        "ok": True,
        "schema": VOICE_RECORD_SCHEMA,
        "record": compact_record(dict(updated)),
        "version": version,
        "job": job,
    }


@router.post("/voice-records/{voice_record_id}/transcribe")
def post_voice_record_transcribe(request: Request, voice_record_id: str) -> dict[str, Any]:
    require_mobile_access(request)
    record = get_voice_record_or_404(voice_record_id)
    record, job = enqueue_transcription(record, retry_failed=True)
    return {"ok": True, "schema": VOICE_RECORD_SCHEMA, "record": compact_record(record), "job": job}


@router.post("/voice-records/{voice_record_id}/understand")
def post_voice_record_understand(request: Request, voice_record_id: str) -> dict[str, Any]:
    require_mobile_access(request)
    record = get_voice_record_or_404(voice_record_id)
    if str(record.get("status") or "") in {"transcribed", "cleanup_pending", "failed_cleanup"}:
        record, job = enqueue_cleanup(record, retry_failed=True)
    else:
        record, job = enqueue_understanding(record, retry_failed=True)
    return {"ok": True, "schema": VOICE_RECORD_SCHEMA, "record": compact_record(record), "job": job}


@router.get("/voice-records/{voice_record_id}/context-snapshots")
def get_voice_context_snapshots(request: Request, voice_record_id: str) -> dict[str, Any]:
    require_mobile_access(request)
    get_voice_record_or_404(voice_record_id)
    return {"ok": True, "schema": VOICE_RECORD_SCHEMA, "snapshots": list_context_snapshots(voice_record_id)}


@router.post("/voice-records/{voice_record_id}/context-snapshots")
def post_voice_context_snapshot(
    request: Request,
    voice_record_id: str,
    payload: VoiceContextBuild = Body(default_factory=VoiceContextBuild),
) -> dict[str, Any]:
    require_mobile_access(request)
    get_voice_record_or_404(voice_record_id)
    scope = safe_context_scope(payload.scope)
    selected_book_ids = list(dict.fromkeys(book_id.strip() for book_id in payload.selected_book_ids if book_id.strip()))
    input_hash = context_request_hash(
        voice_record_id,
        scope=scope,
        selected_book_ids=selected_book_ids,
    )
    job = enqueue_processing_job(
        voice_record_id,
        "build_context",
        payload={"scope": scope, "selected_book_ids": selected_book_ids},
        input_hash=input_hash,
        retry_failed=True,
    )
    wake_voice_processing_worker()
    return {"ok": True, "schema": VOICE_RECORD_SCHEMA, "job": job}


@router.get("/voice-records/{voice_record_id}/context-snapshots/{snapshot_id}")
def get_voice_context_snapshot(request: Request, voice_record_id: str, snapshot_id: str) -> dict[str, Any]:
    require_mobile_access(request)
    get_voice_record_or_404(voice_record_id)
    snapshot = get_context_snapshot(snapshot_id)
    if str(snapshot.get("voice_record_id")) != voice_record_id:
        raise HTTPException(status_code=404, detail="voice context snapshot not found")
    return {"ok": True, "schema": VOICE_RECORD_SCHEMA, "snapshot": snapshot}


@router.get("/voice-records/{voice_record_id}/context-snapshots/{snapshot_id}/citations/{evidence_id}")
def get_voice_context_citation(
    request: Request,
    voice_record_id: str,
    snapshot_id: str,
    evidence_id: str,
) -> dict[str, Any]:
    require_mobile_access(request)
    get_voice_record_or_404(voice_record_id)
    snapshot = get_context_snapshot(snapshot_id)
    if str(snapshot.get("voice_record_id")) != voice_record_id:
        raise HTTPException(status_code=404, detail="voice context snapshot not found")
    try:
        return resolve_snapshot_citation(snapshot_id, evidence_id)
    except CitationResolutionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/voice-records/{voice_record_id}/discussions")
def get_voice_discussions(request: Request, voice_record_id: str) -> dict[str, Any]:
    require_mobile_access(request)
    get_voice_record_or_404(voice_record_id)
    return {"ok": True, "schema": VOICE_RECORD_SCHEMA, "discussions": list_discussions(voice_record_id)}


@router.post("/voice-records/{voice_record_id}/discussions")
def post_voice_discussion(
    request: Request,
    voice_record_id: str,
    payload: VoiceDiscussionCreate = Body(default_factory=VoiceDiscussionCreate),
) -> dict[str, Any]:
    require_mobile_access(request)
    get_voice_record_or_404(voice_record_id)
    scope = safe_context_scope(payload.context_scope)
    selected_book_ids = list(dict.fromkeys(book_id.strip() for book_id in payload.selected_book_ids if book_id.strip()))
    if scope == "selected_books" and not selected_book_ids:
        raise HTTPException(status_code=422, detail="selected_books scope requires at least one book")
    discussion = create_discussion(
        voice_record_id=voice_record_id,
        title=payload.title,
        context_scope=scope,
        context_options={"selected_book_ids": selected_book_ids},
    )
    return {"ok": True, "schema": VOICE_RECORD_SCHEMA, "discussion": discussion, "messages": []}


@router.get("/voice-discussions/{discussion_id}")
def get_voice_discussion(request: Request, discussion_id: str) -> dict[str, Any]:
    require_mobile_access(request)
    discussion = get_discussion_or_404(discussion_id)
    snapshot = None
    if discussion.get("current_context_snapshot_id"):
        snapshot = get_context_snapshot(str(discussion["current_context_snapshot_id"]))
    return {
        "ok": True,
        "schema": VOICE_RECORD_SCHEMA,
        "discussion": discussion,
        "messages": list_discussion_messages(discussion_id),
        "context_snapshot": snapshot,
    }


@router.post("/voice-discussions/{discussion_id}/messages")
def post_voice_discussion_message(
    request: Request,
    discussion_id: str,
    payload: VoiceDiscussionMessageCreate = Body(...),
) -> dict[str, Any]:
    require_mobile_access(request)
    discussion = get_discussion_or_404(discussion_id)
    if discussion.get("status") != "active":
        raise HTTPException(status_code=409, detail="voice discussion is archived")
    content = payload.content.strip()
    client_id = payload.client_message_id.strip()
    user_message = discussion_message_by_client_id(discussion_id, client_id)
    if not user_message:
        user_message = append_discussion_message(
            discussion_id=discussion_id,
            role="user",
            content=content,
            status="succeeded",
            structured_content={"client_message_id": client_id},
            client_message_id=client_id,
        )
    elif str(user_message.get("content") or "") != content:
        raise HTTPException(status_code=409, detail="client_message_id was already used with different content")
    assistant_client_id = f"assistant:{client_id}"
    assistant_message = discussion_message_by_client_id(discussion_id, assistant_client_id)
    if not assistant_message:
        assistant_message = append_discussion_message(
            discussion_id=discussion_id,
            role="assistant",
            content="",
            status="pending",
            client_message_id=assistant_client_id,
        )
    elif assistant_message.get("status") == "failed":
        assistant_message = update_discussion_message(
            str(assistant_message["id"]),
            content="",
            status="pending",
            failure_reason=None,
        )

    context_options = discussion.get("context_options") if isinstance(discussion.get("context_options"), dict) else {}
    selected_book_ids = context_options.get("selected_book_ids") if isinstance(context_options.get("selected_book_ids"), list) else []
    scope = safe_context_scope(str(discussion.get("context_scope") or "recent_reading"))
    snapshot_id = discussion.get("current_context_snapshot_id")
    if not snapshot_id:
        snapshots = list_context_snapshots(str(discussion["voice_record_id"]))
        snapshot = next(
            (
                item
                for item in snapshots
                if context_snapshot_matches(
                    item,
                    scope=scope,
                    selected_book_ids=selected_book_ids,
                )
            ),
            None,
        )
        snapshot_id = (snapshot or {}).get("id")
    context_job = None
    if not snapshot_id:
        context_hash = context_request_hash(
            str(discussion["voice_record_id"]),
            scope=scope,
            selected_book_ids=selected_book_ids,
        )
        context_job = enqueue_processing_job(
            str(discussion["voice_record_id"]),
            "build_context",
            payload={"scope": scope, "selected_book_ids": selected_book_ids},
            input_hash=context_hash,
            priority=10,
            max_attempts=5,
            retry_failed=True,
        )
    discussion_input_hash = json_hash(
        {
            "schema": "click.voice.discussion.turn.v1",
            "discussion_id": discussion_id,
            "user_message_id": user_message["id"],
            "assistant_message_id": assistant_message["id"],
            "context_snapshot_id": snapshot_id,
            "content": content,
        }
    )
    job = enqueue_processing_job(
        str(discussion["voice_record_id"]),
        "prepare_discussion",
        payload={
            "discussion_id": discussion_id,
            "user_message_id": user_message["id"],
            "assistant_message_id": assistant_message["id"],
            "context_snapshot_id": snapshot_id,
        },
        input_hash=discussion_input_hash,
        priority=0,
        max_attempts=5,
        retry_failed=True,
    )
    wake_voice_processing_worker()
    return {
        "ok": True,
        "schema": VOICE_RECORD_SCHEMA,
        "discussion": discussion,
        "user_message": user_message,
        "assistant_message": assistant_message,
        "context_job": context_job,
        "job": job,
    }


@router.post("/voice-records/{voice_record_id}/retry")
def post_voice_record_retry(request: Request, voice_record_id: str) -> dict[str, Any]:
    require_mobile_access(request)
    record = get_voice_record_or_404(voice_record_id)
    status = str(record.get("status") or "")
    if status in {"failed_transcribe", "transcribe_pending", "saved_local"}:
        record, job = enqueue_transcription(record, retry_failed=True)
        return {"ok": True, "schema": VOICE_RECORD_SCHEMA, "record": compact_record(record), "job": job}
    elif status in {"failed_cleanup", "cleanup_pending", "transcribed"}:
        record, job = enqueue_cleanup(record, retry_failed=True)
        return {"ok": True, "schema": VOICE_RECORD_SCHEMA, "record": compact_record(record), "job": job}
    elif status in {"failed_understand", "understand_pending", "cleaned"}:
        record, job = enqueue_understanding(record, retry_failed=True)
        return {"ok": True, "schema": VOICE_RECORD_SCHEMA, "record": compact_record(record), "job": job}
    elif status == "failed_action":
        raise HTTPException(status_code=409, detail="retry the failed action directly")
    else:
        raise HTTPException(status_code=409, detail="voice record is not in a retriable state")
    return {"ok": not str(record.get("status") or "").startswith("failed"), "schema": VOICE_RECORD_SCHEMA, "record": compact_record(record)}


@router.post("/voice-records/{voice_record_id}/actions/{action_id}/apply")
def post_voice_action_apply(
    request: Request,
    voice_record_id: str,
    action_id: str,
    payload: VoiceActionApply = Body(default_factory=VoiceActionApply),
) -> dict[str, Any]:
    require_mobile_access(request)
    action, job = request_voice_action_apply(voice_record_id, action_id, confirmed=payload.confirmed)
    wake_voice_processing_worker()
    return {"ok": True, "schema": VOICE_RECORD_SCHEMA, "action": action, "job": job}


@router.post("/voice-records/{voice_record_id}/archive")
def post_voice_record_archive(request: Request, voice_record_id: str) -> dict[str, Any]:
    require_mobile_access(request)
    return {"ok": True, "schema": VOICE_RECORD_SCHEMA, "record": compact_record(archive_voice_record(voice_record_id))}


@router.delete("/voice-records/{voice_record_id}")
def delete_voice_record(request: Request, voice_record_id: str) -> dict[str, Any]:
    require_mobile_access(request)
    return {"ok": True, "schema": VOICE_RECORD_SCHEMA, "record": compact_record(soft_delete_voice_record(voice_record_id))}
