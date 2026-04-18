from __future__ import annotations

import json
from typing import Any

import httpx


def llm_task_json(
    gateway_url: str,
    token: str,
    *,
    prompt: str,
    input_obj: dict[str, Any] | None = None,
    schema: dict[str, Any] | None = None,
    provider: str = "ollama",
    model: str = "gemma4:26b",
    timeout: float = 120.0,
) -> Any:
    """
    POST /tools/invoke with tool llm-task (OpenClaw Gateway).
    Requires gateway running and llm-task allowed by policy.
    """
    url = f"{gateway_url.rstrip('/')}/tools/invoke"
    args: dict[str, Any] = {
        "prompt": prompt,
        "provider": provider,
        "model": model,
    }
    if input_obj is not None:
        args["input"] = input_obj
    if schema is not None:
        args["schema"] = schema

    body = {
        "tool": "llm-task",
        "action": "json",
        "args": args,
        "sessionKey": "main",
        "dryRun": False,
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=timeout) as client:
        r = client.post(url, headers=headers, json=body)
        r.raise_for_status()
        out = r.json()
    if not out.get("ok"):
        err = out.get("error") or {}
        raise RuntimeError(err.get("message") or json.dumps(out))
    return out.get("result")
