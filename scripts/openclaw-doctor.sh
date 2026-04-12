#!/usr/bin/env bash
# Run openclaw doctor (health / setup checks) using the repo's local CLI.
#
# Usage: bash scripts/openclaw-doctor.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=_openclaw_run.sh
source "$SCRIPT_DIR/_openclaw_run.sh"
cd "$ROOT"

openclaw_run doctor "$@"
