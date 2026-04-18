from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from book_pipeline.supervisor.nodes import (
    node_edit_one_chunk,
    node_init_manifest,
    node_ollama_plan,
    node_openclaw_tools,
    node_staging,
    route_after_edit,
)
from book_pipeline.supervisor.state import SupervisorState

_SUP_APPS: dict[tuple[str, bool], Any] = {}

try:
    from langgraph.checkpoint.sqlite import SqliteSaver

    _HAS_SQLITE = True
except ImportError:
    _HAS_SQLITE = False


def _checkpointer(workspace: Path, *, marathon: bool = False):
    """Persist checkpoints under workspace/.pipeline/ (relative URI, cwd-safe).

    Marathon (guided / pause-after-chunk) uses a separate DB so the compiled graph
    shape differs from the default full-run graph and must not share checkpoints.
    """
    import os

    pipeline_dir = (workspace / ".pipeline").resolve()
    pipeline_dir.mkdir(parents=True, exist_ok=True)
    db_name = "checkpoints_marathon.sqlite" if marathon else "checkpoints.sqlite"
    if _HAS_SQLITE:
        try:
            prev = os.getcwd()
            os.chdir(pipeline_dir)
            try:
                return SqliteSaver.from_conn_string(f"sqlite:///{db_name}")
            finally:
                os.chdir(prev)
        except Exception:
            return MemorySaver()
    return MemorySaver()


def _route_init(state: SupervisorState) -> str:
    return "abort" if state.get("error") else "ok"


def build_supervisor_app(workspace: Path, *, marathon: bool = False):
    """
    Compiled graph + checkpointer bound to workspace (for sqlite path).

    When marathon=True, pauses after every ``edit_chunk`` so a driver loop can
    invoke(None) repeatedly — checkpoints land between chunks (200-page jobs).
    """
    g = StateGraph(SupervisorState)
    g.add_node("init", node_init_manifest)
    g.add_node("plan", node_ollama_plan)
    g.add_node("openclaw", node_openclaw_tools)
    g.add_node("edit_chunk", node_edit_one_chunk)
    g.add_node("staging", node_staging)

    g.add_edge(START, "init")
    g.add_conditional_edges("init", _route_init, {"abort": END, "ok": "plan"})
    g.add_edge("plan", "openclaw")
    g.add_edge("openclaw", "edit_chunk")
    g.add_conditional_edges(
        "edit_chunk",
        route_after_edit,
        {"edit": "edit_chunk", "staging": "staging"},
    )
    g.add_edge("staging", END)

    cp = _checkpointer(workspace, marathon=marathon)
    ia = ["edit_chunk"] if marathon else None
    return g.compile(checkpointer=cp, interrupt_after=ia)


def get_supervisor_app(workspace: Path, *, marathon: bool = False):
    """Reuse compiled graph + checkpointer per workspace (full vs marathon compile)."""
    key = (str(workspace.resolve()), marathon)
    if key not in _SUP_APPS:
        _SUP_APPS[key] = build_supervisor_app(workspace, marathon=marathon)
    return _SUP_APPS[key]


def _initial_supervisor_state(
    workspace: Path,
    *,
    user_goal: str,
    goal_preset: str,
    use_openclaw_after_plan: bool,
    openclaw_tool: str,
    openclaw_args_json: str,
    llm_provider: str | None,
) -> SupervisorState:
    st: SupervisorState = {
        "workspace": str(workspace.resolve()),
        "user_goal": user_goal,
        "goal_preset": goal_preset,
        "use_openclaw_after_plan": use_openclaw_after_plan,
        "openclaw_tool": openclaw_tool,
        "openclaw_args_json": openclaw_args_json,
        "log": [],
    }
    p = (llm_provider or "").strip()
    if p:
        st["llm_provider"] = p
    return st


def run_supervisor(
    workspace: Path,
    *,
    thread_id: str,
    user_goal: str,
    goal_preset: str,
    use_openclaw_after_plan: bool,
    openclaw_tool: str,
    openclaw_args_json: str,
    llm_provider: str | None = None,
) -> SupervisorState:
    app = get_supervisor_app(workspace, marathon=False)
    config = {"configurable": {"thread_id": thread_id}}
    initial = _initial_supervisor_state(
        workspace,
        user_goal=user_goal,
        goal_preset=goal_preset,
        use_openclaw_after_plan=use_openclaw_after_plan,
        openclaw_tool=openclaw_tool,
        openclaw_args_json=openclaw_args_json,
        llm_provider=llm_provider,
    )
    return app.invoke(initial, config)


def run_supervisor_marathon(
    workspace: Path,
    *,
    thread_id: str,
    user_goal: str,
    goal_preset: str,
    use_openclaw_after_plan: bool,
    openclaw_tool: str,
    openclaw_args_json: str,
    llm_provider: str | None = None,
    sleep_s: float = 0.0,
    heartbeat_path: Path | None = None,
    on_step: Callable[[SupervisorState, list[str]], None] | None = None,
) -> SupervisorState:
    """
    Same pipeline as ``run_supervisor``, but stops after each chunk edit so work
    is checkpointed to SQLite every chunk. Re-run with the same ``thread_id`` to
    resume after a crash (marathon compile + shared checkpoint DB).

    Uses local Ollama only (no cloud metered cost).
    """
    app = get_supervisor_app(workspace, marathon=True)
    config = {"configurable": {"thread_id": thread_id}}
    initial = _initial_supervisor_state(
        workspace,
        user_goal=user_goal,
        goal_preset=goal_preset,
        use_openclaw_after_plan=use_openclaw_after_plan,
        openclaw_tool=openclaw_tool,
        openclaw_args_json=openclaw_args_json,
        llm_provider=llm_provider,
    )

    def _snapshot() -> SupervisorState:
        snap = app.get_state(config)
        vals = getattr(snap, "values", None) or {}
        return dict(vals) if isinstance(vals, dict) else {}

    def _heartbeat(values: SupervisorState) -> None:
        if heartbeat_path is None:
            return
        chunks = list(values.get("chunks") or [])
        idx = int(values.get("chunk_index") or 0)
        payload = {
            "thread_id": thread_id,
            "ts": time.time(),
            "chunk_index": idx,
            "total_chunks": len(chunks),
            "staging_path": values.get("staging_path"),
            "error": values.get("error"),
            "last_logs": (values.get("log") or [])[-8:],
        }
        heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        heartbeat_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    snap0 = app.get_state(config)
    vals0 = dict(getattr(snap0, "values", None) or {})
    pending0 = list(getattr(snap0, "next", ()) or ())
    if vals0 and not pending0:
        return vals0

    if not pending0:
        app.invoke(initial, config)

    vals = _snapshot()
    _heartbeat(vals)
    if on_step:
        on_step(vals, list(vals.get("log") or [])[-5:])

    while True:
        snap = app.get_state(config)
        nxt = list(getattr(snap, "next", ()) or ())
        if not nxt:
            break
        if sleep_s > 0:
            time.sleep(sleep_s)
        app.invoke(None, config)
        vals = _snapshot()
        _heartbeat(vals)
        if on_step:
            on_step(vals, list(vals.get("log") or [])[-5:])

    return _snapshot()


def get_supervisor_state(workspace: Path, thread_id: str) -> Any:
    app = get_supervisor_app(workspace, marathon=False)
    config = {"configurable": {"thread_id": thread_id}}
    snap = app.get_state(config)
    return snap


def supervisor_guided_step(
    workspace: Path,
    thread_id: str,
    *,
    user_goal: str,
    goal_preset: str,
    use_openclaw_after_plan: bool,
    openclaw_tool: str,
    openclaw_args_json: str,
    llm_provider: str | None = None,
) -> SupervisorState:
    """
    Run exactly one LangGraph ``invoke`` for the marathon (interrupt-after-edit) app.

    - First call for a new ``thread_id``: pass full goal fields; invokes from START
      until the graph pauses after the first completed ``edit_chunk`` (or finishes).
    - Later calls: pass the same goal fields (ignored if state already exists) and
      this function invokes ``None`` to resume until the next pause or END.
    """
    app = get_supervisor_app(workspace, marathon=True)
    config = {"configurable": {"thread_id": thread_id}}
    snap0 = app.get_state(config)
    vals0 = dict(getattr(snap0, "values", None) or {})
    pending0 = list(getattr(snap0, "next", ()) or ())

    if vals0 and not pending0:
        return vals0

    initial = _initial_supervisor_state(
        workspace,
        user_goal=user_goal,
        goal_preset=goal_preset,
        use_openclaw_after_plan=use_openclaw_after_plan,
        openclaw_tool=openclaw_tool,
        openclaw_args_json=openclaw_args_json,
        llm_provider=llm_provider,
    )

    if not pending0:
        app.invoke(initial, config)
    else:
        app.invoke(None, config)

    snap1 = app.get_state(config)
    vals1 = dict(getattr(snap1, "values", None) or {})
    return vals1
