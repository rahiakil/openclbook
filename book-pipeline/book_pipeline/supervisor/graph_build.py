from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from book_pipeline.supervisor.nodes import (
    node_balance_context,
    node_divide_work,
    node_edit_all_chunks_parallel,
    node_edit_one_chunk,
    node_final_review,
    node_init_manifest,
    node_image_prompt_verify,
    node_ollama_plan,
    node_openclaw_tools,
    node_scene_needs_pass,
    node_persist_learnings,
    node_prepare_revision,
    node_staging,
    node_verify_mission,
    route_after_edit,
    route_after_verify,
)
from book_pipeline.supervisor.nodes_prep import (
    node_prep_arc_pass,
    node_prep_character_pass,
    node_prep_strategic_plan,
)
from book_pipeline.supervisor.orchestration import parse_user_statements
from book_pipeline.supervisor.plan_gate import build_resume_initial_state, write_plan_review_artifacts
from book_pipeline.supervisor.prep_gate import (
    build_prep_resume_initial_state,
    prep_answers_path,
    prep_request_path,
    prep_strategy_path,
    write_prep_artifacts,
)
from book_pipeline.supervisor.state import SupervisorState
from book_pipeline.usage_stats import refresh_pipeline_metrics_summaries

# Bump when graph topology or state contract changes (invalidates compiled app cache).
GRAPH_VERSION = 9


def _emit_project_metrics(workspace: Path) -> None:
    refresh_pipeline_metrics_summaries(workspace)

_SUP_APPS: dict[tuple[str, bool, int], Any] = {}
_PLAN_PHASE_APPS: dict[tuple[str, int], Any] = {}
_POST_PLAN_APPS: dict[tuple[str, bool, int], Any] = {}
_PREP_PHASE_ONE_APPS: dict[tuple[str, int], Any] = {}
_POST_PREP_APPS: dict[tuple[str, bool, int], Any] = {}

try:
    from langgraph.checkpoint.sqlite import SqliteSaver

    _HAS_SQLITE = True
except ImportError:
    _HAS_SQLITE = False


def _checkpointer(workspace: Path, *, marathon: bool = False):
    """Persist checkpoints under workspace/.pipeline/ (absolute path, cwd-safe).

    Marathon (guided / pause-after-chunk) uses a separate DB so the compiled graph
    shape differs from the default full-run graph and must not share checkpoints.
    """
    pipeline_dir = (workspace / ".pipeline").resolve()
    pipeline_dir.mkdir(parents=True, exist_ok=True)
    db_name = "checkpoints_marathon.sqlite" if marathon else "checkpoints.sqlite"
    db_path = pipeline_dir / db_name
    if _HAS_SQLITE:
        try:
            conn = sqlite3.connect(str(db_path), check_same_thread=False)
            return SqliteSaver(conn)
        except Exception:
            return MemorySaver()
    return MemorySaver()


def _route_init(state: SupervisorState) -> str:
    return "abort" if state.get("error") else "ok"


def _register_supervisor_tail_nodes(g: StateGraph) -> None:
    """Nodes from ``divide_work`` through ``prepare_revision`` (shared by full graph and post-prep graph)."""
    g.add_node("divide_work", node_divide_work)
    g.add_node("plan", node_ollama_plan)
    g.add_node("scene_needs", node_scene_needs_pass)
    g.add_node("scene_needs_verify", node_image_prompt_verify)
    g.add_node("balance_context", node_balance_context)
    g.add_node("openclaw", node_openclaw_tools)
    g.add_node("edit_chunk", node_edit_one_chunk)
    g.add_node("edit_parallel", node_edit_all_chunks_parallel)
    g.add_node("staging", node_staging)
    g.add_node("verify", node_verify_mission)
    g.add_node("final_review", node_final_review)
    g.add_node("persist_learnings", node_persist_learnings)
    g.add_node("prepare_revision", node_prepare_revision)


def _wire_supervisor_tail_edges(g: StateGraph, *, marathon: bool) -> None:
    g.add_edge("divide_work", "plan")
    g.add_edge("plan", "scene_needs")
    g.add_edge("scene_needs", "scene_needs_verify")
    g.add_edge("scene_needs_verify", "balance_context")
    g.add_edge("balance_context", "openclaw")
    if marathon:
        g.add_edge("openclaw", "edit_chunk")
        g.add_conditional_edges(
            "edit_chunk",
            route_after_edit,
            {"edit": "edit_chunk", "staging": "staging"},
        )
    else:
        g.add_edge("openclaw", "edit_parallel")
        g.add_edge("edit_parallel", "staging")
    g.add_edge("staging", "verify")
    g.add_edge("verify", "final_review")
    g.add_edge("final_review", "persist_learnings")
    g.add_conditional_edges(
        "persist_learnings",
        route_after_verify,
        {"end": END, "retry": "prepare_revision"},
    )
    g.add_edge("prepare_revision", "plan")


def build_supervisor_app(workspace: Path, *, marathon: bool = False):
    """
    Orchestration graph:

    - ``init`` → ``divide_work`` → ``plan`` → ``balance_context`` (resplit chunks if prompt
      would exceed budget) → ``openclaw`` (optional) → **edit** → ``staging`` → ``verify``
      → ``final_review`` → ``persist_learnings``.
    - After learnings: **PASS** or max revisions → ``END``; else ``prepare_revision`` → ``plan``.

    **marathon=True**: sequential ``edit_chunk`` with interrupt after each chunk
    (human-in-the-loop). **marathon=False**: ``edit_parallel`` (thread pool over chunks).
    """
    g = StateGraph(SupervisorState)
    g.add_node("init", node_init_manifest)
    _register_supervisor_tail_nodes(g)

    g.add_edge(START, "init")
    g.add_conditional_edges("init", _route_init, {"abort": END, "ok": "divide_work"})
    _wire_supervisor_tail_edges(g, marathon=marathon)

    cp = _checkpointer(workspace, marathon=marathon)
    ia = ["edit_chunk"] if marathon else None
    return g.compile(checkpointer=cp, interrupt_after=ia)


def build_supervisor_prep_phase_one_app(workspace: Path) -> Any:
    """Prep gate step 1: ``init`` → ``prep_strategic_plan`` → END (writes strategy + human Q&A files)."""
    g = StateGraph(SupervisorState)
    g.add_node("init", node_init_manifest)
    g.add_node("prep_strategic_plan", node_prep_strategic_plan)
    g.add_edge(START, "init")
    g.add_conditional_edges("init", _route_init, {"abort": END, "ok": "prep_strategic_plan"})
    g.add_edge("prep_strategic_plan", END)
    return g.compile(checkpointer=MemorySaver())


def build_supervisor_post_prep_app(workspace: Path, *, marathon: bool = False) -> Any:
    """After human answers: ``prep_character_pass`` → ``prep_arc_pass`` → full supervisor tail."""
    g = StateGraph(SupervisorState)
    g.add_node("prep_character_pass", node_prep_character_pass)
    g.add_node("prep_arc_pass", node_prep_arc_pass)
    _register_supervisor_tail_nodes(g)
    g.add_edge(START, "prep_character_pass")
    g.add_edge("prep_character_pass", "prep_arc_pass")
    g.add_edge("prep_arc_pass", "divide_work")
    _wire_supervisor_tail_edges(g, marathon=marathon)
    cp = _checkpointer(workspace, marathon=marathon)
    ia = ["edit_chunk"] if marathon else None
    return g.compile(checkpointer=cp, interrupt_after=ia)


def build_supervisor_plan_phase_app(workspace: Path) -> Any:
    """Human gate phase 1: ``init`` → ``divide_work`` → ``plan`` → END (writes artifacts in Python)."""
    g = StateGraph(SupervisorState)
    g.add_node("init", node_init_manifest)
    g.add_node("divide_work", node_divide_work)
    g.add_node("plan", node_ollama_plan)
    g.add_node("scene_needs", node_scene_needs_pass)
    g.add_node("scene_needs_verify", node_image_prompt_verify)
    g.add_edge(START, "init")
    g.add_conditional_edges("init", _route_init, {"abort": END, "ok": "divide_work"})
    g.add_edge("divide_work", "plan")
    g.add_edge("plan", "scene_needs")
    g.add_edge("scene_needs", "scene_needs_verify")
    g.add_edge("scene_needs_verify", END)
    return g.compile(checkpointer=MemorySaver())


def build_supervisor_post_plan_app(workspace: Path, *, marathon: bool = False) -> Any:
    """Human gate phase 2: start at ``balance_context`` (same tail as full graph, including verify retry)."""
    g = StateGraph(SupervisorState)
    g.add_node("balance_context", node_balance_context)
    g.add_node("openclaw", node_openclaw_tools)
    g.add_node("edit_chunk", node_edit_one_chunk)
    g.add_node("edit_parallel", node_edit_all_chunks_parallel)
    g.add_node("staging", node_staging)
    g.add_node("verify", node_verify_mission)
    g.add_node("final_review", node_final_review)
    g.add_node("persist_learnings", node_persist_learnings)
    g.add_node("prepare_revision", node_prepare_revision)
    g.add_node("plan", node_ollama_plan)

    g.add_edge(START, "balance_context")
    g.add_edge("balance_context", "openclaw")
    if marathon:
        g.add_edge("openclaw", "edit_chunk")
        g.add_conditional_edges(
            "edit_chunk",
            route_after_edit,
            {"edit": "edit_chunk", "staging": "staging"},
        )
    else:
        g.add_edge("openclaw", "edit_parallel")
        g.add_edge("edit_parallel", "staging")
    g.add_edge("staging", "verify")
    g.add_edge("verify", "final_review")
    g.add_edge("final_review", "persist_learnings")
    g.add_conditional_edges(
        "persist_learnings",
        route_after_verify,
        {"end": END, "retry": "prepare_revision"},
    )
    g.add_edge("prepare_revision", "plan")
    g.add_edge("plan", "balance_context")

    cp = _checkpointer(workspace, marathon=False)
    ia = ["edit_chunk"] if marathon else None
    return g.compile(checkpointer=cp, interrupt_after=ia)


def get_supervisor_plan_phase_app(workspace: Path) -> Any:
    key = (str(workspace.resolve()), GRAPH_VERSION)
    if key not in _PLAN_PHASE_APPS:
        _PLAN_PHASE_APPS[key] = build_supervisor_plan_phase_app(workspace)
    return _PLAN_PHASE_APPS[key]


def get_supervisor_post_plan_app(workspace: Path, *, marathon: bool = False) -> Any:
    key = (str(workspace.resolve()), marathon, GRAPH_VERSION)
    if key not in _POST_PLAN_APPS:
        _POST_PLAN_APPS[key] = build_supervisor_post_plan_app(workspace, marathon=marathon)
    return _POST_PLAN_APPS[key]


def get_supervisor_prep_phase_one_app(workspace: Path) -> Any:
    key = (str(workspace.resolve()), GRAPH_VERSION)
    if key not in _PREP_PHASE_ONE_APPS:
        _PREP_PHASE_ONE_APPS[key] = build_supervisor_prep_phase_one_app(workspace)
    return _PREP_PHASE_ONE_APPS[key]


def get_supervisor_post_prep_app(workspace: Path, *, marathon: bool = False) -> Any:
    key = (str(workspace.resolve()), marathon, GRAPH_VERSION)
    if key not in _POST_PREP_APPS:
        _POST_PREP_APPS[key] = build_supervisor_post_prep_app(workspace, marathon=marathon)
    return _POST_PREP_APPS[key]


def get_supervisor_app(workspace: Path, *, marathon: bool = False):
    """Reuse compiled graph + checkpointer per workspace (full vs marathon compile)."""
    key = (str(workspace.resolve()), marathon, GRAPH_VERSION)
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
    user_statements_json: str = "",
    use_semantic_division: bool = True,
    openclaw_per_chunk: bool = False,
    max_revision_rounds: int = 2,
    supervisor_run_mode: str = "full",
) -> SupervisorState:
    stmts = parse_user_statements(user_goal, user_statements_json)
    try:
        mx = int(max_revision_rounds)
    except (TypeError, ValueError):
        mx = 2
    mx = max(0, min(mx, 8))
    mode = (supervisor_run_mode or "full").strip().lower()
    if mode not in ("full", "marathon"):
        mode = "full"
    st: SupervisorState = {
        "workspace": str(workspace.resolve()),
        "user_goal": user_goal,
        "goal_preset": goal_preset,
        "use_openclaw_after_plan": use_openclaw_after_plan,
        "openclaw_tool": openclaw_tool,
        "openclaw_args_json": openclaw_args_json,
        "log": [],
        "user_statements": stmts,
        "user_statements_json": user_statements_json or "",
        "use_semantic_division": bool(use_semantic_division),
        "openclaw_per_chunk": bool(openclaw_per_chunk),
        "revision_count": 0,
        "max_revision_rounds": mx,
        "supervisor_run_mode": mode,
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
    user_statements_json: str = "",
    use_semantic_division: bool = True,
    openclaw_per_chunk: bool = False,
    max_revision_rounds: int = 2,
    stream_progress: bool = False,
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
        user_statements_json=user_statements_json,
        use_semantic_division=use_semantic_division,
        openclaw_per_chunk=openclaw_per_chunk,
        max_revision_rounds=max_revision_rounds,
        supervisor_run_mode="full",
    )
    if not stream_progress:
        out = app.invoke(initial, config)
        _emit_project_metrics(workspace)
        return out
    streamed = _stream_graph_updates(app, initial, config)
    _emit_project_metrics(workspace)
    return streamed


def _stream_graph_updates(app: Any, initial: SupervisorState, config: dict[str, Any]) -> SupervisorState:
    """Stream graph execution for a fresh ``invoke`` (non-null input)."""
    import sys

    def _emit_update(update: dict[str, object]) -> None:
        for node_name, payload in update.items():
            line = f"[supervisor] {node_name}"
            if isinstance(payload, dict):
                err = payload.get("error")
                if err:
                    line += f"  error={str(err)[:160]}"
                logs = payload.get("log")
                if isinstance(logs, list) and logs:
                    tail = str(logs[-1])
                    line += "  |  " + (tail[:240] + "…" if len(tail) > 240 else tail)
            print(line, flush=True, file=sys.stderr)

    for chunk in app.stream(initial, config, stream_mode="updates"):
        if isinstance(chunk, tuple) and len(chunk) == 2:
            _, chunk = chunk
        if isinstance(chunk, dict):
            _emit_update(chunk)
        else:
            print(f"[supervisor] {chunk!r}", flush=True, file=sys.stderr)
    snap = app.get_state(config)
    vals = getattr(snap, "values", None) or {}
    return dict(vals) if isinstance(vals, dict) else {}


def _stream_invoke_updates(app: Any, initial: SupervisorState, config: dict[str, Any]) -> SupervisorState:
    return _stream_graph_updates(app, initial, config)


def run_supervisor_resume_checkpoint(
    workspace: Path,
    *,
    thread_id: str,
    stream_progress: bool = False,
) -> SupervisorState:
    """
    Continue the default (non-marathon) supervisor graph from the last SQLite checkpoint.

    Use the same ``thread_id`` as the interrupted or partial ``run_supervisor`` / ``ingest-run``.
    If the graph already finished (no pending nodes), returns the terminal state unchanged.
    If no checkpoint exists for the thread, returns ``error`` ``no_checkpoint_for_thread``.
    """
    app = get_supervisor_app(workspace, marathon=False)
    config = {"configurable": {"thread_id": thread_id}}
    snap = app.get_state(config)
    vals = dict(getattr(snap, "values", None) or {})
    nxt = list(getattr(snap, "next", ()) or ())
    if not nxt:
        _emit_project_metrics(workspace)
        if not vals:
            return {
                "workspace": str(workspace.resolve()),
                "log": [f"resume-graph: no checkpoint for thread_id={thread_id!r}"],
                "error": "no_checkpoint_for_thread",
            }
        return vals
    if stream_progress:
        import sys

        print(
            "[supervisor] resume-graph: checkpoint resume uses a single invoke batch "
            "(per-node stream lines apply to fresh runs, not LangGraph resume input).",
            flush=True,
            file=sys.stderr,
        )
    app.invoke(None, config)
    snap2 = app.get_state(config)
    out = dict(getattr(snap2, "values", None) or {})
    _emit_project_metrics(workspace)
    return out if isinstance(out, dict) else {}


def run_supervisor_plan_phase(
    workspace: Path,
    *,
    thread_id: str,
    user_goal: str,
    goal_preset: str,
    use_openclaw_after_plan: bool,
    openclaw_tool: str,
    openclaw_args_json: str,
    llm_provider: str | None = None,
    user_statements_json: str = "",
    use_semantic_division: bool = True,
    openclaw_per_chunk: bool = False,
    max_revision_rounds: int = 2,
    stream_progress: bool = False,
) -> SupervisorState:
    """
    Human gate **step 1**: run ``init`` → ``divide_work`` → ``plan``, then write
    ``outputs/plan_for_review.md`` and ``.pipeline/plan_gate_<thread>.json``.

    Edit the markdown, then call :func:`resume_supervisor_post_plan` (CLI: ``supervisor-resume-plan``).
    """
    app = get_supervisor_plan_phase_app(workspace)
    config = {"configurable": {"thread_id": thread_id}}
    initial = _initial_supervisor_state(
        workspace,
        user_goal=user_goal,
        goal_preset=goal_preset,
        use_openclaw_after_plan=use_openclaw_after_plan,
        openclaw_tool=openclaw_tool,
        openclaw_args_json=openclaw_args_json,
        llm_provider=llm_provider,
        user_statements_json=user_statements_json,
        use_semantic_division=use_semantic_division,
        openclaw_per_chunk=openclaw_per_chunk,
        max_revision_rounds=max_revision_rounds,
        supervisor_run_mode="full",
    )
    if stream_progress:
        out = _stream_invoke_updates(app, initial, config)
    else:
        raw = app.invoke(initial, config)
        out = dict(raw) if isinstance(raw, dict) else {}
    if not out.get("error"):
        ws = workspace.resolve()
        pr, bp = write_plan_review_artifacts(workspace, thread_id, out)
        out["plan_gate_review_path"] = str(pr.relative_to(ws))
        out["plan_gate_bundle_path"] = str(bp.relative_to(ws))
    out["plan_gate_paused"] = True
    _emit_project_metrics(workspace)
    return out


def resume_supervisor_post_plan(
    workspace: Path,
    *,
    thread_id: str,
    plan_file: Path | None = None,
    marathon: bool = False,
    stream_progress: bool = False,
    exec_thread_suffix: str = "exec",
) -> SupervisorState:
    """
    Human gate **step 2**: load bundle + edited plan, run ``balance_context`` → … → ``END``.
    """
    app = get_supervisor_post_plan_app(workspace, marathon=marathon)
    exec_tid = f"{thread_id}-{exec_thread_suffix}"
    config = {"configurable": {"thread_id": exec_tid}}
    initial = build_resume_initial_state(workspace, thread_id, plan_file=plan_file)
    if stream_progress:
        st = _stream_invoke_updates(app, initial, config)
        _emit_project_metrics(workspace)
        return st
    raw = app.invoke(initial, config)
    _emit_project_metrics(workspace)
    return dict(raw) if isinstance(raw, dict) else {}


def run_supervisor_prep_phase_one(
    workspace: Path,
    *,
    thread_id: str,
    user_goal: str,
    goal_preset: str,
    use_openclaw_after_plan: bool,
    openclaw_tool: str,
    openclaw_args_json: str,
    llm_provider: str | None = None,
    user_statements_json: str = "",
    use_semantic_division: bool = True,
    openclaw_per_chunk: bool = False,
    max_revision_rounds: int = 2,
    stream_progress: bool = False,
) -> SupervisorState:
    """
    Prep gate **step 1**: ``init`` → ``prep_strategic_plan`` → END, then write strategy + Q&A + bundle.
    Edit ``outputs/human_input_answers.md``, then :func:`resume_supervisor_after_prep`.
    """
    app = get_supervisor_prep_phase_one_app(workspace)
    config = {"configurable": {"thread_id": thread_id}}
    initial = _initial_supervisor_state(
        workspace,
        user_goal=user_goal,
        goal_preset=goal_preset,
        use_openclaw_after_plan=use_openclaw_after_plan,
        openclaw_tool=openclaw_tool,
        openclaw_args_json=openclaw_args_json,
        llm_provider=llm_provider,
        user_statements_json=user_statements_json,
        use_semantic_division=use_semantic_division,
        openclaw_per_chunk=openclaw_per_chunk,
        max_revision_rounds=max_revision_rounds,
        supervisor_run_mode="full",
    )
    if stream_progress:
        out = _stream_invoke_updates(app, initial, config)
    else:
        raw = app.invoke(initial, config)
        out = dict(raw) if isinstance(raw, dict) else {}
    if not out.get("error"):
        ws = workspace.resolve()
        write_prep_artifacts(workspace, thread_id, out)
        out["prep_gate_strategy_path"] = str(prep_strategy_path(ws).relative_to(ws))
        out["prep_gate_request_path"] = str(prep_request_path(ws).relative_to(ws))
        out["prep_gate_answers_path"] = str(prep_answers_path(ws).relative_to(ws))
    out["prep_gate_paused"] = True
    _emit_project_metrics(workspace)
    return out


def resume_supervisor_after_prep(
    workspace: Path,
    *,
    thread_id: str,
    marathon: bool = False,
    stream_progress: bool = False,
    prep_thread_suffix: str = "prepflow",
) -> SupervisorState:
    """
    Prep gate **step 2**: character + arc memory passes, then full supervisor tail (divide → … → END).
    """
    ws = workspace.resolve()
    app = get_supervisor_post_prep_app(workspace, marathon=marathon)
    exec_tid = f"{thread_id}-{prep_thread_suffix}"
    config = {"configurable": {"thread_id": exec_tid}}
    initial = build_prep_resume_initial_state(workspace, thread_id)
    if not prep_strategy_path(ws).is_file():
        raise FileNotFoundError(f"missing {prep_strategy_path(ws)}; run prep phase one first")
    if not prep_answers_path(ws).is_file():
        raise FileNotFoundError(f"missing {prep_answers_path(ws)}; create it (stub is created in step 1)")
    if stream_progress:
        st = _stream_invoke_updates(app, initial, config)
        _emit_project_metrics(workspace)
        return st
    raw = app.invoke(initial, config)
    _emit_project_metrics(workspace)
    return dict(raw) if isinstance(raw, dict) else {}


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
    user_statements_json: str = "",
    use_semantic_division: bool = True,
    openclaw_per_chunk: bool = False,
    max_revision_rounds: int = 2,
    sleep_s: float = 0.0,
    heartbeat_path: Path | None = None,
    on_step: Callable[[SupervisorState, list[str]], None] | None = None,
) -> SupervisorState:
    """
    Same orchestration as ``run_supervisor``, but stops after each ``edit_chunk`` so work
    is checkpointed to SQLite every chunk. Re-run with the same ``thread_id`` to resume.
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
        user_statements_json=user_statements_json,
        use_semantic_division=use_semantic_division,
        openclaw_per_chunk=openclaw_per_chunk,
        max_revision_rounds=max_revision_rounds,
        supervisor_run_mode="marathon",
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
            "verification_passed": values.get("verification_passed"),
            "revision_count": values.get("revision_count"),
            "last_logs": (values.get("log") or [])[-8:],
        }
        heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        heartbeat_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    snap0 = app.get_state(config)
    vals0 = dict(getattr(snap0, "values", None) or {})
    pending0 = list(getattr(snap0, "next", ()) or ())
    if vals0 and not pending0:
        _emit_project_metrics(workspace)
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

    _emit_project_metrics(workspace)
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
    user_statements_json: str = "",
    use_semantic_division: bool = True,
    openclaw_per_chunk: bool = False,
    max_revision_rounds: int = 2,
) -> SupervisorState:
    """
    Run exactly one LangGraph ``invoke`` for the marathon (interrupt-after-edit) app.
    """
    app = get_supervisor_app(workspace, marathon=True)
    config = {"configurable": {"thread_id": thread_id}}
    snap0 = app.get_state(config)
    vals0 = dict(getattr(snap0, "values", None) or {})
    pending0 = list(getattr(snap0, "next", ()) or ())

    if vals0 and not pending0:
        _emit_project_metrics(workspace)
        return vals0

    initial = _initial_supervisor_state(
        workspace,
        user_goal=user_goal,
        goal_preset=goal_preset,
        use_openclaw_after_plan=use_openclaw_after_plan,
        openclaw_tool=openclaw_tool,
        openclaw_args_json=openclaw_args_json,
        llm_provider=llm_provider,
        user_statements_json=user_statements_json,
        use_semantic_division=use_semantic_division,
        openclaw_per_chunk=openclaw_per_chunk,
        max_revision_rounds=max_revision_rounds,
        supervisor_run_mode="marathon",
    )

    if not pending0:
        app.invoke(initial, config)
    else:
        app.invoke(None, config)

    snap1 = app.get_state(config)
    vals1 = dict(getattr(snap1, "values", None) or {})
    _emit_project_metrics(workspace)
    return vals1
