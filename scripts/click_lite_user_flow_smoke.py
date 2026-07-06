#!/usr/bin/env python3
"""Live user-flow smoke for Click Lite reading."""

from __future__ import annotations

import re
import sys
from html import unescape
from urllib.parse import urljoin
from urllib.request import Request, urlopen


BASE = "http://127.0.0.1:18180"


def fail(message: str) -> None:
    print(f"FAIL: {message}")
    raise SystemExit(1)


def fetch(path: str) -> str:
    url = urljoin(BASE, path)
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 (Mobile; rv:48.0) Gecko/48.0 KAIOS/2.5"})
    with urlopen(request, timeout=10) as response:  # noqa: S310 - local smoke target.
        body = response.read().decode("utf-8", errors="replace")
    if not body:
        fail(f"empty response from {path}")
    return body


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        fail(f"missing {label}: {needle}")


def extract_first(pattern: str, text: str, label: str) -> str:
    match = re.search(pattern, text)
    if not match:
        fail(f"could not extract {label}")
    return unescape(match.group(1))


def extract_book_ids(library_html: str) -> list[str]:
    ids: list[str] = []
    for book_id in re.findall(r"/reader-lite\?book_id=([^\"&]+)", library_html):
        book_id = unescape(book_id)
        if book_id not in ids:
            ids.append(book_id)
    return ids


def choose_readable_book(library_html: str) -> tuple[str, str, list[str]]:
    for book_id in extract_book_ids(library_html):
        toc = fetch(f"/reader-lite/toc?book_id={book_id}")
        chapter_links = re.findall(r"/reader-lite\?book_id=[^\"']+?chapter=(\d+)[^\"']*", toc)
        if len(chapter_links) >= 5:
            return book_id, toc, chapter_links
    fail("library-lite has no book with enough readable chapters for the live flow smoke")


def main() -> None:
    home = fetch("/home?ui=lite")
    require(home, "/library-lite", "Lite reading entry")
    require(home, "/recordings", "Lite recordings entry")
    require(home, "/hermes", "Lite Hermes entry")

    library = fetch("/library-lite")
    require(library, "Click Lite 书库", "Lite library title")
    require(library, "继续读", "Lite continue reading action")
    require(library, "/reader-lite/toc", "Lite TOC action")
    book_id, toc, chapter_links = choose_readable_book(library)

    reader = fetch(f"/reader-lite?book_id={book_id}&auto=1")
    require(reader, "sentence-link", "clickable正文 sentence")
    if "<header" in reader or "lite-top" in reader or "已跳过封面" in reader:
        fail("reader page should display正文 only, without visible chrome or skip notice")
    if "page-turn" not in reader:
        fail("reader page should expose left/right page-turn hit zones")
    require(reader, "buildPages", "screen-fit pagination script")
    require(reader, "/reader-lite/position", "screen page position save route")
    if "本页暂无正文" in reader:
        fail("reader default page still has no正文")
    if "图书在版" in reader or "CIP" in reader:
        fail("reader default page opened front matter instead of正文")
    if "<form" in reader:
        fail("reader default page should not show red/note/audio forms before selection")

    require(toc, "Click Lite 目录", "TOC page")
    preferred = None
    for value in chapter_links:
        chapter = int(value)
        if chapter >= 13:
            preferred = chapter
            break
    if preferred is None:
        preferred = int(chapter_links[min(4, len(chapter_links) - 1)])

    chapter_page = fetch(f"/reader-lite?book_id={book_id}&chapter={preferred}&page=0&auto=0")
    require(chapter_page, "sentence-link", "chapter正文 sentence")
    if "<header" in chapter_page or "lite-top" in chapter_page:
        fail(f"TOC chapter {preferred} showed reader chrome")
    if "page-turn" not in chapter_page:
        fail(f"TOC chapter {preferred} has no page-turn hit zone")
    require(chapter_page, "sentence-unit", "chapter sentence units")
    if "本页暂无正文" in chapter_page:
        fail(f"TOC chapter {preferred} opened an empty page")
    sentence_id = extract_first(r"selected=([^\"&]+)&auto=0#s", chapter_page, "first sentence id")

    selected = fetch(f"/reader-lite?book_id={book_id}&chapter={preferred}&page=0&selected={sentence_id}&auto=0")
    require(selected, "selected-panel", "selected sentence action panel")
    require(selected, "selected-actions", "compact selected action row")
    require(selected, "/reader-lite/red", "selected red form")
    require(selected, "/reader-lite/note", "selected note form")
    require(selected, "/reader-lite/audio-note", "selected audio-note form")

    print(
        "OK: Click Lite live user flow passed "
        f"book_id={book_id} chapter={preferred} selected_sentence={sentence_id}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - smoke should return a concise failure.
        if isinstance(exc, SystemExit):
            raise
        print(f"FAIL: {exc}")
        raise SystemExit(1) from exc
