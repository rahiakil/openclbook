"""Project-scoped workspaces under ``BOOK_PIPELINE_PROJECTS_DIR`` (or default ``book-pipeline/projects``)."""

from __future__ import annotations

import os
import re
from pathlib import Path


def projects_root() -> Path:
    raw = (os.environ.get("BOOK_PIPELINE_PROJECTS_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    # book_pipeline/ -> book-pipeline/
    pkg = Path(__file__).resolve().parent.parent
    return (pkg / "projects").resolve()


def sanitize_project_id(project_id: str) -> str:
    s = (project_id or "").strip()
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", s).strip("-")[:120]
    if not safe:
        raise ValueError("project_id must contain at least one letter, digit, dot, underscore, or hyphen")
    return safe


def slug_gutendex_project_id(gut_id: int, title: str) -> str:
    """
    Stable folder name for a Gutenberg-backed project (``gut-<id>-<slugified-title>``).

    Keep in sync with ``scripts/gutendex_process_batch.py`` batch runner.
    """
    raw = re.sub(r"[^a-zA-Z0-9._-]+", "-", f"gut-{gut_id}-{title}").strip("-") or f"gut-{gut_id}"
    return sanitize_project_id(raw)


def project_workspace_path(project_id: str) -> Path:
    """Return (and create) ``<projects_root>/<sanitized_id>/``."""
    sid = sanitize_project_id(project_id)
    root = projects_root()
    root.mkdir(parents=True, exist_ok=True)
    ws = (root / sid).resolve()
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def ensure_project_layout(workspace: Path) -> Path:
    """
    If ``config.yaml`` is missing, write the default template and standard dirs.

    Does not overwrite an existing config.
    """
    from book_pipeline.project_scaffold import default_config_template

    ws = workspace.resolve()
    cfg = ws / "config.yaml"
    if not cfg.is_file():
        cfg.write_text(default_config_template(), encoding="utf-8")
    for sub in (
        "manuscript",
        "sections",
        "outputs",
        ".memory/characters",
        ".memory/research",
        ".memory/agentic",
        ".pipeline",
    ):
        (ws / sub).mkdir(parents=True, exist_ok=True)
    learn = ws / ".memory" / "agentic" / "project_learnings.md"
    if not learn.is_file():
        learn.write_text(
            "# Project learnings (auto-append)\n\n"
            "Each supervisor run appends a dated block: goals, preset, verification, "
            "and chunk summary. Edit freely to record **story arc**, **character graph** "
            "notes, and standing **edit requirements**; the planner and chunk editors "
            "see this file together with other ``.memory`` markdown.\n",
            encoding="utf-8",
        )
    return ws
