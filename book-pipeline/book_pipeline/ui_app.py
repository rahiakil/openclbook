from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, ValidationError

import httpx

from book_pipeline.config import Settings, load_settings
from book_pipeline.ingest import read_document_from_bytes
from book_pipeline.manuscript_lab import analyze_manuscript_structure, chunk_manuscript, comments_to_goal_block
from book_pipeline.manuscript_session_store import (
    commit_manuscript_session,
    list_session_meta,
    load_session,
    merge_manuscript_goal_text,
    persist_session,
)
from book_pipeline.project_scaffold import allocate_slug_dir, create_project_workspace
from book_pipeline.project_workspace import projects_root as book_pipeline_projects_root
from book_pipeline.projects_library_scan import (
    list_pipeline_output_files,
    read_artifact,
    read_outputs_workspace_file,
    scan_local_completed_projects,
)
from book_pipeline.studio_db import (
    connect,
    ensure_default_user,
    get_project,
    get_user,
    insert_project,
    list_projects,
    projects_root,
    studio_db_path,
    update_user_display_name,
)
from book_pipeline.supervisor.graph_build import (
    get_supervisor_app,
    run_supervisor,
    supervisor_guided_step,
)
from book_pipeline.usage_stats import (
    aggregate_anthropic_usage,
    aggregate_ollama_usage,
    estimate_cloud_cost_usd,
    rough_pass_estimate,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"

_RUN_LOCK = threading.Lock()
_RUN_STATUS: dict[str, str] = {}

_MS_LOCK = threading.Lock()
_MS_SESSIONS: dict[str, dict] = {}


def _disk_persist(session: dict) -> None:
    wsf = str(session.get("workspace_field") or "").strip()
    if not wsf:
        return
    ws = Path(wsf).expanduser().resolve()
    if not ws.is_dir():
        return
    try:
        persist_session(ws, session)
    except (OSError, ValueError, TypeError):
        pass


def _ms_put(session: dict) -> None:
    sid = str(session.get("id") or "").strip()
    if not sid:
        return
    with _MS_LOCK:
        _MS_SESSIONS[sid] = session
    _disk_persist(session)


def _ms_require(session_id: str, workspace_query: str = "") -> dict:
    with _MS_LOCK:
        s = _MS_SESSIONS.get(session_id)
    if s is not None:
        return s
    wq = (workspace_query or "").strip()
    if not wq:
        raise HTTPException(
            404,
            "unknown manuscript session — pass ?workspace=BOOK_DIR to load from "
            "BOOK_DIR/.pipeline/manuscript_sessions/ after a UI restart",
        )
    ws = Path(wq).expanduser().resolve()
    if not ws.is_dir():
        raise HTTPException(400, "bad workspace for session load")
    s = load_session(ws, session_id)
    if not s:
        raise HTTPException(404, "unknown manuscript session")
    with _MS_LOCK:
        _MS_SESSIONS[session_id] = s
    return s


# Typical dev layout for this operator; only applied when the path exists (or env override).
_DEV_WORKSPACE_FALLBACK = Path("/home/papa/doc")


def _manuscript_samples_api_base() -> str:
    """HTTP API origin for GET /v1/manuscript-samples (no trailing slash). From env only."""
    return (os.environ.get("MANUSCRIPT_SAMPLES_API_BASE") or "").strip().rstrip("/")


def suggested_default_workspace() -> str:
    """
    Default book workspace for the web UI when not passed on the URL.

    Priority: BOOK_WORKSPACE_DEFAULT env → existing /home/papa/doc directory → empty.
    """
    env = (os.environ.get("BOOK_WORKSPACE_DEFAULT") or "").strip()
    if env:
        p = Path(env).expanduser().resolve()
        return str(p) if p.is_dir() else ""
    if _DEV_WORKSPACE_FALLBACK.is_dir():
        return str(_DEV_WORKSPACE_FALLBACK.resolve())
    return ""


# Request body models must be module-level — classes nested inside ``create_app()`` are not
# fully resolved for OpenAPI/Pydantic and FastAPI returns 422 (e.g. ``loc``: ``["query", "body"]``).


class RunBody(BaseModel):
    workspace: str = Field(description="Absolute or relative path to book workspace")
    llm_provider: str | None = Field(
        default=None,
        description="ollama | anthropic — overrides workspace config for this run",
    )
    thread_id: str | None = None
    user_goal: str = ""
    goal_preset: str = "rewrite"
    use_openclaw_after_plan: bool = False
    openclaw_tool: str = ""
    openclaw_args_json: str = "{}"
    manuscript_session_id: str | None = None
    include_manuscript_notes: bool = True
    # Orchestration: semantic chapter split, verify loop, optional per-chunk OpenClaw
    user_statements_json: str = Field(
        default="",
        description='Optional JSON array of strings, e.g. ["Tone: …","Plot: …"]. Empty = derive from user_goal.',
    )
    use_semantic_division: bool = Field(
        default=True,
        description="Run division-of-work LLM to split a single draft into chapter chunks before plan/edit.",
    )
    openclaw_per_chunk: bool = Field(
        default=False,
        description="After each chunk LLM edit, call the same OpenClaw tool with chunk_path/excerpt args.",
    )
    max_revision_rounds: int = Field(
        default=2,
        ge=0,
        le=8,
        description="After staging, verifier may send graph back to re-plan/re-edit; max full cycles.",
    )


class MergeBody(BaseModel):
    workspace: str


class StudioMePatch(BaseModel):
    display_name: str = "local"


class CreateProjectBody(BaseModel):
    user_id: int = 1
    name: str = ""


class MemoryWriteBody(BaseModel):
    workspace: str
    path: str = ""
    content: str = ""


class ManuscriptCommitBody(BaseModel):
    workspace: str
    mode: str = "draft"


class ManuscriptCommentBody(BaseModel):
    chunk_id: int = Field(ge=0)
    body: str = ""


class ManuscriptGlobalNoteBody(BaseModel):
    workspace: str = ""
    text: str = ""


def merge_goal(ws: Path, b: RunBody) -> str:
    with _MS_LOCK:
        snap = dict(_MS_SESSIONS)
    return merge_manuscript_goal_text(
        ws,
        b.user_goal,
        b.manuscript_session_id,
        b.include_manuscript_notes,
        extra_sessions=snap,
    )


def create_app() -> FastAPI:
    app = FastAPI(title="Book supervisor UI", version="0.2.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/")
    async def index():
        index_path = STATIC_DIR / "index.html"
        if not index_path.is_file():
            raise HTTPException(404, "Missing static/index.html")
        return FileResponse(index_path)

    @app.get("/api/health")
    async def health():
        return {
            "ok": True,
            "studio_api": True,
            "app": "book_pipeline.ui_app",
            "manuscript_samples_proxy": bool(_manuscript_samples_api_base()),
        }

    @app.get("/api/ui-defaults")
    async def ui_defaults():
        """Browser UI: suggested workspace when URL/localStorage do not set one."""
        ws = suggested_default_workspace()
        pipeline_pr = book_pipeline_projects_root()
        api_base = _manuscript_samples_api_base()
        return {
            "workspace": ws,
            "has_dev_default": bool(ws),
            "projects_root": str(pipeline_pr.resolve()),
            "manuscript_samples_api_base": api_base or None,
            "manuscript_samples_proxy_configured": bool(api_base),
        }

    @app.post("/api/supervisor/run")
    async def supervisor_run(payload: RunBody):
        ws = Path(payload.workspace).expanduser().resolve()
        if not ws.is_dir():
            raise HTTPException(400, f"workspace not a directory: {ws}")
        tid = (payload.thread_id or "").strip() or str(uuid.uuid4())
        with _RUN_LOCK:
            if _RUN_STATUS.get(tid) == "running":
                raise HTTPException(409, "thread_id run already in progress")
            _RUN_STATUS[tid] = "running"

        def work() -> None:
            try:
                goal_final = merge_goal(ws, payload)
                run_supervisor(
                    ws,
                    thread_id=tid,
                    user_goal=goal_final,
                    goal_preset=payload.goal_preset,
                    use_openclaw_after_plan=payload.use_openclaw_after_plan,
                    openclaw_tool=payload.openclaw_tool,
                    openclaw_args_json=payload.openclaw_args_json,
                    llm_provider=payload.llm_provider,
                    user_statements_json=payload.user_statements_json,
                    use_semantic_division=payload.use_semantic_division,
                    openclaw_per_chunk=payload.openclaw_per_chunk,
                    max_revision_rounds=payload.max_revision_rounds,
                )
                _RUN_STATUS[tid] = "done"
            except Exception as e:  # noqa: BLE001
                _RUN_STATUS[tid] = f"error: {e}"

        threading.Thread(target=work, daemon=True).start()
        return {"thread_id": tid, "status": "started"}

    @app.get("/api/supervisor/status/{thread_id}")
    async def supervisor_status(thread_id: str):
        return {"thread_id": thread_id, "run": _RUN_STATUS.get(thread_id, "unknown")}

    @app.get("/api/supervisor/state/{thread_id}")
    async def supervisor_state(
        thread_id: str,
        workspace: str = Query("", description="Book workspace path"),
        guided: bool = Query(False, description="Use marathon graph state (guided / pause-after-chunk runs)"),
    ):
        if not (workspace or "").strip():
            raise HTTPException(422, "Missing required query parameter: workspace (directory path)")
        ws = Path(workspace).expanduser().resolve()
        if not ws.is_dir():
            raise HTTPException(400, "bad workspace")
        app_g = get_supervisor_app(ws, marathon=guided)
        snap = app_g.get_state({"configurable": {"thread_id": thread_id}})
        values = getattr(snap, "values", None) or {}
        payload = {
            "thread_id": thread_id,
            "values": dict(values) if isinstance(values, dict) else values,
            "next": list(getattr(snap, "next", []) or []),
        }
        return jsonable_encoder(payload)

    @app.post("/api/supervisor/guided/step")
    async def supervisor_guided_step_route(payload: RunBody):
        """One LangGraph step for the marathon (pause-after-chunk) supervisor — human-in-the-loop."""
        ws = Path(payload.workspace).expanduser().resolve()
        if not ws.is_dir():
            raise HTTPException(400, f"workspace not a directory: {ws}")
        tid = (payload.thread_id or "").strip() or str(uuid.uuid4())

        def one() -> dict:
            with _RUN_LOCK:
                goal_final = merge_goal(ws, payload)
                supervisor_guided_step(
                    ws,
                    tid,
                    user_goal=goal_final,
                    goal_preset=payload.goal_preset,
                    use_openclaw_after_plan=payload.use_openclaw_after_plan,
                    openclaw_tool=payload.openclaw_tool,
                    openclaw_args_json=payload.openclaw_args_json or "{}",
                    llm_provider=payload.llm_provider,
                    user_statements_json=payload.user_statements_json,
                    use_semantic_division=payload.use_semantic_division,
                    openclaw_per_chunk=payload.openclaw_per_chunk,
                    max_revision_rounds=payload.max_revision_rounds,
                )
                app_m = get_supervisor_app(ws, marathon=True)
                snap = app_m.get_state({"configurable": {"thread_id": tid}})
                pending = list(getattr(snap, "next", ()) or ())
                vals = dict(getattr(snap, "values", None) or {})
                st = "paused" if pending else "done"
                return {"thread_id": tid, "status": st, "next": [str(x) for x in pending], "values": vals}

        loop = asyncio.get_running_loop()
        out = await loop.run_in_executor(None, one)
        return jsonable_encoder(out)

    @app.post("/api/supervisor/merge")
    async def supervisor_merge(payload: MergeBody):
        ws = Path(payload.workspace).expanduser().resolve()
        staging = ws / "outputs" / "staging_merged.md"
        if not staging.is_file():
            raise HTTPException(400, "staging outputs/staging_merged.md missing — run supervisor first")
        dest_dir = ws / "manuscript"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "canonical_merged.md"
        shutil.copyfile(staging, dest)
        return {"ok": True, "path": str(dest.relative_to(ws))}

    def _ms_settings(workspace_field: str) -> tuple[Path, Settings]:
        raw = (workspace_field or "").strip()
        cfg_ws = Path(raw).expanduser().resolve() if raw else Path.cwd().resolve()
        cfg_path = cfg_ws / "config.yaml" if (cfg_ws / "config.yaml").is_file() else None
        return cfg_ws, load_settings(cfg_ws, cfg_path)

    @app.get("/api/workspace-llm")
    async def workspace_llm(workspace: str = Query(..., min_length=1)):
        """LLM provider defaults for a workspace (no secrets)."""
        ws = Path(workspace).expanduser().resolve()
        if not ws.is_dir():
            raise HTTPException(400, "bad workspace")
        _w, settings = _ms_settings(str(ws))
        return {
            "llm_provider": settings.llm_provider,
            "anthropic_model": settings.anthropic_model,
            "anthropic_thinking": settings.anthropic_thinking,
            "anthropic_api_key_configured": bool(settings.anthropic_api_key),
            "ollama_model": settings.ollama_model,
        }

    def _safe_memory_path(rel: str, settings: Settings) -> Path:
        p = Path((rel or "").strip().replace("\\", "/"))
        if p.is_absolute() or ".." in p.parts:
            raise HTTPException(400, "invalid memory path")
        root = settings.memory_root.resolve()
        full = (root / p).resolve()
        try:
            full.relative_to(root)
        except ValueError as e:
            raise HTTPException(400, "memory path escapes .memory root") from e
        return full

    @app.get("/api/studio/bootstrap")
    async def studio_bootstrap():
        conn = connect()
        uid = ensure_default_user(conn)
        row = get_user(conn, uid)
        conn.close()
        return {
            "user_id": uid,
            "display_name": (row["display_name"] if row else "local"),
            "studio_db": str(studio_db_path()),
            "projects_root": str(projects_root()),
        }

    @app.patch("/api/studio/me")
    async def studio_me_patch(payload: StudioMePatch, user_id: int = Query(1, ge=1)):
        conn = connect()
        ensure_default_user(conn)
        if get_user(conn, user_id) is None:
            conn.close()
            raise HTTPException(404, "user not found")
        update_user_display_name(conn, user_id, payload.display_name)
        row = get_user(conn, user_id)
        conn.close()
        return {"ok": True, "user": dict(row) if row else {}}

    @app.post("/api/studio/projects")
    async def studio_create_project(
        request: Request,
        user_id: int = Query(1, ge=1),
        name: str = Query(""),
    ):
        """Create a project. Prefer JSON body; if the body is empty (some proxies/clients drop it),
        ``user_id`` and ``name`` query parameters are accepted instead."""
        raw = await request.body()
        if raw.strip():
            try:
                payload = CreateProjectBody.model_validate_json(raw)
            except ValidationError as e:
                raise HTTPException(status_code=422, detail=jsonable_encoder(e.errors())) from None
        else:
            payload = CreateProjectBody(user_id=user_id, name=name)
        name = (payload.name or "").strip()
        if not name:
            raise HTTPException(400, "name required")
        conn = connect()
        uid = ensure_default_user(conn)
        target_user = int(payload.user_id) if payload.user_id else uid
        if get_user(conn, target_user) is None:
            conn.close()
            raise HTTPException(404, "user not found")
        pr = projects_root()
        slug, dest = allocate_slug_dir(pr, name)
        try:
            create_project_workspace(dest)
        except FileExistsError:
            conn.close()
            raise HTTPException(500, "project directory collision") from None
        pid = insert_project(conn, user_id=target_user, name=name, slug=slug, workspace_path=dest)
        conn.close()
        return {"ok": True, "project_id": pid, "slug": slug, "workspace": str(dest)}

    @app.get("/api/studio/projects")
    async def studio_projects_list(user_id: int = Query(1, ge=1)):
        conn = connect()
        ensure_default_user(conn)
        rows = list_projects(conn, user_id)
        conn.close()
        return {"projects": rows}

    @app.get("/api/studio/projects/{project_id}")
    async def studio_project_get(project_id: int):
        conn = connect()
        row = get_project(conn, project_id)
        conn.close()
        if not row:
            raise HTTPException(404, "project not found")
        return dict(row)

    @app.get("/api/studio/usage")
    async def studio_usage(workspace: str = Query(..., min_length=1)):
        ws = Path(workspace).expanduser().resolve()
        if not ws.is_dir():
            raise HTTPException(400, "bad workspace")
        oll = aggregate_ollama_usage(ws)
        ant = aggregate_anthropic_usage(ws)
        pt = int(oll["prompt_tokens"]) + int(ant["prompt_tokens"])
        ct = int(oll["completion_tokens"]) + int(ant["completion_tokens"])
        cost = estimate_cloud_cost_usd(pt, ct)
        return {
            "workspace": str(ws),
            "ollama": oll,
            "anthropic": ant,
            "combined_prompt_tokens": pt,
            "combined_completion_tokens": ct,
            "cost": cost,
        }

    @app.get("/api/studio/usage-estimate")
    async def studio_usage_estimate(
        workspace: str = Query(..., min_length=1),
        chunk_count: int = Query(6, ge=1, le=500),
        include_analyze: bool = Query(True),
    ):
        ws = Path(workspace).expanduser().resolve()
        if not ws.is_dir():
            raise HTTPException(400, "bad workspace")
        rough = rough_pass_estimate(ws, chunk_count=chunk_count, include_analyze=include_analyze)
        return {"workspace": str(ws), **rough}

    @app.get("/api/studio/memory-tree")
    async def studio_memory_tree(workspace: str = Query(..., min_length=1)):
        ws = Path(workspace).expanduser().resolve()
        if not ws.is_dir():
            raise HTTPException(400, "bad workspace")
        _ws2, settings = _ms_settings(str(ws))
        root = settings.memory_root
        if not root.is_dir():
            return {"memory_root": settings.memory_dir_name, "files": []}
        files: list[dict[str, int | str]] = []
        for p in sorted(root.rglob("*.md")):
            if p.is_file():
                rel = str(p.relative_to(root)).replace("\\", "/")
                files.append({"path": rel, "size": int(p.stat().st_size)})
        return {"memory_root": settings.memory_dir_name, "files": files}

    @app.get("/api/studio/memory-file")
    async def studio_memory_file_get(workspace: str = Query(..., min_length=1), path: str = Query(..., min_length=1)):
        ws = Path(workspace).expanduser().resolve()
        if not ws.is_dir():
            raise HTTPException(400, "bad workspace")
        _ws2, settings = _ms_settings(str(ws))
        full = _safe_memory_path(path, settings)
        if not full.is_file():
            raise HTTPException(404, "file not found")
        return {"path": path, "content": full.read_text(encoding="utf-8", errors="replace")}

    @app.put("/api/studio/memory-file")
    async def studio_memory_file_put(payload: MemoryWriteBody):
        ws = Path(payload.workspace).expanduser().resolve()
        if not ws.is_dir():
            raise HTTPException(400, "bad workspace")
        _ws2, settings = _ms_settings(str(ws))
        full = _safe_memory_path(payload.path, settings)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(payload.content, encoding="utf-8")
        return {"ok": True, "path": payload.path}

    @app.post("/api/manuscript/upload")
    async def manuscript_upload(
        workspace: str = Form(""),
        file: UploadFile = File(...),
    ):
        name = (file.filename or "upload").strip() or "upload.txt"
        data = await file.read()
        if not data:
            raise HTTPException(400, "empty file")
        if len(data) > 12 * 1024 * 1024:
            raise HTTPException(400, "file too large (max 12 MiB for this UI)")
        try:
            text = read_document_from_bytes(name, data)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        cfg_ws, _settings = _ms_settings(workspace)
        parts = chunk_manuscript(text, target_chars=6500)
        sid = str(uuid.uuid4())
        sess = {
            "id": sid,
            "filename": name,
            "workspace_field": str(cfg_ws),
            "text": text,
            "chunks": parts,
            "structure_markdown": None,
            "chunk_insights": None,
            "analyze_status": "idle",
            "analyze_error": None,
            "comments": [],
            "global_note": "",
            "analyze_thinking": "",
            "created": time.time(),
        }
        _ms_put(sess)
        light_chunks = [{"id": c["id"], "preview": c["preview"], "word_count": c["word_count"]} for c in parts]
        wc = len(text.split())
        return {
            "session_id": sid,
            "filename": name,
            "word_count": wc,
            "chunk_count": len(parts),
            "chunks": light_chunks,
        }

    @app.get("/api/manuscript/session/{session_id}")
    async def manuscript_session(session_id: str, workspace: str = Query("")):
        s = _ms_require(session_id, workspace)
        return jsonable_encoder(
            {
                "session_id": s["id"],
                "filename": s["filename"],
                "workspace_field": s["workspace_field"],
                "word_count": len(str(s.get("text") or "").split()),
                "chunk_count": len(s.get("chunks") or []),
                "chunks": s.get("chunks") or [],
                "structure_markdown": s.get("structure_markdown"),
                "chunk_insights": s.get("chunk_insights"),
                "analyze_status": s.get("analyze_status") or "idle",
                "analyze_error": s.get("analyze_error"),
                "comments": s.get("comments") or [],
                "global_note": s.get("global_note") or "",
                "analyze_thinking": s.get("analyze_thinking") or "",
            }
        )

    @app.get("/api/manuscript/recent")
    async def manuscript_recent(workspace: str = Query(..., min_length=1)):
        ws = Path(workspace).expanduser().resolve()
        if not ws.is_dir():
            raise HTTPException(400, "bad workspace")
        return {"sessions": list_session_meta(ws, limit=40)}

    @app.post("/api/manuscript/session/{session_id}/commit-to-workspace")
    async def manuscript_commit_to_workspace(session_id: str, payload: ManuscriptCommitBody):
        ws = Path(payload.workspace).expanduser().resolve()
        if not ws.is_dir():
            raise HTTPException(400, f"workspace not a directory: {ws}")
        s = _ms_require(session_id, str(ws))
        try:
            out = commit_manuscript_session(ws, s, mode=payload.mode)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        s["last_commit"] = {"ts": time.time(), "paths": out.get("paths"), "mode": out.get("mode")}
        _ms_put(s)
        return {"ok": True, **out}

    @app.post("/api/manuscript/session/{session_id}/analyze")
    async def manuscript_analyze(
        session_id: str,
        request: Request,
        workspace: str = Query("", description="Workspace for Ollama config (optional if JSON body or upload set it)"),
    ):
        json_workspace = ""
        llm_provider_body = ""
        ct = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ct == "application/json":
            raw_bytes = await request.body()
            if raw_bytes.strip():
                try:
                    raw = json.loads(raw_bytes.decode("utf-8"))
                    if isinstance(raw, dict):
                        json_workspace = str(raw.get("workspace") or "").strip()
                        llm_provider_body = str(raw.get("llm_provider") or "").strip()
                except json.JSONDecodeError:
                    raise HTTPException(400, "Invalid JSON body") from None

        ws_hint = json_workspace or (workspace or "").strip()
        _ms_require(session_id, ws_hint)
        with _MS_LOCK:
            cur = _MS_SESSIONS.get(session_id)
            if not cur:
                raise HTTPException(404, "unknown manuscript session")
            if cur.get("analyze_status") == "running":
                raise HTTPException(409, "analysis already running")
            cur["analyze_status"] = "running"
            cur["analyze_error"] = None
        text = str(cur.get("text") or "")
        _disk_persist(cur)

        chosen_ws = ws_hint or str(cur.get("workspace_field") or "")
        cfg_ws, settings = _ms_settings(chosen_ws)
        prov = llm_provider_body or None

        def work() -> None:
            try:
                structure, insights, think = analyze_manuscript_structure(
                    text,
                    settings=settings,
                    llm_provider=prov,
                )
                with _MS_LOCK:
                    s2 = _MS_SESSIONS.get(session_id)
                    if s2:
                        s2["structure_markdown"] = structure
                        s2["chunk_insights"] = insights
                        s2["analyze_thinking"] = think
                        s2["analyze_status"] = "done"
                        _disk_persist(s2)
            except Exception as e:  # noqa: BLE001
                with _MS_LOCK:
                    s2 = _MS_SESSIONS.get(session_id)
                    if s2:
                        s2["analyze_status"] = "error"
                        s2["analyze_error"] = str(e)
                        _disk_persist(s2)

        threading.Thread(target=work, daemon=True).start()
        return {"session_id": session_id, "status": "started"}

    @app.post("/api/manuscript/session/{session_id}/comments")
    async def manuscript_add_comment(
        session_id: str,
        payload: ManuscriptCommentBody,
        workspace: str = Query(""),
    ):
        note = (payload.body or "").strip()
        if not note:
            raise HTTPException(400, "comment body required")
        cid = str(uuid.uuid4())
        s = _ms_require(session_id, workspace)
        chunks = s.get("chunks") or []
        if payload.chunk_id >= len(chunks):
            raise HTTPException(400, "chunk_id out of range")
        rec = {"id": cid, "chunk_id": payload.chunk_id, "body": note, "ts": time.time()}
        s.setdefault("comments", []).append(rec)
        _ms_put(s)
        return {"ok": True, "comment": rec}

    @app.delete("/api/manuscript/session/{session_id}/comments/{comment_id}")
    async def manuscript_delete_comment(
        session_id: str,
        comment_id: str,
        workspace: str = Query(""),
    ):
        s = _ms_require(session_id, workspace)
        comments = s.setdefault("comments", [])
        before = len(comments)
        s["comments"] = [c for c in comments if str(c.get("id")) != comment_id]
        if len(s["comments"]) == before:
            raise HTTPException(404, "comment not found")
        _ms_put(s)
        return {"ok": True}

    @app.post("/api/manuscript/session/{session_id}/global-note")
    async def manuscript_global_note(
        session_id: str,
        payload: ManuscriptGlobalNoteBody,
        workspace: str = Query(""),
    ):
        wq = (payload.workspace or workspace or "").strip()
        s = _ms_require(session_id, wq)
        s["global_note"] = payload.text or ""
        _ms_put(s)
        return {"ok": True}

    @app.get("/api/manuscript/session/{session_id}/goal-block")
    async def manuscript_goal_block(session_id: str, workspace: str = Query("")):
        s = _ms_require(session_id, workspace)
        block = comments_to_goal_block(list(s.get("comments") or []))
        return {"text": block}

    @app.get("/api/projects-library/local")
    async def projects_library_local():
        """Scan ``projects_root()`` for workspaces with draft / staging / export files."""
        return scan_local_completed_projects()

    @app.get("/api/projects-library/artifact")
    async def projects_library_artifact(
        workspace: str = Query(..., min_length=1),
        kind: str = Query(..., min_length=1, description="draft | canonical | staging | converted"),
    ):
        ws = Path(workspace).expanduser().resolve()
        try:
            rel, text = read_artifact(ws, kind)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return {"path": rel, "content": text}

    @app.get("/api/projects-library/pipeline-stages")
    async def projects_library_pipeline_stages(workspace: str = Query(..., min_length=1)):
        """List ``outputs/**/*.md|txt|json`` (e.g. per-chunk ``staging_chunks/`` snapshots)."""
        ws = Path(workspace).expanduser().resolve()
        if not ws.is_dir():
            raise HTTPException(400, "bad workspace")
        return list_pipeline_output_files(ws)

    @app.get("/api/projects-library/output-file")
    async def projects_library_output_file(
        workspace: str = Query(..., min_length=1),
        path: str = Query(..., min_length=1, description="Relative path under workspace, must start with outputs/"),
    ):
        ws = Path(workspace).expanduser().resolve()
        try:
            rel, text = read_outputs_workspace_file(ws, path)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return {"path": rel, "content": text}

    @app.get("/api/manuscript-samples/proxy/catalog")
    async def manuscript_samples_proxy_catalog(request: Request):
        """Forward GET /v1/manuscript-samples (optional ``Authorization: Bearer`` from client)."""
        base = _manuscript_samples_api_base()
        if not base:
            raise HTTPException(
                503,
                "MANUSCRIPT_SAMPLES_API_BASE is not set (HTTP API origin only, no trailing slash)",
            )
        auth = (request.headers.get("authorization") or request.headers.get("Authorization") or "").strip()
        headers: dict[str, str] = {}
        if auth:
            headers["Authorization"] = auth
        url = f"{base}/v1/manuscript-samples"
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.get(url, headers=headers)
        ct = r.headers.get("content-type") or "application/json"
        return Response(content=r.content, status_code=r.status_code, media_type=ct)

    @app.get("/api/manuscript-samples/proxy/project/{project_id}")
    async def manuscript_samples_proxy_project(project_id: str, request: Request):
        """Forward GET /v1/manuscript-samples/{projectId} (fresh presigned URLs)."""
        base = _manuscript_samples_api_base()
        if not base:
            raise HTTPException(503, "MANUSCRIPT_SAMPLES_API_BASE is not set")
        pid = (project_id or "").strip()
        if not pid or ".." in pid:
            raise HTTPException(400, "invalid project_id")
        auth = (request.headers.get("authorization") or request.headers.get("Authorization") or "").strip()
        headers: dict[str, str] = {}
        if auth:
            headers["Authorization"] = auth
        url = f"{base}/v1/manuscript-samples/{quote(pid, safe='')}"
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.get(url, headers=headers)
        ct = r.headers.get("content-type") or "application/json"
        return Response(content=r.content, status_code=r.status_code, media_type=ct)

    return app

app = create_app()
