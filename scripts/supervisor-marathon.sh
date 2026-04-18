#!/usr/bin/env bash
# Continuous supervisor run: one Ollama edit per LangGraph step, SQLite checkpoint
# after each chunk — suitable for ~200-page script → Netflix preset (local Ollama only).
#
# Usage (from repo root):
#   bash scripts/supervisor-marathon.sh --workspace book-pipeline/workspace \
#     --goal "Convert entire script to Netflix format; keep subplot X." \
#     --thread-id my-job-1
#
# Re-run the same command after a crash or reboot to resume the same thread-id.
# Optional: MARATHON_SLEEP=2 bash scripts/supervisor-marathon.sh ...

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BP="$ROOT/book-pipeline"
VENV="$BP/.venv"
PY="$VENV/bin/python"

if [[ ! -x "$PY" ]]; then
  echo "supervisor-marathon: no venv at $VENV — run scripts/book-ui.sh once or: python3 -m venv $VENV && pip install -e $BP" >&2
  exit 1
fi

SLEEP_ARGS=()
if [[ -n "${MARATHON_SLEEP:-}" ]]; then
  SLEEP_ARGS=(--sleep "$MARATHON_SLEEP")
fi

exec "$PY" -m book_pipeline.cli supervisor-marathon "${SLEEP_ARGS[@]}" "$@"
