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

Outstanding completions
-----------------------

If the network drops after local work finishes, results are queued under
``.pipeline/remote_worker_outstanding.jsonl`` and retried before the next claim.

Fabletome stack: after ``terraform apply`` with ``book_pipeline_worker_token`` set, use
``REMOTE_JOBS_BASE_URL`` = ``api_base_url`` output (no path suffix). Create jobs with
``POST /v1/jobs`` and ``runMode: book_pipeline`` (see Fabletome Lambda ``http.mjs``).
"""
from __future__ import annotations

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
        "capabilities": ["gutendex_ingest", "upload_manuscripts", "shell"],
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
    try:
        if typ == "gutendex_ingest":
            ok, detail = run_gutendex_ingest(client, job_id, payload)
        elif typ == "upload_manuscripts":
            ok, detail = run_upload_manuscripts(client, job_id, payload)
        elif typ == "shell":
            ok, detail = run_shell(client, job_id, payload)
        else:
            ok, detail = False, f"unsupported job type: {typ!r}"
    except Exception as e:  # noqa: BLE001
        ok, detail = False, f"{type(e).__name__}: {e}"

    body = {
        "ok": ok,
        "type": typ,
        "worker_id": _env("REMOTE_JOBS_WORKER_ID") or socket.gethostname(),
        "log_tail": detail[-8000:] if detail else "",
    }
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
