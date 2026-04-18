from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any


def studio_root() -> Path:
    env = (os.environ.get("BOOK_STUDIO_ROOT") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent / ".studio"


def studio_db_path() -> Path:
    env = (os.environ.get("BOOK_STUDIO_DB") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return studio_root() / "studio.sqlite"


def projects_root() -> Path:
    env = (os.environ.get("BOOK_STUDIO_PROJECTS_ROOT") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return studio_root() / "projects"


def connect() -> sqlite3.Connection:
    path = studio_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS studio_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            display_name TEXT NOT NULL DEFAULT 'local',
            created REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS studio_projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES studio_users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            slug TEXT NOT NULL UNIQUE,
            workspace_path TEXT NOT NULL,
            created REAL NOT NULL,
            updated REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_projects_user ON studio_projects(user_id);
        """
    )
    conn.commit()


def ensure_default_user(conn: sqlite3.Connection) -> int:
    init_schema(conn)
    row = conn.execute("SELECT id FROM studio_users ORDER BY id LIMIT 1").fetchone()
    if row:
        return int(row["id"])
    now = time.time()
    cur = conn.execute(
        "INSERT INTO studio_users (display_name, created) VALUES (?, ?)",
        ("local", now),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_user(conn: sqlite3.Connection, user_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT id, display_name, created FROM studio_users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def update_user_display_name(conn: sqlite3.Connection, user_id: int, display_name: str) -> None:
    conn.execute(
        "UPDATE studio_users SET display_name = ? WHERE id = ?",
        ((display_name or "local").strip() or "local", user_id),
    )
    conn.commit()


def list_projects(conn: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, user_id, name, slug, workspace_path, created, updated "
        "FROM studio_projects WHERE user_id = ? ORDER BY updated DESC",
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_project(conn: sqlite3.Connection, project_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, user_id, name, slug, workspace_path, created, updated FROM studio_projects WHERE id = ?",
        (project_id,),
    ).fetchone()
    return dict(row) if row else None


def insert_project(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    name: str,
    slug: str,
    workspace_path: Path,
) -> int:
    now = time.time()
    cur = conn.execute(
        "INSERT INTO studio_projects (user_id, name, slug, workspace_path, created, updated) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, name.strip(), slug, str(workspace_path.resolve()), now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def touch_project(conn: sqlite3.Connection, project_id: int) -> None:
    conn.execute("UPDATE studio_projects SET updated = ? WHERE id = ?", (time.time(), project_id))
    conn.commit()
