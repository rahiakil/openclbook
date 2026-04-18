from __future__ import annotations

import json
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[misc, assignment]


def pop_task(todo_path: Path) -> dict | None:
    """
    Pop first JSON object from todo.file (one JSON object per line, JSONL).
    Uses flock on Unix for concurrent watchers.
    """
    if not todo_path.exists():
        return None

    if fcntl is None:
        raw = todo_path.read_text(encoding="utf-8")
        lines = [ln for ln in raw.splitlines() if ln.strip()]
        if not lines:
            todo_path.write_text("", encoding="utf-8")
            return None
        task = json.loads(lines[0])
        todo_path.write_text("\n".join(lines[1:]) + ("\n" if len(lines) > 1 else ""), encoding="utf-8")
        return task

    with open(todo_path, "r+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            raw = f.read()
            lines = [ln for ln in raw.splitlines() if ln.strip()]
            if not lines:
                f.seek(0)
                f.truncate()
                return None
            task = json.loads(lines[0])
            rest = "\n".join(lines[1:])
            f.seek(0)
            f.write(rest + ("\n" if rest else ""))
            f.truncate()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    return task


def append_task(todo_path: Path, task: dict) -> None:
    line = json.dumps(task, ensure_ascii=False)
    todo_path.parent.mkdir(parents=True, exist_ok=True)
    with open(todo_path, "a", encoding="utf-8") as f:
        if fcntl:
            fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(line + "\n")
        finally:
            if fcntl:
                fcntl.flock(f, fcntl.LOCK_UN)
