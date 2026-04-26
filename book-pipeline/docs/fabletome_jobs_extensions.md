# Fabletome jobs API — extensions for SceneWeaver

Terraform for the HTTP API lives in your infra repo; this document is the **contract** the Sceneweaver frontend and ``remote_job_worker.py`` expect after you extend the Lambda + Dynamo handlers.

## 1. ``manuscript_ingest`` payload additions

| Field | Type | Notes |
| --- | --- | --- |
| ``adaptation_spec`` | object | Optional. Merged into the effective ``user_goal`` on the worker (see ``_merge_user_goal_with_adaptation``). |
| ``adaptation_spec.pipeline`` | ``faithful`` \| ``twist`` | |
| ``adaptation_spec.seasons`` | int | Clamped on worker when building skeleton tree (1–6). |
| ``adaptation_spec.episodesPerSeason`` | int | Clamped (1–24) for placeholder tree. |
| ``adaptation_spec.twistAxis`` | string | One of ``time_period``, ``character``, ``mood``, ``length``, ``extra_season``, ``prelude``. |
| ``adaptation_spec.notes`` | string | Appended to the expanded block. |

## 2. Job completion body (POST …/complete)

The worker now sends:

```json
{
  "ok": true,
  "type": "manuscript_ingest",
  "worker_id": "hostname",
  "log_tail": "...",
  "result": {
    "adaptation_tree": {
      "source": "Original manuscript / uploaded file in project .pipeline",
      "seasons": [
        {
          "title": "Season 1",
          "episodes": [
            { "title": "Episode 1", "screenplay": "(Draft lives in project workspace...)" }
          ]
        }
      ]
    },
    "adaptation_spec": { }
  }
}
```

**Lambda change:** persist ``result`` on the job item and return it from ``GET /v1/jobs/{jobId}`` (e.g. as top-level ``result`` on the job JSON). The Studio reads ``detail.result.adaptation_tree``.

## 3. Pause signals

- Users post messages containing ``CONTROL:PAUSE`` or ``PAUSE_REQUEST:`` (legacy UI string).
- Worker: if ``REMOTE_JOBS_READ_MESSAGES_TOKEN`` or ``REMOTE_JOBS_TOKEN`` is set with a JWT allowed to call ``GET /v1/jobs/{jobId}/messages``, the worker runs a **pre-ingest** pause check.
- **Mid-run pause** requires either (a) streaming ingest with cooperative checkpoints, or (b) a dedicated ``POST /v1/jobs/{jobId}/pause`` authenticated with ``X-Book-Pipeline-Worker`` that sets Dynamo ``status=paused`` and the worker polling job state between chunks.

## 4. First-class review + credits (Dynamo)

Add optional attributes on the job item (names are suggestions—align with your Lambda):

| Attribute | Type | Purpose |
| --- | --- | --- |
| ``reviewState`` | string | ``none``, ``pending_review``, ``approved``, ``rejected``, ``resent``. |
| ``pausedReason`` | string | When ``status`` is ``paused``. |
| ``creditsReserved`` | number | Server-side reserved credits for this job (authoritative billing). |
| ``creditsConsumed`` | number | Charged on success/fail. |

**Routes (suggested):**

- ``PATCH /v1/jobs/{jobId}/review`` — body ``{ "reviewState": "approved" }`` (JWT: owner or admin).
- ``GET /v1/jobs/{jobId}/messages`` — allow worker principal **or** user JWT (worker needs read for pause; scope narrowly).

## 5. Chunk / artifact editor (future)

Expose either:

- ``GET /v1/jobs/{jobId}/artifact?key=relative/path`` with presigned S3 redirect, or  
- A manifest endpoint listing ``outputs/staging_chunks/**`` for the job’s project prefix.

The Studio can then attach an episode leaf to fetched text.

## 6. Authoritative credits

Replace client-only estimates with:

- ``GET /v1/billing/summary`` (JWT) → ``{ tier, monthlyBudget, used, remaining }``, or  
- Echo ``creditsReserved`` / ``creditsConsumed`` on each job in ``GET /v1/jobs``.
