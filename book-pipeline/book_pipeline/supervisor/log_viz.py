"""ASCII diagrams and banners for supervisor ``log`` output (terminals + UI run log)."""

from __future__ import annotations

from typing import Any


def _trim(s: str, max_len: int) -> str:
    s = (s or "").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 20] + "\n  … (truncated)"


def banner(title: str, width: int = 72) -> str:
    t = title.strip().upper()
    w = min(width, max(len(t) + 6, 40))
    pad = max(0, (w - len(t) - 2) // 2)
    line = "=" * w
    return f"{line}\n{' ' * pad}{t}\n{line}"


def pipeline_flow_ascii(*, mode: str) -> str:
    edit_note = "parallel thread pool over chunks" if mode == "full" else "sequential; marathon pauses each chunk"
    return f"""
  ORCHESTRATION FLOW  (run_mode = {mode})
  ─────────────────
       .---.
       |INIT|  load sections/*.md or manuscript/draft.md (+ size-split if needed)
       '---'
          |
          v
    .------------.
    | DIVIDE_WORK|  optional LLM chapter split (single big draft)
    '------------'
          |
          v
       .------.
       | PLAN |  orchestration markdown + per-chunk notes
       '------'
          |
          v
  .------------------.
  | BALANCE_CONTEXT |  resplit chunks if memory+plan+chunk > budget (no silent truncation)
  '------------------'
          |
          v
    .----------.
    | OPENCLAW |  optional global tool (skipped if disabled)
    '----------'
          |
          v
       .------.
       | EDIT |  {edit_note}
       '------'  (+ optional OpenClaw per chunk)
          |
          v
     .---------.
     | STAGING |  merge -> outputs/staging_merged.md
     '---------'
          |
          v
     .--------.
     | VERIFY |  multi-pass LLM rubric vs statements -> PASS or retry loop
     '--------'
          |
          v
  .-------------------.
  | PERSIST_LEARNINGS |  append run to .memory/agentic/project_learnings.md
  '-------------------'
          |
    fail + rounds left
          |
          v
  .------------------.
  | PREPARE_REVISION |  reset chunk proposals -> back to PLAN
  '------------------'
""".rstrip()


def chunk_inventory_ascii(chunks: list[dict[str, Any]], *, max_rows: int = 24) -> str:
    rows = []
    mx = max((len(x.get("original") or "") for x in chunks), default=1)
    for i, c in enumerate(chunks[:max_rows]):
        path = str(c.get("path", "?"))[:52]
        n = len(c.get("original") or "")
        bar_n = min(40, int(40 * n / mx)) if mx else 0
        bar = "#" * bar_n + "." * (40 - bar_n)
        title = (c.get("chapter_title") or "")[:28]
        suf = f"  ({title})" if title else ""
        rows.append(f"  [{i + 1:3}] {n:6} chars  |{bar}|  {path}{suf}")
    extra = ""
    if len(chunks) > max_rows:
        extra = f"\n  … +{len(chunks) - max_rows} more chunk(s)"
    head = banner("CHUNK INVENTORY (input size)", width=72)
    return head + "\n" + "\n".join(rows) + extra


def divide_result_ascii(chunks: list[dict[str, Any]]) -> str:
    lines = [banner("DIVISION OF WORK — chapter map", width=72)]
    for i, c in enumerate(chunks[:32]):
        title = (c.get("chapter_title") or "").strip() or "(untitled)"
        path = str(c.get("path", ""))[:40]
        n = len(c.get("original") or "")
        lines.append(f"  {(i + 1):2}. {title[:36]:<36}  {n:6} chars  {path}")
    if len(chunks) > 32:
        lines.append(f"  … +{len(chunks) - 32} more")
    return "\n".join(lines)


def plan_excerpt_for_log(plan_markdown: str, *, max_chars: int = 2800) -> str:
    body = _trim(plan_markdown or "(empty)", max_chars)
    return (
        banner("PLAN (markdown excerpt — full text in Plan panel / state)", width=72)
        + "\n"
        + "\n".join(f"  {ln}" for ln in body.splitlines())
        + "\n"
        + banner("END PLAN EXCERPT", width=72)
    )


def thinking_excerpt_for_log(label: str, thinking: str, *, max_chars: int = 2400) -> str:
    if not (thinking or "").strip():
        return f"[{label}] (no provider thinking trace for this call)"
    body = _trim(thinking, max_chars)
    return (
        banner(f"THINKING — {label}", width=72)
        + "\n"
        + "\n".join(f"  {ln}" for ln in body.splitlines())
        + "\n"
        + banner(f"END THINKING — {label}", width=72)
    )


def openclaw_ascii(*, tool: str, ok: bool, detail: str) -> str:
    status = "OK" if ok else "FAIL"
    box = banner(f"OPENCLAW  {tool}  [{status}]", width=72)
    return box + "\n" + "\n".join(f"  {ln}" for ln in _trim(detail, 1200).splitlines())


def verify_ascii(*, passed: bool, summary: str, violations: list[Any]) -> str:
    gate = "PASS" if passed else "FAIL"
    lines = [banner(f"VERIFIER  [{gate}]", width=72), f"  summary: {_trim(summary, 400)}"]
    if violations:
        lines.append("  violations:")
        for v in violations[:12]:
            if isinstance(v, dict):
                lines.append(f"    - stmt {v.get('statement_index', '?')}: {v.get('issue', '')}"[:120])
            else:
                lines.append(f"    - {v!s}"[:120])
        if len(violations) > 12:
            lines.append(f"    … +{len(violations) - 12} more")
    return "\n".join(lines)


def staging_banner(staging_rel: str, merged_chars: int) -> str:
    return (
        banner("STAGING MERGE", width=72)
        + f"\n  wrote .......... {staging_rel}\n  merged chars ... {merged_chars}\n"
        + "  format ......... ## <chunk path> then body, chunks separated by ---\\n\\n"
    )


def edit_batch_ascii(*, done: int, total: int, parallel: bool) -> str:
    mode = "parallel pool" if parallel else "sequential"
    bar_w = 40
    filled = int(bar_w * done / max(total, 1))
    bar = "[" + ("#" * filled) + ("-" * (bar_w - filled)) + "]"
    return banner(f"CHUNK EDITS  {done}/{total}  ({mode})", width=72) + f"\n  {bar}\n"


def run_intro_ascii(
    *,
    mode: str,
    preset: str,
    goal_preview: str,
    n_statements: int,
    max_revision_rounds: int,
    semantic_on: bool,
    openclaw_after_plan: bool,
    openclaw_per_chunk: bool,
) -> str:
    gp = _trim(goal_preview, 320).replace("\n", "\n  ")
    oc = "on" if openclaw_after_plan else "off"
    ocp = "on" if openclaw_per_chunk else "off"
    div = "on" if semantic_on else "off"
    lines = [
        banner("BOOK SUPERVISOR — run started", width=72),
        f"  run_mode ........ {mode}",
        f"  preset .......... {preset}",
        f"  user statements . {n_statements} item(s)",
        f"  max rev rounds .. {max_revision_rounds}",
        f"  semantic divide . {div}",
        f"  openclaw (plan) . {oc}",
        f"  openclaw /chunk . {ocp}",
        "",
        "  goal (preview):",
        f"  {gp}",
        "",
        pipeline_flow_ascii(mode=mode),
    ]
    return "\n".join(lines)


def prepare_revision_ascii(*, revision_count: int, max_rounds: int) -> str:
    return (
        banner("PREPARE REVISION — re-entering PLAN", width=72)
        + f"\n  revision_count ... {revision_count} (after this prepare)\n"
        + f"  max_revision_rounds cap: {max_rounds}\n"
        + "  action ............ clear proposed text, reset chunk_index; keep orchestration_feedback for planner\n"
    )
