from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _usage_path(workspace: Path) -> Path:
    return (workspace / ".pipeline" / "ollama_usage.jsonl").resolve()


def _anthropic_usage_path(workspace: Path) -> Path:
    return (workspace / ".pipeline" / "anthropic_usage.jsonl").resolve()


def aggregate_anthropic_usage(workspace: Path) -> dict[str, Any]:
    path = _anthropic_usage_path(workspace)
    if not path.is_file():
        return {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "models": {},
        }
    calls = 0
    pt = 0
    ct = 0
    by_model: dict[str, dict[str, int]] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            calls += 1
            p = int(row.get("input_tokens") or 0)
            c = int(row.get("output_tokens") or 0)
            pt += p
            ct += c
            m = str(row.get("model") or "unknown")
            acc = by_model.setdefault(m, {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0})
            acc["prompt_tokens"] += p
            acc["completion_tokens"] += c
            acc["calls"] += 1
    except OSError:
        pass
    return {
        "calls": calls,
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "total_tokens": pt + ct,
        "models": by_model,
        "log_path": str(path.relative_to(workspace)) if path.is_file() else ".pipeline/anthropic_usage.jsonl",
    }


def aggregate_ollama_usage(workspace: Path) -> dict[str, Any]:
    """
    Sum token-ish counters from Ollama /api/chat responses logged by ``ollama_chat``.

    Ollama fields: ``prompt_eval_count`` (prompt tokens), ``eval_count`` (generated tokens).
    """
    path = _usage_path(workspace)
    if not path.is_file():
        return {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "models": {},
        }

    calls = 0
    pt = 0
    ct = 0
    by_model: dict[str, dict[str, int]] = {}

    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            calls += 1
            p = int(row.get("prompt_eval_count") or 0)
            c = int(row.get("eval_count") or 0)
            pt += p
            ct += c
            m = str(row.get("model") or "unknown")
            acc = by_model.setdefault(m, {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0})
            acc["prompt_tokens"] += p
            acc["completion_tokens"] += c
            acc["calls"] += 1
    except OSError:
        pass

    return {
        "calls": calls,
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "total_tokens": pt + ct,
        "models": by_model,
        "log_path": str(path.relative_to(workspace)) if path.is_file() else ".pipeline/ollama_usage.jsonl",
    }


def estimate_cloud_cost_usd(
    prompt_tokens: int,
    completion_tokens: int,
) -> dict[str, Any]:
    """
    Optional hypothetical cloud cost from env (USD per 1M tokens).

    BOOK_COST_PER_1M_PROMPT and BOOK_COST_PER_1M_COMPLETION — omit or set 0 for local-only.
    """
    try:
        p_rate = float((os.environ.get("BOOK_COST_PER_1M_PROMPT") or "0").strip() or 0)
    except ValueError:
        p_rate = 0.0
    try:
        c_rate = float((os.environ.get("BOOK_COST_PER_1M_COMPLETION") or "0").strip() or 0)
    except ValueError:
        c_rate = 0.0

    usd = (prompt_tokens / 1_000_000.0) * p_rate + (completion_tokens / 1_000_000.0) * c_rate
    return {
        "usd_hypothetical": round(usd, 6),
        "usd_local_ollama": 0.0,
        "rates_configured": bool(p_rate > 0 or c_rate > 0),
        "per_1m_prompt_usd": p_rate,
        "per_1m_completion_usd": c_rate,
    }


def rough_pass_estimate(
    workspace: Path,
    *,
    chunk_count: int,
    include_analyze: bool,
) -> dict[str, Any]:
    """
    Heuristic: plan + one Ollama call per chunk (edit) + optional analyze calls.

    Real usage varies with manuscript size and model; this is for UI hints only.
    """
    # Analyze path: 1 structure + up to ~6 detail + 1 synth for long works — cap guess
    analyze_calls = 3 if include_analyze else 0
    edit_calls = max(1, chunk_count) + 1  # plan + edits
    est_calls = analyze_calls + edit_calls
    # Assume average prompt 4k eval + 1.5k completion per call (very rough)
    est_prompt = est_calls * 4000
    est_completion = est_calls * 1500
    cost = estimate_cloud_cost_usd(est_prompt, est_completion)
    return {
        "estimated_calls": est_calls,
        "estimated_prompt_tokens": est_prompt,
        "estimated_completion_tokens": est_completion,
        "hypothetical_usd_if_configured": cost["usd_hypothetical"],
        "note": "Rough order-of-magnitude; actual Ollama counts appear in usage after runs.",
    }
