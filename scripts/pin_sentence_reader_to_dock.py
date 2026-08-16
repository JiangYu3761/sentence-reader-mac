#!/usr/bin/env python3
from __future__ import annotations

import argparse
import plistlib
import subprocess
from pathlib import Path
from urllib.parse import quote, unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "build" / "Click.app"
DOCK_PLIST = Path.home() / "Library/Preferences/com.apple.dock.plist"


def app_uri(app_path: Path) -> str:
    return "file://" + quote(str(app_path), safe="/") + "/"


def dock_entry_url(entry: dict) -> str:
    tile_data = entry.get("tile-data") if isinstance(entry, dict) else {}
    file_data = tile_data.get("file-data") if isinstance(tile_data, dict) else {}
    value = file_data.get("_CFURLString") if isinstance(file_data, dict) else None
    return value if isinstance(value, str) else ""


def dock_entry_label(entry: dict) -> str:
    tile_data = entry.get("tile-data") if isinstance(entry, dict) else {}
    value = tile_data.get("file-label") if isinstance(tile_data, dict) else None
    return value if isinstance(value, str) else ""


def dock_entry_is_target(entry: dict, app_path: Path) -> bool:
    raw_url = dock_entry_url(entry)
    decoded_url = unquote(raw_url)
    parsed_path = unquote(urlparse(raw_url).path).rstrip("/")
    return (
        parsed_path == str(app_path)
        or decoded_url.rstrip("/") == app_uri(app_path).rstrip("/")
    )


def dock_entry_is_legacy_reader(entry: dict) -> bool:
    raw_url = dock_entry_url(entry)
    label = dock_entry_label(entry)
    parsed_path = unquote(urlparse(raw_url).path).rstrip("/")
    return label == "Sentence Reader" or parsed_path.endswith("Sentence Reader.app")


def dock_entry_is_legacy_voice(entry: dict) -> bool:
    raw_url = dock_entry_url(entry)
    label = dock_entry_label(entry)
    parsed_path = unquote(urlparse(raw_url).path).rstrip("/")
    return label == "Click Voice" or parsed_path.endswith("Click Voice.app")


def app_label(app_path: Path) -> str:
    plist_path = app_path / "Contents" / "Info.plist"
    try:
        with plist_path.open("rb") as handle:
            payload = plistlib.load(handle)
        label = payload.get("CFBundleDisplayName") or payload.get("CFBundleName")
        if isinstance(label, str) and label.strip():
            return label.strip()
    except Exception:
        pass
    return app_path.stem


def dedupe_dock_entries(app_path: Path) -> int:
    if not DOCK_PLIST.exists():
        return 0
    try:
        with DOCK_PLIST.open("rb") as handle:
            payload = plistlib.load(handle)
    except Exception:
        return 0
    apps = payload.get("persistent-apps")
    if not isinstance(apps, list):
        return 0
    kept: list[dict] = []
    removed = 0
    seen_click = False
    remove_legacy_reader = app_path.name == "Click.app"
    remove_legacy_voice = app_path.name == "Tingle.app"
    for entry in apps:
        if isinstance(entry, dict) and dock_entry_is_target(entry, app_path):
            if seen_click:
                removed += 1
                continue
            seen_click = True
        elif remove_legacy_reader and isinstance(entry, dict) and dock_entry_is_legacy_reader(entry):
            removed += 1
            continue
        elif remove_legacy_voice and isinstance(entry, dict) and dock_entry_is_legacy_voice(entry):
            removed += 1
            continue
        kept.append(entry)
    if removed == 0:
        return 0
    payload["persistent-apps"] = kept
    with DOCK_PLIST.open("wb") as handle:
        plistlib.dump(payload, handle)
    subprocess.run(["killall", "Dock"], text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return removed


def dock_contains(app_path: Path) -> bool:
    result = subprocess.run(
        ["defaults", "read", "com.apple.dock", "persistent-apps"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    haystack = result.stdout
    return (
        str(app_path) in haystack
        or app_uri(app_path) in haystack
        or quote(str(app_path), safe="/") in haystack
    )


def dock_item_xml(app_path: Path) -> str:
    escaped = app_uri(app_path).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    escaped_label = app_label(app_path).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        "<dict>"
        "<key>tile-data</key>"
        "<dict>"
        "<key>file-data</key>"
        "<dict>"
        "<key>_CFURLString</key>"
        f"<string>{escaped}</string>"
        "<key>_CFURLStringType</key>"
        "<integer>0</integer>"
        "</dict>"
        "<key>file-label</key>"
        f"<string>{escaped_label}</string>"
        "</dict>"
        "<key>tile-type</key>"
        "<string>file-tile</string>"
        "</dict>"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Pin the packaged Click app to the macOS Dock.")
    parser.add_argument("--app", default=str(APP))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    app_path = Path(args.app).expanduser().resolve()
    if not app_path.exists():
        print(f"dock pin FAIL missing_app={app_path}")
        return 1
    removed = dedupe_dock_entries(app_path)
    if dock_contains(app_path):
        suffix = f" removed_duplicates={removed}" if removed else ""
        print(f"dock pin SKIP already_present={app_path}{suffix}")
        return 0
    if args.dry_run:
        print(f"dock pin DRY_RUN would_add={app_path}")
        return 0

    result = subprocess.run(
        ["defaults", "write", "com.apple.dock", "persistent-apps", "-array-add", dock_item_xml(app_path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.returncode != 0:
        print(result.stdout)
        return result.returncode
    subprocess.run(["killall", "Dock"], text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"dock pin PASS added={app_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
