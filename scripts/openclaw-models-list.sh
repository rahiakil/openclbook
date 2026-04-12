#!/usr/bin/env bash
# List models OpenClaw knows about (includes Ollama after pull + discovery).
#
# Prerequisite: Ollama auth for discovery is often OLLAMA_API_KEY=ollama-local
# (see OpenClaw Ollama docs). If list is empty, export that and retry.
#
# Usage:
#   export OLLAMA_API_KEY=ollama-local   # if needed
#   bash scripts/openclaw-models-list.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=_openclaw_run.sh
source "$SCRIPT_DIR/_openclaw_run.sh"
cd "$ROOT"

export OLLAMA_API_KEY="${OLLAMA_API_KEY:-ollama-local}"

openclaw_run models list
