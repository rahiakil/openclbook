from __future__ import annotations

from typing import Any, TypedDict


class ChunkRecord(TypedDict, total=False):
    id: str
    path: str
    original: str
    proposed: str
    status: str  # pending | edited | error


class SupervisorState(TypedDict, total=False):
    """Global orchestration state (checkpointed per thread_id)."""

    workspace: str
    llm_provider: str
    user_goal: str
    goal_preset: str
    chunks: list[ChunkRecord]
    chunk_index: int
    plan_markdown: str
    plan_thinking: str
    thinking_trace: list[str]
    log: list[str]
    error: str
    staging_path: str
    openclaw_tool: str
    openclaw_args_json: str
    openclaw_last_result: Any
    use_openclaw_after_plan: bool


def log_append(state: SupervisorState, msg: str) -> list[str]:
    logs = list(state.get("log") or [])
    logs.append(msg)
    return logs
