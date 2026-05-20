"""Microsoft Graph API async HTTP client for OneNote, with rate-limit handling."""

import asyncio
import logging
from typing import AsyncGenerator, Callable

import aiohttp

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class GraphAccessDenied(Exception):
    """403 from Graph — permission denied on this resource. Not retryable."""

    def __init__(self, url: str, message: str = ""):
        self.url = url
        super().__init__(f"403 Forbidden: {url} {message}".strip())


class GraphNotFound(Exception):
    """404 from Graph — resource missing/deleted. Not retryable."""

    def __init__(self, url: str):
        self.url = url
        super().__init__(f"404 Not Found: {url}")

_NOTEBOOK_SELECT = (
    "id,displayName,isDefault,isShared,createdDateTime,lastModifiedDateTime,"
    "userRole,sectionsUrl,sectionGroupsUrl,createdBy,lastModifiedBy"
)
_SECTION_SELECT = (
    "id,displayName,createdDateTime,lastModifiedDateTime,pagesUrl,"
    "createdBy,lastModifiedBy"
)
_SECTION_GROUP_SELECT = (
    "id,displayName,parentNotebook,parentSectionGroup,sectionsUrl,sectionGroupsUrl"
)
_PAGE_SELECT = (
    "id,title,createdDateTime,lastModifiedDateTime,contentUrl,"
    "parentSection,parentNotebook,level,order"
)
_PAGE_SELECT_FULL = (
    "id,title,createdDateTime,lastModifiedDateTime,contentUrl,"
    "parentSection,parentNotebook,level,order,createdBy,lastModifiedBy"
)


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

    def _headers(self, content_type: str = "application/json") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": content_type,
        }

    async def _request(
        self,
        method: str,
        url: str,
        *,
        return_text: bool = False,
        headers: dict | None = None,
        **kwargs,
    ):
        assert self._session is not None, "Use as async context manager"
        backoff = 2
        for attempt in range(6):
            try:
                req_headers = headers if headers is not None else self._headers()
                async with self._session.request(
                    method, url, headers=req_headers, **kwargs
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

                    if resp.status == 403:
                        body = await resp.text()
                        raise GraphAccessDenied(url, body[:200])

                    if resp.status == 404:
                        raise GraphNotFound(url)

                    # 4xx (other than 401 which we retry once) is terminal —
                    # retrying won't fix a bad request or quota error.
                    if 400 <= resp.status < 500:
                        body = await resp.text()
                        logger.warning(
                            "Graph %d on %s: %s", resp.status, url, body[:300]
                        )
                        raise GraphAccessDenied(url, f"HTTP {resp.status}: {body[:200]}")

                    resp.raise_for_status()

                    if resp.status == 204:
                        return "" if return_text else {}
                    if return_text:
                        return await resp.text()
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

    async def get_text(self, path: str, params: dict | None = None) -> str:
        url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
        return await self._request("GET", url, params=params, return_text=True)

    async def post(self, path: str, body: dict) -> dict:
        return await self._request("POST", f"{GRAPH_BASE}{path}", json=body)

    async def patch(self, path: str, body) -> dict:
        return await self._request("PATCH", f"{GRAPH_BASE}{path}", json=body)

    async def _paginate(self, url: str, params: dict | None = None) -> list[dict]:
        items: list[dict] = []
        next_url: str | None = url
        next_params = params
        while next_url:
            data = await self._request("GET", next_url, params=next_params)
            next_params = None
            items.extend(data.get("value", []))
            next_url = data.get("@odata.nextLink")
        return items

    # ── Notebooks ───────────────────────────────────────────────────────────

    async def get_notebooks(self) -> list[dict]:
        return await self._paginate(
            f"{GRAPH_BASE}/me/onenote/notebooks",
            params={"$select": _NOTEBOOK_SELECT, "$top": "100"},
        )

    # ── Sections / section groups ───────────────────────────────────────────

    async def get_all_sections(self) -> list[dict]:
        """Flat list of ALL accessible sections with expanded parent notebook.

        This bypasses the per-notebook /sections endpoint, which fails with
        Graph error 10008 when the user's OneDrive holds >5000 OneNote items.
        """
        return await self._paginate(
            f"{GRAPH_BASE}/me/onenote/sections",
            params={
                "$expand": "parentNotebook,parentSectionGroup",
                "$top": "100",
            },
        )

    async def get_sections_in_notebook(self, notebook_id: str) -> list[dict]:
        return await self._paginate(
            f"{GRAPH_BASE}/me/onenote/notebooks/{notebook_id}/sections",
            params={"$select": _SECTION_SELECT, "$top": "100"},
        )

    async def get_section_groups_in_notebook(self, notebook_id: str) -> list[dict]:
        return await self._paginate(
            f"{GRAPH_BASE}/me/onenote/notebooks/{notebook_id}/sectionGroups",
            params={"$select": _SECTION_GROUP_SELECT, "$top": "100"},
        )

    async def get_sections_in_section_group(self, group_id: str) -> list[dict]:
        return await self._paginate(
            f"{GRAPH_BASE}/me/onenote/sectionGroups/{group_id}/sections",
            params={"$select": _SECTION_SELECT, "$top": "100"},
        )

    async def get_section_groups_in_section_group(self, group_id: str) -> list[dict]:
        return await self._paginate(
            f"{GRAPH_BASE}/me/onenote/sectionGroups/{group_id}/sectionGroups",
            params={"$select": _SECTION_GROUP_SELECT, "$top": "100"},
        )

    # ── Pages ───────────────────────────────────────────────────────────────

    async def iter_pages_in_section(
        self, section_id: str
    ) -> AsyncGenerator[list[dict], None]:
        """Yield pages of page metadata for a section."""
        url: str | None = f"{GRAPH_BASE}/me/onenote/sections/{section_id}/pages"
        params: dict | None = {
            "$select": _PAGE_SELECT,
            "$orderby": "lastModifiedDateTime desc",
            "$top": "100",
        }
        while url:
            data = await self._request("GET", url, params=params)
            params = None
            yield data.get("value", [])
            url = data.get("@odata.nextLink")

    async def iter_recent_pages(
        self, top: int = 100
    ) -> AsyncGenerator[list[dict], None]:
        """Yield pages of recent page metadata across all notebooks."""
        url: str | None = f"{GRAPH_BASE}/me/onenote/pages"
        params: dict | None = {
            "$select": _PAGE_SELECT,
            "$orderby": "lastModifiedDateTime desc",
            "$top": str(top),
        }
        while url:
            data = await self._request("GET", url, params=params)
            params = None
            yield data.get("value", [])
            url = data.get("@odata.nextLink")

    async def get_page(self, page_id: str) -> dict:
        return await self.get(
            f"/me/onenote/pages/{page_id}",
            params={"$select": _PAGE_SELECT_FULL},
        )

    async def get_page_content(self, page_id: str) -> str:
        """GET /pages/{id}/content — returns HTML, not JSON."""
        return await self.get_text(f"/me/onenote/pages/{page_id}/content")

    # ── Page mutations ──────────────────────────────────────────────────────

    async def create_page(
        self,
        section_id: str,
        html: str,
        content_type: str = "text/html",
    ) -> dict:
        url = f"{GRAPH_BASE}/me/onenote/sections/{section_id}/pages"
        headers = {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": content_type,
        }
        return await self._request(
            "POST",
            url,
            headers=headers,
            data=html.encode("utf-8"),
        )

    async def update_page_content(
        self, page_id: str, commands: list[dict]
    ) -> dict:
        """PATCH /pages/{id}/content — body is a JSON array of OneNote commands."""
        url = f"{GRAPH_BASE}/me/onenote/pages/{page_id}/content"
        return await self._request("PATCH", url, json=commands)
