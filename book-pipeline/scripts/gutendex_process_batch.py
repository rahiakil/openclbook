#!/usr/bin/env python3
"""
For each cleaned Gutendex title: ask Ollama for transformation ideas (JSON), init a project,
run ``ingest-run`` fully (streaming, serial workers via env), no human gates.

Prereqs: Ollama up; ``python scripts/gutendex_download_batch.py`` already produced ``gutenberg_library/manifest.json``.

Usage:
  export SUPERVISOR_PARALLEL_WORKERS=1
  python scripts/gutendex_process_batch.py \\
    --manifest gutenberg_library/manifest.json \\
    --settings-workspace workspace \\
    --limit 3

Optional: ``--max-input-chars 120000`` to truncate huge books for faster demo runs.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from book_pipeline.config import load_settings  # noqa: E402
from book_pipeline.llm_complete import complete_chat  # noqa: E402
from book_pipeline.project_workspace import projects_root, sanitize_project_id  # noqa: E402
from book_pipeline.supervisor.orchestration import extract_json_object  # noqa: E402

PRESETS = [
    "netflix_script",
    "feature_film",
    "stage_play",
    "korean_drama_script",
    "tv_episodic_arcs",
    "translation_adapt",
    "rewrite",
    "docs",
]


def slug_project_id(gut_id: int, title: str) -> str:
    raw = re.sub(r"[^a-zA-Z0-9._-]+", "-", f"gut-{gut_id}-{title}").strip("-") or f"gut-{gut_id}"
    return sanitize_project_id(raw)


def brainstorm(
    settings_workspace: Path,
    *,
    excerpt: str,
    meta: dict,
    ollama_ctx: int | None,
) -> dict:
    settings = load_settings(settings_workspace, settings_workspace / "config.yaml")
    sys_prompt = (
        "You help film/show writers brainstorm ADAPTATIONS of public-domain literature.\n"
        "Output ONLY JSON (no markdown fences) with keys:\n"
        '  "freeform_ideas": string (2-4 sentences of wild creative directions),\n'
        '  "preset": one of '
        + json.dumps(PRESETS)
        + ",\n"
        '  "user_goal": string (single paragraph: concrete adaptation brief for an LLM rewriter),\n'
        '  "user_statements": string[] (3-8 hard requirements, e.g. era, tone mix, format, casting notes),\n'
        '  "one_line_pitch": string\n'
        "Pick a preset that best matches user_goal. Be specific and shootable."
    )
    user = (
        f"METADATA:\n{json.dumps({k: meta.get(k) for k in ('id', 'title', 'authors', 'languages', 'download_count', 'gutendex_url')}, indent=2)}\n\n"
        f"EXCERPT (first part of cleaned text):\n{excerpt}\n"
    )
    raw, _thinking, _meta = complete_chat(
        settings,
        llm_provider_override="ollama",
        messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}],
        temperature=0.45,
        workspace=settings_workspace,
        tag="gutendex-batch-brainstorm",
        ollama_num_ctx=ollama_ctx,
    )
    obj = extract_json_object(raw) or {}
    if not isinstance(obj, dict):
        return {}
    return obj


def main() -> int:
    ap = argparse.ArgumentParser(description="Batch: Ollama brief → init project → ingest-run (no HITL)")
    ap.add_argument("--manifest", type=Path, default=ROOT / "gutenberg_library" / "manifest.json")
    ap.add_argument(
        "--settings-workspace",
        type=Path,
        default=ROOT / "workspace",
        help="Directory with config.yaml (Ollama URL/model) for brainstorm LLM calls",
    )
    ap.add_argument("--library-root", type=Path, default=ROOT / "gutenberg_library", help="Root that contains manifest paths")
    ap.add_argument("--limit", type=int, default=0, help="Process at most N books (0 = all in manifest)")
    ap.add_argument("--start-index", type=int, default=0, help="Skip first N manifest entries")
    ap.add_argument(
        "--only-ids",
        type=str,
        default="",
        help="Comma-separated Gutenberg numeric ids to process (subset of manifest after start-index/limit)",
    )
    ap.add_argument(
        "--max-input-chars",
        type=int,
        default=0,
        help="If >0, truncate clean text to this many chars before copying into the project (demo speed)",
    )
    ap.add_argument(
        "--excerpt-chars",
        type=int,
        default=14_000,
        help="How much of the clean text to show the brainstorm model",
    )
    ap.add_argument(
        "--brainstorm-ctx",
        type=int,
        default=8192,
        help="Ollama num_ctx for the small brainstorm call only",
    )
    ap.add_argument("--sleep-between", type=float, default=5.0, help="Seconds idle between pipeline runs")
    args = ap.parse_args()

    manifest_path = args.manifest.expanduser().resolve()
    if not manifest_path.is_file():
        print(f"missing manifest: {manifest_path}", file=sys.stderr)
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    books: list[dict] = list(manifest.get("books") or [])
    books = books[args.start_index :]
    if args.limit and args.limit > 0:
        books = books[: args.limit]
    only_raw = (args.only_ids or "").strip()
    if only_raw:
        only = {int(x.strip()) for x in only_raw.split(",") if x.strip().isdigit()}
        books = [b for b in books if int(b.get("id") or 0) in only]
        if not books:
            print(f"no manifest entries match --only-ids {only_raw!r}", file=sys.stderr)
            return 1

    settings_ws = args.settings_workspace.expanduser().resolve()
    lib_root = args.library_root.expanduser().resolve()
    runs_path = lib_root / "library_runs.jsonl"
    lib_root.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("SUPERVISOR_PARALLEL_WORKERS", "1")

    for i, meta in enumerate(books):
        bid = int(meta.get("id") or 0)
        title = (meta.get("title") or f"book-{bid}").strip()
        rel_clean = (meta.get("paths") or {}).get("clean")
        if not rel_clean:
            print(f"skip id={bid} (no clean path in manifest)", flush=True)
            continue
        clean_path = (lib_root / rel_clean).resolve()
        if not clean_path.is_file():
            print(f"skip id={bid} missing file {clean_path}", flush=True)
            continue

        raw_text = clean_path.read_text(encoding="utf-8", errors="replace")
        body = raw_text.strip()
        if args.max_input_chars and len(body) > args.max_input_chars:
            body = body[: args.max_input_chars].rstrip() + "\n\n[TRUNCATED_FOR_BATCH_DEMO]\n"

        excerpt = body[: args.excerpt_chars]
        print(f"\n=== [{i+1}/{len(books)}] brainstorm id={bid} {title[:70]!r}", flush=True)
        plan = brainstorm(
            settings_ws,
            excerpt=excerpt,
            meta=meta,
            ollama_ctx=args.brainstorm_ctx,
        )
        preset = str(plan.get("preset") or "netflix_script").strip()
        if preset not in PRESETS:
            preset = "netflix_script"
        goal = str(plan.get("user_goal") or "").strip() or (
            f"Bold adaptation of this public-domain work: {title}. Explore contemporary resonance while "
            "respecting the original plot spine unless you intentionally restructure for drama."
        )
        stmts = plan.get("user_statements")
        if not isinstance(stmts, list):
            stmts = []
        stmts_json = json.dumps([str(s).strip() for s in stmts if str(s).strip()], ensure_ascii=False)

        project_id = slug_project_id(bid, title)
        print(f"  project_id={project_id} preset={preset}", flush=True)

        subprocess.run(
            [sys.executable, "-m", "book_pipeline", "init-project", project_id],
            cwd=str(ROOT),
            check=True,
        )
        proj_root = (projects_root() / project_id).resolve()
        goal_file = proj_root / ".pipeline" / "gutendex_batch_goal.txt"
        goal_file.parent.mkdir(parents=True, exist_ok=True)
        goal_file.write_text(goal, encoding="utf-8")
        brief_file = proj_root / ".pipeline" / "gutendex_batch_brainstorm.json"
        brief_file.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")

        # Write (possibly truncated) manuscript source for ingest
        ingest_inp = proj_root / ".pipeline" / "gutendex_source.txt"
        ingest_inp.write_text(body, encoding="utf-8")

        tid = f"gutendex-batch-{bid}"
        cmd = [
            sys.executable,
            "-m",
            "book_pipeline",
            "ingest-run",
            "--project-id",
            project_id,
            "--input",
            str(ingest_inp),
            "--preset",
            preset,
            "--goal-file",
            str(goal_file),
            "--user-statements-json",
            stmts_json,
            "--thread-id",
            tid,
            "--output-format",
            "txt",
            "--archive-sections",
            "--stream",
        ]
        print("  ingest-run:", " ".join(cmd[2:]), flush=True)
        t0 = time.time()
        r = subprocess.run(cmd, cwd=str(ROOT))
        elapsed = time.time() - t0
        rec = {
            "gutenberg_id": bid,
            "project_id": project_id,
            "preset": preset,
            "thread_id": tid,
            "exit_code": r.returncode,
            "elapsed_sec": round(elapsed, 2),
            "brainstorm": plan,
            "manifest_title": title,
        }
        with runs_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if r.returncode != 0:
            print(f"  ingest-run FAILED exit={r.returncode} (continuing)", flush=True)
        time.sleep(max(0.0, args.sleep_between))

    print(f"\nappend run log: {runs_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
