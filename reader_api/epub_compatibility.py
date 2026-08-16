from __future__ import annotations

import codecs
import hashlib
import html
import importlib.metadata
import json
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections import Counter
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional
from xml.etree import ElementTree as ET


REPORT_SCHEMA = "click.epub.compatibility_report.v1"
DISPLAY_VARIANT_SCHEMA = "click.reader.display_variant.v1"
READING_PROFILES = {"TEXT_REFLOW", "IMAGE_COMIC", "FIXED_LAYOUT", "MIXED", "UNKNOWN"}
SCRIPT_MODES = {"original", "simplified", "traditional"}
TEXT_MEDIA_TYPES = {"application/xhtml+xml", "text/html", "application/xml", "text/xml"}
FONT_EXTENSIONS = (".ttf", ".otf", ".woff", ".woff2", ".ttc")
SKIP_TEXT_TAGS = {"script", "style", "noscript", "svg"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def normalize_member(base: str, href: str) -> str:
    clean = str(href or "").split("#", 1)[0].replace("\\", "/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(base), clean)).lstrip("/")


def declared_encoding(raw: bytes) -> str:
    head = raw[:4096]
    ascii_head = head.decode("ascii", errors="ignore")
    patterns = (
        r"<\?xml[^>]+encoding\s*=\s*['\"]\s*([A-Za-z0-9._:-]+)",
        r"<meta[^>]+charset\s*=\s*['\"]?\s*([A-Za-z0-9._:-]+)",
        r"<meta[^>]+content\s*=\s*['\"][^'\"]*charset\s*=\s*([A-Za-z0-9._:-]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, ascii_head, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip().lower()
    return ""


def _normalized_encoding(value: str) -> str:
    aliases = {
        "utf8": "utf-8",
        "utf16": "utf-16",
        "gb2312": "gb18030",
        "gbk": "gb18030",
        "cp936": "gb18030",
    }
    return aliases.get(value.strip().lower(), value.strip().lower())


def _bom_encoding(raw: bytes) -> str:
    if raw.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if raw.startswith(codecs.BOM_UTF32_LE) or raw.startswith(codecs.BOM_UTF32_BE):
        return "utf-32"
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        return "utf-16"
    return ""


def decode_epub_bytes(raw: bytes) -> tuple[str, dict[str, Any]]:
    """Decode one EPUB text member without treating arbitrary bytes as BOM-less UTF-16."""

    declared = _normalized_encoding(declared_encoding(raw))
    bom = _bom_encoding(raw)
    candidates: list[tuple[str, str]] = []
    if bom:
        candidates.append((bom, "bom"))
    if declared and declared not in {item[0] for item in candidates}:
        candidates.append((declared, "declaration"))
    candidates.append(("utf-8", "strict_utf8"))
    candidates.append(("gb18030", "strict_gb18030"))

    errors: list[str] = []
    attempted: set[str] = set()
    for encoding, source in candidates:
        if encoding in attempted:
            continue
        attempted.add(encoding)
        if encoding.startswith("utf-16") and not (bom or declared.startswith("utf-16")):
            continue
        try:
            text = raw.decode(encoding, errors="strict")
            return text, {
                "encoding": encoding,
                "source": source,
                "declared_encoding": declared,
                "bom_encoding": bom,
                "confidence": 1.0,
                "degraded": False,
                "errors": errors,
            }
        except (LookupError, UnicodeDecodeError) as exc:
            errors.append(f"{encoding}:{exc.__class__.__name__}")

    try:
        from charset_normalizer import from_bytes

        match = from_bytes(raw).best()
        if match is not None:
            encoding = _normalized_encoding(str(match.encoding or ""))
            coherence = float(getattr(match, "percent_coherence", 0.0) or 0.0) / 100.0
            chaos = float(getattr(match, "percent_chaos", 100.0) or 100.0) / 100.0
            confidence = max(coherence, 1.0 - chaos)
            if encoding and confidence >= 0.65 and not (encoding.startswith("utf-16") and not (bom or declared)):
                return str(match), {
                    "encoding": encoding,
                    "source": "charset_normalizer",
                    "declared_encoding": declared,
                    "bom_encoding": bom,
                    "confidence": round(confidence, 4),
                    "degraded": False,
                    "errors": errors,
                }
    except Exception as exc:  # noqa: BLE001 - charset detection is a final optional fallback.
        errors.append(f"charset_normalizer:{exc.__class__.__name__}")

    return raw.decode("utf-8", errors="replace"), {
        "encoding": "utf-8",
        "source": "replacement_fallback",
        "declared_encoding": declared,
        "bom_encoding": bom,
        "confidence": 0.0,
        "degraded": True,
        "errors": errors,
    }


class VisibleTextCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.nodes: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag.lower() in SKIP_TEXT_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in SKIP_TEXT_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data and data.strip():
            self.nodes.append(data)


def visible_text_nodes(markup: str) -> list[str]:
    parser = VisibleTextCollector()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:
        pass
    return parser.nodes


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


class OpenCCConverters:
    def __init__(self) -> None:
        self.available = False
        self.version = "unavailable"
        self.error = ""
        self._simplified: Any = None
        self._traditional: Any = None
        try:
            from opencc import OpenCC

            # Use regional phrase dictionaries so the display mode follows the
            # reader's expected vocabulary (for example 軟體 <-> 软件), while
            # the source EPUB and source locators remain unchanged.
            self._simplified = OpenCC("tw2sp")
            self._traditional = OpenCC("s2twp")
            self.available = True
            self.version = _package_version("opencc-python-reimplemented")
        except Exception as exc:  # noqa: BLE001 - report degradation instead of blocking import.
            self.error = f"{exc.__class__.__name__}: {exc}"

    def convert_length_preserving(self, value: str, mode: str) -> tuple[str, str]:
        if mode == "original" or not value or not self.available:
            return value, "original"
        converter = self._simplified if mode == "simplified" else self._traditional
        converted = str(converter.convert(value))
        if _utf16_length(converted) == _utf16_length(value):
            return converted, "phrase"
        characters = [str(converter.convert(character)) for character in value]
        converted = "".join(item if _utf16_length(item) == _utf16_length(value[index]) else value[index] for index, item in enumerate(characters))
        if _utf16_length(converted) == _utf16_length(value):
            return converted, "character_fallback"
        return value, "source_fallback"


def build_display_variants(
    archive: zipfile.ZipFile,
    text_members: list[str],
    *,
    book_id: str,
    source_hash: str,
) -> dict[str, Any]:
    converters = OpenCCConverters()
    resources: dict[str, Any] = {}
    node_count = 0
    changed_count = 0
    fallback_count = 0
    for href in text_members:
        try:
            markup, decode = decode_epub_bytes(archive.read(href))
        except KeyError:
            continue
        nodes: list[dict[str, Any]] = []
        occurrences: Counter[str] = Counter()
        for index, source in enumerate(visible_text_nodes(markup)):
            simplified, simplified_strategy = converters.convert_length_preserving(source, "simplified")
            traditional, traditional_strategy = converters.convert_length_preserving(source, "traditional")
            fingerprint = hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]
            occurrence_index = occurrences[fingerprint]
            occurrences[fingerprint] += 1
            length = _utf16_length(source)
            nodes.append(
                {
                    "node_index": index,
                    "node_path": f"visible-text-node[{index}]",
                    "node_fingerprint": fingerprint,
                    "occurrence_index": occurrence_index,
                    "source_text": source,
                    "display_text": {
                        "simplified": simplified,
                        "traditional": traditional,
                    },
                    "strategies": {
                        "simplified": simplified_strategy,
                        "traditional": traditional_strategy,
                    },
                    "source_start": 0,
                    "source_end": length,
                    "source_to_display_runs": [[0, length, 0, length]],
                    "display_to_source_runs": [[0, length, 0, length]],
                    "length_preserving": True,
                }
            )
            node_count += 1
            if simplified != source or traditional != source:
                changed_count += 1
            if "fallback" in simplified_strategy or "fallback" in traditional_strategy:
                fallback_count += 1
        resources[href] = {
            "href": href,
            "decode": decode,
            "nodes": nodes,
        }
    return {
        "schema": DISPLAY_VARIANT_SCHEMA,
        "book_id": book_id,
        "source_hash": source_hash,
        "generated_at": now_iso(),
        "modes": sorted(SCRIPT_MODES),
        "opencc": {
            "available": converters.available,
            "binding": "opencc-python-reimplemented",
            "version": converters.version,
            "configs": {"simplified": "tw2sp", "traditional": "s2twp"},
            "error": converters.error,
            "online_service_used": False,
        },
        "offset_contract": "utf16_length_preserving_identity_runs",
        "node_count": node_count,
        "changed_node_count": changed_count,
        "fallback_node_count": fallback_count,
        "resources": resources,
    }


def _rootfile_path(archive: zipfile.ZipFile) -> str:
    raw = archive.read("META-INF/container.xml")
    text, _ = decode_epub_bytes(raw)
    root = ET.fromstring(text)
    for node in root.iter():
        if local_name(node.tag) == "rootfile" and node.attrib.get("full-path"):
            return node.attrib["full-path"].lstrip("/")
    raise ValueError("EPUB rootfile not found")


def _opf_metadata(archive: zipfile.ZipFile) -> dict[str, Any]:
    opf_path = _rootfile_path(archive)
    opf_text, decode = decode_epub_bytes(archive.read(opf_path))
    root = ET.fromstring(opf_text)
    manifest: dict[str, dict[str, str]] = {}
    spine: list[str] = []
    progression = "default"
    metadata_values: list[tuple[str, str, str]] = []
    for node in root.iter():
        name = local_name(node.tag)
        if name == "item" and node.attrib.get("id") and node.attrib.get("href"):
            manifest[node.attrib["id"]] = {
                "href": normalize_member(opf_path, node.attrib["href"]),
                "media_type": node.attrib.get("media-type", ""),
                "properties": node.attrib.get("properties", ""),
            }
        elif name == "itemref" and node.attrib.get("idref"):
            spine.append(node.attrib["idref"])
        elif name == "spine":
            progression = node.attrib.get("page-progression-direction", "default").lower()
        elif name == "meta":
            metadata_values.append(
                (
                    str(node.attrib.get("property") or node.attrib.get("name") or "").lower(),
                    str(node.attrib.get("content") or "").lower(),
                    "".join(node.itertext()).strip().lower(),
                )
            )
    fixed_layout = any(
        (key == "rendition:layout" and (text == "pre-paginated" or content == "pre-paginated"))
        or (key in {"fixed-layout", "rendition:layout"} and content in {"true", "yes", "pre-paginated"})
        for key, content, text in metadata_values
    )
    return {
        "opf_path": opf_path,
        "opf_text": opf_text,
        "opf_decode": decode,
        "manifest": manifest,
        "spine": spine,
        "page_progression": progression if progression in {"ltr", "rtl"} else "ltr",
        "fixed_layout": fixed_layout,
    }


def _toc_depth_counts(archive: zipfile.ZipFile, metadata: dict[str, Any]) -> dict[str, int]:
    counts: Counter[int] = Counter()
    manifest = metadata["manifest"]
    nav_items = [item for item in manifest.values() if "nav" in item.get("properties", "").split()]
    ncx_items = [
        item
        for item in manifest.values()
        if item.get("media_type") == "application/x-dtbncx+xml" or item.get("href", "").lower().endswith(".ncx")
    ]

    def walk_html_list(node: ET.Element, depth: int) -> None:
        for child in list(node):
            if local_name(child.tag) != "li":
                continue
            counts[depth] += 1
            for nested in list(child):
                if local_name(nested.tag) in {"ol", "ul"}:
                    walk_html_list(nested, depth + 1)

    for item in nav_items:
        try:
            text, _ = decode_epub_bytes(archive.read(item["href"]))
            root = ET.fromstring(text)
            nav = next((node for node in root.iter() if local_name(node.tag) == "nav"), None)
            top = next((node for node in list(nav or []) if local_name(node.tag) in {"ol", "ul"}), None)
            if top is not None:
                walk_html_list(top, 0)
                if counts:
                    return {str(key): value for key, value in sorted(counts.items())}
        except Exception:
            continue

    def walk_ncx(node: ET.Element, depth: int) -> None:
        counts[depth] += 1
        for child in list(node):
            if local_name(child.tag) == "navPoint":
                walk_ncx(child, depth + 1)

    for item in ncx_items:
        try:
            text, _ = decode_epub_bytes(archive.read(item["href"]))
            root = ET.fromstring(text)
            nav_map = next((node for node in root.iter() if local_name(node.tag) == "navMap"), None)
            for child in list(nav_map or []):
                if local_name(child.tag) == "navPoint":
                    walk_ncx(child, 0)
            if counts:
                return {str(key): value for key, value in sorted(counts.items())}
        except Exception:
            continue
    return {}


def _character_issues(value: str) -> dict[str, int]:
    return {
        "private_use": sum(1 for char in value if "\ue000" <= char <= "\uf8ff"),
        "replacement": value.count("\ufffd"),
        "placeholder_square": value.count("□"),
    }


def _epubcheck_jar() -> Optional[Path]:
    configured = os.getenv("CLICK_EPUBCHECK_JAR", "").strip()
    if configured:
        path = Path(configured).expanduser()
        return path if path.exists() else None
    root = Path.home() / "Library" / "Application Support" / "Click" / "Tools" / "epubcheck"
    candidates = sorted(root.glob("**/epubcheck.jar"), reverse=True) if root.exists() else []
    return candidates[0] if candidates else None


def run_epubcheck(epub_path: Path) -> dict[str, Any]:
    jar = _epubcheck_jar()
    if jar is None:
        return {"status": "unavailable", "messages": [], "jar": "", "version": ""}
    java_candidates = [
        Path(os.environ.get("JAVA_HOME", "")) / "bin" / "java" if os.environ.get("JAVA_HOME") else None,
        Path.home() / "Library" / "Application Support" / "ClickAndroidToolchain" / "jdk" / "jdk-17" / "Contents" / "Home" / "bin" / "java",
        Path(shutil.which("java") or ""),
    ]
    java_path = next((candidate for candidate in java_candidates if candidate and candidate.is_file()), None)
    if java_path is None:
        return {"status": "unavailable", "messages": [], "jar": str(jar), "version": "", "error": "java_not_found"}
    with tempfile.TemporaryDirectory(prefix="click-epubcheck-") as temp:
        output = Path(temp) / "report.json"
        try:
            completed = subprocess.run(
                [str(java_path), "-jar", str(jar), str(epub_path), "--json", str(output)],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            payload = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
            messages = payload.get("messages") if isinstance(payload, dict) else []
            return {
                "status": "pass" if completed.returncode == 0 else "issues",
                "messages": messages if isinstance(messages, list) else [],
                "jar": str(jar),
                "jar_sha256": file_sha256(jar),
                "version": jar.parent.name,
                "returncode": completed.returncode,
                "stderr": completed.stderr[-2000:],
            }
        except Exception as exc:  # noqa: BLE001 - conformance checking must not destroy imports.
            return {
                "status": "error",
                "messages": [],
                "jar": str(jar),
                "error": f"{exc.__class__.__name__}: {exc}",
            }


def _inject_fixed_layout_opf(opf_text: str) -> str:
    additions = (
        '<meta property="rendition:layout">pre-paginated</meta>'
        '<meta property="rendition:spread">auto</meta>'
        '<meta name="fixed-layout" content="true"/>'
    )
    if re.search(r"rendition:layout[^>]*(?:pre-paginated|fixed)", opf_text, flags=re.IGNORECASE):
        return opf_text
    return re.sub(r"</metadata\s*>", additions + "</metadata>", opf_text, count=1, flags=re.IGNORECASE)


def _inject_comic_css(markup: str) -> str:
    style = (
        "<style id=\"click-image-comic-runtime\">"
        "html,body{margin:0!important;padding:0!important;width:100%!important;height:100%!important;overflow:hidden!important;}"
        "img,svg{display:block!important;margin:auto!important;width:auto!important;height:auto!important;"
        "max-width:100vw!important;max-height:100vh!important;object-fit:contain!important;}"
        "</style>"
    )
    if "click-image-comic-runtime" in markup:
        return markup
    if re.search(r"</head\s*>", markup, flags=re.IGNORECASE):
        return re.sub(r"</head\s*>", style + "</head>", markup, count=1, flags=re.IGNORECASE)
    return style + markup


def create_comic_runtime_epub(source: Path, target: Path, metadata: dict[str, Any], text_members: set[str]) -> Path:
    source_hash = file_sha256(source)
    marker = target.with_suffix(target.suffix + ".source-sha256")
    if target.exists() and marker.exists() and marker.read_text(encoding="utf-8").strip() == source_hash:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with zipfile.ZipFile(source) as source_zip, zipfile.ZipFile(temporary, "w") as output_zip:
        for info in source_zip.infolist():
            raw = source_zip.read(info.filename)
            if info.filename == metadata["opf_path"]:
                text, _ = decode_epub_bytes(raw)
                raw = re.sub(r"encoding=['\"][^'\"]+['\"]", 'encoding="utf-8"', _inject_fixed_layout_opf(text), count=1).encode("utf-8")
            elif info.filename in text_members:
                text, _ = decode_epub_bytes(raw)
                raw = re.sub(r"encoding=['\"][^'\"]+['\"]", 'encoding="utf-8"', _inject_comic_css(text), count=1).encode("utf-8")
            clone = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            clone.comment = info.comment
            clone.extra = info.extra
            clone.internal_attr = info.internal_attr
            clone.external_attr = info.external_attr
            clone.create_system = info.create_system
            clone.compress_type = zipfile.ZIP_STORED if info.filename == "mimetype" else info.compress_type
            output_zip.writestr(clone, raw)
    temporary.replace(target)
    marker.write_text(source_hash + "\n", encoding="utf-8")
    return target


def audit_epub(
    epub_path: Path,
    *,
    book_id: str,
    output_dir: Path,
    generate_runtime: bool = True,
    generate_variants: bool = True,
) -> dict[str, Any]:
    epub_path = epub_path.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    source_hash_before = file_sha256(epub_path)
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "book_id": book_id,
        "source_file": str(epub_path),
        "source_hash": source_hash_before,
        "checked_at": now_iso(),
        "original_epub_modified": False,
    }
    with zipfile.ZipFile(epub_path) as archive:
        metadata = _opf_metadata(archive)
        manifest = metadata["manifest"]
        spine_items = [manifest[item_id] for item_id in metadata["spine"] if item_id in manifest]
        text_members = [
            item["href"]
            for item in manifest.values()
            if item.get("media_type") in TEXT_MEDIA_TYPES or item.get("href", "").lower().endswith((".xhtml", ".html", ".htm", ".ncx"))
        ]
        issues: list[dict[str, Any]] = []
        encoding_counts: Counter[str] = Counter()
        declared_counts: Counter[str] = Counter()
        total_private_use = 0
        total_replacement = 0
        total_squares = 0
        image_only = 0
        readable_spine = 0
        for item in spine_items:
            href = item.get("href", "")
            if href not in archive.namelist() or href not in text_members:
                continue
            text, decode = decode_epub_bytes(archive.read(href))
            encoding_counts[str(decode["encoding"])] += 1
            if decode.get("declared_encoding"):
                declared_counts[str(decode["declared_encoding"])] += 1
            chars = _character_issues(text)
            total_private_use += chars["private_use"]
            total_replacement += chars["replacement"]
            total_squares += chars["placeholder_square"]
            if decode.get("degraded") or any(chars.values()):
                issues.append({"href": href, "decode": decode, "characters": chars})
            visible = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"<(script|style)\b.*?</\1>", "", text, flags=re.I | re.S)))
            visible = re.sub(r"\s+", "", visible)
            images = len(re.findall(r"<(?:img|image)\b", text, flags=re.IGNORECASE))
            readable_spine += 1
            if images and len(visible) <= 80:
                image_only += 1
        image_only_ratio = image_only / readable_spine if readable_spine else 0.0
        if metadata["fixed_layout"]:
            profile = "FIXED_LAYOUT"
        elif readable_spine and image_only_ratio >= 0.8:
            profile = "IMAGE_COMIC"
        elif readable_spine and image_only_ratio >= 0.25:
            profile = "MIXED"
        elif readable_spine:
            profile = "TEXT_REFLOW"
        else:
            profile = "UNKNOWN"
        has_fonts = any(item.get("href", "").lower().endswith(FONT_EXTENSIONS) for item in manifest.values())
        runtime_path = ""
        if generate_runtime and profile == "IMAGE_COMIC" and not metadata["fixed_layout"]:
            runtime_path = str(
                create_comic_runtime_epub(
                    epub_path,
                    output_dir / "runtime.epub",
                    metadata,
                    set(text_members),
                )
            )
        variants_path = ""
        variants_summary: dict[str, Any] = {"available": False}
        if generate_variants:
            variants = build_display_variants(archive, text_members, book_id=book_id, source_hash=source_hash_before)
            variants_file = output_dir / "display_variants.json"
            variants_file.write_text(json.dumps(variants, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
            variants_path = str(variants_file)
            variants_summary = {
                "available": bool(variants.get("opencc", {}).get("available")),
                "path": variants_path,
                "node_count": variants.get("node_count", 0),
                "changed_node_count": variants.get("changed_node_count", 0),
                "fallback_node_count": variants.get("fallback_node_count", 0),
                "opencc": variants.get("opencc"),
                "offset_contract": variants.get("offset_contract"),
            }
        report.update(
            {
                "opf_path": metadata["opf_path"],
                "opf_decode": metadata["opf_decode"],
                "spine_count": len(spine_items),
                "text_spine_count": readable_spine,
                "declared_encodings": dict(declared_counts),
                "resolved_encodings": dict(encoding_counts),
                "decode_warnings": issues[:200],
                "private_use_characters": total_private_use,
                "replacement_characters": total_replacement,
                "placeholder_squares": total_squares,
                "missing_font_mapping_suspected": bool(total_private_use and not has_fonts),
                "toc_depth_counts": _toc_depth_counts(archive, metadata),
                "reading_profile": profile,
                "image_only_ratio": round(image_only_ratio, 6),
                "page_progression": metadata["page_progression"],
                "source_fixed_layout": metadata["fixed_layout"],
                "recommended_spread": "auto" if profile in {"IMAGE_COMIC", "FIXED_LAYOUT"} else "never",
                "normalization_status": "runtime_derivative" if runtime_path else "source_unchanged",
                "runtime_epub_path": runtime_path,
                "display_variants_path": variants_path,
                "display_variants": variants_summary,
                "correction_queue_required": bool(total_private_use or total_replacement or total_squares or any(item["decode"].get("degraded") for item in issues)),
            }
        )
    report["epubcheck"] = run_epubcheck(epub_path)
    source_hash_after = file_sha256(epub_path)
    report["source_hash_after"] = source_hash_after
    report["original_epub_modified"] = source_hash_before != source_hash_after
    if report["original_epub_modified"]:
        raise RuntimeError("EPUB compatibility audit modified the original EPUB")
    (output_dir / "compatibility_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def cached_report(output_dir: Path, source_hash: str) -> Optional[dict[str, Any]]:
    path = output_dir / "compatibility_report.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("schema") != REPORT_SCHEMA or payload.get("source_hash") != source_hash:
        return None
    variants = str(payload.get("display_variants_path") or "")
    if variants and not Path(variants).exists():
        return None
    runtime = str(payload.get("runtime_epub_path") or "")
    if runtime and not Path(runtime).exists():
        return None
    if (payload.get("epubcheck") or {}).get("status") == "unavailable" and _epubcheck_jar() is not None:
        return None
    return payload


def ensure_epub_assets(epub_path: Path, *, book_id: str, output_dir: Path, force: bool = False) -> dict[str, Any]:
    source_hash = file_sha256(epub_path)
    if not force:
        existing = cached_report(output_dir, source_hash)
        if existing is not None:
            return existing
    return audit_epub(epub_path, book_id=book_id, output_dir=output_dir)
