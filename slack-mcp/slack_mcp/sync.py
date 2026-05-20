"""Slack full sync and incremental delta sync."""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import database as db
from .api import SlackAuthError, SlackClient, SlackError

logger = logging.getLogger(__name__)

LOG_PATH = Path.home() / ".slack-mcp" / "sync.log"

_last_sync_error: str | None = None
_last_sync_time: str | None = None
_sync_lock = asyncio.Lock()


# ── Normalizers ───────────────────────────────────────────────────────────────

def _normalize_workspace(t: dict) -> dict:
    return {
        "id": str(t.get("id", "")),
        "name": t.get("name", ""),
        "domain": t.get("domain", ""),
        "url": t.get("url", ""),
    }


def _normalize_user(u: dict) -> dict:
    profile = u.get("profile") or {}
    return {
        "id": str(u.get("id", "")),
        "name": u.get("name", "") or "",
        "real_name": profile.get("real_name") or u.get("real_name") or "",
        "display_name": profile.get("display_name") or "",
        "email": profile.get("email") or "",
        "is_bot": 1 if u.get("is_bot") else 0,
        "is_deleted": 1 if u.get("deleted") else 0,
        "image_url": profile.get("image_192") or profile.get("image_72") or "",
        "tz": u.get("tz") or "",
        "updated": int(u.get("updated") or 0),
        "profile_title": profile.get("title") or "",
    }


def _conv_type(c: dict) -> str:
    if c.get("is_im"):
        return "im"
    if c.get("is_mpim"):
        return "mpim"
    if c.get("is_private"):
        return "private_channel"
    return "public_channel"


def _normalize_conversation(c: dict) -> dict:
    topic_obj = c.get("topic") or {}
    purpose_obj = c.get("purpose") or {}
    return {
        "id": str(c.get("id", "")),
        "name": c.get("name") or c.get("name_normalized") or "",
        "type": _conv_type(c),
        "created": int(c.get("created") or 0),
        "is_archived": 1 if c.get("is_archived") else 0,
        "is_member": 1 if (c.get("is_member") or c.get("is_im") or c.get("is_mpim")) else 0,
        "num_members": int(c.get("num_members") or 0),
        "topic": topic_obj.get("value", "") if isinstance(topic_obj, dict) else "",
        "purpose": purpose_obj.get("value", "") if isinstance(purpose_obj, dict) else "",
        "user_id": c.get("user") if c.get("is_im") else None,
    }


def _normalize_message(m: dict, channel_id: str) -> dict:
    ts = str(m.get("ts", ""))
    thread_ts = m.get("thread_ts")
    files = m.get("files") or []
    reactions = m.get("reactions") or []
    edited = m.get("edited") or {}
    return {
        "channel_id": channel_id,
        "ts": ts,
        "user_id": m.get("user") or m.get("bot_id") or "",
        "text": m.get("text") or "",
        "thread_ts": str(thread_ts) if thread_ts else None,
        "reply_count": int(m.get("reply_count") or 0),
        "subtype": m.get("subtype") or "",
        "edited_ts": edited.get("ts") if isinstance(edited, dict) else None,
        "has_files": 1 if files else 0,
        "files_json": json.dumps(files),
        "reactions_json": json.dumps(reactions),
        "blocks_json": json.dumps(m.get("blocks") or []),
        "raw_json": json.dumps(m),
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _user_display(u: dict) -> str:
    return u.get("display_name") or u.get("real_name") or u.get("name") or ""


def _ingest_messages(messages: list[dict], channel_id: str, channel_name: str,
                     users_map: dict[str, dict]) -> tuple[str | None, str | None]:
    """Returns (min_ts, max_ts) inserted (as strings) or (None, None)."""
    if not messages:
        return None, None
    min_ts: str | None = None
    max_ts: str | None = None
    with db.get_conn() as conn:
        for m in messages:
            norm = _normalize_message(m, channel_id)
            uid = norm["user_id"] or ""
            uname = _user_display(users_map.get(uid) or {})
            db.upsert_message(conn, norm, channel_name=channel_name, user_name=uname)
            ts = norm["ts"]
            if not min_ts or ts < min_ts:
                min_ts = ts
            if not max_ts or ts > max_ts:
                max_ts = ts
    return min_ts, max_ts


async def _sync_thread(client: SlackClient, channel_id: str, thread_ts: str,
                       channel_name: str, users_map: dict[str, dict]) -> int:
    count = 0
    try:
        async for batch in client.iter_replies(channel_id, thread_ts):
            # The first message in replies is the parent — already stored, but
            # safe to upsert again.
            _ingest_messages(batch, channel_id, channel_name, users_map)
            count += len(batch)
    except SlackError as exc:
        _log(f"  thread {thread_ts} in {channel_name}: {exc.error}")
    return count


# ── Full sync ─────────────────────────────────────────────────────────────────

async def _sync_users(client: SlackClient) -> int:
    count = 0
    async for batch in client.users_list():
        with db.get_conn() as conn:
            for u in batch:
                db.upsert_user(conn, _normalize_user(u))
        count += len(batch)
    return count


async def _sync_conversations_list(client: SlackClient) -> int:
    count = 0
    async for batch in client.conversations_list():
        with db.get_conn() as conn:
            for c in batch:
                db.upsert_conversation(conn, _normalize_conversation(c))
        count += len(batch)
    return count


def _format_channel_label(conv: dict, users_map: dict[str, dict]) -> str:
    t = conv["type"]
    if t == "im":
        uid = conv.get("user_id") or ""
        u = users_map.get(uid) or {}
        return f"DM: {_user_display(u) or uid}"
    return conv.get("name") or conv["id"]


async def _full_sync_channel(client: SlackClient, conv: dict,
                             users_map: dict[str, dict]) -> int:
    channel_id = conv["id"]
    channel_name = _format_channel_label(conv, users_map)
    print(f"  → {channel_name}", flush=True)

    # Resume support: if we have an oldest_synced_ts, continue paginating older
    # from that point.
    latest_bound = conv.get("oldest_synced_ts") if conv.get("latest_synced_ts") else None
    overall_oldest = conv.get("oldest_synced_ts")
    overall_latest = conv.get("latest_synced_ts")
    total = 0

    try:
        async for batch in client.iter_history(channel_id, latest=latest_bound):
            min_ts, max_ts = _ingest_messages(batch, channel_id, channel_name, users_map)

            # Threads
            for m in batch:
                if int(m.get("reply_count") or 0) > 0:
                    thread_ts = str(m.get("thread_ts") or m.get("ts"))
                    await _sync_thread(client, channel_id, thread_ts,
                                       channel_name, users_map)

            if min_ts and (not overall_oldest or min_ts < overall_oldest):
                overall_oldest = min_ts
            if max_ts and (not overall_latest or max_ts > overall_latest):
                overall_latest = max_ts

            with db.get_conn() as conn:
                db.update_conversation_sync(
                    conn, channel_id,
                    oldest_synced_ts=overall_oldest,
                    latest_synced_ts=overall_latest,
                )

            total += len(batch)
            if total % 1000 == 0:
                print(f"     {channel_name}: {total} messages so far", flush=True)

        with db.get_conn() as conn:
            db.update_conversation_sync(conn, channel_id, fully_synced=1)
        print(f"     {channel_name}: done ({total} messages)", flush=True)

    except SlackAuthError as exc:
        _log(f"Auth error on {channel_name}: {exc.error} — stopping")
        raise
    except SlackError as exc:
        _log(f"Error syncing {channel_name}: {exc.error}")
    except Exception as exc:
        _log(f"Error syncing {channel_name}: {exc}")
        logger.error("Sync error on %s", channel_name, exc_info=True)

    return total


async def run_full_sync(get_token: Callable[[], str]) -> None:
    global _last_sync_error, _last_sync_time

    async with _sync_lock:
        _log("Full sync started")
        print("\n" + "=" * 56)
        print("  FULL SLACK SYNC — this may take a while")
        print("=" * 56 + "\n", flush=True)

        try:
            async with SlackClient(get_token) as client:
                # 1. Workspace
                team = await client.team_info()
                if team:
                    with db.get_conn() as conn:
                        db.upsert_workspace(conn, _normalize_workspace(team))

                # 2. Users
                print("Syncing users...", flush=True)
                n_users = await _sync_users(client)
                print(f"  {n_users} users", flush=True)

                # 3. Conversations list
                print("Syncing conversations list...", flush=True)
                n_conv = await _sync_conversations_list(client)
                print(f"  {n_conv} conversations", flush=True)

                # 4. Per-channel history (resumable)
                users_map = db.get_users_map()
                unsynced = db.get_unsynced_conversations()
                print(f"\nSyncing history for {len(unsynced)} channel(s)...\n", flush=True)
                for conv in unsynced:
                    try:
                        await _full_sync_channel(client, conv, users_map)
                    except SlackAuthError:
                        raise
                    except Exception as exc:
                        msg = f"Error syncing channel {conv.get('name') or conv['id']}: {exc}"
                        logger.error(msg)
                        _log(msg)
                        _last_sync_error = str(exc)

            _last_sync_time = datetime.now().isoformat()
            db.set_sync_state("last_full_sync", _last_sync_time)
            db.set_sync_state("full_sync_complete", "1")
            total = db.get_total_message_count()

            print(f"\n{'=' * 56}")
            print(f"  Full sync complete: {total} total messages")
            print(f"{'=' * 56}\n", flush=True)
            _log(f"Full sync complete: {total} total messages")

        except SlackAuthError as exc:
            _last_sync_error = f"auth: {exc.error}"
            _log(f"Full sync aborted (auth): {exc.error}")
            logger.error("Full sync auth error: %s — not retrying", exc.error)
        except Exception as exc:
            _last_sync_error = str(exc)
            _log(f"Full sync failed: {exc}")
            logger.error("Full sync failed: %s", exc, exc_info=True)
            raise


# ── Delta sync ────────────────────────────────────────────────────────────────

async def run_delta_sync(get_token: Callable[[], str]) -> int:
    global _last_sync_error, _last_sync_time

    if _sync_lock.locked():
        logger.debug("Sync already in progress, skipping delta")
        return 0

    async with _sync_lock:
        _log("Delta sync started")
        total_changes = 0

        try:
            async with SlackClient(get_token) as client:
                # Refresh users once
                try:
                    await _sync_users(client)
                except SlackError as exc:
                    _log(f"users.list during delta: {exc.error}")

                users_map = db.get_users_map()

                conversations = [
                    c for c in db.get_all_conversations()
                    if c.get("is_member") and c.get("latest_synced_ts")
                ]

                for conv in conversations:
                    channel_id = conv["id"]
                    channel_name = _format_channel_label(conv, users_map)
                    latest = conv.get("latest_synced_ts")
                    try:
                        new_messages: list[dict] = []
                        async for batch in client.iter_history(channel_id, oldest=latest):
                            new_messages.extend(batch)

                        if not new_messages:
                            continue

                        min_ts, max_ts = _ingest_messages(
                            new_messages, channel_id, channel_name, users_map
                        )

                        # Threaded follow-ups for any new messages with replies
                        for m in new_messages:
                            if int(m.get("reply_count") or 0) > 0:
                                thread_ts = str(m.get("thread_ts") or m.get("ts"))
                                await _sync_thread(client, channel_id, thread_ts,
                                                   channel_name, users_map)

                        new_latest = max_ts if max_ts and max_ts > (latest or "") else latest
                        with db.get_conn() as conn:
                            db.update_conversation_sync(
                                conn, channel_id, latest_synced_ts=new_latest
                            )
                        total_changes += len(new_messages)

                    except SlackAuthError:
                        raise
                    except SlackError as exc:
                        _log(f"Delta error in {channel_name}: {exc.error}")
                    except Exception as exc:
                        msg = f"Delta error in {channel_name}: {exc}"
                        logger.error(msg)
                        _log(msg)
                        _last_sync_error = str(exc)

            _last_sync_time = datetime.now().isoformat()
            db.set_sync_state("last_delta_sync", _last_sync_time)

            if total_changes:
                logger.info("Delta sync: %d new messages", total_changes)
                _log(f"Delta sync: {total_changes} new messages")
            else:
                logger.debug("Delta sync: no changes")

        except SlackAuthError as exc:
            _last_sync_error = f"auth: {exc.error}"
            _log(f"Delta sync aborted (auth): {exc.error}")
            logger.error("Delta sync auth error: %s — not retrying", exc.error)
        except Exception as exc:
            _last_sync_error = str(exc)
            _log(f"Delta sync failed: {exc}")
            logger.error("Delta sync failed: %s", exc, exc_info=True)

        return total_changes


# ── Channel resync ────────────────────────────────────────────────────────────

async def run_channel_resync(get_token: Callable[[], str]) -> int:
    """Refresh the conversations list so newly-joined channels are picked up."""
    if _sync_lock.locked():
        return 0
    async with _sync_lock:
        try:
            async with SlackClient(get_token) as client:
                n = await _sync_conversations_list(client)
                _log(f"Channel resync: {n} conversations refreshed")
                return n
        except SlackAuthError as exc:
            _log(f"Channel resync aborted (auth): {exc.error}")
            return 0
        except Exception as exc:
            _log(f"Channel resync failed: {exc}")
            return 0


# ── Status ────────────────────────────────────────────────────────────────────

def get_sync_status() -> dict:
    ws = db.get_workspace() or {}
    return {
        "workspace_name": ws.get("name"),
        "last_full_sync": db.get_sync_state("last_full_sync"),
        "last_delta_sync": db.get_sync_state("last_delta_sync"),
        "last_sync_time": _last_sync_time,
        "total_messages": db.get_total_message_count(),
        "fully_synced_channels": db.get_fully_synced_count(),
        "last_error": _last_sync_error,
        "sync_in_progress": _sync_lock.locked(),
    }


# ── Logging helper ────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_PATH, "a") as fh:
        fh.write(f"[{ts}] {msg}\n")
