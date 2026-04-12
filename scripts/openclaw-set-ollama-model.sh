#!/usr/bin/env bash
# Point OpenClaw's default at a local Ollama model.
#
# Usage:
#   export OLLAMA_MODEL="gemma4:31b"
#   bash scripts/openclaw-set-ollama-model.sh
#
# Runs from repo root; prefers `node …/openclaw.mjs` over `npx` (see _openclaw_run.sh).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=_openclaw_run.sh
source "$SCRIPT_DIR/_openclaw_run.sh"
cd "$ROOT"

OLLAMA_MODEL="${OLLAMA_MODEL:?Set OLLAMA_MODEL to your Ollama tag, e.g. gemma4:31b}"

REF="ollama/${OLLAMA_MODEL}"
echo "Setting OpenClaw primary model to: $REF"
openclaw_run models set "$REF"
echo "Done. Verify with: bash scripts/openclaw-models-list.sh"
