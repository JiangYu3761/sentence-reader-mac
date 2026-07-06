#!/usr/bin/env python3
"""Live full-library smoke for Click Lite reading."""

from __future__ import annotations

import re
import time
from html import unescape
from urllib.parse import urljoin
from urllib.request import Request, urlopen


BASE = "http://127.0.0.1:18180"
LEGACY_UA = "Mozilla/5.0 (Mobile; rv:48.0) Gecko/48.0 KAIOS/2.5"


def fail(message: str) -> None:
    print(f"FAIL: {message}")
    raise SystemExit(1)


def fetch(path: str, timeout: int = 20) -> tuple[str, float]:
    request = Request(urljoin(BASE, path), headers={"User-Agent": LEGACY_UA})
    started = time.monotonic()
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - local smoke target.
        body = response.read().decode("utf-8", errors="replace")
    elapsed = time.monotonic() - started
    if not body:
        fail(f"empty response from {path}")
    return body, elapsed


def extract_book_links(library_html: str) -> list[str]:
    ids = re.findall(r"/reader-lite\?book_id=([^\"&]+)", library_html)
    output: list[str] = []
    for book_id in ids:
        if book_id not in output:
            output.append(unescape(book_id))
    return output


def extract_chapter_links(toc_html: str) -> list[tuple[int, str, bool]]:
    output: list[tuple[int, str, bool]] = []
    for item in re.findall(r"<li class=\"([^\"]+)\"[^>]*>\s*<a href=\"([^\"]+)\">([^<]+)</a>", toc_html):
        class_name, href, title = item
        match = re.search(r"[?&]chapter=(\d+)", href)
        if not match:
            continue
        output.append((int(match.group(1)), unescape(title), "front-matter" in class_name))
    return output


def choose_probe_chapters(chapters: list[tuple[int, str, bool]]) -> list[tuple[int, str]]:
    readable = [(index, title) for index, title, front in chapters if not front]
    if not readable:
        return []
    picks: list[tuple[int, str]] = []
    for position in (0, 4, 13, len(readable) - 1):
        if 0 <= position < len(readable):
            candidate = readable[position]
            if candidate not in picks:
                picks.append(candidate)
    for candidate in readable:
        if "第十四" in candidate[1] or "第14" in candidate[1] or "第 14" in candidate[1]:
            if candidate not in picks:
                picks.append(candidate)
            break
    return picks


def assert_chapter_page(book_id: str, chapter_index: int, title: str) -> None:
    path = f"/reader-lite?book_id={book_id}&chapter={chapter_index}&page=0&auto=0"
    html, elapsed = fetch(path)
    if elapsed > 5:
        fail(f"chapter page too slow ({elapsed:.2f}s): {book_id} chapter={chapter_index}")
    if "此章节正文提取失败" in html or "本页暂无正文" in html:
        fail(f"chapter extraction failed: {book_id} chapter={chapter_index} {title}")
    if "<form" in html:
        fail(f"default reader page shows forms before sentence selection: {book_id} chapter={chapter_index}")
    if "<header" in html or "lite-top" in html or "已跳过封面" in html:
        fail(f"reader page showed visible chrome/notice: {book_id} chapter={chapter_index}")
    if "page-turn" not in html:
        fail(f"reader page has no side page-turn hit zone: {book_id} chapter={chapter_index}")
    if "buildPages" not in html or "/reader-lite/position" not in html:
        fail(f"reader page missing screen pagination or page save: {book_id} chapter={chapter_index}")
    sentence_count = html.count("sentence-link")
    if sentence_count < 2:
        fail(f"chapter has too little正文: {book_id} chapter={chapter_index} {title} sentence_count={sentence_count}")
    if "上一篇 回目录 下一篇" in html or "上一篇回目录下一篇" in html:
        fail(f"chapter still contains EPUB inline navigation: {book_id} chapter={chapter_index}")
    first_sentence = re.search(r"selected=([^\"&]+)&auto=0#s", html)
    if not first_sentence:
        fail(f"chapter has no selectable sentence: {book_id} chapter={chapter_index}")
    selected_id = unescape(first_sentence.group(1))
    selected_html, _elapsed = fetch(f"/reader-lite?book_id={book_id}&chapter={chapter_index}&page=0&selected={selected_id}&auto=0")
    for marker in ("selected-panel", "/reader-lite/red", "/reader-lite/note", "/reader-lite/audio-note"):
        if marker not in selected_html:
            fail(f"selected sentence missing {marker}: {book_id} chapter={chapter_index}")


def main() -> None:
    library_html, _elapsed = fetch("/library-lite")
    book_ids = extract_book_links(library_html)
    if not book_ids:
        fail("library-lite has no books")
    tested_books = 0
    skipped_books = 0
    for book_id in book_ids:
        toc_html, toc_elapsed = fetch(f"/reader-lite/toc?book_id={book_id}", timeout=30)
        if toc_elapsed > 5:
            fail(f"TOC too slow ({toc_elapsed:.2f}s): {book_id}")
        if "data-lite-manifest=\"readingOrder spine toc\"" not in toc_html:
            fail(f"TOC missing Lite manifest marker: {book_id}")
        chapters = extract_chapter_links(toc_html)
        if not chapters:
            fail(f"TOC has no chapter links: {book_id}")
        picks = choose_probe_chapters(chapters)
        if not picks:
            skipped_books += 1
            continue
        for chapter_index, title in picks:
            assert_chapter_page(book_id, chapter_index, title)
        tested_books += 1
    if tested_books == 0:
        fail("no readable books were tested")
    print(f"OK: Click Lite full-library smoke passed books={tested_books} skipped_front_matter_only={skipped_books}")


if __name__ == "__main__":
    main()
