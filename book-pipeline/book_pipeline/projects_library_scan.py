"""Scan ``BOOK_PIPELINE_PROJECTS_DIR`` for workspaces with manuscript outputs (local library UI)."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import yaml

from book_pipeline.project_workspace import projects_root, slug_gutendex_project_id


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def gutendex_manifest_path() -> Path | None:
    """``BOOK_GUTENDEX_MANIFEST`` or ``<repo>/gutenberg_library/manifest.json`` if present."""
    env = (os.environ.get("BOOK_GUTENDEX_MANIFEST") or "").strip()
    if env:
        p = Path(env).expanduser().resolve()
        return p if p.is_file() else None
    guess = Path(__file__).resolve().parent.parent / "gutenberg_library" / "manifest.json"
    return guess if guess.is_file() else None


def load_manifest_queue() -> tuple[Path | None, list[dict]]:
    """
    Ordered queue from Gutendex manifest ``books[]`` (same order as batch processing).

    Each row: ``queue_index`` (1-based), ``gutenberg_id``, ``title``, ``project_id`` (folder name).
    """
    mp = gutendex_manifest_path()
    if not mp:
        return None, []
    data = _load_json(mp)
    books = data.get("books") or []
    out: list[dict] = []
    for i, b in enumerate(books):
        if not isinstance(b, dict):
            continue
        try:
            gut_id = int(b.get("id"))
        except (TypeError, ValueError):
            continue
        title = (b.get("title") or "").strip() or "book"
        pid = slug_gutendex_project_id(gut_id, title)
        out.append(
            {
                "queue_index": i + 1,
                "gutenberg_id": gut_id,
                "title": title,
                "project_id": pid,
            }
        )
    return mp, out


def manifest_queue_index_for_project_id(project_id: str, queue: list[dict]) -> int | None:
    """Match ``projects/`` folder name to manifest order (by exact id or ``gut-<id>-`` prefix)."""
    for row in queue:
        if row.get("project_id") == project_id:
            return int(row["queue_index"])
    m = re.match(r"^gut-(\d+)-", project_id)
    if not m:
        return None
    try:
        gid = int(m.group(1))
    except ValueError:
        return None
    for row in queue:
        if int(row.get("gutenberg_id") or -1) == gid:
            return int(row["queue_index"])
    return None


def resolve_user_ask_preview(ws: Path, max_len: int = 280) -> str:
    goal_txt = ws / ".pipeline" / "gutendex_batch_goal.txt"
    if goal_txt.is_file():
        t = goal_txt.read_text(encoding="utf-8", errors="replace").strip()
        if t:
            return t if len(t) <= max_len else t[: max_len - 1] + "…"
    brain = ws / ".pipeline" / "gutendex_batch_brainstorm.json"
    if brain.is_file():
        obj = _load_json(brain)
        ug = (obj.get("user_goal") or "").strip()
        if ug:
            return ug if len(ug) <= max_len else ug[: max_len - 1] + "…"
    return ""


def resolve_converted_relpath(ws: Path) -> str | None:
    """Best-effort primary export path relative to workspace (same rules as upload script)."""
    root = ws.resolve()
    ex = _load_json(root / ".pipeline" / "export_last.json")
    rel = (ex.get("export_path") or "").strip()
    if rel:
        cand = (root / rel).resolve()
        if cand.is_file():
            try:
                cand.relative_to(root)
            except ValueError:
                pass
            else:
                return rel.replace("\\", "/")
    out = root / "outputs"
    if out.is_dir():
        stamped = sorted(out.glob("staging_merged_*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
        if stamped:
            try:
                return str(stamped[0].relative_to(root)).replace("\\", "/")
            except ValueError:
                pass
        for name in ("staging_merged.md", "staging_merged.txt"):
            p = out / name
            if p.is_file():
                try:
                    return str(p.relative_to(root)).replace("\\", "/")
                except ValueError:
                    pass
    draft = root / "manuscript" / "draft.md"
    if draft.is_file():
        return "manuscript/draft.md"
    return None


def _title_for_workspace(ws: Path) -> str:
    cfg = ws / "config.yaml"
    if cfg.is_file():
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            t = (data.get("title") or data.get("project_title") or "").strip()
            if t:
                return t
        except (OSError, yaml.YAMLError, TypeError, AttributeError):
            pass
    return ws.name.replace("-", " ")


def list_pipeline_output_files(workspace: Path, *, max_files: int = 400) -> dict:
    """
    Index markdown/text/json under ``outputs/`` (chunk snapshots live in ``outputs/staging_chunks/``).

    Sorted by path; capped for UI.
    """
    root = workspace.expanduser().resolve()
    out = root / "outputs"
    if not out.is_dir():
        return {"workspace": str(root), "outputs_dir": "outputs", "files": [], "truncated": False}
    all_files = sorted(
        [p for p in out.rglob("*") if p.is_file() and p.suffix.lower() in (".md", ".txt", ".json")],
        key=lambda p: str(p).lower(),
    )
    truncated = len(all_files) > max_files
    slice_ = all_files[:max_files]
    files: list[dict] = []
    for p in slice_:
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            continue
        try:
            sz = int(p.stat().st_size)
        except OSError:
            sz = 0
        files.append({"path": rel, "bytes": sz})
    return {"workspace": str(root), "outputs_dir": "outputs", "files": files, "truncated": truncated}


def read_outputs_workspace_file(workspace: Path, rel: str, *, max_bytes: int = 4 * 1024 * 1024) -> tuple[str, str]:
    """Read a single file under ``workspace/outputs/`` (relative path)."""
    root = workspace.expanduser().resolve()
    p = Path((rel or "").replace("\\", "/").strip())
    if p.is_absolute() or ".." in p.parts:
        raise ValueError("invalid path")
    if not p.parts or p.parts[0] != "outputs":
        raise ValueError("path must start with outputs/")
    full = (root / p).resolve()
    full.relative_to(root)
    if not full.is_file():
        raise ValueError("file not found")
    if full.suffix.lower() not in (".md", ".txt", ".json"):
        raise ValueError("unsupported file type")
    try:
        sz = int(full.stat().st_size)
    except OSError as e:
        raise ValueError("cannot stat file") from e
    if sz > max_bytes:
        raise ValueError(f"file too large (max {max_bytes} bytes for viewer)")
    return p.as_posix(), full.read_text(encoding="utf-8", errors="replace")


def scan_local_completed_projects() -> dict:
    """
    List child directories of ``projects_root()`` that have at least one known manuscript artifact.

    When ``gutenberg_library/manifest.json`` (or ``BOOK_GUTENDEX_MANIFEST``) exists, attach
    ``manifest_queue_index`` and sort rows in **manifest order** (unknown folders last, by name).
    """
    pr = projects_root().resolve()
    manifest_path, queue = load_manifest_queue()

    if not pr.is_dir():
        return {
            "projects_root": str(pr),
            "gutendex_manifest_path": str(manifest_path) if manifest_path else None,
            "manifest_queue": queue,
            "entries": [],
        }

    entries: list[dict] = []
    for child in sorted(pr.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        ws = child.resolve()
        draft = ws / "manuscript" / "draft.md"
        canonical = ws / "manuscript" / "canonical_merged.md"
        st_md = ws / "outputs" / "staging_merged.md"
        st_txt = ws / "outputs" / "staging_merged.txt"
        out_dir = ws / "outputs"
        stamped = list(out_dir.glob("staging_merged_*.txt")) if out_dir.is_dir() else []
        conv_rel = resolve_converted_relpath(ws)
        has_any = (
            draft.is_file()
            or canonical.is_file()
            or st_md.is_file()
            or st_txt.is_file()
            or bool(stamped)
            or bool(conv_rel)
        )
        if not has_any:
            continue
        pid = ws.name
        qix = manifest_queue_index_for_project_id(pid, queue) if queue else None
        entries.append(
            {
                "id": pid,
                "workspace": str(ws),
                "title": _title_for_workspace(ws),
                "userAskPreview": resolve_user_ask_preview(ws),
                "manifest_queue_index": qix,
                "artifacts": {
                    "draft_md": draft.is_file(),
                    "canonical_merged": canonical.is_file(),
                    "staging_merged": st_md.is_file() or st_txt.is_file() or bool(stamped),
                    "converted_relpath": conv_rel,
                },
            }
        )

    def sort_key(e: dict) -> tuple:
        q = e.get("manifest_queue_index")
        return (q is None, q if q is not None else 10**9, e["id"].lower())

    entries.sort(key=sort_key)

    return {
        "projects_root": str(pr),
        "gutendex_manifest_path": str(manifest_path) if manifest_path else None,
        "manifest_queue": queue,
        "entries": entries,
    }


def read_artifact(workspace: Path, kind: str) -> tuple[str, str]:
    """
    Resolve ``kind`` to a file under ``workspace`` and return ``(relative_path, text)``.

    ``kind``: draft | canonical | staging | converted
    """
    root = workspace.expanduser().resolve()
    if not root.is_dir():
        raise ValueError("workspace is not a directory")

    rel: str | None = None
    k = (kind or "").strip().lower()
    if k == "draft":
        rel = "manuscript/draft.md"
    elif k == "canonical":
        rel = "manuscript/canonical_merged.md"
    elif k == "staging":
        out = root / "outputs"
        if out.is_dir():
            stamped = sorted(out.glob("staging_merged_*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
            if stamped:
                rel = str(stamped[0].relative_to(root)).replace("\\", "/")
            elif (out / "staging_merged.md").is_file():
                rel = "outputs/staging_merged.md"
            elif (out / "staging_merged.txt").is_file():
                rel = "outputs/staging_merged.txt"
    elif k == "converted":
        rel = resolve_converted_relpath(root)
    else:
        raise ValueError("kind must be draft, canonical, staging, or converted")

    if not rel:
        raise ValueError(f"no file for kind={kind!r}")

    parts = Path(rel.replace("\\", "/"))
    if parts.is_absolute() or ".." in parts.parts:
        raise ValueError("invalid path")
    full = (root / parts).resolve()
    full.relative_to(root)
    if not full.is_file():
        raise ValueError("file not found")
    text = full.read_text(encoding="utf-8", errors="replace")
    return rel.replace("\\", "/"), text
