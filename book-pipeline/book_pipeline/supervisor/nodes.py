from __future__ import annotations

import json
import uuid
from pathlib import Path

from book_pipeline.config import load_settings
from book_pipeline.manuscript_lab import chunk_manuscript
from book_pipeline.memory import load_memory_markdown
from book_pipeline.llm_complete import complete_chat
from book_pipeline.openclaw_tools import invoke_openclaw_tool
from book_pipeline.supervisor.state import ChunkRecord, SupervisorState, log_append


def _ws(state: SupervisorState) -> Path:
    return Path(state["workspace"]).resolve()


def node_init_manifest(state: SupervisorState) -> SupervisorState:
    ws = _ws(state)
    settings = load_settings(ws, ws / "config.yaml")
    sec_dir = ws / settings.sections_dir
    chunks: list[ChunkRecord] = []

    if sec_dir.is_dir():
        for p in sorted(sec_dir.glob("*.md")):
            rel = p.relative_to(ws)
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
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
            text = draft.read_text(encoding="utf-8", errors="replace")
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

    log_msg = f"init: {len(expanded)} chunk(s)"
    if split_count:
        log_msg += f" (split {split_count} oversized file(s) ≤{max_chars} chars for Ollama passes)"

    return {
        **state,
        "chunks": expanded,
        "chunk_index": 0,
        "error": "",
        "log": log_append(state, log_msg),
    }


def node_ollama_plan(state: SupervisorState) -> SupervisorState:
    if state.get("error"):
        return state
    ws = _ws(state)
    settings = load_settings(ws, ws / "config.yaml")
    memory = load_memory_markdown(settings)[:12000]
    goal = (state.get("user_goal") or "").strip() or "Improve clarity and pacing."
    preset = (state.get("goal_preset") or "rewrite").strip()
    summary_lines = [f"- {c['path']} ({len(c.get('original', ''))} chars)" for c in state.get("chunks", [])]
    cap = 120
    summary = "\n".join(summary_lines[:cap])
    if len(summary_lines) > cap:
        summary += f"\n… and {len(summary_lines) - cap} more (total {len(summary_lines)} chunks)"

    sys = (
        "You are the planning node for a book pipeline. Output ONLY Markdown.\n"
        "Include: ## Overview, ## Per-chunk notes (bullet list by path), ## Risks, ## Merge strategy.\n"
        "Do not output JSON. Be concise."
    )
    user = (
        f"TRANSFORMATION_PRESET: {preset}\n\n"
        f"USER_GOAL:\n{goal}\n\n"
        f"CHARACTER_AND_RESEARCH_MEMORY (truncated):\n{memory or '(none)'}\n\n"
        f"CHUNKS:\n{summary}\n"
    )
    try:
        prov = (state.get("llm_provider") or "").strip() or None
        plan, thinking, meta = complete_chat(
            settings,
            llm_provider_override=prov,
            messages=[{"role": "system", "content": sys}, {"role": "user", "content": user}],
            temperature=0.25,
            workspace=ws,
            tag="supervisor-plan",
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

    return {
        **state,
        "plan_markdown": plan,
        "plan_thinking": thinking.strip(),
        "thinking_trace": trace,
        "log": log_append(state, f"plan: {meta.get('provider', 'llm')} ok"),
    }


def node_openclaw_tools(state: SupervisorState) -> SupervisorState:
    if state.get("error"):
        return state
    if not state.get("use_openclaw_after_plan"):
        return {**state, "log": log_append(state, "openclaw: skipped")}
    ws = _ws(state)
    settings = load_settings(ws, ws / "config.yaml")
    if not settings.openclaw_gateway_url or not settings.openclaw_gateway_token:
        return {**state, "log": log_append(state, "openclaw: no gateway config")}

    tool = (state.get("openclaw_tool") or "").strip()
    if not tool:
        return {**state, "log": log_append(state, "openclaw: no tool name")}

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
        return {
            **state,
            "openclaw_last_result": result,
            "log": log_append(state, f"openclaw: {tool} ok"),
        }
    except Exception as e:  # noqa: BLE001
        return {
            **state,
            "openclaw_last_result": {"error": str(e)},
            "log": log_append(state, f"openclaw: {tool} failed: {e}"),
        }


_PRESETS = {
    "netflix_script": "Rewrite as a streaming series script (slug lines, NAME caps, dialogue). Output Markdown.",
    "stage_play": "Rewrite as a stage play (acts/scenes, dramatis personae if needed). Output Markdown.",
    "rewrite": "Line-edit for clarity and voice; keep plot. Output Markdown.",
    "docs": "Rewrite as long-form technical documentation. Output Markdown.",
}


def node_edit_one_chunk(state: SupervisorState) -> SupervisorState:
    if state.get("error"):
        return state
    chunks = list(state.get("chunks") or [])
    idx = int(state.get("chunk_index") or 0)
    if idx >= len(chunks):
        return state

    ws = _ws(state)
    settings = load_settings(ws, ws / "config.yaml")
    memory = load_memory_markdown(settings)[:8000]
    goal = (state.get("user_goal") or "").strip()
    preset = (state.get("goal_preset") or "rewrite").strip()
    extra = _PRESETS.get(preset, _PRESETS["rewrite"])
    plan = (state.get("plan_markdown") or "")[:6000]
    c = chunks[idx]
    original = c.get("original") or ""

    sys = (
        "You transform a single manuscript chunk. Output ONLY the transformed chunk in Markdown.\n"
        "No preamble, no 'Here is'. Preserve continuity with MEMORY when relevant."
    )
    user = (
        f"{extra}\n\nUSER_GOAL:\n{goal}\n\n"
        f"PLAN_EXCERPT:\n{plan}\n\n"
        f"MEMORY:\n{memory or '(none)'}\n\n"
        f"FILE: {c.get('path')}\n\nCHUNK:\n{original}\n"
    )
    trace = list(state.get("thinking_trace") or [])
    try:
        prov = (state.get("llm_provider") or "").strip() or None
        out, thinking, meta = complete_chat(
            settings,
            llm_provider_override=prov,
            messages=[{"role": "system", "content": sys}, {"role": "user", "content": user}],
            temperature=0.35,
            workspace=ws,
            tag=f"supervisor-edit-{idx}",
        )
        if thinking:
            trace.append(f"## Edit chunk {idx + 1} — {c.get('path')}\n\n### Thinking\n\n{thinking.strip()}")
        trace.append(f"### Edit chunk {idx + 1} — meta\n\n`{meta}`")
        c = {**c, "proposed": out, "status": "edited"}
        chunks[idx] = c
    except Exception as e:  # noqa: BLE001
        c = {**c, "status": "error", "proposed": f"(error: {e})"}
        chunks[idx] = c
        return {
            **state,
            "chunks": chunks,
            "chunk_index": idx + 1,
            "log": log_append(state, f"edit chunk {idx} failed: {e}"),
        }

    return {
        **state,
        "chunks": chunks,
        "chunk_index": idx + 1,
        "thinking_trace": trace,
        "log": log_append(state, f"edit chunk {idx + 1}/{len(chunks)} ok"),
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
    staging.write_text("\n\n---\n\n".join(parts), encoding="utf-8")
    rel = staging.relative_to(ws)
    return {
        **state,
        "staging_path": str(rel).replace("\\", "/"),
        "log": log_append(state, f"staging: {rel}"),
    }


def route_after_edit(state: SupervisorState) -> str:
    if state.get("error"):
        return "staging"
    idx = int(state.get("chunk_index") or 0)
    n = len(state.get("chunks") or [])
    if idx < n:
        return "edit"
    return "staging"
