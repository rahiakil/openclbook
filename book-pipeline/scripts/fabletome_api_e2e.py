#!/usr/bin/env python3
"""
End-to-end checks against the Fabletome HTTP API (API Gateway + Lambda).

Uses ``small.txt`` (or any file) as ``document_text`` for a ``manuscript_ingest`` job so a local
``remote_job_worker.py`` can claim it and create ``projects/<project_id>/``.

Environment (see also ``--help``)
---------------------------------

**Required**

- ``FABLETOME_API_BASE`` — same as ``REMOTE_JOBS_BASE_URL``: origin only, e.g.
  ``https://xxxxx.execute-api.us-east-1.amazonaws.com`` (no trailing slash, no ``/prod`` path
  unless your API really mounts there).

**Who is allowed to POST jobs (pick what your Lambda implements)**

1. **API key** (API Gateway usage plan / key): set ``FABLETOME_API_KEY`` → sent as ``x-api-key``.
   *pic-new-res default Terraform* (HTTP API → Lambda, Supabase URL empty) **does not check**
   this header in Lambda—leave unset for local smoke tests unless you added your own authorizer.
2. **Bearer JWT** (e.g. Supabase): set ``FABLETOME_BEARER_TOKEN`` → ``Authorization: Bearer …``.
   The token ``sub`` must match the user your authorizer indexes jobs under.
3. **Dev body userId** (no Bearer): set ``FABLETOME_USER_ID`` to a stable dummy string
   (e.g. ``local-e2e-user``). The script adds ``userId`` to ``POST /v1/jobs`` and
   ``GET /v1/jobs?userId=…`` exactly like Sceneweaver with ``VITE_JOBS_DEV_USER_ID``.

You can combine **API key + userId** or **API key + Bearer** depending on Terraform.

**SceneWeaver vs Fabletome (same API Gateway)**

The *pic-new-res* stack does not provision a second HTTP API for SceneWeaver. Terraform adds
SceneWeaver only as allowed **browser origins** (``cors_allow_origins``) on the same API Gateway
and on the **artifacts S3** CORS rule so browsers at ``https://sceneweaver.one`` (etc.) may call
the API and use presigned URLs. All routes (``/v1/jobs``, ``/v1/manuscript-samples``, …) are
shared; DynamoDB table names follow ``project_name`` (default ``fabletome-api-*``). Copy
``.env.example`` in this folder for demo env var names.

**Optional — worker routes (same token as Terraform ``book_pipeline_worker_token``)**

- ``FABLETOME_WORKER_TOKEN`` or ``REMOTE_JOBS_WORKER_TOKEN`` / ``BOOK_PIPELINE_WORKER_TOKEN``
  → ``X-Book-Pipeline-Worker`` on ``POST /v1/jobs/claim`` (smoke test only; does not run ingest).

**Optional — DynamoDB (direct read; needs AWS creds + table name)**

- ``FABLETOME_DYNAMODB_TABLE`` — jobs table name.
- ``AWS_REGION`` or ``AWS_DEFAULT_REGION``.
- Install: ``uv pip install -e ".[aws]"`` (boto3).

**Optional — local project check after job succeeds**

- ``BOOK_PIPELINE_ROOT`` — repo root containing ``projects/`` (defaults to parent of ``scripts/``).

Where values come from (important)
------------------------------------

- **Cached ``aws configure`` / SSO credentials** are **IAM identity** used to call **AWS APIs**
  (SSM, Secrets Manager, DynamoDB). They are **not** the string you send as ``x-api-key`` to
  API Gateway unless your API is explicitly IAM-authenticated (unusual for this SPA stack).
- **``FABLETOME_API_KEY``** is an **API Gateway usage-plan API key** value. You get it from the
  AWS console (API Gateway → API keys) or from Terraform output / SSM where your infra stores it.
- **``FABLETOME_USER_ID``** is either (a) any stable string your Lambda accepts in the JSON body
  when there is no JWT, or (b) the **``sub``** claim inside ``FABLETOME_BEARER_TOKEN`` when you
  use Supabase (or similar) JWT auth.
- **``FABLETOME_WORKER_TOKEN``** is the **shared secret** Terraform sets as ``book_pipeline_worker_token``
  (same value as ``REMOTE_JOBS_WORKER_TOKEN`` on ``remote_job_worker.py``). It is **not** an IAM key;
  it is a long random string in tfvars, SSM, or Secrets Manager.

**Pull API key / worker token / user id from AWS using your cached profile**

If boto3 is installed (``uv pip install -e ".[aws]"`` or ``pip install boto3``), set **only** the
parameter/secret **names** — the script fills the real ``FABLETOME_*`` env vars before calling HTTP:

- ``FABLETOME_API_KEY_SSM`` — SSM parameter name (e.g. ``/fabletome/prod/gateway-api-key``).
- ``FABLETOME_WORKER_TOKEN_SSM`` — SSM parameter name for the worker HMAC/header secret.
- ``FABLETOME_USER_ID_SSM`` — optional SSM string for dummy ``userId`` when not using Bearer.

Or one JSON secret in **Secrets Manager**:

- ``FABLETOME_SECRETS_JSON_SECRET_ID`` — secret id whose ``SecretString`` is JSON, e.g.
  ``{"FABLETOME_API_KEY":"...","FABLETOME_WORKER_TOKEN":"...","FABLETOME_USER_ID":"..."}``
  (extra keys ignored). Loaded only for keys that are not already set in the environment.

Use ``AWS_REGION`` / ``AWS_DEFAULT_REGION`` (default ``us-east-1``). Your IAM user/role needs
``ssm:GetParameter`` (and optionally ``secretsmanager:GetSecretValue``, ``dynamodb:GetItem``).

Exit code **0** if POST job + GET job detail succeed and polling sees a terminal status (or
``--no-wait``). Non-zero on hard failures (4xx on POST job, missing jobId, etc.).
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx

ROOT = Path(__file__).resolve().parents[1]

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
except Exception:
    pass


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    if v is None or not str(v).strip():
        return default
    return str(v).strip()


def _aws_region() -> str:
    return _env("AWS_REGION") or _env("AWS_DEFAULT_REGION") or "us-east-1"


def _ssm_get_parameter(name: str, region: str) -> str | None:
    try:
        import boto3  # type: ignore[import-not-found]
    except ImportError:
        print("boto3 not installed; cannot read SSM. pip install boto3 or uv pip install -e '.[aws]'", file=sys.stderr)
        return None
    try:
        client = boto3.client("ssm", region_name=region)
        resp = client.get_parameter(Name=name, WithDecryption=True)
        p = resp.get("Parameter") or {}
        v = str(p.get("Value") or "").strip()
        return v or None
    except Exception as e:
        print(f"SSM GetParameter {name!r} failed: {e}", file=sys.stderr)
        return None


def _secrets_get_string(secret_id: str, region: str) -> str | None:
    try:
        import boto3  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        client = boto3.client("secretsmanager", region_name=region)
        resp = client.get_secret_value(SecretId=secret_id)
        s = resp.get("SecretString")
        if s is None and resp.get("SecretBinary"):
            print(f"Secret {secret_id!r} is binary-only; use SecretString JSON.", file=sys.stderr)
            return None
        return str(s or "").strip() or None
    except Exception as e:
        print(f"Secrets Manager GetSecretValue {secret_id!r} failed: {e}", file=sys.stderr)
        return None


def _hydrate_credentials_from_aws() -> None:
    """Fill ``FABLETOME_*`` / worker env from SSM or Secrets Manager using default AWS credential chain."""
    region = _aws_region()
    # SSM individual parameters (do not override explicit env)
    pairs = [
        ("FABLETOME_API_KEY", "FABLETOME_API_KEY_SSM"),
        ("FABLETOME_WORKER_TOKEN", "FABLETOME_WORKER_TOKEN_SSM"),
        ("FABLETOME_USER_ID", "FABLETOME_USER_ID_SSM"),
        ("FABLETOME_BEARER_TOKEN", "FABLETOME_BEARER_TOKEN_SSM"),
    ]
    for target, src in pairs:
        if _env(target):
            continue
        pname = _env(src)
        if not pname:
            continue
        val = _ssm_get_parameter(pname, region)
        if val:
            os.environ[target] = val
            print(f"(from SSM {pname} → set {target})", file=sys.stderr)

    # One JSON bundle in Secrets Manager
    sid = _env("FABLETOME_SECRETS_JSON_SECRET_ID") or _env("FABLETOME_CONFIG_SECRET_ID")
    if not sid:
        return
    raw = _secrets_get_string(sid, region)
    if not raw:
        return
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        print(f"Secret {sid!r} is not JSON; put a JSON object or use *_SSM parameters instead.", file=sys.stderr)
        return
    if not isinstance(obj, dict):
        return
    bundles: list[tuple[str, list[str]]] = [
        ("FABLETOME_API_KEY", ["FABLETOME_API_KEY", "apiKey", "gateway_api_key"]),
        ("FABLETOME_WORKER_TOKEN", ["FABLETOME_WORKER_TOKEN", "worker_token", "book_pipeline_worker_token"]),
        ("FABLETOME_USER_ID", ["FABLETOME_USER_ID", "user_id", "userId"]),
        ("FABLETOME_BEARER_TOKEN", ["FABLETOME_BEARER_TOKEN", "bearer_token", "jwt"]),
    ]
    for env_key, json_keys in bundles:
        if _env(env_key):
            continue
        for jk in json_keys:
            if jk in obj and str(obj[jk]).strip():
                os.environ[env_key] = str(obj[jk]).strip()
                print(f"(from secret {sid} key {jk!r} → set {env_key})", file=sys.stderr)
                break


def _base() -> str:
    b = _env("FABLETOME_API_BASE") or _env("REMOTE_JOBS_BASE_URL")
    if not b:
        print("Set FABLETOME_API_BASE (or REMOTE_JOBS_BASE_URL) to the API Gateway origin.", file=sys.stderr)
        sys.exit(2)
    return b.rstrip("/")


def _user_headers() -> dict[str, str]:
    h: dict[str, str] = {"Content-Type": "application/json", "Accept": "application/json"}
    key = _env("FABLETOME_API_KEY")
    if key:
        h["x-api-key"] = key
    bearer = _env("FABLETOME_BEARER_TOKEN")
    if bearer:
        h["Authorization"] = f"Bearer {bearer}"
    return h


def _worker_headers() -> dict[str, str]:
    h: dict[str, str] = {"Content-Type": "application/json", "Accept": "application/json"}
    tok = _env("FABLETOME_WORKER_TOKEN") or _env("REMOTE_JOBS_WORKER_TOKEN") or _env("BOOK_PIPELINE_WORKER_TOKEN")
    if not tok:
        return h
    h["X-Book-Pipeline-Worker"] = tok
    h["X-Worker-Id"] = _env("REMOTE_JOBS_WORKER_ID") or socket.gethostname()
    return h


def _log(name: str, method: str, path: str, status: int, detail: str = "") -> None:
    sym = "OK " if status < 400 else "ERR"
    extra = f" {detail}" if detail else ""
    print(f"[{sym}] {status:3d}  {method:4}  {path}{extra}")


def _safe_json(res: httpx.Response) -> Any:
    try:
        return res.json()
    except Exception:
        return res.text


def _try(client: httpx.Client, method: str, path: str, headers: dict[str, str], **kwargs: Any) -> tuple[int, Any]:
    url = f"{_base()}{path if path.startswith('/') else '/' + path}"
    r = client.request(method, url, headers=headers, timeout=60.0, **kwargs)
    return r.status_code, _safe_json(r)


def _optional_dynamo_get_job(job_id: str) -> dict[str, Any] | None:
    table = _env("FABLETOME_DYNAMODB_TABLE")
    if not table:
        return None
    try:
        import boto3  # type: ignore[import-not-found]
    except ImportError:
        print("[SKIP] boto3 not installed; pip install boto3 or use optional-deps [aws]", file=sys.stderr)
        return None
    region = _env("AWS_REGION") or _env("AWS_DEFAULT_REGION") or "us-east-1"
    pk_name = _env("FABLETOME_DYNAMODB_PK", "jobId")
    client = boto3.client("dynamodb", region_name=region)
    resp = client.get_item(TableName=table, Key={pk_name: {"S": job_id}})
    return resp.get("Item") or {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--source",
        type=Path,
        default=ROOT / "bookfolder" / "small.txt",
        help="Manuscript text file (default: bookfolder/small.txt)",
    )
    ap.add_argument("--poll-sec", type=float, default=5.0, help="Sleep between job status polls")
    ap.add_argument("--max-wait", type=float, default=900.0, help="Max seconds to wait for terminal job status")
    ap.add_argument("--no-wait", action="store_true", help="Do not poll after POST; exit after job is accepted")
    ap.add_argument("--project-id", default="", help="Optional project_id slug (default: api-e2e-<unix>)")
    args = ap.parse_args()

    _hydrate_credentials_from_aws()

    base = _base()
    user_id = _env("FABLETOME_USER_ID", "local-e2e-user")
    bearer = _env("FABLETOME_BEARER_TOKEN")
    if not bearer and not _env("FABLETOME_API_KEY"):
        print(
            "Note: no FABLETOME_BEARER_TOKEN and no FABLETOME_API_KEY; if POST /v1/jobs returns 403, add the key or JWT your gateway requires.",
            file=sys.stderr,
        )

    src: Path = args.source.expanduser().resolve()
    if not src.is_file():
        print(f"Source file not found: {src}", file=sys.stderr)
        sys.exit(2)
    document_text = src.read_text(encoding="utf-8", errors="replace")
    if len(document_text) > 4_000_000:
        print("Source file too large for this harness.", file=sys.stderr)
        sys.exit(2)

    project_id = (args.project_id or "").strip() or f"api-e2e-{int(time.time())}"

    post_body: dict[str, Any] = {
        "runMode": "book_pipeline",
        "claimQueueKey": "book_pipeline",
        "prompt": f"manuscript_ingest:{src.name}:rewrite",
        "bookJob": {
            "type": "manuscript_ingest",
            "payload": {
                "filename": src.name if src.suffix else "small.txt",
                "document_text": document_text,
                "user_goal": (
                    "Faithful short adaptation: preserve voice; produce screenplay-shaped markdown. "
                    "Keep under reasonable length for a smoke test."
                ),
                "goal_preset": "rewrite",
                "output_format": "md",
                "project_id": project_id,
                "settings_workspace": "workspace",
            },
        },
    }
    if not bearer:
        post_body["userId"] = user_id

    print(f"API base: {base}")
    print(f"User attribution: {'Bearer JWT' if bearer else f'body userId={user_id!r}'}")
    print(f"Project id: {project_id}")
    print("--- smoke: user-facing routes ---")

    hard_fail = False
    job_id: str | None = None
    last_status = ""
    timed_out = False

    with httpx.Client(follow_redirects=True) as client:
        uh = _user_headers()

        # GET /v1/manuscript-samples
        st, data = _try(client, "GET", "/v1/manuscript-samples", uh)
        _log("manuscript-samples", "GET", "/v1/manuscript-samples", st)
        sample_id: str | None = None
        if st == 200 and isinstance(data, dict):
            samples = data.get("samples")
            if isinstance(samples, list) and samples:
                first = samples[0]
                if isinstance(first, dict) and first.get("projectId"):
                    sample_id = str(first["projectId"])

        if sample_id:
            st2, _ = _try(client, "GET", f"/v1/manuscript-samples/{quote(sample_id, safe='')}", uh)
            _log("manuscript-sample-detail", "GET", f"/v1/manuscript-samples/{sample_id}", st2)
        else:
            print("      (no sample id from list; skipping detail route)")

        # POST /v1/jobs
        st, data = _try(client, "POST", "/v1/jobs", uh, content=json.dumps(post_body))
        _log("create job", "POST", "/v1/jobs", st, str(data)[:200] if not isinstance(data, dict) else "")
        if st >= 400 or not isinstance(data, dict):
            hard_fail = True
        else:
            job_id = str(data.get("jobId") or data.get("id") or "").strip() or None
            if not job_id:
                print("      ERROR: response missing jobId", file=sys.stderr)
                hard_fail = True

        if hard_fail or not job_id:
            print(json.dumps(data, indent=2) if isinstance(data, dict) else data, file=sys.stderr)
            return 1

        # GET /v1/jobs
        q = urlencode({"limit": "25"})
        if not bearer:
            q += f"&{urlencode({'userId': user_id})}"
        st, data = _try(client, "GET", f"/v1/jobs?{q}", uh)
        _log("list jobs", "GET", f"/v1/jobs?{q}", st)
        if st >= 400:
            hard_fail = True
        elif isinstance(data, dict):
            jobs = data.get("jobs")
            found = False
            if isinstance(jobs, list):
                for j in jobs:
                    if isinstance(j, dict) and str(j.get("jobId") or j.get("id")) == job_id:
                        found = True
                        break
            print(f"      job {job_id} present in list: {found}")

        # GET /v1/jobs/{jobId} (poll)
        deadline = time.monotonic() + (0 if args.no_wait else args.max_wait)
        terminal = {"succeeded", "failed", "canceled", "cancelled"}
        while True:
            st, detail = _try(client, "GET", f"/v1/jobs/{quote(job_id, safe='')}", uh)
            _log("job detail", "GET", f"/v1/jobs/{job_id}", st)
            if st >= 400:
                hard_fail = True
                break
            status = ""
            if isinstance(detail, dict):
                status = str(detail.get("status") or detail.get("jobStatus") or "").strip()
            last_status = status or last_status
            print(f"      status={last_status!r}")
            if args.no_wait:
                break
            if status.lower() in terminal or status.upper() == "DONE":
                break
            if time.monotonic() >= deadline:
                timed_out = True
                print(f"      TIMEOUT after {args.max_wait}s (last status={last_status!r})", file=sys.stderr)
                break
            time.sleep(args.poll_sec)

        # messages
        st, msgs = _try(client, "GET", f"/v1/jobs/{quote(job_id, safe='')}/messages?limit=10", uh)
        _log("list messages", "GET", f"/v1/jobs/{job_id}/messages", st)
        st2, _ = _try(
            client,
            "POST",
            f"/v1/jobs/{quote(job_id, safe='')}/messages",
            uh,
            content=json.dumps({"role": "user", "content": "E2E harness ping from fabletome_api_e2e.py"}),
        )
        _log("post message", "POST", f"/v1/jobs/{job_id}/messages", st2)

        # optional artifact routes (404 expected until Lambda implements them)
        art_path = f"/v1/jobs/{quote(job_id, safe='')}/artifact-url?{urlencode({'userId': user_id, 'key': f'inputs/manuscript/{src.name}'})}"
        st3, _ = _try(client, "GET", art_path, uh)
        _log("artifact-url", "GET", "/v1/jobs/.../artifact-url", st3)
        st4, _ = _try(
            client,
            "POST",
            f"/v1/jobs/{quote(job_id, safe='')}/artifact-presign-put",
            uh,
            content=json.dumps(
                {"keySuffix": f"inputs/manuscript/{src.name}", "contentType": "text/plain", "expiresInSeconds": 600}
            ),
        )
        _log("artifact-presign-put", "POST", "/v1/jobs/.../artifact-presign-put", st4)

        # worker claim smoke (does not claim our job unless it is next in queue)
        wh = _worker_headers()
        if wh.get("X-Book-Pipeline-Worker"):
            body = {
                "worker_id": wh.get("X-Worker-Id"),
                "capabilities": ["gutendex_ingest", "upload_manuscripts", "shell", "manuscript_ingest"],
            }
            stw, cw = _try(client, "POST", "/v1/jobs/claim", wh, content=json.dumps(body))
            _log("worker claim", "POST", "/v1/jobs/claim", stw, "job present" if isinstance(cw, dict) and cw.get("job") else "empty queue ok")
        else:
            print("[SKIP] worker claim: set FABLETOME_WORKER_TOKEN (or REMOTE_JOBS_WORKER_TOKEN)")

    # DynamoDB optional
    item = _optional_dynamo_get_job(job_id)
    if item is not None:
        if item:
            print("--- DynamoDB get_item: found row (keys only) ---")
            print(list(item.keys()))
        else:
            print("--- DynamoDB get_item: no item (check PK name FABLETOME_DYNAMODB_PK default jobId) ---")

    # Local project folder (after worker completes)
    bp_root = Path(_env("BOOK_PIPELINE_ROOT") or str(ROOT)).resolve()
    proj = bp_root / "projects" / project_id
    if proj.is_dir():
        print(f"--- local project exists: {proj} ---")
    else:
        print(
            f"--- local project not yet at {proj} (start remote_job_worker.py with same API + worker token; "
            f"or wait for status succeeded) ---",
            file=sys.stderr,
        )

    if not args.no_wait and timed_out and last_status and last_status.lower() not in terminal:
        return 1
    return 1 if hard_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
