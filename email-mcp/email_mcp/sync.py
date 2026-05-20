"""Full mailbox sync and incremental delta sync engine."""

import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

from bs4 import BeautifulSoup

from . import database as db
from .graph import GraphClient

logger = logging.getLogger(__name__)

LOG_PATH = Path.home() / ".email-mcp" / "sync.log"

_last_sync_error: str | None = None
_last_sync_time: str | None = None
_sync_lock = asyncio.Lock()


# ── HTML stripping ────────────────────────────────────────────────────────────

def _strip_html(html: str) -> str:
    if not html:
        return ""
    try:
        return BeautifulSoup(html, "lxml").get_text(separator=" ", strip=True)
    except Exception:
        return html[:10_000]


# ── Graph → DB normalization ──────────────────────────────────────────────────

def _normalize_message(msg: dict, folder_id: str) -> dict:
    sender = msg.get("from", {}).get("emailAddress", {})

    def _rcpts(key: str) -> str:
        return json.dumps([
            {"name": r["emailAddress"].get("name", ""),
             "email": r["emailAddress"].get("address", "").lower()}
            for r in msg.get(key, [])
        ])

    body = msg.get("body", {})
    content_type = (body.get("contentType") or "").lower()
    raw_content = body.get("content", "")

    if content_type == "html":
        body_html = raw_content
        body_text = _strip_html(raw_content)
    else:
        body_html = ""
        body_text = raw_content

    attachments = [
        a.get("name", "")
        for a in msg.get("attachments", [])
        if not a.get("isInline", False)
    ]

    return {
        "id": msg["id"],
        "conversation_id": msg.get("conversationId"),
        "folder_id": folder_id,
        "sender_name": sender.get("name", ""),
        "sender_email": sender.get("address", "").lower(),
        "to_recipients": _rcpts("toRecipients"),
        "cc_recipients": _rcpts("ccRecipients"),
        "subject": msg.get("subject") or "(no subject)",
        "body_html": body_html,
        "body_text": body_text,
        "received_datetime": msg.get("receivedDateTime", ""),
        "is_read": 1 if msg.get("isRead") else 0,
        "has_attachments": 1 if msg.get("hasAttachments") else 0,
        "attachment_names": json.dumps(attachments),
        "importance": msg.get("importance", "normal"),
        "is_draft": 1 if msg.get("isDraft") else 0,
    }


def _normalize_folder(f: dict) -> dict:
    return {
        "id": f["id"],
        "display_name": f.get("displayName", ""),
        "parent_folder_id": f.get("parentFolderId"),
        "child_folder_count": f.get("childFolderCount", 0),
        "unread_item_count": f.get("unreadItemCount", 0),
        "total_item_count": f.get("totalItemCount", 0),
    }


# ── Full sync ─────────────────────────────────────────────────────────────────

async def _sync_all_folders(client: GraphClient) -> list[dict]:
    """Enumerate and persist all mail folders. Returns raw folder list."""
    print("Enumerating mail folders...", flush=True)
    raw_folders = await client.get_all_folders()

    with db.get_conn() as conn:
        for f in raw_folders:
            db.upsert_folder(conn, _normalize_folder(f))

    print(f"Found {len(raw_folders)} folders", flush=True)
    return raw_folders


async def _full_sync_folder(client: GraphClient, folder: dict) -> int:
    """Full-sync one folder. Skips if already marked fully_synced (resumable)."""
    folder_id = folder["id"]
    name = folder.get("display_name", folder_id)

    # Resumable: skip if already done
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT fully_synced FROM folders WHERE id = ?", (folder_id,)
        ).fetchone()
        if row and row["fully_synced"]:
            return 0

    print(f"  → {name}", flush=True)
    count = 0
    page = 0

    async for messages in client.iter_folder_messages(folder_id):
        if not messages:
            break
        with db.get_conn() as conn:
            for msg in messages:
                db.upsert_message(conn, _normalize_message(msg, folder_id))
        count += len(messages)
        page += 1
        print(f"     {name}: {count} messages (page {page})", flush=True)

    # Capture initial delta token for future incremental syncs
    _, delta_token = await client.get_folder_delta(folder_id)

    with db.get_conn() as conn:
        db.set_folder_synced(conn, folder_id, delta_token)

    print(f"     {name}: done ({count} messages)", flush=True)
    return count


async def run_full_sync(get_token: Callable[[], str]) -> None:
    """Full mailbox sync. Resumable across restarts."""
    global _last_sync_error, _last_sync_time

    async with _sync_lock:
        _log("Full sync started")
        print("\n" + "=" * 56)
        print("  FULL MAILBOX SYNC — this may take several minutes")
        print("=" * 56 + "\n", flush=True)

        try:
            async with GraphClient(get_token) as client:
                await _sync_all_folders(client)

                unsynced = db.get_unsynced_folders()
                all_count = len(db.get_all_folders_with_delta())
                skipped = all_count - len(unsynced)

                print(
                    f"\nSyncing {len(unsynced)} folders "
                    f"({skipped} already complete, skipping)...\n",
                    flush=True,
                )

                total_new = 0
                for folder in unsynced:
                    try:
                        total_new += await _full_sync_folder(client, folder)
                    except Exception as exc:
                        msg = f"Error syncing folder '{folder.get('display_name')}': {exc}"
                        logger.error(msg)
                        _log(msg)
                        _last_sync_error = str(exc)

            _last_sync_time = datetime.now().isoformat()
            db.set_sync_state("last_full_sync", _last_sync_time)
            total = db.get_total_message_count()

            print(f"\n{'=' * 56}")
            print(f"  Full sync complete: {total_new} new, {total} total")
            print(f"{'=' * 56}\n", flush=True)
            _log(f"Full sync complete: {total} total messages")

        except Exception as exc:
            _last_sync_error = str(exc)
            _log(f"Full sync failed: {exc}")
            logger.error("Full sync failed: %s", exc, exc_info=True)
            raise


# ── Delta sync ────────────────────────────────────────────────────────────────

async def run_delta_sync(get_token: Callable[[], str]) -> int:
    """Incremental sync using per-folder delta tokens. Returns total changes."""
    global _last_sync_error, _last_sync_time

    if _sync_lock.locked():
        logger.debug("Sync already in progress, skipping delta")
        return 0

    async with _sync_lock:
        _log("Delta sync started")
        total_changes = 0

        try:
            async with GraphClient(get_token) as client:
                folders = [f for f in db.get_all_folders_with_delta() if f["fully_synced"]]

                for folder in folders:
                    try:
                        changes, new_token = await client.get_folder_delta(
                            folder["id"], folder.get("delta_token")
                        )
                        if changes or new_token:
                            with db.get_conn() as conn:
                                for change in changes:
                                    if change.get("@removed"):
                                        db.delete_message(conn, change["id"])
                                    else:
                                        db.upsert_message(
                                            conn, _normalize_message(change, folder["id"])
                                        )
                                if new_token:
                                    db.update_folder_delta_token(conn, folder["id"], new_token)
                            total_changes += len(changes)

                    except Exception as exc:
                        msg = f"Delta error for '{folder['display_name']}': {exc}"
                        logger.error(msg)
                        _log(msg)
                        _last_sync_error = str(exc)

            _last_sync_time = datetime.now().isoformat()
            db.set_sync_state("last_delta_sync", _last_sync_time)

            if total_changes:
                logger.info("Delta sync: %d changes", total_changes)
                _log(f"Delta sync: {total_changes} changes")
            else:
                logger.debug("Delta sync: no changes")

        except Exception as exc:
            _last_sync_error = str(exc)
            _log(f"Delta sync failed: {exc}")
            logger.error("Delta sync failed: %s", exc, exc_info=True)

        return total_changes


# ── Status ────────────────────────────────────────────────────────────────────

def get_sync_status() -> dict:
    return {
        "last_full_sync": db.get_sync_state("last_full_sync"),
        "last_delta_sync": db.get_sync_state("last_delta_sync"),
        "last_sync_time": _last_sync_time,
        "total_messages": db.get_total_message_count(),
        "last_error": _last_sync_error,
        "sync_in_progress": _sync_lock.locked(),
    }


# ── Logging helper ────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_PATH, "a") as fh:
        fh.write(f"[{ts}] {msg}\n")
