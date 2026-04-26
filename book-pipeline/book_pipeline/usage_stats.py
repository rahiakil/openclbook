from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any


def _ollama_row_wall_ms(row: dict[str, Any]) -> float:
    """Ollama-reported request duration (nanoseconds in API → ms in log)."""
    v = row.get("total_duration_ms")
    if v is not None:
        return float(v)
    ns = row.get("total_duration_ns")
    if ns is not None:
        return float(ns) / 1_000_000.0
    return 0.0


def _ollama_row_tokens(row: dict[str, Any]) -> int:
    if row.get("total_tokens") is not None:
        return int(row["total_tokens"])
    return int(row.get("prompt_eval_count") or 0) + int(row.get("eval_count") or 0)


def _ollama_row_ms_per_1k(row: dict[str, Any], ms: float, tot: int) -> float | None:
    v = row.get("wall_ms_per_1k_total_tokens")
    if v is not None:
        return float(v)
    if tot > 0 and ms > 0:
        return (ms / float(tot)) * 1000.0
    return None


def _token_bucket_label(n: int) -> str:
    if n < 2000:
        return "[0,2k)"
    if n < 4000:
        return "[2k,4k)"
    if n < 8000:
        return "[4k,8k)"
    return "[8k,)"


def _percentiles(vals: list[float]) -> dict[str, float]:
    if not vals:
        return {}
    s = sorted(vals)
    n = len(s)

    def pick(q: float) -> float:
        if n == 1:
            return float(s[0])
        pos = (n - 1) * q
        lo = int(pos)
        hi = min(lo + 1, n - 1)
        return float(s[lo] + (s[hi] - s[lo]) * (pos - lo))

    return {
        "min": float(s[0]),
        "p50": pick(0.5),
        "p90": pick(0.9),
        "max": float(s[-1]),
    }


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
            "total_tokens": 0,
            "models": {},
            "total_wall_ms": 0.0,
            "calls_with_timing": 0,
            "timed_tokens": 0,
            "aggregate_tokens_per_second": None,
            "avg_ms_per_1k_total_tokens": None,
            "ms_per_1k_distribution": {},
            "by_tag": {},
            "token_buckets": {},
        }
    calls = 0
    pt = 0
    ct = 0
    by_model: dict[str, dict[str, int | float]] = {}
    total_wall_ms = 0.0
    calls_with_timing = 0
    timed_tokens = 0
    ms_per_1k_samples: list[float] = []
    by_tag: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "calls": 0,
            "total_tokens": 0,
            "timed_tokens": 0,
            "total_wall_ms": 0.0,
            "calls_with_timing": 0,
        }
    )
    buckets: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {"calls": 0, "total_tokens": 0, "timed_tokens": 0, "total_wall_ms": 0.0}
    )
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
            tot = p + c
            pt += p
            ct += c
            m = str(row.get("model") or "unknown")
            acc = by_model.setdefault(m, {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0, "total_wall_ms": 0.0})
            acc["prompt_tokens"] = int(acc["prompt_tokens"]) + p
            acc["completion_tokens"] = int(acc["completion_tokens"]) + c
            acc["calls"] = int(acc["calls"]) + 1
            wms = float(row.get("wall_duration_ms") or 0.0)
            if wms > 0:
                calls_with_timing += 1
                total_wall_ms += wms
                timed_tokens += tot
                acc["total_wall_ms"] = float(acc["total_wall_ms"]) + wms
                mpk = row.get("wall_ms_per_1k_total_tokens")
                if mpk is not None:
                    ms_per_1k_samples.append(float(mpk))
                elif tot > 0:
                    ms_per_1k_samples.append((wms / float(tot)) * 1000.0)
            tag = str(row.get("tag") or "").strip() or "_untagged"
            tg = by_tag[tag]
            tg["calls"] = int(tg["calls"]) + 1
            tg["total_tokens"] = int(tg["total_tokens"]) + tot
            if wms > 0:
                tg["total_wall_ms"] = float(tg["total_wall_ms"]) + wms
                tg["calls_with_timing"] = int(tg["calls_with_timing"]) + 1
                tg["timed_tokens"] = int(tg["timed_tokens"]) + tot
            bl = _token_bucket_label(tot)
            bk = buckets[bl]
            bk["calls"] = int(bk["calls"]) + 1
            bk["total_tokens"] = int(bk["total_tokens"]) + tot
            bk["total_wall_ms"] = float(bk["total_wall_ms"]) + wms
            if wms > 0:
                bk["timed_tokens"] = int(bk["timed_tokens"]) + tot
    except OSError:
        pass
    tot_tok = pt + ct
    agg_tps = None
    avg_1k = None
    if total_wall_ms > 0 and timed_tokens > 0:
        agg_tps = round(timed_tokens / (total_wall_ms / 1000.0), 4)
        avg_1k = round(total_wall_ms / (timed_tokens / 1000.0), 4)
    dist = _percentiles(ms_per_1k_samples)
    bucket_out: dict[str, Any] = {}
    for label, b in buckets.items():
        bt = int(b["total_tokens"])
        btt = int(b["timed_tokens"])
        bm = float(b["total_wall_ms"])
        bucket_out[label] = {
            "calls": int(b["calls"]),
            "total_tokens": bt,
            "timed_tokens": btt,
            "total_wall_ms": round(bm, 3),
            "avg_ms_per_1k_total_tokens": round(bm / (btt / 1000.0), 4) if btt > 0 and bm > 0 else None,
        }
    tag_out: dict[str, Any] = {}
    for tag, tg in by_tag.items():
        tt = int(tg["total_tokens"])
        ttt = int(tg["timed_tokens"])
        tw = float(tg["total_wall_ms"])
        cwt = int(tg["calls_with_timing"])
        tag_out[tag] = {
            "calls": int(tg["calls"]),
            "total_tokens": tt,
            "timed_tokens": ttt,
            "total_wall_ms": round(tw, 3),
            "calls_with_timing": cwt,
            "avg_ms_per_1k_total_tokens": round(tw / (ttt / 1000.0), 4) if ttt > 0 and tw > 0 else None,
            "aggregate_tokens_per_second": round(ttt / (tw / 1000.0), 4) if tw > 0 and ttt > 0 else None,
        }
    return {
        "calls": calls,
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "total_tokens": tot_tok,
        "models": by_model,
        "log_path": str(path.relative_to(workspace)) if path.is_file() else ".pipeline/anthropic_usage.jsonl",
        "total_wall_ms": round(total_wall_ms, 3),
        "calls_with_timing": calls_with_timing,
        "timed_tokens": timed_tokens,
        "aggregate_tokens_per_second": agg_tps,
        "avg_ms_per_1k_total_tokens": avg_1k,
        "ms_per_1k_distribution": dist,
        "by_tag": tag_out,
        "token_buckets": bucket_out,
        "note_wall_ms": "Client-side HTTP wall time (not provider eval time).",
    }


def aggregate_ollama_usage(workspace: Path) -> dict[str, Any]:
    """
    Sum token-ish counters from Ollama /api/chat responses logged by ``ollama_chat``.

    Ollama fields: ``prompt_eval_count`` (prompt tokens), ``eval_count`` (generated tokens).
    When present, ``total_duration_ns`` / ``total_duration_ms`` are summed for throughput estimates.
    """
    path = _usage_path(workspace)
    empty = {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "models": {},
        "total_wall_ms": 0.0,
        "calls_with_timing": 0,
        "timed_tokens": 0,
        "aggregate_tokens_per_second": None,
        "avg_ms_per_1k_total_tokens": None,
        "ms_per_1k_distribution": {},
        "by_tag": {},
        "token_buckets": {},
    }
    if not path.is_file():
        return {**empty, "log_path": ".pipeline/ollama_usage.jsonl"}

    calls = 0
    pt = 0
    ct = 0
    by_model: dict[str, dict[str, int | float]] = {}
    total_wall_ms = 0.0
    calls_with_timing = 0
    timed_tokens = 0
    ms_per_1k_samples: list[float] = []
    by_tag: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "calls": 0,
            "total_tokens": 0,
            "timed_tokens": 0,
            "total_wall_ms": 0.0,
            "calls_with_timing": 0,
        }
    )
    buckets: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {"calls": 0, "total_tokens": 0, "timed_tokens": 0, "total_wall_ms": 0.0}
    )

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
            tot = _ollama_row_tokens(row)
            if tot == 0:
                tot = p + c
            pt += p
            ct += c
            m = str(row.get("model") or "unknown")
            acc = by_model.setdefault(m, {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0, "total_wall_ms": 0.0})
            acc["prompt_tokens"] = int(acc["prompt_tokens"]) + p
            acc["completion_tokens"] = int(acc["completion_tokens"]) + c
            acc["calls"] = int(acc["calls"]) + 1
            wms = _ollama_row_wall_ms(row)
            if wms > 0:
                calls_with_timing += 1
                total_wall_ms += wms
                timed_tokens += tot
                acc["total_wall_ms"] = float(acc["total_wall_ms"]) + wms
                mpk = _ollama_row_ms_per_1k(row, wms, tot)
                if mpk is not None:
                    ms_per_1k_samples.append(mpk)
            tag = str(row.get("tag") or "").strip() or "_untagged"
            tg = by_tag[tag]
            tg["calls"] = int(tg["calls"]) + 1
            tg["total_tokens"] = int(tg["total_tokens"]) + tot
            if wms > 0:
                tg["total_wall_ms"] = float(tg["total_wall_ms"]) + wms
                tg["calls_with_timing"] = int(tg["calls_with_timing"]) + 1
                tg["timed_tokens"] = int(tg["timed_tokens"]) + tot
            bl = _token_bucket_label(tot)
            bk = buckets[bl]
            bk["calls"] = int(bk["calls"]) + 1
            bk["total_tokens"] = int(bk["total_tokens"]) + tot
            bk["total_wall_ms"] = float(bk["total_wall_ms"]) + wms
            if wms > 0:
                bk["timed_tokens"] = int(bk["timed_tokens"]) + tot
    except OSError:
        pass

    tot_tok = pt + ct
    agg_tps = None
    avg_1k = None
    if total_wall_ms > 0 and timed_tokens > 0:
        agg_tps = round(timed_tokens / (total_wall_ms / 1000.0), 4)
        avg_1k = round(total_wall_ms / (timed_tokens / 1000.0), 4)
    dist = _percentiles(ms_per_1k_samples)
    bucket_out: dict[str, Any] = {}
    for label, b in buckets.items():
        bt = int(b["total_tokens"])
        btt = int(b["timed_tokens"])
        bm = float(b["total_wall_ms"])
        bucket_out[label] = {
            "calls": int(b["calls"]),
            "total_tokens": bt,
            "timed_tokens": btt,
            "total_wall_ms": round(bm, 3),
            "avg_ms_per_1k_total_tokens": round(bm / (btt / 1000.0), 4) if btt > 0 and bm > 0 else None,
        }
    tag_out: dict[str, Any] = {}
    for tag, tg in by_tag.items():
        tt = int(tg["total_tokens"])
        ttt = int(tg["timed_tokens"])
        tw = float(tg["total_wall_ms"])
        cwt = int(tg["calls_with_timing"])
        tag_out[tag] = {
            "calls": int(tg["calls"]),
            "total_tokens": tt,
            "timed_tokens": ttt,
            "total_wall_ms": round(tw, 3),
            "calls_with_timing": cwt,
            "avg_ms_per_1k_total_tokens": round(tw / (ttt / 1000.0), 4) if ttt > 0 and tw > 0 else None,
            "aggregate_tokens_per_second": round(ttt / (tw / 1000.0), 4) if tw > 0 and ttt > 0 else None,
        }

    return {
        "calls": calls,
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "total_tokens": tot_tok,
        "models": by_model,
        "log_path": str(path.relative_to(workspace)) if path.is_file() else ".pipeline/ollama_usage.jsonl",
        "total_wall_ms": round(total_wall_ms, 3),
        "calls_with_timing": calls_with_timing,
        "timed_tokens": timed_tokens,
        "aggregate_tokens_per_second": agg_tps,
        "avg_ms_per_1k_total_tokens": avg_1k,
        "ms_per_1k_distribution": dist,
        "by_tag": tag_out,
        "token_buckets": bucket_out,
        "note_wall_ms": "Ollama-reported total_duration (eval wall), comparable across runs for the same model.",
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


def write_project_metrics_summary(workspace: Path) -> dict[str, Any]:
    """
    Write ``<workspace>/.pipeline/project_metrics_summary.json`` from JSONL usage logs.

    Per-call detail stays in ``ollama_usage.jsonl`` / ``anthropic_usage.jsonl``; this file
    holds rolled-up token totals, wall time, ms per 1k total tokens, and coarse token buckets.
    """
    ws = workspace.resolve()
    oll = aggregate_ollama_usage(ws)
    ant = aggregate_anthropic_usage(ws)
    payload: dict[str, Any] = {
        "workspace": str(ws),
        "generated_ts": time.time(),
        "ollama": oll,
        "anthropic": ant,
    }
    dest = ws / ".pipeline" / "project_metrics_summary.json"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        payload["summary_path"] = str(dest.relative_to(ws))
    except OSError:
        payload["summary_path"] = None
        payload["summary_write_error"] = True
    return payload


def refresh_pipeline_metrics_summaries(workspace: Path) -> dict[str, Any]:
    """Alias for :func:`write_project_metrics_summary` (supervisor / CLI hooks)."""
    return write_project_metrics_summary(workspace)
