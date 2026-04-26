from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from book_pipeline.config import load_settings


def _resolve_workspace_from_run_args(args: argparse.Namespace) -> Path:
    """Resolve ``--workspace`` or ``--project-id`` (under ``BOOK_PIPELINE_PROJECTS_DIR`` / ``book-pipeline/projects``)."""
    from book_pipeline.project_workspace import ensure_project_layout, project_workspace_path

    pid = (getattr(args, "project_id", None) or "").strip()
    ws = getattr(args, "workspace", None)
    if pid and ws is not None:
        print("error: pass only one of --workspace and --project-id", file=sys.stderr)
        raise SystemExit(2)
    if pid:
        base = project_workspace_path(pid)
        ensure_project_layout(base)
        return base
    if ws is None:
        print("error: --workspace or --project-id is required", file=sys.stderr)
        raise SystemExit(2)
    return Path(ws).expanduser().resolve()
from book_pipeline.graph import run_task
from book_pipeline.ingest import read_document, split_by_h2, write_sections
from book_pipeline.ollama_client import ollama_tags
from book_pipeline.queue import append_task, pop_task


def _print_supervisor_verbose(out: dict) -> None:
    """Print merged thinking trace after a run (provider reasoning blocks)."""
    tr = out.get("thinking_trace") or []
    if not tr:
        print("\n(no thinking_trace entries — provider may not return thinking text)\n", file=sys.stderr)
        return
    print("\n======== thinking_trace (verbose) ========\n", flush=True)
    print("\n\n--- section ---\n\n".join(str(x) for x in tr), flush=True)
    print("\n======== end thinking_trace ========\n", flush=True)


def cmd_run_once(ws: Path) -> int:
    settings = load_settings(ws, ws / "config.yaml")
    task = pop_task(settings.todo_path)
    if not task:
        print("todo.file: empty (nothing to do)", file=sys.stderr)
        return 0
    print(f"task: {json.dumps(task, ensure_ascii=False)[:500]}...")
    out = run_task(ws, task)
    for line in out.get("log") or []:
        print(line)
    if out.get("error"):
        print(f"error: {out['error']}", file=sys.stderr)
        return 1
    print(f"wrote: {out.get('output_path', '')}")
    for chained in task.get("chain") or []:
        if isinstance(chained, dict):
            append_task(settings.todo_path, chained)
            print("chained task appended to todo.file")
    return 0


def cmd_enqueue(ws: Path, payload: str) -> int:
    settings = load_settings(ws, ws / "config.yaml")
    task = json.loads(payload)
    append_task(settings.todo_path, task)
    print("appended 1 task to todo.file")
    return 0


def cmd_split(ws: Path, file_rel: str) -> int:
    settings = load_settings(ws, ws / "config.yaml")
    path = (ws / file_rel).resolve()
    if not str(path).startswith(str(ws.resolve())):
        print("path escapes workspace", file=sys.stderr)
        return 1
    text = read_document(path)
    pairs = split_by_h2(text)
    sec_dir = ws / settings.sections_dir
    written = write_sections(sec_dir, pairs)
    print(f"wrote {len(written)} sections under {settings.sections_dir}/")
    for p in written:
        print(" ", p.relative_to(ws))
    return 0


def cmd_ingest_run(args: argparse.Namespace) -> int:
    """Import source → draft.md, run supervisor, export merged staging to requested format."""
    from book_pipeline.format_bridge import export_staging_merged, import_source_to_draft
    from book_pipeline.manuscript_session_store import merge_manuscript_goal_text
    from book_pipeline.config import load_settings
    from book_pipeline.supervisor.graph_build import (
        resume_supervisor_after_prep,
        resume_supervisor_post_plan,
        run_supervisor,
        run_supervisor_plan_phase,
        run_supervisor_prep_phase_one,
        run_supervisor_resume_checkpoint,
    )
    from book_pipeline.supervisor.prep_gate import prep_answers_path, prep_plan_prerequisite_ok

    ws = _resolve_workspace_from_run_args(args)
    resume = bool(getattr(args, "resume_plan", False))
    resume_graph = bool(getattr(args, "resume_graph", False))
    plan_gate = bool(getattr(args, "plan_gate", False))
    prep_gate = bool(getattr(args, "prep_gate", False))
    prep_resume = bool(getattr(args, "prep_resume", False))
    auto_prep = bool(getattr(args, "auto_prep", False))

    if not resume and not prep_resume and not auto_prep and not resume_graph and args.input is None:
        print(
            "ingest-run: --input is required unless --resume-plan, --resume-graph, --prep-resume, or --auto-prep",
            file=sys.stderr,
        )
        return 1
    if resume and not (args.thread_id or "").strip():
        print("ingest-run: --resume-plan requires --thread-id (same id from the --plan-gate run)", file=sys.stderr)
        return 1
    if resume_graph and not (args.thread_id or "").strip():
        print(
            "ingest-run: --resume-graph requires --thread-id (same id as the interrupted full supervisor run)",
            file=sys.stderr,
        )
        return 1
    if prep_resume and not (args.thread_id or "").strip():
        print("ingest-run: --prep-resume requires --thread-id (same id from the --prep-gate run)", file=sys.stderr)
        return 1
    if prep_gate or prep_resume or auto_prep:
        st_prep = load_settings(ws, ws / "config.yaml")
        if not st_prep.supervisor_enable_prep_passes:
            print(
                "ingest-run: set supervisor_enable_prep_passes: true in config.yaml to use --prep-gate / --prep-resume / --auto-prep",
                file=sys.stderr,
            )
            return 1

    if not resume and not prep_resume and not resume_graph:
        assert args.input is not None
        inp = args.input.expanduser().resolve()
        try:
            import_source_to_draft(
                ws,
                inp,
                archive_sections=bool(args.archive_sections),
                requested_output_format=args.output_format,
            )
        except (FileNotFoundError, ValueError) as e:
            print(f"ingest: {e}", file=sys.stderr)
            return 1

    tid = (args.thread_id or "").strip() or str(uuid.uuid4())
    goal_text = (args.goal or "").strip()
    gfp = getattr(args, "goal_file", None)
    if gfp is not None:
        gp = Path(gfp).expanduser().resolve()
        if gp.is_file():
            goal_text = gp.read_text(encoding="utf-8", errors="replace").strip()
        elif not goal_text:
            print("ingest-run: --goal-file missing or not a file, and --goal is empty", file=sys.stderr)
            return 1
    goal_f = merge_manuscript_goal_text(
        ws,
        goal_text,
        (args.manuscript_session or "").strip() or None,
        include_manuscript_notes=not args.skip_manuscript_notes,
        extra_sessions=None,
    )
    stream = bool(getattr(args, "stream", False))
    if stream:
        print(
            "ingest-run: streaming supervisor steps to stderr (graph node lines as they complete). "
            "Each LLM call also logs a line like [llm] ollama … (tokens, duration_ms, tok/s). "
            "Set BOOK_PIPELINE_LLM_STATS_STDERR=0 to hide those. "
            "Per-call rows append to .pipeline/ollama_usage.jsonl (and anthropic_usage.jsonl); "
            "Metrics summary refreshes after each LLM call by default; set BOOK_PIPELINE_REFRESH_METRICS_EACH_CALL=0 to skip.",
            flush=True,
        )
    else:
        print(
            "ingest-run: supervisor is running — stdout stays quiet until the full graph finishes. "
            "Use --stream for live node lines on stderr.",
            flush=True,
        )

    if resume:
        pf = getattr(args, "plan_file", None)
        out = resume_supervisor_post_plan(
            ws,
            thread_id=tid,
            plan_file=Path(pf).expanduser() if pf else None,
            stream_progress=stream,
        )
    elif resume_graph:
        out = run_supervisor_resume_checkpoint(ws, thread_id=tid, stream_progress=stream)
    elif auto_prep:
        # One-shot: prep phase one (strategy + artifacts) then immediately continue the full run
        # with answers auto-filled as NONE (no human edit loop).
        out1 = run_supervisor_prep_phase_one(
            ws,
            thread_id=tid,
            user_goal=goal_f,
            goal_preset=args.preset,
            use_openclaw_after_plan=args.use_openclaw and bool(str(args.openclaw_tool).strip()),
            openclaw_tool=args.openclaw_tool,
            openclaw_args_json=args.openclaw_args,
            user_statements_json=getattr(args, "user_statements_json", "") or "",
            use_semantic_division=not getattr(args, "no_semantic_division", False),
            openclaw_per_chunk=getattr(args, "openclaw_per_chunk", False),
            max_revision_rounds=int(getattr(args, "max_revision_rounds", 2) or 0),
            stream_progress=stream,
        )
        if out1.get("error"):
            out = out1
        else:
            ap = prep_answers_path(ws)
            if ap.is_file():
                txt = ap.read_text(encoding="utf-8", errors="replace")
                if "## Answers" in txt and len(txt.strip()) < 2000 and "NONE" not in txt:
                    ap.write_text(txt.rstrip() + "\n\nNONE\n", encoding="utf-8")
            else:
                ap.parent.mkdir(parents=True, exist_ok=True)
                ap.write_text("# Prep answers\n\n## Answers\n\nNONE\n", encoding="utf-8")
            try:
                out = resume_supervisor_after_prep(ws, thread_id=tid, stream_progress=stream)
            except FileNotFoundError as e:
                print(f"ingest-run: {e}", file=sys.stderr)
                return 1
    elif prep_resume:
        try:
            out = resume_supervisor_after_prep(ws, thread_id=tid, stream_progress=stream)
        except FileNotFoundError as e:
            print(f"ingest-run: {e}", file=sys.stderr)
            return 1
    elif plan_gate:
        st_plan = load_settings(ws, ws / "config.yaml")
        if st_plan.supervisor_enable_prep_passes and not getattr(args, "skip_prep_requirement", False):
            prep_ok, prep_msg = prep_plan_prerequisite_ok(ws, tid)
            if not prep_ok:
                print(
                    "ingest-run: --plan-gate is blocked until prep has run: "
                    "supervisor_enable_prep_passes is true in config.yaml.\n"
                    f"  {prep_msg}\n"
                    "  Use the same --thread-id for --prep-gate as for this plan (then edit "
                    "outputs/human_input_answers.md before --prep-resume if you continue the prep flow).\n"
                    "  To plan without that check: --skip-prep-requirement\n",
                    file=sys.stderr,
                )
                return 1
        out = run_supervisor_plan_phase(
            ws,
            thread_id=tid,
            user_goal=goal_f,
            goal_preset=args.preset,
            use_openclaw_after_plan=args.use_openclaw and bool(str(args.openclaw_tool).strip()),
            openclaw_tool=args.openclaw_tool,
            openclaw_args_json=args.openclaw_args,
            user_statements_json=getattr(args, "user_statements_json", "") or "",
            use_semantic_division=not getattr(args, "no_semantic_division", False),
            openclaw_per_chunk=getattr(args, "openclaw_per_chunk", False),
            max_revision_rounds=int(getattr(args, "max_revision_rounds", 2) or 0),
            stream_progress=stream,
        )
    elif prep_gate:
        out = run_supervisor_prep_phase_one(
            ws,
            thread_id=tid,
            user_goal=goal_f,
            goal_preset=args.preset,
            use_openclaw_after_plan=args.use_openclaw and bool(str(args.openclaw_tool).strip()),
            openclaw_tool=args.openclaw_tool,
            openclaw_args_json=args.openclaw_args,
            user_statements_json=getattr(args, "user_statements_json", "") or "",
            use_semantic_division=not getattr(args, "no_semantic_division", False),
            openclaw_per_chunk=getattr(args, "openclaw_per_chunk", False),
            max_revision_rounds=int(getattr(args, "max_revision_rounds", 2) or 0),
            stream_progress=stream,
        )
    else:
        out = run_supervisor(
            ws,
            thread_id=tid,
            user_goal=goal_f,
            goal_preset=args.preset,
            use_openclaw_after_plan=args.use_openclaw and bool(str(args.openclaw_tool).strip()),
            openclaw_tool=args.openclaw_tool,
            openclaw_args_json=args.openclaw_args,
            user_statements_json=getattr(args, "user_statements_json", "") or "",
            use_semantic_division=not getattr(args, "no_semantic_division", False),
            openclaw_per_chunk=getattr(args, "openclaw_per_chunk", False),
            max_revision_rounds=int(getattr(args, "max_revision_rounds", 2) or 0),
            stream_progress=stream,
        )

    print("thread_id:", tid)
    logs = out.get("log") or []
    if stream:
        print("\n--- run log (last 15 lines); full trace was streamed on stderr ---\n", flush=True)
        for line in logs[-15:]:
            print(line, flush=True)
    else:
        for line in logs:
            print(line, flush=True)
    if out.get("error"):
        print("error:", out["error"], file=sys.stderr)
        return 1
    print("staging:", out.get("staging_path", ""))
    if plan_gate and not resume:
        print("\nPlan gate: edit", ws / "outputs" / "plan_for_review.md", flush=True)
        print(
            "Then: python -m book_pipeline ingest-run --workspace … --resume-plan "
            f"--thread-id {tid} --output-format {args.output_format} …\n",
            flush=True,
        )
        if getattr(args, "verbose", False):
            _print_supervisor_verbose(out)
        return 0
    if prep_gate and not prep_resume:
        print("\nPrep gate: answer questions in", ws / "outputs" / "human_input_answers.md", flush=True)
        print(
            "Then: python -m book_pipeline ingest-run --workspace … --prep-resume "
            f"--thread-id {tid} --output-format {args.output_format} …\n",
            flush=True,
        )
        if getattr(args, "verbose", False):
            _print_supervisor_verbose(out)
        return 0
    try:
        name = (args.output_name or "").strip() or None
        export_res = export_staging_merged(
            ws,
            args.output_format,
            output_name=name,
            stamp_filename=not getattr(args, "no_export_stamp", False),
        )
    except (FileNotFoundError, ValueError) as e:
        print(f"export: {e}", file=sys.stderr)
        return 1
    final = export_res.path
    print("exported:", final.relative_to(ws))
    wb = export_res.words_before
    print(
        "word_count: before="
        + (str(wb) if wb is not None else "n/a")
        + f" after={export_res.words_after} "
        f"(staging_merged.md words={export_res.staging_merged_words})",
        flush=True,
    )
    if export_res.before_source:
        print(f"word_count_before_source: {export_res.before_source}", flush=True)
    print("export_metrics: .pipeline/export_last.json", flush=True)
    if getattr(args, "verbose", False):
        _print_supervisor_verbose(out)
    return 0


def cmd_verify(ws: Path) -> int:
    settings = load_settings(ws, ws / "config.yaml")
    try:
        names = ollama_tags(settings.ollama_base_url)
    except Exception as e:  # noqa: BLE001
        print(f"ollama: unreachable ({e})", file=sys.stderr)
        return 1
    print("ollama models:", ", ".join(names) if names else "(none)")
    if settings.ollama_model not in names and names:
        print(
            f"warning: {settings.ollama_model!r} not in tag list "
            f"(pull it or fix OLLAMA_MODEL)",
            file=sys.stderr,
        )
    return 0


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Book pipeline (LangGraph + Ollama)")
    sub = p.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("run-once", help="Pop one JSONL task from todo.file and run graph")
    p1.add_argument("--workspace", type=Path, default=Path.cwd())

    p2 = sub.add_parser("enqueue", help="Append a JSON task to todo.file")
    p2.add_argument("--workspace", type=Path, default=Path.cwd())
    p2.add_argument("--json", required=True, help='Task JSON, e.g. {"action":"insights",...}')

    p3 = sub.add_parser("split", help="Split a manuscript into section files by ## headings")
    p3.add_argument("--workspace", type=Path, default=Path.cwd())
    p3.add_argument("--file", required=True, help="Relative path inside workspace")

    p4 = sub.add_parser("verify-ollama", help="List models from local Ollama")
    p4.add_argument("--workspace", type=Path, default=Path.cwd())

    p5 = sub.add_parser("supervisor-run", help="Run LangGraph supervisor once (CLI)")
    p5w = p5.add_mutually_exclusive_group(required=True)
    p5w.add_argument("--workspace", type=Path, default=None)
    p5w.add_argument("--project-id", type=str, default=None, metavar="PROJECT", help="Use book-pipeline/projects/<id>/")
    p5.add_argument("--thread-id", default="", help="Checkpoint thread id (default: random)")
    p5.add_argument("--goal", default="", help="User instructions")
    p5.add_argument(
        "--preset",
        default="rewrite",
        help="rewrite|netflix_script|stage_play|korean_drama_script|feature_film|tv_episodic_arcs|translation_adapt|docs",
    )
    p5.add_argument("--use-openclaw", action="store_true", help="Call OpenClaw tool after plan")
    p5.add_argument("--openclaw-tool", default="", help="Gateway tool name (with --use-openclaw)")
    p5.add_argument("--openclaw-args", default="{}", help="JSON object for tool args")
    p5.add_argument(
        "--manuscript-session",
        default="",
        help="UUID of manuscript lab session under workspace .pipeline/manuscript_sessions/",
    )
    p5.add_argument(
        "--skip-manuscript-notes",
        action="store_true",
        help="Do not merge chunk notes / structure from that session into --goal",
    )
    p5.add_argument(
        "--user-statements-json",
        default="",
        help='Optional JSON array of requirement strings, e.g. \'["Tone: …"]\' (empty: derive from --goal)',
    )
    p5.add_argument(
        "--no-semantic-division",
        action="store_true",
        help="Skip LLM chapter split (division of work) before plan",
    )
    p5.add_argument(
        "--openclaw-per-chunk",
        action="store_true",
        help="Invoke the same OpenClaw tool after each chunk edit (args get chunk_path, excerpt, workspace)",
    )
    p5.add_argument(
        "--max-revision-rounds",
        type=int,
        default=2,
        help="Verifier-driven re-plan/re-edit cycles after staging (0–8)",
    )
    p5.add_argument(
        "--verbose",
        action="store_true",
        help="After run, print thinking_trace (plan/edit/verify reasoning) to stdout",
    )
    p5.add_argument(
        "--stream",
        action="store_true",
        help="Print each graph node to stderr as it finishes (otherwise no output until the run completes)",
    )
    p5.add_argument(
        "--plan-gate",
        action="store_true",
        help="Stop after orchestration plan; edit outputs/plan_for_review.md then supervisor-resume-plan. "
        "If supervisor_enable_prep_passes is true, prep must have completed first (same --thread-id)",
    )
    p5.add_argument(
        "--skip-prep-requirement",
        action="store_true",
        help="With --plan-gate only: allow plan without prep artifacts (when prep passes are enabled in config)",
    )

    p6 = sub.add_parser("ui", help="Start small web UI (FastAPI)")
    p6.add_argument("--workspace", type=Path, default=Path("workspace"))
    p6.add_argument("--host", default="127.0.0.1")
    p6.add_argument("--port", type=int, default=9876)

    p7 = sub.add_parser(
        "supervisor-marathon",
        help="Long-run supervisor: checkpoint after each chunk; resume with same --thread-id (local Ollama)",
    )
    p7w = p7.add_mutually_exclusive_group(required=True)
    p7w.add_argument("--workspace", type=Path, default=None)
    p7w.add_argument("--project-id", type=str, default=None, metavar="PROJECT", help="Use book-pipeline/projects/<id>/")
    p7.add_argument("--thread-id", default="", help="Stable id for resume (default: random new)")
    p7.add_argument("--goal", default="", help="User instructions")
    p7.add_argument(
        "--preset",
        default="netflix_script",
        help="rewrite|netflix_script|stage_play|korean_drama_script|feature_film|tv_episodic_arcs|translation_adapt|docs",
    )
    p7.add_argument("--use-openclaw", action="store_true")
    p7.add_argument("--openclaw-tool", default="")
    p7.add_argument("--openclaw-args", default="{}")
    p7.add_argument("--manuscript-session", default="", help="Merge manuscript lab session into goal (see supervisor-run)")
    p7.add_argument("--skip-manuscript-notes", action="store_true")
    p7.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help="Seconds to sleep between chunk edits (cool GPU / cooperative multitasking)",
    )
    p7.add_argument(
        "--heartbeat",
        type=str,
        default="",
        help="Relative path under workspace for JSON progress (default: .pipeline/marathon_heartbeat.json)",
    )
    p7.add_argument("--user-statements-json", default="", help="See supervisor-run")
    p7.add_argument("--no-semantic-division", action="store_true", help="See supervisor-run")
    p7.add_argument("--openclaw-per-chunk", action="store_true", help="See supervisor-run")
    p7.add_argument("--max-revision-rounds", type=int, default=2, help="See supervisor-run")
    p7.add_argument("--verbose", action="store_true", help="See supervisor-run")

    p8 = sub.add_parser(
        "ingest-run",
        help="Import .txt/.md/.docx → manuscript/draft.md, run supervisor, export merged result to md/txt/docx",
    )
    p8w = p8.add_mutually_exclusive_group(required=True)
    p8w.add_argument("--workspace", type=Path, default=None)
    p8w.add_argument("--project-id", type=str, default=None, metavar="PROJECT", help="Use book-pipeline/projects/<id>/")
    p8g = p8.add_mutually_exclusive_group(required=False)
    p8g.add_argument(
        "--plan-gate",
        action="store_true",
        help="After orchestration plan, stop and write outputs/plan_for_review.md; resume with --resume-plan. "
        "If supervisor_enable_prep_passes is true, prep (--prep-gate, same --thread-id) must have completed first",
    )
    p8g.add_argument(
        "--resume-plan",
        action="store_true",
        help="Continue from a prior --plan-gate run (requires --thread-id; skips import)",
    )
    p8g.add_argument(
        "--prep-gate",
        action="store_true",
        help="Prep: init + strategic plan, then stop; edit outputs/human_input_answers.md; requires supervisor_enable_prep_passes in config",
    )
    p8g.add_argument(
        "--prep-resume",
        action="store_true",
        help="Prep: character + arc memory passes then full supervisor (requires --thread-id from --prep-gate; skips import)",
    )
    p8g.add_argument(
        "--auto-prep",
        action="store_true",
        help="Run prep gate phase one, auto-fill human answers as NONE, then continue with prep memory passes + full supervisor + export (no human edit loop). Requires supervisor_enable_prep_passes in config",
    )
    p8g.add_argument(
        "--resume-graph",
        action="store_true",
        help="Resume full supervisor from .pipeline/checkpoints.sqlite (same --thread-id as the interrupted run; skips import)",
    )
    p8.add_argument(
        "--input",
        type=Path,
        default=None,
        help="Source file (.txt, .md, .docx). Required unless --resume-plan, --resume-graph, --prep-resume, or --auto-prep.",
    )
    p8.add_argument(
        "--output-format",
        default="md",
        choices=["md", "txt", "docx"],
        help="Export format (ignored for --plan-gate until you resume)",
    )
    p8.add_argument(
        "--output-name",
        default="",
        help="Optional basename under outputs/ (e.g. mybook.docx). Default files get _YYYYMMDD_HHMMSS before the extension unless --no-export-stamp",
    )
    p8.add_argument(
        "--no-export-stamp",
        action="store_true",
        help="Omit run timestamp from the export filename (fixed name as given or default)",
    )
    p8.add_argument(
        "--skip-prep-requirement",
        action="store_true",
        help="With --plan-gate only: allow plan even if prep artifacts are missing (when prep passes are enabled in config)",
    )
    p8.add_argument(
        "--archive-sections",
        action="store_true",
        help="Move existing sections/*.md into .pipeline/archived_sections_* so the new draft is used",
    )
    p8.add_argument("--thread-id", default="", help="Checkpoint thread id (default: random)")
    p8.add_argument("--goal", default="", help="User instructions (merged with optional manuscript session)")
    p8.add_argument(
        "--goal-file",
        type=Path,
        default=None,
        help="Read goal instructions from this UTF-8 file (overrides --goal when the file exists)",
    )
    p8.add_argument(
        "--preset",
        default="rewrite",
        help="rewrite|netflix_script|stage_play|korean_drama_script|feature_film|tv_episodic_arcs|translation_adapt|docs",
    )
    p8.add_argument("--use-openclaw", action="store_true")
    p8.add_argument("--openclaw-tool", default="")
    p8.add_argument("--openclaw-args", default="{}")
    p8.add_argument("--manuscript-session", default="")
    p8.add_argument("--skip-manuscript-notes", action="store_true")
    p8.add_argument("--user-statements-json", default="")
    p8.add_argument("--no-semantic-division", action="store_true")
    p8.add_argument("--openclaw-per-chunk", action="store_true")
    p8.add_argument("--max-revision-rounds", type=int, default=2)
    p8.add_argument("--verbose", action="store_true", help="See supervisor-run")
    p8.add_argument("--stream", action="store_true", help="See supervisor-run")
    p8.add_argument(
        "--plan-file",
        type=Path,
        default=None,
        help="With --resume-plan: markdown inside workspace (default: outputs/plan_for_review.md)",
    )

    p_resume = sub.add_parser(
        "supervisor-resume-plan",
        help="Step 2 of human plan gate: balance→edit→verify after editing outputs/plan_for_review.md",
    )
    p_resw = p_resume.add_mutually_exclusive_group(required=True)
    p_resw.add_argument("--workspace", type=Path, default=None)
    p_resw.add_argument("--project-id", type=str, default=None, metavar="PROJECT")
    p_resume.add_argument(
        "--thread-id",
        required=True,
        help="Same --thread-id used for the matching ingest-run/supervisor-run --plan-gate",
    )
    p_resume.add_argument(
        "--plan-file",
        type=Path,
        default=None,
        help="Markdown inside workspace (default: outputs/plan_for_review.md)",
    )
    p_resume.add_argument("--stream", action="store_true")
    p_resume.add_argument("--verbose", action="store_true")

    p9 = sub.add_parser(
        "init-project",
        help="Create or refresh book-pipeline/projects/<id>/ (config + dirs + project_learnings.md)",
    )
    p9.add_argument("project_id", help="Project directory name under the projects root")

    args = p.parse_args(argv)
    if args.cmd == "run-once":
        raise SystemExit(cmd_run_once(args.workspace))
    if args.cmd == "enqueue":
        raise SystemExit(cmd_enqueue(args.workspace, args.json))
    if args.cmd == "split":
        raise SystemExit(cmd_split(args.workspace, args.file))
    if args.cmd == "verify-ollama":
        raise SystemExit(cmd_verify(args.workspace))
    if args.cmd == "init-project":
        from book_pipeline.project_workspace import ensure_project_layout, project_workspace_path

        ws = ensure_project_layout(project_workspace_path(args.project_id))
        print(ws)
        raise SystemExit(0)
    if args.cmd == "supervisor-resume-plan":
        from book_pipeline.supervisor.graph_build import resume_supervisor_post_plan

        ws = _resolve_workspace_from_run_args(args)
        tid = args.thread_id.strip()
        pf = getattr(args, "plan_file", None)
        stream = bool(args.stream)
        if stream:
            print("supervisor-resume-plan: streaming to stderr…", flush=True)
        out = resume_supervisor_post_plan(
            ws,
            thread_id=tid,
            plan_file=Path(pf).expanduser() if pf else None,
            stream_progress=stream,
        )
        for line in (out.get("log") or [])[-25:]:
            print(line, flush=True)
        if out.get("error"):
            print("error:", out["error"], file=sys.stderr)
            raise SystemExit(1)
        print("staging:", out.get("staging_path", ""))
        if args.verbose:
            _print_supervisor_verbose(out)
        raise SystemExit(0)
    if args.cmd == "ingest-run":
        raise SystemExit(cmd_ingest_run(args))
    if args.cmd == "supervisor-run":
        from book_pipeline.manuscript_session_store import merge_manuscript_goal_text
        from book_pipeline.supervisor.graph_build import run_supervisor, run_supervisor_plan_phase

        tid = (args.thread_id or "").strip() or str(uuid.uuid4())
        ws = _resolve_workspace_from_run_args(args)
        goal_f = merge_manuscript_goal_text(
            ws,
            args.goal,
            (args.manuscript_session or "").strip() or None,
            include_manuscript_notes=not args.skip_manuscript_notes,
            extra_sessions=None,
        )
        stream = bool(getattr(args, "stream", False))
        if stream:
            print(
                "supervisor-run: streaming graph steps to stderr…",
                flush=True,
            )
        else:
            print(
                "supervisor-run: no output until the graph completes (try --stream for live stderr lines)…",
                flush=True,
            )
        if getattr(args, "plan_gate", False):
            from book_pipeline.supervisor.prep_gate import prep_plan_prerequisite_ok

            _st = load_settings(ws, ws / "config.yaml")
            if _st.supervisor_enable_prep_passes and not getattr(args, "skip_prep_requirement", False):
                _ok, _msg = prep_plan_prerequisite_ok(ws, tid)
                if not _ok:
                    print(
                        "supervisor-run: --plan-gate is blocked until prep has run: "
                        "supervisor_enable_prep_passes is true in config.yaml.\n"
                        f"  {_msg}\n"
                        "  Run ingest-run --prep-gate with the same --thread-id (and your usual --input), "
                        "or use --skip-prep-requirement.\n",
                        file=sys.stderr,
                    )
                    raise SystemExit(1)
            out = run_supervisor_plan_phase(
                ws,
                thread_id=tid,
                user_goal=goal_f,
                goal_preset=args.preset,
                use_openclaw_after_plan=args.use_openclaw and bool(str(args.openclaw_tool).strip()),
                openclaw_tool=args.openclaw_tool,
                openclaw_args_json=args.openclaw_args,
                user_statements_json=getattr(args, "user_statements_json", "") or "",
                use_semantic_division=not getattr(args, "no_semantic_division", False),
                openclaw_per_chunk=getattr(args, "openclaw_per_chunk", False),
                max_revision_rounds=int(getattr(args, "max_revision_rounds", 2) or 0),
                stream_progress=stream,
            )
        else:
            out = run_supervisor(
                ws,
                thread_id=tid,
                user_goal=goal_f,
                goal_preset=args.preset,
                use_openclaw_after_plan=args.use_openclaw and bool(str(args.openclaw_tool).strip()),
                openclaw_tool=args.openclaw_tool,
                openclaw_args_json=args.openclaw_args,
                user_statements_json=getattr(args, "user_statements_json", "") or "",
                use_semantic_division=not getattr(args, "no_semantic_division", False),
                openclaw_per_chunk=getattr(args, "openclaw_per_chunk", False),
                max_revision_rounds=int(getattr(args, "max_revision_rounds", 2) or 0),
                stream_progress=stream,
            )
        print("thread_id:", tid)
        logs = out.get("log") or []
        if stream:
            print("\n--- run log (last 15 lines) ---\n", flush=True)
            for line in logs[-15:]:
                print(line, flush=True)
        else:
            for line in logs:
                print(line, flush=True)
        if out.get("error"):
            print("error:", out["error"], file=sys.stderr)
            raise SystemExit(1)
        if getattr(args, "plan_gate", False) and not out.get("error"):
            print("Plan gate: edit", ws / "outputs" / "plan_for_review.md", flush=True)
            print(
                "Then: python -m book_pipeline supervisor-resume-plan "
                f"--workspace {ws} --thread-id {tid}\n",
                flush=True,
            )
        if out.get("staging_path"):
            print("staging:", out.get("staging_path", ""))
        if getattr(args, "verbose", False):
            _print_supervisor_verbose(out)
        raise SystemExit(0)
    if args.cmd == "ui":
        import uvicorn

        ws = args.workspace.resolve()
        print(f"UI: http://{args.host}:{args.port}/?workspace={ws}")
        uvicorn.run(
            "book_pipeline.ui_app:app",
            host=args.host,
            port=args.port,
            reload=False,
        )
    if args.cmd == "supervisor-marathon":
        from book_pipeline.manuscript_session_store import merge_manuscript_goal_text
        from book_pipeline.supervisor.graph_build import run_supervisor_marathon

        ws = _resolve_workspace_from_run_args(args)
        tid = (args.thread_id or "").strip() or str(uuid.uuid4())
        hb_raw = (args.heartbeat or "").strip()
        hb_path = (ws / hb_raw).resolve() if hb_raw else (ws / ".pipeline" / "marathon_heartbeat.json").resolve()
        try:
            ok_rel = hb_path == ws or hb_path.is_relative_to(ws)
        except AttributeError:
            ok_rel = str(hb_path).startswith(str(ws) + str(Path.sep))
        if not ok_rel:
            print("heartbeat path must be inside workspace", file=sys.stderr)
            raise SystemExit(1)

        def on_step(state: dict, tail: list[str]) -> None:
            idx = int(state.get("chunk_index") or 0)
            n = len(state.get("chunks") or [])
            if tail:
                print(f"[marathon {idx}/{n}] {tail[-1]}")

        goal_f = merge_manuscript_goal_text(
            ws,
            args.goal,
            (args.manuscript_session or "").strip() or None,
            include_manuscript_notes=not args.skip_manuscript_notes,
            extra_sessions=None,
        )
        out = run_supervisor_marathon(
            ws,
            thread_id=tid,
            user_goal=goal_f,
            goal_preset=args.preset,
            use_openclaw_after_plan=args.use_openclaw and bool(str(args.openclaw_tool).strip()),
            openclaw_tool=args.openclaw_tool,
            openclaw_args_json=args.openclaw_args,
            user_statements_json=getattr(args, "user_statements_json", "") or "",
            use_semantic_division=not getattr(args, "no_semantic_division", False),
            openclaw_per_chunk=getattr(args, "openclaw_per_chunk", False),
            max_revision_rounds=int(getattr(args, "max_revision_rounds", 2) or 0),
            sleep_s=float(args.sleep),
            heartbeat_path=hb_path,
            on_step=on_step,
        )
        print("thread_id:", tid)
        for line in (out.get("log") or [])[-20:]:
            print(line)
        if out.get("error"):
            print("error:", out["error"], file=sys.stderr)
            raise SystemExit(1)
        print("staging:", out.get("staging_path", ""))
        if getattr(args, "verbose", False):
            _print_supervisor_verbose(out)
        raise SystemExit(0)
    raise SystemExit(2)


if __name__ == "__main__":
    main()
