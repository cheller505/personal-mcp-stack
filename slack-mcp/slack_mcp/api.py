"""Slack Web API async client with rate limiting and retries."""

import asyncio
import logging
import time
from collections import deque
from typing import AsyncGenerator, Callable

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://slack.com/api"

_RATE_WINDOW_SECONDS = 60
_RATE_MAX = 50  # defensive global cap; tier-3 endpoints are ~50/min


class SlackError(RuntimeError):
    def __init__(self, error: str):
        super().__init__(error)
        self.error = error


class SlackAuthError(SlackError):
    pass


class SlackRateLimit(SlackError):
    def __init__(self, retry_after: float = 1.0):
        super().__init__("ratelimited")
        self.retry_after = retry_after


_AUTH_ERRORS = {"invalid_auth", "not_authed", "token_revoked", "account_inactive"}


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


class SlackClient:
    def __init__(self, get_token: Callable[[], str]):
        self._get_token = get_token
        self._client: httpx.AsyncClient | None = None
        self._limiter = _RateLimiter(_RATE_MAX, _RATE_WINDOW_SECONDS)

    async def __aenter__(self) -> "SlackClient":
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        return self

    async def __aexit__(self, *_) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/x-www-form-urlencoded",
        }

    async def _request(self, method: str, params: dict | None = None) -> dict:
        assert self._client is not None, "Use as async context manager"
        url = f"{API_BASE}/{method}"

        backoff = 2
        for attempt in range(6):
            await self._limiter.acquire()
            try:
                resp = await self._client.get(url, headers=self._headers(), params=params)
            except httpx.HTTPError as exc:
                if attempt >= 5:
                    raise
                logger.warning("HTTP error (attempt %d) on %s: %s — retry in %ds",
                               attempt + 1, method, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue

            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", backoff))
                logger.warning("Rate limited on %s (attempt %d); waiting %.1fs",
                               method, attempt + 1, wait)
                await asyncio.sleep(wait)
                backoff = min(backoff * 2, 120)
                continue

            if 500 <= resp.status_code < 600:
                if attempt >= 5:
                    resp.raise_for_status()
                logger.warning("Server error %d on %s (attempt %d) — retry in %ds",
                               resp.status_code, method, attempt + 1, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue

            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                err = data.get("error", "unknown")
                if err in _AUTH_ERRORS:
                    raise SlackAuthError(err)
                if err == "ratelimited":
                    # Some endpoints return ok=false ratelimited rather than HTTP 429
                    wait = float(resp.headers.get("Retry-After", backoff))
                    await asyncio.sleep(wait)
                    backoff = min(backoff * 2, 120)
                    continue
                raise SlackError(err)
            return data

        raise RuntimeError(f"Request failed after retries: {method}")

    # ── High-level endpoints ────────────────────────────────────────────────

    async def auth_test(self) -> dict:
        return await self._request("auth.test")

    async def team_info(self) -> dict:
        data = await self._request("team.info")
        return data.get("team", {}) or {}

    async def users_list(self) -> AsyncGenerator[list[dict], None]:
        cursor: str | None = None
        while True:
            params = {"limit": "200"}
            if cursor:
                params["cursor"] = cursor
            data = await self._request("users.list", params=params)
            yield data.get("members", []) or []
            cursor = (data.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                return

    async def users_info(self, user_id: str) -> dict:
        data = await self._request("users.info", params={"user": user_id})
        return data.get("user", {}) or {}

    async def conversations_list(
        self, types: str = "public_channel,private_channel,mpim,im"
    ) -> AsyncGenerator[list[dict], None]:
        cursor: str | None = None
        while True:
            params = {
                "types": types,
                "limit": "200",
                "exclude_archived": "false",
            }
            if cursor:
                params["cursor"] = cursor
            data = await self._request("conversations.list", params=params)
            yield data.get("channels", []) or []
            cursor = (data.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                return

    async def conversations_info(self, channel_id: str) -> dict:
        data = await self._request("conversations.info", params={"channel": channel_id})
        return data.get("channel", {}) or {}

    async def iter_history(
        self, channel_id: str, oldest: str | None = None, latest: str | None = None
    ) -> AsyncGenerator[list[dict], None]:
        cursor: str | None = None
        while True:
            params: dict = {"channel": channel_id, "limit": "200"}
            if oldest:
                params["oldest"] = oldest
            if latest:
                params["latest"] = latest
            if cursor:
                params["cursor"] = cursor
            data = await self._request("conversations.history", params=params)
            yield data.get("messages", []) or []
            if not data.get("has_more"):
                return
            cursor = (data.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                return

    async def iter_replies(
        self, channel_id: str, thread_ts: str, oldest: str | None = None
    ) -> AsyncGenerator[list[dict], None]:
        cursor: str | None = None
        while True:
            params: dict = {
                "channel": channel_id,
                "ts": thread_ts,
                "limit": "200",
            }
            if oldest:
                params["oldest"] = oldest
            if cursor:
                params["cursor"] = cursor
            data = await self._request("conversations.replies", params=params)
            yield data.get("messages", []) or []
            if not data.get("has_more"):
                return
            cursor = (data.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                return
