"""ClickUp REST API async client with rate limiting and retries."""

import asyncio
import logging
import time
from collections import deque
from typing import Callable

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.clickup.com/api/v2"

_RATE_WINDOW_SECONDS = 60
_RATE_MAX = 95  # stay safely under 100/min


class _RateLimiter:
    def __init__(self, max_calls: int, window: float):
        self._max = max_calls
        self._window = window
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            while self._calls and self._calls[0] <= now - self._window:
                self._calls.popleft()
            if len(self._calls) >= self._max:
                wait = self._window - (now - self._calls[0])
                if wait > 0:
                    logger.debug("Rate limiter sleeping %.2fs", wait)
                    await asyncio.sleep(wait)
                now = time.monotonic()
                while self._calls and self._calls[0] <= now - self._window:
                    self._calls.popleft()
            self._calls.append(time.monotonic())


class ClickUpClient:
    def __init__(self, get_token: Callable[[], str]):
        self._get_token = get_token
        self._client: httpx.AsyncClient | None = None
        self._limiter = _RateLimiter(_RATE_MAX, _RATE_WINDOW_SECONDS)

    async def __aenter__(self) -> "ClickUpClient":
        self._client = httpx.AsyncClient(timeout=60.0)
        return self

    async def __aexit__(self, *_) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": self._get_token(),
            "Content-Type": "application/json",
        }

    async def _request(self, method: str, path: str, *, params: dict | None = None,
                       json_body: dict | None = None, allow_404: bool = False) -> dict | None:
        assert self._client is not None, "Use as async context manager"
        url = path if path.startswith("http") else f"{API_BASE}{path}"

        backoff = 2
        for attempt in range(6):
            await self._limiter.acquire()
            try:
                resp = await self._client.request(
                    method, url, headers=self._headers(), params=params, json=json_body
                )
            except httpx.HTTPError as exc:
                if attempt >= 5:
                    raise
                logger.warning("HTTP error (attempt %d) on %s %s: %s — retry in %ds",
                               attempt + 1, method, url, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue

            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", backoff))
                logger.warning("Rate limited (attempt %d); waiting %ds", attempt + 1, wait)
                await asyncio.sleep(wait)
                backoff = min(backoff * 2, 120)
                continue

            if resp.status_code == 401:
                raise RuntimeError(
                    "Token is invalid — delete ~/.clickup-mcp/config.json and restart"
                )

            if resp.status_code == 404 and allow_404:
                return None

            if 500 <= resp.status_code < 600:
                if attempt >= 5:
                    resp.raise_for_status()
                logger.warning("Server error %d on %s %s (attempt %d) — retry in %ds",
                               resp.status_code, method, url, attempt + 1, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue

            resp.raise_for_status()
            if resp.status_code == 204 or not resp.content:
                return {}
            return resp.json()

        raise RuntimeError(f"Request failed after retries: {method} {url}")

    async def get(self, path: str, params: dict | None = None, allow_404: bool = False) -> dict | None:
        return await self._request("GET", path, params=params, allow_404=allow_404)

    async def post(self, path: str, body: dict) -> dict:
        result = await self._request("POST", path, json_body=body)
        return result or {}

    async def put(self, path: str, body: dict) -> dict:
        result = await self._request("PUT", path, json_body=body)
        return result or {}

    # ── High-level endpoints ────────────────────────────────────────────────

    async def get_user(self) -> dict:
        data = await self.get("/user")
        return (data or {}).get("user", {})

    async def get_workspaces(self) -> list[dict]:
        data = await self.get("/team")
        return (data or {}).get("teams", [])

    async def get_spaces(self, workspace_id: str) -> list[dict]:
        out: list[dict] = []
        for archived in ("false", "true"):
            data = await self.get(f"/team/{workspace_id}/space", params={"archived": archived})
            for sp in (data or {}).get("spaces", []):
                sp["_archived_flag"] = archived == "true"
                out.append(sp)
        return out

    async def get_folders(self, space_id: str) -> list[dict]:
        out: list[dict] = []
        for archived in ("false", "true"):
            data = await self.get(f"/space/{space_id}/folder", params={"archived": archived})
            for f in (data or {}).get("folders", []):
                f["_archived_flag"] = archived == "true"
                out.append(f)
        return out

    async def get_folderless_lists(self, space_id: str) -> list[dict]:
        out: list[dict] = []
        for archived in ("false", "true"):
            data = await self.get(f"/space/{space_id}/list", params={"archived": archived})
            for lst in (data or {}).get("lists", []):
                lst["_archived_flag"] = archived == "true"
                out.append(lst)
        return out

    async def get_lists(self, folder_id: str) -> list[dict]:
        out: list[dict] = []
        for archived in ("false", "true"):
            data = await self.get(f"/folder/{folder_id}/list", params={"archived": archived})
            for lst in (data or {}).get("lists", []):
                lst["_archived_flag"] = archived == "true"
                out.append(lst)
        return out

    async def get_tasks(self, list_id: str, page: int = 0, date_updated_gt: int | None = None,
                        include_closed: bool = True, archived: bool = False) -> list[dict]:
        params: dict = {
            "page": str(page),
            "include_closed": "true" if include_closed else "false",
            "subtasks": "true",
            "archived": "true" if archived else "false",
        }
        if date_updated_gt is not None:
            params["date_updated_gt"] = str(date_updated_gt)
        data = await self.get(f"/list/{list_id}/task", params=params)
        return (data or {}).get("tasks", [])

    async def get_task(self, task_id: str) -> dict | None:
        data = await self.get(f"/task/{task_id}", params={"include_subtasks": "true"}, allow_404=True)
        return data

    async def get_task_comments(self, task_id: str) -> list[dict]:
        data = await self.get(f"/task/{task_id}/comment", allow_404=True)
        return (data or {}).get("comments", [])

    async def get_members(self, workspace_id: str) -> list[dict]:
        """Members for a workspace. ClickUp returns members nested in /team."""
        data = await self.get("/team")
        out: list[dict] = []
        for team in (data or {}).get("teams", []):
            if str(team.get("id")) != str(workspace_id):
                continue
            for m in team.get("members", []):
                user = m.get("user") or {}
                if user:
                    out.append(user)
        return out

    async def create_task(self, list_id: str, payload: dict) -> dict:
        return await self.post(f"/list/{list_id}/task", payload)

    async def update_task(self, task_id: str, payload: dict) -> dict:
        return await self.put(f"/task/{task_id}", payload)

    async def add_comment(self, task_id: str, comment_text: str, notify_all: bool = False) -> dict:
        return await self.post(
            f"/task/{task_id}/comment",
            {"comment_text": comment_text, "notify_all": notify_all},
        )
