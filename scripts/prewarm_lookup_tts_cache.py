#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
IMPORTABLE_JSON = ROOT / "reports" / "lifestudy_vocab_final_review" / "final_database_import_prepare_all_4102.json"
DEFAULT_BASE_URL = "http://127.0.0.1:18180"
DEFAULT_VOICE = "en-US-BrianNeural"


def load_terms(limit: int | None = None) -> list[str]:
    payload = json.loads(IMPORTABLE_JSON.read_text(encoding="utf-8"))
    seen: set[str] = set()
    terms: list[str] = []
    for item in payload.get("items") or []:
        term = str(item.get("term") or item.get("lemma") or "").strip()
        if not term or term in seen:
            continue
        seen.add(term)
        terms.append(term)
        if limit and len(terms) >= limit:
            break
    return terms


def post_json(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def prewarm_one(base_url: str, voice: str, term: str, timeout: float) -> dict[str, Any]:
    started = time.time()
    try:
        status = post_json(f"{base_url}/lookup/tts/status", {"text": term, "voice": voice}, timeout=timeout)
        if status.get("cached") is True:
            return {"term": term, "ok": True, "cached_before": True, "seconds": round(time.time() - started, 3)}
        payload = post_json(f"{base_url}/lookup/tts", {"text": term, "voice": voice}, timeout=timeout)
        return {
            "term": term,
            "ok": payload.get("ok") is True,
            "cached_before": False,
            "seconds": round(time.time() - started, 3),
            "error": payload.get("error") or "",
        }
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"term": term, "ok": False, "cached_before": False, "seconds": round(time.time() - started, 3), "error": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Pre-generate local lookup TTS cache for Life-study words.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--voice", default=DEFAULT_VOICE)
    parser.add_argument("--limit", type=int, default=0, help="0 means all terms")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=50)
    args = parser.parse_args()

    terms = load_terms(limit=args.limit or None)
    if not terms:
        raise SystemExit("prewarm lookup TTS cache FAIL: no terms found")

    ok = 0
    cached = 0
    failed: list[dict[str, Any]] = []
    started = time.time()
    workers = max(1, min(8, args.workers))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(prewarm_one, args.base_url.rstrip("/"), args.voice, term, args.timeout) for term in terms]
        for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
            result = future.result()
            if result.get("ok"):
                ok += 1
            else:
                failed.append(result)
            if result.get("cached_before"):
                cached += 1
            if index % 25 == 0 or index == len(terms):
                print(f"prewarm progress {index}/{len(terms)} ok={ok} cached_before={cached} failed={len(failed)}")

    summary = {
        "ok": len(failed) == 0,
        "terms": len(terms),
        "generated_or_cached": ok,
        "cached_before": cached,
        "failed": failed[:20],
        "voice": args.voice,
        "workers": workers,
        "seconds": round(time.time() - started, 3),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
