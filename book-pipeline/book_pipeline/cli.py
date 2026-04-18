from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from book_pipeline.config import load_settings
from book_pipeline.graph import run_task
from book_pipeline.ingest import read_document, split_by_h2, write_sections
from book_pipeline.ollama_client import ollama_tags
from book_pipeline.queue import append_task, pop_task


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
    p5.add_argument("--workspace", type=Path, required=True)
    p5.add_argument("--thread-id", default="", help="Checkpoint thread id (default: random)")
    p5.add_argument("--goal", default="", help="User instructions")
    p5.add_argument("--preset", default="rewrite", help="rewrite|netflix_script|stage_play|docs")
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

    p6 = sub.add_parser("ui", help="Start small web UI (FastAPI)")
    p6.add_argument("--workspace", type=Path, default=Path("workspace"))
    p6.add_argument("--host", default="127.0.0.1")
    p6.add_argument("--port", type=int, default=9876)

    p7 = sub.add_parser(
        "supervisor-marathon",
        help="Long-run supervisor: checkpoint after each chunk; resume with same --thread-id (local Ollama)",
    )
    p7.add_argument("--workspace", type=Path, required=True)
    p7.add_argument("--thread-id", default="", help="Stable id for resume (default: random new)")
    p7.add_argument("--goal", default="", help="User instructions")
    p7.add_argument("--preset", default="netflix_script", help="rewrite|netflix_script|stage_play|docs")
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

    args = p.parse_args(argv)
    if args.cmd == "run-once":
        raise SystemExit(cmd_run_once(args.workspace))
    if args.cmd == "enqueue":
        raise SystemExit(cmd_enqueue(args.workspace, args.json))
    if args.cmd == "split":
        raise SystemExit(cmd_split(args.workspace, args.file))
    if args.cmd == "verify-ollama":
        raise SystemExit(cmd_verify(args.workspace))
    if args.cmd == "supervisor-run":
        from book_pipeline.manuscript_session_store import merge_manuscript_goal_text
        from book_pipeline.supervisor.graph_build import run_supervisor

        tid = (args.thread_id or "").strip() or str(uuid.uuid4())
        ws = args.workspace.resolve()
        goal_f = merge_manuscript_goal_text(
            ws,
            args.goal,
            (args.manuscript_session or "").strip() or None,
            include_manuscript_notes=not args.skip_manuscript_notes,
            extra_sessions=None,
        )
        out = run_supervisor(
            ws,
            thread_id=tid,
            user_goal=goal_f,
            goal_preset=args.preset,
            use_openclaw_after_plan=args.use_openclaw and bool(str(args.openclaw_tool).strip()),
            openclaw_tool=args.openclaw_tool,
            openclaw_args_json=args.openclaw_args,
        )
        print("thread_id:", tid)
        for line in out.get("log") or []:
            print(line)
        if out.get("error"):
            print("error:", out["error"], file=sys.stderr)
            raise SystemExit(1)
        print("staging:", out.get("staging_path", ""))
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

        ws = args.workspace.resolve()
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
        raise SystemExit(0)
    raise SystemExit(2)


if __name__ == "__main__":
    main()
