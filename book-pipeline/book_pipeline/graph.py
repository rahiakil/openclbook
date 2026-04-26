from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from book_pipeline.config import Settings, load_settings
from book_pipeline.gutenberg import strip_project_gutenberg_boilerplate
from book_pipeline.ingest import read_document
from book_pipeline.memory import append_research_note, load_memory_markdown
from book_pipeline.ollama_client import ollama_chat
from book_pipeline.openclaw_optional import llm_task_json
from book_pipeline.prompts import default_system_for


class PipelineState(TypedDict, total=False):
    workspace: str
    task: dict[str, Any]
    log: list[str]
    memory_bundle: str
    source_text: str
    result_text: str
    output_path: str
    error: str
    _settings: Settings


def _log(state: PipelineState, msg: str) -> list[str]:
    logs = list(state.get("log") or [])
    logs.append(msg)
    return logs


def node_prepare(state: PipelineState) -> PipelineState:
    ws = Path(state["workspace"])
    settings = load_settings(ws, ws / "config.yaml")
    task = state.get("task") or {}
    logs = _log(state, "prepare: load memory + source")

    memory = load_memory_markdown(settings)
    src_rel = task.get("source") or task.get("path") or "manuscript/draft.md"
    src_path = (ws / src_rel).resolve()
    if not str(src_path).startswith(str(ws.resolve())):
        return {
            **state,
            "_settings": settings,
            "error": "source path escapes workspace",
            "log": _log(state, "error: path traversal"),
        }
    if not src_path.is_file():
        return {
            **state,
            "_settings": settings,
            "memory_bundle": memory,
            "source_text": "",
            "error": f"missing source file: {src_rel}",
            "log": _log(state, f"error: missing {src_rel}"),
        }
    try:
        text = strip_project_gutenberg_boilerplate(read_document(src_path))
    except Exception as e:  # noqa: BLE001
        return {
            **state,
            "_settings": settings,
            "error": str(e),
            "log": _log(state, f"error ingest: {e}"),
        }

    return {
        **state,
        "_settings": settings,
        "memory_bundle": memory,
        "source_text": text,
        "log": logs,
        "error": "",
    }


def node_optional_plan(state: PipelineState) -> PipelineState:
    """Optional OpenClaw llm-task JSON plan (does not block on failure)."""
    settings = state.get("_settings")
    if not settings or not settings.openclaw_gateway_url or not settings.openclaw_gateway_token:
        return {**state, "log": _log(state, "plan: skipped (no OpenClaw gateway env)")}
    task = state.get("task") or {}
    try:
        plan = llm_task_json(
            settings.openclaw_gateway_url,
            settings.openclaw_gateway_token,
            prompt=(
                "Return JSON with keys: focus_points (array of strings), risks (array), "
                "next_edit_goal (string). Base it on the input excerpt and action."
            ),
            input_obj={
                "action": task.get("action", "rewrite_section"),
                "instruction": task.get("instruction", ""),
                "excerpt": (state.get("source_text") or "")[:8000],
            },
            provider="ollama",
            model=settings.ollama_model,
        )
        note = json.dumps(plan, indent=2) if not isinstance(plan, str) else plan
        append_research_note(settings, "planner-output", note)
        return {**state, "log": _log(state, "plan: OpenClaw llm-task ok")}
    except Exception as e:  # noqa: BLE001
        return {**state, "log": _log(state, f"plan: skipped ({e})")}


def node_generate(state: PipelineState) -> PipelineState:
    if state.get("error"):
        return state
    settings = state["_settings"]
    task = state.get("task") or {}
    action = str(task.get("action", "rewrite_section"))
    instruction = str(task.get("instruction", "")).strip()
    system = default_system_for(action)
    memory = state.get("memory_bundle", "")
    source = state.get("source_text", "")

    user_parts = [
        "CHARACTER_AND_RESEARCH_MEMORY:\n" + (memory or "(none)"),
        "USER_INSTRUCTION:\n" + (instruction or "(none)"),
        "SOURCE:\n" + source,
    ]
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]
    try:
        _usage = Path(state["workspace"]) / ".pipeline" / "ollama_usage.jsonl"
        out, _usage_row = ollama_chat(
            settings.ollama_base_url,
            settings.ollama_model,
            messages,
            temperature=float(task.get("temperature", 0.35)),
            usage_log_path=_usage,
            log_tag="graph_generate",
            timeout=float(settings.ollama_http_timeout_seconds),
        )
    except Exception as e:  # noqa: BLE001
        return {**state, "error": str(e), "log": _log(state, f"generate error: {e}")}
    return {**state, "result_text": out, "log": _log(state, "generate: ok")}


def node_persist(state: PipelineState) -> PipelineState:
    if state.get("error"):
        return state
    settings = state["_settings"]
    task = state.get("task") or {}
    out_rel = task.get("output") or task.get("output_path")
    if not out_rel:
        action = str(task.get("action", "edit"))
        out_rel = f"outputs/{action}.md"
    path = (settings.workspace / out_rel).resolve()
    if not str(path).startswith(str(settings.workspace.resolve())):
        return {**state, "error": "output path escapes workspace", "log": _log(state, "persist: bad path")}
    path.parent.mkdir(parents=True, exist_ok=True)
    text = state.get("result_text") or ""
    path.write_text(text, encoding="utf-8")
    return {
        **state,
        "output_path": str(path),
        "log": _log(state, f"persist: {out_rel}"),
    }


def build_pipeline_graph() -> Any:
    g = StateGraph(PipelineState)
    g.add_node("prepare", node_prepare)
    g.add_node("plan", node_optional_plan)
    g.add_node("generate", node_generate)
    g.add_node("persist", node_persist)
    g.add_edge(START, "prepare")
    g.add_edge("prepare", "plan")
    g.add_edge("plan", "generate")
    g.add_edge("generate", "persist")
    g.add_edge("persist", END)
    return g.compile()


def run_task(workspace: Path, task: dict) -> PipelineState:
    graph = build_pipeline_graph()
    return graph.invoke(
        {
            "workspace": str(workspace.resolve()),
            "task": task,
            "log": [],
        }
    )
