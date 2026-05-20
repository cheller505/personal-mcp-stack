"""ClickUp full sync and incremental delta sync."""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import database as db
from .api import ClickUpClient

logger = logging.getLogger(__name__)

LOG_PATH = Path.home() / ".clickup-mcp" / "sync.log"

_last_sync_error: str | None = None
_last_sync_time: str | None = None
_sync_lock = asyncio.Lock()


# ── Normalizers ───────────────────────────────────────────────────────────────

def _normalize_workspace(t: dict) -> dict:
    return {"id": str(t["id"]), "name": t.get("name", "")}


def _normalize_space(sp: dict, workspace_id: str) -> dict:
    return {
        "id": str(sp["id"]),
        "workspace_id": workspace_id,
        "name": sp.get("name", ""),
        "archived": 1 if sp.get("archived") or sp.get("_archived_flag") else 0,
    }


def _normalize_folder(f: dict, space_id: str) -> dict:
    return {
        "id": str(f["id"]),
        "space_id": space_id,
        "name": f.get("name", ""),
        "archived": 1 if f.get("archived") or f.get("_archived_flag") else 0,
    }


def _normalize_list(lst: dict, space_id: str, folder_id: str | None) -> dict:
    status = lst.get("status")
    status_str = status.get("status") if isinstance(status, dict) else status
    return {
        "id": str(lst["id"]),
        "folder_id": folder_id,
        "space_id": space_id,
        "name": lst.get("name", ""),
        "status": status_str or "",
        "task_count": lst.get("task_count") or 0,
        "archived": 1 if lst.get("archived") or lst.get("_archived_flag") else 0,
    }


def _normalize_task(task: dict, ctx: dict) -> dict:
    status_obj = task.get("status") or {}
    if isinstance(status_obj, dict):
        status = status_obj.get("status", "")
        status_color = status_obj.get("color", "")
    else:
        status = str(status_obj or "")
        status_color = ""

    priority_obj = task.get("priority")
    if isinstance(priority_obj, dict):
        try:
            priority = int(priority_obj.get("priority") or priority_obj.get("id") or 0) or None
        except (ValueError, TypeError):
            priority = None
    elif isinstance(priority_obj, (int, str)) and priority_obj:
        try:
            priority = int(priority_obj)
        except (ValueError, TypeError):
            priority = None
    else:
        priority = None

    assignees = [
        {"id": str(a.get("id", "")), "username": a.get("username", ""), "email": a.get("email", "")}
        for a in task.get("assignees", []) or []
    ]
    tags = [t.get("name", "") for t in task.get("tags", []) or []]

    creator = task.get("creator") or {}

    subtasks = task.get("subtasks") or []
    subtask_ids = [str(s.get("id")) for s in subtasks if s.get("id")]

    parent = task.get("parent")
    parent_task_id = str(parent) if parent else None

    list_obj = task.get("list") or {}
    folder_obj = task.get("folder") or {}
    space_obj = task.get("space") or {}

    return {
        "task_id": str(task["id"]),
        "list_id": str(list_obj.get("id") or ctx.get("list_id") or ""),
        "list_name": list_obj.get("name") or ctx.get("list_name", ""),
        "folder_id": str(folder_obj.get("id")) if folder_obj.get("id") else ctx.get("folder_id"),
        "folder_name": folder_obj.get("name") or ctx.get("folder_name", ""),
        "space_id": str(space_obj.get("id")) if space_obj.get("id") else ctx.get("space_id"),
        "space_name": space_obj.get("name") or ctx.get("space_name", ""),
        "workspace_id": ctx.get("workspace_id"),
        "name": task.get("name", ""),
        "description": task.get("description") or task.get("text_content") or "",
        "status": status,
        "status_color": status_color,
        "priority": priority,
        "assignees": json.dumps(assignees),
        "tags": json.dumps(tags),
        "due_date": task.get("due_date"),
        "start_date": task.get("start_date"),
        "time_estimate": task.get("time_estimate"),
        "time_spent": task.get("time_spent"),
        "creator_id": str(creator.get("id")) if creator.get("id") else None,
        "creator_name": creator.get("username", ""),
        "date_created": task.get("date_created"),
        "date_updated": task.get("date_updated"),
        "date_closed": task.get("date_closed"),
        "is_closed": 1 if status_obj and isinstance(status_obj, dict) and status_obj.get("type") == "closed" else 0,
        "is_deleted": 0,
        "parent_task_id": parent_task_id,
        "subtask_ids": json.dumps(subtask_ids),
        "custom_fields": json.dumps(task.get("custom_fields") or []),
        "url": task.get("url", ""),
        "archived": 1 if task.get("archived") else 0,
    }


def _normalize_member(u: dict) -> dict:
    return {
        "id": str(u.get("id", "")),
        "username": u.get("username", ""),
        "email": u.get("email", ""),
        "color": u.get("color", ""),
        "profile_picture": u.get("profilePicture") or u.get("profile_picture", ""),
    }


def _normalize_comment(c: dict, task_id: str) -> dict:
    text_parts = []
    for chunk in c.get("comment", []) or []:
        if isinstance(chunk, dict):
            text_parts.append(chunk.get("text", ""))
    text = "".join(text_parts) or c.get("comment_text", "")
    user = c.get("user") or {}
    return {
        "id": str(c.get("id", "")),
        "task_id": task_id,
        "comment_text": text,
        "user_id": str(user.get("id")) if user.get("id") else None,
        "date": c.get("date", ""),
        "resolved": 1 if c.get("resolved") else 0,
    }


# ── Hierarchy enumeration ─────────────────────────────────────────────────────

async def _enumerate_hierarchy(client: ClickUpClient) -> list[dict]:
    """Walk workspaces → spaces → folders → lists. Persist all. Return list of
    list-context dicts ready for task sync."""
    print("Enumerating ClickUp hierarchy...", flush=True)
    workspaces = await client.get_workspaces()

    list_contexts: list[dict] = []

    with db.get_conn() as conn:
        for ws in workspaces:
            db.upsert_workspace(conn, _normalize_workspace(ws))

    for ws in workspaces:
        ws_id = str(ws["id"])
        ws_name = ws.get("name", "")
        spaces = await client.get_spaces(ws_id)
        with db.get_conn() as conn:
            for sp in spaces:
                db.upsert_space(conn, _normalize_space(sp, ws_id))

        for sp in spaces:
            sp_id = str(sp["id"])
            sp_name = sp.get("name", "")

            folders = await client.get_folders(sp_id)
            with db.get_conn() as conn:
                for f in folders:
                    db.upsert_folder(conn, _normalize_folder(f, sp_id))

            # Folderless lists in this space
            folderless = await client.get_folderless_lists(sp_id)
            with db.get_conn() as conn:
                for lst in folderless:
                    db.upsert_list(conn, _normalize_list(lst, sp_id, None))
            for lst in folderless:
                list_contexts.append({
                    "list_id": str(lst["id"]),
                    "list_name": lst.get("name", ""),
                    "folder_id": None,
                    "folder_name": "",
                    "space_id": sp_id,
                    "space_name": sp_name,
                    "workspace_id": ws_id,
                })

            # Lists inside folders
            for folder in folders:
                folder_id = str(folder["id"])
                folder_name = folder.get("name", "")
                # ClickUp returns folder.lists inline sometimes
                inline_lists = folder.get("lists") or []
                if inline_lists:
                    lists = inline_lists
                else:
                    lists = await client.get_lists(folder_id)
                with db.get_conn() as conn:
                    for lst in lists:
                        db.upsert_list(conn, _normalize_list(lst, sp_id, folder_id))
                for lst in lists:
                    list_contexts.append({
                        "list_id": str(lst["id"]),
                        "list_name": lst.get("name", ""),
                        "folder_id": folder_id,
                        "folder_name": folder_name,
                        "space_id": sp_id,
                        "space_name": sp_name,
                        "workspace_id": ws_id,
                    })

    print(f"Found {len(list_contexts)} lists across {len(workspaces)} workspace(s)", flush=True)
    return list_contexts


# ── Full sync ─────────────────────────────────────────────────────────────────

async def _full_sync_list(client: ClickUpClient, ctx: dict) -> int:
    list_id = ctx["list_id"]
    name = ctx["list_name"] or list_id

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT fully_synced FROM lists WHERE id = ?", (list_id,)
        ).fetchone()
        if row and row["fully_synced"]:
            return 0

    print(f"  → {name}", flush=True)
    page = 0
    total = 0
    max_updated: int | None = None

    while True:
        tasks = await client.get_tasks(list_id, page=page, include_closed=True)
        if not tasks:
            break

        with db.get_conn() as conn:
            for t in tasks:
                norm = _normalize_task(t, ctx)
                db.upsert_task(conn, norm)
                try:
                    du = int(norm["date_updated"]) if norm["date_updated"] else None
                except (ValueError, TypeError):
                    du = None
                if du is not None and (max_updated is None or du > max_updated):
                    max_updated = du

        total += len(tasks)
        print(f"     {name}: {total} tasks (page {page})", flush=True)
        if len(tasks) < 100:
            break
        page += 1

    with db.get_conn() as conn:
        db.set_list_fully_synced(conn, list_id, str(max_updated) if max_updated else None)

    print(f"     {name}: done ({total} tasks)", flush=True)
    return total


async def run_full_sync(get_token: Callable[[], str]) -> None:
    global _last_sync_error, _last_sync_time

    async with _sync_lock:
        _log("Full sync started")
        print("\n" + "=" * 56)
        print("  FULL CLICKUP SYNC — this may take several minutes")
        print("=" * 56 + "\n", flush=True)

        try:
            async with ClickUpClient(get_token) as client:
                contexts = await _enumerate_hierarchy(client)

                # Members for each workspace
                seen_ws: set[str] = set()
                for ctx in contexts:
                    ws_id = ctx["workspace_id"]
                    if ws_id in seen_ws:
                        continue
                    seen_ws.add(ws_id)
                    try:
                        members = await client.get_members(ws_id)
                        with db.get_conn() as conn:
                            for m in members:
                                db.upsert_member(conn, _normalize_member(m))
                    except Exception as exc:
                        _log(f"Member sync failed for workspace {ws_id}: {exc}")

                unsynced = db.get_unsynced_lists()
                all_lists = db.get_all_lists()
                skipped = len(all_lists) - len(unsynced)
                print(f"\nSyncing {len(unsynced)} lists ({skipped} already complete, skipping)...\n",
                      flush=True)

                ctx_by_id = {c["list_id"]: c for c in contexts}
                total_new = 0
                for lst in unsynced:
                    ctx = ctx_by_id.get(lst["id"])
                    if not ctx:
                        # List exists in DB but no context (e.g. partial state); build minimal
                        ctx = {
                            "list_id": lst["id"],
                            "list_name": lst["name"],
                            "folder_id": lst["folder_id"],
                            "folder_name": "",
                            "space_id": lst["space_id"],
                            "space_name": "",
                            "workspace_id": None,
                        }
                    try:
                        total_new += await _full_sync_list(client, ctx)
                    except Exception as exc:
                        msg = f"Error syncing list '{ctx.get('list_name')}': {exc}"
                        logger.error(msg)
                        _log(msg)
                        _last_sync_error = str(exc)

            _last_sync_time = datetime.now().isoformat()
            db.set_sync_state("last_full_sync", _last_sync_time)
            total = db.get_total_task_count()

            print(f"\n{'=' * 56}")
            print(f"  Full sync complete: {total_new} new, {total} total tasks")
            print(f"{'=' * 56}\n", flush=True)
            _log(f"Full sync complete: {total} total tasks")

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
            async with ClickUpClient(get_token) as client:
                lists = [lst for lst in db.get_all_lists() if lst["fully_synced"]]

                # Build context for each list from its space/folder/workspace ancestry
                spaces = {s["id"]: s for s in db.get_spaces()}
                folders = {f["id"]: f for f in db.get_folders()}

                for lst in lists:
                    sp = spaces.get(lst["space_id"]) or {}
                    folder = folders.get(lst["folder_id"]) if lst["folder_id"] else None
                    ctx = {
                        "list_id": lst["id"],
                        "list_name": lst["name"],
                        "folder_id": lst["folder_id"],
                        "folder_name": folder.get("name", "") if folder else "",
                        "space_id": lst["space_id"],
                        "space_name": sp.get("name", ""),
                        "workspace_id": sp.get("workspace_id"),
                    }

                    try:
                        last_gt = lst.get("last_updated_gt")
                        date_gt = int(last_gt) if last_gt else None
                        page = 0
                        max_updated = date_gt
                        list_changes = 0

                        while True:
                            tasks = await client.get_tasks(
                                lst["id"], page=page,
                                date_updated_gt=date_gt,
                                include_closed=True,
                            )
                            if not tasks:
                                break
                            with db.get_conn() as conn:
                                for t in tasks:
                                    norm = _normalize_task(t, ctx)
                                    db.upsert_task(conn, norm)
                                    try:
                                        du = int(norm["date_updated"]) if norm["date_updated"] else None
                                    except (ValueError, TypeError):
                                        du = None
                                    if du is not None and (max_updated is None or du > max_updated):
                                        max_updated = du
                            list_changes += len(tasks)
                            if len(tasks) < 100:
                                break
                            page += 1

                        if list_changes and max_updated is not None:
                            with db.get_conn() as conn:
                                db.update_list_last_updated_gt(conn, lst["id"], str(max_updated))
                        total_changes += list_changes

                    except Exception as exc:
                        msg = f"Delta error for '{lst['name']}': {exc}"
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


# ── Member-only refresh ───────────────────────────────────────────────────────

async def run_member_sync(get_token: Callable[[], str]) -> int:
    count = 0
    try:
        async with ClickUpClient(get_token) as client:
            workspaces = await client.get_workspaces()
            for ws in workspaces:
                ws_id = str(ws["id"])
                try:
                    members = await client.get_members(ws_id)
                    with db.get_conn() as conn:
                        for m in members:
                            db.upsert_member(conn, _normalize_member(m))
                    count += len(members)
                except Exception as exc:
                    _log(f"Member sync failed for ws {ws_id}: {exc}")
    except Exception as exc:
        _log(f"Member sync failed: {exc}")
    return count


# ── Status ────────────────────────────────────────────────────────────────────

def get_sync_status() -> dict:
    return {
        "last_full_sync": db.get_sync_state("last_full_sync"),
        "last_delta_sync": db.get_sync_state("last_delta_sync"),
        "last_sync_time": _last_sync_time,
        "total_tasks": db.get_total_task_count(),
        "last_error": _last_sync_error,
        "sync_in_progress": _sync_lock.locked(),
    }


# ── Logging helper ────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_PATH, "a") as fh:
        fh.write(f"[{ts}] {msg}\n")
