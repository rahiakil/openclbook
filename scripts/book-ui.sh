#!/usr/bin/env bash
# Local book supervisor web UI (LangGraph + Ollama; optional OpenClaw tools).
# Usage: bash scripts/book-ui.sh
# Creates book-pipeline/.venv on first run and pip install -e . (needs python3-venv).
#
# Port: default 9876 (8765 is often busy). Override: BOOK_UI_PORT=8765 ../scripts/book-ui.sh
# or: ../scripts/book-ui.sh --port 8767

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BP="$ROOT/book-pipeline"
VENV="$BP/.venv"
PY="$VENV/bin/python"

cd "$BP"

if [[ ! -x "$PY" ]]; then
  echo "book-ui: creating venv at $VENV …"
  if ! python3 -m venv "$VENV"; then
    echo "book-ui: failed to create venv. On Debian/Ubuntu try: sudo apt install python3-venv" >&2
    exit 1
  fi
fi

# Require full editable install (not just PyYAML): old venvs skipped reinstall and missed /api/studio/* routes.
if ! "$PY" -c "import yaml, fastapi; import book_pipeline.studio_db" 2>/dev/null; then
  echo "book-ui: installing dependencies into venv …"
  "$PY" -m ensurepip --upgrade 2>/dev/null || true
  if ! "$PY" -m pip --version >/dev/null 2>&1; then
    echo "book-ui: venv has no pip. Remove the broken venv and install python3-venv:" >&2
    echo "  rm -rf \"$VENV\" && sudo apt install python3-venv && bash \"$0\"" >&2
    exit 1
  fi
  "$PY" -m pip install -q -U pip
  "$PY" -m pip install -q -e "$BP"
fi

PORT="${BOOK_UI_PORT:-9876}"
exec "$PY" -m book_pipeline ui --workspace "$BP/workspace" --host 127.0.0.1 --port "$PORT" "$@"
