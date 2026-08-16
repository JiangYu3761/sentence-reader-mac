from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from pypdf import PdfReader


PDF_PREFLIGHT_SCHEMA = "click.pdf.preflight.v1"
PDF_LOCATOR_SCHEMA = "click.pdf.locator.v1"


def pdf_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _metadata_text(metadata: Any, key: str) -> str:
    if not metadata:
        return ""
    value = getattr(metadata, key, None)
    return str(value or "").strip()


def _page_box_size(page: Any) -> tuple[float, float]:
    box = page.cropbox if page.cropbox is not None else page.mediabox
    return max(0.0, float(box.width)), max(0.0, float(box.height))


def _image_count(page: Any) -> int:
    try:
        return len(page.images)
    except Exception:  # noqa: BLE001 - malformed image resources must not block import.
        return 0


def inspect_pdf(path: str | Path) -> dict[str, Any]:
    """Read a PDF without mutating it and return the Click P1 capability profile."""

    source = Path(path).expanduser().resolve()
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(source)
    if source.suffix.lower() != ".pdf":
        raise ValueError("source is not a PDF file")

    result: dict[str, Any] = {
        "schema": PDF_PREFLIGHT_SCHEMA,
        "source_file": str(source),
        "source_file_modified": False,
        "file_hash": f"sha256:{pdf_sha256(source)}",
        "byte_size": source.stat().st_size,
        "encrypted": False,
        "unlocked": True,
        "page_count": 0,
        "text_page_count": 0,
        "scanned_page_count": 0,
        "empty_page_count": 0,
        "document_profile": "unknown",
        "title": "",
        "author": "",
        "pages": [],
        "warnings": [],
        "capabilities": {
            "mac_pdfkit": True,
            "text_selection": False,
            "text_lookup": False,
            "text_to_speech": False,
            "area_annotation": False,
            "source_pdf_write_allowed": False,
        },
    }

    try:
        reader = PdfReader(str(source), strict=False)
    except Exception as exc:  # noqa: BLE001 - import needs a stable, serializable error.
        result["document_profile"] = "invalid"
        result["unlocked"] = False
        result["warnings"].append(f"pdf_open_failed:{exc.__class__.__name__}")
        return result

    result["encrypted"] = bool(reader.is_encrypted)
    if reader.is_encrypted:
        try:
            unlocked = bool(reader.decrypt(""))
        except Exception:  # noqa: BLE001 - encrypted PDFs may reject empty passwords in many ways.
            unlocked = False
        result["unlocked"] = unlocked
        if not unlocked:
            result["document_profile"] = "locked"
            result["warnings"].append("password_required")
            return result

    metadata = reader.metadata
    result["title"] = _metadata_text(metadata, "title")
    result["author"] = _metadata_text(metadata, "author")

    page_rows: list[dict[str, Any]] = []
    for page_index, page in enumerate(reader.pages):
        width, height = _page_box_size(page)
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001 - one damaged page must not block the book.
            text = ""
            result["warnings"].append(f"page_{page_index + 1}_text_extract_failed:{exc.__class__.__name__}")
        normalized_text = " ".join(text.split())
        image_count = _image_count(page)
        text_extractable = bool(normalized_text)
        probable_scanned = not text_extractable and image_count > 0
        if text_extractable:
            result["text_page_count"] += 1
        elif probable_scanned:
            result["scanned_page_count"] += 1
        else:
            result["empty_page_count"] += 1
        page_rows.append(
            {
                "page_index": page_index,
                "page_number": page_index + 1,
                "width_points": round(width, 3),
                "height_points": round(height, 3),
                "rotation": int(page.rotation or 0) % 360,
                "text_length": len(normalized_text),
                "text_extractable": text_extractable,
                "probable_scanned": probable_scanned,
                "image_count": image_count,
                "locator": {
                    "schema": PDF_LOCATOR_SCHEMA,
                    "page_index": page_index,
                    "page_number": page_index + 1,
                    "display_box": "cropBox",
                    "rotation": int(page.rotation or 0) % 360,
                },
            }
        )

    result["pages"] = page_rows
    result["page_count"] = len(page_rows)
    if not page_rows:
        result["document_profile"] = "empty"
    elif result["text_page_count"] == len(page_rows):
        result["document_profile"] = "text"
    elif result["scanned_page_count"] == len(page_rows):
        result["document_profile"] = "scanned"
    elif result["text_page_count"] or result["scanned_page_count"]:
        result["document_profile"] = "mixed"
    else:
        result["document_profile"] = "empty"

    has_text = result["text_page_count"] > 0
    result["capabilities"] = {
        "mac_pdfkit": True,
        "text_selection": has_text,
        "text_lookup": has_text,
        "text_to_speech": has_text,
        "area_annotation": result["page_count"] > 0,
        "source_pdf_write_allowed": False,
    }
    return result
