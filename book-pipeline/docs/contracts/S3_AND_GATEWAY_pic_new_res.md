# Manuscript jobs → S3 (pic-new-res `terraform-http-api`)

## Do not create “one S3 bucket per run”

AWS accounts have a **soft bucket limit** (~100) and many buckets complicate IAM/CORS. The pic-new-res stack already uses **one artifacts bucket per environment**:

- `aws_s3_bucket.artifacts` → `${var.project_name}-artifacts-${account_id}` (see `main.tf`)

**Timestamps belong in object keys**, not new bucket names. `job-store.mjs` already standardizes:

```text
users/{userId}/jobs/{YYYY-MM-DD}/{jobId}/
```

## Recommended keys for book-pipeline uploads

After `POST /v1/jobs` returns `jobId` (and the worker / your uploader knows `userId` + `createdAt` for `buildS3ArtifactPrefix`), store inputs under that prefix:

| Relative key | Purpose |
|--------------|---------|
| `inputs/manuscript/{filename}` | Raw upload (.md / .txt / .docx) |
| `inputs/book_job_request.json` | `manuscript_job_v1` contract (this repo: `docs/contracts/manuscript_job_v1.schema.json`) |
| `meta/client_status.json` | `{ "client_status": "draft|ready" }` so workers skip incomplete drafts |
| `outputs/` | Reserved for worker exports (txt/md/docx, images, metrics) |

`client_status` in the JSON contract should mirror `meta/client_status.json` for simple pollers.

## Gateway surface (same API Gateway HTTP API)

Job lifecycle + artifacts:

- `POST /v1/jobs` — body includes `prompt`, `options`, optional `projectId`, `runMode`, …
- `GET /v1/jobs/{jobId}` — poll status
- `GET /v1/jobs/{jobId}/artifact-url?userId=…&key=…` — **presigned GET** (time-limited HTTPS URL) so the browser can **download** one private S3 object without AWS credentials. “Presigned” means AWS signs the exact `GET` + `bucket/key` (+ expiry); anyone with the URL can read that object until it expires.
- `POST /v1/jobs/{jobId}/artifact-presign-put` — **presigned PUT** for **browser → S3 uploads** (same auth rules as other job routes). Body:

```json
{
  "keySuffix": "inputs/manuscript/draft.md",
  "contentType": "text/markdown",
  "expiresInSeconds": 600
}
```

Response:

```json
{
  "jobId": "job_…",
  "key": "users/…/jobs/2026-04-25/job_…/inputs/manuscript/draft.md",
  "uploadUrl": "https://…amazonaws.com/…",
  "method": "PUT",
  "headers": { "Content-Type": "text/markdown" },
  "expiresInSeconds": 600
}
```

The SPA must `fetch(uploadUrl, { method: "PUT", headers, body: fileBlobOrBuffer })` using **exactly** the signed `Content-Type`.

## CORS (SceneWeaver / `sceneweaver.one`)

Terraform variable `cors_allow_origins` drives **both**:

- API Gateway HTTP API CORS (browser → `https://i5ygx46inb.execute-api.us-east-1.amazonaws.com/...`)
- S3 artifacts bucket CORS (browser → `https://…s3…amazonaws.com/...` on presigned PUT/GET)

Set explicit origins in `terraform.tfvars` (see `terraform.tfvars.example` in pic-new-res), e.g. `https://sceneweaver.one` and `https://www.sceneweaver.one`. The S3 bucket rule includes **PUT** so presigned uploads work from the SPA.

After `terraform apply`, if API Gateway reports a route conflict for the new path, use the repo’s import script pattern noted in `main.tf` for job routes.

## DynamoDB

Jobs are rows in `${project_name}-jobs` with `jobId` as PK. Treat `book_job_request.json` as the **source of truth** for “what the user wanted”; Dynamo carries operational status (`queued`, `running`, `succeeded`, …).
