#!/usr/bin/env python3
"""
Upload completed book-pipeline projects to S3: Gutenberg/source original, converted export,
user brief (``user_ask``), optional free-tier preview (first N chapter blocks), and manifests.

Default bucket name (override with ``--bucket`` or env ``MANUSCRIPTS_S3_BUCKET``):
  manuscripts-done-internal

Typical usage (every few days after runs finish):

  cd book-pipeline
  uv sync --extra aws
  export AWS_PROFILE=...   # or AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
  python scripts/upload_completed_manuscripts_s3.py --dry-run
  python scripts/upload_completed_manuscripts_s3.py

Emit static files for the Sceneweaver UI (``public/samples``) without uploading:

  python scripts/upload_completed_manuscripts_s3.py --emit-sceneweaver /path/to/sceneweaver/public/samples

S3 object layout::

  projects/<project_id>/original<suffix>
  projects/<project_id>/converted<suffix>
  projects/<project_id>/user_ask.txt
  projects/<project_id>/preview.txt          # first ``--free-chapters`` sections (see below)
  projects/<project_id>/project_manifest.json
  samples_index.json                          # registry for UIs / ops

Chapter boundaries for ``preview.txt`` are detected from converted text: lines matching
``...#chapter-NN...`` (staging merge) or markdown ``^##\\s+Chapter`` / ``^#\\s+Chapter``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from book_pipeline.project_workspace import projects_root  # noqa: E402

DEFAULT_BUCKET = "manuscripts-done-internal"
CHAPTER_ANCHOR_RE = re.compile(r"^[^\n]*#chapter-\d+[^\n]*$", re.MULTILINE)
MD_CHAPTER_RE = re.compile(r"^#{1,2}\s+Chapter\b", re.MULTILINE | re.IGNORECASE)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def resolve_user_ask(ws: Path) -> str:
    goal_txt = ws / ".pipeline" / "gutendex_batch_goal.txt"
    if goal_txt.is_file():
        t = goal_txt.read_text(encoding="utf-8").strip()
        if t:
            return t
    brain = ws / ".pipeline" / "gutendex_batch_brainstorm.json"
    if brain.is_file():
        obj = load_json(brain)
        ug = (obj.get("user_goal") or "").strip()
        if ug:
            return ug
    return ""


def resolve_original_path(ws: Path) -> tuple[Path | None, str]:
    """Return absolute path to original source file and file suffix (e.g. .txt)."""
    inj = load_json(ws / ".pipeline" / "ingest_job.json")
    src = (inj.get("source_input") or "").strip()
    suf = (inj.get("source_suffix") or ".txt").strip() or ".txt"
    if not src:
        gut = ws / ".pipeline" / "gutendex_source.txt"
        if gut.is_file():
            return gut.resolve(), ".txt"
        return None, suf
    p = Path(src).expanduser()
    if not p.is_absolute():
        p = (ws / p).resolve()
    if p.is_file():
        return p, suf if suf.startswith(".") else f".{suf}"
    return None, suf


def resolve_converted_path(ws: Path) -> tuple[Path | None, str]:
    ex = load_json(ws / ".pipeline" / "export_last.json")
    rel = (ex.get("export_path") or "").strip()
    if rel:
        cand = (ws / rel).resolve()
        if cand.is_file():
            suf = cand.suffix or ".txt"
            return cand, suf
    out = ws / "outputs"
    if out.is_dir():
        stamped = sorted(out.glob("staging_merged_*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
        if stamped:
            p = stamped[0]
            return p.resolve(), p.suffix or ".txt"
        for name in ("staging_merged.md", "staging_merged.txt"):
            p = out / name
            if p.is_file():
                return p.resolve(), p.suffix
    draft = ws / "manuscript" / "draft.md"
    if draft.is_file():
        return draft.resolve(), ".md"
    return None, ".md"


def split_chapter_sections(text: str) -> list[str]:
    if not (text or "").strip():
        return []
    if CHAPTER_ANCHOR_RE.search(text):
        matches = list(CHAPTER_ANCHOR_RE.finditer(text))
        parts: list[str] = []
        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            parts.append(text[start:end].strip())
        return [p for p in parts if p]
    if MD_CHAPTER_RE.search(text):
        matches = list(MD_CHAPTER_RE.finditer(text))
        parts = []
        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            parts.append(text[start:end].strip())
        return [p for p in parts if p]
    return [text.strip()]


def build_preview(converted_text: str, free_chapters: int) -> str:
    sections = split_chapter_sections(converted_text)
    if not sections:
        return ""
    n = max(1, free_chapters)
    return "\n\n---\n\n".join(sections[:n])


def pretty_title(project_id: str) -> str:
    if project_id.startswith("gut-") and "-" in project_id[4:]:
        rest = project_id.split("-", 2)
        if len(rest) >= 3:
            return rest[2].replace("-", " ").strip() or project_id
    return project_id.replace("-", " ").replace("_", " ")


def collect_project_payload(ws: Path, free_chapters: int) -> dict[str, Any] | None:
    orig, orig_suf = resolve_original_path(ws)
    conv, conv_suf = resolve_converted_path(ws)
    if orig is None or conv is None:
        return None
    user_ask = resolve_user_ask(ws)
    converted_body = conv.read_text(encoding="utf-8", errors="replace")
    preview = build_preview(converted_body, free_chapters)
    return {
        "project_id": ws.name,
        "title": pretty_title(ws.name),
        "user_ask": user_ask,
        "original_path": orig,
        "original_suffix": orig_suf,
        "converted_path": conv,
        "converted_suffix": conv_suf,
        "preview_text": preview,
        "converted_bytes": conv.stat().st_size,
        "original_bytes": orig.stat().st_size,
    }


def write_sceneweaver_samples(samples_dir: Path, entries: list[dict[str, Any]], free_chapters: int) -> None:
    samples_dir = samples_dir.resolve()
    samples_dir.mkdir(parents=True, exist_ok=True)
    manifest_samples: list[dict[str, Any]] = []
    for e in entries:
        pid = e["project_id"]
        sub = samples_dir / pid
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "preview.txt").write_text(e.get("preview_text") or "", encoding="utf-8")
        (sub / "user_ask.txt").write_text(e.get("user_ask") or "", encoding="utf-8")
        manifest_samples.append(
            {
                "id": pid,
                "title": e["title"],
                "userAsk": e.get("user_ask") or "",
                "paths": {
                    "preview": f"/samples/{pid}/preview.txt",
                    "userAsk": f"/samples/{pid}/user_ask.txt",
                },
            }
        )
    manifest = {
        "updatedAt": _utc_now(),
        "freeChapterCount": free_chapters,
        "subscriptionCta": {
            "headline": "Continue with a paid subscription",
            "body": "Free reading includes the first chapters only. Subscribe to unlock the full manuscript and new releases.",
            "buttonLabel": "View subscription options",
            "href": "https://example.com/subscribe",
        },
        "samples": manifest_samples,
    }
    (samples_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _s3_base(prefix: str, project_id: str) -> str:
    parts = [p.strip("/") for p in (prefix, project_id) if p and p.strip("/")]
    return "/".join(parts)


def upload_one(
    s3,
    bucket: str,
    prefix: str,
    payload: dict[str, Any],
    free_chapters: int,
    dry_run: bool,
) -> dict[str, Any]:
    base = _s3_base(prefix, payload["project_id"])
    keys = {
        "original": f"{base}/original{payload['original_suffix']}",
        "converted": f"{base}/converted{payload['converted_suffix']}",
        "user_ask": f"{base}/user_ask.txt",
        "preview": f"{base}/preview.txt",
        "project_manifest": f"{base}/project_manifest.json",
    }
    pm = {
        "project_id": payload["project_id"],
        "title": payload["title"],
        "user_ask": payload["user_ask"],
        "free_chapters": free_chapters,
        "uploaded_at": _utc_now(),
        "objects": keys,
        "bytes": {"original": payload["original_bytes"], "converted": payload["converted_bytes"]},
    }
    if dry_run:
        return {"keys": keys, "project_manifest": pm}

    extra = {"ContentType": "text/plain; charset=utf-8"}
    s3.upload_file(str(payload["original_path"]), bucket, keys["original"], ExtraArgs=extra)
    s3.upload_file(str(payload["converted_path"]), bucket, keys["converted"], ExtraArgs=extra)
    s3.put_object(Bucket=bucket, Key=keys["user_ask"], Body=payload["user_ask"].encode("utf-8"), **extra)
    s3.put_object(
        Bucket=bucket,
        Key=keys["preview"],
        Body=(payload.get("preview_text") or "").encode("utf-8"),
        **extra,
    )
    s3.put_object(
        Bucket=bucket,
        Key=keys["project_manifest"],
        Body=json.dumps(pm, indent=2, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )
    return {"keys": keys, "project_manifest": pm}


def main() -> int:
    ap = argparse.ArgumentParser(description="Upload completed manuscripts + user ask to S3")
    ap.add_argument("--projects-dir", type=Path, default=None, help="Defaults to book_pipeline.projects_root()")
    ap.add_argument("--bucket", default=os.environ.get("MANUSCRIPTS_S3_BUCKET", DEFAULT_BUCKET))
    ap.add_argument("--prefix", default=os.environ.get("MANUSCRIPTS_S3_PREFIX", ""))
    ap.add_argument("--free-chapters", type=int, default=int(os.environ.get("MANUSCRIPTS_FREE_CHAPTERS", "3")))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--emit-sceneweaver",
        type=Path,
        default=None,
        help="Write public/samples manifest + preview files for the Sceneweaver app",
    )
    ap.add_argument("--only", nargs="*", help="Only these project directory names")
    args = ap.parse_args()

    try:
        import boto3  # type: ignore
    except ImportError:
        boto3 = None  # type: ignore
    if boto3 is None and not args.dry_run:
        print("error: boto3 required for S3 upload. Install: uv sync --extra aws", file=sys.stderr)
        return 1

    root = (args.projects_dir or projects_root()).resolve()
    if not root.is_dir():
        print(f"error: projects dir missing: {root}", file=sys.stderr)
        return 1

    only = {x.strip() for x in (args.only or []) if x.strip()}
    payloads: list[dict[str, Any]] = []
    skipped: list[str] = []

    for d in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        if only and d.name not in only:
            continue
        pl = collect_project_payload(d, args.free_chapters)
        if pl is None:
            skipped.append(d.name)
            continue
        payloads.append(pl)

    if args.emit_sceneweaver:
        write_sceneweaver_samples(args.emit_sceneweaver, payloads, args.free_chapters)
        print(f"wrote Sceneweaver samples under {args.emit_sceneweaver.resolve()}")

    s3_client = None
    if not args.dry_run and boto3 is not None:
        s3_client = boto3.client("s3")

    index_samples: list[dict[str, Any]] = []
    for pl in payloads:
        if args.dry_run or s3_client is None:
            out = upload_one(None, args.bucket, args.prefix, pl, args.free_chapters, dry_run=True)
            print(f"[dry-run] {pl['project_id']} -> {out['keys']}")
        else:
            out = upload_one(s3_client, args.bucket, args.prefix, pl, args.free_chapters, dry_run=False)
            print(f"uploaded {pl['project_id']}")
        pm = out["project_manifest"]
        index_samples.append(
            {
                "project_id": pl["project_id"],
                "title": pl["title"],
                "user_ask": pl["user_ask"],
                "objects": pm["objects"],
            }
        )

    if skipped:
        print(f"skipped (missing original or converted): {', '.join(skipped)}", file=sys.stderr)

    index = {
        "updated_at": _utc_now(),
        "bucket": args.bucket,
        "prefix": args.prefix,
        "free_chapters": args.free_chapters,
        "projects": index_samples,
    }
    index_path = root.parent / "samples_index.latest.json"
    index_path.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {index_path}")

    if not args.dry_run and s3_client is not None:
        key = _s3_base(args.prefix, "samples_index.json")
        s3_client.put_object(
            Bucket=args.bucket,
            Key=key,
            Body=json.dumps(index, indent=2, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json; charset=utf-8",
        )
        print(f"s3://{args.bucket}/{key}")
    elif not args.dry_run:
        print("(no S3 upload: dry-run or boto3 missing)", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
