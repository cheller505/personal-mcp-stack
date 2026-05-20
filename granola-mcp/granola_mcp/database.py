"""SQLite database layer with FTS5 full-text search over Granola notes + transcripts."""

import json
import logging
import sqlite3
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

logger = logging.getLogger(__name__)

DB_PATH = Path.home() / ".granola-mcp" / "granola.db"

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS folders (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    parent_folder_id  TEXT
);
CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent_folder_id);

CREATE TABLE IF NOT EXISTS notes (
    note_id                TEXT PRIMARY KEY,
    title                  TEXT,
    owner_name             TEXT,
    owner_email            TEXT,
    created_at             TEXT,
    updated_at             TEXT,
    summary_text           TEXT,
    summary_markdown       TEXT,
    calendar_event_title   TEXT,
    calendar_event_id      TEXT,
    scheduled_start_time   TEXT,
    scheduled_end_time     TEXT,
    organiser_email        TEXT,
    attendees              TEXT DEFAULT '[]',
    folder_ids             TEXT DEFAULT '[]',
    transcript_cached      INTEGER DEFAULT 0,
    is_pending             INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_notes_updated ON notes(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_notes_created ON notes(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notes_pending ON notes(is_pending);

CREATE TABLE IF NOT EXISTS transcripts (
    note_id           TEXT PRIMARY KEY,
    transcript_zlib   BLOB,
    transcript_text   TEXT,
    fetched_at        TEXT
);

CREATE TABLE IF NOT EXISTS notes_fts_rowid (
    note_id   TEXT PRIMARY KEY,
    rowid     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_fts_rowid ON notes_fts_rowid(rowid);

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    title,
    summary_text,
    transcript_text,
    attendees_text,
    tokenize='unicode61 remove_diacritics 2'
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


# ── Folders ──────────────────────────────────────────────────────────────────

def upsert_folder(conn: sqlite3.Connection, folder: dict) -> None:
    conn.execute(
        """
        INSERT INTO folders (id, name, parent_folder_id)
        VALUES (:id, :name, :parent_folder_id)
        ON CONFLICT(id) DO UPDATE SET
            name             = excluded.name,
            parent_folder_id = excluded.parent_folder_id
        """,
        folder,
    )


def get_folders() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM folders ORDER BY name")]


def get_folder(folder_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
        return dict(row) if row else None


def find_folders_by_name(substring: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM folders WHERE lower(name) LIKE lower(?) ORDER BY name",
            (f"%{substring}%",),
        ).fetchall()
        return [dict(r) for r in rows]


# ── Notes ────────────────────────────────────────────────────────────────────

def upsert_note(conn: sqlite3.Connection, note: dict) -> None:
    conn.execute(
        """
        INSERT INTO notes (
            note_id, title, owner_name, owner_email, created_at, updated_at,
            summary_text, summary_markdown, calendar_event_title, calendar_event_id,
            scheduled_start_time, scheduled_end_time, organiser_email,
            attendees, folder_ids, transcript_cached, is_pending
        ) VALUES (
            :note_id, :title, :owner_name, :owner_email, :created_at, :updated_at,
            :summary_text, :summary_markdown, :calendar_event_title, :calendar_event_id,
            :scheduled_start_time, :scheduled_end_time, :organiser_email,
            :attendees, :folder_ids, :transcript_cached, :is_pending
        )
        ON CONFLICT(note_id) DO UPDATE SET
            title                = excluded.title,
            owner_name           = excluded.owner_name,
            owner_email          = excluded.owner_email,
            created_at           = excluded.created_at,
            updated_at           = excluded.updated_at,
            summary_text         = excluded.summary_text,
            summary_markdown     = excluded.summary_markdown,
            calendar_event_title = excluded.calendar_event_title,
            calendar_event_id    = excluded.calendar_event_id,
            scheduled_start_time = excluded.scheduled_start_time,
            scheduled_end_time   = excluded.scheduled_end_time,
            organiser_email      = excluded.organiser_email,
            attendees            = excluded.attendees,
            folder_ids           = excluded.folder_ids,
            is_pending           = excluded.is_pending
        """,
        note,
    )


def set_note_pending(conn: sqlite3.Connection, note_id: str, pending: bool) -> None:
    conn.execute(
        "UPDATE notes SET is_pending = ? WHERE note_id = ?",
        (1 if pending else 0, note_id),
    )


def set_transcript_cached(conn: sqlite3.Connection, note_id: str) -> None:
    conn.execute(
        "UPDATE notes SET transcript_cached = 1 WHERE note_id = ?",
        (note_id,),
    )


def get_note(note_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM notes WHERE note_id = ?", (note_id,)).fetchone()
        return dict(row) if row else None


def get_max_updated_at() -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT MAX(updated_at) FROM notes").fetchone()
        return row[0] if row and row[0] else None


def get_total_note_count() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM notes WHERE is_pending = 0").fetchone()[0]


def get_cached_transcript_count() -> int:
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM notes WHERE transcript_cached = 1"
        ).fetchone()[0]


def get_pending_count() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM notes WHERE is_pending = 1").fetchone()[0]


def get_pending_note_ids() -> list[str]:
    with get_conn() as conn:
        rows = conn.execute("SELECT note_id FROM notes WHERE is_pending = 1").fetchall()
        return [r[0] for r in rows]


def list_notes(
    folder_id: str | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    attendee: str | None = None,
    title_contains: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    with get_conn() as conn:
        sql = "SELECT * FROM notes WHERE is_pending = 0"
        params: list = []
        if folder_id:
            sql += " AND folder_ids LIKE ?"
            params.append(f"%\"{folder_id}\"%")
        if created_after:
            sql += " AND created_at >= ?"
            params.append(created_after)
        if created_before:
            sql += " AND created_at <= ?"
            params.append(created_before)
        if attendee:
            sql += " AND lower(attendees) LIKE lower(?)"
            params.append(f"%{attendee}%")
        if title_contains:
            sql += " AND lower(title) LIKE lower(?)"
            params.append(f"%{title_contains}%")
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def count_notes_in_folder(folder_id: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM notes WHERE is_pending = 0 AND folder_ids LIKE ?",
            (f"%\"{folder_id}\"%",),
        ).fetchone()
        return row[0] if row else 0


# ── Transcripts ──────────────────────────────────────────────────────────────

def store_transcript(conn: sqlite3.Connection, note_id: str, transcript: list[dict]) -> str:
    """Stores compressed transcript JSON + plain text. Returns the flat text."""
    text = _transcript_to_text(transcript)
    blob = zlib.compress(json.dumps(transcript).encode("utf-8"))
    conn.execute(
        """
        INSERT INTO transcripts (note_id, transcript_zlib, transcript_text, fetched_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(note_id) DO UPDATE SET
            transcript_zlib = excluded.transcript_zlib,
            transcript_text = excluded.transcript_text,
            fetched_at      = excluded.fetched_at
        """,
        (note_id, blob, text),
    )
    return text


def _transcript_to_text(transcript: list[dict]) -> str:
    parts = []
    for seg in transcript or []:
        text = (seg.get("text") or "").strip()
        if text:
            parts.append(text)
    return " ".join(parts)


def get_transcript(note_id: str) -> list[dict] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT transcript_zlib FROM transcripts WHERE note_id = ?", (note_id,)
        ).fetchone()
        if not row or not row[0]:
            return None
        try:
            return json.loads(zlib.decompress(row[0]).decode("utf-8"))
        except Exception:
            return None


def get_transcript_text(note_id: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT transcript_text FROM transcripts WHERE note_id = ?", (note_id,)
        ).fetchone()
        return row[0] if row else None


# ── FTS5 (contentless) management ────────────────────────────────────────────

def _attendees_text(attendees_json: str | None) -> str:
    try:
        atts = json.loads(attendees_json or "[]")
    except Exception:
        return ""
    parts: list[str] = []
    for a in atts:
        if isinstance(a, dict):
            for k in ("name", "displayName", "email"):
                v = a.get(k)
                if v:
                    parts.append(str(v))
        elif isinstance(a, str):
            parts.append(a)
    return " ".join(parts)


def _get_or_assign_fts_rowid(conn: sqlite3.Connection, note_id: str) -> int:
    row = conn.execute(
        "SELECT rowid FROM notes_fts_rowid WHERE note_id = ?", (note_id,)
    ).fetchone()
    if row:
        return row[0]
    row = conn.execute("SELECT COALESCE(MAX(rowid), 0) + 1 FROM notes_fts_rowid").fetchone()
    new_rowid = row[0] if row else 1
    conn.execute(
        "INSERT INTO notes_fts_rowid (note_id, rowid) VALUES (?, ?)",
        (note_id, new_rowid),
    )
    return new_rowid


def _delete_fts_row(conn: sqlite3.Connection, rowid: int, old: dict | None) -> None:
    """Issue a delete to the contentless FTS5 table. Need the old column values."""
    if old is None:
        old = {"title": "", "summary_text": "", "transcript_text": "", "attendees_text": ""}
    conn.execute(
        "INSERT INTO notes_fts(notes_fts, rowid, title, summary_text, transcript_text, attendees_text) "
        "VALUES ('delete', ?, ?, ?, ?, ?)",
        (rowid, old.get("title", ""), old.get("summary_text", ""),
         old.get("transcript_text", ""), old.get("attendees_text", "")),
    )


def _fts_row_state(conn: sqlite3.Connection, note_id: str) -> dict:
    """Pull a row's current FTS values out of notes + transcripts (best-effort)."""
    n = conn.execute(
        "SELECT title, summary_text, attendees FROM notes WHERE note_id = ?",
        (note_id,),
    ).fetchone()
    t = conn.execute(
        "SELECT transcript_text FROM transcripts WHERE note_id = ?", (note_id,)
    ).fetchone()
    return {
        "title": (n["title"] if n else "") or "",
        "summary_text": (n["summary_text"] if n else "") or "",
        "transcript_text": (t["transcript_text"] if t else "") or "",
        "attendees_text": _attendees_text(n["attendees"]) if n else "",
    }


def index_note_for_fts(conn: sqlite3.Connection, note: dict) -> None:
    """Insert/replace FTS row for a note (no transcript text yet)."""
    note_id = note["note_id"]
    # Capture old state BEFORE upsert overwrites notes row (assumed already done by caller).
    # Approach: we may already have a rowid; if so we issue a delete using current FTS state
    # which we reconstruct from the now-current notes row (safe for contentless tables — the
    # FTS index will be rebuilt by the insert).
    existing = conn.execute(
        "SELECT rowid FROM notes_fts_rowid WHERE note_id = ?", (note_id,)
    ).fetchone()

    if existing:
        rowid = existing[0]
        old_state = _fts_row_state(conn, note_id)
        _delete_fts_row(conn, rowid, old_state)
    else:
        rowid = _get_or_assign_fts_rowid(conn, note_id)

    state = _fts_row_state(conn, note_id)
    conn.execute(
        "INSERT INTO notes_fts (rowid, title, summary_text, transcript_text, attendees_text) "
        "VALUES (?, ?, ?, ?, ?)",
        (rowid, state["title"], state["summary_text"], state["transcript_text"], state["attendees_text"]),
    )


def index_transcript_for_fts(conn: sqlite3.Connection, note_id: str) -> None:
    """Re-insert FTS row with the transcript text now populated."""
    existing = conn.execute(
        "SELECT rowid FROM notes_fts_rowid WHERE note_id = ?", (note_id,)
    ).fetchone()
    if not existing:
        # No metadata row indexed yet; bail.
        return
    rowid = existing[0]
    old_state = _fts_row_state(conn, note_id)
    # Issue delete with whatever the FTS index currently has — best-effort reconstruction.
    _delete_fts_row(conn, rowid, old_state)
    state = _fts_row_state(conn, note_id)
    conn.execute(
        "INSERT INTO notes_fts (rowid, title, summary_text, transcript_text, attendees_text) "
        "VALUES (?, ?, ?, ?, ?)",
        (rowid, state["title"], state["summary_text"], state["transcript_text"], state["attendees_text"]),
    )


def search_notes_fts(
    query: str,
    folder_id: str | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    attendee: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Returns rows with: note + snippet + which_field."""
    with get_conn() as conn:
        sql = """
            SELECT
                n.*,
                fts.rank AS _rank,
                snippet(notes_fts, -1, '[', ']', '…', 12) AS _snippet,
                highlight(notes_fts, 0, '[', ']') AS _h_title,
                highlight(notes_fts, 1, '[', ']') AS _h_summary,
                highlight(notes_fts, 2, '[', ']') AS _h_transcript,
                highlight(notes_fts, 3, '[', ']') AS _h_attendees
            FROM notes_fts fts
            JOIN notes_fts_rowid rid ON rid.rowid = fts.rowid
            JOIN notes n             ON n.note_id = rid.note_id
            WHERE notes_fts MATCH ?
              AND n.is_pending = 0
        """
        params: list = [query]
        if folder_id:
            sql += " AND n.folder_ids LIKE ?"
            params.append(f"%\"{folder_id}\"%")
        if created_after:
            sql += " AND n.created_at >= ?"
            params.append(created_after)
        if created_before:
            sql += " AND n.created_at <= ?"
            params.append(created_before)
        if attendee:
            sql += " AND lower(n.attendees) LIKE lower(?)"
            params.append(f"%{attendee}%")

        sql += " ORDER BY fts.rank LIMIT ?"
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
