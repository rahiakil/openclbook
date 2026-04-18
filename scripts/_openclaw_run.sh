#!/usr/bin/env bash
# INTERNAL ONLY — do not run this file directly; it defines openclaw_run() for other
# scripts to source. It does not open any UI or start the gateway.
#
# Call: scripts/openclaw-tui.sh  OR  scripts/openclaw-gateway-run.sh  OR  npx openclaw …
#
# Resolves Node (nvm/fnm/…) and runs node …/openclaw.mjs (avoids some npx URL issues).
# Requires ROOT set by the caller to the repo root (directory containing node_modules).

# shellcheck disable=SC1091
_ensure_node_on_path() {
  type -P node >/dev/null 2>&1 && return 0

  export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
  # Silence nvm status lines when sourcing (e.g. "default -> 22" / version hints).
  export NVM_SILENT="${NVM_SILENT:-1}"
  [[ -s "$NVM_DIR/nvm.sh" ]] && . "$NVM_DIR/nvm.sh"
  type -P node >/dev/null 2>&1 && return 0

  [[ -f "$HOME/.asdf/asdf.sh" ]] && . "$HOME/.asdf/asdf.sh"
  type -P node >/dev/null 2>&1 && return 0

  if command -v fnm >/dev/null 2>&1; then
    eval "$(fnm env 2>/dev/null)" || true
  fi
  type -P node >/dev/null 2>&1 && return 0

  export VOLTA_HOME="${VOLTA_HOME:-$HOME/.volta}"
  [[ -d "$VOLTA_HOME/bin" ]] && PATH="$VOLTA_HOME/bin:$PATH"
  type -P node >/dev/null 2>&1 && return 0

  # mise (https://mise.jdx.dev)
  if command -v mise >/dev/null 2>&1; then
    eval "$(mise activate bash 2>/dev/null)" || true
  fi
  type -P node >/dev/null 2>&1 && return 0

  return 0
}

# Prefer a real executable path (avoids "node: command not found" if PATH is odd).
_openclaw_node_bin() {
  local b
  b=$(type -P node 2>/dev/null) || true
  [[ -n "$b" ]] && { echo "$b"; return 0; }
  b=$(type -P nodejs 2>/dev/null) || true
  [[ -n "$b" ]] && { echo "$b"; return 0; }
  for b in /usr/bin/node /usr/local/bin/node; do
    [[ -x "$b" ]] && { echo "$b"; return 0; }
  done
  return 1
}

openclaw_run() {
  local mjs="${ROOT}/node_modules/openclaw/openclaw.mjs"

  if [[ ! -f "$mjs" ]]; then
    command npx openclaw "$@"
    return
  fi

  _ensure_node_on_path

  local node_bin
  if node_bin="$(_openclaw_node_bin)"; then
    "$node_bin" "$mjs" "$@"
    return $?
  fi

  echo "openclaw_run: no usable node/nodejs binary; trying npx…" >&2
  command npx openclaw "$@"
}
