from __future__ import annotations

import json
from typing import Any

import httpx


def invoke_openclaw_tool(
    gateway_url: str,
    token: str,
    tool: str,
    args: dict[str, Any] | None = None,
    *,
    session_key: str = "main",
    timeout: float = 180.0,
) -> Any:
    """
    Generic OpenClaw Gateway POST /tools/invoke.
    Tool must be allowed by gateway policy (many tools are HTTP-denylisted by default).
    """
    url = f"{gateway_url.rstrip('/')}/tools/invoke"
    body: dict[str, Any] = {
        "tool": tool.strip(),
        "args": args or {},
        "sessionKey": session_key,
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
