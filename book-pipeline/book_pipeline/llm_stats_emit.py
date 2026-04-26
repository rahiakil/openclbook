from __future__ import annotations

import os
import sys
from typing import Any


def stderr_llm_stats_enabled() -> bool:
    v = (os.environ.get("BOOK_PIPELINE_LLM_STATS_STDERR") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def emit_ollama_stats_stderr(row: dict[str, Any]) -> None:
    if not stderr_llm_stats_enabled():
        return
    print(
        "[llm] ollama "
        f"model={row.get('model')} "
        f"tag={row.get('tag') or '-'} "
        f"tokens={row.get('total_tokens')} "
        f"prompt={row.get('prompt_eval_count')} "
        f"eval={row.get('eval_count')} "
        f"duration_ms={row.get('total_duration_ms')} "
        f"tok/s={row.get('tokens_per_second')} "
        f"ms/1k={row.get('wall_ms_per_1k_total_tokens')}",
        file=sys.stderr,
        flush=True,
    )


def emit_anthropic_stats_stderr(row: dict[str, Any]) -> None:
    if not stderr_llm_stats_enabled():
        return
    print(
        "[llm] anthropic "
        f"model={row.get('model')} "
        f"tag={row.get('tag') or '-'} "
        f"tokens={row.get('total_tokens')} "
        f"in={row.get('input_tokens')} "
        f"out={row.get('output_tokens')} "
        f"wall_ms={row.get('wall_duration_ms')} "
        f"tok/s={row.get('tokens_per_second')} "
        f"ms/1k={row.get('wall_ms_per_1k_total_tokens')}",
        file=sys.stderr,
        flush=True,
    )
