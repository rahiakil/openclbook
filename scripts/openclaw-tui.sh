#!/usr/bin/env bash
# Open the OpenClaw terminal UI (chat in the console).
#
# The TUI talks to the Gateway over WebSocket. Start the gateway first if nothing is listening:
#   bash scripts/openclaw-gateway-run.sh
#   # or: npm run gateway:run
#   # or use a user service from: openclaw onboard --install-daemon
#
# Usage:
#   bash scripts/openclaw-tui.sh
#   bash scripts/openclaw-tui.sh --session main --deliver

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=_openclaw_run.sh
source "$SCRIPT_DIR/_openclaw_run.sh"
cd "$ROOT"

openclaw_run tui "$@"
