#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PG_BIN="${POSTGRES_APP_BIN:-/Applications/Postgres.app/Contents/Versions/latest/bin}"
PGDATA="${SENTENCE_READER_PGDATA:-$HOME/Library/Application Support/SentenceReader/Postgres/data-18}"
PGLOG="${SENTENCE_READER_PGLOG:-$HOME/Library/Logs/SentenceReader/postgres-18.log}"
BOOTSTRAP_SCRIPT="$ROOT/scripts/sentence_reader_runtime_bootstrap.py"
APP_SUPPORT="${SENTENCE_READER_APP_SUPPORT:-$HOME/Library/Application Support/SentenceReader}"
READER_RUNTIME_DATABASE_URL="${READER_DATABASE_URL:-${DATABASE_URL:-postgresql://localhost/sentence_reader}}"
BUNDLED_PYTHON="$ROOT/Python3.framework/Versions/3.9/bin/python3.9"
BUNDLED_PYTHON_HOME="$ROOT/Python3.framework/Versions/3.9"
BUNDLED_SITE_PACKAGES="$BUNDLED_PYTHON_HOME/lib/python3.9/site-packages"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
export READER_DATABASE_URL="$READER_RUNTIME_DATABASE_URL"
export SENTENCE_READER_REPORTS="${SENTENCE_READER_REPORTS:-$APP_SUPPORT/Reports}"
export CLICK_MOBILE_REQUIRE_APPROVAL=1

if [ -n "${READER_API_PYTHON:-}" ]; then
  PYTHON="$READER_API_PYTHON"
elif [ -x "$BUNDLED_PYTHON" ]; then
  PYTHON="$BUNDLED_PYTHON"
  export PYTHONHOME="$BUNDLED_PYTHON_HOME"
  export PYTHONPATH="$BUNDLED_SITE_PACKAGES${PYTHONPATH:+:$PYTHONPATH}"
elif [ -x "$ROOT/.venv-reader-api/bin/python" ]; then
  PYTHON="$ROOT/.venv-reader-api/bin/python"
elif [ -f "$BOOTSTRAP_SCRIPT" ] && command -v python3 >/dev/null 2>&1; then
  BOOTSTRAP_ARGS=(
    "$BOOTSTRAP_SCRIPT"
    --runtime "$ROOT"
    --app-support "$APP_SUPPORT"
    --pg-bin "$PG_BIN"
    --print-python
  )
  if [ "${SENTENCE_READER_BOOTSTRAP_REPAIR:-0}" = "1" ]; then
    BOOTSTRAP_ARGS+=(--repair-python)
  fi
  if [ "${SENTENCE_READER_BOOTSTRAP_INSTALL_DEPS:-0}" = "1" ]; then
    BOOTSTRAP_ARGS+=(--install-deps)
  fi
  PYTHON="$(python3 "${BOOTSTRAP_ARGS[@]}" 2>/dev/null || true)"
else
  PYTHON=""
fi

if [ ! -x "$PYTHON" ]; then
  echo "Reader API Python not found under runtime: $BUNDLED_PYTHON"
  echo "Run bootstrap preflight: python3 $BOOTSTRAP_SCRIPT --runtime $ROOT"
  echo "To create a user-level venv explicitly: SENTENCE_READER_BOOTSTRAP_REPAIR=1 SENTENCE_READER_BOOTSTRAP_INSTALL_DEPS=1 $0"
  exit 1
fi

cd "$ROOT"
if [ -x "$PG_BIN/postgres" ] && [ -x "$PG_BIN/pg_ctl" ] && [ -x "$PG_BIN/initdb" ]; then
  mkdir -p "$(dirname "$PGDATA")" "$(dirname "$PGLOG")"
  if [ ! -s "$PGDATA/PG_VERSION" ]; then
    "$PG_BIN/initdb" -D "$PGDATA" -U "$USER" --encoding=UTF8 --locale=C
  fi
  if ! "$PG_BIN/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1; then
    "$PG_BIN/pg_ctl" -D "$PGDATA" -l "$PGLOG" -o "-p 5432 -h localhost" start
  fi
  export PATH="$PG_BIN:$PATH"
fi

"$PYTHON" "$ROOT/scripts/reader_pg_migrate.py" --database-url "$READER_RUNTIME_DATABASE_URL" --create-database
exec "$PYTHON" -m uvicorn reader_api.app:app --host "${READER_API_HOST:-127.0.0.1}" --port "${READER_API_PORT:-18180}"
