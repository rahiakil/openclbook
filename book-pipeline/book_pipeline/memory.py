from __future__ import annotations

from pathlib import Path

from book_pipeline.config import Settings


def load_memory_markdown(settings: Settings) -> str:
    """Concatenate all .md under .memory (character sheets, research notes, etc.)."""
    root = settings.memory_root
    if not root.is_dir():
        return ""

    chunks: list[str] = []
    for path in sorted(root.rglob("*.md")):
        if path.is_file():
            rel = path.relative_to(root)
            try:
                text = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if text:
                chunks.append(f"### File: {rel}\n\n{text}")
    return "\n\n".join(chunks).strip()


def load_editor_memory_bundle(settings: Settings) -> str:
    """
    Memory seen by planner and chunk editors: prior **project learnings** first, then all other
    ``.memory/**/*.md`` except ``agentic/project_learnings.md`` (so it is not duplicated).
    """
    root = settings.memory_root
    blocks: list[str] = []
    learn = root / "agentic" / "project_learnings.md"
    if learn.is_file():
        try:
            t = learn.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            t = ""
        if t:
            blocks.append("### Project learnings (prior runs, arc, requirements)\n\n" + t)
    if not root.is_dir():
        return "\n\n".join(blocks).strip()

    rest: list[str] = []
    for path in sorted(root.rglob("*.md")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if str(rel).replace("\\", "/") == "agentic/project_learnings.md":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if text:
            rest.append(f"### File: {rel}\n\n{text}")
    if rest:
        blocks.append("\n\n".join(rest))
    return "\n\n".join(blocks).strip()


def load_editor_memory_digest(
    settings: Settings,
    *,
    max_total_chars: int = 12_000,
    max_per_file: int = 2_000,
) -> str:
    """
    Short, deterministic substitute for :func:`load_editor_memory_bundle` — first project
    learnings (truncated), then each ``.memory/**/*.md`` with a per-file cap until a total cap.
    """
    preamble = (
        "### Memory digest (truncated from `.memory/`, not full character/research dumps)\n\n"
        "Use this plus USER_GOAL, USER_STATEMENTS, and PLAN for continuity.\n\n"
    )
    root = settings.memory_root
    bundle_budget = max(800, int(max_total_chars) - len(preamble))
    per_cap = max(200, int(max_per_file))
    segments: list[str] = []
    used = 0

    def add_segment(title: str, body: str, cap: int) -> None:
        nonlocal used
        if used >= bundle_budget:
            return
        room = bundle_budget - used
        cap = min(cap, room)
        if cap < 120:
            return
        txt = body.strip()
        if len(txt) > cap:
            txt = txt[: max(0, cap - 40)].rstrip() + "\n\n…(truncated)…\n"
        block = f"#### {title}\n\n{txt}\n"
        if len(block) > room:
            return
        segments.append(block)
        used += len(block)

    learn = root / "agentic" / "project_learnings.md"
    if learn.is_file():
        try:
            t = learn.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            t = ""
        if t:
            add_segment("Project learnings", t, min(4_000, per_cap * 2))

    if root.is_dir():
        for path in sorted(root.rglob("*.md")):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            rel_s = str(rel).replace("\\", "/")
            if rel_s == "agentic/project_learnings.md":
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if text:
                add_segment(f"File: {rel_s}", text, per_cap)

    if not segments:
        return preamble + "_(no memory files under `.memory/` — digest empty)_\n"

    tail = ""
    if used >= bundle_budget - 80:
        tail = "\n_(digest character budget reached; additional files may be omitted.)_\n"

    return preamble + "\n".join(segments) + tail


def load_supervisor_memory_context(settings: Settings) -> str:
    """
    Memory block for planner / balance / chunk editors.

    ``settings.supervisor_memory_context``: ``full`` | ``digest`` | ``none``.
    """
    mode = (getattr(settings, "supervisor_memory_context", "digest") or "digest").strip().lower()
    if mode in ("none", "off", "false", "0"):
        return (
            "(Memory bundle omitted: `supervisor_memory_context: none`. "
            "Use USER_GOAL, USER_STATEMENTS, and PLAN for continuity.)\n"
        )
    if mode == "full":
        return load_editor_memory_bundle(settings)
    max_total = int(getattr(settings, "supervisor_memory_digest_max_chars", 12_000) or 12_000)
    max_per = int(getattr(settings, "supervisor_memory_digest_per_file", 2_000) or 2_000)
    return load_editor_memory_digest(
        settings,
        max_total_chars=max_total,
        max_per_file=max_per,
    )


def append_research_note(settings: Settings, title: str, body: str) -> Path:
    """Append a timestamped research note (visible only under .memory)."""
    settings.research_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in title.lower())[:80]
    path = settings.research_dir / f"{safe}.md"
    from datetime import datetime

    stamp = datetime.utcnow().isoformat() + "Z"
    block = f"\n\n## {stamp}\n\n{body.strip()}\n"
    if path.exists():
        path.write_text(path.read_text(encoding="utf-8") + block, encoding="utf-8")
    else:
        path.write_text(f"# {title}\n{block}", encoding="utf-8")
    return path
