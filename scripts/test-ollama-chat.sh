#!/usr/bin/env bash
# Smoke-test Ollama: list local models and run one non-streaming chat.
#
# Usage:
#   export OLLAMA_MODEL="gemma4:31b"   # exact name from: ollama list
#   bash scripts/test-ollama-chat.sh
#
# Optional:
#   OLLAMA_HOST=http://127.0.0.1:11434
#   OLLAMA_PROMPT="Say hello in exactly five words."
#
# Requires: curl, python3 (for safe JSON). jq optional (prettier output).

set -euo pipefail

OLLAMA_HOST="${OLLAMA_HOST:-http://127.0.0.1:11434}"
OLLAMA_MODEL="${OLLAMA_MODEL:?Set OLLAMA_MODEL (e.g. gemma4:31b — see ollama list)}"
OLLAMA_PROMPT="${OLLAMA_PROMPT:-Say hello in exactly five words.}"
export OLLAMA_MODEL OLLAMA_PROMPT

echo "== Ollama host: $OLLAMA_HOST"
echo "== Model:       $OLLAMA_MODEL"
echo

echo "== GET /api/tags (installed models)"
TAGS_JSON="$(curl -fsS "$OLLAMA_HOST/api/tags")"
if command -v jq >/dev/null 2>&1; then
  echo "$TAGS_JSON" | jq -r '.models[]?.name? // empty'
else
  echo "$TAGS_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); [print(m['name']) for m in d.get('models',[])]"
fi
echo

echo "== POST /api/chat (single turn, stream=false)"
BODY="$(python3 -c '
import json, os
print(json.dumps({
  "model": os.environ["OLLAMA_MODEL"],
  "stream": False,
  "messages": [{"role": "user", "content": os.environ["OLLAMA_PROMPT"]}],
}))
')"
RESP="$(curl -fsS "$OLLAMA_HOST/api/chat" \
  -H "Content-Type: application/json" \
  -d "$BODY")"

if command -v jq >/dev/null 2>&1; then
  echo "$RESP" | jq '{model: .model, message: .message.content, done: .done}'
else
  echo "$RESP" | python3 -m json.tool
fi

echo
echo "OK — Ollama responded. If this failed, run: ollama pull $OLLAMA_MODEL"
