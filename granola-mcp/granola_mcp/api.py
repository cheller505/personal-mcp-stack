"""Granola REST API async client with rate limiting and retries."""

import asyncio
import logging
import time
from collections import deque
from typing import AsyncIterator, Callable

import httpx

from .auth import InvalidAPIKeyError

logger = logging.getLogger(__name__)

API_BASE = "https://public-api.granola.ai/v1"

# Rate limits per Granola docs: 25 req / 5s burst, 5 req/s sustained (300/min).
# Stay safely under both.
_BURST_WINDOW = 5.0
_BURST_MAX = 24
_SUSTAINED_WINDOW = 60.0
_SUSTAINED_MAX = 290


class _RateLimiter:
    """Sliding-window limiter enforcing both burst and sustained caps."""

    def __init__(self) -> None:
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                # Drop calls older than the larger window
                while self._calls and self._calls[0] <= now - _SUSTAINED_WINDOW:
                    self._calls.popleft()

                burst_count = sum(1 for t in self._calls if t > now - _BURST_WINDOW)
                sustained_count = len(self._calls)

                waits: list[float] = []
                if burst_count >= _BURST_MAX:
                    # Find the oldest call inside the burst window
                    for t in self._calls:
                        if t > now - _BURST_WINDOW:
                            waits.append(_BURST_WINDOW - (now - t))
                            break
                if sustained_count >= _SUSTAINED_MAX:
                    waits.append(_SUSTAINED_WINDOW - (now - self._calls[0]))

                if not waits:
                    self._calls.append(now)
                    return

                wait = max(waits) + 0.05
                logger.debug("Rate limiter sleeping %.2fs (burst=%d, sustained=%d)",
                             wait, burst_count, sustained_count)
                await asyncio.sleep(wait)


class GranolaClient:
    def __init__(self, get_key: Callable[[], str]):
        self._get_key = get_key
        self._client: httpx.AsyncClient | None = None
        self._limiter = _RateLimiter()

    async def __aenter__(self) -> "GranolaClient":
        self._client = httpx.AsyncClient(timeout=60.0)
        return self

    async def __aexit__(self, *_) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_key()}",
            "Accept": "application/json",
        }

    async def _request(self, method: str, path: str, *, params: dict | None = None,
                       allow_404: bool = False) -> dict | None:
        assert self._client is not None, "Use as async context manager"
        url = path if path.startswith("http") else f"{API_BASE}{path}"

        backoff = 2
        for attempt in range(6):
            await self._limiter.acquire()
            try:
                resp = await self._client.request(
                    method, url, headers=self._headers(), params=params
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
                raise InvalidAPIKeyError(
                    "Granola API key rejected (401) — update ~/.granola-mcp/config.json and restart"
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

    # ── High-level endpoints ────────────────────────────────────────────────

    async def validate(self) -> int | None:
        data = await self.get("/notes", params={"page_size": 1})
        notes = (data or {}).get("notes") or []
        if (data or {}).get("hasMore"):
            return None
        return len(notes)

    async def iter_folders(self) -> AsyncIterator[dict]:
        cursor: str | None = None
        while True:
            params: dict = {"page_size": 30}
            if cursor:
                params["cursor"] = cursor
            data = await self.get("/folders", params=params) or {}
            for f in data.get("folders") or []:
                yield f
            if not data.get("hasMore"):
                return
            cursor = data.get("cursor")
            if not cursor:
                return

    async def iter_notes(
        self,
        updated_after: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
    ) -> AsyncIterator[dict]:
        cursor: str | None = None
        while True:
            params: dict = {"page_size": 30}
            if updated_after:
                params["updated_after"] = updated_after
            if created_after:
                params["created_after"] = created_after
            if created_before:
                params["created_before"] = created_before
            if cursor:
                params["cursor"] = cursor
            data = await self.get("/notes", params=params) or {}
            for n in data.get("notes") or []:
                yield n
            if not data.get("hasMore"):
                return
            cursor = data.get("cursor")
            if not cursor:
                return

    async def get_note(self, note_id: str, include_transcript: bool = False) -> dict | None:
        params = {"include": "transcript"} if include_transcript else None
        return await self.get(f"/notes/{note_id}", params=params, allow_404=True)
