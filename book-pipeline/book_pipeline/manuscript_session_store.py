from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from book_pipeline.manuscript_lab import comments_to_goal_block


def sessions_dir(workspace: Path) -> Path:
    d = (workspace / ".pipeline" / "manuscript_sessions").resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


def session_path(workspace: Path, session_id: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9._-]", "", session_id)[:128]
    if not safe:
        raise ValueError("invalid session_id")
    return sessions_dir(workspace) / f"{safe}.json"


def persist_session(workspace: Path, session: dict[str, Any]) -> None:
    """Write full manuscript lab session to workspace disk (survives UI restart)."""
    sid = str(session.get("id") or "").strip()
    if not sid:
        raise ValueError("session missing id")
    path = session_path(workspace, sid)
    path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")


def load_session(workspace: Path, session_id: str) -> dict[str, Any] | None:
    path = session_path(workspace, session_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def list_session_meta(workspace: Path, *, limit: int = 30) -> list[dict[str, Any]]:
    """Newest first; light metadata from JSON (read full file for simplicity, cap limit)."""
    root = sessions_dir(workspace)
    files = sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    out: list[dict[str, Any]] = []
    for p in files:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        sid = str(data.get("id") or p.stem)
        out.append(
            {
                "session_id": sid,
                "filename": data.get("filename", ""),
                "updated": p.stat().st_mtime,
                "word_count": len(str(data.get("text") or "").split()),
                "comment_count": len(data.get("comments") or []),
            }
        )
    return out


def merge_manuscript_goal_text(
    workspace: Path,
    user_goal: str,
    manuscript_session_id: str | None,
    include_manuscript_notes: bool,
    *,
    extra_sessions: dict[str, dict] | None = None,
) -> str:
    """
    Combine the UI goal with manuscript-lab chunk notes + structure digest for ``run_supervisor``.

    ``extra_sessions`` is an optional in-memory map (e.g. UI server cache) keyed by session id.
    """
    g = (user_goal or "").strip()
    sid = (manuscript_session_id or "").strip()
    if not sid or not include_manuscript_notes:
        return g
    s = load_session(workspace, sid)
    if not s and extra_sessions:
        s = extra_sessions.get(sid)
    if not s:
        return g
    gn = str(s.get("global_note") or "").strip()
    if gn:
        block_gn = (
            "[Global directive — apply across the whole manuscript; preserve continuity, "
            "character arcs, and facts unless explicitly overridden.]\n"
            + gn
        )
        g = (g + "\n\n" + block_gn).strip() if g else block_gn
    b = comments_to_goal_block(list(s.get("comments") or []))
    if b:
        g = (g + "\n\n" + b).strip() if g else b
    st = (s.get("structure_markdown") or "").strip()
    if st:
        g = (g + "\n\n## Ollama structure digest\n" + st[:12_000]).strip()
    return g


def commit_manuscript_session(
    workspace: Path,
    session: dict[str, Any],
    *,
    mode: str,
) -> dict[str, Any]:
    """
    Write manuscript text into the book workspace for the LangGraph supervisor.

    - ``draft``: ``manuscript/draft.md`` (single file; supervisor uses this when no sections).
    - ``sections``: ``sections/upload-*.md`` via ``write_sections``.
    Always writes ``manuscript/PIPELINE_CONTEXT.md`` (chunk notes + optional structure digest).
    """
    from book_pipeline.ingest import write_sections

    mode_n = (mode or "draft").strip().lower()
    text = str(session.get("text") or "").strip()
    if not text:
        raise ValueError("empty manuscript text")

    written: list[str] = []

    if mode_n == "draft":
        md = workspace / "manuscript"
        md.mkdir(parents=True, exist_ok=True)
        dest = md / "draft.md"
        dest.write_text(str(session.get("text") or ""), encoding="utf-8")
        written.append(str(dest.relative_to(workspace)))
    elif mode_n == "sections":
        chunks = list(session.get("chunks") or [])
        pairs: list[tuple[str, str]] = []
        for c in chunks:
            i = int(c.get("id", 0))
            body = str(c.get("text") or "")
            pairs.append((f"upload-part-{i + 1:03d}", body))
        if not pairs:
            pairs = [("upload-full", str(session.get("text") or ""))]
        sec = workspace / "sections"
        paths = write_sections(sec, pairs, prefix="upload")
        written.extend(str(p.relative_to(workspace)) for p in paths)
    else:
        raise ValueError("mode must be 'draft' or 'sections'")

    notes = comments_to_goal_block(list(session.get("comments") or []))
    structure = str(session.get("structure_markdown") or "").strip()
    global_note = str(session.get("global_note") or "").strip()
    ctx = workspace / "manuscript" / "PIPELINE_CONTEXT.md"
    ctx.parent.mkdir(parents=True, exist_ok=True)
    parts: list[str] = ["# Pipeline context (manuscript lab)\n\n"]
    parts.append("## Global directive\n\n")
    parts.append(global_note if global_note else "_(none)_\n")
    parts.append("\n\n## Chunk notes for supervisor\n\n")
    parts.append(notes if notes else "_(none)_\n")
    if structure:
        parts.append("\n\n## Ollama structure digest\n\n")
        parts.append(structure[:24_000])
        if len(structure) > 24_000:
            parts.append("\n\n_(truncated)_\n")
    ctx.write_text("".join(parts), encoding="utf-8")
    written.append(str(ctx.relative_to(workspace)))

    return {"mode": mode_n, "paths": written}
