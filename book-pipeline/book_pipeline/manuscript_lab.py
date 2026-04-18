from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from book_pipeline.config import Settings
from book_pipeline.llm_complete import complete_chat


def chunk_manuscript(text: str, *, target_chars: int = 6500) -> list[dict[str, Any]]:
    """Paragraph-aware chunks for UI + incremental LLM passes."""
    raw = (text or "").strip()
    if not raw:
        return []
    paras = [p.strip() for p in re.split(r"\n\s*\n", raw) if p.strip()]
    if not paras:
        paras = [raw]
    chunk_bodies: list[str] = []
    buf: list[str] = []
    size = 0
    for p in paras:
        extra = len(p) + (2 if buf else 0)
        if buf and size + extra > target_chars:
            chunk_bodies.append("\n\n".join(buf))
            buf = [p]
            size = len(p)
        else:
            buf.append(p)
            size += extra
    if buf:
        chunk_bodies.append("\n\n".join(buf))
    out: list[dict[str, Any]] = []
    for i, body in enumerate(chunk_bodies):
        out.append(
            {
                "id": i,
                "preview": (body[:280] + "…") if len(body) > 280 else body,
                "word_count": len(body.split()),
                "text": body,
            }
        )
    return out


def _merge_for_analysis(parts: list[dict[str, Any]], max_parts: int) -> list[dict[str, Any]]:
    """Merge consecutive UI chunks into fewer (larger) blocks for LLM detail passes."""
    if len(parts) <= max_parts:
        return [{"id": p["id"], "text": str(p["text"]), "source_ids": [int(p["id"])]} for p in parts]
    texts = [str(p["text"]) for p in parts]
    n = len(texts)
    k = max_parts
    size = (n + k - 1) // k
    merged: list[dict[str, Any]] = []
    for j in range(k):
        start = j * size
        if start >= n:
            break
        end = min(n, start + size)
        block = "\n\n".join(texts[start:end])
        src_ids = [int(parts[i]["id"]) for i in range(start, end)]
        merged.append({"id": src_ids[0], "text": block, "source_ids": src_ids})
    return merged


def _structure_prompt_single(excerpt: str) -> list[dict[str, str]]:
    sys = (
        "You are a story editor. Read the excerpt and describe structure for the author. "
        "Use markdown with these sections exactly: "
        "## Characters\n## Story flow\n## Tone and themes\n## Suggestions\n"
        "Be specific to this text; stay under 400 words."
    )
    return [
        {"role": "system", "content": sys},
        {"role": "user", "content": f"Manuscript excerpt:\n\n{excerpt}"},
    ]


def _chunk_insight_prompt(chunk: str, index: int, total: int) -> list[dict[str, str]]:
    sys = (
        "You analyze one slice of a longer manuscript. Reply in markdown bullets only, "
        "max 120 words. Cover: named characters, what this slice accomplishes (beat), "
        "tone, any hooks or twists. If slice is dialogue-heavy or exposition-heavy, say so."
    )
    return [
        {"role": "system", "content": sys},
        {
            "role": "user",
            "content": f"Part {index + 1} of {total}:\n\n{chunk}",
        },
    ]


def _synthesis_prompt(fragment_notes: str) -> list[dict[str, str]]:
    sys = (
        "You merge fragment-level notes into one author-facing structure brief. "
        "Use markdown with: ## Characters (roster + roles), ## Story flow / beats, "
        "## Tone and themes, ## Pacing. End with ## Revision hooks (bullet ideas for "
        "rewrites: format shifts, new twists, tone shifts). Max 450 words. No preamble."
    )
    return [
        {"role": "system", "content": sys},
        {"role": "user", "content": f"Fragment analyses:\n\n{fragment_notes}"},
    ]


def analyze_manuscript_structure(
    text: str,
    *,
    settings: Settings,
    llm_provider: str | None = None,
    target_chars: int = 6500,
    max_detail_chunks: int = 6,
    temperature: float = 0.35,
) -> tuple[str, list[dict[str, Any]], str]:
    """
    Returns ``(structure_markdown, chunk_insights, combined_thinking)``.

    ``combined_thinking`` concatenates model thinking blocks (Anthropic extended thinking)
    or is empty for Ollama.
    """
    body = (text or "").strip()
    if not body:
        return ("_(Nothing to analyze.)_", [], "")

    ws = settings.workspace
    thinking_parts: list[str] = []

    def _run(messages: list[dict[str, str]], tag: str, num_ctx: int | None) -> str:
        t, th, _meta = complete_chat(
            settings,
            llm_provider_override=llm_provider,
            messages=messages,
            temperature=temperature,
            workspace=ws,
            tag=tag,
            ollama_num_ctx=num_ctx,
        )
        if th.strip():
            thinking_parts.append(f"## {tag}\n\n{th.strip()}")
        return t

    ui_parts = chunk_manuscript(body, target_chars=target_chars)

    if len(body) <= 9000:
        md = _run(_structure_prompt_single(body[:120_000]), "manuscript-structure-short", 32_000)
        combined = "\n\n---\n\n".join(thinking_parts) if thinking_parts else ""
        return (md, [], combined)

    analysis_blocks = _merge_for_analysis(ui_parts, max_detail_chunks)
    insights: list[dict[str, Any]] = []
    notes_for_synth: list[str] = []
    total = len(analysis_blocks)
    for idx, p in enumerate(analysis_blocks):
        chunk_text = str(p.get("text") or "")
        label_ids = p.get("source_ids") or [int(p["id"])]
        span = f"{label_ids[0] + 1}"
        if len(label_ids) > 1:
            span = f"{label_ids[0] + 1}–{label_ids[-1] + 1}"
        note = _run(
            _chunk_insight_prompt(chunk_text, idx, total),
            f"manuscript-insight-{idx + 1}-of-{total}",
            24_000,
        )
        insights.append({"id": int(p["id"]), "span_chunks": label_ids, "markdown": note})
        notes_for_synth.append(f"### Segments {span} (analysis block {idx + 1} of {total})\n{note}")

    merged_notes = "\n\n".join(notes_for_synth)
    structure = _run(_synthesis_prompt(merged_notes), "manuscript-structure-synth", 32_000)
    combined = "\n\n---\n\n".join(thinking_parts) if thinking_parts else ""
    return (structure, insights, combined)


def comments_to_goal_block(comments: list[dict[str, Any]]) -> str:
    """Turn UI notes into instructions for the supervisor goal field."""
    if not comments:
        return ""
    lines = ["[Manuscript notes — apply where relevant]"]
    for c in sorted(comments, key=lambda x: (int(x.get("chunk_id", 0)), str(x.get("id", "")))):
        cid = int(c.get("chunk_id", 0))
        body = str(c.get("body", "")).strip()
        if body:
            lines.append(f"- Chunk {cid + 1}: {body}")
    return "\n".join(lines)
