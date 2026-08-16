#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_APP_SUPPORT = Path(
    os.getenv(
        "SENTENCE_READER_APP_SUPPORT",
        str(Path.home() / "Library" / "Application Support" / "SentenceReader"),
    )
)
REPORTS = Path(os.getenv("SENTENCE_READER_REPORTS", str(DEFAULT_APP_SUPPORT / "Reports")))
DEFAULT_RUNTIME = ROOT if ROOT.name == "ReaderRuntime" else ROOT / "build" / "Click.app" / "Contents" / "Resources" / "ReaderRuntime"
DEFAULT_PG_BIN = Path(os.getenv("POSTGRES_APP_BIN", "/Applications/Postgres.app/Contents/Versions/latest/bin"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_status(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": path.exists(),
        "is_file": path.is_file(),
        "is_dir": path.is_dir(),
        "is_symlink": path.is_symlink(),
        "executable": os.access(path, os.X_OK),
        "realpath": str(path.resolve(strict=False)),
    }


def run_deps(python: Path) -> dict[str, Any]:
    if not python.exists():
        return {"ok": False, "error": "missing_python"}
    try:
        result = subprocess.run(
            [str(python), "-c", "import fastapi, uvicorn, psycopg, httpx; print('deps ok')"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=15,
        )
        return {"ok": result.returncode == 0, "returncode": result.returncode, "output": result.stdout.strip()}
    except Exception as exc:
        return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}


def check_tcp(host: str = "127.0.0.1", port: int = 5432, timeout: float = 0.8) -> dict[str, Any]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"ok": True, "host": host, "port": port}
    except OSError as exc:
        return {"ok": False, "host": host, "port": port, "error": str(exc)}


def create_user_venv(source_python: Path, target_venv: Path) -> dict[str, Any]:
    if (target_venv / "bin" / "python").exists():
        return {"ok": True, "created": False, "path": str(target_venv)}
    target_venv.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [str(source_python), "-m", "venv", str(target_venv)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
    )
    return {"ok": result.returncode == 0, "created": result.returncode == 0, "path": str(target_venv), "output": result.stdout}


def install_requirements(python: Path, requirements: Path) -> dict[str, Any]:
    if not requirements.exists():
        return {"ok": False, "error": "requirements_missing", "path": str(requirements)}
    result = subprocess.run(
        [str(python), "-m", "pip", "install", "-r", str(requirements)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=300,
    )
    return {"ok": result.returncode == 0, "returncode": result.returncode, "output_tail": result.stdout[-4000:]}


def candidate_python(runtime: Path, app_support: Path) -> list[dict[str, Any]]:
    candidates: list[tuple[str, Path | None]] = [
        ("env_READER_API_PYTHON", Path(os.environ["READER_API_PYTHON"]) if os.getenv("READER_API_PYTHON") else None),
        ("bundled_signed_python_framework", runtime / "Python3.framework" / "Versions" / "3.9" / "bin" / "python3.9"),
        ("user_app_support_venv", app_support / "Runtime" / ".venv-reader-api" / "bin" / "python"),
        ("bundled_runtime_venv", runtime / ".venv-reader-api" / "bin" / "python"),
        ("system_python3", Path(shutil.which("python3")) if shutil.which("python3") else None),
    ]
    rows: list[dict[str, Any]] = []
    for source, path in candidates:
        if path is None:
            rows.append({"source": source, "path": None, "exists": False, "deps": {"ok": False, "error": "missing"}})
            continue
        status = file_status(path)
        status["source"] = source
        status["deps"] = run_deps(path) if status["exists"] else {"ok": False, "error": "missing"}
        rows.append(status)
    return rows


def postgres_preflight(pg_bin: Path) -> dict[str, Any]:
    binaries = {name: pg_bin / name for name in ("postgres", "pg_ctl", "initdb", "psql", "pg_dump")}
    binary_status = {name: file_status(path) for name, path in binaries.items()}
    tools_ready = all(binary_status[name]["exists"] for name in ("postgres", "pg_ctl", "initdb", "psql"))
    tcp = check_tcp()
    return {
        "strategy": "external_preflight_then_run_reader_api_bootstrap",
        "pg_bin": str(pg_bin),
        "tools_ready": tools_ready,
        "server_ready": tcp["ok"],
        "tcp_5432": tcp,
        "binaries": binary_status,
        "decision": "can_start_or_use_existing" if (tools_ready or tcp["ok"]) else "blocked_missing_postgresql",
        "repair_hint": "Install Postgres.app or set POSTGRES_APP_BIN to a directory containing postgres, pg_ctl, initdb, and psql.",
    }


def select_python(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in candidates:
        if item.get("exists") and item.get("deps", {}).get("ok"):
            return item
    return None


def build_report(runtime: Path, app_support: Path, pg_bin: Path, repair_python: bool, install_deps: bool) -> dict[str, Any]:
    runtime = runtime.expanduser()
    app_support = app_support.expanduser()
    pg_bin = pg_bin.expanduser()
    requirements = runtime / "requirements-reader-api.txt"
    bootstrap_venv = app_support / "Runtime" / ".venv-reader-api"
    repair: dict[str, Any] = {"requested": repair_python, "install_deps": install_deps, "venv": str(bootstrap_venv)}

    source_python = Path(shutil.which("python3")) if shutil.which("python3") else None
    if repair_python:
        if source_python is None:
            repair["create_venv"] = {"ok": False, "error": "system_python3_missing"}
        else:
            repair["create_venv"] = create_user_venv(source_python, bootstrap_venv)
            if repair["create_venv"].get("ok") and install_deps:
                repair["install_deps"] = install_requirements(bootstrap_venv / "bin" / "python", requirements)

    candidates = candidate_python(runtime, app_support)
    selected = select_python(candidates)
    postgres = postgres_preflight(pg_bin)
    python_ready = selected is not None
    postgres_decision = postgres["tools_ready"] or postgres["server_ready"]
    startup_ready = bool(python_ready and postgres_decision)
    blocked_reasons: list[str] = []
    if not python_ready:
        blocked_reasons.append("python_dependencies_not_ready")
    if not postgres_decision:
        blocked_reasons.append("postgresql_not_ready")

    return {
        "schema": "sentence_reader.runtime_bootstrap_report.v1",
        "generated_at": now_iso(),
        "runtime": str(runtime),
        "app_support": str(app_support),
        "requirements": str(requirements),
        "python_candidates": candidates,
        "selected_python": selected,
        "python_ready": python_ready,
        "postgres": postgres,
        "startup_ready": startup_ready,
        "blocked_reasons": blocked_reasons,
        "repair": repair,
        "policy": {
            "auto_install_dependencies_by_default": False,
            "auto_install_postgresql_by_default": False,
            "repair_python_requires_explicit_flag": "--repair-python",
            "dependency_install_requires_explicit_flag": "--install-deps",
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    selected = report.get("selected_python") or {}
    lines = [
        "# Sentence Reader Runtime Bootstrap",
        "",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Startup ready: `{report.get('startup_ready')}`",
        f"- Python ready: `{report.get('python_ready')}`",
        f"- Selected Python: `{selected.get('path') or '-'}`",
        f"- PostgreSQL decision: `{(report.get('postgres') or {}).get('decision')}`",
        "",
        "## Blocked Reasons",
        "",
    ]
    blocked = report.get("blocked_reasons") or []
    lines.extend([f"- `{item}`" for item in blocked] or ["- none"])
    lines.extend(["", "## Policy", ""])
    for key, value in (report.get("policy") or {}).items():
        lines.append(f"- `{key}`: `{value}`")
    return "\n".join(lines).strip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Preflight and optionally bootstrap Sentence Reader runtime dependencies.")
    parser.add_argument("--runtime", default=str(DEFAULT_RUNTIME))
    parser.add_argument("--app-support", default=str(DEFAULT_APP_SUPPORT))
    parser.add_argument("--pg-bin", default=str(DEFAULT_PG_BIN))
    parser.add_argument("--output", default=str(REPORTS / "sentence_reader_runtime_bootstrap_report.json"))
    parser.add_argument("--markdown", default=str(REPORTS / "sentence_reader_runtime_bootstrap_report.md"))
    parser.add_argument("--repair-python", action="store_true")
    parser.add_argument("--install-deps", action="store_true")
    parser.add_argument("--print-python", action="store_true")
    parser.add_argument("--require-startup-ready", action="store_true")
    parser.add_argument("--require-postgres-decision", action="store_true")
    args = parser.parse_args()

    report = build_report(
        runtime=Path(args.runtime),
        app_support=Path(args.app_support),
        pg_bin=Path(args.pg_bin),
        repair_python=args.repair_python,
        install_deps=args.install_deps,
    )
    output = Path(args.output).expanduser()
    markdown = Path(args.markdown).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    markdown.parent.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    if args.require_startup_ready and not report["startup_ready"]:
        failures.append("startup_ready")
    if args.require_postgres_decision and not (report["postgres"]["tools_ready"] or report["postgres"]["server_ready"]):
        failures.append("postgres_decision")
    report["ok"] = not failures
    report["failures"] = failures
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown.write_text(render_markdown(report), encoding="utf-8")

    if args.print_python:
        selected = report.get("selected_python") or {}
        path = selected.get("path")
        if path:
            print(path)
            return 0
        return 2

    print(f"runtime_bootstrap_report={output}")
    print(f"runtime_bootstrap_markdown={markdown}")
    print(f"startup_ready={report['startup_ready']} blocked={report['blocked_reasons']}")
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
