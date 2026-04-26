"""Human gate before main rewrite: strategy + Q&A markdown, then character + story-arc passes into ``.memory/``."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from book_pipeline.supervisor.state import SupervisorState

PREP_STRATEGY_FILENAME = "supervisor_prep_strategy.md"
HUMAN_PREP_REQUEST_FILENAME = "human_input_request.md"
HUMAN_PREP_ANSWERS_FILENAME = "human_input_answers.md"


def prep_strategy_path(workspace: Path) -> Path:
    return (workspace / "outputs" / PREP_STRATEGY_FILENAME).resolve()


def prep_request_path(workspace: Path) -> Path:
    return (workspace / "outputs" / HUMAN_PREP_REQUEST_FILENAME).resolve()


def prep_answers_path(workspace: Path) -> Path:
    return (workspace / "outputs" / HUMAN_PREP_ANSWERS_FILENAME).resolve()


def prep_bundle_path(workspace: Path, thread_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in thread_id.strip())[:120]
    return (workspace / ".pipeline" / f"prep_gate_{safe}.json").resolve()


def prep_plan_prerequisite_ok(workspace: Path, thread_id: str) -> tuple[bool, str]:
    """
    True when prep **phase one** has completed for ``thread_id`` (same id as ``--prep-gate``).

    Used to enforce **prep before plan-gate** when ``supervisor_enable_prep_passes`` is on in
    ``config.yaml``: ``outputs/supervisor_prep_strategy.md`` and
    ``.pipeline/prep_gate_<thread_id>.json`` must exist so each plan run is tied to a recorded prep flow.
    """
    ws = workspace.resolve()
    tid = (thread_id or "").strip()
    if not tid:
        return False, "thread_id is empty (use the same --thread-id you passed to --prep-gate)"
    sp = prep_strategy_path(ws)
    bp = prep_bundle_path(ws, tid)
    if not sp.is_file():
        return (
            False,
            f"missing {sp.relative_to(ws)} — run ingest-run with --prep-gate and this --thread-id first",
        )
    if not bp.is_file():
        return (
            False,
            f"missing {bp.relative_to(ws)} — run --prep-gate with the same --thread-id as this plan "
            "(bundle is written when prep phase one finishes)",
        )
    return True, ""


def _snapshot_keys() -> tuple[str, ...]:
    return (
        "workspace",
        "llm_provider",
        "user_goal",
        "goal_preset",
        "user_statements",
        "user_statements_json",
        "use_semantic_division",
        "openclaw_per_chunk",
        "max_revision_rounds",
        "revision_count",
        "use_openclaw_after_plan",
        "openclaw_tool",
        "openclaw_args_json",
        "chunks",
        "from_multi_file_sections",
        "chunk_index",
        "orchestration_feedback",
        "supervisor_run_mode",
        "error",
        "log",
        "prep_strategy_markdown",
    )


def snapshot_prep_bundle(state: SupervisorState) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in _snapshot_keys():
        if k not in state:
            continue
        v = state[k]
        if k == "log" and isinstance(v, list):
            out[k] = v[-40:]
        else:
            out[k] = v
    return out


def extract_human_questions_section(strategy_md: str) -> str:
    m = re.search(r"##\s*Human questions\s*([\s\S]*?)(?=\n##\s|\Z)", strategy_md, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return (
        "(The strategy file has no `## Human questions` section. If the model asked for your input "
        "elsewhere, reply under **## Answers** anyway. Otherwise write `NONE`.)\n"
    )


def write_prep_artifacts(workspace: Path, thread_id: str, state: SupervisorState) -> tuple[Path, Path, Path]:
    """Write strategy MD, human request + answers stub, and JSON bundle for ``--prep-resume``."""
    ws = workspace.resolve()
    (ws / "outputs").mkdir(parents=True, exist_ok=True)
    (ws / ".pipeline").mkdir(parents=True, exist_ok=True)
    strat = (state.get("prep_strategy_markdown") or "").strip()
    sp = prep_strategy_path(ws)
    sp.write_text(strat + ("\n" if strat and not strat.endswith("\n") else "\n"), encoding="utf-8")

    q_body = extract_human_questions_section(strat)
    banner = (
        "<!-- Prep gate: answer in `outputs/human_input_answers.md`, then run:\n"
        f"     python -m book_pipeline ingest-run --workspace {ws} --prep-resume --thread-id {thread_id} …\n"
        "  Under **## Answers**, paste your reply under each question (or write NONE).\n-->\n\n"
    )
    pr = prep_request_path(ws)
    pr.write_text(
        banner
        + "# Questions for you\n\n"
        + q_body
        + "\n\n---\n\n"
        + "Full strategy (same as `supervisor_prep_strategy.md`):\n\n"
        + strat[:60_000]
        + ("\n" if len(strat) <= 60_000 else "\n\n…(truncated in this copy; see supervisor_prep_strategy.md)…\n"),
        encoding="utf-8",
    )

    pa = prep_answers_path(ws)
    if not pa.is_file():
        pa.write_text(
            "# Your answers\n\n"
            "Paste under each question from `human_input_request.md`.\n"
            "If there are no open questions, write exactly:\n\n"
            "## Answers\n\nNONE\n",
            encoding="utf-8",
        )

    bundle = snapshot_prep_bundle(state)
    bundle["prep_gate_thread_id"] = thread_id
    bp = prep_bundle_path(ws, thread_id)
    bp.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    meta = {
        "thread_id": thread_id,
        "prep_strategy": str(sp.relative_to(ws)),
        "prep_request": str(pr.relative_to(ws)),
        "prep_answers": str(pa.relative_to(ws)),
        "bundle": str(bp.relative_to(ws)),
    }
    (ws / ".pipeline" / "prep_gate_last.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return sp, pr, bp


def _parse_answers_file(text: str) -> str:
    t = text.strip()
    if not t:
        return "(empty answers file)\n"
    return t


def build_prep_resume_initial_state(workspace: Path, thread_id: str) -> SupervisorState:
    """Load prep bundle + human answers; ready for ``prep_character_pass`` → full tail."""
    ws = workspace.resolve()
    bp = prep_bundle_path(ws, thread_id)
    if not bp.is_file():
        raise FileNotFoundError(f"prep bundle not found: {bp} (run --prep-gate first with this thread_id)")
    bundle = json.loads(bp.read_text(encoding="utf-8"))
    ap = prep_answers_path(ws)
    answers_raw = ap.read_text(encoding="utf-8", errors="replace") if ap.is_file() else ""
    answers = _parse_answers_file(answers_raw)

    st: SupervisorState = {}
    for k, v in bundle.items():
        if k == "prep_gate_thread_id":
            continue
        st[k] = v  # type: ignore[assignment]

    st["prep_human_answers"] = answers
    if prep_strategy_path(ws).is_file():
        st["prep_strategy_markdown"] = prep_strategy_path(ws).read_text(encoding="utf-8", errors="replace")
    st["workspace"] = str(ws)
    st.setdefault("log", [])
    st.setdefault("chunk_index", 0)
    st.setdefault("revision_count", int(st.get("revision_count") or 0))
    st.setdefault("orchestration_feedback", "")
    st.setdefault("plan_markdown", "")
    st.setdefault("plan_thinking", "")
    st.setdefault("thinking_trace", [])
    st.setdefault("merged_preview", "")
    st.setdefault("verification_passed", True)
    st.setdefault("verification_notes", "")
    st.setdefault("verification_violations", [])
    st["log"] = list(st.get("log") or []) + ["prep_resume: loaded bundle + human_input_answers.md"]
    return st
