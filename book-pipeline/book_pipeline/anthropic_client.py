from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx


def _append_anthropic_usage(path: Path, row: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        row["ts"] = time.time()
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _split_system_user(messages: list[dict[str, str]]) -> tuple[str | None, list[dict[str, Any]]]:
    sys_parts: list[str] = []
    out: list[dict[str, Any]] = []
    for m in messages:
        role = str(m.get("role") or "")
        content = str(m.get("content") or "")
        if role == "system":
            sys_parts.append(content)
        elif role in ("user", "assistant"):
            out.append({"role": role, "content": content})
    system = "\n\n".join(sys_parts).strip() if sys_parts else None
    return system, out


def _extract_thinking_and_text(content: list[Any]) -> tuple[str, str]:
    thinking_parts: list[str] = []
    text_parts: list[str] = []
    if not isinstance(content, list):
        return "", ""
    for block in content:
        if not isinstance(block, dict):
            continue
        t = block.get("type")
        if t == "thinking":
            th = block.get("thinking")
            if isinstance(th, str) and th.strip():
                thinking_parts.append(th.strip())
        elif t == "redacted_thinking":
            thinking_parts.append("(reasoning redacted in API response)")
        elif t == "text":
            tx = block.get("text")
            if isinstance(tx, str) and tx.strip():
                text_parts.append(tx.strip())
    return "\n\n".join(thinking_parts), "\n\n".join(text_parts)


def _thinking_payload(
    model: str,
    thinking_mode: str,
    budget: int,
) -> dict[str, Any] | None:
    mode = (thinking_mode or "off").strip().lower()
    if mode in ("", "off", "false", "0", "none"):
        return None
    m = model.lower()
    if mode == "adaptive":
        return {"type": "adaptive"}
    # Opus 4.7+ rejects enabled+budget — prefer adaptive via config for those IDs
    if "opus-4-7" in m or "opus-4-7-" in m:
        return {"type": "adaptive"}
    if mode == "enabled":
        b = max(1024, min(int(budget or 10_000), 60_000))
        return {"type": "enabled", "budget_tokens": b}
    # unknown → try adaptive for 4.x models, else off
    if "claude" in m and ("4-" in m or "4_" in m or "sonnet-4" in m or "opus-4" in m):
        return {"type": "adaptive"}
    return None


def anthropic_messages(
    *,
    api_key: str,
    base_url: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float = 0.35,
    max_tokens: int = 16_000,
    thinking_mode: str = "adaptive",
    thinking_budget: int = 10_000,
    timeout: float = 600.0,
    usage_log_path: Path | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """
    Call Anthropic Messages API. Returns (assistant_text, thinking_text, usage_dict).

    ``thinking_mode``: ``adaptive`` | ``enabled`` | ``off`` (see Anthropic extended thinking docs).
    """
    system, msgs = _split_system_user(messages)
    if not msgs:
        raise ValueError("anthropic: no user/assistant messages")

    url = f"{base_url.rstrip('/')}/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": msgs,
    }
    if system:
        body["system"] = system
    # temperature not supported on all models with thinking — omit when thinking on
    think = _thinking_payload(model, thinking_mode, thinking_budget)
    if think is not None:
        body["thinking"] = think
    else:
        body["temperature"] = temperature

    with httpx.Client(timeout=timeout) as client:
        r = client.post(url, headers=headers, json=body)
        r.raise_for_status()
        data = r.json()

    usage = data.get("usage") or {}
    if usage_log_path is not None:
        _append_anthropic_usage(
            usage_log_path,
            {
                "provider": "anthropic",
                "model": model,
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
            },
        )

    content = data.get("content") or []
    thinking, text = _extract_thinking_and_text(content)
    if not text and isinstance(content, list):
        # fallback: stringify unknown blocks
        text = json.dumps(content, ensure_ascii=False)[:80_000]
    return text.strip(), thinking.strip(), dict(usage)
