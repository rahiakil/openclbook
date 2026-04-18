from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx


def _append_usage_log(path: Path, row: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        row["ts"] = time.time()
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def ollama_chat(
    base_url: str,
    model: str,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.35,
    num_ctx: int | None = None,
    timeout: float = 600.0,
    usage_log_path: Path | None = None,
) -> str:
    """Call Ollama /api/chat; returns assistant message content only."""
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
    if usage_log_path is not None:
        _append_usage_log(
            usage_log_path,
            {
                "model": model,
                "prompt_eval_count": data.get("prompt_eval_count"),
                "eval_count": data.get("eval_count"),
                "total_duration_ns": data.get("total_duration"),
            },
        )
    msg = data.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, str):
        return json.dumps(data, indent=2)
    return content.strip()


def ollama_tags(base_url: str, timeout: float = 30.0) -> list[str]:
    url = f"{base_url.rstrip('/')}/api/tags"
    with httpx.Client(timeout=timeout) as client:
        r = client.get(url)
        r.raise_for_status()
        data = r.json()
    models = data.get("models") or []
    return [str(m.get("name", "")) for m in models if m.get("name")]
