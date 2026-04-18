#!/usr/bin/env bash
# Watch workspace/todo.file and run one queued task per event (debounced).
# Requires: inotify-tools (inotifywait), Python env with book-pipeline installed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE="${1:-$SCRIPT_DIR/workspace}"
TODO="$WORKSPACE/todo.file"

if ! command -v inotifywait >/dev/null 2>&1; then
  echo "install inotify-tools: sudo apt install inotify-tools" >&2
  exit 1
fi

mkdir -p "$WORKSPACE"
touch "$TODO"

run_once() {
  # Prefer venv next to this script
  if [[ -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
    "$SCRIPT_DIR/.venv/bin/python" -m book_pipeline run-once --workspace "$WORKSPACE"
  else
    python3 -m book_pipeline run-once --workspace "$WORKSPACE"
  fi
}

echo "Watching $TODO (modify/close_write). Ctrl+C to stop."
while true; do
  inotifywait -e modify,close_write,moved_to --format '%e' "$TODO" 2>/dev/null || true
  sleep 0.7
  run_once || true
done
