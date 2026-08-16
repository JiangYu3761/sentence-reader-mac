from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from reader_api.mobile_workspace import (
    html_response_with_access_cookies,
    mobile_response_payload,
    require_mobile_access,
)
from reader_api.tingle import (
    TINGLE_SCHEMA,
    archive_tingle_inspiration,
    create_hermes_inspiration,
    get_tingle_inspiration,
    get_tingle_inspiration_for_voice_record,
    list_tingle_inspirations,
    permanent_delete_tingle_inspiration,
    restore_tingle_inspiration,
    update_tingle_inspiration,
)
from reader_api.voice_inbox import enqueue_cleanup, enqueue_transcription, enqueue_understanding
from reader_api.voice_inbox_service import get_voice_record_or_404
from reader_api.voice_processing import wake_voice_processing_worker
from reader_api.voice_workbench import click_voice_workbench_html


router = APIRouter()


class TingleInspirationCreate(BaseModel):
    title: str = Field(default="", max_length=80)
    content: str = Field(min_length=1, max_length=200_000)
    external_ref: Optional[str] = Field(default=None, max_length=500)
    source_created_at: Optional[datetime] = None


class TingleInspirationPatch(BaseModel):
    title: Optional[str] = Field(default=None, max_length=80)
    content: Optional[str] = Field(default=None, max_length=200_000)


class TinglePermanentDeleteRequest(BaseModel):
    confirmation_intent: Literal["delete_permanently"]
    expected_content_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")
    acknowledge_linked_external_targets: bool = False


@router.get("/tingle", response_class=HTMLResponse)
def tingle_page(request: Request) -> HTMLResponse:
    require_mobile_access(request)
    return html_response_with_access_cookies(click_voice_workbench_html(), request)


@router.get("/tingle/inspirations")
def get_tingle_inspirations(
    request: Request,
    archived: bool = Query(default=False),
    query: str = Query(default="", max_length=200),
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    require_mobile_access(request)
    return mobile_response_payload(request, {
        "ok": True,
        "schema": TINGLE_SCHEMA,
        "inspirations": list_tingle_inspirations(archived=archived, query=query, limit=limit),
    })


@router.post("/tingle/inspirations")
def post_tingle_inspiration(
    request: Request,
    payload: TingleInspirationCreate = Body(...),
) -> dict[str, Any]:
    require_mobile_access(request)
    item = create_hermes_inspiration(
        title=payload.title,
        content=payload.content,
        external_ref=payload.external_ref,
        source_created_at=payload.source_created_at,
    )
    return mobile_response_payload(
        request,
        {"ok": True, "schema": TINGLE_SCHEMA, "inspiration": item},
    )


@router.get("/tingle/inspirations/by-voice-record/{voice_record_id}")
def get_tingle_inspiration_by_voice_record(request: Request, voice_record_id: str) -> dict[str, Any]:
    require_mobile_access(request)
    return mobile_response_payload(request, {
        "ok": True,
        "schema": TINGLE_SCHEMA,
        "inspiration": get_tingle_inspiration_for_voice_record(voice_record_id),
    })


@router.get("/tingle/inspirations/{inspiration_id}")
def get_tingle_inspiration_detail(request: Request, inspiration_id: str) -> dict[str, Any]:
    require_mobile_access(request)
    return mobile_response_payload(request, {
        "ok": True,
        "schema": TINGLE_SCHEMA,
        "inspiration": get_tingle_inspiration(inspiration_id),
    })


@router.patch("/tingle/inspirations/{inspiration_id}")
def patch_tingle_inspiration(
    request: Request,
    inspiration_id: str,
    payload: TingleInspirationPatch = Body(...),
) -> dict[str, Any]:
    require_mobile_access(request)
    return mobile_response_payload(request, {
        "ok": True,
        "schema": TINGLE_SCHEMA,
        "inspiration": update_tingle_inspiration(
            inspiration_id,
            title=payload.title,
            content=payload.content,
        ),
    })


@router.post("/tingle/inspirations/{inspiration_id}/archive")
def post_tingle_archive(request: Request, inspiration_id: str) -> dict[str, Any]:
    require_mobile_access(request)
    return mobile_response_payload(request, {
        "ok": True,
        "schema": TINGLE_SCHEMA,
        "inspiration": archive_tingle_inspiration(inspiration_id),
    })


@router.post("/tingle/inspirations/{inspiration_id}/restore")
def post_tingle_restore(request: Request, inspiration_id: str) -> dict[str, Any]:
    require_mobile_access(request)
    return mobile_response_payload(request, {
        "ok": True,
        "schema": TINGLE_SCHEMA,
        "inspiration": restore_tingle_inspiration(inspiration_id),
    })


@router.post("/tingle/inspirations/{inspiration_id}/delete-permanently")
def post_tingle_delete_permanently(
    request: Request,
    inspiration_id: str,
    payload: TinglePermanentDeleteRequest = Body(...),
) -> dict[str, Any]:
    require_mobile_access(request)
    receipt = permanent_delete_tingle_inspiration(
        inspiration_id,
        confirmation_intent=payload.confirmation_intent,
        expected_content_hash=payload.expected_content_hash,
        acknowledge_linked_external_targets=payload.acknowledge_linked_external_targets,
    )
    return mobile_response_payload(request, {
        "ok": True,
        "schema": "tingle.deletion_receipt.v1",
        "receipt": receipt,
    })


@router.post("/tingle/inspirations/{inspiration_id}/retry")
def post_tingle_retry(request: Request, inspiration_id: str) -> dict[str, Any]:
    require_mobile_access(request)
    item = get_tingle_inspiration(inspiration_id)
    voice_record_id = item.get("voice_record_id")
    if not voice_record_id:
        raise HTTPException(status_code=409, detail="text inspiration has no processing step to retry")
    record = get_voice_record_or_404(str(voice_record_id))
    status = str(record.get("status") or "")
    if status in {"failed_transcribe", "transcribe_pending", "saved_local"}:
        _, job = enqueue_transcription(record, retry_failed=True)
    elif status in {"failed_cleanup", "cleanup_pending", "transcribed"}:
        _, job = enqueue_cleanup(record, retry_failed=True)
    elif status in {"failed_understand", "understand_pending", "cleaned"}:
        _, job = enqueue_understanding(record, retry_failed=True)
    else:
        raise HTTPException(status_code=409, detail="this inspiration is not in a retriable state")
    wake_voice_processing_worker()
    return mobile_response_payload(request, {
        "ok": True,
        "schema": TINGLE_SCHEMA,
        "inspiration": get_tingle_inspiration(inspiration_id),
        "job": job,
    })
