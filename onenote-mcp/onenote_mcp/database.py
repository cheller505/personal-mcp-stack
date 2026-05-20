"""SQLite database layer with FTS5 full-text search for OneNote."""

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

logger = logging.getLogger(__name__)

DB_PATH = Path.home() / ".onenote-mcp" / "onenote.db"

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS notebooks (
    id                  TEXT PRIMARY KEY,
    display_name        TEXT NOT NULL,
    is_default          INTEGER DEFAULT 0,
    is_shared           INTEGER DEFAULT 0,
    user_role           TEXT,
    created_at          TEXT,
    modified_at         TEXT,
    created_by_name     TEXT,
    modified_by_name    TEXT,
    last_synced         TEXT
);

CREATE TABLE IF NOT EXISTS section_groups (
    id                          TEXT PRIMARY KEY,
    display_name                TEXT NOT NULL,
    parent_notebook_id          TEXT,
    parent_section_group_id     TEXT,
    last_synced                 TEXT
);

CREATE INDEX IF NOT EXISTS idx_sg_parent_nb ON section_groups(parent_notebook_id);
CREATE INDEX IF NOT EXISTS idx_sg_parent_sg ON section_groups(parent_section_group_id);

CREATE TABLE IF NOT EXISTS sections (
    id                          TEXT PRIMARY KEY,
    display_name                TEXT NOT NULL,
    parent_notebook_id          TEXT,
    parent_section_group_id     TEXT,
    created_at                  TEXT,
    modified_at                 TEXT,
    created_by_name             TEXT,
    modified_by_name            TEXT,
    fully_synced                INTEGER DEFAULT 0,
    last_synced                 TEXT
);

CREATE INDEX IF NOT EXISTS idx_sec_parent_nb ON sections(parent_notebook_id);

CREATE TABLE IF NOT EXISTS pages (
    id                  TEXT PRIMARY KEY,
    section_id          TEXT,
    notebook_id         TEXT,
    title               TEXT,
    level               INTEGER DEFAULT 0,
    page_order          INTEGER DEFAULT 0,
    content_url         TEXT,
    created_at          TEXT,
    modified_at         TEXT,
    created_by_name     TEXT,
    modified_by_name    TEXT,
    content_html        TEXT,
    content_text        TEXT,
    content_fetched_at  TEXT,
    is_deleted          INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_pages_section  ON pages(section_id);
CREATE INDEX IF NOT EXISTS idx_pages_notebook ON pages(notebook_id);
CREATE INDEX IF NOT EXISTS idx_pages_modified ON pages(modified_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
    title,
    content_text,
    content='pages',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
    INSERT INTO pages_fts(rowid, title, content_text)
    VALUES (new.rowid, new.title, new.content_text);
END;

CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
    INSERT INTO pages_fts(pages_fts, rowid, title, content_text)
    VALUES ('delete', old.rowid, old.title, old.content_text);
END;

CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages BEGIN
    INSERT INTO pages_fts(pages_fts, rowid, title, content_text)
    VALUES ('delete', old.rowid, old.title, old.content_text);
    INSERT INTO pages_fts(rowid, title, content_text)
    VALUES (new.rowid, new.title, new.content_text);
END;

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


# ── Notebooks ────────────────────────────────────────────────────────────────

def upsert_notebook(conn: sqlite3.Connection, nb: dict) -> None:
    conn.execute(
        """
        INSERT INTO notebooks (id, display_name, is_default, is_shared, user_role,
                               created_at, modified_at, created_by_name, modified_by_name,
                               last_synced)
        VALUES (:id, :display_name, :is_default, :is_shared, :user_role,
                :created_at, :modified_at, :created_by_name, :modified_by_name,
                datetime('now'))
        ON CONFLICT(id) DO UPDATE SET
            display_name        = excluded.display_name,
            is_default          = excluded.is_default,
            is_shared           = excluded.is_shared,
            user_role           = excluded.user_role,
            created_at          = excluded.created_at,
            modified_at         = excluded.modified_at,
            created_by_name     = excluded.created_by_name,
            modified_by_name    = excluded.modified_by_name,
            last_synced         = datetime('now')
        """,
        nb,
    )


def get_notebooks() -> list[dict]:
    with get_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM notebooks ORDER BY display_name"
            )
        ]


def get_notebook(notebook_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM notebooks WHERE id = ?", (notebook_id,)
        ).fetchone()
        return dict(row) if row else None


# ── Section groups ───────────────────────────────────────────────────────────

def upsert_section_group(conn: sqlite3.Connection, sg: dict) -> None:
    conn.execute(
        """
        INSERT INTO section_groups (id, display_name, parent_notebook_id,
                                    parent_section_group_id, last_synced)
        VALUES (:id, :display_name, :parent_notebook_id, :parent_section_group_id,
                datetime('now'))
        ON CONFLICT(id) DO UPDATE SET
            display_name                = excluded.display_name,
            parent_notebook_id          = excluded.parent_notebook_id,
            parent_section_group_id     = excluded.parent_section_group_id,
            last_synced                 = datetime('now')
        """,
        sg,
    )


def get_section_groups_in_notebook(notebook_id: str) -> list[dict]:
    with get_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM section_groups WHERE parent_notebook_id = ? "
                "ORDER BY display_name",
                (notebook_id,),
            )
        ]


def get_section_groups_in_group(group_id: str) -> list[dict]:
    with get_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM section_groups WHERE parent_section_group_id = ? "
                "ORDER BY display_name",
                (group_id,),
            )
        ]


# ── Sections ─────────────────────────────────────────────────────────────────

def upsert_section(conn: sqlite3.Connection, s: dict) -> None:
    conn.execute(
        """
        INSERT INTO sections (id, display_name, parent_notebook_id, parent_section_group_id,
                              created_at, modified_at, created_by_name, modified_by_name,
                              last_synced)
        VALUES (:id, :display_name, :parent_notebook_id, :parent_section_group_id,
                :created_at, :modified_at, :created_by_name, :modified_by_name,
                datetime('now'))
        ON CONFLICT(id) DO UPDATE SET
            display_name                = excluded.display_name,
            parent_notebook_id          = excluded.parent_notebook_id,
            parent_section_group_id     = excluded.parent_section_group_id,
            created_at                  = excluded.created_at,
            modified_at                 = excluded.modified_at,
            created_by_name             = excluded.created_by_name,
            modified_by_name            = excluded.modified_by_name,
            last_synced                 = datetime('now')
        """,
        s,
    )


def set_section_synced(conn: sqlite3.Connection, section_id: str) -> None:
    conn.execute(
        "UPDATE sections SET fully_synced = 1, last_synced = datetime('now') WHERE id = ?",
        (section_id,),
    )


def get_sections_in_notebook(notebook_id: str) -> list[dict]:
    with get_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM sections WHERE parent_notebook_id = ? AND "
                "parent_section_group_id IS NULL ORDER BY display_name",
                (notebook_id,),
            )
        ]


def get_sections_in_group(group_id: str) -> list[dict]:
    with get_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM sections WHERE parent_section_group_id = ? "
                "ORDER BY display_name",
                (group_id,),
            )
        ]


def get_all_sections() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM sections")]


def get_unsynced_sections() -> list[dict]:
    with get_conn() as conn:
        return [
            dict(r)
            for r in conn.execute("SELECT * FROM sections WHERE fully_synced = 0")
        ]


def get_section(section_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sections WHERE id = ?", (section_id,)
        ).fetchone()
        return dict(row) if row else None


# ── Pages ────────────────────────────────────────────────────────────────────

def upsert_page(conn: sqlite3.Connection, p: dict) -> None:
    """Upsert a page WITHOUT touching content_html/content_text/content_fetched_at."""
    conn.execute(
        """
        INSERT INTO pages (id, section_id, notebook_id, title, level, page_order,
                           content_url, created_at, modified_at,
                           created_by_name, modified_by_name)
        VALUES (:id, :section_id, :notebook_id, :title, :level, :page_order,
                :content_url, :created_at, :modified_at,
                :created_by_name, :modified_by_name)
        ON CONFLICT(id) DO UPDATE SET
            section_id          = excluded.section_id,
            notebook_id         = excluded.notebook_id,
            title               = excluded.title,
            level               = excluded.level,
            page_order          = excluded.page_order,
            content_url         = excluded.content_url,
            created_at          = excluded.created_at,
            modified_at         = excluded.modified_at,
            created_by_name     = excluded.created_by_name,
            modified_by_name    = excluded.modified_by_name
        """,
        p,
    )


def update_page_content(
    conn: sqlite3.Connection,
    page_id: str,
    html: str,
    text: str,
) -> None:
    conn.execute(
        """
        UPDATE pages
        SET content_html = ?, content_text = ?, content_fetched_at = datetime('now')
        WHERE id = ?
        """,
        (html, text, page_id),
    )


def get_page(page_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
        return dict(row) if row else None


def get_pages_in_section(
    section_id: str, limit: int = 50, offset: int = 0
) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, section_id, notebook_id, title, modified_at, created_at,
                   level, page_order,
                   substr(content_text, 1, 200) AS snippet,
                   (content_text IS NOT NULL) AS has_content
            FROM pages
            WHERE section_id = ? AND is_deleted = 0
            ORDER BY modified_at DESC
            LIMIT ? OFFSET ?
            """,
            (section_id, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


def get_recent_pages(
    limit: int = 25, notebook_id: str | None = None
) -> list[dict]:
    with get_conn() as conn:
        if notebook_id:
            rows = conn.execute(
                """
                SELECT id, section_id, notebook_id, title, modified_at, created_at,
                       substr(content_text, 1, 200) AS snippet,
                       (content_text IS NOT NULL) AS has_content
                FROM pages
                WHERE is_deleted = 0 AND notebook_id = ?
                ORDER BY modified_at DESC
                LIMIT ?
                """,
                (notebook_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, section_id, notebook_id, title, modified_at, created_at,
                       substr(content_text, 1, 200) AS snippet,
                       (content_text IS NOT NULL) AS has_content
                FROM pages
                WHERE is_deleted = 0
                ORDER BY modified_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def search_pages(
    query: str,
    notebook_id: str | None = None,
    section_id: str | None = None,
    is_shared: bool | None = None,
    modified_after: str | None = None,
    modified_before: str | None = None,
    limit: int = 50,
) -> list[dict]:
    with get_conn() as conn:
        params: list = [query]
        sql = """
            SELECT p.id, p.section_id, p.notebook_id, p.title, p.modified_at,
                   snippet(pages_fts, 1, '[', ']', '...', 16) AS snippet,
                   fts.rank
            FROM pages_fts fts
            JOIN pages p ON p.rowid = fts.rowid
            LEFT JOIN notebooks n ON n.id = p.notebook_id
            WHERE pages_fts MATCH ? AND p.is_deleted = 0
        """
        if notebook_id:
            sql += " AND p.notebook_id = ?"
            params.append(notebook_id)
        if section_id:
            sql += " AND p.section_id = ?"
            params.append(section_id)
        if is_shared is True:
            sql += " AND n.is_shared = 1"
        elif is_shared is False:
            sql += " AND n.is_shared = 0"
        if modified_after:
            sql += " AND p.modified_at >= ?"
            params.append(modified_after)
        if modified_before:
            sql += " AND p.modified_at <= ?"
            params.append(modified_before)
        sql += " ORDER BY fts.rank, p.modified_at DESC LIMIT ?"
        params.append(limit)

        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def mark_page_deleted(conn: sqlite3.Connection, page_id: str) -> None:
    conn.execute("UPDATE pages SET is_deleted = 1 WHERE id = ?", (page_id,))


def get_total_counts() -> dict:
    with get_conn() as conn:
        nb = conn.execute("SELECT COUNT(*) FROM notebooks").fetchone()[0]
        sec = conn.execute("SELECT COUNT(*) FROM sections").fetchone()[0]
        pg = conn.execute("SELECT COUNT(*) FROM pages WHERE is_deleted = 0").fetchone()[0]
        pg_cached = conn.execute(
            "SELECT COUNT(*) FROM pages WHERE content_text IS NOT NULL AND is_deleted = 0"
        ).fetchone()[0]
        return {
            "notebooks": nb,
            "sections": sec,
            "pages": pg,
            "pages_with_content": pg_cached,
        }


# ── Sync state ────────────────────────────────────────────────────────────────

def get_sync_state(key: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM sync_state WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None


def set_sync_state(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)",
            (key, value),
        )
