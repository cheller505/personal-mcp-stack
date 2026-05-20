"""Granola full sync, delta sync, and lazy transcript fetch."""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import database as db
from .api import GranolaClient
from .auth import InvalidAPIKeyError

logger = logging.getLogger(__name__)

LOG_PATH = Path.home() / ".granola-mcp" / "sync.log"

_last_sync_error: str | None = None
_last_sync_time: str | None = None
_sync_lock = asyncio.Lock()
_auth_dead = False


# ── Normalizers ───────────────────────────────────────────────────────────────

def _normalize_folder(f: dict) -> dict:
    return {
        "id": str(f.get("id", "")),
        "name": f.get("name", "") or "",
        "parent_folder_id": f.get("parent_folder_id") or f.get("parentFolderId"),
    }


def _normalize_note(n: dict) -> dict:
    owner = n.get("owner") or n.get("creator") or {}
    if not isinstance(owner, dict):
        owner = {}

    cal = n.get("calendar_event") or n.get("calendarEvent") or {}
    if not isinstance(cal, dict):
        cal = {}

    attendees = n.get("attendees") or cal.get("attendees") or []
    if not isinstance(attendees, list):
        attendees = []

    folder_membership = n.get("folder_membership") or n.get("folderMembership") or []
    folder_ids: list[str] = []
    if isinstance(folder_membership, list):
        for fm in folder_membership:
            if isinstance(fm, dict):
                fid = fm.get("folder_id") or fm.get("folderId") or fm.get("id")
                if fid:
                    folder_ids.append(str(fid))
            elif isinstance(fm, str):
                folder_ids.append(fm)

    organiser = cal.get("organiser") or cal.get("organizer") or {}
    organiser_email = ""
    if isinstance(organiser, dict):
        organiser_email = organiser.get("email") or ""
    elif isinstance(organiser, str):
        organiser_email = organiser

    return {
        "note_id": str(n.get("id") or n.get("note_id") or ""),
        "title": n.get("title"),
        "owner_name": owner.get("name") or owner.get("displayName") or "",
        "owner_email": owner.get("email") or "",
        "created_at": n.get("created_at") or n.get("createdAt") or "",
        "updated_at": n.get("updated_at") or n.get("updatedAt") or "",
        "summary_text": n.get("summary_text") or n.get("summaryText") or "",
        "summary_markdown": n.get("summary_markdown") or n.get("summaryMarkdown") or "",
        "calendar_event_title": cal.get("title") or cal.get("summary") or "",
        "calendar_event_id": str(cal.get("id") or cal.get("event_id") or ""),
        "scheduled_start_time": cal.get("start_time") or cal.get("startTime") or "",
        "scheduled_end_time": cal.get("end_time") or cal.get("endTime") or "",
        "organiser_email": organiser_email,
        "attendees": json.dumps(attendees),
        "folder_ids": json.dumps(folder_ids),
        "transcript_cached": 0,
        "is_pending": 0,
    }


# ── Full sync ─────────────────────────────────────────────────────────────────

async def run_full_sync(get_key: Callable[[], str]) -> None:
    global _last_sync_error, _last_sync_time, _auth_dead

    if _auth_dead:
        return

    async with _sync_lock:
        _log("Full sync started")
        print("\n" + "=" * 56)
        print("  FULL GRANOLA SYNC — metadata only (transcripts lazy)")
        print("=" * 56 + "\n", flush=True)

        try:
            async with GranolaClient(get_key) as client:
                # Folders first
                folder_count = 0
                with db.get_conn() as conn:
                    async for f in client.iter_folders():
                        db.upsert_folder(conn, _normalize_folder(f))
                        folder_count += 1
                print(f"  Folders: {folder_count}", flush=True)
                _log(f"Synced {folder_count} folders")

                # Notes (metadata)
                page = 0
                total = 0
                async for n in client.iter_notes():
                    norm = _normalize_note(n)
                    if not norm["note_id"]:
                        continue
                    with db.get_conn() as conn:
                        db.upsert_note(conn, norm)
                        db.index_note_for_fts(conn, norm)
                    total += 1
                    if total % 30 == 0:
                        page += 1
                        print(f"     page {page}: {total} notes so far", flush=True)

                print(f"  Notes: {total}", flush=True)

            _last_sync_time = datetime.now().isoformat()
            db.set_sync_state("last_full_sync", _last_sync_time)
            db.set_sync_state("full_sync_complete", "1")

            print(f"\n{'=' * 56}")
            print(f"  Full sync complete: {total} notes, {folder_count} folders")
            print(f"{'=' * 56}\n", flush=True)
            _log(f"Full sync complete: {total} notes, {folder_count} folders")

        except InvalidAPIKeyError as exc:
            _auth_dead = True
            _last_sync_error = str(exc)
            _log(f"AUTH FAILED: {exc} — update ~/.granola-mcp/config.json and restart")
            logger.error("Granola API key invalid — sync halted: %s", exc)
            return
        except Exception as exc:
            _last_sync_error = str(exc)
            _log(f"Full sync failed: {exc}")
            logger.error("Full sync failed: %s", exc, exc_info=True)
            raise


# ── Incremental sync ──────────────────────────────────────────────────────────

async def run_incremental_sync(get_key: Callable[[], str]) -> int:
    global _last_sync_error, _last_sync_time, _auth_dead

    if _auth_dead:
        return 0

    if _sync_lock.locked():
        logger.debug("Sync already in progress, skipping incremental")
        return 0

    async with _sync_lock:
        _log("Incremental sync started")
        changes = 0

        try:
            async with GranolaClient(get_key) as client:
                # Refresh folders (lightweight)
                with db.get_conn() as conn:
                    async for f in client.iter_folders():
                        db.upsert_folder(conn, _normalize_folder(f))

                last_updated = db.get_max_updated_at()
                async for n in client.iter_notes(updated_after=last_updated):
                    norm = _normalize_note(n)
                    if not norm["note_id"]:
                        continue
                    with db.get_conn() as conn:
                        # Preserve transcript_cached flag from existing row
                        existing = conn.execute(
                            "SELECT transcript_cached FROM notes WHERE note_id = ?",
                            (norm["note_id"],),
                        ).fetchone()
                        if existing:
                            norm["transcript_cached"] = existing[0] or 0
                        db.upsert_note(conn, norm)
                        db.index_note_for_fts(conn, norm)
                    changes += 1

            _last_sync_time = datetime.now().isoformat()
            db.set_sync_state("last_incremental_sync", _last_sync_time)

            if changes:
                logger.info("Incremental sync: %d changes", changes)
                _log(f"Incremental sync: {changes} changes")
            else:
                logger.debug("Incremental sync: no changes")

        except InvalidAPIKeyError as exc:
            _auth_dead = True
            _last_sync_error = str(exc)
            _log(f"AUTH FAILED: {exc} — update ~/.granola-mcp/config.json and restart")
            logger.error("Granola API key invalid — sync halted: %s", exc)
            return 0
        except Exception as exc:
            _last_sync_error = str(exc)
            _log(f"Incremental sync failed: {exc}")
            logger.error("Incremental sync failed: %s", exc, exc_info=True)

        return changes


# ── Lazy transcript fetch ─────────────────────────────────────────────────────

async def ensure_transcript(get_key: Callable[[], str], note_id: str) -> list[dict] | None:
    """Return cached transcript if present; else fetch + cache + index for FTS.
    Returns None if note is unavailable (404 — pending)."""
    global _auth_dead

    if _auth_dead:
        cached = db.get_transcript(note_id)
        return cached

    cached = db.get_transcript(note_id)
    if cached is not None:
        return cached

    try:
        async with GranolaClient(get_key) as client:
            data = await client.get_note(note_id, include_transcript=True)
    except InvalidAPIKeyError as exc:
        _auth_dead = True
        _log(f"AUTH FAILED (transcript fetch): {exc}")
        logger.error("Granola API key invalid: %s", exc)
        return None

    if data is None:
        # 404 — mark pending
        with db.get_conn() as conn:
            db.set_note_pending(conn, note_id, True)
        _log(f"Note {note_id} returned 404 — marked pending")
        return None

    # Update the note row with anything new (summary, attendees, etc.)
    norm = _normalize_note(data)
    if norm["note_id"]:
        with db.get_conn() as conn:
            existing = conn.execute(
                "SELECT transcript_cached FROM notes WHERE note_id = ?",
                (norm["note_id"],),
            ).fetchone()
            if existing:
                norm["transcript_cached"] = 1
            db.upsert_note(conn, norm)
            db.set_note_pending(conn, norm["note_id"], False)

    transcript = data.get("transcript") or []
    if not isinstance(transcript, list):
        transcript = []

    with db.get_conn() as conn:
        db.store_transcript(conn, note_id, transcript)
        db.set_transcript_cached(conn, note_id)
        db.index_transcript_for_fts(conn, note_id)

    return transcript


# ── Pending retry ─────────────────────────────────────────────────────────────

async def retry_pending(get_key: Callable[[], str]) -> int:
    """Periodically retry notes that previously 404'd."""
    global _auth_dead
    if _auth_dead:
        return 0

    note_ids = db.get_pending_note_ids()
    if not note_ids:
        return 0

    cleared = 0
    try:
        async with GranolaClient(get_key) as client:
            for nid in note_ids:
                try:
                    data = await client.get_note(nid, include_transcript=False)
                except InvalidAPIKeyError:
                    _auth_dead = True
                    return cleared
                if data is None:
                    continue
                norm = _normalize_note(data)
                if not norm["note_id"]:
                    continue
                with db.get_conn() as conn:
                    db.upsert_note(conn, norm)
                    db.set_note_pending(conn, norm["note_id"], False)
                    db.index_note_for_fts(conn, norm)
                cleared += 1
    except Exception as exc:
        _log(f"retry_pending failed: {exc}")
        logger.warning("retry_pending failed: %s", exc)

    if cleared:
        _log(f"retry_pending: cleared {cleared} previously-pending notes")
    return cleared


# ── Status ────────────────────────────────────────────────────────────────────

def get_sync_status() -> dict:
    return {
        "last_full_sync": db.get_sync_state("last_full_sync"),
        "last_incremental_sync": db.get_sync_state("last_incremental_sync"),
        "full_sync_complete": db.get_sync_state("full_sync_complete") == "1",
        "total_notes": db.get_total_note_count(),
        "transcripts_cached": db.get_cached_transcript_count(),
        "pending_count": db.get_pending_count(),
        "last_error": _last_sync_error,
        "sync_in_progress": _sync_lock.locked(),
        "auth_dead": _auth_dead,
    }


# ── Logging helper ────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_PATH, "a") as fh:
        fh.write(f"[{ts}] {msg}\n")
