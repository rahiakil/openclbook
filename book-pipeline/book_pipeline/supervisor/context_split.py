"""Split oversized chunks so edit prompts stay within a configurable context budget (LangGraph, no silent drop)."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from book_pipeline.manuscript_lab import chunk_manuscript
from book_pipeline.supervisor.state import ChunkRecord

if TYPE_CHECKING:
    from book_pipeline.config import Settings


def _estimate_edit_payload_chars(
    *,
    memory_bundle: str,
    plan_excerpt: str,
    goal: str,
    statements_block: str,
    feedback_block: str,
    preset_extra: str,
    chunk_original: str,
) -> int:
    """Rough character budget for the edit user message + templates."""
    overhead = 3500
    return (
        overhead
        + len(memory_bundle)
        + len(plan_excerpt)
        + len(goal)
        + len(statements_block)
        + len(feedback_block)
        + len(preset_extra)
        + len(chunk_original)
    )


def rebalance_chunks_for_context_budget(
    chunks: list[ChunkRecord],
    settings: "Settings",
    *,
    memory_bundle: str,
    goal: str,
    statements_block: str,
    feedback_block: str,
    preset_extra: str,
    plan_excerpt: str,
) -> tuple[list[ChunkRecord], list[str]]:
    """
    Subdivide chunks whose estimated edit payload exceeds ``settings.edit_context_budget_chars()``.

    Repeats wave splits until each piece fits or ``chunk_manuscript`` cannot split further.
    """
    budget = settings.edit_context_budget_chars()
    logs: list[str] = []
    out: list[ChunkRecord] = []
    min_target = max(1200, settings.supervisor_max_chunk_chars // 8)

    for c in chunks:
        pieces: list[ChunkRecord] = [dict(c)]
        while True:
            next_wave: list[ChunkRecord] = []
            changed = False
            for cur in pieces:
                orig = str(cur.get("original") or "")
                base_path = str(cur.get("path") or "chunk").replace("\\", "/")
                ch_title = (str(cur.get("chapter_title") or "")).strip()
                est = _estimate_edit_payload_chars(
                    memory_bundle=memory_bundle,
                    plan_excerpt=plan_excerpt,
                    goal=goal,
                    statements_block=statements_block,
                    feedback_block=feedback_block,
                    preset_extra=preset_extra,
                    chunk_original=orig,
                )
                if est <= budget or len(orig) <= min_target * 2:
                    next_wave.append(cur)
                    continue
                target = max(min_target, len(orig) // 2)
                raw_parts = chunk_manuscript(orig, target_chars=target)
                if len(raw_parts) <= 1:
                    next_wave.append(cur)
                    if est > budget:
                        logs.append(
                            f"context_split: cannot subdivide {base_path} further (~{est} chars vs budget {budget})"
                        )
                    continue
                changed = True
                logs.append(
                    f"context_split: split {base_path} → {len(raw_parts)} slices (budget {budget}, was ~{est})"
                )
                for p in raw_parts:
                    pid = str(uuid.uuid4())[:8]
                    n = int(p["id"]) + 1
                    piece = str(p.get("text") or "")
                    subpath = f"{base_path}#ctx-{n:03d}"
                    rec: ChunkRecord = {
                        "id": pid,
                        "path": subpath,
                        "original": piece,
                        "proposed": "",
                        "status": "pending",
                    }
                    if ch_title:
                        rec["chapter_title"] = ch_title
                    next_wave.append(rec)
            pieces = next_wave
            if not changed:
                break
        out.extend(pieces)

    return out, logs
