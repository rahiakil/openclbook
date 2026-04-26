# Frontend options (book supervisor)

## `goal_preset` (`--preset` / `RunBody.goal_preset`)

Values implemented in the supervisor (`_PRESETS` in `book_pipeline/supervisor/nodes.py`):

| Value | Use case |
|-------|----------|
| `rewrite` | Line edit / clarity; keep plot |
| `netflix_script` | Streaming series screenplay (slug lines, NAME caps) |
| `korean_drama_script` | KDrama-style pacing + ensemble emotion (script in English unless goal says otherwise) |
| `feature_film` | Feature film screenplay |
| `tv_episodic_arcs` | Episodic engine + arc / bible style notes + pilot-friendly beats |
| `translation_adapt` | Translate + culturally adapt |
| `stage_play` | Stage play (acts/scenes) |
| `docs` | Long-form technical documentation |

**Custom asks** (period change, transgender character arc, reduce length, etc.) should still go in **`user_goal`** plus optional **`user_statements_json`** array for verifier-aligned hard requirements.

## `POST /api/supervisor/run` (`RunBody` in `book_pipeline/ui_app.py`)

| Field | Type | Notes |
|-------|------|--------|
| `workspace` | string | Project directory |
| `llm_provider` | `ollama` \| `anthropic` \| null | Overrides `config.yaml` |
| `thread_id` | string? | Stable id for resume / checkpoints |
| `user_goal` | string | Main creative brief |
| `goal_preset` | string | See table above |
| `use_openclaw_after_plan` | bool | After plan node |
| `openclaw_tool` | string | Gateway tool name |
| `openclaw_args_json` | string | JSON string |
| `manuscript_session_id` | string? | Lab session merge |
| `include_manuscript_notes` | bool | Merge session notes into goal |
| `user_statements_json` | string | JSON array of requirement strings |
| `use_semantic_division` | bool | LLM chapter split before plan |
| `openclaw_per_chunk` | bool | Tool per chunk edit |
| `max_revision_rounds` | int 0–8 | Verify loop cap |

CLI-only ingest flags (not on `RunBody` today): `--plan-gate`, `--prep-gate`, `--prep-resume`, `--auto-prep`, `--skip-prep-requirement`, `--archive-sections`, `--output-format`, `--output-name`, `--no-export-stamp`, `--stream`, `--verbose`.

## Workspace `config.yaml` (common knobs)

| Key | Purpose |
|-----|---------|
| `llm_provider` | `ollama` or `anthropic` |
| `ollama_model` / `ollama_num_ctx` / `ollama_http_timeout_seconds` | Local model + context + timeout |
| `supervisor_parallel_workers` | Chunk edit concurrency (1 = one GPU job at a time) |
| `supervisor_verify_passes` | Independent verifier passes |
| `supervisor_max_chunk_chars` | Chunk size hint |
| `supervisor_edit_context_budget_chars` | Prompt budget → may resplit chunks |
| `supervisor_memory_context` | `digest` \| `full` \| `none` |
| `supervisor_enable_prep_passes` | Enables prep / plan prerequisite behavior |

See `docs/contracts/manuscript_job_v1.schema.json` for a **portable JSON contract** you can POST alongside uploads to S3 / your gateway.
