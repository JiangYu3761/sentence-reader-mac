#!/usr/bin/env python3
"""Static contract smoke for Click old-device Lite fallback."""

from __future__ import annotations

import ast
import pathlib
import re
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "reader_api" / "app.py"
MOBILE = ROOT / "reader_api" / "mobile_workspace.py"


def fail(message: str) -> None:
    print(f"FAIL: {message}")
    raise SystemExit(1)


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        fail(f"missing {label}: {needle}")


def check_static_contracts() -> None:
    app = APP.read_text(encoding="utf-8")
    mobile = MOBILE.read_text(encoding="utf-8")

    require(mobile, "def should_use_lite_ui", "lite decision function")
    require(mobile, "LEGACY_LITE_UA_PATTERNS", "high-confidence legacy UA list")
    require(mobile, 'UI_PREFERENCE_COOKIE = "click_ui"', "click_ui preference cookie")
    require(mobile, 'request.query_params.get("ui")', "explicit ?ui preference")
    require(mobile, "def modern_capability_guard", "ES5 capability guard")
    require(mobile, "/home-lite?reason=capability", "home capability redirect")
    require(mobile, '@router.get("/home-lite"', "home-lite route")
    require(mobile, "def home_lite_html", "home-lite HTML renderer")
    require(mobile, 'href="/library-lite"', "lite reading entry")
    require(mobile, 'href="/recordings"', "lite recordings entry")
    require(mobile, 'href="/hermes"', "lite Hermes entry")
    if r"\bMobile\b" in mobile or r"\bSafari\b" in mobile or r"\bChrome\b" in mobile:
        fail("legacy UA list must not match broad Mobile/Safari/Chrome patterns")

    require(app, '@app.get("/library-lite"', "library-lite route")
    require(app, '@app.get("/reader-lite"', "reader-lite route")
    require(app, '@app.get("/reader-lite/toc"', "reader-lite toc route")
    require(app, '@app.get("/reader-lite/position"', "reader-lite screen page position route")
    require(app, '@app.post("/reader-lite/red"', "lite red route")
    require(app, '@app.post("/reader-lite/note"', "lite note route")
    require(app, '@app.post("/reader-lite/audio-note"', "lite audio note route")
    require(app, "def library_lite_html", "library-lite HTML renderer")
    require(app, "def reader_lite_html", "reader-lite HTML renderer")
    require(app, "def reader_lite_toc_html", "reader-lite TOC renderer")
    require(app, "def lite_reading_order", "reader-lite reading order manifest")
    require(app, "readingOrder spine toc", "reader-lite manifest/toc marker")
    require(app, "def lite_first_readable_chapter_payload", "reader-lite empty chapter skip helper")
    require(app, "def lite_chapter_readability_score", "reader-lite front matter filter")
    require(app, "LITE_FRONT_MATTER_RE", "reader-lite front matter marker list")
    require(app, "LITE_FRONT_MATTER_TITLE_RE", "reader-lite strong front matter title marker")
    require(app, "LITE_FRONT_MATTER_HREF_RE", "reader-lite strong front matter href marker")
    require(app, "LITE_FIRST_MAIN_CHAPTER_RE", "reader-lite first chapter preference")
    require(app, 'LITE_SCREEN_PAGED_MODE = "screen_paged"', "reader-lite screen pagination mode")
    require(app, "def lite_adjacent_readable_chapter_index", "reader-lite adjacent chapter navigation")
    require(app, "def lite_continue_href", "reader-lite continue-reading href")
    require(app, "def lite_chapter_index_for_locator", "reader-lite saved locator mapping")
    require(app, "def lite_upsert_reading_position", "reader-lite position persistence")
    require(app, "def lite_reading_progress_ratio", "reader-lite page-aware progress ratio")
    require(app, "reader.reading_positions", "existing reading position reuse")
    require(app, "图书在版", "reader-lite skips CIP pages")
    require(app, "此章节正文提取失败", "reader-lite explicit extraction failure")
    require(app, "selected-panel", "reader-lite selected sentence action panel")
    require(app, "sentence-link", "reader-lite readable sentence links")
    require(app, "page-turn prev", "reader-lite left page-turn hit zone")
    require(app, "page-turn next", "reader-lite right page-turn hit zone")
    require(app, "sentence-unit", "reader-lite sentence units for screen pagination")
    require(app, "buildPages", "reader-lite screen pagination builder")
    require(app, "viewportHeight", "reader-lite viewport measurement")
    require(app, "visualViewport", "reader-lite visual viewport measurement")
    require(app, "bottomSafeReserve", "reader-lite browser navigation safe area")
    require(app, "unitHeight", "reader-lite rendered sentence measurement")
    require(app, "savePosition", "reader-lite page position save")
    require(app, "syncVisiblePageForms", "reader-lite current page form sync")
    require(app, "setQueryParam", "reader-lite current page link sync")
    require(app, "/reader-lite/position?book_id=", "reader-lite position beacon")
    require(app, "selected-actions", "reader-lite compact selected action row")
    require(app, "action-form", "reader-lite compact red action")
    require(app, "lite-action", "reader-lite compact note/voice actions")
    require(app, "touchstart", "reader-lite swipe start listener")
    require(app, "touchend", "reader-lite swipe end listener")
    require(app, "Click Lite 目录", "reader-lite TOC page title")
    require(app, "继续读", "library-lite continue reading entry")
    require(app, "目录</a>", "library-lite TOC entry")
    require(app, "reader.annotations", "existing annotation table reuse")
    require(app, "create_annotation(", "existing annotation write helper reuse")
    require(app, "patch_annotation(", "existing note patch helper reuse")
    require(app, "create_audio_note(", "existing audio note helper reuse")
    require(app, "start_lan_audio_note_transcription", "Mac voice pipeline reuse")
    require(app, 'window.location.replace(\'/library-lite?reason=capability\')', "library capability redirect")
    require(app, "application/x-www-form-urlencoded", "dependency-light urlencoded form parser")
    require(app, "multipart/form-data", "dependency-light audio upload parser")
    toc_section = app.split("def reader_lite_toc_html", 1)[1].split("def library_page_html_v2", 1)[0]
    reader_section = app.split("def reader_lite_html", 1)[1].split("def reader_lite_toc_html", 1)[0]
    if "lite_chapter_payload" in toc_section or "lite_chapter_readability_score" in toc_section:
        fail("reader-lite TOC must not open every chapter to extract正文")
    if 'nav{{' in reader_section:
        fail("reader-lite reading page must not keep a persistent nav toolbar")
    for forbidden in ("<header>", "lite-top", "已跳过封面"):
        if forbidden in reader_section:
            fail(f"reader-lite reading page must not show chrome/notice: {forbidden}")
    red_section = app.split('@app.post("/reader-lite/red"', 1)[1].split('@app.post("/reader-lite/note"', 1)[0]
    if '"已标红", selected' in red_section or '"已取消红标", selected' in red_section:
        fail("reader-lite red action must exit selected mode after marking")
    if " Form(" in app or " File(" in app or "UploadFile" in app:
        fail("Lite routes must not require python-multipart at import time")
    if "ALTER TABLE" in app and "reader-lite" in app:
        fail("Lite fallback must not add PostgreSQL schema migrations")


def check_ua_routing() -> None:
    tree = ast.parse(MOBILE.read_text(encoding="utf-8"))
    patterns: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            if "LEGACY_LITE_UA_PATTERNS" in names:
                value = ast.literal_eval(node.value)
                patterns = list(value)
                break
    if not patterns:
        fail("could not read LEGACY_LITE_UA_PATTERNS")

    def ua_is_lite(ua: str, query: dict[str, str] | None = None, cookies: dict[str, str] | None = None) -> bool:
        query = query or {}
        cookies = cookies or {}
        explicit = str(query.get("ui") or "").strip().lower()
        if explicit == "modern":
            return False
        if explicit == "lite":
            return True
        cookie = str(cookies.get("click_ui") or "").strip().lower()
        if cookie == "modern":
            return False
        if cookie == "lite":
            return True
        return any(re.search(pattern, ua, flags=re.IGNORECASE) for pattern in patterns)

    modern_uas = [
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 Version/17.5 Safari/605.1.15",
        "Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 Version/17.5 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 Version/4.0 Chrome/124.0 Mobile Safari/537.36",
        "Mozilla/5.0 (Linux; Android 12; wv) AppleWebKit/537.36 Version/4.0 Chrome/120.0 Mobile Safari/537.36",
    ]
    legacy_uas = [
        "Mozilla/5.0 (Mobile; LYF/F90M/LYF-F90M-000-03-12-280219; rv:48.0) Gecko/48.0 Firefox/48.0 KAIOS/2.5",
        "Opera/9.80 (Android; Opera Mini/36.2.2254/191.249; U; en) Presto/2.12.423 Version/12.16",
        "Mozilla/5.0 (Linux; U; Android 4.4.2; en-us; Nexus 4 Build/KOT49H) AppleWebKit/534.30 Version/4.0 Mobile Safari/534.30",
    ]
    for ua in modern_uas:
        if ua_is_lite(ua):
            fail(f"modern UA was incorrectly routed to Lite: {ua}")
    for ua in legacy_uas:
        if not ua_is_lite(ua):
            fail(f"legacy UA was not routed to Lite: {ua}")
    if not ua_is_lite(modern_uas[0], query={"ui": "lite"}):
        fail("?ui=lite must force Lite")
    if ua_is_lite(legacy_uas[0], query={"ui": "modern"}):
        fail("?ui=modern must force modern")
    if not ua_is_lite(modern_uas[0], cookies={"click_ui": "lite"}):
        fail("click_ui=lite cookie must select Lite")
    if ua_is_lite(legacy_uas[0], cookies={"click_ui": "modern"}):
        fail("click_ui=modern cookie must select modern")


def main() -> None:
    check_static_contracts()
    check_ua_routing()
    print("OK: Click Lite static smoke passed")


if __name__ == "__main__":
    main()
