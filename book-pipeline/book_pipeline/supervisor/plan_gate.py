"""Two-step human gate: run through ``plan``, edit ``outputs/plan_for_review.md``, then resume execution."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from book_pipeline.supervisor.orchestration import parse_user_statements
from book_pipeline.supervisor.state import SupervisorState

PLAN_REVIEW_FILENAME = "plan_for_review.md"


def plan_bundle_path(workspace: Path, thread_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in thread_id.strip())[:120]
    return (workspace / ".pipeline" / f"plan_gate_{safe}.json").resolve()


def plan_review_path(workspace: Path) -> Path:
    return (workspace / "outputs" / PLAN_REVIEW_FILENAME).resolve()


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
        "plan_markdown",
        "plan_thinking",
        "orchestration_feedback",
        "supervisor_run_mode",
        "chunk_index",
        "error",
        "log",
    )


def snapshot_plan_gate_bundle(state: SupervisorState) -> dict[str, Any]:
    """Serializable subset for post-plan resume (chunks + goals + flags)."""
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


def write_plan_review_artifacts(workspace: Path, thread_id: str, state: SupervisorState) -> tuple[Path, Path]:
    """Write editable plan markdown and JSON bundle for ``supervisor-resume-plan``."""
    ws = workspace.resolve()
    (ws / "outputs").mkdir(parents=True, exist_ok=True)
    (ws / ".pipeline").mkdir(parents=True, exist_ok=True)
    plan_md = state.get("plan_markdown") or ""
    banner = (
        "<!-- Human gate: edit this file, then run:\n"
        f"     python -m book_pipeline supervisor-resume-plan --workspace {ws} "
        f"--thread-id {thread_id}\n"
        "     (or --project-id …).\n"
        "  You may change the **orchestration plan** and/or the **creative brief**:\n"
        "  • YAML front matter at top (--- key: value … closing ---).\n"
        "  • Or here-doc blocks: <<<USER_GOAL>>> … <<<END>>>, <<<PRESET>>> … <<<END>>>,\n"
        "    <<<USER_STATEMENTS>>> lines <<<END>>> (blocks are stripped from plan_markdown).\n"
        "  Example pivot: rewrite as a Netflix season in 2300 AD; swap every character's role. -->\n\n"
    )
    pr = plan_review_path(ws)
    pr.write_text(banner + plan_md, encoding="utf-8")
    bundle = snapshot_plan_gate_bundle(state)
    bundle["plan_gate_thread_id"] = thread_id
    bp = plan_bundle_path(ws, thread_id)
    bp.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    meta = {
        "thread_id": thread_id,
        "plan_review": str(pr.relative_to(ws)),
        "bundle": str(bp.relative_to(ws)),
    }
    (ws / ".pipeline" / "plan_gate_last.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return pr, bp


def _strip_first_html_comment(text: str) -> str:
    if "<!--" not in text or "-->" not in text:
        return text
    start = text.find("<!--")
    end = text.find("-->", start)
    if start < 0 or end < 0:
        return text
    return (text[:start] + text[end + 3 :]).lstrip()


def _parse_yaml_front_matter(text: str) -> tuple[str, dict[str, Any]]:
    """
    If ``text`` starts with ``---`` YAML front matter, return ``(body_after, meta)``.
    ``meta`` may include ``user_goal``, ``goal_preset`` / ``preset``, ``user_statements``.
    """
    t = text.lstrip("\ufeff").lstrip()
    if not t.startswith("---"):
        return text, {}
    lines = t.splitlines()
    if not lines or lines[0].strip() != "---":
        return text, {}
    end_i: int | None = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_i = i
            break
    if end_i is None:
        return text, {}
    raw_yaml = "\n".join(lines[1:end_i])
    body = "\n".join(lines[end_i + 1 :]).lstrip("\n")
    try:
        meta = yaml.safe_load(raw_yaml) or {}
    except yaml.YAMLError:
        return text, {}
    if not isinstance(meta, dict):
        return text, {}
    out: dict[str, Any] = {}
    if meta.get("user_goal") is not None:
        out["user_goal"] = str(meta["user_goal"]).strip()
    gp = meta.get("goal_preset") or meta.get("preset")
    if gp is not None:
        out["goal_preset"] = str(gp).strip()
    if meta.get("user_statements") is not None:
        us = meta["user_statements"]
        if isinstance(us, list):
            out["user_statements"] = [str(x).strip() for x in us if str(x).strip()]
        elif isinstance(us, str):
            out["user_statements"] = [ln.strip() for ln in us.splitlines() if ln.strip()]
    if meta.get("user_statements_json") is not None:
        out["user_statements_json"] = str(meta["user_statements_json"]).strip()
    return body, out


_BLOCK = re.compile(
    r"<<<(?P<tag>USER_GOAL|PRESET|GOAL_PRESET|USER_STATEMENTS_JSON|USER_STATEMENTS)\s*>>>\s*\n?"
    r"(?P<body>.*?)"
    r"\s*<<<END\s*>>>\s*",
    re.DOTALL | re.IGNORECASE,
)


def _parse_here_pipeline_blocks(text: str) -> tuple[str, dict[str, Any]]:
    """
    Freeform blocks (good for long natural-language pivots), e.g.::

        <<<USER_GOAL>>>
        Rewrite the entire script as a Netflix season set in 2300 AD.
        Swap each character's role with another ensemble member by episode 4.
        <<<END>>>

        <<<PRESET>>>
        netflix_script
        <<<END>>>
    """
    meta: dict[str, Any] = {}
    cur = text
    while True:
        m = _BLOCK.search(cur)
        if not m:
            break
        tag = m.group("tag").upper().replace("GOAL_PRESET", "PRESET")
        body = (m.group("body") or "").strip()
        if tag == "USER_GOAL" and body:
            meta["user_goal"] = body
        elif tag == "PRESET" and body:
            meta["goal_preset"] = body.splitlines()[0].strip()
        elif tag == "USER_STATEMENTS_JSON" and body:
            meta["user_statements_json"] = body
        elif tag == "USER_STATEMENTS" and body:
            lines = [ln.strip().lstrip("-*• ").strip() for ln in body.splitlines() if ln.strip()]
            if lines:
                meta["user_statements"] = lines
        cur = cur[: m.start()] + cur[m.end() :]
    return cur.strip(), meta


def _merge_pivot_meta(into: SupervisorState, meta: dict[str, Any]) -> None:
    """Apply parsed overrides onto resume state (goal, preset, statements)."""
    if not meta:
        return
    if meta.get("user_goal"):
        into["user_goal"] = str(meta["user_goal"]).strip()
    if meta.get("goal_preset"):
        into["goal_preset"] = str(meta["goal_preset"]).strip()
    if meta.get("user_statements_json"):
        into["user_statements_json"] = str(meta["user_statements_json"]).strip()
        into["user_statements"] = parse_user_statements(
            str(into.get("user_goal") or ""),
            into["user_statements_json"],
        )
    elif meta.get("user_statements"):
        into["user_statements"] = list(meta["user_statements"])


def parse_plan_review_for_resume(raw: str) -> tuple[str, dict[str, Any]]:
    """
    Strip banner comment, then apply (in order) YAML front matter and ``<<<TAG>>>`` blocks.

    Returns ``(plan_markdown_body, pivot_meta)`` — ``pivot_meta`` keys overlap :func:`_merge_pivot_meta`.
    """
    text = _strip_first_html_comment(raw)
    pivot: dict[str, Any] = {}
    body, ymeta = _parse_yaml_front_matter(text)
    pivot.update(ymeta)
    body2, bmeta = _parse_here_pipeline_blocks(body)
    pivot.update(bmeta)
    return body2.strip(), pivot


def load_plan_gate_bundle(workspace: Path, thread_id: str) -> dict[str, Any]:
    p = plan_bundle_path(workspace, thread_id)
    if not p.is_file():
        raise FileNotFoundError(f"missing plan gate bundle: {p} (run with --plan-gate first)")
    return json.loads(p.read_text(encoding="utf-8"))


def _path_under_workspace(path: Path, workspace: Path) -> bool:
    path = path.resolve()
    base = workspace.resolve()
    try:
        return path.is_relative_to(base)
    except AttributeError:
        return str(path).startswith(str(base) + "/")


def build_resume_initial_state(
    workspace: Path,
    thread_id: str,
    *,
    plan_file: Path | None = None,
) -> SupervisorState:
    """
    Merge frozen bundle with the edited plan file.

    The plan file may **redefine the creative brief** (not only the orchestration plan):

    - **YAML front matter** at the very top (after the HTML banner)::

        ---
        user_goal: |
          Rewrite as a full Netflix-style season set in 2300 AD; swap every character's role.
        goal_preset: netflix_script
        user_statements:
          - Dorothy is now the cynical fixer; Tin Man leads the resistance.
        ---

    - **Here-doc blocks** anywhere in the file (removed from ``plan_markdown`` so they do not duplicate)::

        <<<USER_GOAL>>>
        Your long natural-language pivot…
        <<<END>>>

        <<<PRESET>>>
        netflix_script
        <<<END>>>

        <<<USER_STATEMENTS>>>
        - bullet requirement one
        - bullet two
        <<<END>>>
    """
    ws = workspace.resolve()
    bundle = load_plan_gate_bundle(ws, thread_id)
    pf = (plan_file or plan_review_path(ws)).expanduser().resolve()
    if not _path_under_workspace(pf, ws):
        raise ValueError("plan_file must be inside workspace")
    raw = pf.read_text(encoding="utf-8", errors="replace") if pf.is_file() else ""
    plan_body, pivot = parse_plan_review_for_resume(raw)
    st: SupervisorState = {k: v for k, v in bundle.items() if k != "plan_gate_thread_id"}
    st["plan_markdown"] = plan_body
    st["workspace"] = str(ws)
    st["supervisor_run_mode"] = "full"
    st.setdefault("revision_count", int(bundle.get("revision_count") or 0))
    st.setdefault("max_revision_rounds", int(bundle.get("max_revision_rounds") or 2))
    st.setdefault("chunk_index", 0)
    st.setdefault("log", list(bundle.get("log") or []))
    _merge_pivot_meta(st, pivot)
    # New goal alone: refresh rubric statements from the pivot (drop stale bundle heuristics).
    if pivot.get("user_goal") and "user_statements_json" not in pivot and "user_statements" not in pivot:
        st["user_statements"] = parse_user_statements(str(st.get("user_goal") or ""), "")
    return st
