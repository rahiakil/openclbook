#!/usr/bin/env bash
# Run the OpenClaw Gateway in the foreground (this terminal stays busy).
# Open another terminal and run: bash scripts/openclaw-tui.sh
#
# Usage:
#   bash scripts/openclaw-gateway-run.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=_openclaw_run.sh
source "$SCRIPT_DIR/_openclaw_run.sh"
cd "$ROOT"

openclaw_run gateway run "$@"
