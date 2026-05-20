"""SQLite database layer with FTS5 full-text search over Slack messages."""

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

logger = logging.getLogger(__name__)

DB_PATH = Path.home() / ".slack-mcp" / "slack.db"

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS workspaces (
    id          TEXT PRIMARY KEY,
    name        TEXT,
    domain      TEXT,
    url         TEXT,
    synced_at   TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    name            TEXT,
    real_name       TEXT,
    display_name    TEXT,
    email           TEXT,
    is_bot          INTEGER DEFAULT 0,
    is_deleted      INTEGER DEFAULT 0,
    image_url       TEXT,
    tz              TEXT,
    updated         INTEGER,
    profile_title   TEXT
);

CREATE INDEX IF NOT EXISTS idx_users_name        ON users(name);
CREATE INDEX IF NOT EXISTS idx_users_real_name   ON users(real_name);
CREATE INDEX IF NOT EXISTS idx_users_email       ON users(email);

CREATE TABLE IF NOT EXISTS conversations (
    id                  TEXT PRIMARY KEY,
    name                TEXT,
    type                TEXT,
    created             INTEGER,
    is_archived         INTEGER DEFAULT 0,
    is_member           INTEGER DEFAULT 0,
    num_members         INTEGER DEFAULT 0,
    topic               TEXT,
    purpose             TEXT,
    user_id             TEXT,
    fully_synced        INTEGER DEFAULT 0,
    oldest_synced_ts    TEXT,
    latest_synced_ts    TEXT,
    last_sync_time      TEXT
);

CREATE INDEX IF NOT EXISTS idx_conv_type   ON conversations(type);
CREATE INDEX IF NOT EXISTS idx_conv_member ON conversations(is_member);

CREATE TABLE IF NOT EXISTS messages (
    channel_id      TEXT NOT NULL,
    ts              TEXT NOT NULL,
    user_id         TEXT,
    text            TEXT,
    thread_ts       TEXT,
    reply_count     INTEGER DEFAULT 0,
    subtype         TEXT,
    edited_ts       TEXT,
    has_files       INTEGER DEFAULT 0,
    files_json      TEXT DEFAULT '[]',
    reactions_json  TEXT DEFAULT '[]',
    blocks_json     TEXT,
    raw_json        TEXT,
    PRIMARY KEY (channel_id, ts)
);

CREATE INDEX IF NOT EXISTS idx_msg_thread ON messages(thread_ts);
CREATE INDEX IF NOT EXISTS idx_msg_user   ON messages(user_id);
CREATE INDEX IF NOT EXISTS idx_msg_ts     ON messages(ts DESC);

-- FTS5 contentless table — body spans messages + users + conversations,
-- so we maintain it manually rather than via triggers/content=.
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text,
    channel_name,
    user_name,
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS messages_fts_rowid (
    rowid       INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id  TEXT NOT NULL,
    ts          TEXT NOT NULL,
    UNIQUE (channel_id, ts)
);

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


# ── Workspaces ────────────────────────────────────────────────────────────────

def upsert_workspace(conn: sqlite3.Connection, ws: dict) -> None:
    conn.execute(
        """
        INSERT INTO workspaces (id, name, domain, url, synced_at)
        VALUES (:id, :name, :domain, :url, datetime('now'))
        ON CONFLICT(id) DO UPDATE SET
            name      = excluded.name,
            domain    = excluded.domain,
            url       = excluded.url,
            synced_at = excluded.synced_at
        """,
        ws,
    )


def get_workspace() -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM workspaces LIMIT 1").fetchone()
        return dict(row) if row else None


# ── Users ─────────────────────────────────────────────────────────────────────

def upsert_user(conn: sqlite3.Connection, u: dict) -> None:
    conn.execute(
        """
        INSERT INTO users (id, name, real_name, display_name, email, is_bot,
                           is_deleted, image_url, tz, updated, profile_title)
        VALUES (:id, :name, :real_name, :display_name, :email, :is_bot,
                :is_deleted, :image_url, :tz, :updated, :profile_title)
        ON CONFLICT(id) DO UPDATE SET
            name          = excluded.name,
            real_name     = excluded.real_name,
            display_name  = excluded.display_name,
            email         = excluded.email,
            is_bot        = excluded.is_bot,
            is_deleted    = excluded.is_deleted,
            image_url     = excluded.image_url,
            tz            = excluded.tz,
            updated       = excluded.updated,
            profile_title = excluded.profile_title
        """,
        u,
    )


def get_user(user_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def get_users(include_deleted: bool = False, include_bots: bool = False,
              name_contains: str | None = None) -> list[dict]:
    with get_conn() as conn:
        sql = "SELECT * FROM users WHERE 1=1"
        params: list = []
        if not include_deleted:
            sql += " AND is_deleted = 0"
        if not include_bots:
            sql += " AND is_bot = 0"
        if name_contains:
            sql += (" AND (lower(name) LIKE ? OR lower(real_name) LIKE ? "
                    "OR lower(display_name) LIKE ? OR lower(email) LIKE ?)")
            like = f"%{name_contains.lower()}%"
            params += [like, like, like, like]
        sql += " ORDER BY real_name, name"
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def find_user(query: str) -> dict | None:
    """Lookup by ID, login, real_name, display_name or email substring."""
    q = query.strip()
    if not q:
        return None
    with get_conn() as conn:
        # Exact ID
        row = conn.execute("SELECT * FROM users WHERE id = ?", (q,)).fetchone()
        if row:
            return dict(row)
        # Exact login / display
        row = conn.execute(
            "SELECT * FROM users WHERE lower(name) = lower(?) OR lower(display_name) = lower(?)",
            (q, q),
        ).fetchone()
        if row:
            return dict(row)
        # Email exact
        row = conn.execute(
            "SELECT * FROM users WHERE lower(email) = lower(?)", (q,)
        ).fetchone()
        if row:
            return dict(row)
        # Substring fallback
        like = f"%{q.lower()}%"
        row = conn.execute(
            """
            SELECT * FROM users
            WHERE lower(name) LIKE ? OR lower(real_name) LIKE ?
               OR lower(display_name) LIKE ? OR lower(email) LIKE ?
            ORDER BY is_deleted, real_name
            LIMIT 1
            """,
            (like, like, like, like),
        ).fetchone()
        return dict(row) if row else None


def get_users_map() -> dict[str, dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM users").fetchall()
        return {r["id"]: dict(r) for r in rows}


# ── Conversations ─────────────────────────────────────────────────────────────

def upsert_conversation(conn: sqlite3.Connection, c: dict) -> None:
    conn.execute(
        """
        INSERT INTO conversations (id, name, type, created, is_archived, is_member,
                                   num_members, topic, purpose, user_id)
        VALUES (:id, :name, :type, :created, :is_archived, :is_member,
                :num_members, :topic, :purpose, :user_id)
        ON CONFLICT(id) DO UPDATE SET
            name        = excluded.name,
            type        = excluded.type,
            created     = excluded.created,
            is_archived = excluded.is_archived,
            is_member   = excluded.is_member,
            num_members = excluded.num_members,
            topic       = excluded.topic,
            purpose     = excluded.purpose,
            user_id     = excluded.user_id
        """,
        c,
    )


def update_conversation_sync(conn: sqlite3.Connection, channel_id: str,
                             oldest_synced_ts: str | None = None,
                             latest_synced_ts: str | None = None,
                             fully_synced: int | None = None) -> None:
    sets = ["last_sync_time = datetime('now')"]
    params: list = []
    if oldest_synced_ts is not None:
        sets.append("oldest_synced_ts = ?")
        params.append(oldest_synced_ts)
    if latest_synced_ts is not None:
        sets.append("latest_synced_ts = ?")
        params.append(latest_synced_ts)
    if fully_synced is not None:
        sets.append("fully_synced = ?")
        params.append(fully_synced)
    params.append(channel_id)
    conn.execute(f"UPDATE conversations SET {', '.join(sets)} WHERE id = ?", params)


def get_conversation(channel_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (channel_id,)
        ).fetchone()
        return dict(row) if row else None


def get_conversations(*, type: str | None = None, is_archived: bool | None = None,
                      member_only: bool = True,
                      name_contains: str | None = None) -> list[dict]:
    with get_conn() as conn:
        sql = "SELECT * FROM conversations WHERE 1=1"
        params: list = []
        if type and type != "all":
            sql += " AND type = ?"
            params.append(type)
        if is_archived is not None:
            sql += " AND is_archived = ?"
            params.append(1 if is_archived else 0)
        if member_only:
            sql += " AND is_member = 1"
        if name_contains:
            sql += " AND lower(name) LIKE ?"
            params.append(f"%{name_contains.lower()}%")
        sql += " ORDER BY type, name"
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_all_conversations() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM conversations")]


def get_unsynced_conversations() -> list[dict]:
    with get_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM conversations WHERE fully_synced = 0 AND is_member = 1"
            )
        ]


def find_im_channel(user_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM conversations WHERE type = 'im' AND user_id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


# ── Messages + FTS ────────────────────────────────────────────────────────────

def _fts_rowid_for(conn: sqlite3.Connection, channel_id: str, ts: str) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO messages_fts_rowid (channel_id, ts) VALUES (?, ?)",
        (channel_id, ts),
    )
    row = conn.execute(
        "SELECT rowid FROM messages_fts_rowid WHERE channel_id = ? AND ts = ?",
        (channel_id, ts),
    ).fetchone()
    return int(row["rowid"])


def upsert_message(conn: sqlite3.Connection, msg: dict,
                   channel_name: str | None = None,
                   user_name: str | None = None) -> None:
    conn.execute(
        """
        INSERT INTO messages (channel_id, ts, user_id, text, thread_ts, reply_count,
                              subtype, edited_ts, has_files, files_json, reactions_json,
                              blocks_json, raw_json)
        VALUES (:channel_id, :ts, :user_id, :text, :thread_ts, :reply_count,
                :subtype, :edited_ts, :has_files, :files_json, :reactions_json,
                :blocks_json, :raw_json)
        ON CONFLICT(channel_id, ts) DO UPDATE SET
            user_id        = excluded.user_id,
            text           = excluded.text,
            thread_ts      = excluded.thread_ts,
            reply_count    = excluded.reply_count,
            subtype        = excluded.subtype,
            edited_ts      = excluded.edited_ts,
            has_files      = excluded.has_files,
            files_json     = excluded.files_json,
            reactions_json = excluded.reactions_json,
            blocks_json    = excluded.blocks_json,
            raw_json       = excluded.raw_json
        """,
        msg,
    )

    rowid = _fts_rowid_for(conn, msg["channel_id"], msg["ts"])
    # Remove any prior FTS row at this rowid, then insert.
    conn.execute("DELETE FROM messages_fts WHERE rowid = ?", (rowid,))
    conn.execute(
        "INSERT INTO messages_fts (rowid, text, channel_name, user_name) VALUES (?, ?, ?, ?)",
        (rowid, msg.get("text") or "", channel_name or "", user_name or ""),
    )


def reindex_channel_fts(channel_id: str, channel_name: str,
                        users_map: dict[str, dict]) -> int:
    """Re-write FTS rows for every message in a channel. Used when channel
    or user names change so the denormalized fields stay current."""
    count = 0
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT ts, user_id, text FROM messages WHERE channel_id = ?",
            (channel_id,),
        ).fetchall()
        for r in rows:
            rowid = _fts_rowid_for(conn, channel_id, r["ts"])
            u = users_map.get(r["user_id"] or "") or {}
            user_name = u.get("display_name") or u.get("real_name") or u.get("name") or ""
            conn.execute("DELETE FROM messages_fts WHERE rowid = ?", (rowid,))
            conn.execute(
                "INSERT INTO messages_fts (rowid, text, channel_name, user_name) VALUES (?, ?, ?, ?)",
                (rowid, r["text"] or "", channel_name, user_name),
            )
            count += 1
    return count


def get_message(channel_id: str, ts: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM messages WHERE channel_id = ? AND ts = ?",
            (channel_id, ts),
        ).fetchone()
        return dict(row) if row else None


def get_channel_messages(channel_id: str, limit: int = 50,
                         before_ts: str | None = None,
                         after_ts: str | None = None) -> list[dict]:
    with get_conn() as conn:
        sql = "SELECT * FROM messages WHERE channel_id = ?"
        params: list = [channel_id]
        if before_ts:
            sql += " AND ts < ?"
            params.append(before_ts)
        if after_ts:
            sql += " AND ts > ?"
            params.append(after_ts)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_thread_messages(channel_id: str, thread_ts: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM messages
            WHERE channel_id = ? AND (ts = ? OR thread_ts = ?)
            ORDER BY ts ASC
            """,
            (channel_id, thread_ts, thread_ts),
        ).fetchall()
        return [dict(r) for r in rows]


def get_messages_by_user(user_id: str, limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE user_id = ? ORDER BY ts DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_total_message_count() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]


def get_fully_synced_count() -> int:
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM conversations WHERE fully_synced = 1"
        ).fetchone()[0]


def search_messages(
    query: str,
    channel_id: str | None = None,
    user_id: str | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
    channel_type: str | None = None,
    is_thread: bool | None = None,
    limit: int = 50,
) -> list[dict]:
    with get_conn() as conn:
        sql = """
            SELECT m.*, r.channel_id AS _cid, r.ts AS _ts,
                   snippet(messages_fts, 0, '[', ']', '...', 12) AS snippet,
                   fts.rank AS _rank
            FROM messages_fts fts
            JOIN messages_fts_rowid r ON r.rowid = fts.rowid
            JOIN messages m ON m.channel_id = r.channel_id AND m.ts = r.ts
            LEFT JOIN conversations c ON c.id = m.channel_id
            WHERE messages_fts MATCH ?
        """
        params: list = [query]
        if channel_id:
            sql += " AND m.channel_id = ?"
            params.append(channel_id)
        if user_id:
            sql += " AND m.user_id = ?"
            params.append(user_id)
        if from_ts:
            sql += " AND m.ts >= ?"
            params.append(from_ts)
        if to_ts:
            sql += " AND m.ts <= ?"
            params.append(to_ts)
        if channel_type:
            sql += " AND c.type = ?"
            params.append(channel_type)
        if is_thread is True:
            sql += " AND m.thread_ts IS NOT NULL AND m.thread_ts != m.ts"
        elif is_thread is False:
            sql += " AND (m.thread_ts IS NULL OR m.thread_ts = m.ts)"
        sql += " ORDER BY _rank, m.ts DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


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
