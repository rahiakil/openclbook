#!/usr/bin/env python3
"""
Poll a remote job API (HTTPS), run work on **local Ollama**, report progress, complete/fail.

Outbound-only: this machine calls API Gateway; you do not expose Ollama to the internet.

Environment
-----------

**Required**

- ``REMOTE_JOBS_BASE_URL`` — e.g. ``https://xxxx.execute-api.region.amazonaws.com/prod``

**Auth**

- **Fabletome / book-pipeline worker**: set ``REMOTE_JOBS_WORKER_TOKEN`` to the same value as Terraform
  ``book_pipeline_worker_token``; it is sent as header ``X-Book-Pipeline-Worker`` on claim/progress/complete/fail.
- **Other APIs** (optional): ``REMOTE_JOBS_TOKEN`` (``Authorization: Bearer``) or ``REMOTE_JOBS_API_KEY`` (``x-api-key``).

**Optional**

- ``REMOTE_JOBS_WORKER_ID`` — default hostname
- ``REMOTE_JOBS_POLL_SEC`` — default ``15``
- ``BOOK_PIPELINE_ROOT`` — default: parent of ``scripts/``

**Endpoint paths** (defaults; override if your API uses different routes)

- ``REMOTE_JOBS_CLAIM_PATH`` default ``/v1/jobs/claim``
- ``REMOTE_JOBS_PROGRESS_PATH`` default ``/v1/jobs/{id}/progress`` (``PATCH``)
- ``REMOTE_JOBS_COMPLETE_PATH`` default ``/v1/jobs/{id}/complete`` (``POST``)
- ``REMOTE_JOBS_FAIL_PATH`` default ``/v1/jobs/{id}/fail`` (``POST``)

Claim response JSON::

  { "job": null }
  { "job": { "id": "uuid", "type": "gutendex_ingest", "payload": { ... } } }

Job types
---------

**gutendex_ingest** — runs ``scripts/gutendex_process_batch.py`` for specific ids::

  payload: {
    "gutenberg_ids": [11, 43],
    "manifest": "gutenberg_library/manifest.json",
    "settings_workspace": "workspace",
    "sleep_between": 5.0,
    "brainstorm_ctx": 8192,
    "max_input_chars": 0,
    "excerpt_chars": 14000
  }

**upload_manuscripts** — runs ``scripts/upload_completed_manuscripts_s3.py``::

  payload: { "project_ids": ["gut-11-Alice-..."] }

**shell** — escape hatch::

  payload: { "argv": ["python", "-c", "print(1)"] }  # first element can be ``python`` or absolute

**manuscript_ingest** — user-uploaded source via Sceneweaver / ``POST /v1/jobs``::

  payload: {
    "filename": "novel.docx",
    "document_base64": "<optional base64>",
    "document_text": "<optional utf-8 if no file>",
    "user_goal": "instructions for the supervisor",
    "goal_preset": "rewrite|netflix_script|…",
    "output_format": "md|txt|docx",
    "project_id": "<optional slug under projects/>",
    "settings_workspace": "workspace",
    "adaptation_spec": {
      "pipeline": "faithful|twist",
      "seasons": 1,
      "episodesPerSeason": 8,
      "twistAxis": "time_period|character|mood|length|extra_season|prelude",
      "notes": "optional freeform"
    }
  }

  Runs ``init-project`` then ``ingest-run --project-id … --input .pipeline/remote_uploads/<filename>``.

Outstanding completions
-----------------------

If the network drops after local work finishes, results are queued under
``.pipeline/remote_worker_outstanding.jsonl`` and retried before the next claim.

Fabletome stack: after ``terraform apply`` with ``book_pipeline_worker_token`` set, use
``REMOTE_JOBS_BASE_URL`` = ``api_base_url`` output (no path suffix). Create jobs with
``POST /v1/jobs`` and ``runMode: book_pipeline`` (see Fabletome Lambda ``http.mjs``).
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    if v is None or not str(v).strip():
        return default
    return str(v).strip()


def _headers() -> dict[str, str]:
    h: dict[str, str] = {"Content-Type": "application/json", "Accept": "application/json"}
    wpt = _env("REMOTE_JOBS_WORKER_TOKEN") or _env("BOOK_PIPELINE_WORKER_TOKEN")
    if wpt:
        h["X-Book-Pipeline-Worker"] = wpt
    tok = _env("REMOTE_JOBS_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    key = _env("REMOTE_JOBS_API_KEY")
    if key:
        h["x-api-key"] = key
    wid = _env("REMOTE_JOBS_WORKER_ID") or socket.gethostname()
    h["X-Worker-Id"] = wid
    return h


def _url(path: str) -> str:
    base = (_env("REMOTE_JOBS_BASE_URL") or "").rstrip("/")
    if not base:
        raise SystemExit("REMOTE_JOBS_BASE_URL is required")
    p = path if path.startswith("/") else f"/{path}"
    return f"{base}{p}"


def outstanding_path() -> Path:
    d = ROOT / ".pipeline"
    d.mkdir(parents=True, exist_ok=True)
    return d / "remote_worker_outstanding.jsonl"


def replay_outstanding(client: httpx.Client) -> None:
    path = outstanding_path()
    if not path.is_file():
        return
    raw = path.read_text(encoding="utf-8")
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return
    kept: list[str] = []
    for ln in lines:
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        action = rec.get("action")
        job_id = rec.get("job_id")
        if not job_id or action not in ("complete", "fail"):
            continue
        try:
            if action == "complete":
                complete_job(client, str(job_id), rec.get("body") or {})
            else:
                fail_job(client, str(job_id), str(rec.get("error") or "unknown error"))
        except Exception:
            kept.append(ln)
    path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")


def queue_outstanding(action: str, job_id: str, **kwargs: Any) -> None:
    rec = {"action": action, "job_id": job_id, **kwargs}
    with open(outstanding_path(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def claim_job(client: httpx.Client) -> dict[str, Any] | None:
    claim_path = _env("REMOTE_JOBS_CLAIM_PATH", "/v1/jobs/claim")
    body = {
        "worker_id": _env("REMOTE_JOBS_WORKER_ID") or socket.gethostname(),
        "capabilities": ["gutendex_ingest", "upload_manuscripts", "shell", "manuscript_ingest"],
    }
    r = client.post(_url(claim_path), headers=_headers(), json=body, timeout=60.0)
    r.raise_for_status()
    data = r.json()
    job = data.get("job")
    if not job:
        return None
    if not isinstance(job, dict) or not job.get("id"):
        return None
    return job


def progress_job(client: httpx.Client, job_id: str, progress: float, message: str = "") -> None:
    tmpl = _env("REMOTE_JOBS_PROGRESS_PATH", "/v1/jobs/{id}/progress")
    path = tmpl.replace("{id}", job_id)
    body = {"progress": max(0.0, min(1.0, progress)), "message": message}
    r = client.patch(_url(path), headers=_headers(), json=body, timeout=60.0)
    r.raise_for_status()


def complete_job(client: httpx.Client, job_id: str, body: dict[str, Any]) -> None:
    tmpl = _env("REMOTE_JOBS_COMPLETE_PATH", "/v1/jobs/{id}/complete")
    path = tmpl.replace("{id}", job_id)
    r = client.post(_url(path), headers=_headers(), json=body, timeout=120.0)
    r.raise_for_status()


def fail_job(client: httpx.Client, job_id: str, error: str) -> None:
    tmpl = _env("REMOTE_JOBS_FAIL_PATH", "/v1/jobs/{id}/fail")
    path = tmpl.replace("{id}", job_id)
    r = client.post(_url(path), headers=_headers(), json={"error": error}, timeout=60.0)
    r.raise_for_status()


def _expand_adaptation_spec(spec: dict[str, Any]) -> str:
    """Mirror Sceneweaver ``buildUserGoalFromAdaptationSpec`` (keep in sync)."""
    pipeline = str(spec.get("pipeline") or "faithful").strip().lower()
    seasons = int(spec.get("seasons") or 1)
    eps = int(spec.get("episodesPerSeason") or 8)
    twist = str(spec.get("twistAxis") or "").strip().lower()
    notes = str(spec.get("notes") or "").strip()
    axis_hint: dict[str, str] = {
        "time_period": "Re-stage in a different era or near-future; keep core plot beats recognizable.",
        "character": "Rebalance POV / supporting cast; merge or reinterpret roles where it strengthens theme.",
        "mood": "Shift tone (gothic, comedy, thriller) without breaking the spine of the plot.",
        "length": "Emphasize tighter or more expansive episode rhythm; call out montage vs dialogue.",
        "extra_season": "Reserve a B-plot spine for an additional season beyond the novel's natural close.",
        "prelude": "Open with a framing cold open that pays off late; keep canon events intact afterward.",
    }
    if pipeline == "faithful":
        base = (
            "Faithful screenplay conversion: preserve plot, character arcs, and the author's intent. "
            f"Target structure: {seasons} season(s), {eps} episodes per season. "
            "Use industry-standard screenplay formatting and streaming act breaks. "
            "Do not introduce deliberate high-concept twists unless the source already implies them."
        )
    else:
        hint = axis_hint.get(twist, "Follow the user's twist brief.")
        base = (
            "Classical adaptation with a deliberate creative twist (still structurally coherent). "
            f"Twist axis ({twist or 'custom'}): {hint} "
            f"Target structure: {seasons} season(s), {eps} episodes per season. "
            "Preserve recognizable story DNA while executing the twist boldly."
        )
    return f"{base}\n\nAdditional direction:\n{notes}" if notes else base


def _merge_user_goal_with_adaptation(payload: dict[str, Any]) -> str:
    base = str(payload.get("user_goal") or "").strip()
    spec = payload.get("adaptation_spec")
    if not isinstance(spec, dict) or not spec:
        return base
    block = _expand_adaptation_spec(spec)
    if not base:
        return block
    return f"{base}\n\n--- adaptation_spec ---\n{block}"


def _build_adaptation_tree(spec: dict[str, Any]) -> dict[str, Any]:
    seasons_n = max(1, min(6, int(spec.get("seasons") or 1)))
    eps_n = max(1, min(24, int(spec.get("episodesPerSeason") or 8)))
    seasons: list[dict[str, Any]] = []
    for si in range(1, seasons_n + 1):
        episodes = [
            {
                "title": f"Episode {ei}",
                "screenplay": "(Draft lives in project workspace after ingest-run completes.)",
            }
            for ei in range(1, eps_n + 1)
        ]
        seasons.append({"title": f"Season {si}", "episodes": episodes})
    return {
        "source": "Original manuscript / uploaded file in project .pipeline",
        "seasons": seasons,
    }


def _fetch_job_messages(client: httpx.Client, job_id: str) -> list[Any]:
    """Optional: set ``REMOTE_JOBS_READ_MESSAGES_TOKEN`` (Bearer) or reuse ``REMOTE_JOBS_TOKEN``."""
    read_tok = _env("REMOTE_JOBS_READ_MESSAGES_TOKEN") or _env("REMOTE_JOBS_TOKEN")
    if not read_tok:
        return []
    path = f"/v1/jobs/{job_id}/messages?limit=40"
    try:
        h = {"Content-Type": "application/json", "Accept": "application/json", "Authorization": f"Bearer {read_tok}"}
        r = client.get(_url(path), headers=h, timeout=30.0)
        if r.status_code >= 400:
            return []
        data = r.json()
        msgs = data.get("messages") if isinstance(data, dict) else None
        return msgs if isinstance(msgs, list) else []
    except Exception:
        return []


def _messages_request_pause(messages: list[Any]) -> bool:
    for m in messages:
        if not isinstance(m, dict):
            continue
        c = str(m.get("content") or "")
        if "CONTROL:PAUSE" in c or "PAUSE_REQUEST:" in c:
            return True
    return False


def _pre_ingest_pause_requested(client: httpx.Client, job_id: str) -> bool:
    return _messages_request_pause(_fetch_job_messages(client, job_id))


def _run_subprocess(job_id: str, argv: list[str], cwd: Path, on_line: Any) -> int:
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        on_line(line.rstrip("\n"))
    return proc.wait()


def run_gutendex_ingest(client: httpx.Client, job_id: str, payload: dict[str, Any]) -> tuple[bool, str]:
    ids = payload.get("gutenberg_ids") or []
    if not isinstance(ids, list) or not ids:
        return False, "payload.gutenberg_ids required (non-empty list)"
    ids_int = []
    for x in ids:
        try:
            ids_int.append(int(x))
        except (TypeError, ValueError):
            return False, f"invalid gutenberg id: {x!r}"
    manifest = Path(payload.get("manifest") or "gutenberg_library/manifest.json")
    if not manifest.is_absolute():
        manifest = (ROOT / manifest).resolve()
    settings_ws = Path(payload.get("settings_workspace") or "workspace")
    if not settings_ws.is_absolute():
        settings_ws = (ROOT / settings_ws).resolve()
    sleep_between = float(payload.get("sleep_between") or 5.0)
    brainstorm_ctx = int(payload.get("brainstorm_ctx") or 8192)
    max_input_chars = int(payload.get("max_input_chars") or 0)
    excerpt_chars = int(payload.get("excerpt_chars") or 14_000)
    ids_csv = ",".join(str(i) for i in sorted(set(ids_int)))
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "gutendex_process_batch.py"),
        "--manifest",
        str(manifest),
        "--settings-workspace",
        str(settings_ws),
        "--only-ids",
        ids_csv,
        "--sleep-between",
        str(sleep_between),
        "--brainstorm-ctx",
        str(brainstorm_ctx),
        "--excerpt-chars",
        str(excerpt_chars),
    ]
    if max_input_chars > 0:
        cmd.extend(["--max-input-chars", str(max_input_chars)])

    lines: list[str] = []

    def on_line(ln: str) -> None:
        lines.append(ln)
        print(ln, flush=True)
        if len(lines) % 25 == 0:
            try:
                progress_job(client, job_id, 0.1 + 0.85 * min(1.0, len(lines) / 2000.0), ln[:500])
            except Exception:
                pass

    progress_job(client, job_id, 0.05, "starting gutendex_process_batch")
    code = _run_subprocess(job_id, cmd, ROOT, on_line)
    tail = "\n".join(lines[-40:])
    return code == 0, tail or f"exit {code}"


def run_upload_manuscripts(client: httpx.Client, job_id: str, payload: dict[str, Any]) -> tuple[bool, str]:
    pids = payload.get("project_ids") or []
    if not isinstance(pids, list) or not pids:
        return False, "payload.project_ids required"
    cmd = [sys.executable, str(ROOT / "scripts" / "upload_completed_manuscripts_s3.py"), "--only", *[str(x) for x in pids]]
    lines: list[str] = []

    def on_line(ln: str) -> None:
        lines.append(ln)
        print(ln, flush=True)

    progress_job(client, job_id, 0.1, "upload_completed_manuscripts_s3")
    code = _run_subprocess(job_id, cmd, ROOT, on_line)
    tail = "\n".join(lines[-40:])
    return code == 0, tail or f"exit {code}"


def run_manuscript_ingest(client: httpx.Client, job_id: str, payload: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    """Create ``projects/<id>/``, materialize upload, run ``ingest-run`` with Ollama (local).

    Returns ``(ok, log_tail, result_extras)`` where ``result_extras`` is merged into the job completion body.
    """
    from book_pipeline.project_workspace import sanitize_project_id

    filename = (payload.get("filename") or "").strip() or "source.md"
    filename = Path(filename).name
    if not filename or ".." in filename:
        return False, "invalid filename", {}

    suf = Path(filename).suffix.lower()
    allowed = (".md", ".txt", ".docx", ".html", ".odt", ".rtf", ".doc")
    if suf not in allowed:
        return False, f"unsupported file extension {suf!r} (allowed: {allowed})", {}

    goal = _merge_user_goal_with_adaptation(payload)
    if not goal:
        return False, "user_goal is required (or adaptation_spec must expand to non-empty text)", {}

    if _pre_ingest_pause_requested(client, job_id):
        return False, "paused_before_start: CONTROL:PAUSE or PAUSE_REQUEST in job messages", {}

    preset = (payload.get("goal_preset") or "rewrite").strip()
    out_fmt = (payload.get("output_format") or "md").strip().lower()
    if out_fmt not in ("md", "txt", "docx"):
        out_fmt = "md"

    raw_pid = (payload.get("project_id") or "").strip() or f"sw-{job_id}"
    try:
        pid = sanitize_project_id(raw_pid)
    except ValueError:
        try:
            pid = sanitize_project_id(f"sw-{job_id}")
        except ValueError:
            pid = sanitize_project_id("sw-submit")

    b64 = payload.get("document_base64")
    text = payload.get("document_text")

    def log_line(ln: str) -> None:
        print(ln, flush=True)

    progress_job(client, job_id, 0.02, f"init-project {pid}")
    code_init = _run_subprocess(
        job_id,
        [sys.executable, "-m", "book_pipeline", "init-project", pid],
        ROOT,
        log_line,
    )
    if code_init != 0:
        return False, f"init-project exit {code_init}", {}

    from book_pipeline.project_workspace import project_workspace_path

    ws = project_workspace_path(pid)
    up = ws / ".pipeline" / "remote_uploads"
    up.mkdir(parents=True, exist_ok=True)
    dest = (up / filename).resolve()
    try:
        dest.relative_to(ws.resolve())
    except ValueError:
        return False, "invalid upload path", {}

    if b64 is not None and str(b64).strip():
        try:
            raw = base64.b64decode(str(b64).strip(), validate=False)
        except (ValueError, binascii.Error) as e:
            return False, f"invalid document_base64: {e}", {}
        if len(raw) > 5 * 1024 * 1024:
            return False, "document too large (max 5 MiB decoded)", {}
        dest.write_bytes(raw)
    elif text is not None and str(text).strip():
        dest.write_text(str(text), encoding="utf-8", errors="replace")
    else:
        return False, "document_base64 or document_text is required", {}

    cmd = [
        sys.executable,
        "-m",
        "book_pipeline",
        "ingest-run",
        "--project-id",
        pid,
        "--input",
        str(dest),
        "--goal",
        goal,
        "--preset",
        preset,
        "--output-format",
        out_fmt,
        "--stream",
    ]
    lines: list[str] = []

    def on_line(ln: str) -> None:
        lines.append(ln)
        print(ln, flush=True)
        if len(lines) % 35 == 0:
            try:
                progress_job(
                    client,
                    job_id,
                    0.08 + 0.88 * min(1.0, len(lines) / 8000.0),
                    ln[:480],
                )
            except Exception:
                pass

    progress_job(client, job_id, 0.06, "ingest-run starting")
    code = _run_subprocess(job_id, cmd, ROOT, on_line)
    tail = "\n".join(lines[-80:])
    spec_raw = payload.get("adaptation_spec")
    extras: dict[str, Any] = {}
    if isinstance(spec_raw, dict) and spec_raw:
        extras["adaptation_tree"] = _build_adaptation_tree(spec_raw)
        extras["adaptation_spec"] = spec_raw
    return code == 0, tail or f"exit {code}", extras


def run_shell(client: httpx.Client, job_id: str, payload: dict[str, Any]) -> tuple[bool, str]:
    argv = payload.get("argv")
    if not isinstance(argv, list) or not argv:
        return False, "payload.argv must be a non-empty list"
    parts = [str(x) for x in argv]
    if parts[0] == "python":
        parts[0] = sys.executable
    lines: list[str] = []

    def on_line(ln: str) -> None:
        lines.append(ln)
        print(ln, flush=True)

    progress_job(client, job_id, 0.1, "shell " + " ".join(parts[:6]))
    code = _run_subprocess(job_id, parts, ROOT, on_line)
    tail = "\n".join(lines[-80:])
    return code == 0, tail or f"exit {code}"


def handle_job(client: httpx.Client, job: dict[str, Any]) -> None:
    job_id = str(job["id"])
    typ = str(job.get("type") or "").strip()
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}

    ok = False
    detail = ""
    extra: dict[str, Any] = {}
    try:
        if typ == "gutendex_ingest":
            ok, detail = run_gutendex_ingest(client, job_id, payload)
        elif typ == "upload_manuscripts":
            ok, detail = run_upload_manuscripts(client, job_id, payload)
        elif typ == "shell":
            ok, detail = run_shell(client, job_id, payload)
        elif typ == "manuscript_ingest":
            ok, detail, extra = run_manuscript_ingest(client, job_id, payload)
        else:
            ok, detail, extra = False, f"unsupported job type: {typ!r}", {}
    except Exception as e:  # noqa: BLE001
        ok, detail, extra = False, f"{type(e).__name__}: {e}", {}

    body: dict[str, Any] = {
        "ok": ok,
        "type": typ,
        "worker_id": _env("REMOTE_JOBS_WORKER_ID") or socket.gethostname(),
        "log_tail": detail[-8000:] if detail else "",
    }
    if isinstance(extra, dict) and extra:
        # Prefer nesting so ``GET /v1/jobs/{id}`` can expose ``result.adaptation_tree`` for Studio.
        body["result"] = dict(extra)
    try:
        if ok:
            progress_job(client, job_id, 1.0, "complete")
            complete_job(client, job_id, body)
        else:
            fail_job(client, job_id, detail[:8000])
    except Exception as e:  # noqa: BLE001
        if ok:
            queue_outstanding("complete", job_id, body=body)
        else:
            queue_outstanding("fail", job_id, error=detail[:8000])
        print(f"could not reach API to finalize job {job_id}: {e}", file=sys.stderr)


def main() -> int:
    poll = float(_env("REMOTE_JOBS_POLL_SEC", "15") or "15")
    print(f"remote_job_worker root={ROOT} poll={poll}s", flush=True)
    with httpx.Client(follow_redirects=True) as client:
        while True:
            try:
                replay_outstanding(client)
            except Exception as e:  # noqa: BLE001
                print(f"replay_outstanding: {e}", file=sys.stderr)
            try:
                job = claim_job(client)
            except Exception as e:  # noqa: BLE001
                print(f"claim failed: {e}", file=sys.stderr)
                time.sleep(poll)
                continue
            if not job:
                time.sleep(poll)
                continue
            print(f"claimed job {job.get('id')} type={job.get('type')!r}", flush=True)
            handle_job(client, job)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
