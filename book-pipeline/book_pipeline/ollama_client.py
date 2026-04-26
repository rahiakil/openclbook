from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx

from book_pipeline.llm_stats_emit import emit_ollama_stats_stderr


def _append_usage_log(path: Path, row: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        row["ts"] = time.time()
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _ollama_usage_row(
    model: str,
    data: dict[str, Any],
    *,
    log_tag: str | None,
) -> dict[str, Any]:
    """Derive latency / throughput for project metrics (``ollama_usage.jsonl``)."""
    p = int(data.get("prompt_eval_count") or 0)
    c = int(data.get("eval_count") or 0)
    tot = p + c
    ns = int(data.get("total_duration") or 0)
    dur_ms = ns / 1_000_000.0 if ns else 0.0
    dur_s = ns / 1_000_000_000.0 if ns else 0.0
    row: dict[str, Any] = {
        "provider": "ollama",
        "model": model,
        "prompt_eval_count": p or None,
        "eval_count": c or None,
        "total_tokens": tot,
        "total_duration_ns": ns or None,
        "total_duration_ms": round(dur_ms, 3) if ns else None,
        "tokens_per_second": round(tot / dur_s, 4) if dur_s > 0 and tot > 0 else None,
        "wall_ms_per_1k_total_tokens": round((dur_ms / tot) * 1000.0, 4) if tot > 0 and dur_ms > 0 else None,
    }
    if log_tag:
        row["tag"] = log_tag
    return row


def ollama_chat(
    base_url: str,
    model: str,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.35,
    num_ctx: int | None = None,
    timeout: float = 600.0,
    usage_log_path: Path | None = None,
    log_tag: str | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """Call Ollama /api/chat; returns ``(assistant_text, usage_row_or_none)``."""
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature},
    }
    if num_ctx is not None:
        payload["options"]["num_ctx"] = num_ctx

    url = f"{base_url.rstrip('/')}/api/chat"
    with httpx.Client(timeout=timeout) as client:
        r = client.post(url, json=payload)
        r.raise_for_status()
        data = r.json()
    usage_row: dict[str, Any] | None = None
    if usage_log_path is not None:
        usage_row = _ollama_usage_row(model, data, log_tag=log_tag)
        _append_usage_log(usage_log_path, usage_row)
        emit_ollama_stats_stderr(usage_row)
    msg = data.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, str):
        return json.dumps(data, indent=2), usage_row
    return content.strip(), usage_row


def ollama_tags(base_url: str, timeout: float = 30.0) -> list[str]:
    url = f"{base_url.rstrip('/')}/api/tags"
    with httpx.Client(timeout=timeout) as client:
        r = client.get(url)
        r.raise_for_status()
        data = r.json()
    models = data.get("models") or []
    return [str(m.get("name", "")) for m in models if m.get("name")]
