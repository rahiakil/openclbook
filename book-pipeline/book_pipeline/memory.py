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
