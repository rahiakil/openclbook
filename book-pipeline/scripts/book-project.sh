#!/usr/bin/env bash
# Wrapper for book-pipeline project workflows (init → plan-gate → resume).
# Run from anywhere; defaults suit project "wizofoz" unless you override env/args.
#
#   ./scripts/book-project.sh init [PROJECT_ID]
#   ./scripts/book-project.sh plan [INPUT_FILE]   # ingest + --plan-gate
#   ./scripts/book-project.sh resume              # --resume-plan + export
#   ./scripts/book-project.sh prep <INPUT>        # --prep-gate (needs supervisor_enable_prep_passes)
#   ./scripts/book-project.sh prep-resume         # --prep-resume after editing human_input_answers.md
#
# Environment (optional):
#   BOOK_PROJECT_ID       default wizofoz
#   BOOK_THREAD_ID        default wizofoz-run1
#   BOOK_INPUT            manuscript source for plan (or pass as arg)
#   BOOK_OUTPUT_FORMAT    md | txt | docx (default txt)
#   BOOK_OUTPUT_NAME      e.g. wizofoz_netflix.txt (becomes wizofoz_netflix_YYYYMMDD_HHMMSS.txt per run)
#   BOOK_EXPORT_STAMP     1 (default) = timestamp in export filename; 0|false = fixed --output-name
#   BOOK_PRESET           default netflix_script
#   BOOK_GOAL             default goal string
#   BOOK_EXTRA            extra CLI args (quoted), appended to python -m book_pipeline
#   If config has supervisor_enable_prep_passes: true, run `prep` before `plan` (same BOOK_THREAD_ID);
#   or one-off: BOOK_EXTRA='--skip-prep-requirement' for plan only.
#   SUPERVISOR_PARALLEL_WORKERS  optional override (default 1 serial); see config supervisor_parallel_workers

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PROJECT_ID="${BOOK_PROJECT_ID:-wizofoz}"
THREAD_ID="${BOOK_THREAD_ID:-wizofoz-run1}"
OUTPUT_FORMAT="${BOOK_OUTPUT_FORMAT:-txt}"
OUTPUT_NAME="${BOOK_OUTPUT_NAME:-wizofoz_netflix.txt}"
PRESET="${BOOK_PRESET:-netflix_script}"
STAMP_FLAG=()
case "${BOOK_EXPORT_STAMP:-1}" in
  0|false|False|no|NO) STAMP_FLAG=(--no-export-stamp) ;;
esac
GOAL="${BOOK_GOAL:-Rewrite as a tight streaming series script with slug lines and NAME caps.}"
INPUT="${BOOK_INPUT:-}"

py() {
  # shellcheck disable=2086
  python -m book_pipeline "$@" ${BOOK_EXTRA:-}
}

cmd="${1:-}"
shift || true

case "$cmd" in
  init)
    pid="${1:-$PROJECT_ID}"
    echo "==> init-project $pid"
    py init-project "$pid"
    echo "Config: $ROOT/projects/$pid/config.yaml"
    ;;
  plan)
    inp="${1:-$INPUT}"
    if [[ -z "$inp" ]]; then
      echo "Usage: $0 plan <INPUT_FILE>" >&2
      echo "Or set BOOK_INPUT to the .txt/.md/.docx path." >&2
      exit 1
    fi
    inp="$(realpath "$inp")"
    echo "==> ingest-run --project-id $PROJECT_ID --plan-gate (thread $THREAD_ID)"
    py ingest-run \
      --project-id "$PROJECT_ID" \
      --input "$inp" \
      --plan-gate \
      --output-format "$OUTPUT_FORMAT" \
      "${STAMP_FLAG[@]}" \
      --output-name "$OUTPUT_NAME" \
      --preset "$PRESET" \
      --goal "$GOAL" \
      --archive-sections \
      --thread-id "$THREAD_ID" \
      --stream
    echo ""
    echo "Edit: $ROOT/projects/$PROJECT_ID/outputs/plan_for_review.md"
    echo "Then: BOOK_THREAD_ID=$THREAD_ID $0 resume"
    ;;
  resume)
    echo "==> ingest-run --project-id $PROJECT_ID --resume-plan (thread $THREAD_ID)"
    py ingest-run \
      --project-id "$PROJECT_ID" \
      --resume-plan \
      --thread-id "$THREAD_ID" \
      --output-format "$OUTPUT_FORMAT" \
      "${STAMP_FLAG[@]}" \
      --output-name "$OUTPUT_NAME" \
      --stream
    ;;
  prep)
    inp="${1:-$INPUT}"
    if [[ -z "$inp" ]]; then
      echo "Usage: $0 prep <INPUT_FILE>  (requires supervisor_enable_prep_passes: true in project config)" >&2
      exit 1
    fi
    inp="$(realpath "$inp")"
    echo "==> ingest-run --prep-gate (thread $THREAD_ID)"
    py ingest-run \
      --project-id "$PROJECT_ID" \
      --input "$inp" \
      --prep-gate \
      --output-format "$OUTPUT_FORMAT" \
      "${STAMP_FLAG[@]}" \
      --output-name "$OUTPUT_NAME" \
      --preset "$PRESET" \
      --goal "$GOAL" \
      --archive-sections \
      --thread-id "$THREAD_ID" \
      --stream
    echo ""
    echo "Edit: $ROOT/projects/$PROJECT_ID/outputs/human_input_answers.md"
    echo "Then: BOOK_THREAD_ID=$THREAD_ID $0 prep-resume"
    ;;
  prep-resume)
    echo "==> ingest-run --prep-resume (thread $THREAD_ID)"
    py ingest-run \
      --project-id "$PROJECT_ID" \
      --prep-resume \
      --thread-id "$THREAD_ID" \
      --output-format "$OUTPUT_FORMAT" \
      "${STAMP_FLAG[@]}" \
      --output-name "$OUTPUT_NAME" \
      --stream
    ;;
  ""|-h|--help|help)
    cat <<EOF
Usage: $(basename "$0") <command> [args]

Commands:
  init [PROJECT_ID]     Create/update projects/<id>/ (default: \$BOOK_PROJECT_ID or wizofoz)
  plan <INPUT_FILE>     Import + supervisor through plan only (--plan-gate)
  resume                Continue from edited plan; export to outputs/
  prep <INPUT_FILE>     Prep gate: strategy + human Q&A files (config: supervisor_enable_prep_passes)
  prep-resume           After editing human_input_answers.md: memory passes + full supervisor + export

Defaults: BOOK_PROJECT_ID=$PROJECT_ID  BOOK_THREAD_ID=$THREAD_ID
Set BOOK_INPUT, BOOK_GOAL, BOOK_PRESET, BOOK_OUTPUT_*, BOOK_EXTRA as needed.
EOF
    ;;
  *)
    echo "Unknown command: $cmd (try: init | plan | resume | prep | prep-resume | help)" >&2
    exit 1
    ;;
esac
