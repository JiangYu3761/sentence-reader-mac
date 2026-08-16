#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME = ROOT / "build" / "Click.app" / "Contents" / "Resources" / "ReaderRuntime"
PG_BIN = Path(os.getenv("POSTGRES_APP_BIN", "/Applications/Postgres.app/Contents/Versions/latest/bin"))


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def health(port: int) -> dict:
    try:
        request = Request(f"http://127.0.0.1:{port}/health", headers={"Accept": "application/json"})
        with urlopen(request, timeout=0.8) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return {"ok": bool(payload.get("ok")), "payload": payload}
    except Exception as exc:
        return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}


def assert_runtime_ready(runtime: Path) -> Path:
    python = runtime / "Python3.framework" / "Versions" / "3.9" / "bin" / "python3.9"
    script = runtime / "scripts" / "run_reader_api.sh"
    migration = runtime / "migrations" / "reader" / "001_reader_schema.sql"
    missing = [str(path) for path in (python, script, migration) if not path.exists()]
    if missing:
        raise RuntimeError(f"packaged runtime missing required files: {missing}")
    result = subprocess.run(
        [str(python), "-c", "import fastapi, uvicorn, psycopg, httpx; print('deps ok')"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"bundled signed runtime python deps failed: {result.stdout}")
    return python


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch the packaged ReaderRuntime and verify Reader API health.")
    parser.add_argument("--runtime", default=str(DEFAULT_RUNTIME))
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=8.0)
    args = parser.parse_args()

    runtime = Path(args.runtime).expanduser()
    port = args.port or free_port()
    process: subprocess.Popen | None = None
    log_path = Path(tempfile.gettempdir()) / f"sentence-reader-runtime-launch-{port}.log"
    try:
        python = assert_runtime_ready(runtime)
        env = os.environ.copy()
        env.pop("READER_API_PYTHON", None)
        env["READER_API_HOST"] = "127.0.0.1"
        env["READER_API_PORT"] = str(port)
        if PG_BIN.exists():
            env["PATH"] = f"{PG_BIN}:{env.get('PATH', '')}"

        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [str(runtime / "scripts" / "run_reader_api.sh")],
                cwd=runtime,
                env=env,
                text=True,
                stdout=log,
                stderr=subprocess.STDOUT,
            )

        deadline = time.time() + args.timeout
        last = {"ok": False, "error": "not checked"}
        while time.time() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"runtime process exited early code={process.returncode} log={log_path.read_text(encoding='utf-8')}")
            last = health(port)
            if last["ok"]:
                print(f"reader runtime launch smoke PASS port={port} log={log_path}")
                return 0
            time.sleep(0.2)
        raise RuntimeError(f"runtime health timeout port={port} last={last} log={log_path.read_text(encoding='utf-8')}")
    except Exception as exc:
        print(f"reader runtime launch smoke FAIL: {exc}")
        return 1
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


if __name__ == "__main__":
    raise SystemExit(main())
