from __future__ import annotations

import json
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from book_pipeline.config import load_settings
from book_pipeline.manuscript_lab import chunk_manuscript
from book_pipeline.gutenberg import strip_project_gutenberg_boilerplate
from book_pipeline.llm_stats_emit import stderr_llm_stats_enabled
from book_pipeline.learnings import append_supervisor_run_learnings
from book_pipeline.memory import load_supervisor_memory_context
from book_pipeline.llm_complete import complete_chat
from book_pipeline.openclaw_tools import invoke_openclaw_tool
from book_pipeline.supervisor.log_viz import (
    chunk_inventory_ascii,
    divide_result_ascii,
    edit_batch_ascii,
    openclaw_ascii,
    plan_excerpt_for_log,
    prepare_revision_ascii,
    run_intro_ascii,
    staging_banner,
    thinking_excerpt_for_log,
    verify_ascii,
)
from book_pipeline.supervisor.context_split import rebalance_chunks_for_context_budget
from book_pipeline.supervisor.orchestration import (
    coalesce_size_split_parts,
    extract_json_object,
    split_manuscript_into_chapters,
)
from book_pipeline.supervisor.state import ChunkRecord, SupervisorState, log_append


def _super_chat(
    settings,
    workspace: Path,
    *,
    messages: list[dict[str, str]],
    temperature: float,
    tag: str,
    llm_provider_override: str | None,
):
    from book_pipeline.llm_complete import complete_chat

    return complete_chat(
        settings,
        llm_provider_override=llm_provider_override,
        messages=messages,
        temperature=temperature,
        workspace=workspace,
        tag=tag,
        ollama_num_ctx=settings.ollama_num_ctx,
    )


def _ws(state: SupervisorState) -> Path:
    return Path(state["workspace"]).resolve()


_SCENE_NEEDS_MAX_MEMORY_CHARS = 12_000
_SCENE_NEEDS_MAX_PLAN_CHARS = 48_000
_IMAGE_PROMPT_VERIFY_MAX_PLAN_CHARS = 24_000
_FINAL_REVIEW_MAX_MANUSCRIPT_CHARS = 120_000


def _truncate_prompt_block(text: str, max_chars: int) -> tuple[str, bool]:
    """Return (text, was_truncated) for oversized user prompts."""
    t = text or ""
    if len(t) <= max_chars:
        return t, False
    head = max(1, max_chars // 2)
    tail = max(1, max_chars - head - 80)
    return (
        t[:head] + "\n\n… [truncated for LLM prompt size] …\n\n" + t[-tail:],
        True,
    )


def _is_llm_timeout_error(exc: BaseException) -> bool:
    low = str(exc).lower()
    if "timed out" in low or "readtimeout" in low or "timeout" in low:
        return True
    if isinstance(exc, TimeoutError):
        return True
    try:
        import httpx

        if isinstance(exc, (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
            return True
    except Exception:
        pass
    return False


def _write_staging_chunk_snapshot(ws: Path, settings, idx: int, c: ChunkRecord) -> None:
    """Write ``outputs/staging_chunks/NNN__path.md`` after each chunk edit (live progress)."""
    try:
        out_dir = ws / settings.outputs_dir / "staging_chunks"
        out_dir.mkdir(parents=True, exist_ok=True)
        raw_path = str(c.get("path") or "chunk").replace("\\", "/")
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in raw_path)[:160]
        name = f"{idx + 1:03d}__{safe}.md"
        prop = c.get("proposed")
        body = prop if isinstance(prop, str) else ""
        status = str(c.get("status") or "")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        header = (
            f"---\nchunk_index: {idx + 1}\npath: {raw_path}\nstatus: {status}\nts: {stamp}\n---\n\n"
        )
        tail = "" if body.endswith("\n") else ("\n" if body else "")
        (out_dir / name).write_text(header + body + tail, encoding="utf-8")
    except OSError:
        pass


def _openclaw_chunk_side_effect(
    ws: Path,
    settings,
    chunk: ChunkRecord,
    tool: str,
    raw_args: str,
) -> str:
    if not tool or not settings.openclaw_gateway_url or not settings.openclaw_gateway_token:
        return "openclaw chunk: skipped (no tool or gateway)"
    try:
        args = json.loads((raw_args or "{}").strip() or "{}")
        if not isinstance(args, dict):
            args = {}
    except json.JSONDecodeError:
        args = {}
    path = str(chunk.get("path") or "")
    excerpt = chunk.get("proposed") or chunk.get("original") or ""
    args.setdefault("chunk_path", path)
    args.setdefault("excerpt", excerpt)
    args.setdefault("workspace", str(ws))
    try:
        invoke_openclaw_tool(
            settings.openclaw_gateway_url,
            settings.openclaw_gateway_token,
            tool,
            args,
        )
        return f"openclaw chunk: {tool} ok for {path}"
    except Exception as e:  # noqa: BLE001
        return f"openclaw chunk: {tool} failed ({path}): {e}"


def node_init_manifest(state: SupervisorState) -> SupervisorState:
    ws = _ws(state)
    settings = load_settings(ws, ws / "config.yaml")
    sec_dir = ws / settings.sections_dir
    chunks: list[ChunkRecord] = []
    n_section_files = 0
    if sec_dir.is_dir():
        md_files = sorted(sec_dir.glob("*.md"))
        n_section_files = len(md_files)
        for p in md_files:
            rel = p.relative_to(ws)
            try:
                text = strip_project_gutenberg_boilerplate(
                    p.read_text(encoding="utf-8", errors="replace")
                )
            except OSError as e:
                chunks.append(
                    {
                        "id": str(uuid.uuid4())[:8],
                        "path": str(rel).replace("\\", "/"),
                        "original": "",
                        "proposed": "",
                        "status": "error",
                    }
                )
                continue
            chunks.append(
                {
                    "id": str(uuid.uuid4())[:8],
                    "path": str(rel).replace("\\", "/"),
                    "original": text,
                    "proposed": "",
                    "status": "pending",
                }
            )

    if not chunks:
        draft = ws / settings.manuscript_dir / "draft.md"
        if draft.is_file():
            text = strip_project_gutenberg_boilerplate(
                draft.read_text(encoding="utf-8", errors="replace")
            )
            chunks.append(
                {
                    "id": "whole",
                    "path": f"{settings.manuscript_dir}/draft.md",
                    "original": text,
                    "proposed": "",
                    "status": "pending",
                }
            )

    if not chunks:
        return {
            **state,
            "chunks": [],
            "chunk_index": 0,
            "from_multi_file_sections": False,
            "error": "No sections/*.md and no manuscript draft — add content first.",
            "log": log_append(state, "init: no chunks"),
        }

    max_chars = int(settings.supervisor_max_chunk_chars)
    expanded: list[ChunkRecord] = []
    split_count = 0
    for c in chunks:
        original = c.get("original") or ""
        base_path = str(c.get("path") or "chunk").replace("\\", "/")
        if len(original) <= max_chars:
            expanded.append(c)
            continue
        parts = chunk_manuscript(original, target_chars=max_chars)
        split_count += 1
        for p in parts:
            pid = str(uuid.uuid4())[:8]
            n = int(p["id"]) + 1
            expanded.append(
                {
                    "id": pid,
                    "path": f"{base_path}#part-{n:03d}",
                    "original": str(p.get("text") or ""),
                    "proposed": "",
                    "status": "pending",
                }
            )

    multi_file = n_section_files >= 2
    log_msg = f"init: {len(expanded)} chunk(s)"
    if split_count:
        log_msg += f" (split {split_count} oversized file(s) ≤{max_chars} chars for Ollama passes)"
    if multi_file:
        log_msg += " (multiple section files — semantic division skipped later)"

    intro = run_intro_ascii(
        mode=state.get("supervisor_run_mode") or "full",
        preset=(state.get("goal_preset") or "rewrite").strip(),
        goal_preview=(state.get("user_goal") or "").strip(),
        n_statements=len(state.get("user_statements") or []),
        max_revision_rounds=int(state.get("max_revision_rounds") or 0),
        semantic_on=bool(state.get("use_semantic_division", True)),
        openclaw_after_plan=bool(state.get("use_openclaw_after_plan")),
        openclaw_per_chunk=bool(state.get("openclaw_per_chunk")),
    )
    inv = chunk_inventory_ascii(expanded)
    base = list(state.get("log") or [])
    return {
        **state,
        "chunks": expanded,
        "chunk_index": 0,
        "from_multi_file_sections": multi_file,
        "error": "",
        "log": base + [intro, inv, log_msg],
    }


def node_divide_work(state: SupervisorState) -> SupervisorState:
    """LLM proposes chapter count; split single-chunk manuscripts for downstream plan/edit."""
    if state.get("error"):
        return state
    if not state.get("use_semantic_division", True):
        skip = "divide_work: skipped (semantic division disabled in run options)"
        return {**state, "log": log_append(state, skip)}
    if state.get("from_multi_file_sections"):
        skip = "divide_work: skipped — multiple section files already define chapters"
        return {**state, "log": log_append(state, skip)}
    chunks = list(state.get("chunks") or [])
    coalesced = coalesce_size_split_parts(chunks)
    if len(chunks) == 1:
        full_text = (chunks[0].get("original") or "").strip()
        base_path = str(chunks[0].get("path") or "manuscript/draft.md").replace("\\", "/")
    elif coalesced:
        full_text, base_path = coalesced
        full_text = full_text.strip()
        state = {
            **state,
            "log": log_append(
                state,
                f"divide_work: coalesced {len(chunks)} size-split parts from {base_path} for chapter division",
            ),
        }
    else:
        skip = (
            f"divide_work: skipped ({len(chunks)} chunks) — need one draft chunk or "
            "only ``…#part-NNN`` slices from the same file (see init split)."
        )
        return {**state, "log": log_append(state, skip)}
    if len(full_text) < 2500:
        return {**state, "log": log_append(state, "divide_work: skipped (manuscript < 2500 chars)")}

    ws = _ws(state)
    settings = load_settings(ws, ws / "config.yaml")
    goal = (state.get("user_goal") or "").strip()
    preset = (state.get("goal_preset") or "rewrite").strip()
    sample_budget = settings.divide_llm_sample_chars()
    if len(full_text) <= sample_budget:
        sample_body = full_text
        tail = ""
    else:
        head_n = max(4000, int(sample_budget * 0.55))
        tail_n = max(2000, int(sample_budget * 0.35))
        if head_n + tail_n >= len(full_text) - 100:
            sample_body = full_text
            tail = ""
        else:
            head_n = min(head_n, len(full_text) - tail_n - 100)
            sample_body = full_text[:head_n]
            tail = full_text[-tail_n:]
    sys = (
        "You divide a book-length manuscript into chapters for downstream parallel editing.\n"
        "Output ONLY a JSON object, no markdown fences, no commentary:\n"
        '{"n_chapters": <integer 3-24>, "titles": [<string>, ...]}\n'
        "titles length must match n_chapters. Choose n_chapters from length and narrative scope."
    )
    omitted = max(0, len(full_text) - len(sample_body) - len(tail))
    mid_note = (
        f"\n\n...[middle {omitted} chars omitted for division LLM sample; Python uses the FULL manuscript for splitting]...\n\n"
        if omitted > 0 and tail
        else ""
    )
    user = (
        f"TRANSFORMATION_PRESET: {preset}\nUSER_GOAL:\n{goal}\n\n"
        f"CHARS_TOTAL: {len(full_text)}\nMANUSCRIPT_SAMPLE:\n{sample_body}\n"
        + (f"{mid_note}END_SAMPLE:\n{tail}\n" if tail else "\n")
    )
    state = {**state, "log": log_append(state, "divide_work: calling LLM for n_chapters + titles (JSON)…")}
    try:
        prov = (state.get("llm_provider") or "").strip() or None
        raw, thinking, meta = _super_chat(
            settings,
            ws,
            messages=[{"role": "system", "content": sys}, {"role": "user", "content": user}],
            temperature=0.2,
            tag="supervisor-divide-work",
            llm_provider_override=prov,
        )
    except Exception as e:  # noqa: BLE001
        return {
            **state,
            "log": log_append(state, f"divide_work: LLM failed — keeping single-chunk layout ({e})"),
        }

    obj = extract_json_object(raw) or {}
    n_ch = int(obj.get("n_chapters") or 6)
    titles = obj.get("titles")
    tlist = [str(x).strip() for x in titles] if isinstance(titles, list) else []
    new_chunks = split_manuscript_into_chapters(full_text, n_ch, tlist, base_path)
    trace = list(state.get("thinking_trace") or [])
    if thinking:
        trace.append("## Division of work — thinking\n\n" + thinking.strip())
    trace.append(f"## Division of work — meta\n\n`{meta}`")

    blocks = [
        divide_result_ascii(new_chunks),
        thinking_excerpt_for_log("division-of-work", thinking or ""),
        f"divide_work: split into {len(new_chunks)} chapter chunk(s); n_chapters requested ≈ {n_ch}",
    ]
    base = list(state.get("log") or [])
    return {
        **state,
        "chunks": new_chunks,
        "chunk_index": 0,
        "thinking_trace": trace,
        "log": base + blocks,
    }


def node_ollama_plan(state: SupervisorState) -> SupervisorState:
    if state.get("error"):
        return state
    ws = _ws(state)
    settings = load_settings(ws, ws / "config.yaml")
    memory = load_supervisor_memory_context(settings)
    goal = (state.get("user_goal") or "").strip() or "Improve clarity and pacing."
    preset = (state.get("goal_preset") or "rewrite").strip()
    statements = list(state.get("user_statements") or [])
    stmt_block = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(statements)) if statements else "(none)"
    feedback = (state.get("orchestration_feedback") or "").strip()
    fb_block = f"\n\nPRIOR_VERIFIER_FEEDBACK (address in plan):\n{feedback}\n" if feedback else ""
    summary_lines = [f"- {c['path']} ({len(c.get('original', ''))} chars)" for c in state.get("chunks", [])]
    cap = 120
    summary = "\n".join(summary_lines[:cap])
    if len(summary_lines) > cap:
        summary += f"\n… and {len(summary_lines) - cap} more (total {len(summary_lines)} chunks)"

    sys = (
        "You are the orchestration planner for a book pipeline. Output ONLY Markdown.\n"
        "Include: ## Overview, ## Per-chunk notes (bullet list by path), ## Risks, ## Merge strategy, ## Verification strategy.\n"
        "Map USER_STATEMENTS to concrete chunk-level work. Do not output JSON."
    )
    user = (
        f"TRANSFORMATION_PRESET: {preset}\n\nUSER_GOAL:\n{goal}\n\n"
        f"USER_STATEMENTS (must all be satisfied in final merged output):\n{stmt_block}\n"
        f"{fb_block}\n"
        f"CHARACTER_AND_RESEARCH_MEMORY:\n{memory or '(none)'}\n\n"
        f"CHUNKS:\n{summary}\n"
    )
    try:
        prov = (state.get("llm_provider") or "").strip() or None
        plan, thinking, meta = _super_chat(
            settings,
            ws,
            messages=[{"role": "system", "content": sys}, {"role": "user", "content": user}],
            temperature=0.25,
            tag="supervisor-plan",
            llm_provider_override=prov,
        )
    except Exception as e:  # noqa: BLE001
        return {**state, "error": str(e), "log": log_append(state, f"plan failed: {e}")}

    trace = list(state.get("thinking_trace") or [])
    if thinking:
        trace.append("## Plan — model reasoning (thinking)\n\n" + thinking.strip())
    trace.append(
        "## Plan — provider\n\n"
        + f"- backend: {meta.get('provider', '?')}\n"
        + f"- usage: `{meta}`\n"
    )

    blocks = [
        plan_excerpt_for_log(plan),
        thinking_excerpt_for_log("plan", thinking or ""),
        f"plan: {meta.get('provider', 'llm')} ok — full markdown also in Plan panel / plan_markdown",
    ]
    base = list(state.get("log") or [])
    return {
        **state,
        "plan_markdown": plan,
        "plan_thinking": thinking.strip(),
        "thinking_trace": trace,
        "log": base + blocks,
    }


def _write_outputs_text(ws: Path, rel_path: str, text: str) -> str:
    """Write under outputs/ (workspace-relative path), return normalized rel path."""
    rp = rel_path.replace("\\", "/").lstrip("/")
    p = (ws / rp).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    return str(p.relative_to(ws)).replace("\\", "/")


def _write_pipeline_json(ws: Path, filename: str, obj: object) -> str:
    p = (ws / ".pipeline" / filename).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(p.relative_to(ws)).replace("\\", "/")


def _scene_needs_degraded_state(
    state: SupervisorState,
    ws: Path,
    *,
    meta_note: str,
    log_line: str,
) -> SupervisorState:
    """Persist empty scene-needs artifacts without setting ``error`` (e.g. after LLM read timeout)."""
    obj: dict[str, object] = {"moments": [], "notes": meta_note}
    scene_md = (
        "# Scene needs (post-plan)\n\n"
        "_Degraded run: scene-needs LLM call failed (often HTTP read timeout vs Ollama). "
        "Increase `ollama_http_timeout_seconds` in config or use a faster model; then re-run if needed._\n\n"
        f"**Note:** {meta_note}\n"
    )
    out_md = _write_outputs_text(ws, "outputs/scene_needs.md", scene_md)
    out_json = _write_pipeline_json(ws, "scene_needs.json", obj)
    manifest: dict[str, object] = {"moments": []}
    out_manifest = _write_pipeline_json(ws, "scene_needs_manifest.json", manifest)
    base = list(state.get("log") or [])
    trace = list(state.get("thinking_trace") or [])
    trace.append("## Scene needs — degraded\n\n" + meta_note + "\n")
    return {
        **state,
        "scene_needs_markdown": scene_md,
        "scene_needs_json": [],
        "thinking_trace": trace,
        "log": base + [f"{log_line}; wrote {out_md}, {out_json}, {out_manifest}"],
    }


def node_scene_needs_pass(state: SupervisorState) -> SupervisorState:
    """
    Post-plan pass: identify high-impact moments needing clearer staging / scene replacement
    and visual references (storyboard-grade pencil sketch prompts and a few color keyframes).
    Writes outputs/scene_needs.md and .pipeline/scene_needs.json.
    """
    if state.get("error"):
        return state
    ws = _ws(state)
    settings = load_settings(ws, ws / "config.yaml")
    memory = load_supervisor_memory_context(settings)
    goal = (state.get("user_goal") or "").strip()
    preset = (state.get("goal_preset") or "rewrite").strip()
    plan_raw = (state.get("plan_markdown") or "").strip()
    if not plan_raw:
        return {**state, "log": log_append(state, "scene_needs: skipped (no plan_markdown)")}

    mem_block, mem_cut = _truncate_prompt_block(memory, _SCENE_NEEDS_MAX_MEMORY_CHARS)
    plan, plan_cut = _truncate_prompt_block(plan_raw, _SCENE_NEEDS_MAX_PLAN_CHARS)
    trunc_note = ""
    if mem_cut or plan_cut:
        trunc_note = f" (prompt trimmed: memory_cut={mem_cut}, plan_cut={plan_cut})"

    sys = (
        "You are a showrunner + storyboard planning assistant.\n"
        "Identify probable scene needs: complex staging, defining character moments, era/geography/tech reveals,\n"
        "or places where the plan implies a scene replacement.\n"
        "Output ONLY JSON (no markdown fences) with shape:\n"
        "{\n"
        '  "moments": [\n'
        "    {\n"
        '      "id": "M01",\n'
        '      "why": "short rationale why this moment deserves visual planning",\n'
        '      "scene_hint": "slugline or location/time if known",\n'
        '      "beat": "what happens (camera-visible, shootable)",\n'
        '      "pencil_sketch_prompt": "intricate pencil sketch / lead art, cinematic lighting, conceptual storyboard style, photorealistic shading, high-res JPG",\n'
        '      "color_key_prompt": "a color keyframe prompt (cinematic, saturated, high contrast), high-res JPG"\n'
        "    }\n"
        "  ],\n"
        '  "notes": "optional: constraints (timing/era/demography/geography continuity)"}'
    )
    user = (
        f"TRANSFORMATION_PRESET: {preset}\nUSER_GOAL:\n{goal}\n\n"
        f"CHARACTER_AND_RESEARCH_MEMORY:\n{mem_block or '(none)'}\n\n"
        f"PLAN_MARKDOWN:\n{plan}\n"
    )
    try:
        prov = (state.get("llm_provider") or "").strip() or None
        raw, thinking, meta = _super_chat(
            settings,
            ws,
            messages=[{"role": "system", "content": sys}, {"role": "user", "content": user}],
            temperature=0.25,
            tag="supervisor-scene-needs",
            llm_provider_override=prov,
        )
    except Exception as e:  # noqa: BLE001
        if _is_llm_timeout_error(e):
            return _scene_needs_degraded_state(
                state,
                ws,
                meta_note=f"LLM timed out; continuing without scene needs.{trunc_note} Original error: {e}",
                log_line=f"scene_needs: timed out — wrote empty stubs; pipeline continues{trunc_note}",
            )
        return {**state, "error": str(e), "log": log_append(state, f"scene_needs failed: {e}")}

    obj = extract_json_object(raw) or {}
    moments = obj.get("moments") if isinstance(obj, dict) else None
    moments_list = moments if isinstance(moments, list) else []

    # Always materialize prompt files + expected image paths (rendering can be done later by any tool).
    # This is the "render by default" behavior in a repo-safe way: we persist prompts + placeholders.
    img_dir = (ws / "outputs" / "images").resolve()
    prompt_dir = (img_dir / "prompts").resolve()
    img_dir.mkdir(parents=True, exist_ok=True)
    prompt_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {"moments": []}
    for m in moments_list[:60]:
        if not isinstance(m, dict):
            continue
        mid = (m.get("id") or "").strip() or "M??"
        pencil_prompt = (m.get("pencil_sketch_prompt") or "").strip()
        color_prompt = (m.get("color_key_prompt") or "").strip()
        pencil_img = f"outputs/images/{mid}_pencil.png"
        color_img = f"outputs/images/{mid}_color.png"
        (prompt_dir / f"{mid}_pencil.txt").write_text(pencil_prompt + ("\n" if pencil_prompt else ""), encoding="utf-8")
        (prompt_dir / f"{mid}_color.txt").write_text(color_prompt + ("\n" if color_prompt else ""), encoding="utf-8")
        m["expected_pencil_image"] = pencil_img
        m["expected_color_image"] = color_img
        cast = manifest["moments"]
        if isinstance(cast, list):
            cast.append(
                {
                    "id": mid,
                    "scene_hint": (m.get("scene_hint") or "").strip(),
                    "why": (m.get("why") or "").strip(),
                    "beat": (m.get("beat") or "").strip(),
                    "pencil_prompt_file": f"outputs/images/prompts/{mid}_pencil.txt",
                    "color_prompt_file": f"outputs/images/prompts/{mid}_color.txt",
                    "expected_pencil_image": pencil_img,
                    "expected_color_image": color_img,
                }
            )
    md_lines = ["# Scene needs (post-plan)\n"]
    md_lines.append(f"- provider: `{meta}`\n")
    if moments_list:
        for m in moments_list[:30]:
            if not isinstance(m, dict):
                continue
            md_lines.append(f"## {m.get('id','M??')} — {m.get('scene_hint','(scene)')}\n")
            md_lines.append(f"**Why:** {m.get('why','').strip()}\n")
            md_lines.append(f"**Beat:** {m.get('beat','').strip()}\n")
            md_lines.append(
                f"**Expected images:** `{m.get('expected_pencil_image','')}` and `{m.get('expected_color_image','')}`\n"
            )
            md_lines.append("### Pencil sketch prompt\n")
            md_lines.append(m.get("pencil_sketch_prompt", "").strip() or "(missing)")
            md_lines.append("\n\n### Color key prompt\n")
            md_lines.append(m.get("color_key_prompt", "").strip() or "(missing)")
            md_lines.append("\n")
    else:
        md_lines.append("_No moments returned by model._\n")
    scene_md = "\n".join(md_lines).rstrip() + "\n"

    out_md = _write_outputs_text(ws, "outputs/scene_needs.md", scene_md)
    out_json = _write_pipeline_json(ws, "scene_needs.json", obj)
    out_manifest = _write_pipeline_json(ws, "scene_needs_manifest.json", manifest)
    base = list(state.get("log") or [])
    trace = list(state.get("thinking_trace") or [])
    if thinking:
        trace.append("## Scene needs — model reasoning (thinking)\n\n" + thinking.strip())
    trace.append("## Scene needs — provider\n\n" + f"`{meta}`\n\n" + (raw if len(raw) < 50_000 else raw[:40_000] + "\n…\n" + raw[-8000:]))
    return {
        **state,
        "scene_needs_markdown": scene_md,
        "scene_needs_json": moments_list,
        "thinking_trace": trace,
        "log": base
        + [
            f"scene_needs: wrote {out_md}, {out_json}, {out_manifest} ({len(moments_list)} moment(s)); prompts under outputs/images/prompts/",
        ],
    }


def node_image_prompt_verify(state: SupervisorState) -> SupervisorState:
    """
    Lightweight LLM verification that the selected moments/prompts align with USER_STATEMENTS
    and do not contradict era/geography/character continuity.
    """
    if state.get("error"):
        return state
    ws = _ws(state)
    settings = load_settings(ws, ws / "config.yaml")
    moments = state.get("scene_needs_json") or []
    plan_raw = (state.get("plan_markdown") or "").strip()
    if not plan_raw or not moments:
        return {**state, "log": log_append(state, "image_prompt_verify: skipped (missing plan or scene needs)")}

    plan_block, plan_cut = _truncate_prompt_block(plan_raw, _IMAGE_PROMPT_VERIFY_MAX_PLAN_CHARS)
    plan_note = " (plan truncated for prompt size)" if plan_cut else ""

    statements = list(state.get("user_statements") or [])
    stmt_block = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(statements)) if statements else "(none)"
    sys = (
        "You are a strict verifier for storyboard prompt planning.\n"
        "Check that each moment is justified and aligned with USER_STATEMENTS and the plan.\n"
        "Output ONLY Markdown with:\n"
        "- a short PASS/FAIL line\n"
        "- 3-8 bullets confirming what is covered\n"
        "- if FAIL: bullets of what to fix (moment ids)\n"
    )
    user = (
        f"USER_STATEMENTS:\n{stmt_block}\n\nPLAN:{plan_note}\n{plan_block}\n\n"
        f"SCENE_NEEDS (JSON excerpt):\n{json.dumps(moments[:20], ensure_ascii=False, indent=2)}\n"
    )
    try:
        prov = (state.get("llm_provider") or "").strip() or None
        out_md, thinking, meta = _super_chat(
            settings,
            ws,
            messages=[{"role": "system", "content": sys}, {"role": "user", "content": user}],
            temperature=0.1,
            tag="supervisor-image-prompt-verify",
            llm_provider_override=prov,
        )
    except Exception as e:  # noqa: BLE001
        return {**state, "log": log_append(state, f"image_prompt_verify: skipped ({e})")}

    rel = _write_outputs_text(ws, "outputs/scene_needs_verify.md", out_md.strip() + "\n")
    base = list(state.get("log") or [])
    trace = list(state.get("thinking_trace") or [])
    if thinking:
        trace.append("## Scene needs verify — model reasoning (thinking)\n\n" + thinking.strip())
    trace.append("## Scene needs verify — provider\n\n" + f"`{meta}`\n\n" + (out_md if len(out_md) < 50_000 else out_md[:40_000] + "\n…\n" + out_md[-8000:]))
    return {
        **state,
        "image_prompt_verification": out_md.strip(),
        "thinking_trace": trace,
        "log": base + [f"image_prompt_verify: wrote {rel}"],
    }


def node_balance_context(state: SupervisorState) -> SupervisorState:
    """
    After planning, subdivide chunks if memory+plan+chunk would exceed the edit context budget.

    Uses LangGraph-visible splitting (``#ctx-NNN`` paths) instead of silently truncating prompts.
    """
    if state.get("error"):
        return state
    ws = _ws(state)
    settings = load_settings(ws, ws / "config.yaml")
    memory = load_supervisor_memory_context(settings)
    goal = (state.get("user_goal") or "").strip()
    preset = (state.get("goal_preset") or "rewrite").strip()
    extra = _PRESETS.get(preset, _PRESETS["rewrite"])
    statements = list(state.get("user_statements") or [])
    stmt_txt = "\n".join(f"- {s}" for s in statements) if statements else "(none)"
    fb = (state.get("orchestration_feedback") or "").strip()
    fb_txt = f"\nVERIFIER_FEEDBACK (fix if relevant):\n{fb}\n" if fb else ""
    plan = state.get("plan_markdown") or ""
    chunks = list(state.get("chunks") or [])
    if not chunks:
        return state
    new_chunks, logs = rebalance_chunks_for_context_budget(
        chunks,
        settings,
        memory_bundle=memory,
        goal=goal,
        statements_block=stmt_txt,
        feedback_block=fb_txt,
        preset_extra=extra,
        plan_excerpt=plan,
    )
    base = list(state.get("log") or [])
    extra_logs = [f"balance_context: {len(chunks)} → {len(new_chunks)} chunk(s)"] + logs
    return {
        **state,
        "chunks": new_chunks,
        "chunk_index": 0,
        "log": base + extra_logs,
    }


def node_openclaw_tools(state: SupervisorState) -> SupervisorState:
    if state.get("error"):
        return state
    if not state.get("use_openclaw_after_plan"):
        return {**state, "log": log_append(state, "openclaw: skipped (use_openclaw_after_plan is off)")}
    ws = _ws(state)
    settings = load_settings(ws, ws / "config.yaml")
    if not settings.openclaw_gateway_url or not settings.openclaw_gateway_token:
        return {**state, "log": log_append(state, "openclaw: skipped (no gateway URL/token in env or config)")}

    tool = (state.get("openclaw_tool") or "").strip()
    if not tool:
        return {**state, "log": log_append(state, "openclaw: skipped (no tool name in run)")}

    raw_args = (state.get("openclaw_args_json") or "{}").strip() or "{}"
    try:
        args = json.loads(raw_args)
        if not isinstance(args, dict):
            args = {}
    except json.JSONDecodeError:
        args = {}

    try:
        result = invoke_openclaw_tool(
            settings.openclaw_gateway_url,
            settings.openclaw_gateway_token,
            tool,
            args,
        )
        detail = json.dumps(result, ensure_ascii=False, default=str)[:1800]
        block = openclaw_ascii(tool=tool, ok=True, detail=detail)
        return {
            **state,
            "openclaw_last_result": result,
            "log": log_append(state, block),
        }
    except Exception as e:  # noqa: BLE001
        block = openclaw_ascii(tool=tool, ok=False, detail=str(e))
        return {
            **state,
            "openclaw_last_result": {"error": str(e)},
            "log": log_append(state, block),
        }


_PRESETS = {
    "netflix_script": "Rewrite as a streaming series script (slug lines, NAME caps, dialogue). Output Markdown.",
    "stage_play": "Rewrite as a stage play (acts/scenes, dramatis personae if needed). Output Markdown.",
    "korean_drama_script": (
        "Rewrite as a Korean drama-style series script (episode rhythm, emotional reversals, "
        "slug lines, NAME caps, dialogue). Write in English unless USER_GOAL says otherwise. Output Markdown."
    ),
    "feature_film": (
        "Rewrite as a feature-film screenplay (slug lines, NAME caps, lean shootable action, dialogue). "
        "Output Markdown."
    ),
    "tv_episodic_arcs": (
        "Rewrite as an episodic series bible + pilot script beats: cold open, act breaks, season arc hooks, "
        "character A/B/C stories. Output Markdown."
    ),
    "translation_adapt": (
        "Translate and culturally adapt the SOURCE per USER_GOAL (target language, register, idioms). "
        "Preserve plot unless USER_GOAL says otherwise. Output Markdown."
    ),
    "rewrite": "Line-edit for clarity and voice; keep plot. Output Markdown.",
    "docs": "Rewrite as long-form technical documentation. Output Markdown.",
}


def _edit_chunk_core(
    state: SupervisorState,
    idx: int,
    c: ChunkRecord,
) -> tuple[int, ChunkRecord, list[str]]:
    """Returns (idx, updated_chunk, trace_fragments)."""
    ws = _ws(state)
    settings = load_settings(ws, ws / "config.yaml")
    memory = load_supervisor_memory_context(settings)
    goal = (state.get("user_goal") or "").strip()
    preset = (state.get("goal_preset") or "rewrite").strip()
    extra = _PRESETS.get(preset, _PRESETS["rewrite"])
    plan = state.get("plan_markdown") or ""
    statements = list(state.get("user_statements") or [])
    stmt_txt = "\n".join(f"- {s}" for s in statements) if statements else "(none)"
    fb = (state.get("orchestration_feedback") or "").strip()
    fb_txt = f"\nVERIFIER_FEEDBACK (fix if relevant):\n{fb}\n" if fb else ""
    ch_title = (c.get("chapter_title") or "").strip()
    title_line = f"CHAPTER_TITLE: {ch_title}\n" if ch_title else ""
    original = c.get("original") or ""
    sys = (
        "You transform a single manuscript chunk. Output ONLY the transformed chunk in Markdown.\n"
        "No preamble, no 'Here is'. Preserve continuity with MEMORY when relevant.\n"
        "Satisfy every USER_STATEMENT that applies to this chunk.\n"
        "If the chunk still contains Project Gutenberg license, donation, or trademark boilerplate, "
        "omit it entirely from your output—do not rewrite or summarize long legal text."
    )
    user = (
        f"{extra}\n\nUSER_GOAL:\n{goal}\n\nUSER_STATEMENTS:\n{stmt_txt}\n{fb_txt}"
        f"{title_line}PLAN_EXCERPT:\n{plan}\n\nMEMORY:\n{memory or '(none)'}\n\n"
        f"FILE: {c.get('path')}\n\nCHUNK:\n{original}\n"
    )
    trace_fr: list[str] = []
    try:
        prov = (state.get("llm_provider") or "").strip() or None
        out, thinking, meta = _super_chat(
            settings,
            ws,
            messages=[{"role": "system", "content": sys}, {"role": "user", "content": user}],
            temperature=0.35,
            tag=f"supervisor-edit-{idx}",
            llm_provider_override=prov,
        )
        if thinking:
            trace_fr.append(f"## Edit chunk {idx + 1} — {c.get('path')}\n\n### Thinking\n\n{thinking.strip()}")
        trace_fr.append(f"### Edit chunk {idx + 1} — meta\n\n`{meta}`")
        c2: ChunkRecord = {**c, "proposed": out, "status": "edited"}
        if state.get("openclaw_per_chunk"):
            log_line = _openclaw_chunk_side_effect(
                ws,
                settings,
                c2,
                (state.get("openclaw_tool") or "").strip(),
                state.get("openclaw_args_json") or "{}",
            )
            trace_fr.append(f"### OpenClaw\n\n{log_line}")
        _write_staging_chunk_snapshot(ws, settings, idx, c2)
        return idx, c2, trace_fr
    except Exception as e:  # noqa: BLE001
        c2 = {**c, "status": "error", "proposed": f"(error: {e})"}
        _write_staging_chunk_snapshot(ws, settings, idx, c2)
        return idx, c2, trace_fr


def node_edit_one_chunk(state: SupervisorState) -> SupervisorState:
    if state.get("error"):
        return state
    chunks = list(state.get("chunks") or [])
    idx = int(state.get("chunk_index") or 0)
    if idx >= len(chunks):
        return state

    c = chunks[idx]
    idx2, c2, traces = _edit_chunk_core(state, idx, c)
    chunks[idx2] = c2
    trace = list(state.get("thinking_trace") or [])
    trace.extend(traces)
    if c2.get("status") == "error":
        return {
            **state,
            "chunks": chunks,
            "chunk_index": idx + 1,
            "log": log_append(state, f"edit chunk {idx + 1}/{len(chunks)} FAILED — see thinking_trace"),
            "thinking_trace": trace,
        }

    n = len(chunks)
    done = idx + 1
    extra: list[str] = []
    if n <= 16 or done in (1, n) or (n > 16 and done % max(1, n // 6) == 0):
        extra.append(edit_batch_ascii(done=done, total=n, parallel=False))
    extra.append(f"edit chunk {done}/{n} ok — {c2.get('path', '')}")
    base = list(state.get("log") or [])
    return {
        **state,
        "chunks": chunks,
        "chunk_index": idx + 1,
        "thinking_trace": trace,
        "log": base + extra,
    }


def node_edit_all_chunks_parallel(state: SupervisorState) -> SupervisorState:
    """Edit every chunk (parallel LLM). Marathon graph uses sequential node instead."""
    if state.get("error"):
        return state
    chunks = list(state.get("chunks") or [])
    if not chunks:
        return state
    ws = _ws(state)
    settings = load_settings(ws, ws / "config.yaml")
    n = len(chunks)
    mx = max(1, min(int(settings.supervisor_parallel_workers), 8, n))
    out_chunks = list(chunks)
    all_traces: list[str] = []
    parallel = mx > 1
    mode = "serial (1 worker)" if mx == 1 else f"parallel ({mx} workers)"
    start_note = (
        f"edit_parallel: dispatching {n} chunk(s) — {mode}\n"
        f"live chunk files: `{settings.outputs_dir}/staging_chunks/*.md` (updated as each chunk finishes)\n"
        + edit_batch_ascii(done=0, total=n, parallel=parallel)
    )
    st0 = {**state, "log": log_append(state, start_note)}

    def job(i: int) -> tuple[int, ChunkRecord, list[str]]:
        return _edit_chunk_core(st0, i, chunks[i])

    def _emit_progress(done_n: int, i: int, c2: ChunkRecord) -> None:
        if stderr_llm_stats_enabled():
            print(
                f"[supervisor] edit_parallel progress {done_n}/{n} "
                f"chunk={i + 1} status={c2.get('status')} path={c2.get('path', '')}",
                file=sys.stderr,
                flush=True,
            )

    if mx == 1:
        for i in range(n):
            i, c2, tr = job(i)
            out_chunks[i] = c2
            all_traces.extend(tr)
            _emit_progress(i + 1, i, c2)
    else:
        with ThreadPoolExecutor(max_workers=mx) as ex:
            futs = {ex.submit(job, i): i for i in range(n)}
            done_n = 0
            for fut in as_completed(futs):
                i, c2, tr = fut.result()
                out_chunks[i] = c2
                all_traces.extend(tr)
                done_n += 1
                _emit_progress(done_n, i, c2)

    trace = list(st0.get("thinking_trace") or [])
    trace.extend(all_traces)
    ok = sum(1 for c in out_chunks if c.get("status") == "edited")
    tail = (
        edit_batch_ascii(done=n, total=n, parallel=parallel)
        + f"\nedit_parallel: finished {ok}/{n} edited, {n - ok} error(s), {mode}"
    )
    return {
        **st0,
        "chunks": out_chunks,
        "chunk_index": n,
        "thinking_trace": trace,
        "log": log_append(st0, tail),
    }


def node_staging(state: SupervisorState) -> SupervisorState:
    if state.get("error"):
        return state
    ws = _ws(state)
    settings = load_settings(ws, ws / "config.yaml")
    out_dir = ws / settings.outputs_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    staging = out_dir / "staging_merged.md"
    parts: list[str] = []
    for c in state.get("chunks") or []:
        path = c.get("path", "")
        prop = c.get("proposed") or c.get("original") or ""
        parts.append(f"## {path}\n\n{prop}\n")
    merged = "\n\n---\n\n".join(parts)
    staging.write_text(merged, encoding="utf-8")
    rel = staging.relative_to(ws)
    # Keep checkpoints small: verify reads full text from ``staging_path`` on disk.
    preview = ""
    sb = staging_banner(str(rel).replace("\\", "/"), len(merged))
    base = list(state.get("log") or [])
    return {
        **state,
        "staging_path": str(rel).replace("\\", "/"),
        "merged_preview": preview,
        "log": base + [sb, f"staging: {rel} ({len(merged)} chars merged)"],
    }


def node_verify_mission(state: SupervisorState) -> SupervisorState:
    """LLM rubric: statements + preset satisfied? If not, set feedback for replan/re-edit."""
    if state.get("error"):
        return {**state, "verification_passed": True, "log": log_append(state, "verify: skipped (error set)")}
    ws = _ws(state)
    settings = load_settings(ws, ws / "config.yaml")
    preview = ""
    sp = state.get("staging_path")
    if sp:
        p = ws / sp
        if p.is_file():
            preview = p.read_text(encoding="utf-8", errors="replace")
    if not preview.strip():
        preview = (state.get("merged_preview") or "").strip()

    statements = list(state.get("user_statements") or [])
    stmt_block = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(statements)) if statements else "(none)"
    goal = (state.get("user_goal") or "").strip()
    preset = (state.get("goal_preset") or "rewrite").strip()
    sys = (
        "You verify a merged manuscript against user requirements.\n"
        "Output ONLY JSON (no markdown fences):\n"
        '{"passed": <true|false>, "summary": "<one line>", "violations": [{"statement_index": <0-based int or -1 for global>, "issue": "<short>"}]}\n'
        "passed=true only if the merged work clearly satisfies every USER_STATEMENT and the TRANSFORMATION_PRESET.\n"
        "Always set summary to a non-empty one-line outcome (what you checked and whether it holds); on pass, "
        "violations may be []."
    )
    passes = max(1, int(settings.supervisor_verify_passes))
    prov = (state.get("llm_provider") or "").strip() or None
    trace = list(state.get("thinking_trace") or [])
    outcomes: list[bool] = []
    summaries: list[str] = []
    all_viol: list[Any] = []
    raw_last = ""
    meta_last: dict = {}
    thinking_last = ""

    try:
        for pi in range(passes):
            user = (
                f"(Verifier pass {pi + 1} of {passes}; all passes must agree PASS for overall pass.)\n\n"
                f"TRANSFORMATION_PRESET: {preset}\nUSER_GOAL:\n{goal}\n\n"
                f"USER_STATEMENTS:\n{stmt_block}\n\nMERGED_MANUSCRIPT:\n{preview}\n"
            )
            raw, thinking, meta = _super_chat(
                settings,
                ws,
                messages=[{"role": "system", "content": sys}, {"role": "user", "content": user}],
                temperature=0.1,
                tag=f"supervisor-verify-{pi + 1}",
                llm_provider_override=prov,
            )
            raw_last = raw
            meta_last = dict(meta) if isinstance(meta, dict) else {"meta": meta}
            thinking_last = thinking or ""
            obj = extract_json_object(raw) or {}
            ok = bool(obj.get("passed"))
            outcomes.append(ok)
            summaries.append(str(obj.get("summary") or "").strip())
            viol = obj.get("violations")
            vlist = viol if isinstance(viol, list) else []
            all_viol.extend(vlist)
            if thinking_last:
                trace.append(f"## Verify pass {pi + 1} — thinking\n\n{thinking_last.strip()}")
            trace.append(
                f"## Verify pass {pi + 1} — provider\n\n`{meta_last}`\n\n"
                + (raw_last if len(raw_last) < 50_000 else raw_last[:40_000] + "\n…\n" + raw_last[-8000:])
            )
    except Exception as e:  # noqa: BLE001
        return {
            **state,
            "verification_passed": True,
            "verification_notes": f"verify skipped (error): {e}",
            "log": log_append(state, f"verify: treat as pass ({e})"),
        }

    passed = all(outcomes) if outcomes else True
    summary = " | ".join(s for s in summaries if s) or ("PASS" if passed else "FAIL")
    vlist = all_viol

    fb_parts = [summary] if summary else []
    for v in vlist:
        if isinstance(v, dict):
            fb_parts.append(f"- stmt {v.get('statement_index', '?')}: {v.get('issue', '')}")
    feedback = "\n".join(fb_parts).strip()

    vblock = verify_ascii(passed=passed, summary=summary, violations=vlist)
    tblock = thinking_excerpt_for_log("verify", thinking_last or "")
    one = f"verify: {'PASS' if passed else 'FAIL (may retry)'} — {summary[:160]}"
    base = list(state.get("log") or [])
    return {
        **state,
        "verification_passed": passed,
        "verification_notes": summary,
        "verification_violations": vlist,
        "orchestration_feedback": "" if passed else feedback,
        "thinking_trace": trace,
        "log": base + [vblock, tblock, one],
    }


def node_final_review(state: SupervisorState) -> SupervisorState:
    """One thorough editorial pass on merged staging; writes ``outputs/final_review.md``."""
    if state.get("error"):
        return {**state, "log": log_append(state, "final_review: skipped (error set)")}
    ws = _ws(state)
    settings = load_settings(ws, ws / "config.yaml")
    preview = ""
    sp = state.get("staging_path")
    if sp:
        p = ws / sp
        if p.is_file():
            preview = p.read_text(encoding="utf-8", errors="replace")
    if not preview.strip():
        return {**state, "log": log_append(state, "final_review: skipped (no staging)")}

    truncated, was_cut = _truncate_prompt_block(preview, _FINAL_REVIEW_MAX_MANUSCRIPT_CHARS)
    goal = (state.get("user_goal") or "").strip()
    preset = (state.get("goal_preset") or "rewrite").strip()
    statements = list(state.get("user_statements") or [])
    stmt_block = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(statements)) if statements else "(none)"
    vpass = state.get("verification_passed")
    vnotes = (state.get("verification_notes") or "").strip()
    pass_line = f"prior_verify_passed={vpass}; prior_verify_summary={vnotes[:400]}"

    sys = (
        "You are a senior development editor delivering a thorough pre-submission review.\n"
        "Read the merged manuscript and assess: clarity, voice, structure, continuity, dialogue (if any), "
        "preset/genre fit, and alignment with USER_GOAL and USER_STATEMENTS.\n"
        "Output Markdown only (no JSON): start with one line **Verdict:** READY | NEEDS_WORK | UNCLEAR.\n"
        "Then sections: ## Strengths, ## Issues (numbered), ## Recommendations (prioritized), "
        "## Pre-submission checklist (markdown `- [ ]` task items). Be concrete.\n"
    )
    user = (
        f"{pass_line}\n"
        f"(Manuscript may be truncated for the model window: truncated={was_cut})\n\n"
        f"TRANSFORMATION_PRESET: {preset}\nUSER_GOAL:\n{goal}\n\n"
        f"USER_STATEMENTS:\n{stmt_block}\n\n"
        f"MERGED_MANUSCRIPT:\n{truncated}\n"
    )
    try:
        prov = (state.get("llm_provider") or "").strip() or None
        out_md, thinking, meta = _super_chat(
            settings,
            ws,
            messages=[{"role": "system", "content": sys}, {"role": "user", "content": user}],
            temperature=0.2,
            tag="supervisor-final-review",
            llm_provider_override=prov,
        )
    except Exception as e:  # noqa: BLE001
        return {**state, "log": log_append(state, f"final_review: skipped ({e})")}

    rel = _write_outputs_text(ws, "outputs/final_review.md", out_md.strip() + "\n")
    base = list(state.get("log") or [])
    trace = list(state.get("thinking_trace") or [])
    if thinking:
        trace.append("## Final review — thinking\n\n" + thinking.strip())
    trace.append("## Final review — provider\n\n" + f"`{meta}`\n")
    return {
        **state,
        "final_review_markdown": out_md.strip(),
        "thinking_trace": trace,
        "log": base + [f"final_review: wrote {rel}"],
    }


def node_persist_learnings(state: SupervisorState) -> SupervisorState:
    """Append run outcome to ``.memory/agentic/project_learnings.md`` (durable project memory)."""
    if state.get("error"):
        return state
    ws = _ws(state)
    settings = load_settings(ws, ws / "config.yaml")
    try:
        append_supervisor_run_learnings(
            settings,
            user_goal=str(state.get("user_goal") or ""),
            goal_preset=str(state.get("goal_preset") or ""),
            user_statements=list(state.get("user_statements") or []),
            verification_passed=state.get("verification_passed"),
            verification_notes=str(state.get("verification_notes") or ""),
            verification_violations=list(state.get("verification_violations") or []),
            revision_count=int(state.get("revision_count") or 0),
            staging_path=str(state.get("staging_path") or ""),
            n_chunks=len(state.get("chunks") or []),
            error=str(state.get("error") or "") or None,
        )
    except OSError as e:
        return {**state, "log": log_append(state, f"learnings: write failed ({e})")}
    return {**state, "log": log_append(state, "learnings: appended to .memory/agentic/project_learnings.md")}


def node_prepare_revision(state: SupervisorState) -> SupervisorState:
    chunks = list(state.get("chunks") or [])
    reset: list[ChunkRecord] = []
    for c in chunks:
        reset.append(
            {
                **c,
                "proposed": "",
                "status": "pending",
            }
        )
    rc = int(state.get("revision_count") or 0) + 1
    mx = int(state.get("max_revision_rounds") or 0)
    prep = prepare_revision_ascii(revision_count=rc, max_rounds=mx)
    base = list(state.get("log") or [])
    return {
        **state,
        "chunks": reset,
        "chunk_index": 0,
        "revision_count": rc,
        "plan_markdown": "",
        "plan_thinking": "",
        "merged_preview": "",
        "final_review_markdown": "",
        "log": base + [prep, f"prepare_revision: cycle {rc} (re-plan + re-edit); max rounds = {mx}"],
    }


def route_after_edit(state: SupervisorState) -> str:
    if state.get("error"):
        return "staging"
    idx = int(state.get("chunk_index") or 0)
    n = len(state.get("chunks") or [])
    if idx < n:
        return "edit"
    return "staging"


def route_after_verify(state: SupervisorState) -> str:
    if state.get("error"):
        return "end"
    if state.get("verification_passed", True):
        return "end"
    rc = int(state.get("revision_count") or 0)
    mx = int(state.get("max_revision_rounds") or 2)
    if rc < mx:
        return "retry"
    return "end"
