"""SQLite database layer with FTS5 full-text search."""

import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

logger = logging.getLogger(__name__)

DB_PATH = Path.home() / ".email-mcp" / "mail.db"

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS folders (
    id                  TEXT PRIMARY KEY,
    display_name        TEXT NOT NULL,
    parent_folder_id    TEXT,
    child_folder_count  INTEGER DEFAULT 0,
    unread_item_count   INTEGER DEFAULT 0,
    total_item_count    INTEGER DEFAULT 0,
    delta_token         TEXT,
    fully_synced        INTEGER DEFAULT 0,
    last_sync_time      TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id                  TEXT PRIMARY KEY,
    conversation_id     TEXT,
    folder_id           TEXT NOT NULL,
    sender_name         TEXT,
    sender_email        TEXT,
    to_recipients       TEXT DEFAULT '[]',
    cc_recipients       TEXT DEFAULT '[]',
    subject             TEXT,
    body_html           TEXT,
    body_text           TEXT,
    received_datetime   TEXT,
    is_read             INTEGER DEFAULT 0,
    has_attachments     INTEGER DEFAULT 0,
    attachment_names    TEXT DEFAULT '[]',
    importance          TEXT DEFAULT 'normal',
    is_draft            INTEGER DEFAULT 0,
    created_at          TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_messages_folder   ON messages(folder_id);
CREATE INDEX IF NOT EXISTS idx_messages_thread   ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_messages_received ON messages(received_datetime DESC);
CREATE INDEX IF NOT EXISTS idx_messages_sender   ON messages(sender_email);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    subject,
    body_text,
    sender_name,
    sender_email,
    content='messages',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, subject, body_text, sender_name, sender_email)
    VALUES (new.rowid, new.subject, new.body_text, new.sender_name, new.sender_email);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, subject, body_text, sender_name, sender_email)
    VALUES ('delete', old.rowid, old.subject, old.body_text, old.sender_name, old.sender_email);
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, subject, body_text, sender_name, sender_email)
    VALUES ('delete', old.rowid, old.subject, old.body_text, old.sender_name, old.sender_email);
    INSERT INTO messages_fts(rowid, subject, body_text, sender_name, sender_email)
    VALUES (new.rowid, new.subject, new.body_text, new.sender_name, new.sender_email);
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


# ── Folders ──────────────────────────────────────────────────────────────────

def upsert_folder(conn: sqlite3.Connection, folder: dict) -> None:
    conn.execute(
        """
        INSERT INTO folders (id, display_name, parent_folder_id, child_folder_count,
                             unread_item_count, total_item_count)
        VALUES (:id, :display_name, :parent_folder_id, :child_folder_count,
                :unread_item_count, :total_item_count)
        ON CONFLICT(id) DO UPDATE SET
            display_name        = excluded.display_name,
            parent_folder_id    = excluded.parent_folder_id,
            child_folder_count  = excluded.child_folder_count,
            unread_item_count   = excluded.unread_item_count,
            total_item_count    = excluded.total_item_count
        """,
        folder,
    )


def set_folder_synced(conn: sqlite3.Connection, folder_id: str, delta_token: str | None) -> None:
    conn.execute(
        """
        UPDATE folders
        SET fully_synced = 1, last_sync_time = datetime('now'),
            delta_token = COALESCE(?, delta_token)
        WHERE id = ?
        """,
        (delta_token, folder_id),
    )


def update_folder_delta_token(conn: sqlite3.Connection, folder_id: str, delta_token: str) -> None:
    conn.execute(
        "UPDATE folders SET delta_token = ?, last_sync_time = datetime('now') WHERE id = ?",
        (delta_token, folder_id),
    )


def get_folders() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM folders ORDER BY display_name")]


def get_unsynced_folders() -> list[dict]:
    with get_conn() as conn:
        return [
            dict(r)
            for r in conn.execute("SELECT * FROM folders WHERE fully_synced = 0")
        ]


def get_all_folders_with_delta() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM folders")]


# ── Messages ──────────────────────────────────────────────────────────────────

def upsert_message(conn: sqlite3.Connection, msg: dict) -> None:
    conn.execute(
        """
        INSERT INTO messages (id, conversation_id, folder_id, sender_name, sender_email,
                              to_recipients, cc_recipients, subject, body_html, body_text,
                              received_datetime, is_read, has_attachments, attachment_names,
                              importance, is_draft)
        VALUES (:id, :conversation_id, :folder_id, :sender_name, :sender_email,
                :to_recipients, :cc_recipients, :subject, :body_html, :body_text,
                :received_datetime, :is_read, :has_attachments, :attachment_names,
                :importance, :is_draft)
        ON CONFLICT(id) DO UPDATE SET
            conversation_id  = excluded.conversation_id,
            folder_id        = excluded.folder_id,
            sender_name      = excluded.sender_name,
            sender_email     = excluded.sender_email,
            to_recipients    = excluded.to_recipients,
            cc_recipients    = excluded.cc_recipients,
            subject          = excluded.subject,
            body_html        = excluded.body_html,
            body_text        = excluded.body_text,
            received_datetime= excluded.received_datetime,
            is_read          = excluded.is_read,
            has_attachments  = excluded.has_attachments,
            attachment_names = excluded.attachment_names,
            importance       = excluded.importance,
            is_draft         = excluded.is_draft
        """,
        msg,
    )


def delete_message(conn: sqlite3.Connection, message_id: str) -> None:
    conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))


def get_message_by_id(message_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return dict(row) if row else None


def get_messages_by_folder(folder_id: str, limit: int = 50, offset: int = 0) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, conversation_id, folder_id, sender_name, sender_email,
                   to_recipients, subject, received_datetime, is_read, has_attachments,
                   substr(body_text, 1, 200) AS snippet
            FROM messages
            WHERE folder_id = ?
            ORDER BY received_datetime DESC
            LIMIT ? OFFSET ?
            """,
            (folder_id, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


def get_thread(conversation_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY received_datetime ASC",
            (conversation_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def search_messages(
    query: str,
    folder_id: str | None = None,
    sender: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    is_read: bool | None = None,
    limit: int = 50,
) -> list[dict]:
    with get_conn() as conn:
        params: list = [query]
        sql = """
            SELECT m.id, m.conversation_id, m.folder_id, m.sender_name, m.sender_email,
                   m.to_recipients, m.subject, m.received_datetime, m.is_read, m.has_attachments,
                   substr(m.body_text, 1, 200) AS snippet,
                   fts.rank
            FROM messages_fts fts
            JOIN messages m ON m.rowid = fts.rowid
            WHERE messages_fts MATCH ?
        """
        if folder_id:
            sql += " AND m.folder_id = ?"
            params.append(folder_id)
        if sender:
            sql += " AND (m.sender_email LIKE ? OR m.sender_name LIKE ?)"
            params += [f"%{sender}%", f"%{sender}%"]
        if date_from:
            sql += " AND m.received_datetime >= ?"
            params.append(date_from)
        if date_to:
            sql += " AND m.received_datetime <= ?"
            params.append(date_to)
        if is_read is not None:
            sql += " AND m.is_read = ?"
            params.append(1 if is_read else 0)
        sql += " ORDER BY fts.rank, m.received_datetime DESC LIMIT ?"
        params.append(limit)

        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_total_message_count() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]


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
