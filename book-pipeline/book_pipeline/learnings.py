"""Append-only project learnings log (story arc, requirements, verification outcomes)."""

from __future__ import annotations

import json
import textwrap
import time
from pathlib import Path
from typing import Any

from book_pipeline.config import Settings


def _verification_human_lines(
    *,
    verification_passed: bool | None,
    verification_notes: str,
    verification_violations: list[Any],
) -> str:
    """Readable bullets for learnings: confirmations on pass, issues on fail."""
    notes = (verification_notes or "").strip()
    viol = list(verification_violations or [])
    lines: list[str] = []

    if viol:
        for v in viol:
            if isinstance(v, dict):
                idx = v.get("statement_index", "?")
                issue = (v.get("issue") or "").strip()
                lines.append(f"- **Issue** (statement {idx}): {issue or '(no description)'}")
            else:
                lines.append(f"- {v!s}")
        return "\n".join(lines) if lines else "- *(violations list was empty or malformed)*"

    if not notes or notes == "(none)":
        if verification_passed is False:
            return "- **FAIL** but verifier returned no summary or violations (check Ollama logs / JSON parse)."
        return "- *(No verifier summary text; e.g. verify was skipped or model returned empty `summary`.)*"

    # Multi-pass verify joins summaries with " | "
    parts = [p.strip() for p in notes.split("|") if p.strip()]
    if len(parts) > 1:
        for i, p in enumerate(parts, start=1):
            lines.append(f"- **Pass {i}:** {p}")
    else:
        lines.append(f"- {notes}")
    return "\n".join(lines)


def project_learnings_path(settings: Settings) -> Path:
    return (settings.memory_root / "agentic" / "project_learnings.md").resolve()


def append_supervisor_run_learnings(
    settings: Settings,
    *,
    user_goal: str,
    goal_preset: str,
    user_statements: list[str],
    verification_passed: bool | None = None,
    verification_notes: str,
    verification_violations: list[Any],
    revision_count: int,
    staging_path: str,
    n_chunks: int,
    error: str | None,
) -> Path:
    """Append a markdown section documenting this run (called from LangGraph)."""
    path = project_learnings_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    viol_txt = json.dumps(verification_violations, ensure_ascii=False, indent=2)
    stmts = "\n".join(f"- {s}" for s in user_statements) if user_statements else "(none)"
    vhuman = _verification_human_lines(
        verification_passed=verification_passed,
        verification_notes=verification_notes,
        verification_violations=verification_violations,
    )
    block = textwrap.dedent(
        f"""

        ---

        ## Run {stamp}

        - **preset**: {goal_preset}
        - **staging**: `{staging_path}`
        - **chunks**: {n_chunks}
        - **revision_cycle**: {revision_count}
        - **verification_passed**: {verification_passed}
        - **verification_notes**: {verification_notes or "(none)"}
        - **error**: {error or "(none)"}

        ### User goal

        {user_goal.strip() or "(empty)"}

        ### User statements / requirements

        {stmts}

        ### Verification (human-readable)

        {vhuman}

        ### Verification violations (raw JSON)

        ```json
        {viol_txt}
        ```
        """
    ).strip()
    prev = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    sep = "\n\n" if prev and not prev.endswith("\n") else ""
    path.write_text(prev + sep + block + "\n", encoding="utf-8")
    return path
