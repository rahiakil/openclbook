# Ingest → LangGraph supervisor → outputs (and roadmap)

This document describes **what the book-pipeline does today** when you supply **`.txt` / `.md` / `.docx`**, how **LangGraph** is wired, and how that compares to a **richer “story bible + human scope + targeted rewrite”** workflow you might want next.

---

## 1. Big picture: ingest is not LangGraph

Import runs **before** the supervisor graph. It is plain Python I/O + optional section archival.

```
  USER FILE (.txt | .md | .docx)
           |
           v
  +---------------------------+
  |  read_document()          |  <- python-docx / text decode
  |  strip PG boilerplate     |  <- optional Gutenberg cleanup
  +---------------------------+
           |
           v
  +---------------------------+
  |  manuscript/draft.md      |  <- single canonical markdown body
  |  .pipeline/ingest_job.json|
  +---------------------------+
           |
           |   (optional) archive old sections/*.md -> .pipeline/archived_sections_*/
           v
  sections/*.md   OR   draft-only path
      (if present)     (supervisor can split draft.md in init)
```

**There is no separate “plan to convert to MD” LLM step** for ingest: conversion is **deterministic** (`format_bridge.import_source_to_draft`). The **first LLM “plan”** is the supervisor’s **`node_ollama_plan`** (rewrite strategy JSON + prose), not “convert file format”.

---

## 2. LangGraph supervisor (full run, no plan-gate split)

Compiled in `book_pipeline/supervisor/graph_build.py` as `build_supervisor_app`.

```
                              START
                                |
                                v
                         +-------------+
                         |    init     |  manifest: sections/*.md OR draft.md -> chunk records
                         +-------------+
                                | (ok)
                                v
                      +-------------------+
                      |   divide_work     |  LLM + Python: chapter / size splits -> chunks[]
                      +-------------------+
                                |
                                v
                      +-------------------+
                      |       plan        |  LLM: rewrite plan JSON + narrative plan
                      +-------------------+
                                |
                                v
                      +-------------------+
                      |  balance_context  |  may resplit chunks to fit edit context budget
                      +-------------------+
                                |
                                v
                      +-------------------+
                      |     openclaw      |  optional HTTP tool batch (often no-op)
                      +-------------------+
                                |
              +-----------------+------------------+
              | marathon                          | default (parallel)
              v                                     v
       +-------------+                      +-------------------+
       | edit_chunk  |  sequential          |  edit_parallel    |  N x LLM (thread pool)
       | (interrupt) |  1 chunk / step      +-------------------+
       +-------------+                                |
              |                                         |
              +-----------------+-----------------------+
                                v
                      +-------------------+
                      |     staging       |  writes outputs/staging_*.md per chunk + merged
                      +-------------------+
                                |
                                v
                      +-------------------+
                      |      verify       |  LLM mission checks (multi-pass)
                      +-------------------+
                                |
                                v
                      +-------------------+
                      | persist_learnings |  append run notes
                      +-------------------+
                                |
                    +-----------+------------+
                    | PASS / max revisions   |
                    v                          v
                  END                  +-------------------+
                                       | prepare_revision  |  feedback -> back to plan
                                       +-------------------+
                                                 |
                                                 v
                                               plan
                                               (loop)
```

**Chunking timeline:** chunks exist after **`init`** (from files) and may change after **`divide_work`** and again after **`balance_context`**. There is **no dedicated node** named “find all characters” or “generate story images with Ollama” in this graph.

**What *does* feed every chunk edit today**

- **USER_GOAL** / preset / optional **user statements** (structured or free text in config/CLI).
- **Plan excerpt** (from the latest plan).
- **Editor memory bundle** (markdown under workspace **`.memory/`** — characters, research, etc., if you maintain those files).
- The **chunk text** for that file path.

So “summarize the story and keep it in context for every paragraph” is **partially** approximated by **plan + memory**, not a separate rolling synopsis node.

---

## 3. Plan-gate workflow (human edits the plan before execute)

Used by `ingest-run --plan-gate` then `ingest-run --resume-plan` with the **same `--thread-id`**.

### Phase A — plan only (separate small graph)

```
START -> init -> divide_work -> plan -> END
                |
                +--> outputs/plan_for_review.md
                +--> .pipeline/plan_gate_<thread>.json
```

Human edits **`outputs/plan_for_review.md`** (YAML front matter, goal blocks, etc.).

### Phase B — execute tail (checkpointed graph)

```
START -> balance_context -> openclaw -> edit_parallel -> staging -> verify
              -> persist_learnings --+--> END
              |                     +--> prepare_revision -> plan -> balance_context ...
```

Same logical tail as the full graph, but **starts after** your edited plan is merged back into state (`plan_gate.py`).

---

## 4. Answers to specific “does it…” questions

| Question | Today |
|----------|--------|
| Plan to convert input to Markdown first? | **No LLM**; `import_source_to_draft` writes **`manuscript/draft.md`** directly. |
| Then chunk? | **`init`** builds chunks from **`sections/*.md`** or **`draft.md`**; **`divide_work`** / **`balance_context`** may refine them. |
| “First pass” where characters are discovered? | **No automatic character-discovery pass.** You can maintain **`.memory/characters/*.md`** (and similar) yourself; the bundle is **loaded** for edits. |
| Generate an image with Ollama? | **Not in core supervisor nodes.** Optional **OpenClaw** HTTP tools or your own tooling could; the pipeline does not call Ollama vision/image by default for book pages. |
| Story evolution summarized every paragraph? | **No dedicated rolling summary artifact.** **Plan + memory** are injected into each chunk edit prompt. |
| Ask human what must change after first pass? | **Plan gate = human on the *plan*** before big edits. There is **no built-in “review staging then pick chapters to redo”** UI; verify loop retries on failed checks, not selective human paragraph picking. |
| Simple NL instruction (“rewrite chapter 8 only”) | Supported as **user goal / statements** driving **`divide_work` + `plan` + edits**; use **new thread** or **edit plan + resume** depending on whether you want a fresh plan or a tweak. |

---

## 5. Target roadmap (what you described, not all implemented)

If you want the **full narrative**:

1. **Ingest** (already) → canonical `draft.md`.
2. **Structure pass** (already partly `divide_work`) → stable chapter/chunk IDs.
3. **Story bible LLM** (new) → characters, arcs, tone, open threads → **`outputs/story_bible.md`** checked into context every edit.
4. **Optional media pass** (new) → image briefs / assets; wire to Ollama image or external tool (not core today).
5. **First-pass draft** (already) → `staging` / merged outputs.
6. **Human scope pass** (new) → e.g. `outputs/rewrite_scope.md`: “chapter 8 shorter; add character X; remove legal boilerplate…”
7. **Re-onboard rewrite** (orchestration) → merge scope into state → rerun **tail** or only affected chunks (new routing).

**Hook points in current code:** `node_ollama_plan`, `node_divide_work`, `_edit_chunk_core` user prompt assembly in `supervisor/nodes.py`, and `plan_gate` bundle for human-edited fields.

---

## 6. Re-onboarding a rewrite **today** (practical)

**A. Small goal change, same overall plan thread**

1. Edit **`outputs/plan_for_review.md`** (front matter / `<<<USER_GOAL>>>` blocks as documented in your workflow).
2. Run **`ingest-run --resume-plan --thread-id <same>`** (or `./scripts/book-project.sh resume` with `BOOK_THREAD_ID`).

**B. Large new direction (fresh plan)**

1. New **`--thread-id`** (or new project workspace).
2. Run **`ingest-run --plan-gate …`** again so **`plan`** and **`divide_work`** re-run from scratch.

**C. Natural-language examples** (put in goal / manuscript session / plan file)

- “Rewrite **chapter 8** only; keep voice; cut 30%.”
- “Reduce length of **Part II**; preserve plot beats.”
- “Add character **Mara** in chapters 3–5; foreshadow in chapter 2.”
- “Put **the Tin Woodman** through a loyalty challenge in chapter 6.”

Those affect **`divide_work`** and **`plan`** first; if chunks do not change as you expect, prefer a **new thread** or explicitly edit the plan’s structural JSON.

---

## 7. Files worth inspecting when debugging

| Path | Role |
|------|------|
| `manuscript/draft.md` | Post-ingest canonical source |
| `sections/*.md` | Optional multi-file source (overrides draft-only path in init) |
| `outputs/plan_for_review.md` | Human-editable plan (plan-gate) |
| `.pipeline/plan_gate_<thread>.json` | Bundle for resume |
| `outputs/staging_merged.md` | Merged chunk proposals (errors show per chunk) |
| `.pipeline/ollama_usage.jsonl` | Per-call token/latency log |
| `.pipeline/project_metrics_summary.json` | Rolled-up metrics |

---

## 8. ASCII: one-line lifecycle (cheat sheet)

```
docx/txt --> draft.md --> [LangGraph: init --> divide --> plan --> balance --> edit* --> staging --> verify --> learnings] --> staging_merged / export
                     ^                                              |
                     +----------- plan-gate human edits plan --------+
```

---

## 8b. Prep gate (strategy + human answers + two memory passes)

When **`supervisor_enable_prep_passes: true`** in `config.yaml`:

1. **`ingest-run --prep-gate --input … --thread-id TID`** — runs **`init`** → **`prep_strategic_plan`** (LLM writes how to solve the job + optional `## Human questions`). Writes:
   - `outputs/supervisor_prep_strategy.md`
   - `outputs/human_input_request.md` (instructions + questions)
   - `outputs/human_input_answers.md` (stub — **you paste answers here**)
   - `.pipeline/prep_gate_<TID>.json` (bundle for resume)

2. Edit **`outputs/human_input_answers.md`**, then **`ingest-run --prep-resume --thread-id TID`** (no `--input`). Runs:
   - **`prep_character_pass`** → `.memory/characters/pipeline_pass1_characters.md` (summarized roster)
   - **`prep_arc_pass`** → `.memory/research/pipeline_pass2_story_arc.md` (growth + arc summary)
   - Then the **normal supervisor tail** (`divide_work` → `plan` → … → export).

**One-shot variant:** **`ingest-run --auto-prep --input … --thread-id TID`** runs step 1, auto-fills `human_input_answers.md` with `NONE`, then runs step 2 + the full supervisor and export (no human edit loop).

With prep passes **enabled**, **`--plan-gate` is blocked** until prep phase one has finished for the **same `--thread-id`** (checks `outputs/supervisor_prep_strategy.md` and `.pipeline/prep_gate_<TID>.json`). Use **`--skip-prep-requirement`** only as an escape hatch. **`--resume-plan`** is not gated (you already passed the plan checkpoint). If prep is **disabled** in config, plan-gate behaves as before.

## 9a. Live chunk staging files

After **each** chunk edit (serial or parallel), the pipeline writes:

`outputs/staging_chunks/NNN__<sanitized-chunk-path>.md`

with a small YAML front matter (`chunk_index`, `path`, `status`, `ts`) and the **proposed** body (or `(error: …)`).  
You can `watch ls -lt outputs/staging_chunks` or open the latest file in an editor while a run is in progress. The final merged file **`outputs/staging_merged.md`** is still written at the **`staging`** node when all edits in that pass are done.

## 9b. Memory in prompts (`supervisor_memory_context`)

| Value | Behavior |
|-------|----------|
| **`digest`** (default) | Truncated digest of `.memory/` (project learnings first, then per-file caps). Smaller prompts than dumping every character sheet. |
| **`full`** | Previous behavior: entire `load_editor_memory_bundle` text. |
| **`none`** | No memory text; rely on USER_GOAL / statements / plan only. |

Tune with `supervisor_memory_digest_max_chars` and `supervisor_memory_digest_per_file` in `config.yaml`.

## 9. Chunk edit parallelism vs local Ollama (contention)

**Default is now serial (`supervisor_parallel_workers: 1`).** Each chunk edit is one blocking `/api/chat` to Ollama. Running many in parallel (`ThreadPoolExecutor`) often hurts on **one GPU**:

- VRAM is shared; multiple **large `num_ctx`** loads compete and each request slows down.
- You hit **httpx read timeouts** more often (each call stays in flight longer).
- Ollama typically **queues** extra work anyway, so wall-clock may not improve.

**When to raise workers:** multiple GPUs, small context, remote high-throughput endpoint, or you have measured that 2–4 improves throughput.

**Override without editing YAML:** `SUPERVISOR_PARALLEL_WORKERS=2`.

**Fully sequential human-in-the-loop alternative:** use the **marathon** supervisor graph (`--marathon` / guided step), which edits **one chunk per graph step** (checkpoint between chunks), not the same as `edit_parallel` with `workers=1` but similar “no overlap” behavior with interrupts.

For questions or changes to **this document**, edit `docs/INGEST_LANGGRAPH_AND_HUMAN_LOOP.md` in the repo.
