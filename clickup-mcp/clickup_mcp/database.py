"""SQLite database layer with FTS5 full-text search over tasks."""

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

logger = logging.getLogger(__name__)

DB_PATH = Path.home() / ".clickup-mcp" / "clickup.db"

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS workspaces (
    id    TEXT PRIMARY KEY,
    name  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS spaces (
    id            TEXT PRIMARY KEY,
    workspace_id  TEXT NOT NULL,
    name          TEXT NOT NULL,
    archived      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS folders (
    id        TEXT PRIMARY KEY,
    space_id  TEXT NOT NULL,
    name      TEXT NOT NULL,
    archived  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS lists (
    id                TEXT PRIMARY KEY,
    folder_id         TEXT,
    space_id          TEXT NOT NULL,
    name              TEXT NOT NULL,
    status            TEXT,
    task_count        INTEGER DEFAULT 0,
    archived          INTEGER DEFAULT 0,
    fully_synced      INTEGER DEFAULT 0,
    last_sync_time    TEXT,
    last_updated_gt   TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id          TEXT PRIMARY KEY,
    list_id          TEXT,
    list_name        TEXT,
    folder_id        TEXT,
    folder_name      TEXT,
    space_id         TEXT,
    space_name       TEXT,
    workspace_id     TEXT,
    name             TEXT,
    description      TEXT,
    status           TEXT,
    status_color     TEXT,
    priority         INTEGER,
    assignees        TEXT DEFAULT '[]',
    tags             TEXT DEFAULT '[]',
    due_date         TEXT,
    start_date       TEXT,
    time_estimate    INTEGER,
    time_spent       INTEGER,
    creator_id       TEXT,
    creator_name     TEXT,
    date_created     TEXT,
    date_updated     TEXT,
    date_closed      TEXT,
    is_closed        INTEGER DEFAULT 0,
    is_deleted       INTEGER DEFAULT 0,
    parent_task_id   TEXT,
    subtask_ids      TEXT DEFAULT '[]',
    custom_fields    TEXT DEFAULT '[]',
    url              TEXT,
    archived         INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_tasks_list     ON tasks(list_id);
CREATE INDEX IF NOT EXISTS idx_tasks_updated  ON tasks(date_updated DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_parent   ON tasks(parent_task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status   ON tasks(status);

CREATE VIRTUAL TABLE IF NOT EXISTS tasks_fts USING fts5(
    name,
    description,
    tags,
    content='tasks',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS tasks_ai AFTER INSERT ON tasks BEGIN
    INSERT INTO tasks_fts(rowid, name, description, tags)
    VALUES (new.rowid, new.name, new.description, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS tasks_ad AFTER DELETE ON tasks BEGIN
    INSERT INTO tasks_fts(tasks_fts, rowid, name, description, tags)
    VALUES ('delete', old.rowid, old.name, old.description, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS tasks_au AFTER UPDATE ON tasks BEGIN
    INSERT INTO tasks_fts(tasks_fts, rowid, name, description, tags)
    VALUES ('delete', old.rowid, old.name, old.description, old.tags);
    INSERT INTO tasks_fts(rowid, name, description, tags)
    VALUES (new.rowid, new.name, new.description, new.tags);
END;

CREATE TABLE IF NOT EXISTS members (
    id              TEXT PRIMARY KEY,
    username        TEXT,
    email           TEXT,
    color           TEXT,
    profile_picture TEXT
);

CREATE TABLE IF NOT EXISTS comments (
    id            TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL,
    comment_text  TEXT,
    user_id       TEXT,
    date          TEXT,
    resolved      INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_comments_task ON comments(task_id);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    logger.info("Database initialized at %s", DB_PATH)


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Workspaces / Spaces / Folders / Lists ────────────────────────────────────

def upsert_workspace(conn: sqlite3.Connection, ws: dict) -> None:
    conn.execute(
        "INSERT INTO workspaces (id, name) VALUES (:id, :name) "
        "ON CONFLICT(id) DO UPDATE SET name = excluded.name",
        ws,
    )


def upsert_space(conn: sqlite3.Connection, sp: dict) -> None:
    conn.execute(
        """
        INSERT INTO spaces (id, workspace_id, name, archived)
        VALUES (:id, :workspace_id, :name, :archived)
        ON CONFLICT(id) DO UPDATE SET
            workspace_id = excluded.workspace_id,
            name         = excluded.name,
            archived     = excluded.archived
        """,
        sp,
    )


def upsert_folder(conn: sqlite3.Connection, f: dict) -> None:
    conn.execute(
        """
        INSERT INTO folders (id, space_id, name, archived)
        VALUES (:id, :space_id, :name, :archived)
        ON CONFLICT(id) DO UPDATE SET
            space_id = excluded.space_id,
            name     = excluded.name,
            archived = excluded.archived
        """,
        f,
    )


def upsert_list(conn: sqlite3.Connection, lst: dict) -> None:
    conn.execute(
        """
        INSERT INTO lists (id, folder_id, space_id, name, status, task_count, archived)
        VALUES (:id, :folder_id, :space_id, :name, :status, :task_count, :archived)
        ON CONFLICT(id) DO UPDATE SET
            folder_id  = excluded.folder_id,
            space_id   = excluded.space_id,
            name       = excluded.name,
            status     = excluded.status,
            task_count = excluded.task_count,
            archived   = excluded.archived
        """,
        lst,
    )


def set_list_fully_synced(conn: sqlite3.Connection, list_id: str, last_updated_gt: str | None) -> None:
    conn.execute(
        """
        UPDATE lists SET fully_synced = 1, last_sync_time = datetime('now'),
                         last_updated_gt = COALESCE(?, last_updated_gt)
        WHERE id = ?
        """,
        (last_updated_gt, list_id),
    )


def update_list_last_updated_gt(conn: sqlite3.Connection, list_id: str, last_updated_gt: str) -> None:
    conn.execute(
        "UPDATE lists SET last_updated_gt = ?, last_sync_time = datetime('now') WHERE id = ?",
        (last_updated_gt, list_id),
    )


def get_workspaces() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM workspaces ORDER BY name")]


def get_spaces(workspace_id: str | None = None) -> list[dict]:
    with get_conn() as conn:
        if workspace_id:
            rows = conn.execute(
                "SELECT * FROM spaces WHERE workspace_id = ? ORDER BY name", (workspace_id,)
            )
        else:
            rows = conn.execute("SELECT * FROM spaces ORDER BY name")
        return [dict(r) for r in rows]


def get_folders(space_id: str | None = None) -> list[dict]:
    with get_conn() as conn:
        if space_id:
            rows = conn.execute(
                "SELECT * FROM folders WHERE space_id = ? ORDER BY name", (space_id,)
            )
        else:
            rows = conn.execute("SELECT * FROM folders ORDER BY name")
        return [dict(r) for r in rows]


def get_lists(folder_id: str | None = None, space_id: str | None = None) -> list[dict]:
    with get_conn() as conn:
        if folder_id:
            rows = conn.execute(
                "SELECT * FROM lists WHERE folder_id = ? ORDER BY name", (folder_id,)
            )
        elif space_id:
            rows = conn.execute(
                "SELECT * FROM lists WHERE space_id = ? ORDER BY name", (space_id,)
            )
        else:
            rows = conn.execute("SELECT * FROM lists ORDER BY name")
        return [dict(r) for r in rows]


def get_all_lists() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM lists")]


def get_unsynced_lists() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM lists WHERE fully_synced = 0")]


# ── Tasks ─────────────────────────────────────────────────────────────────────

def upsert_task(conn: sqlite3.Connection, task: dict) -> None:
    conn.execute(
        """
        INSERT INTO tasks (
            task_id, list_id, list_name, folder_id, folder_name, space_id, space_name,
            workspace_id, name, description, status, status_color, priority,
            assignees, tags, due_date, start_date, time_estimate, time_spent,
            creator_id, creator_name, date_created, date_updated, date_closed,
            is_closed, is_deleted, parent_task_id, subtask_ids, custom_fields, url, archived
        ) VALUES (
            :task_id, :list_id, :list_name, :folder_id, :folder_name, :space_id, :space_name,
            :workspace_id, :name, :description, :status, :status_color, :priority,
            :assignees, :tags, :due_date, :start_date, :time_estimate, :time_spent,
            :creator_id, :creator_name, :date_created, :date_updated, :date_closed,
            :is_closed, :is_deleted, :parent_task_id, :subtask_ids, :custom_fields, :url, :archived
        )
        ON CONFLICT(task_id) DO UPDATE SET
            list_id        = excluded.list_id,
            list_name      = excluded.list_name,
            folder_id      = excluded.folder_id,
            folder_name    = excluded.folder_name,
            space_id       = excluded.space_id,
            space_name     = excluded.space_name,
            workspace_id   = excluded.workspace_id,
            name           = excluded.name,
            description    = excluded.description,
            status         = excluded.status,
            status_color   = excluded.status_color,
            priority       = excluded.priority,
            assignees      = excluded.assignees,
            tags           = excluded.tags,
            due_date       = excluded.due_date,
            start_date     = excluded.start_date,
            time_estimate  = excluded.time_estimate,
            time_spent     = excluded.time_spent,
            creator_id     = excluded.creator_id,
            creator_name   = excluded.creator_name,
            date_created   = excluded.date_created,
            date_updated   = excluded.date_updated,
            date_closed    = excluded.date_closed,
            is_closed      = excluded.is_closed,
            is_deleted     = excluded.is_deleted,
            parent_task_id = excluded.parent_task_id,
            subtask_ids    = excluded.subtask_ids,
            custom_fields  = excluded.custom_fields,
            url            = excluded.url,
            archived       = excluded.archived
        """,
        task,
    )


def mark_task_deleted(conn: sqlite3.Connection, task_id: str) -> None:
    conn.execute("UPDATE tasks SET is_deleted = 1 WHERE task_id = ?", (task_id,))


def get_task_by_id(task_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return dict(row) if row else None


def get_tasks_by_list(list_id: str, include_closed: bool = True, limit: int = 100, offset: int = 0) -> list[dict]:
    with get_conn() as conn:
        sql = "SELECT * FROM tasks WHERE list_id = ? AND is_deleted = 0"
        params: list = [list_id]
        if not include_closed:
            sql += " AND is_closed = 0"
        sql += " ORDER BY date_updated DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_subtasks(parent_task_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE parent_task_id = ? AND is_deleted = 0 ORDER BY date_created",
            (parent_task_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_total_task_count() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM tasks WHERE is_deleted = 0").fetchone()[0]


def search_tasks_sql(
    fts_query: str | None,
    list_id: str | None = None,
    status: str | None = None,
    priority: int | None = None,
    assignee: str | None = None,
    tag: str | None = None,
    due_from: str | None = None,
    due_to: str | None = None,
    include_closed: bool = True,
    limit: int = 50,
) -> list[dict]:
    with get_conn() as conn:
        params: list = []
        if fts_query:
            sql = """
                SELECT t.*, fts.rank AS _rank
                FROM tasks_fts fts
                JOIN tasks t ON t.rowid = fts.rowid
                WHERE tasks_fts MATCH ?
                  AND t.is_deleted = 0
            """
            params.append(fts_query)
        else:
            sql = "SELECT t.*, 0 AS _rank FROM tasks t WHERE t.is_deleted = 0"

        if list_id:
            sql += " AND t.list_id = ?"
            params.append(list_id)
        if status:
            sql += " AND lower(t.status) = lower(?)"
            params.append(status)
        if priority is not None:
            sql += " AND t.priority = ?"
            params.append(priority)
        if assignee:
            sql += " AND t.assignees LIKE ?"
            params.append(f"%{assignee}%")
        if tag:
            sql += " AND t.tags LIKE ?"
            params.append(f"%{tag}%")
        if due_from:
            sql += " AND t.due_date >= ?"
            params.append(due_from)
        if due_to:
            sql += " AND t.due_date <= ?"
            params.append(due_to)
        if not include_closed:
            sql += " AND t.is_closed = 0"

        sql += " ORDER BY _rank, t.date_updated DESC LIMIT ?"
        params.append(limit)

        return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ── Members ──────────────────────────────────────────────────────────────────

def upsert_member(conn: sqlite3.Connection, m: dict) -> None:
    conn.execute(
        """
        INSERT INTO members (id, username, email, color, profile_picture)
        VALUES (:id, :username, :email, :color, :profile_picture)
        ON CONFLICT(id) DO UPDATE SET
            username        = excluded.username,
            email           = excluded.email,
            color           = excluded.color,
            profile_picture = excluded.profile_picture
        """,
        m,
    )


def get_member(member_id: str | None = None, username: str | None = None, email: str | None = None) -> dict | None:
    with get_conn() as conn:
        if member_id:
            row = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
        elif email:
            row = conn.execute("SELECT * FROM members WHERE lower(email) = lower(?)", (email,)).fetchone()
        elif username:
            row = conn.execute("SELECT * FROM members WHERE lower(username) = lower(?)", (username,)).fetchone()
        else:
            return None
        return dict(row) if row else None


def get_all_members() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM members ORDER BY username")]


# ── Comments ─────────────────────────────────────────────────────────────────

def upsert_comment(conn: sqlite3.Connection, c: dict) -> None:
    conn.execute(
        """
        INSERT INTO comments (id, task_id, comment_text, user_id, date, resolved)
        VALUES (:id, :task_id, :comment_text, :user_id, :date, :resolved)
        ON CONFLICT(id) DO UPDATE SET
            comment_text = excluded.comment_text,
            user_id      = excluded.user_id,
            date         = excluded.date,
            resolved     = excluded.resolved
        """,
        c,
    )


def get_comments_for_task(task_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM comments WHERE task_id = ? ORDER BY date ASC", (task_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# ── Sync state ────────────────────────────────────────────────────────────────

def get_sync_state(key: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None


def set_sync_state(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)", (key, value)
        )
