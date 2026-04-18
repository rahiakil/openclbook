# Book pipeline — end-to-end workflow

This document describes the **full path** from an uploaded manuscript to a merged output on disk, using the **web UI** (`book-pipeline ui`) and **local Ollama** (no cloud LLM billing).

## Architecture (short)

1. **Manuscript lab** — upload, chunk, optional Ollama structure analysis, per-chunk notes. State is saved under `WORKSPACE/.pipeline/manuscript_sessions/<uuid>.json` so it survives UI restarts.
2. **Commit to workspace** — writes source files the LangGraph supervisor reads:
   - **draft** mode: `manuscript/draft.md` (single file).
   - **sections** mode: `sections/upload-*.md` (one file per chunk).
   - Always writes `manuscript/PIPELINE_CONTEXT.md` (notes + structure digest for humans / future tooling).
3. **Run full pipeline** — LangGraph supervisor: plan → optional OpenClaw tool → edit each chunk → `outputs/staging_merged.md`. If “merge manuscript session” is checked, **chunk notes + structure digest** from the active session are appended to the supervisor goal.
4. **Approve merge** — copies `outputs/staging_merged.md` → `manuscript/canonical_merged.md`.

## Operator steps (UI)

1. Set **workspace** to your book directory (must contain `config.yaml` or rely on env for Ollama).
2. **Load manuscript** (`.docx`, `.md`, `.txt`, `.html`, `.odt`, `.rtf`).
3. Optional: **Analyze with Ollama**; add **chunk notes** (tone, format, twists, etc.).
4. **Commit to workspace** — choose draft vs sections, then commit.
5. Edit **User goal** / **Preset** as needed; enable **merge manuscript session** if you want lab notes in the run goal.
6. **Run full pipeline**; wait until run status is **done** (poll / refresh).
7. **Approve → canonical_merged.md**.

## After a UI restart

- Open **Recent sessions** (requires workspace) or pass `GET /api/manuscript/session/<id>?workspace=...` from the API.
- If the session list is empty, upload again; old JSON files remain on disk under `.pipeline/manuscript_sessions/`.

## Supervisor input priority

The supervisor **prefers `sections/*.md`** when that directory has markdown files; otherwise it uses **`manuscript/draft.md`**. If you committed **draft** but still have old `sections/*.md`, the pipeline may use sections first — remove or archive those files if you intend a draft-only run.

## CLI (same merge semantics)

```bash
# One-shot supervisor with merged lab session goal
book-pipeline supervisor-run --workspace /path/to/book \
  --goal "Convert to Netflix-style script." \
  --preset netflix_script \
  --manuscript-session <uuid>

# Long run (checkpoint per chunk)
book-pipeline supervisor-marathon --workspace /path/to/book \
  --thread-id my-job-1 \
  --manuscript-session <uuid> \
  --goal "…"
```

Use `--skip-manuscript-notes` to ignore the session file and use only `--goal`.

## Ollama usage log

Each `/api/chat` call from the supervisor (and manuscript analysis when run from the UI) appends one JSON line to:

`WORKSPACE/.pipeline/ollama_usage.jsonl`

Fields include `prompt_eval_count`, `eval_count`, `total_duration_ns`, and `model`.

## OpenClaw (optional)

Gateway URL + token in env or `config.yaml`. The supervisor only calls OpenClaw when enabled in the UI / CLI; it is **not** required for the manuscript → merge flow.
