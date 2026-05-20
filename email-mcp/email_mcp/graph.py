"""Microsoft Graph API async HTTP client with rate-limit handling."""

import asyncio
import logging
from typing import AsyncGenerator, Callable
from urllib.parse import parse_qs, urlparse

import aiohttp

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

_MSG_SELECT = (
    "id,conversationId,from,toRecipients,ccRecipients,"
    "subject,body,receivedDateTime,isRead,hasAttachments,importance,isDraft"
)
_MSG_EXPAND = "attachments($select=name,isInline)"


class GraphClient:
    def __init__(self, get_token: Callable[[], str]):
        self._get_token = get_token
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "GraphClient":
        self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *_) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
        }

    async def _request(self, method: str, url: str, **kwargs) -> dict:
        assert self._session is not None, "Use as async context manager"
        backoff = 2
        for attempt in range(6):
            try:
                async with self._session.request(
                    method, url, headers=self._headers(), **kwargs
                ) as resp:
                    if resp.status in (429, 503):
                        wait = int(resp.headers.get("Retry-After", backoff))
                        logger.warning("Rate limited (attempt %d); waiting %ds", attempt + 1, wait)
                        await asyncio.sleep(wait)
                        backoff = min(backoff * 2, 120)
                        continue

                    if resp.status == 401:
                        logger.info("Got 401 on attempt %d — token may have just expired", attempt + 1)
                        await asyncio.sleep(2)
                        continue

                    resp.raise_for_status()

                    if resp.status == 204:
                        return {}
                    return await resp.json()

            except aiohttp.ClientError as exc:
                if attempt >= 5:
                    raise
                wait = backoff
                logger.warning("Request error (attempt %d): %s — retrying in %ds", attempt + 1, exc, wait)
                await asyncio.sleep(wait)
                backoff = min(backoff * 2, 60)

        raise RuntimeError(f"Request failed after all retries: {method} {url}")

    async def get(self, path: str, params: dict | None = None) -> dict:
        url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
        return await self._request("GET", url, params=params)

    async def post(self, path: str, body: dict) -> dict:
        return await self._request("POST", f"{GRAPH_BASE}{path}", json=body)

    async def patch(self, path: str, body: dict) -> dict:
        return await self._request("PATCH", f"{GRAPH_BASE}{path}", json=body)

    # ── Folder enumeration ──────────────────────────────────────────────────

    async def get_all_folders(self) -> list[dict]:
        """Recursively enumerate all mail folders."""
        root: list[dict] = []
        url: str | None = f"{GRAPH_BASE}/me/mailFolders"
        params: dict | None = {"$top": "100", "includeHiddenFolders": "true"}

        while url:
            data = await self._request("GET", url, params=params)
            params = None
            root.extend(data.get("value", []))
            url = data.get("@odata.nextLink")

        all_folders = list(root)
        queue = [f for f in root if f.get("childFolderCount", 0) > 0]

        while queue:
            parent = queue.pop(0)
            url = f"{GRAPH_BASE}/me/mailFolders/{parent['id']}/childFolders"
            params = {"$top": "100"}
            while url:
                data = await self._request("GET", url, params=params)
                params = None
                children = data.get("value", [])
                all_folders.extend(children)
                queue.extend(c for c in children if c.get("childFolderCount", 0) > 0)
                url = data.get("@odata.nextLink")

        return all_folders

    # ── Message pagination ──────────────────────────────────────────────────

    async def iter_folder_messages(self, folder_id: str) -> AsyncGenerator[list[dict], None]:
        """Yield pages of messages for a folder (full content)."""
        url: str | None = f"{GRAPH_BASE}/me/mailFolders/{folder_id}/messages"
        params: dict | None = {
            "$top": "999",
            "$select": _MSG_SELECT,
            "$expand": _MSG_EXPAND,
            "$orderby": "receivedDateTime desc",
        }
        while url:
            data = await self._request("GET", url, params=params)
            params = None
            yield data.get("value", [])
            url = data.get("@odata.nextLink")

    # ── Delta sync ──────────────────────────────────────────────────────────

    async def get_folder_delta(
        self, folder_id: str, delta_token: str | None = None
    ) -> tuple[list[dict], str | None]:
        """Return (changes, new_delta_token) for a folder since the last delta token."""
        if delta_token:
            url: str | None = f"{GRAPH_BASE}/me/mailFolders/{folder_id}/messages/delta"
            # Graph emits the param as lowercase $deltatoken in @odata.deltaLink
            # but accepts either casing on input. Use lowercase for consistency.
            params: dict | None = {"$deltatoken": delta_token}
        else:
            url = f"{GRAPH_BASE}/me/mailFolders/{folder_id}/messages/delta"
            params = {
                "$top": "999",
                "$select": _MSG_SELECT,
                "$expand": _MSG_EXPAND,
            }

        changes: list[dict] = []
        new_delta_token: str | None = None

        while url:
            data = await self._request("GET", url, params=params)
            params = None
            changes.extend(data.get("value", []))

            delta_link = data.get("@odata.deltaLink")
            if delta_link:
                qs = parse_qs(urlparse(delta_link).query)
                # Graph returns $deltatoken (lowercase), but accept either.
                tokens = qs.get("$deltatoken", []) or qs.get("$deltaToken", [])
                new_delta_token = tokens[0] if tokens else None
                break
            url = data.get("@odata.nextLink")

        return changes, new_delta_token

    # ── Draft creation ──────────────────────────────────────────────────────

    async def create_draft(
        self,
        to: list[str],
        subject: str,
        body: str,
        body_type: str = "Text",
        cc: list[str] | None = None,
        reply_to_id: str | None = None,
    ) -> dict:
        if reply_to_id:
            draft = await self.post(f"/me/messages/{reply_to_id}/createReply", {})
            draft_id = draft["id"]
            update: dict = {
                "toRecipients": [{"emailAddress": {"address": a}} for a in to],
                "body": {"contentType": body_type, "content": body},
            }
            if cc:
                update["ccRecipients"] = [{"emailAddress": {"address": a}} for a in cc]
            return await self.patch(f"/me/messages/{draft_id}", update)

        message: dict = {
            "subject": subject,
            "body": {"contentType": body_type, "content": body},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to],
        }
        if cc:
            message["ccRecipients"] = [{"emailAddress": {"address": a}} for a in cc]
        return await self.post("/me/messages", message)
