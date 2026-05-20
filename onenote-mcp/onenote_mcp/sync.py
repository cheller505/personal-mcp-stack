"""Full OneNote sync, delta sync, and lazy page-content fetcher."""

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable

from bs4 import BeautifulSoup

from . import database as db
from .graph import GraphAccessDenied, GraphClient, GraphNotFound

logger = logging.getLogger(__name__)

LOG_PATH = Path.home() / ".onenote-mcp" / "sync.log"

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

def _user_name(field: dict | None) -> str:
    if not field:
        return ""
    user = field.get("user") or {}
    return user.get("displayName", "") or ""


def _normalize_notebook(nb: dict) -> dict:
    return {
        "id": nb["id"],
        "display_name": nb.get("displayName", ""),
        "is_default": 1 if nb.get("isDefault") else 0,
        "is_shared": 1 if nb.get("isShared") else 0,
        "user_role": nb.get("userRole", ""),
        "created_at": nb.get("createdDateTime", ""),
        "modified_at": nb.get("lastModifiedDateTime", ""),
        "created_by_name": _user_name(nb.get("createdBy")),
        "modified_by_name": _user_name(nb.get("lastModifiedBy")),
    }


def _normalize_section_group(
    sg: dict,
    parent_notebook_id: str | None = None,
    parent_section_group_id: str | None = None,
) -> dict:
    # Prefer explicit parents passed by caller (BFS context), else fall back to embedded fields.
    pnb = parent_notebook_id
    psg = parent_section_group_id
    if pnb is None:
        pnb_obj = sg.get("parentNotebook") or {}
        pnb = pnb_obj.get("id")
    if psg is None:
        psg_obj = sg.get("parentSectionGroup") or {}
        psg = psg_obj.get("id") if psg_obj else None
    return {
        "id": sg["id"],
        "display_name": sg.get("displayName", ""),
        "parent_notebook_id": pnb,
        "parent_section_group_id": psg,
    }


def _normalize_section(
    s: dict,
    parent_notebook_id: str | None = None,
    parent_section_group_id: str | None = None,
) -> dict:
    return {
        "id": s["id"],
        "display_name": s.get("displayName", ""),
        "parent_notebook_id": parent_notebook_id,
        "parent_section_group_id": parent_section_group_id,
        "created_at": s.get("createdDateTime", ""),
        "modified_at": s.get("lastModifiedDateTime", ""),
        "created_by_name": _user_name(s.get("createdBy")),
        "modified_by_name": _user_name(s.get("lastModifiedBy")),
    }


def _normalize_page(p: dict) -> dict:
    sec = p.get("parentSection") or {}
    nb = p.get("parentNotebook") or {}
    return {
        "id": p["id"],
        "section_id": sec.get("id"),
        "notebook_id": nb.get("id"),
        "title": p.get("title") or "(untitled)",
        "level": int(p.get("level") or 0),
        "page_order": int(p.get("order") or 0),
        "content_url": p.get("contentUrl", ""),
        "created_at": p.get("createdDateTime", ""),
        "modified_at": p.get("lastModifiedDateTime", ""),
        "created_by_name": _user_name(p.get("createdBy")),
        "modified_by_name": _user_name(p.get("lastModifiedBy")),
    }


# ── Notebook + section structure sync ─────────────────────────────────────────

async def _sync_notebook_structure_flat(client: GraphClient) -> list[dict]:
    """Sync structure using flat /me/onenote/sections (bypasses error 10008).

    The Graph API rejects per-notebook section enumeration when the user's
    OneDrive has >5000 OneNote items. Instead we list ALL sections in one
    flat query (with $expand=parentNotebook) and derive notebook membership
    from each section's expanded parent.
    """
    print("Enumerating notebooks...", flush=True)
    try:
        notebooks = await client.get_notebooks()
    except (GraphAccessDenied, GraphNotFound) as exc:
        _log(f"Could not list notebooks: {exc}")
        notebooks = []

    with db.get_conn() as conn:
        for nb in notebooks:
            db.upsert_notebook(conn, _normalize_notebook(nb))
    print(f"Found {len(notebooks)} notebook(s) via top-level enumeration", flush=True)

    print("Enumerating all sections (flat)...", flush=True)
    try:
        all_sections = await client.get_all_sections()
    except (GraphAccessDenied, GraphNotFound) as exc:
        _log(f"Flat section enumeration failed: {exc}")
        all_sections = []

    print(f"Found {len(all_sections)} accessible section(s)", flush=True)

    # Group sections by parent notebook for nicer progress output
    by_notebook: dict[str, list[dict]] = {}
    notebooks_from_sections: dict[str, str] = {}  # id -> displayName

    for sec in all_sections:
        pnb = sec.get("parentNotebook") or {}
        nb_id = pnb.get("id") or ""
        nb_name = pnb.get("displayName") or "(unknown)"
        if nb_id:
            notebooks_from_sections[nb_id] = nb_name
        by_notebook.setdefault(nb_id, []).append(sec)

    # Backfill notebooks table from anything we saw via sections that wasn't
    # in the top-level notebook list (rare, but defensive).
    known_nb_ids = {nb["id"] for nb in notebooks}
    with db.get_conn() as conn:
        for nb_id, nb_name in notebooks_from_sections.items():
            if nb_id not in known_nb_ids:
                db.upsert_notebook(conn, {
                    "id": nb_id,
                    "display_name": nb_name,
                    "is_default": 0,
                    "is_shared": 0,
                    "user_role": "",
                    "created_at": "",
                    "modified_at": "",
                    "created_by_name": "",
                    "modified_by_name": "",
                })

    # Persist sections, with parent info derived from expand
    with db.get_conn() as conn:
        for sec in all_sections:
            pnb = sec.get("parentNotebook") or {}
            psg = sec.get("parentSectionGroup") or {}
            norm = _normalize_section(
                sec,
                parent_notebook_id=pnb.get("id"),
                parent_section_group_id=psg.get("id"),
            )
            db.upsert_section(conn, norm)

            # Persist parent section group if we have it (best-effort, no name)
            sg_id = psg.get("id")
            if sg_id:
                db.upsert_section_group(conn, {
                    "id": sg_id,
                    "display_name": psg.get("displayName", ""),
                    "parent_notebook_id": pnb.get("id"),
                    "parent_section_group_id": None,
                })

    # Per-notebook progress summary
    for nb_id, secs in by_notebook.items():
        nb_name = notebooks_from_sections.get(nb_id) or "(unknown)"
        print(f"  → {nb_name}: {len(secs)} section(s)", flush=True)

    # Identify notebooks listed at the top level but with no accessible sections
    seen_nb_ids = set(by_notebook.keys())
    for nb in notebooks:
        if nb["id"] not in seen_nb_ids:
            print(
                f"  → {nb.get('displayName')}: no accessible sections "
                f"(likely behind the 5000-item OneNote limit)",
                flush=True,
            )
            _log(
                f"notebook '{nb.get('displayName')}' has no sections reachable "
                "via Graph (error 10008)"
            )

    return notebooks


async def _sync_notebook_structure(client: GraphClient) -> list[dict]:
    """Deprecated: the old top-down enumeration that hits Graph error 10008.

    Retained for reference; the working path is _sync_notebook_structure_flat.
    """
    print("Enumerating notebooks...", flush=True)
    notebooks = await client.get_notebooks()

    with db.get_conn() as conn:
        for nb in notebooks:
            db.upsert_notebook(conn, _normalize_notebook(nb))

    print(f"Found {len(notebooks)} notebook(s)", flush=True)

    for nb in notebooks:
        nb_id = nb["id"]
        nb_name = nb.get("displayName", nb_id)

        # Top-level sections in this notebook
        try:
            sections = await client.get_sections_in_notebook(nb_id)
        except (GraphAccessDenied, GraphNotFound) as exc:
            msg = f"  → {nb_name}: skipped ({exc.__class__.__name__})"
            print(msg, flush=True)
            _log(msg)
            continue

        with db.get_conn() as conn:
            for s in sections:
                db.upsert_section(
                    conn,
                    _normalize_section(
                        s, parent_notebook_id=nb_id, parent_section_group_id=None
                    ),
                )

        # BFS through nested section groups
        try:
            top_groups = await client.get_section_groups_in_notebook(nb_id)
        except (GraphAccessDenied, GraphNotFound) as exc:
            _log(f"section groups denied for '{nb_name}': {exc}")
            top_groups = []

        queue: list[tuple[dict, str | None]] = [(g, None) for g in top_groups]

        with db.get_conn() as conn:
            for g in top_groups:
                db.upsert_section_group(
                    conn,
                    _normalize_section_group(
                        g, parent_notebook_id=nb_id, parent_section_group_id=None
                    ),
                )

        sg_count = len(top_groups)
        sec_count = len(sections)

        while queue:
            group, _parent_sg = queue.pop(0)
            gid = group["id"]

            try:
                sub_sections = await client.get_sections_in_section_group(gid)
            except (GraphAccessDenied, GraphNotFound) as exc:
                _log(f"sections denied for section group {gid}: {exc}")
                sub_sections = []
            with db.get_conn() as conn:
                for s in sub_sections:
                    db.upsert_section(
                        conn,
                        _normalize_section(
                            s,
                            parent_notebook_id=nb_id,
                            parent_section_group_id=gid,
                        ),
                    )
            sec_count += len(sub_sections)

            try:
                sub_groups = await client.get_section_groups_in_section_group(gid)
            except (GraphAccessDenied, GraphNotFound) as exc:
                _log(f"section groups denied under {gid}: {exc}")
                sub_groups = []
            with db.get_conn() as conn:
                for sg in sub_groups:
                    db.upsert_section_group(
                        conn,
                        _normalize_section_group(
                            sg,
                            parent_notebook_id=nb_id,
                            parent_section_group_id=gid,
                        ),
                    )
            queue.extend((sg, gid) for sg in sub_groups)
            sg_count += len(sub_groups)

        print(
            f"  → {nb_name}: {sec_count} section(s), {sg_count} section group(s)",
            flush=True,
        )

    return notebooks


# ── Per-section page metadata sync ────────────────────────────────────────────

async def _full_sync_section_pages(client: GraphClient, section: dict) -> int:
    """Paginate page metadata for one section. Resumable via fully_synced."""
    section_id = section["id"]
    name = section.get("display_name", section_id)

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT fully_synced FROM sections WHERE id = ?", (section_id,)
        ).fetchone()
        if row and row["fully_synced"]:
            return 0

    count = 0
    page_num = 0
    try:
        async for pages in client.iter_pages_in_section(section_id):
            if not pages:
                break
            with db.get_conn() as conn:
                for p in pages:
                    norm = _normalize_page(p)
                    if not norm["section_id"]:
                        norm["section_id"] = section_id
                    if not norm["notebook_id"]:
                        norm["notebook_id"] = section.get("parent_notebook_id")
                    db.upsert_page(conn, norm)
            count += len(pages)
            page_num += 1
            print(f"     {name}: {count} pages (batch {page_num})", flush=True)
    except (GraphAccessDenied, GraphNotFound) as exc:
        msg = f"     {name}: skipped ({exc.__class__.__name__})"
        print(msg, flush=True)
        _log(msg)
        # Mark synced so we don't keep retrying a forbidden section forever.
        with db.get_conn() as conn:
            db.set_section_synced(conn, section_id)
        return count

    with db.get_conn() as conn:
        db.set_section_synced(conn, section_id)

    print(f"     {name}: done ({count} pages)", flush=True)
    return count


# ── Full sync ─────────────────────────────────────────────────────────────────

async def run_full_sync(get_token: Callable[[], str]) -> None:
    """Full OneNote sync. Resumable across restarts via sections.fully_synced."""
    global _last_sync_error, _last_sync_time

    async with _sync_lock:
        _log("Full sync started")
        print("\n" + "=" * 56)
        print("  FULL ONENOTE SYNC — this may take several minutes")
        print("=" * 56 + "\n", flush=True)

        try:
            async with GraphClient(get_token) as client:
                await _sync_notebook_structure_flat(client)

                unsynced = db.get_unsynced_sections()
                total_sections = len(db.get_all_sections())
                skipped = total_sections - len(unsynced)

                print(
                    f"\nSyncing pages in {len(unsynced)} section(s) "
                    f"({skipped} already complete, skipping)...\n",
                    flush=True,
                )

                total_new = 0
                for section in unsynced:
                    try:
                        total_new += await _full_sync_section_pages(client, section)
                    except Exception as exc:
                        msg = f"Error syncing section '{section.get('display_name')}': {exc}"
                        logger.error(msg)
                        _log(msg)
                        _last_sync_error = str(exc)

            _last_sync_time = datetime.now().isoformat()
            db.set_sync_state("last_full_sync", _last_sync_time)
            counts = db.get_total_counts()

            print(f"\n{'=' * 56}")
            print(
                f"  Full sync complete: {counts['notebooks']} notebook(s), "
                f"{counts['sections']} section(s), {counts['pages']} page(s)"
            )
            print(f"{'=' * 56}\n", flush=True)
            _log(
                f"Full sync complete: {counts['notebooks']} nb, "
                f"{counts['sections']} sec, {counts['pages']} pg"
            )

        except Exception as exc:
            _last_sync_error = str(exc)
            _log(f"Full sync failed: {exc}")
            logger.error("Full sync failed: %s", exc, exc_info=True)
            raise


# ── Delta sync (newest-first walk) ────────────────────────────────────────────

async def run_delta_sync(get_token: Callable[[], str]) -> int:
    """Walk recent pages newest-first until we hit pages older than last delta."""
    global _last_sync_error, _last_sync_time

    if _sync_lock.locked():
        logger.debug("Sync already in progress, skipping delta")
        return 0

    async with _sync_lock:
        _log("Delta sync started")

        last_delta = db.get_sync_state("last_delta_sync_cutoff") or ""
        total_changes = 0
        new_cutoff: str | None = None

        try:
            async with GraphClient(get_token) as client:
                # The flat /me/onenote/pages endpoint returns Graph error 20266
                # for users with >5000 OneNote items in OneDrive (same root cause
                # as the 10008 we work around in full sync). So we walk per
                # known section instead, stopping each walk when we hit pages
                # older than the cutoff.
                sections = db.get_all_sections()
                for section in sections:
                    section_id = section["id"]
                    section_done = False
                    try:
                        async for batch in client.iter_pages_in_section(section_id):
                            if not batch:
                                break
                            with db.get_conn() as conn:
                                for p in batch:
                                    mod = p.get("lastModifiedDateTime", "") or ""
                                    if new_cutoff is None or mod > new_cutoff:
                                        new_cutoff = mod
                                    if last_delta and mod and mod <= last_delta:
                                        section_done = True
                                        break
                                    norm = _normalize_page(p)
                                    if not norm["section_id"]:
                                        norm["section_id"] = section_id
                                    if not norm["notebook_id"]:
                                        norm["notebook_id"] = section.get("parent_notebook_id")
                                    db.upsert_page(conn, norm)
                                    total_changes += 1
                            if section_done:
                                break
                    except (GraphAccessDenied, GraphNotFound) as exc:
                        _log(f"delta: skipping section {section_id}: {exc}")
                        continue

            _last_sync_time = datetime.now().isoformat()
            db.set_sync_state("last_delta_sync", _last_sync_time)
            if new_cutoff:
                db.set_sync_state("last_delta_sync_cutoff", new_cutoff)

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


# ── Lightweight structure re-enum ────────────────────────────────────────────

async def run_structure_sync(get_token: Callable[[], str]) -> None:
    """Re-enumerate notebooks/sections/section groups (no pages)."""
    if _sync_lock.locked():
        logger.debug("Sync already in progress, skipping structure")
        return

    async with _sync_lock:
        _log("Structure sync started")
        try:
            async with GraphClient(get_token) as client:
                await _sync_notebook_structure_flat(client)
            db.set_sync_state("last_structure_sync", datetime.now().isoformat())
            _log("Structure sync complete")
        except Exception as exc:
            _log(f"Structure sync failed: {exc}")
            logger.error("Structure sync failed: %s", exc, exc_info=True)


# ── Lazy page content fetch ──────────────────────────────────────────────────

async def ensure_page_content(
    page_id: str, get_token: Callable[[], str]
) -> dict | None:
    """Return the page row, fetching/caching content if not yet cached."""
    page = db.get_page(page_id)
    if page and page.get("content_text"):
        return page

    async with GraphClient(get_token) as client:
        try:
            html = await client.get_page_content(page_id)
        except Exception as exc:
            _log(f"Failed to fetch content for {page_id}: {exc}")
            logger.error("Failed to fetch content for %s: %s", page_id, exc)
            return page

        # Make sure the page metadata row exists (it should, but be defensive)
        if not page:
            try:
                meta = await client.get_page(page_id)
                with db.get_conn() as conn:
                    db.upsert_page(conn, _normalize_page(meta))
            except Exception as exc:
                logger.error("Failed to fetch page metadata for %s: %s", page_id, exc)
                return None

    text = _strip_html(html)
    with db.get_conn() as conn:
        db.update_page_content(conn, page_id, html, text)

    return db.get_page(page_id)


# ── Status ────────────────────────────────────────────────────────────────────

def get_sync_status() -> dict:
    counts = db.get_total_counts()
    return {
        "last_full_sync": db.get_sync_state("last_full_sync"),
        "last_delta_sync": db.get_sync_state("last_delta_sync"),
        "last_structure_sync": db.get_sync_state("last_structure_sync"),
        "last_sync_time": _last_sync_time,
        "notebooks": counts["notebooks"],
        "sections": counts["sections"],
        "pages": counts["pages"],
        "pages_with_content": counts["pages_with_content"],
        "last_error": _last_sync_error,
        "sync_in_progress": _sync_lock.locked(),
    }


# ── Logging helper ────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_PATH, "a") as fh:
        fh.write(f"[{ts}] {msg}\n")
