"""Prep passes: strategic plan + human Q&A, then character digest and story-arc digest into ``.memory/``."""

from __future__ import annotations

from pathlib import Path

from book_pipeline.config import load_settings
from book_pipeline.llm_complete import complete_chat
from book_pipeline.supervisor.state import SupervisorState, log_append


def _ws(state: SupervisorState) -> Path:
    return Path(state["workspace"]).resolve()


def _manuscript_sample_from_chunks(state: SupervisorState, max_chars: int) -> str:
    parts: list[str] = []
    n = 0
    for c in state.get("chunks") or []:
        if n >= max_chars:
            break
        p = str(c.get("path") or "")
        body = str(c.get("original") or "")
        chunk = f"### {p}\n\n{body}\n"
        if n + len(chunk) > max_chars:
            chunk = chunk[: max(0, max_chars - n)] + "\n…(truncated)…\n"
        parts.append(chunk)
        n += len(chunk)
    return "\n\n".join(parts).strip() or "(no chunk text)"


def node_prep_strategic_plan(state: SupervisorState) -> SupervisorState:
    """
    LLM writes an execution strategy and optional ``## Human questions`` for the author.
    Artifacts are written to disk by ``run_supervisor_prep_phase_one`` (like plan gate).
    """
    if state.get("error"):
        return state
    ws = _ws(state)
    settings = load_settings(ws, ws / "config.yaml")
    goal = (state.get("user_goal") or "").strip() or "Improve the manuscript."
    preset = (state.get("goal_preset") or "rewrite").strip()
    statements = list(state.get("user_statements") or [])
    stmt_txt = "\n".join(f"- {s}" for s in statements) if statements else "(none)"
    inv = [f"- {c.get('path')} ({len(str(c.get('original') or ''))} chars)" for c in state.get("chunks") or []]
    inv_s = "\n".join(inv[:80])
    if len(inv) > 80:
        inv_s += f"\n… {len(inv) - 80} more chunks"
    sample = _manuscript_sample_from_chunks(state, 28_000)
    sys = (
        "You are the lead editor planning how to run a multi-pass book pipeline.\n"
        "Output **only Markdown** with these sections in order:\n"
        "## Strategy — how we will approach this rewrite\n"
        "## Passes — ordered list (what each automated pass should do)\n"
        "## Risks — what could go wrong\n"
        "## Human questions — numbered questions ONLY if you truly need the author to decide something "
        "(constraints, canon, tone, audience). If nothing is needed, write exactly: `None` under this heading.\n"
        "## Memory plan — what should be summarized into project memory after passes\n"
        "Be concrete. Do not output JSON."
    )
    user = (
        f"TRANSFORMATION_PRESET: {preset}\n\nUSER_GOAL:\n{goal}\n\nUSER_STATEMENTS:\n{stmt_txt}\n\n"
        f"CHUNK_INVENTORY:\n{inv_s}\n\nMANUSCRIPT_SAMPLE:\n{sample}\n"
    )
    try:
        prov = (state.get("llm_provider") or "").strip() or None
        plan, thinking, meta = complete_chat(
            settings,
            llm_provider_override=prov,
            messages=[{"role": "system", "content": sys}, {"role": "user", "content": user}],
            temperature=0.25,
            workspace=ws,
            tag="supervisor-prep-strategy",
            ollama_num_ctx=settings.ollama_num_ctx,
        )
    except Exception as e:  # noqa: BLE001
        return {**state, "error": str(e), "log": log_append(state, f"prep_strategic_plan failed: {e}")}

    trace = list(state.get("thinking_trace") or [])
    if thinking:
        trace.append("## Prep strategy — thinking\n\n" + thinking.strip())
    trace.append(f"## Prep strategy — meta\n\n`{meta}`\n")
    base = list(state.get("log") or [])
    return {
        **state,
        "prep_strategy_markdown": plan.strip(),
        "thinking_trace": trace,
        "log": base + ["prep_strategic_plan: model ok — edit outputs/human_input_answers.md then --prep-resume"],
    }


def node_prep_character_pass(state: SupervisorState) -> SupervisorState:
    """Pass 1: summarized character roster → ``.memory/characters/pipeline_pass1_characters.md``."""
    if state.get("error"):
        return state
    ws = _ws(state)
    settings = load_settings(ws, ws / "config.yaml")
    settings.characters_dir.mkdir(parents=True, exist_ok=True)
    strat = (state.get("prep_strategy_markdown") or "").strip()
    ans = (state.get("prep_human_answers") or "").strip()
    sample = _manuscript_sample_from_chunks(state, 32_000)
    sys = (
        "You extract a **concise character roster** from the manuscript sample for downstream editors.\n"
        "Output **only Markdown**: short sections per major character (name, role, voice, relationships). "
        "Skip minor walk-ons in one line. No preamble."
    )
    user = (
        f"EDITOR_STRATEGY:\n{strat[:24_000]}\n\nAUTHOR_ANSWERS:\n{ans[:8_000]}\n\n"
        f"MANUSCRIPT_SAMPLE:\n{sample}\n"
    )
    try:
        prov = (state.get("llm_provider") or "").strip() or None
        out, thinking, meta = complete_chat(
            settings,
            llm_provider_override=prov,
            messages=[{"role": "system", "content": sys}, {"role": "user", "content": user}],
            temperature=0.2,
            workspace=ws,
            tag="supervisor-prep-characters",
            ollama_num_ctx=settings.ollama_num_ctx,
        )
    except Exception as e:  # noqa: BLE001
        return {**state, "error": str(e), "log": log_append(state, f"prep_character_pass failed: {e}")}

    out_path = settings.characters_dir / "pipeline_pass1_characters.md"
    header = "# Pipeline pass 1 — characters (auto)\n\n_Summarized for edit prompts; safe to edit by hand._\n\n"
    out_path.write_text(header + out.strip() + "\n", encoding="utf-8")
    trace = list(state.get("thinking_trace") or [])
    if thinking:
        trace.append("## Prep characters — thinking\n\n" + thinking.strip())
    trace.append(f"## Prep characters — meta\n\n`{meta}`\n")
    rel = out_path.relative_to(ws)
    base = list(state.get("log") or [])
    return {
        **state,
        "thinking_trace": trace,
        "log": base + [f"prep_character_pass: wrote {rel}"],
    }


def node_prep_arc_pass(state: SupervisorState) -> SupervisorState:
    """Pass 2: growth + story arc → ``.memory/research/pipeline_pass2_story_arc.md``."""
    if state.get("error"):
        return state
    ws = _ws(state)
    settings = load_settings(ws, ws / "config.yaml")
    settings.research_dir.mkdir(parents=True, exist_ok=True)
    strat = (state.get("prep_strategy_markdown") or "").strip()
    ans = (state.get("prep_human_answers") or "").strip()
    p1 = settings.characters_dir / "pipeline_pass1_characters.md"
    pass1_txt = ""
    if p1.is_file():
        pass1_txt = p1.read_text(encoding="utf-8", errors="replace").strip()[:12_000]
    sample = _manuscript_sample_from_chunks(state, 28_000)
    sys = (
        "You summarize **character growth** and **story arc** (beats, turning points, open threads) "
        "for editors. Output **only Markdown**, tight bullets and short paragraphs. No preamble."
    )
    user = (
        f"EDITOR_STRATEGY:\n{strat[:20_000]}\n\nAUTHOR_ANSWERS:\n{ans[:8_000]}\n\n"
        f"PASS1_CHARACTERS:\n{pass1_txt or '(missing pass1 file)'}\n\n"
        f"MANUSCRIPT_SAMPLE:\n{sample}\n"
    )
    try:
        prov = (state.get("llm_provider") or "").strip() or None
        out, thinking, meta = complete_chat(
            settings,
            llm_provider_override=prov,
            messages=[{"role": "system", "content": sys}, {"role": "user", "content": user}],
            temperature=0.2,
            workspace=ws,
            tag="supervisor-prep-arc",
            ollama_num_ctx=settings.ollama_num_ctx,
        )
    except Exception as e:  # noqa: BLE001
        return {**state, "error": str(e), "log": log_append(state, f"prep_arc_pass failed: {e}")}

    out_path = settings.research_dir / "pipeline_pass2_story_arc.md"
    header = "# Pipeline pass 2 — story arc & growth (auto)\n\n_Summarized for edit prompts; safe to edit by hand._\n\n"
    out_path.write_text(header + out.strip() + "\n", encoding="utf-8")
    trace = list(state.get("thinking_trace") or [])
    if thinking:
        trace.append("## Prep arc — thinking\n\n" + thinking.strip())
    trace.append(f"## Prep arc — meta\n\n`{meta}`\n")
    rel = out_path.relative_to(ws)
    base = list(state.get("log") or [])
    return {
        **state,
        "thinking_trace": trace,
        "log": base + [f"prep_arc_pass: wrote {rel}"],
    }
