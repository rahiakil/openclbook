from __future__ import annotations

from typing import Any, TypedDict


class ChunkRecord(TypedDict, total=False):
    id: str
    path: str
    original: str
    proposed: str
    status: str  # pending | edited | error
    chapter_title: str


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
    # Orchestration (semantic division, verify loop, parallel edits)
    from_multi_file_sections: bool
    user_statements: list[str]
    user_statements_json: str
    use_semantic_division: bool
    openclaw_per_chunk: bool
    revision_count: int
    max_revision_rounds: int
    verification_passed: bool
    verification_notes: str
    verification_violations: list[Any]
    orchestration_feedback: str
    merged_preview: str  # last merged body for verify (avoid re-read race)
    supervisor_run_mode: str  # "full" | "marathon" — for log diagrams only
    # Prep gate (--prep-gate / --prep-resume): strategy + human answers + memory passes
    prep_strategy_markdown: str
    prep_human_answers: str
    # Visual planning (scene needs + image prompt artifacts)
    scene_needs_markdown: str
    scene_needs_json: list[Any]
    image_prompts_markdown: str
    image_prompts_json: list[Any]
    image_prompt_verification: str
    final_review_markdown: str


def log_append(state: SupervisorState, msg: str) -> list[str]:
    logs = list(state.get("log") or [])
    logs.append(msg)
    return logs
