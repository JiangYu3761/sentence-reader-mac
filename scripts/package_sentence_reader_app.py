#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VISUAL_PROBE = ROOT / "Probe" / "ReadiumVisualReaderProbe"
NATIVE_SOURCE = ROOT / "Probe" / "NativeSentenceReader" / "SentenceReaderNative.swift"
APP = ROOT / "build" / "Click.app"
LEGACY_APP = ROOT / "build" / "Sentence Reader.app"
EXECUTABLE = APP / "Contents" / "MacOS" / "SentenceReader"
INFO_PLIST = APP / "Contents" / "Info.plist"
RESOURCES = APP / "Contents" / "Resources"
RUNTIME = RESOURCES / "ReaderRuntime"
DEFAULT_EPUB = ROOT / "fixtures" / "sentence-reader-smoke.epub"
NATIVE_BINARY = ROOT / "build" / "SentenceReaderNative"
PACKAGE_CACHE = Path("/tmp/sentence-reader-readium-xcode-packages")
DERIVED_DATA = Path("/tmp/sentence-reader-readium-visual-derived")
PRODUCTS = DERIVED_DATA / "Build" / "Products" / "Debug-maccatalyst"
PRODUCT_BINARY = PRODUCTS / "ReadiumVisualReaderProbe"
READER_VENV = ROOT / ".venv-reader-api"
PYTHON_FRAMEWORK_SOURCE = Path(
    "/Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework"
)
PYTHON_FRAMEWORK = RUNTIME / "Python3.framework"
BUNDLED_RUNTIME_PYTHON = PYTHON_FRAMEWORK / "Versions" / "3.9" / "bin" / "python3.9"
APP_ICON = ROOT / "assets" / "SentenceReader.icns"
APP_ICON_NAME = "SentenceReader"
TTS_RUNTIME = RESOURCES / "ClickTTSRuntime"
TTS_RUNTIME_MANIFEST = TTS_RUNTIME / "runtime-manifest.json"
TTS_HELPER = TTS_RUNTIME / "helper" / "click_tts_helper.py"
EXPECTED_TTS_RUNTIME_MANIFEST_SHA256 = (
    "e4c558c222f759a0730f398b2afe91bc7381e90f75a81443d502b6828ac7eb9b"
)
EXPECTED_TTS_HELPER_SHA256 = (
    "a3de7a2adf43f6f6dc707dc8dccf7d4484f3f32fb74021cdfbdd3c31c21fb1a9"
)
FROZEN_TTS_APP_CANDIDATES = (
    ROOT / "build" / "Click-Gate2C.app",
    ROOT / "build" / "Click-StageB.app",
    ROOT / "build" / "Click-Gate2B.app",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def runtime_file_hashes(root: Path) -> dict[str, str]:
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError(f"Click Microsoft TTS runtime is missing or unsafe: {root}")
    hashes: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"Click Microsoft TTS runtime contains a symlink: {path}")
        if path.is_file():
            hashes[path.relative_to(root).as_posix()] = sha256_file(path)
    if not hashes:
        raise RuntimeError(f"Click Microsoft TTS runtime is empty: {root}")
    return hashes


def verify_frozen_tts_runtime(root: Path) -> dict[str, str]:
    hashes = runtime_file_hashes(root)
    manifest = root / "runtime-manifest.json"
    helper = root / "helper" / "click_tts_helper.py"
    if (
        not manifest.is_file()
        or sha256_file(manifest) != EXPECTED_TTS_RUNTIME_MANIFEST_SHA256
        or not helper.is_file()
        or sha256_file(helper) != EXPECTED_TTS_HELPER_SHA256
    ):
        raise RuntimeError(
            "Click Microsoft TTS runtime does not match the identity compiled "
            "into the native reader"
        )
    return hashes


def copy_frozen_tts_runtime() -> dict[str, str]:
    source: Path | None = None
    source_hashes: dict[str, str] | None = None
    failures: list[str] = []
    for app in FROZEN_TTS_APP_CANDIDATES:
        candidate = app / "Contents" / "Resources" / "ClickTTSRuntime"
        try:
            hashes = verify_frozen_tts_runtime(candidate)
        except RuntimeError as error:
            failures.append(f"{candidate}: {error}")
            continue
        source = candidate
        source_hashes = hashes
        break
    if source is None or source_hashes is None:
        detail = "\n".join(failures) or "no frozen candidates were found"
        raise RuntimeError(
            "No verified frozen Microsoft TTS runtime is available; refusing "
            "to package a Click build without playback.\n" + detail
        )
    if TTS_RUNTIME.exists():
        shutil.rmtree(TTS_RUNTIME)
    shutil.copytree(source, TTS_RUNTIME)
    if verify_frozen_tts_runtime(TTS_RUNTIME) != source_hashes:
        raise RuntimeError("Copied Microsoft TTS runtime differs from its frozen source")
    return source_hashes


def resolve_codesign_identity() -> str:
    configured = os.getenv("CLICK_CODESIGN_IDENTITY", "").strip()
    if configured:
        return configured
    result = subprocess.run(
        ["/usr/bin/security", "find-identity", "-v", "-p", "codesigning"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    identities = (
        re.findall(r'\d+\)\s+([0-9A-F]{40})\s+"([^"]+)"', result.stdout)
        if result.returncode == 0
        else []
    )
    for prefix in ("Developer ID Application:", "Apple Development:", "Apple Distribution:"):
        for fingerprint, name in identities:
            if name.startswith(prefix):
                return fingerprint
    if identities:
        available = ", ".join(name for _, name in identities)
        raise RuntimeError(
            "Click requires an Apple signing identity with a TeamIdentifier; "
            f"available identities are: {available}"
        )
    raise RuntimeError("No valid code signing identity is available")


def signed_team_identifier(target: Path) -> str:
    result = subprocess.run(
        ["/usr/bin/codesign", "-dv", "--verbose=4", str(target)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    match = re.search(r"^TeamIdentifier=(.+)$", result.stdout, flags=re.MULTILINE)
    team_identifier = match.group(1).strip() if match else ""
    if result.returncode != 0 or not team_identifier or team_identifier == "not set":
        raise RuntimeError(f"Click was signed without a TeamIdentifier:\n{result.stdout}")
    return team_identifier


def codesign_app_bundle() -> tuple[str, str]:
    identity = resolve_codesign_identity()
    nested_code = sorted(
        (
            path
            for path in RUNTIME.rglob("*")
            if path.is_file() and path.suffix.lower() in {".so", ".dylib"}
        ),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    targets = [*nested_code, BUNDLED_RUNTIME_PYTHON, PYTHON_FRAMEWORK, EXECUTABLE, APP]
    seen: set[Path] = set()
    for target in targets:
        if target in seen or not target.exists():
            continue
        seen.add(target)
        result = subprocess.run(
            [
                "/usr/bin/codesign",
                "--force",
                "--sign",
                identity,
                "--timestamp=none",
                str(target),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if result.returncode != 0:
            raise RuntimeError(f"codesign failed for {target}:\n{result.stdout}")
    verify = subprocess.run(
        ["/usr/bin/codesign", "--verify", "--strict", "--verbose=2", str(APP)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if verify.returncode != 0:
        raise RuntimeError(f"codesign verification failed:\n{verify.stdout}")
    return identity, signed_team_identifier(APP)


def ensure_default_epub() -> None:
    if DEFAULT_EPUB.exists():
        return
    DEFAULT_EPUB.parent.mkdir(parents=True, exist_ok=True)
    files = {
        "META-INF/container.xml": """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
""",
        "EPUB/package.opf": """<?xml version="1.0" encoding="UTF-8"?>
<package version="3.0" unique-identifier="bookid" xmlns="http://www.idpf.org/2007/opf">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">sentence-reader-smoke</dc:identifier>
    <dc:title>Sentence Reader Smoke Book</dc:title>
    <dc:language>en</dc:language>
    <dc:creator>Sentence Reader</dc:creator>
    <meta property="dcterms:modified">2026-06-29T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="chapter"/>
  </spine>
</package>
""",
        "EPUB/nav.xhtml": """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
  <head><title>Contents</title></head>
  <body>
    <nav epub:type="toc">
      <ol><li><a href="chapter.xhtml">Smoke Chapter</a></li></ol>
    </nav>
  </body>
</html>
""",
        "EPUB/chapter.xhtml": """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
  <head><title>Smoke Chapter</title></head>
  <body>
    <h1>Smoke Chapter</h1>
    <p>Strategy is a coherent response to a real challenge.</p>
    <p>Good reading software should preserve notes, highlights, and position.</p>
  </body>
</html>
""",
    }
    with zipfile.ZipFile(DEFAULT_EPUB, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for name, content in files.items():
            archive.writestr(name, content, compress_type=zipfile.ZIP_DEFLATED)


def copy_bundled_python_runtime() -> bool:
    source_site_packages = READER_VENV / "lib" / "python3.9" / "site-packages"
    if not PYTHON_FRAMEWORK_SOURCE.exists() or not source_site_packages.exists():
        return False
    shutil.copytree(PYTHON_FRAMEWORK_SOURCE, PYTHON_FRAMEWORK, symlinks=True)
    target_site_packages = PYTHON_FRAMEWORK / "Versions" / "3.9" / "lib" / "python3.9" / "site-packages"
    shutil.copytree(
        source_site_packages,
        target_site_packages,
        dirs_exist_ok=True,
        symlinks=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
    )
    return BUNDLED_RUNTIME_PYTHON.exists()


def write_runtime_manifest() -> None:
    runtime_python = BUNDLED_RUNTIME_PYTHON
    manifest = {
        "schema": "click.reader_runtime.manifest.v2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "contract": "click.reader_runtime.v1",
        "api_revision": 3,
        "capabilities": [
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
        ],
        "owner_apps": ["Click", "Tingle"],
        "runtime": str(RUNTIME),
        "reader_api": str(RUNTIME / "reader_api"),
        "requirements": str(RUNTIME / "requirements-reader-api.txt"),
        "bootstrap": {
            "script": str(RUNTIME / "scripts" / "sentence_reader_runtime_bootstrap.py"),
            "first_run_preflight": str(RUNTIME / "scripts" / "sentence_reader_first_run_preflight.py"),
            "runtime_config": str(RUNTIME / "scripts" / "sentence_reader_runtime_config.py"),
            "user_venv": "~/Library/Application Support/SentenceReader/Runtime/.venv-reader-api",
            "auto_repair_requires_env": "SENTENCE_READER_BOOTSTRAP_REPAIR=1",
            "dependency_install_requires_env": "SENTENCE_READER_BOOTSTRAP_INSTALL_DEPS=1",
        },
        "python": {
            "strategy": "bundled_signed_python_framework",
            "path": "ReaderRuntime/Python3.framework/Versions/3.9/bin/python3.9",
            "exists": runtime_python.exists(),
            "realpath": str(runtime_python.resolve(strict=False)),
            "pyvenv_cfg": "",
            "portable_clean_mac_ready": False,
        },
        "postgres": {
            "strategy": "external_postgres_app_or_POSTGRES_APP_BIN",
            "bundled": False,
            "default_bin": "/Applications/Postgres.app/Contents/Versions/latest/bin",
        },
        "portability": {
            "current_machine_supported": True,
            "clean_mac_supported": False,
            "bootstrap_preflight_supported": True,
            "known_blockers": [
                "postgres_not_bundled",
            ],
        },
    }
    (RUNTIME / "runtime_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def copy_reader_runtime() -> None:
    if RUNTIME.exists():
        shutil.rmtree(RUNTIME)
    RUNTIME.mkdir(parents=True, exist_ok=True)

    shutil.copytree(
        ROOT / "reader_api",
        RUNTIME / "reader_api",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copytree(
        ROOT / "migrations",
        RUNTIME / "migrations",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    scripts_dir = RUNTIME / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    for script_name in (
        "run_reader_api.sh",
        "reader_pg_migrate.py",
        "reader_pg_status.py",
        "sentence_reader_runtime_bootstrap.py",
        "click_runtime_manager.py",
        "sentence_reader_runtime_config.py",
        "sentence_reader_first_run_preflight.py",
        "sentence_reader_product_diagnostics.py",
        "sentence_reader_runtime_portability.py",
        "sentence_reader_backup.py",
        "sentence_reader_restore_verify.py",
        "sentence_reader_hermes_ingest.py",
        "sentence_reader_intake_draft.py",
        "sentence_reader_promote_intake_draft.py",
        "sentence_reader_review_queue.py",
        "sentence_reader_active_pack_operator.py",
        "sentence_reader_book_vocab.py",
        "tingle_inspiration.py",
    ):
        source = ROOT / "scripts" / script_name
        if source.exists():
            target = scripts_dir / script_name
            shutil.copy2(source, target)
            target.chmod(0o755)
    shutil.copy2(ROOT / "requirements-reader-api.txt", RUNTIME / "requirements-reader-api.txt")
    if not copy_bundled_python_runtime():
        raise RuntimeError("bundled Python framework or verified Reader API dependencies are unavailable")
    write_runtime_manifest()
    (RUNTIME / "README_RUNTIME.md").write_text(
        "\n".join(
            [
                "# Sentence Reader Runtime",
                "",
                "This folder is the bundled boundary for Reader API startup and product diagnostics.",
                "Click and Tingle reuse this runtime through `scripts/click_runtime_manager.py`.",
                "The signed app bundle contains read-only Reader API code, startup scripts, and a signed Python framework.",
                "The native shell starts `scripts/run_reader_api.sh`, which prefers this bundled signed runtime.",
                "If the bundled runtime is unavailable, startup can call `sentence_reader_runtime_bootstrap.py`.",
                "If bundled startup is unavailable, the native shell can still fall back to the development project script.",
                "Runtime portability is assessed by `scripts/sentence_reader_runtime_portability.py` and `runtime_manifest.json`.",
                "First-run readiness is assessed by `scripts/sentence_reader_first_run_preflight.py`.",
                "Local paths such as FunASR can be configured with `scripts/sentence_reader_runtime_config.py`.",
                "",
            ]
        ),
        encoding="utf-8",
    )


def ensure_app_icon() -> bool:
    generator = ROOT / "scripts" / "generate_sentence_reader_icon.py"
    if APP_ICON.exists() and generator.exists() and APP_ICON.stat().st_mtime >= generator.stat().st_mtime:
        return True
    if not generator.exists():
        return False
    result = subprocess.run(
        ["python3", str(generator), "--quiet"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.returncode != 0:
        print(result.stdout)
        return False
    return APP_ICON.exists()


def main() -> int:
    build = subprocess.run(
        [
            "xcodebuild",
            "-scheme",
            "ReadiumVisualReaderProbe",
            "-destination",
            "generic/platform=macOS,variant=Mac Catalyst",
            "-clonedSourcePackagesDirPath",
            str(PACKAGE_CACHE),
            "-derivedDataPath",
            str(DERIVED_DATA),
            "build",
        ],
        cwd=VISUAL_PROBE,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    readium_probe_ready = build.returncode == 0 and PRODUCT_BINARY.exists()
    if not readium_probe_ready:
        print("warning=Readium visual probe unavailable; packaging verified native Click fallback")
        if build.stdout:
            print(build.stdout)

    native = subprocess.run(
        [
            "swiftc",
            str(NATIVE_SOURCE),
            "-o",
            str(NATIVE_BINARY),
            "-framework",
            "Cocoa",
            "-framework",
            "WebKit",
            "-framework",
            "AVFoundation",
            "-framework",
            "Speech",
            "-framework",
            "PDFKit",
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if native.returncode != 0:
        print(native.stdout)
        return native.returncode

    for app_bundle in (APP, LEGACY_APP):
        if app_bundle.exists():
            shutil.rmtree(app_bundle)

    EXECUTABLE.parent.mkdir(parents=True, exist_ok=True)
    RESOURCES.mkdir(parents=True, exist_ok=True)
    shutil.copy2(NATIVE_BINARY, EXECUTABLE)
    EXECUTABLE.chmod(0o755)

    if readium_probe_ready:
        for bundle in PRODUCTS.glob("*.bundle"):
            shutil.copytree(bundle, RESOURCES / bundle.name)

    copy_reader_runtime()
    frozen_tts_hashes = copy_frozen_tts_runtime()
    icon_ready = ensure_app_icon()
    if icon_ready:
        shutil.copy2(APP_ICON, RESOURCES / f"{APP_ICON_NAME}.icns")

    ensure_default_epub()
    shutil.copy2(DEFAULT_EPUB, RESOURCES / "default-fixture.epub")
    book_dir = RESOURCES / "default-book"
    if book_dir.exists():
        shutil.rmtree(book_dir)
    with zipfile.ZipFile(DEFAULT_EPUB) as archive:
        archive.extractall(book_dir)

    plist = {
        "CFBundleDevelopmentRegion": "zh_CN",
        "CFBundleExecutable": "SentenceReader",
        "CFBundleIdentifier": "local.sentence-reader.v1.readium",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleIconFile": APP_ICON_NAME if icon_ready else "",
        "CFBundleName": "Click",
        "CFBundleDisplayName": "Click",
        "CFBundleURLTypes": [
            {
                "CFBundleURLName": "Click Reader",
                "CFBundleURLSchemes": ["sentence-reader"],
            },
        ],
        "CFBundleDocumentTypes": [
            {
                "CFBundleTypeName": "EPUB Publication",
                "CFBundleTypeExtensions": ["epub"],
                "CFBundleTypeMIMETypes": ["application/epub+zip"],
                "CFBundleTypeRole": "Viewer",
                "LSHandlerRank": "Owner",
                "LSItemContentTypes": ["org.idpf.epub-container"],
            },
            {
                "CFBundleTypeName": "PDF Document",
                "CFBundleTypeExtensions": ["pdf"],
                "CFBundleTypeMIMETypes": ["application/pdf"],
                "CFBundleTypeRole": "Viewer",
                "LSHandlerRank": "Alternate",
                "LSItemContentTypes": ["com.adobe.pdf"],
            },
        ],
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "0.1.5",
        "CFBundleVersion": "6",
        "DTPlatformName": "maccatalyst",
        "LSMinimumSystemVersion": "14.0",
        "LSApplicationCategoryType": "public.app-category.productivity",
        "NSHighResolutionCapable": True,
        "NSDesktopFolderUsageDescription": "Click can open EPUB and PDF files selected from Desktop.",
        "NSDocumentsFolderUsageDescription": "Click can open EPUB and PDF files selected from Documents.",
        "NSMicrophoneUsageDescription": "Click records short voice notes so they can be converted to text.",
        "NSSpeechRecognitionUsageDescription": "Click can use Apple Speech to convert short voice notes to text.",
        "NSSupportsAutomaticGraphicsSwitching": True,
        "UIDeviceFamily": [2, 6],
        "UIApplicationSupportsIndirectInputEvents": True,
    }
    with INFO_PLIST.open("wb") as fh:
        plistlib.dump(plist, fh)

    codesign_identity, team_identifier = codesign_app_bundle()
    if verify_frozen_tts_runtime(TTS_RUNTIME) != frozen_tts_hashes:
        raise RuntimeError("App signing changed the frozen Microsoft TTS runtime")

    print(f"packaged={APP}")
    print(f"executable={EXECUTABLE}")
    print(f"native_shell={NATIVE_SOURCE}")
    print(f"readium_probe_binary={PRODUCT_BINARY if readium_probe_ready else 'unavailable-native-fallback'}")
    print(f"resources={len(list(RESOURCES.glob('*.bundle')))} bundles")
    print(f"reader_runtime={RUNTIME}")
    print(f"reader_runtime_python_bundled={BUNDLED_RUNTIME_PYTHON.exists()}")
    print(f"tts_runtime={TTS_RUNTIME}")
    print(f"tts_runtime_manifest_sha256={sha256_file(TTS_RUNTIME_MANIFEST)}")
    print(f"tts_helper_sha256={sha256_file(TTS_HELPER)}")
    print(f"app_icon={icon_ready}")
    print(f"codesign_identity={codesign_identity}")
    print(f"team_identifier={team_identifier}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
