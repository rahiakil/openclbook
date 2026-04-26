"""Helpers for supervisor orchestration: statements, chapter split, JSON extraction."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from book_pipeline.supervisor.state import ChunkRecord


def coalesce_size_split_parts(chunks: list[dict[str, Any]]) -> tuple[str, str] | None:
    """
    If every chunk path is ``<same_base>#part-NNN`` (init oversized split), return
    ``(concatenated_text, base_path)`` for semantic chapter division.
    """
    if len(chunks) < 2:
        return None
    bases: set[str] = set()
    nums: list[tuple[int, dict[str, Any]]] = []
    for c in chunks:
        p = str(c.get("path") or "").replace("\\", "/")
        m = re.search(r"^(.+)#part-(\d+)$", p)
        if not m:
            return None
        bases.add(m.group(1))
        nums.append((int(m.group(2)), c))
    if len(bases) != 1:
        return None
    base_path = bases.pop()
    nums.sort(key=lambda x: x[0])
    full = "".join((c.get("original") or "") for _, c in nums)
    return (full, base_path)


def parse_user_statements(user_goal: str, statements_json: str) -> list[str]:
    """Prefer explicit JSON list; else split goal on blank lines; else whole goal."""
    raw = (statements_json or "").strip()
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                out = [str(x).strip() for x in data if str(x).strip()]
                if out:
                    return out
        except (json.JSONDecodeError, TypeError):
            pass
    g = (user_goal or "").strip()
    if not g:
        return ["Improve clarity, pacing, and voice while preserving plot."]
    parts = [p.strip() for p in re.split(r"\n\s*\n+", g) if p.strip()]
    if len(parts) >= 2:
        return parts
    lines = [ln.strip() for ln in g.splitlines() if ln.strip()]
    if len(lines) >= 3 and all(
        lines[i].startswith(("- ", "* ", f"{i+1}.", f"{i+1})")) or len(lines[i]) < 200 for i in range(min(3, len(lines)))
    ):
        return [ln.lstrip("-* ").strip() for ln in lines if ln.strip()]
    return [g]


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Parse first JSON object from model output (allows markdown fences)."""
    t = (text or "").strip()
    if "```" in t:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", t, re.IGNORECASE)
        if m:
            t = m.group(1).strip()
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    i = t.find("{")
    j = t.rfind("}")
    if i >= 0 and j > i:
        try:
            obj = json.loads(t[i : j + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def split_manuscript_into_chapters(
    full_text: str,
    n_chapters: int,
    titles: list[str] | None,
    base_path: str,
) -> list[ChunkRecord]:
    """Split ``full_text`` into ``n_chapters`` contiguous spans (equal length, deterministic)."""
    text = full_text or ""
    n = max(2, min(int(n_chapters), 40))
    L = len(text)
    if L < 500:
        return [
            {
                "id": str(uuid.uuid4())[:8],
                "path": base_path,
                "original": text,
                "proposed": "",
                "status": "pending",
            }
        ]
    out: list[ChunkRecord] = []
    tlist = list(titles or [])
    for i in range(n):
        a = (i * L) // n
        b = ((i + 1) * L) // n if i < n - 1 else L
        if b <= a:
            b = min(L, a + 1)
        chunk_text = text[a:b].strip()
        if not chunk_text:
            continue
        title = tlist[i] if i < len(tlist) and tlist[i] else f"Chapter {i + 1}"
        safe = re.sub(r"[^\w\-]+", "-", title.lower()).strip("-")[:48] or f"ch{i + 1:02d}"
        out.append(
            {
                "id": str(uuid.uuid4())[:8],
                "path": f"{base_path}#chapter-{i + 1:02d}-{safe}",
                "original": chunk_text,
                "proposed": "",
                "status": "pending",
                "chapter_title": title,
            }
        )
    if not out:
        return [
            {
                "id": str(uuid.uuid4())[:8],
                "path": base_path,
                "original": text,
                "proposed": "",
                "status": "pending",
            }
        ]
    return out
